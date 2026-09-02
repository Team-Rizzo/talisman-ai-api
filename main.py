#!/usr/bin/env python3
"""
Alpharidge AI API - FastAPI Application

This API provides endpoints for validators to:
- Get unscored tweets for scoring
- Submit rewards, penalties, and completed tweets
- Manage blacklisted hotkeys

Only validators with valid signatures are allowed to access the API.
"""

import asyncio
import os
import math
import json
import logging
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone


# Blocked hotkeys - silently reject requests from these addresses
# Configure via BLOCKED_HOTKEYS env var (comma-separated ss58 addresses)
BLOCKED_HOTKEYS: set[str] = set(
    filter(None, os.getenv("BLOCKED_HOTKEYS", "").split(","))
)

# Tweet allowlist — when set, only these validators receive tweets.
# Configure via TWEET_ALLOWLIST env var (comma-separated ss58 addresses).
# Leave empty to allow all validators.
TWEET_ALLOWLIST: set[str] = set(
    filter(None, os.getenv("TWEET_ALLOWLIST", "").split(","))
)


class SuppressV2LogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "/v2/" in message or '"/v2 ' in message:
            return False
        return True


class SuppressBlockedHotkeyLogFilter(logging.Filter):
    """Suppress log messages from blocked hotkeys to reduce spam."""
    def filter(self, record: logging.LogRecord) -> bool:
        if not BLOCKED_HOTKEYS:
            return True
        message = record.getMessage()
        # Suppress logs that mention any blocked hotkey
        for hotkey in BLOCKED_HOTKEYS:
            if hotkey in message:
                return False
        return True


class SuppressBlockedRequestsFilter(logging.Filter):
    """Suppress uvicorn access logs for 403 Forbidden responses."""
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        # Suppress all 403 Forbidden access logs
        if '" 403 Forbidden' in message or '" 403 ' in message:
            return False
        return True


from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from prisma import Prisma, Json

import httpx

# Local imports
from models import (
    MechanismProfileResponse,
    MechanismAnchorResponse,
    ControllerProposalSubmission,
    Tweet, TweetWithAuthor, Account, TweetAnalysis,
    Scoring, ScoringUpdate,
    Penalty, PenaltyCreate, PenaltyBulkCreate,
    Reward, RewardCreate, RewardBulkCreate,
    BlacklistedHotkey, BlacklistedHotkeyCreate, BlacklistedHotkeyBulkCreate,
    TweetsForScoringResponse, CompletedTweetsSubmission,
    SubmissionResponse, ErrorResponse, TaoPriceResponse,
    AxonCheckRequest, AxonCheckResponse,
    ReputationSnapshotRequest,
    # Telegram models
    TelegramGroup, TelegramMessage, TelegramMessageAnalysis,
    TelegramMessageWithContext, TelegramMessageForScoring,
    TelegramMessagesForScoringResponse, CompletedTelegramMessagesSubmission,
    # News article models
    NewsArticleForScoring, NewsArticlesForScoringResponse,
    CompletedNewsArticlesSubmission,
    # Attestation / verdict models
    AttestationResponse, VerdictsResponse, VerdictLeaf, BroadcastReportCreate,
    # Penalty detail (display-only dashboard attribution)
    PenaltyDetailBulkCreate,
    # Adaptive-dispatch status (display-only dashboard attribution)
    DispatchStatusBulkCreate,
    # Per-miner dispatch/cooldown event log (display-only dashboard attribution)
    MinerEventBulkCreate,
)
import dispatch_status_store
from utils import mechanism_profile as mprofile
from utils.auth import (
    AuthRequest,
    auth_config,
    extract_auth_from_headers,
    verify_auth_request,
)
import hotkey_whitelist
from hotkey_whitelist import (
    is_validator_hotkey,
    initialize_whitelists,
)
import verification as v
from utils import attestation_crypto as ac
# Reuse the dashboard's localhost-only guard (allowlist incl. DASHBOARD_ALLOWED_IPS env).
# Safe to import at module top: dashboard_routes only imports main lazily inside functions.
from dashboard_routes import _require_local

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Apply log filters to suppress blocked hotkey spam
logging.getLogger("utils.auth").addFilter(SuppressBlockedHotkeyLogFilter())
logger.addFilter(SuppressBlockedHotkeyLogFilter())

# Apply filters to uvicorn access logs
uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.addFilter(SuppressV2LogFilter())
uvicorn_access_logger.addFilter(SuppressBlockedHotkeyLogFilter())
uvicorn_access_logger.addFilter(SuppressBlockedRequestsFilter())

# Initialize Prisma clients
# http timeout must exceed the slowest raw query we run: the dashboard miner
# leaderboard aggregation (dashboard_routes._MINERS_AGG_SQL) scans every
# analysis row and currently takes ~45s; the default 30s made it unservable.
prisma = Prisma(http={"timeout": 180})

# Optional separate database for price data
PRICE_DATABASE_URL = os.getenv("PRICE_DATABASE_URL", "")
price_prisma: Optional[Prisma] = None  # Will be set if PRICE_DATABASE_URL is configured

# Feature gate for news article scoring
SERVE_NEWS_ARTICLES = os.getenv("SERVE_NEWS_ARTICLES", "false").lower() == "true"
# Feature gate for telegram scoring. OFF -> /telegram/messages/unscored returns
# empty so validators dispatch no telegram batches (article-only mode). Avoids
# telegram failures generating penalties that zero miner rewards.
SERVE_TELEGRAM = os.getenv("SERVE_TELEGRAM", "true").lower() == "true"
# Include each article's raw HTML in the /articles/unscored response so miners
# and validators can run trafilatura on the real DOM. OFF by default: raw HTML
# is large and rides in the miner synapse. Both sides analyze the same article
# object, so enabling this stays deterministic.
SERVE_RAW_HTML = os.getenv("SERVE_RAW_HTML", "false").lower() == "true"

# Hard guard against title/summary-only analysis: an article must carry a real
# body of at least this many characters before it is eligible to be leased for
# scoring. This guarantees the miner never runs its full analysis (NER + 2 LLM
# calls + embeddings) on a headline/summary alone (e.g. RSS rows whose body fetch
# failed). Mirrors MIN_BODY_CHARS in news-scraper/run.py.
MIN_ARTICLE_CONTENT_CHARS = int(os.getenv("MIN_ARTICLE_CONTENT_CHARS", "200"))

# Recency bound for the Branch B ("no scoring + no analysis") lease scan in
# get_unscored_articles. When the `pending` ready-queue is empty, every request falls
# through to Branch B; without a bound its LEFT JOIN ... IS NULL anti-join degrades to a
# full parallel seq-scan of the whole news_articles + news_article_analysis tables (~60s,
# holding a connection past pool_timeout -> pool exhaustion -> 500 storm) because the
# corpus is title-saturated and almost nothing qualifies. Restricting to rows ingested in
# the last N hours turns it into a bounded index scan on idx_news_articles_created. Fresh,
# servable work always has a recent created_at (ingestion time), so nothing that would be
# served is skipped; the filter is on created_at, not published, so freshly-ingested but
# back-dated articles are still covered.
BRANCH_B_RECENCY_HOURS = int(os.getenv("BRANCH_B_RECENCY_HOURS", "12"))


def _setup_log_filters():
    """Set up log filters to suppress blocked hotkey spam."""
    filters_to_add = [SuppressV2LogFilter(), SuppressBlockedHotkeyLogFilter(), SuppressBlockedRequestsFilter()]
    
    # Apply to uvicorn.access logger
    uvicorn_logger = logging.getLogger("uvicorn.access")
    for f in filters_to_add:
        if not any(isinstance(existing, type(f)) for existing in uvicorn_logger.filters):
            uvicorn_logger.addFilter(f)
    
    # Also apply to all handlers on the logger
    for handler in uvicorn_logger.handlers:
        for f in filters_to_add:
            if not any(isinstance(existing, type(f)) for existing in handler.filters):
                handler.addFilter(f)
    
    # Apply to root logger handlers as well (uvicorn often logs to root)
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        for f in filters_to_add:
            if not any(isinstance(existing, type(f)) for existing in handler.filters):
                handler.addFilter(f)
    
    logger.info(f"Log filters applied. Blocking {len(BLOCKED_HOTKEYS)} hotkeys from logs.")


def _version_ok(client_version: str) -> bool:
    """Return True if client_version >= MIN_VALIDATOR_VERSION (semver)."""
    try:
        c = tuple(int(x) for x in client_version.split("."))
        m = tuple(int(x) for x in MIN_VALIDATOR_VERSION.split("."))
        return c >= m
    except (ValueError, AttributeError):
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup/shutdown events."""
    global price_prisma
    
    # Startup
    logger.info("Starting Alpharidge AI API...")
    
    # Re-apply log filters (uvicorn may reconfigure loggers on startup)
    _setup_log_filters()
    
    # Initialize whitelist caches
    try:
        initialize_whitelists()
        logger.info("Whitelists initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize whitelists: {e}")
    
    # Connect to main database
    try:
        await prisma.connect()
        logger.info("Connected to database")
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        raise
    
    # Connect to price database if configured
    if PRICE_DATABASE_URL:
        try:
            price_prisma = Prisma(datasource={"url": PRICE_DATABASE_URL})
            await price_prisma.connect()
            db_host = PRICE_DATABASE_URL.split('@')[-1] if '@' in PRICE_DATABASE_URL else "configured"
            logger.info(f"Connected to price database: {db_host}")
        except Exception as e:
            logger.error(f"Failed to connect to price database: {e}")
            raise
    
    # Start background jobs (event cluster merging, narrative lifecycle)
    background_task = None
    try:
        from jobs.background_tasks import run_periodic_jobs
        background_task = asyncio.create_task(run_periodic_jobs(prisma))
        logger.info("Background jobs started")
    except Exception as e:
        logger.warning(f"Failed to start background jobs: {e}")

    yield

    # Shutdown
    logger.info("Shutting down Alpharidge AI API...")
    if background_task and not background_task.done():
        background_task.cancel()
        try:
            await background_task
        except asyncio.CancelledError:
            pass
    
    if price_prisma is not None:
        await price_prisma.disconnect()
        logger.info("Disconnected from price database")
    
    await prisma.disconnect()
    logger.info("Disconnected from database")


# Create FastAPI application
app = FastAPI(
    title="Alpharidge AI API",
    description="API for Alpharidge AI subnet validators to score tweets and manage rewards/penalties",
    version="1.0.0",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class BlockedHotkeyMiddleware(BaseHTTPMiddleware):
    """
    Middleware to silently reject requests from blocked hotkeys.
    
    Runs before authentication and rejects known bad actors
    with minimal processing and no logging.
    """
    
    # Paths that don't need hotkey checking (no auth required)
    SKIP_PATHS = {"/health", "/price/tao-usd"}
    
    async def dispatch(self, request: Request, call_next):
        # Skip middleware for paths that don't use authentication
        if request.url.path in self.SKIP_PATHS:
            return await call_next(request)
        
        hotkey = request.headers.get("X-Auth-SS58Address", "")
        
        if hotkey and hotkey in BLOCKED_HOTKEYS:
            # Silently reject - no logging to reduce spam
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "Access denied."},
            )
        
        return await call_next(request)


# Add blocked hotkey middleware (runs before auth)
if BLOCKED_HOTKEYS:
    app.add_middleware(BlockedHotkeyMiddleware)
    logger.info(f"Hotkey blocklist enabled with {len(BLOCKED_HOTKEYS)} hotkeys")


class FilteredAccessLogMiddleware(BaseHTTPMiddleware):
    """
    Custom access logging middleware that filters out 403 and /v2 requests.
    Used instead of uvicorn's built-in access logging.
    """
    
    # Paths that should skip custom logging (use uvicorn default)
    SKIP_PATHS = {"/health", "/price/tao-usd"}
    
    async def dispatch(self, request: Request, call_next):
        # Fast path: skip middleware overhead for frequent/fast endpoints
        if request.url.path in self.SKIP_PATHS:
            return await call_next(request)
        
        response = await call_next(request)
        
        # Skip logging for 403 responses and /v2 paths
        if response.status_code == 403:
            return response
        if "/v2/" in request.url.path or request.url.path == "/v2":
            return response
        
        # Log the access
        client_host = request.client.host if request.client else "-"
        logger.info(
            f'{client_host} - "{request.method} {request.url.path}" {response.status_code}'
        )
        return response


# Add custom access log middleware
app.add_middleware(FilteredAccessLogMiddleware)


# ============================================================================
# API Version Deprecation Shims
# ============================================================================

V2_DEPRECATION_MESSAGE = os.getenv(
    "V2_DEPRECATION_MESSAGE",
    "The /v2 API is deprecated. Please update your code.",
)


@app.api_route("/v2", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.api_route("/v2/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def v2_catchall(request: Request, path: str = ""):
    """
    Catch-all for legacy clients still calling /v2/*.

    We intentionally do not require authentication here so callers get a clear upgrade message
    rather than an auth error.
    """
    return JSONResponse(
        status_code=status.HTTP_410_GONE,
        content={
            "error": "deprecated_api_version",
            "message": V2_DEPRECATION_MESSAGE,
            "requested_path": request.url.path,
            "method": request.method,
        },
        headers={
            # Informational headers that some clients/monitors use for deprecations.
            "Deprecation": "true",
        },
    )


# ============================================================================
# Authentication Dependencies
# ============================================================================

async def get_validator_hotkey(request: Request) -> str:
    """
    Dependency to authenticate validator and return their hotkey.
    
    Only validators are allowed to access the API. This function:
    1. Extracts auth data from request headers
    2. Verifies the signature
    3. Confirms the hotkey belongs to a validator
    4. Returns the validator's hotkey
    
    Raises HTTPException if authentication fails.
    """
    # If auth is disabled (local/testing), allow requests without headers.
    # We still try to read a hotkey from headers if present for attribution.
    if not auth_config.enabled:
        auth_request = extract_auth_from_headers(request)
        if auth_request and auth_request.ss58_address:
            return auth_request.ss58_address
        return "unauthenticated"

    # Extract auth from headers (required when auth is enabled)
    auth_request = extract_auth_from_headers(request)
    if auth_request is None:
        logger.warning("Missing authentication headers")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication headers. Required: X-Auth-SS58Address, X-Auth-Signature, X-Auth-Message, X-Auth-Timestamp",
        )
    
    # Verify auth request
    if not verify_auth_request(auth_request, auth_config):
        logger.warning(f"Authentication failed for hotkey: {auth_request.ss58_address}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication. Signature verification failed.",
        )
    
    # Check if hotkey is a validator
    if not is_validator_hotkey(auth_request.ss58_address):
        logger.warning(f"Non-validator hotkey attempted access: {auth_request.ss58_address}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Only validators are allowed to access this API.",
        )
    
    logger.info(f"Validator authenticated: {auth_request.ss58_address}")
    return auth_request.ss58_address


# ============================================================================
# Health Check
# ============================================================================

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


# ============================================================================
# Subnet Config Endpoint (centralized tuning for all validators)
# ============================================================================

SUBNET_CONFIG = {
    "USD_PRICE_PER_POINT": float(os.getenv("SUBNET_USD_PRICE_PER_POINT", "0.040")),
    "MINER_BATCH_SIZE": int(os.getenv("SUBNET_MINER_BATCH_SIZE", "20")),
    "VALIDATION_FETCH_LIMIT": int(os.getenv("SUBNET_VALIDATION_FETCH_LIMIT", "100")),
    "MIN_PERCENT_PER_POINT": float(os.getenv("SUBNET_MIN_PERCENT_PER_POINT", "0.003")),
    "ENABLE_TWEET_SCORING": os.getenv("SUBNET_ENABLE_TWEET_SCORING", "false").lower() == "true",
    "ARTICLE_STORE_MAX_ARTICLES": int(os.getenv("SUBNET_ARTICLE_STORE_MAX_ARTICLES", "2000")),
    "ADAPTIVE_DISPATCH_ENABLED": os.getenv("SUBNET_ADAPTIVE_DISPATCH_ENABLED", "false").lower() == "true",
    "DISPATCH_WINDOW_MIN": int(os.getenv("SUBNET_DISPATCH_WINDOW_MIN", "1")),
    "DISPATCH_WINDOW_CAP_PCT": float(os.getenv("SUBNET_DISPATCH_WINDOW_CAP_PCT", "0.15")),
    "DISPATCH_WINDOW_GROW": float(os.getenv("SUBNET_DISPATCH_WINDOW_GROW", "1.0")),
    "DISPATCH_WINDOW_SHRINK": float(os.getenv("SUBNET_DISPATCH_WINDOW_SHRINK", "0.5")),
    "DISPATCH_LATE_FRACTION": float(os.getenv("SUBNET_DISPATCH_LATE_FRACTION", "0.6")),
    "DISPATCH_ACK_TIMEOUT_S": float(os.getenv("SUBNET_DISPATCH_ACK_TIMEOUT_S", "12.0")),
    "DISPATCH_CHRONIC_TIMEOUT_N": int(os.getenv("SUBNET_DISPATCH_CHRONIC_TIMEOUT_N", "5")),
    "LIVENESS_TTL_S": int(os.getenv("SUBNET_LIVENESS_TTL_S", "120")),
    "LIVENESS_SWEEP_INTERVAL_S": int(os.getenv("SUBNET_LIVENESS_SWEEP_INTERVAL_S", "60")),
    "ADAPTIVE_PENALTY_SPLIT_ENABLED": os.getenv("SUBNET_ADAPTIVE_PENALTY_SPLIT_ENABLED", "true").lower() == "true",
    "ADAPTIVE_MISSING_ANALYSIS_SPLIT_ENABLED": os.getenv("SUBNET_ADAPTIVE_MISSING_ANALYSIS_SPLIT_ENABLED", "false").lower() == "true",
    "TIER3_THRESHOLD": float(os.getenv("SUBNET_TIER3_THRESHOLD", "0.70")),
    "CLONE_COSINE_THRESHOLD": float(os.getenv("SUBNET_CLONE_COSINE_THRESHOLD", "0.99")),
    "CLONE_DIFFERENTIAL_ENABLED": os.getenv("SUBNET_CLONE_DIFFERENTIAL_ENABLED", "false").lower() == "true",
    "CLONE_DIVERGENCE_MARGIN": float(os.getenv("SUBNET_CLONE_DIVERGENCE_MARGIN", "0.05")),
    # Weight window. These must stay in step with _REMOTE_CONFIG_KEYS on the
    # validator: a key present there but missing here is never applied, and the
    # rollout silently does nothing. Defaults reproduce the prior behaviour.
    "WEIGHT_WINDOW_EPOCHS":       int(os.getenv("SUBNET_WEIGHT_WINDOW_EPOCHS", "1")),
    "WEIGHT_WINDOW_EPOCHS_PREV":  int(os.getenv("SUBNET_WEIGHT_WINDOW_EPOCHS_PREV", "1")),
    "WEIGHT_WINDOW_ACTIVE_BLOCK": int(os.getenv("SUBNET_WEIGHT_WINDOW_ACTIVE_BLOCK", "0")),
    "REPUTATION_SCORING_ENABLED":  os.getenv("SUBNET_REPUTATION_SCORING_ENABLED", "false").lower() == "true",
    "REPUTATION_GATING_ENABLED":   os.getenv("SUBNET_REPUTATION_GATING_ENABLED", "false").lower() == "true",
    "REPUTATION_EMA_ALPHA":        float(os.getenv("SUBNET_REPUTATION_EMA_ALPHA", "0.03")),
    "REPUTATION_PRIOR":            float(os.getenv("SUBNET_REPUTATION_PRIOR", "0.5")),
    "EMISSION_MIDPOINT":           float(os.getenv("SUBNET_EMISSION_MIDPOINT", "0.59")),
    "EMISSION_GAIN":               float(os.getenv("SUBNET_EMISSION_GAIN", "100.0")),
    "VALIDATION_SAMPLE_SIZE":      int(os.getenv("SUBNET_VALIDATION_SAMPLE_SIZE", "1")),
    "SAMPLING_SUBSTANTIVE_WEIGHT": float(os.getenv("SUBNET_SAMPLING_SUBSTANTIVE_WEIGHT", "2.0")),
    "SUMMARY_AGREEMENT_FLOOR":     float(os.getenv("SUBNET_SUMMARY_AGREEMENT_FLOOR", "0.4")),
    "DISPATCH_COOLDOWN_SHADOW_MODE":        os.getenv("SUBNET_DISPATCH_COOLDOWN_SHADOW_MODE", "true").lower() == "true",
    "DISPATCH_COOLDOWN_FAITHFULNESS_FLOOR": float(os.getenv("SUBNET_DISPATCH_COOLDOWN_FAITHFULNESS_FLOOR", "0.33")),
    "DISPATCH_CONSEC_INVALID_N":            int(os.getenv("SUBNET_DISPATCH_CONSEC_INVALID_N", "3")),
    "DISPATCH_INVALID_COOLDOWN_FIRST_S":    int(os.getenv("SUBNET_DISPATCH_INVALID_COOLDOWN_FIRST_S", "60")),
    "DISPATCH_INVALID_COOLDOWN_MAX_S":      int(os.getenv("SUBNET_DISPATCH_INVALID_COOLDOWN_MAX_S", "600")),
    "DISPATCH_FAILSTREAK_SHADOW_MODE":      os.getenv("SUBNET_DISPATCH_FAILSTREAK_SHADOW_MODE", "true").lower() == "true",
    "DISPATCH_CONSEC_FAIL_N":               int(os.getenv("SUBNET_DISPATCH_CONSEC_FAIL_N", "10")),
    "ADAPTIVE_BATCH_SIZE_ENABLED": os.getenv("SUBNET_ADAPTIVE_BATCH_SIZE_ENABLED", "false").lower() == "true",
    "MINER_BATCH_SIZE_MAX":        int(os.getenv("SUBNET_MINER_BATCH_SIZE_MAX", os.getenv("SUBNET_MINER_BATCH_SIZE", "20"))),
    "MINER_BATCH_SIZE_MIN":        int(os.getenv("SUBNET_MINER_BATCH_SIZE_MIN", os.getenv("SUBNET_MINER_BATCH_SIZE", "20"))),
    "BATCH_SIZE_GROW_STEP":        int(os.getenv("SUBNET_BATCH_SIZE_GROW_STEP", "2")),
    "BATCH_SIZE_SHRINK_FACTOR":    float(os.getenv("SUBNET_BATCH_SIZE_SHRINK_FACTOR", "0.75")),
    "EMISSION_BONUS_CEILING":      float(os.getenv("SUBNET_EMISSION_BONUS_CEILING", "0.0")),
    "EMISSION_BONUS_START":        float(os.getenv("SUBNET_EMISSION_BONUS_START", "0.63")),
    "EMISSION_BONUS_FULL":         float(os.getenv("SUBNET_EMISSION_BONUS_FULL", "0.75")),
    # Article triage (schema v3): single cutover switch.
    "TRIAGE_ENFORCED":             os.getenv("SUBNET_TRIAGE_ENFORCED", "false").lower() == "true",
}

MIN_VALIDATOR_VERSION = os.getenv("MIN_VALIDATOR_VERSION", "3.0.0")
MAX_POINTS_PER_ITEM = float(os.getenv("MAX_POINTS_PER_ITEM", "64"))
MAX_OUTSTANDING_LEASES = int(os.getenv("MAX_OUTSTANDING_LEASES", "0"))
REPORT_CONSENSUS_THRESHOLD = int(os.getenv("REPORT_CONSENSUS_THRESHOLD", "2"))
AUDIT_OVERLAP_RATE = float(os.getenv("AUDIT_OVERLAP_RATE", "0"))
REPORTS_AUTO_BLACKLIST = os.getenv("REPORTS_AUTO_BLACKLIST", "false").lower() == "true"
VERDICT_ALLOWLIST_HOTKEYS = set(filter(None, (
    h.strip() for h in os.getenv("VERDICT_ALLOWLIST_HOTKEYS", "").split(","))))

SUBNET_BLACKLISTED_HOTKEYS: list[str] = [
    hk.strip() for hk in os.getenv("SUBNET_BLACKLISTED_HOTKEYS", "").split(",") if hk.strip()
]

RESET_BROADCASTS_BEFORE_EPOCH: int = int(os.getenv("RESET_BROADCASTS_BEFORE_EPOCH", "-1"))
PURGE_BROADCAST_HOTKEYS: list[str] = [
    hk.strip() for hk in os.getenv("PURGE_BROADCAST_HOTKEYS", "").split(",") if hk.strip()
]

RESET_SCORES_ID: str = os.getenv("RESET_SCORES_ID", "")


# Per-validator record of the latest version reported on the hourly /config/subnet
# poll (version, first/last seen, poll count). Persisted to disk so it survives API
# restarts: loaded on startup, rewritten on each poll. Exposed at /validators/versions.
_VALIDATOR_VERSIONS_PATH = Path(os.getenv(
    "VALIDATOR_VERSIONS_PATH",
    str(Path(__file__).resolve().parent / ".validator_versions.json"),
))


def _load_validator_versions() -> dict:
    try:
        if _VALIDATOR_VERSIONS_PATH.exists():
            return json.loads(_VALIDATOR_VERSIONS_PATH.read_text())
    except Exception as e:
        logger.warning(f"Failed to load validator versions from {_VALIDATOR_VERSIONS_PATH}: {e}")
    return {}


def _save_validator_versions() -> None:
    # Atomic write (temp + rename) so a crash mid-write can't corrupt the file.
    try:
        tmp = _VALIDATOR_VERSIONS_PATH.with_name(_VALIDATOR_VERSIONS_PATH.name + ".tmp")
        tmp.write_text(json.dumps(_validator_versions, indent=2))
        tmp.replace(_VALIDATOR_VERSIONS_PATH)
    except Exception as e:
        logger.warning(f"Failed to save validator versions to {_VALIDATOR_VERSIONS_PATH}: {e}")


_validator_versions: dict = _load_validator_versions()


@app.get("/config/subnet")
async def get_subnet_config(
    request: Request,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Returns recommended configuration values for validators.

    Validators poll this once per hour. Local OVERRIDE_<key> env vars
    take precedence on the validator side.
    """
    client_version = request.headers.get("X-Validator-Version", "unknown")
    logger.info(f"Config request from {validator_hotkey[:12]}.. version={client_version}")

    # Record the latest reported version per validator (exposed at /validators/versions).
    _now = datetime.now(timezone.utc).isoformat()
    _rec = _validator_versions.get(validator_hotkey)
    if _rec is None:
        _validator_versions[validator_hotkey] = {
            "version": client_version, "first_seen": _now, "last_seen": _now, "poll_count": 1,
        }
    else:
        _rec["version"] = client_version
        _rec["last_seen"] = _now
        _rec["poll_count"] = _rec.get("poll_count", 0) + 1
    _save_validator_versions()

    return {
        "config": SUBNET_CONFIG,
        "min_validator_version": MIN_VALIDATOR_VERSION,
        "blacklisted_hotkeys": SUBNET_BLACKLISTED_HOTKEYS,
        "reset_broadcasts_before_epoch": RESET_BROADCASTS_BEFORE_EPOCH,
        "purge_broadcast_hotkeys": PURGE_BROADCAST_HOTKEYS,
        "reset_scores_id": RESET_SCORES_ID,
        "version": 2,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/validators/versions", dependencies=[Depends(_require_local)])
async def get_validator_versions():
    """
    Report the latest validator version seen on /config/subnet polls.

    Sourced from the validators' hourly config polls (X-Validator-Version header)
    and held in memory — resets on API restart and repopulates within ~1h as
    validators poll. Useful for confirming a release has propagated to every
    validator. Sorted most-recently-seen first.
    """
    validators = [
        {"hotkey": hk, **rec}
        for hk, rec in sorted(
            _validator_versions.items(),
            key=lambda kv: kv[1].get("last_seen", ""),
            reverse=True,
        )
    ]
    return {
        "validators": validators,
        "count": len(validators),
        "min_validator_version": MIN_VALIDATOR_VERSION,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ============================================================================
# TAO Price Endpoint
# ============================================================================

@app.get(
    "/price/tao-usd",
    response_model=TaoPriceResponse,
    responses={
        503: {"model": ErrorResponse},
    },
)
async def get_tao_price():
    """
    Get the latest TAO/USD price from the database.
    
    Returns the most recent TAO price in USD from the tao_usd_price table.
    If no price data exists, returns 503.
    """
    # Use price database if configured, otherwise use main database
    db = price_prisma if price_prisma is not None else prisma
    
    # Query the most recent price from the database
    latest_price = await db.taousdprice.find_first(
        order={"date": "desc"}
    )
    
    if latest_price is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TAO price not yet available. Please retry shortly.",
        )
    
    # Check if price is stale (older than 1 hour)
    age_seconds = (datetime.now(timezone.utc) - latest_price.date).total_seconds()
    is_stale = age_seconds > 3600  # 1 hour
    
    return TaoPriceResponse(
        price_usd=latest_price.taoPrice,
        last_updated=latest_price.date,
        source="taostats",
        stale=is_stale,
    )


# ============================================================================
# Axon Check Endpoint
# ============================================================================

@app.post(
    "/axon/check",
    response_model=AxonCheckResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def check_axon(
    request: AxonCheckRequest,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Verify a validator's axon is reachable from the internet.
    
    The API attempts to connect to the validator's axon at the provided IP:port.
    Used during validator startup to ensure the axon port is properly configured.
    
    Only accessible by validators.
    """
    axon_timeout = float(os.getenv("AXON_CHECK_TIMEOUT", "5.0"))
    
    try:
        async with httpx.AsyncClient(timeout=axon_timeout) as client:
            # Bittensor axons expose an HTTP server; attempt a simple GET request
            url = f"http://{request.ip}:{request.port}/"
            response = await client.get(url)
            # Any response (even 4xx/5xx) means the port is open and responding
            logger.info(
                f"Axon check PASSED for {validator_hotkey}: "
                f"{request.ip}:{request.port} responded with status {response.status_code}"
            )
            return AxonCheckResponse(reachable=True)
    except httpx.ConnectError as e:
        error_msg = f"Connection refused or host unreachable: {e}"
        logger.warning(
            f"Axon check FAILED for {validator_hotkey}: "
            f"{request.ip}:{request.port} - {error_msg}"
        )
        return AxonCheckResponse(reachable=False, error=error_msg)
    except httpx.TimeoutException:
        error_msg = f"Connection timed out after {axon_timeout}s"
        logger.warning(
            f"Axon check FAILED for {validator_hotkey}: "
            f"{request.ip}:{request.port} - {error_msg}"
        )
        return AxonCheckResponse(reachable=False, error=error_msg)
    except Exception as e:
        error_msg = f"Unexpected error: {type(e).__name__}: {e}"
        logger.warning(
            f"Axon check FAILED for {validator_hotkey}: "
            f"{request.ip}:{request.port} - {error_msg}"
        )
        return AxonCheckResponse(reachable=False, error=error_msg)


@app.post("/reputation/snapshot")
async def post_reputation_snapshot(
    request: ReputationSnapshotRequest,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """Append per-hotkey reputation rows for one epoch (display/monitoring only; decoupled
    from consensus). Append-only insert — never updates or deletes existing rows."""
    rows = request.snapshots or []
    if not rows:
        return {"inserted": 0}
    inserted = 0
    try:
        async with prisma.tx() as tx:
            # Idempotent per (sender, epoch): a sender restart resets its in-memory
            # push guard and would re-send an already-recorded epoch. Skip if present
            # so a re-send never duplicates rows (no update/delete of existing data).
            existing = await tx.query_raw(
                'SELECT 1 FROM "reputation_snapshot" '
                'WHERE "validator_hotkey"=$1 AND "epoch"=$2 LIMIT 1;',
                validator_hotkey, int(request.epoch),
            )
            if existing:
                logger.info(
                    f"reputation_snapshot: epoch={request.epoch} already present "
                    f"from {validator_hotkey[:12]}..; skipped {len(rows)} rows"
                )
                return {"inserted": 0, "skipped": len(rows)}
            for r in rows:
                await tx.execute_raw(
                    """
                    INSERT INTO "reputation_snapshot"
                        ("validator_hotkey","miner_hotkey","epoch","reputation","samples","gate","projected_share")
                    VALUES ($1,$2,$3,$4,$5,$6,$7);
                    """,
                    validator_hotkey, r.miner_hotkey, int(request.epoch),
                    float(r.reputation), int(r.samples), r.gate, r.projected_share,
                )
                inserted += 1
    except Exception as e:
        logger.warning(f"reputation_snapshot insert failed from {validator_hotkey[:12]}..: {e}")
        raise HTTPException(status_code=500, detail="insert failed")
    logger.info(f"reputation_snapshot: inserted {inserted} rows from {validator_hotkey[:12]}.. epoch={request.epoch}")
    return {"inserted": inserted}


# ============================================================================
# Tweet Routes
# ============================================================================

@app.get(
    "/tweets/unscored",
    response_model=TweetsForScoringResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_unscored_tweets(
    request: Request,
    limit: int = 3,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    if TWEET_ALLOWLIST and validator_hotkey not in TWEET_ALLOWLIST:
        return TweetsForScoringResponse(tweets=[], count=0)

    client_ver = request.headers.get("X-Validator-Version", "0.0.0")
    if not _version_ok(client_ver):
        logger.warning(
            f"Validator {validator_hotkey[:12]}.. version {client_ver} below minimum "
            f"{MIN_VALIDATOR_VERSION} — returning empty tweets"
        )
        return TweetsForScoringResponse(tweets=[], count=0)

    """
    Get tweets that need scoring.

    Returns up to `limit` tweets (default 3) that either:
    - Have no scoring records at all, or
    - Have no TweetAnalysis record

    Excludes tweets that already have an 'in_progress' or 'completed' scoring.
    Creates a new scoring record (set to 'in_progress') for tweets without one.

    Only accessible by validators.
    """
    try:
        lease_ttl_seconds = int(os.getenv("SCORING_LEASE_TTL_SECONDS", "900"))
        serve_crypto = os.getenv("SERVE_CRYPTO_TWEETS", "false").lower() == "true"
        rule_tag_filter = "" if serve_crypto else "AND (t.rule_tag IS NULL OR t.rule_tag NOT LIKE 'search_%')"

        async with prisma.tx() as tx:
            # 1) Reclaim expired leases: in_progress older than TTL → pending (unassigned).
            await tx.execute_raw(
                """
                UPDATE scoring
                SET status = 'pending',
                    start_time = NULL,
                    validator_hotkey = NULL
                WHERE status = 'in_progress'
                  AND start_time IS NOT NULL
                  AND start_time < (NOW() AT TIME ZONE 'utc') - (MAKE_INTERVAL(secs => $1));
                """,
                lease_ttl_seconds,
            )

            # Outstanding-lease cap: count this validator's in-progress leases and
            # reduce the grant so it never exceeds MAX_OUTSTANDING_LEASES.
            # Note: audit-overlap injection below may add up to ceil(limit*AUDIT_OVERLAP_RATE) extra in_progress rows beyond this cap (bounded, intentional extra verification work).
            outstanding_rows = await tx.query_raw(
                "SELECT COUNT(*)::int AS c FROM scoring "
                "WHERE validator_hotkey = $1 AND status = 'in_progress';",
                validator_hotkey,
            )
            outstanding = int((outstanding_rows or [{"c": 0}])[0]["c"])
            limit = v.grant_count(limit=limit, outstanding=outstanding,
                                  max_outstanding=MAX_OUTSTANDING_LEASES)
            if limit <= 0:
                return TweetsForScoringResponse(tweets=[], count=0)

            # 2) Pick from two sources:
            #   A) Existing scoring records with status='pending'
            #   B) Tweets with no scoring record and no analysis record

            # A: Atomically claim up to `limit` pending scorings using row locks.
            claimed_pending = await tx.query_raw(
                f"""
                WITH picked AS (
                    SELECT s.id, s.tweet_id
                    FROM scoring s
                    JOIN tweets t ON t.id = s.tweet_id
                    WHERE s.status = 'pending'
                      AND t.text IS NOT NULL
                      AND BTRIM(t.text) <> ''
                      {rule_tag_filter}
                    ORDER BY s.created_at ASC, s.id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                )
                UPDATE scoring s
                SET status = 'in_progress',
                    start_time = (NOW() AT TIME ZONE 'utc'),
                    validator_hotkey = $2
                FROM picked
                WHERE s.id = picked.id
                RETURNING picked.tweet_id;
                """,
                limit,
                validator_hotkey,
            )
            tweet_ids_pending = [row["tweet_id"] for row in (claimed_pending or [])]

            # If need more, get up to `slots_left` tweets that have no scoring and no analysis
            slots_left = max(0, limit - len(tweet_ids_pending))
            tweet_ids_no_scoring = []
            if slots_left > 0:
                # Find tweets WITHOUT any scoring record AND WITHOUT an analysis record,
                # and insert a new scoring record (status = 'in_progress') for each, returning IDs
                # We must avoid race condition: Do all in one statement with row locking
                inserted_rows = await tx.query_raw(
                    f"""
                    WITH unscored_tweets AS (
                        SELECT t.id AS tweet_id
                        FROM tweets t
                        LEFT JOIN scoring s ON s.tweet_id = t.id
                        LEFT JOIN tweet_analysis a ON a.tweet_id = t.id
                        WHERE s.id IS NULL AND a.id IS NULL
                          AND t.text IS NOT NULL
                          AND BTRIM(t.text) <> ''
                          {rule_tag_filter}
                        ORDER BY t.created_at ASC, t.id ASC
                        LIMIT $1
                        FOR UPDATE OF t SKIP LOCKED
                    ), created_scoring AS (
                        INSERT INTO scoring (tweet_id, status, start_time, validator_hotkey, created_at)
                        SELECT tweet_id, 'in_progress', (NOW() AT TIME ZONE 'utc'), $2, (NOW() AT TIME ZONE 'utc')
                        FROM unscored_tweets
                        RETURNING tweet_id
                    )
                    SELECT tweet_id FROM created_scoring;
                    """,
                    slots_left,
                    validator_hotkey,
                )
                tweet_ids_no_scoring = [row["tweet_id"] for row in (inserted_rows or [])]

            # Combine all claimed tweet ids
            tweet_ids = tweet_ids_pending + tweet_ids_no_scoring

            # Audit overlaps: silently re-issue a small fraction of items another
            # validator already completed, so we can later compare categorical keys.
            audit_n = math.ceil(limit * AUDIT_OVERLAP_RATE) if limit > 0 else 0
            if audit_n > 0:
                audit_rows = await tx.query_raw(
                    """
                    SELECT s.tweet_id
                    FROM scoring s
                    JOIN tweets t ON t.id = s.tweet_id
                    WHERE s.status = 'completed'
                      AND (s.validator_hotkey IS NULL OR s.validator_hotkey <> $1)
                      AND t.text IS NOT NULL AND BTRIM(t.text) <> ''
                    ORDER BY s.created_at DESC
                    LIMIT $2;
                    """,
                    validator_hotkey, audit_n,
                )
                for row in (audit_rows or []):
                    tid = row["tweet_id"]
                    if tid in tweet_ids:
                        continue
                    await tx.scoring.create(data={
                        "tweetId": tid, "status": "in_progress",
                        "startTime": datetime.utcnow(), "validatorHotkey": validator_hotkey,
                    })
                    tweet_ids.append(tid)

            if not tweet_ids:
                return TweetsForScoringResponse(tweets=[], count=0)

        # Fetch the claimed tweets + nested author/analysis for response.
        tweets = await prisma.tweet.find_many(
            where={"id": {"in": tweet_ids}},
            include={"author": True, "analysis": True},
        )

        # Preserve claim order
        tweets_by_id = {t.id: t for t in tweets}
        ordered = [tweets_by_id.get(tid) for tid in tweet_ids if tid in tweets_by_id]

        tweets_with_authors = []
        for tweet in ordered:
            # Defensive safety check: never send tweets with NULL/empty/whitespace-only text.
            # (We also filter at claim-time in SQL to avoid leasing these in the first place.)
            if tweet is None or tweet.text is None or not str(tweet.text).strip():
                continue

            author_model = None
            analysis_model = None

            if tweet.author:
                author_model = Account(
                    id=tweet.author.id,
                    name=tweet.author.name,
                    screenName=tweet.author.screenName,
                    userName=tweet.author.userName,
                    location=tweet.author.location,
                    description=tweet.author.description,
                    verified=tweet.author.verified,
                    isBlueVerified=tweet.author.isBlueVerified,
                    followersCount=tweet.author.followersCount,
                    followingCount=tweet.author.followingCount,
                    statusesCount=tweet.author.statusesCount,
                    profileImageUrl=tweet.author.profileImageUrl,
                    createdAt=tweet.author.createdAt,
                )

            if tweet.analysis:
                analysis_model = TweetAnalysis(
                    id=tweet.analysis.id,
                    tweetId=tweet.analysis.tweetId,
                    sentiment=tweet.analysis.sentiment,
                    assetId=tweet.analysis.assetId,
                    assetSymbol=tweet.analysis.assetSymbol,
                    contentType=tweet.analysis.contentType,
                    analyzedAt=tweet.analysis.analyzedAt,
                )

            tweet_data = TweetWithAuthor(
                id=tweet.id,
                type=tweet.type,
                url=tweet.url,
                text=tweet.text,
                lang=tweet.lang,
                retweetCount=tweet.retweetCount,
                replyCount=tweet.replyCount,
                likeCount=tweet.likeCount,
                quoteCount=tweet.quoteCount,
                viewCount=tweet.viewCount,
                bookmarkCount=tweet.bookmarkCount,
                isReply=tweet.isReply,
                inReplyToId=tweet.inReplyToId,
                conversationId=tweet.conversationId,
                authorId=tweet.authorId,
                createdAt=tweet.createdAt,
                receivedAt=tweet.receivedAt,
                author=author_model,
                analysis=analysis_model,
            )
            tweets_with_authors.append(tweet_data)

        logger.info(f"Leased {len(tweets_with_authors)} tweet(s) to validator {validator_hotkey}")
        return TweetsForScoringResponse(tweets=tweets_with_authors, count=len(tweets_with_authors))

    except Exception as e:
        logger.error(f"Error getting unscored tweets: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get unscored tweets: {str(e)}",
        )


async def write_verdict(*, resource_type: str, resource_id: str, validator_hotkey: str,
                        miner_hotkey: str, miner_signature: str, nonce: str,
                        miner_analysis_hash: str, validator_verdict: str,
                        categorical_key: str, points_awarded: float,
                        epoch: int, is_audit: bool = False,
                        storage_id: Optional[str] = None) -> bool:
    """Verify the miner signature against the metagraph and upsert a ScoreVerdict.
    Returns True if a verdict row was written, False if rejected/skipped/errored.
    Best-effort: never raises, so a verdict failure can't break the completed-submission
    request for legacy validators (Phase-1 grace)."""
    # §4 scoped-test allowlist: when configured, only write verdicts for listed validators.
    if VERDICT_ALLOWLIST_HOTKEYS and validator_hotkey not in VERDICT_ALLOWLIST_HOTKEYS:
        return False
    # Phase-1 grace: incomplete verdict payloads simply produce no verdict row.
    # categorical_key uses truthiness (reject ""); epoch uses `is not None` (0 is valid).
    if not all([miner_hotkey, miner_signature, nonce, miner_analysis_hash,
                validator_verdict, categorical_key, epoch is not None]):
        return False
    if not hotkey_whitelist.is_miner_hotkey(miner_hotkey):
        logger.warning(f"Verdict rejected: {miner_hotkey[:12]}.. is not a current miner")
        return False
    if not ac.verify_miner_signature(miner_hotkey, str(resource_id), miner_analysis_hash,
                                     nonce, miner_signature):
        logger.warning(f"Verdict rejected: bad miner signature from {miner_hotkey[:12]}..")
        return False

    clamped = v.clamp_points(points_awarded if points_awarded is not None else 0.0,
                             MAX_POINTS_PER_ITEM)
    # Deterministic group id: all validators scoring the same item+epoch share it,
    # enabling audit/divergence comparison regardless of whether this was an audit lease.
    # storage_id lets several verdicts for one item coexist (overlap dispatch);
    # the miner signature is still verified against the item id.
    stored_id = str(storage_id if storage_id is not None else resource_id)
    audit_group_id = f"{resource_type}:{stored_id}:{int(epoch)}"
    data = {
        "resourceType": resource_type,
        "resourceId": stored_id,
        "epoch": int(epoch),
        "validatorHotkey": validator_hotkey,
        "minerHotkey": miner_hotkey,
        "minerSignature": miner_signature,
        "minerAnalysisHash": miner_analysis_hash,
        "validatorVerdict": validator_verdict,
        "categoricalKey": categorical_key,
        "pointsAwarded": clamped,
        # isAudit is reserved: always False in Phase 1 (audit-lease provenance is not yet
        # propagated from the scoring row). Divergence detection keys off auditGroupId, not this.
        "isAudit": is_audit,
        "auditGroupId": audit_group_id,
    }
    try:
        await prisma.scoreverdict.upsert(
            where={"uq_score_verdict_item": {
                "resourceType": resource_type, "resourceId": stored_id,
                "validatorHotkey": validator_hotkey, "epoch": int(epoch)}},
            data={"create": data, "update": data},
        )
    except Exception as e:
        logger.error(f"write_verdict upsert failed (non-fatal): {e}")
        return False
    return True


async def audit_divergence_for_group(audit_group_id: str) -> list:
    """Return the sorted list of validators whose categorical_key diverges from the
    majority within an audit group (empty if no strict majority / <2 verdicts)."""
    rows = await prisma.scoreverdict.find_many(where={"auditGroupId": audit_group_id})
    group = [{"validator_hotkey": r.validatorHotkey, "categorical_key": r.categoricalKey}
             for r in rows]
    return sorted(v.audit_divergent_validators(group))


@app.post(
    "/tweets/completed",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def submit_completed_tweets(
    submission: CompletedTweetsSubmission,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Submit completed scored tweets.
    
    Updates the scoring status to 'completed' and stores the sentiment in TweetAnalysis.
    Only tweets assigned to the requesting validator can be completed.
    
    Only accessible by validators.
    """
    try:
        updated_count = 0
        
        for completed in submission.completed_tweets:
            # Create or update TweetAnalysis with sentiment + optional richer classification columns.
            analysis_create = {
                "tweetId": completed.tweet_id,
                "sentiment": completed.sentiment,
                "analyzedAt": datetime.utcnow(),
            }
            analysis_update = {
                "sentiment": completed.sentiment,
                "updatedAt": datetime.utcnow(),
                "analyzedAt": datetime.utcnow(),
            }

            # Optional classification columns (only set if provided by the validator).
            optional_fields = {
                "assetId": completed.asset_id,
                "assetSymbol": completed.asset_symbol,
                "contentType": completed.content_type,
                "technicalQuality": completed.technical_quality,
                "marketAnalysis": completed.market_analysis,
                "impactPotential": completed.impact_potential,
                "relevanceConfidence": completed.relevance_confidence,
                "minerHotkey": completed.miner_hotkey,
            }
            for k, v in optional_fields.items():
                if v is not None:
                    analysis_create[k] = v
                    analysis_update[k] = v

            await prisma.tweetanalysis.upsert(
                where={"tweetId": completed.tweet_id},
                data={
                    "create": analysis_create,
                    "update": analysis_update,
                },
            )
            
            # Update scoring status to completed (only if still leased to this validator).
            result = await prisma.scoring.update_many(
                where={
                    "tweetId": completed.tweet_id,
                    "validatorHotkey": validator_hotkey,
                    "status": "in_progress",
                },
                data={"status": "completed"},
            )
            updated_count += result

            # Record a signed verdict (Phase 1+: optional; skipped if fields/sig missing).
            # Only record a verdict if this validator actually held the lease for this
            # item (the update_many above matches only in_progress rows owned by us).
            # This enforces per-miner attribution: a validator cannot earn points for
            # items it was never leased.
            if result:
                await write_verdict(
                    resource_type="tweet",
                    resource_id=str(completed.tweet_id),
                    validator_hotkey=validator_hotkey,
                    miner_hotkey=completed.miner_hotkey,
                    miner_signature=completed.miner_signature,
                    nonce=completed.nonce,
                    miner_analysis_hash=completed.miner_analysis_hash,
                    validator_verdict=completed.validator_verdict,
                    categorical_key=completed.categorical_key,
                    points_awarded=completed.points_awarded,
                    epoch=completed.epoch,
                )

        logger.info(f"Validator {validator_hotkey} completed {updated_count} tweets")
        return SubmissionResponse(
            success=True,
            message=f"Successfully completed {updated_count} tweets",
            count=updated_count,
        )
    
    except Exception as e:
        logger.error(f"Error submitting completed tweets: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to submit completed tweets: {str(e)}",
        )


# ============================================================================
# Telegram Message Routes
# ============================================================================

@app.get(
    "/telegram/messages/unscored",
    response_model=TelegramMessagesForScoringResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
) 
async def get_unscored_telegram_messages(
    request: Request,
    limit: int = 3,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    if not SERVE_TELEGRAM:
        return TelegramMessagesForScoringResponse(messages=[], count=0)

    if TWEET_ALLOWLIST and validator_hotkey not in TWEET_ALLOWLIST:
        return TelegramMessagesForScoringResponse(messages=[], count=0)

    client_ver = request.headers.get("X-Validator-Version", "0.0.0")
    if not _version_ok(client_ver):
        logger.warning(
            f"Validator {validator_hotkey[:12]}.. version {client_ver} below minimum "
            f"{MIN_VALIDATOR_VERSION} — returning empty telegram messages"
        )
        return TelegramMessagesForScoringResponse(messages=[], count=0)

    """
    Get telegram messages that need scoring.

    Returns up to `limit` messages (default 3) that either:
    - Have no scoring records at all, or
    - Have no TelegramMessageAnalysis record

    For each message, context is provided:
    - If the message is a reply, check if the parent message has been classified (has assetId).
      If so, include that classification as inherited_asset_id.
    - If not a reply, grab the previous 2 messages in the same group and check their classification.
      If any have a classification, include that as inherited_asset_id.

    Creates a new scoring record (set to 'in_progress') for messages without one.
    Only accessible by validators.
    """
    try:
        lease_ttl_seconds = int(os.getenv("SCORING_LEASE_TTL_SECONDS", "900"))

        async with prisma.tx() as tx:
            # 1) Reclaim expired leases: in_progress older than TTL → pending (unassigned).
            await tx.execute_raw(
                """
                UPDATE telegram_scoring
                SET status = 'pending',
                    start_time = NULL,
                    validator_hotkey = NULL
                WHERE status = 'in_progress'
                  AND start_time IS NOT NULL
                  AND start_time < (NOW() AT TIME ZONE 'utc') - (MAKE_INTERVAL(secs => $1));
                """,
                lease_ttl_seconds,
            )

            # Outstanding-lease cap: count this validator's in-progress leases and
            # reduce the grant so it never exceeds MAX_OUTSTANDING_LEASES.
            outstanding_rows = await tx.query_raw(
                "SELECT COUNT(*)::int AS c FROM telegram_scoring "
                "WHERE validator_hotkey = $1 AND status = 'in_progress';",
                validator_hotkey,
            )
            outstanding = int((outstanding_rows or [{"c": 0}])[0]["c"])
            limit = v.grant_count(limit=limit, outstanding=outstanding,
                                  max_outstanding=MAX_OUTSTANDING_LEASES)
            if limit <= 0:
                return TelegramMessagesForScoringResponse(messages=[], count=0)

            # 2) Pick from two sources:
            #   A) Existing scoring records with status='pending'
            #   B) Messages with no scoring record and no analysis record

            # A: Atomically claim up to `limit` pending scorings using row locks.
            claimed_pending = await tx.query_raw(
                """
                WITH picked AS (
                    SELECT s.id, s.message_id
                    FROM telegram_scoring s
                    JOIN telegram_messages m ON m.id = s.message_id
                    WHERE s.status = 'pending'
                      AND m.content IS NOT NULL
                      AND BTRIM(m.content) <> ''
                    ORDER BY s.created_at ASC, s.id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                )
                UPDATE telegram_scoring s
                SET status = 'in_progress',
                    start_time = (NOW() AT TIME ZONE 'utc'),
                    validator_hotkey = $2
                FROM picked
                WHERE s.id = picked.id
                RETURNING picked.message_id;
                """,
                limit,
                validator_hotkey,
            )
            message_ids_pending = [row["message_id"] for row in (claimed_pending or [])]

            # If need more, get messages that have no scoring and no analysis
            slots_left = max(0, limit - len(message_ids_pending))
            message_ids_no_scoring = []
            if slots_left > 0:
                inserted_rows = await tx.query_raw(
                    """
                    WITH unscored_messages AS (
                        SELECT m.id AS message_id
                        FROM telegram_messages m
                        LEFT JOIN telegram_scoring s ON s.message_id = m.id
                        LEFT JOIN telegram_message_analysis a ON a.message_id = m.id
                        WHERE s.id IS NULL AND a.id IS NULL
                          AND m.content IS NOT NULL
                          AND BTRIM(m.content) <> ''
                        ORDER BY m.created_at ASC, m.id ASC
                        LIMIT $1
                        FOR UPDATE OF m SKIP LOCKED
                    ), created_scoring AS (
                        INSERT INTO telegram_scoring (message_id, status, start_time, validator_hotkey, created_at)
                        SELECT message_id, 'in_progress', (NOW() AT TIME ZONE 'utc'), $2, (NOW() AT TIME ZONE 'utc')
                        FROM unscored_messages
                        RETURNING message_id
                    )
                    SELECT message_id FROM created_scoring;
                    """,
                    slots_left,
                    validator_hotkey,
                )
                message_ids_no_scoring = [row["message_id"] for row in (inserted_rows or [])]

            # Combine all claimed message ids
            message_ids = message_ids_pending + message_ids_no_scoring
            if not message_ids:
                return TelegramMessagesForScoringResponse(messages=[], count=0)

        # Fetch the claimed messages with group and analysis relations
        messages = await prisma.telegrammessage.find_many(
            where={"id": {"in": message_ids}},
            include={"group": True, "analysis": True},
        )

        # Preserve claim order
        messages_by_id = {m.id: m for m in messages}
        ordered = [messages_by_id.get(mid) for mid in message_ids if mid in messages_by_id]

        messages_for_scoring = []
        for message in ordered:
            if message is None or message.content is None or not str(message.content).strip():
                continue

            # Build the response model
            group_model = None
            analysis_model = None
            context_messages = []
            inherited_asset_id = None
            inherited_asset_symbol = None

            if message.group:
                group_model = TelegramGroup(
                    id=message.group.id,
                    telegramId=message.group.telegramId,
                    title=message.group.title,
                    isMonitored=message.group.isMonitored,
                    isMuted=message.group.isMuted,
                    mutedUntil=message.group.mutedUntil.isoformat() if message.group.mutedUntil else None,
                    createdAt=message.group.createdAt.isoformat() if message.group.createdAt else None,
                    updatedAt=message.group.updatedAt.isoformat() if message.group.updatedAt else None,
                )

            if message.analysis:
                analysis_model = TelegramMessageAnalysis(
                    id=message.analysis.id,
                    messageId=message.analysis.messageId,
                    sentiment=message.analysis.sentiment,
                    assetId=message.analysis.assetId,
                    assetSymbol=message.analysis.assetSymbol,
                    contentType=message.analysis.contentType,
                    technicalQuality=message.analysis.technicalQuality,
                    marketAnalysis=message.analysis.marketAnalysis,
                    impactPotential=message.analysis.impactPotential,
                    relevanceConfidence=message.analysis.relevanceConfidence,
                    analyzedAt=message.analysis.analyzedAt.isoformat() if message.analysis.analyzedAt else None,
                )

            # Check for context based on reply status
            if message.replyToId is not None:
                # This message is a reply - check if parent message has classification
                parent_message = await prisma.telegrammessage.find_first(
                    where={"telegramId": message.replyToId},
                    include={"group": True, "analysis": True},
                )
                if parent_message:
                    parent_group_model = None
                    parent_analysis_model = None
                    
                    if parent_message.group:
                        parent_group_model = TelegramGroup(
                            id=parent_message.group.id,
                            telegramId=parent_message.group.telegramId,
                            title=parent_message.group.title,
                            isMonitored=parent_message.group.isMonitored,
                            isMuted=parent_message.group.isMuted,
                            mutedUntil=parent_message.group.mutedUntil.isoformat() if parent_message.group.mutedUntil else None,
                            createdAt=parent_message.group.createdAt.isoformat() if parent_message.group.createdAt else None,
                            updatedAt=parent_message.group.updatedAt.isoformat() if parent_message.group.updatedAt else None,
                        )
                    
                    if parent_message.analysis:
                        parent_analysis_model = TelegramMessageAnalysis(
                            id=parent_message.analysis.id,
                            messageId=parent_message.analysis.messageId,
                            sentiment=parent_message.analysis.sentiment,
                            assetId=parent_message.analysis.assetId,
                            assetSymbol=parent_message.analysis.assetSymbol,
                            contentType=parent_message.analysis.contentType,
                            technicalQuality=parent_message.analysis.technicalQuality,
                            marketAnalysis=parent_message.analysis.marketAnalysis,
                            impactPotential=parent_message.analysis.impactPotential,
                            relevanceConfidence=parent_message.analysis.relevanceConfidence,
                            analyzedAt=parent_message.analysis.analyzedAt.isoformat() if parent_message.analysis.analyzedAt else None,
                        )
                        if parent_message.analysis.assetId is not None:
                            inherited_asset_id = parent_message.analysis.assetId
                            inherited_asset_symbol = parent_message.analysis.assetSymbol

                    context_messages.append(TelegramMessageWithContext(
                        id=parent_message.id,
                        telegramId=parent_message.telegramId,
                        groupId=parent_message.groupId,
                        senderId=parent_message.senderId,
                        senderUsername=parent_message.senderUsername,
                        senderName=parent_message.senderName,
                        content=parent_message.content,
                        replyToId=parent_message.replyToId,
                        createdAt=parent_message.createdAt.isoformat() if parent_message.createdAt else None,
                        group=parent_group_model,
                        analysis=parent_analysis_model,
                    ))
            else:
                # Not a reply - grab previous 2 messages in the same group for context
                previous_messages = await prisma.telegrammessage.find_many(
                    where={
                        "groupId": message.groupId,
                        "createdAt": {"lt": message.createdAt},
                    },
                    include={"group": True, "analysis": True},
                    order={"createdAt": "desc"},
                    take=2,
                )
                
                for prev_msg in previous_messages:
                    prev_group_model = None
                    prev_analysis_model = None
                    
                    if prev_msg.group:
                        prev_group_model = TelegramGroup(
                            id=prev_msg.group.id,
                            telegramId=prev_msg.group.telegramId,
                            title=prev_msg.group.title,
                            isMonitored=prev_msg.group.isMonitored,
                            isMuted=prev_msg.group.isMuted,
                            mutedUntil=prev_msg.group.mutedUntil.isoformat() if prev_msg.group.mutedUntil else None,
                            createdAt=prev_msg.group.createdAt.isoformat() if prev_msg.group.createdAt else None,
                            updatedAt=prev_msg.group.updatedAt.isoformat() if prev_msg.group.updatedAt else None,
                        )
                    
                    if prev_msg.analysis:
                        prev_analysis_model = TelegramMessageAnalysis(
                            id=prev_msg.analysis.id,
                            messageId=prev_msg.analysis.messageId,
                            sentiment=prev_msg.analysis.sentiment,
                            assetId=prev_msg.analysis.assetId,
                            assetSymbol=prev_msg.analysis.assetSymbol,
                            contentType=prev_msg.analysis.contentType,
                            technicalQuality=prev_msg.analysis.technicalQuality,
                            marketAnalysis=prev_msg.analysis.marketAnalysis,
                            impactPotential=prev_msg.analysis.impactPotential,
                            relevanceConfidence=prev_msg.analysis.relevanceConfidence,
                            analyzedAt=prev_msg.analysis.analyzedAt.isoformat() if prev_msg.analysis.analyzedAt else None,
                        )
                        if inherited_asset_id is None and prev_msg.analysis.assetId is not None:
                            inherited_asset_id = prev_msg.analysis.assetId
                            inherited_asset_symbol = prev_msg.analysis.assetSymbol

                    context_messages.append(TelegramMessageWithContext(
                        id=prev_msg.id,
                        telegramId=prev_msg.telegramId,
                        groupId=prev_msg.groupId,
                        senderId=prev_msg.senderId,
                        senderUsername=prev_msg.senderUsername,
                        senderName=prev_msg.senderName,
                        content=prev_msg.content,
                        replyToId=prev_msg.replyToId,
                        createdAt=prev_msg.createdAt.isoformat() if prev_msg.createdAt else None,
                        group=prev_group_model,
                        analysis=prev_analysis_model,
                    ))

            message_data = TelegramMessageForScoring(
                id=message.id,
                telegramId=message.telegramId,
                groupId=message.groupId,
                senderId=message.senderId,
                senderUsername=message.senderUsername,
                senderName=message.senderName,
                content=message.content,
                replyToId=message.replyToId,
                createdAt=message.createdAt.isoformat() if message.createdAt else None,
                group=group_model,
                analysis=analysis_model,
                contextMessages=context_messages,
                inheritedAssetId=inherited_asset_id,
                inheritedAssetSymbol=inherited_asset_symbol,
            )
            messages_for_scoring.append(message_data)

        logger.info(f"Leased {len(messages_for_scoring)} telegram message(s) to validator {validator_hotkey}")
        return TelegramMessagesForScoringResponse(messages=messages_for_scoring, count=len(messages_for_scoring))

    except Exception as e:
        logger.error(f"Error getting unscored telegram messages: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get unscored telegram messages: {str(e)}",
        )


@app.post(
    "/telegram/messages/completed",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def submit_completed_telegram_messages(
    submission: CompletedTelegramMessagesSubmission,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Submit completed scored telegram messages.
    
    Updates the scoring status to 'completed' and stores the analysis in TelegramMessageAnalysis.
    Only messages assigned to the requesting validator can be completed.
    
    Only accessible by validators.
    """
    try:
        updated_count = 0
        
        for completed in submission.completed_messages:
            # Create or update TelegramMessageAnalysis with sentiment + optional classification columns.
            analysis_create = {
                "messageId": completed.message_id,
                "sentiment": completed.sentiment,
                "analyzedAt": datetime.utcnow(),
            }
            analysis_update = {
                "sentiment": completed.sentiment,
                "updatedAt": datetime.utcnow(),
                "analyzedAt": datetime.utcnow(),
            }

            # Optional classification columns (only set if provided by the validator).
            optional_fields = {
                "assetId": completed.asset_id,
                "assetSymbol": completed.asset_symbol,
                "contentType": completed.content_type,
                "technicalQuality": completed.technical_quality,
                "marketAnalysis": completed.market_analysis,
                "impactPotential": completed.impact_potential,
                "relevanceConfidence": completed.relevance_confidence,
                "minerHotkey": completed.miner_hotkey,
            }
            for k, v in optional_fields.items():
                if v is not None:
                    analysis_create[k] = v
                    analysis_update[k] = v

            await prisma.telegrammessageanalysis.upsert(
                where={"messageId": completed.message_id},
                data={
                    "create": analysis_create,
                    "update": analysis_update,
                },
            )
            
            # Update scoring status to completed (only if still leased to this validator).
            result = await prisma.telegramscoring.update_many(
                where={
                    "messageId": completed.message_id,
                    "validatorHotkey": validator_hotkey,
                    "status": "in_progress",
                },
                data={"status": "completed"},
            )
            updated_count += result

            # Record a signed verdict (Phase 1+: optional; skipped if fields/sig missing).
            # Only record a verdict if this validator actually held the lease for this
            # item (the update_many above matches only in_progress rows owned by us).
            # This enforces per-miner attribution: a validator cannot earn points for
            # items it was never leased.
            if result:
                await write_verdict(
                    resource_type="telegram",
                    resource_id=str(completed.message_id),
                    validator_hotkey=validator_hotkey,
                    miner_hotkey=completed.miner_hotkey,
                    miner_signature=completed.miner_signature,
                    nonce=completed.nonce,
                    miner_analysis_hash=completed.miner_analysis_hash,
                    validator_verdict=completed.validator_verdict,
                    categorical_key=completed.categorical_key,
                    points_awarded=completed.points_awarded,
                    epoch=completed.epoch,
                )

        logger.info(f"Validator {validator_hotkey} completed {updated_count} telegram messages")
        return SubmissionResponse(
            success=True,
            message=f"Successfully completed {updated_count} telegram messages",
            count=updated_count,
        )
    
    except Exception as e:
        logger.error(f"Error submitting completed telegram messages: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to submit completed telegram messages: {str(e)}",
        )


# ============================================================================
# News Article Routes
# ============================================================================

@app.get(
    "/articles/unscored",
    response_model=NewsArticlesForScoringResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_unscored_articles(
    limit: int = 3,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Get news articles that need scoring.

    Serves both RSS and CC-NEWS articles (source_type IN ('rss', 'ccnews')),
    ordered newest-published first (published DESC, NULLs last). CC-NEWS rows
    are ingested without a scoring record, so they enter via the "no scoring +
    no analysis" branch, which creates an 'in_progress' record on lease.

    Returns up to `limit` articles (default 3) that either:
    - Have no scoring records at all, or
    - Have no news_article_analysis record

    Excludes articles that already have an 'in_progress' or 'completed' scoring.
    Creates a new scoring record (set to 'in_progress') for articles without one.

    Only accessible by validators.
    """
    if not SERVE_NEWS_ARTICLES:
        return NewsArticlesForScoringResponse(articles=[], count=0)

    try:
        lease_ttl_seconds = int(os.getenv("SCORING_LEASE_TTL_SECONDS", "900"))

        # Run the claim statements in autocommit (no interactive prisma.tx()).
        # Each statement below is atomic on its own and uses FOR UPDATE SKIP LOCKED, so two
        # validators never claim the same article; cross-statement atomicity isn't required
        # (the lease-reclaim self-heals any partially-claimed rows after the TTL). This avoids
        # a prisma-client-py bug where interactive-transaction commit/rollback intermittently
        # raises "422 malformed request" under the high traffic this endpoint sees.

        # 1) Reclaim expired leases: in_progress older than TTL → pending (unassigned).
        await prisma.execute_raw(
            """
            UPDATE news_article_scoring
            SET status = 'pending',
                start_time = NULL,
                validator_hotkey = NULL
            WHERE status = 'in_progress'
              AND start_time IS NOT NULL
              AND start_time < (NOW() AT TIME ZONE 'utc') - (MAKE_INTERVAL(secs => $1));
            """,
            lease_ttl_seconds,
        )

        # Outstanding-lease cap: count this validator's in-progress leases and
        # reduce the grant so it never exceeds MAX_OUTSTANDING_LEASES.
        outstanding_rows = await prisma.query_raw(
            "SELECT COUNT(*)::int AS c FROM news_article_scoring "
            "WHERE validator_hotkey = $1 AND status = 'in_progress';",
            validator_hotkey,
        )
        outstanding = int((outstanding_rows or [{"c": 0}])[0]["c"])
        limit = v.grant_count(limit=limit, outstanding=outstanding,
                              max_outstanding=MAX_OUTSTANDING_LEASES)
        if limit <= 0:
            return NewsArticlesForScoringResponse(articles=[], count=0)

        # 2) Pick from two sources:
        #   A) Existing scoring records with status='pending'
        #   B) Articles with no scoring record and no analysis record

        # A: Atomically claim up to `limit` pending scorings. Pending is a
        # servable-only ready-queue: every servability filter (title non-empty,
        # content >= MIN, source in (rss,ccnews), title-not-already-in-pipeline) is
        # enforced at INSERT by the writers (scraper rss + ccnews_ingest + path B
        # below), and title/published are denormalized there too. So this is a BARE
        # ordered index walk on idx_scoring_pending_published (published DESC NULLS
        # LAST WHERE status='pending') — a pure Index Scan that halts at LIMIT
        # regardless of how large pending grows; no join to news_articles, no sort,
        # no runtime dedup. The clone gate is batch-local (validator scoring.py), and
        # the in-app response guards (seen_titles + the NULL/empty-title skip in the
        # loop below) remain as the final same-batch clone safety net.
        claimed_pending = await prisma.query_raw(
            """
            WITH picked AS (
                SELECT s.id, s.article_id
                FROM news_article_scoring s
                WHERE s.status = 'pending'
                ORDER BY s.published DESC NULLS LAST, s.id DESC
                FOR UPDATE SKIP LOCKED
                LIMIT $1
            )
            UPDATE news_article_scoring s
            SET status = 'in_progress',
                start_time = (NOW() AT TIME ZONE 'utc'),
                validator_hotkey = $2
            FROM picked
            WHERE s.id = picked.id
            RETURNING picked.article_id;
            """,
            limit,
            validator_hotkey,
        )
        article_ids_pending = [row["article_id"] for row in (claimed_pending or [])]

        # If need more, get up to `slots_left` articles that have no scoring and no analysis
        slots_left = max(0, limit - len(article_ids_pending))
        article_ids_no_scoring = []
        if slots_left > 0:
            inserted_rows = await prisma.query_raw(
                """
                WITH unscored_articles AS (
                    SELECT a.id AS article_id, a.title AS title, a.published AS published
                    FROM news_articles a
                    LEFT JOIN news_article_scoring s ON s.article_id = a.id
                    LEFT JOIN news_article_analysis na ON na.article_id = a.id
                    WHERE s.id IS NULL AND na.id IS NULL
                      AND a.title IS NOT NULL
                      AND BTRIM(a.title) <> ''
                      AND a.content IS NOT NULL
                      AND LENGTH(BTRIM(a.content)) >= $3
                      AND a.source_type IN ('rss', 'ccnews')
                      -- Recency bound (idx_news_articles_created): keep this scan an
                      -- index range on recently-ingested rows instead of a full-table
                      -- anti-join. Without it, an empty `pending` queue makes every
                      -- request run a ~60s whole-table scan that exhausts the pool (the
                      -- corpus is title-saturated, so almost nothing qualifies and the
                      -- ORDER BY published DESC walk is O(table)). Filtered on created_at
                      -- (ingestion time), so back-dated-but-fresh articles are still seen.
                      AND a.created_at > NOW() - MAKE_INTERVAL(hours => $4::int)
                      -- Title dedup: don't enter a second article with a title that is
                      -- already in the scoring pipeline. Duplicate titles produce
                      -- identical title_embeddings and trip the validator's
                      -- cloned-embeddings anti-cheat gate (cosine=1.0), zeroing batches.
                      AND NOT EXISTS (
                          -- Probe the denormalized scoring.title (idx_news_scoring_title_status)
                          -- instead of the whole-table anti-join over news_articles that
                          -- exhausted the connection pool.
                          SELECT 1 FROM news_article_scoring s2
                          WHERE s2.title = a.title
                      )
                    ORDER BY a.published DESC NULLS LAST, a.id DESC
                    LIMIT $1
                    FOR UPDATE OF a SKIP LOCKED
                ), created_scoring AS (
                    -- Populate the denormalized title so the read-side dedup can probe
                    -- an index on news_article_scoring instead of joining back to
                    -- news_articles (see idx_news_scoring_title_status).
                    INSERT INTO news_article_scoring (article_id, title, published, status, start_time, validator_hotkey, created_at)
                    SELECT article_id, title, published, 'in_progress', (NOW() AT TIME ZONE 'utc'), $2, (NOW() AT TIME ZONE 'utc')
                    FROM unscored_articles
                    RETURNING article_id
                )
                SELECT article_id FROM created_scoring;
                """,
                slots_left,
                validator_hotkey,
                MIN_ARTICLE_CONTENT_CHARS,
                BRANCH_B_RECENCY_HOURS,
            )
            article_ids_no_scoring = [row["article_id"] for row in (inserted_rows or [])]

        # Combine all claimed article ids
        article_ids = article_ids_pending + article_ids_no_scoring
        if not article_ids:
            return NewsArticlesForScoringResponse(articles=[], count=0)

        # Fetch the claimed articles for response.
        articles = await prisma.newsarticle.find_many(
            where={"id": {"in": article_ids}},
        )

        # Preserve claim order
        articles_by_id = {a.id: a for a in articles}
        ordered = [articles_by_id.get(aid) for aid in article_ids if aid in articles_by_id]

        articles_for_scoring = []
        seen_titles = set()
        for article in ordered:
            # Defensive safety check: never send articles with NULL/empty/whitespace-only title.
            if article is None or article.title is None or not str(article.title).strip():
                continue

            # Title dedup (final guard): never put two same-title articles in one response.
            # Identical titles -> identical title_embeddings -> the validator's
            # cloned-embeddings gate zeros the batch. The NOT EXISTS in the lease query
            # stops new dup scoring records; this catches pre-existing pending dups and
            # same-fetch dups. Skipped dups keep their lease and revert via TTL, then get
            # dispatched alone in a later (non-duplicate) batch.
            _title_key = str(article.title).strip()
            if _title_key in seen_titles:
                continue
            seen_titles.add(_title_key)

            # Hard guard: never send a title/summary-only article. The miner must
            # never run full analysis on a record that lacks a real body.
            content = getattr(article, "content", None)
            if content is None or len(str(content).strip()) < MIN_ARTICLE_CONTENT_CHARS:
                logger.warning(
                    f"Skipping body-less article {article.id} (content too short) for {validator_hotkey}"
                )
                continue

            article_data = NewsArticleForScoring(
                id=article.id,
                url=article.url,
                title=article.title,
                summary=getattr(article, 'summary', None),
                content=getattr(article, 'content', None),
                # Only ship raw HTML when explicitly enabled (off by default —
                # it is large and rides in the miner synapse). When omitted the
                # analyzer falls back to the already-clean `content`.
                raw_html=getattr(article, 'rawHtml', None) if SERVE_RAW_HTML else None,
                published=article.published.isoformat() if hasattr(article, 'published') and article.published else None,
                source=article.source,
                topic=getattr(article, 'topic', None),
                extra=getattr(article, 'extra', None),
            )
            articles_for_scoring.append(article_data)

        logger.info(f"Leased {len(articles_for_scoring)} article(s) to validator {validator_hotkey}")
        return NewsArticlesForScoringResponse(articles=articles_for_scoring, count=len(articles_for_scoring))

    except Exception as e:
        logger.error(f"Error getting unscored articles: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get unscored articles: {str(e)}",
        )


def _strip_nul(o):
    """Recursively strip NUL bytes — Postgres text/jsonb reject \\u0000 (error 22P05);
    they only arrive from bad scraped content. Sanitize before any DB write."""
    if isinstance(o, str):
        return o.replace("\x00", "")
    if isinstance(o, dict):
        return {k: _strip_nul(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_strip_nul(v) for v in o]
    return o


@app.post(
    "/articles/variants",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def submit_analysis_variants(
    submission: dict,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """Store verifier analyses from overlap dispatch. Append-only; one row per
    (article, miner), later submissions for the same pair are ignored. Entries
    carrying verdict fields also record a signed verdict."""
    variants = submission.get("variants") or []
    if not isinstance(variants, list) or not variants:
        return SubmissionResponse(success=False, message="No variants", count=0)
    stored = failed = 0
    for v in variants[:500]:
        try:
            aid = int(v["article_id"]); mhk = str(v["miner_hotkey"])
            await prisma.analysisvariant.create(data={
                "articleId": aid,
                "minerHotkey": mhk,
                "analysisData": Json(_strip_nul(v["analysis_data"])),
            })
            stored += 1
        except (KeyError, TypeError, ValueError):
            continue   # malformed entry: drop
        except Exception as e:
            if "Unique constraint" in str(e):
                continue   # duplicate (article, miner): already stored
            failed += 1
            continue
        if v.get("miner_signature") and v.get("epoch") is not None:
            # Verdicts only for items this validator leased.
            owned = await prisma.newsarticlescoring.find_first(
                where={"articleId": aid, "validatorHotkey": validator_hotkey})
            if owned is None:
                continue
            await write_verdict(
                resource_type="news_variant",
                resource_id=str(aid),
                storage_id=f"{aid}:{mhk}",
                validator_hotkey=validator_hotkey,
                miner_hotkey=mhk,
                miner_signature=v.get("miner_signature"),
                nonce=v.get("nonce"),
                miner_analysis_hash=v.get("miner_analysis_hash"),
                validator_verdict=v.get("validator_verdict"),
                categorical_key=v.get("categorical_key"),
                points_awarded=v.get("points_awarded"),
                epoch=v.get("epoch"),
            )
    ok = failed == 0 or stored > 0
    return SubmissionResponse(success=ok, message=f"Stored {stored}, failed {failed}", count=stored)


@app.post(
    "/articles/completed",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def submit_completed_articles(
    submission: CompletedNewsArticlesSubmission,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Submit completed scored news articles.

    Updates the scoring status to 'completed' and stores the analysis in news_article_analysis.
    Only articles assigned to the requesting validator can be completed.

    Only accessible by validators.
    """
    if not SERVE_NEWS_ARTICLES:
        return SubmissionResponse(success=False, message="News articles are not enabled", count=0)

    try:
        updated_count = 0

        for completed in submission.completed_articles:
            # Create or update news_article_analysis with sentiment + optional classification columns.
            analysis_create = {
                "articleId": completed.article_id,
                "sentiment": completed.sentiment,
                "analyzedAt": datetime.utcnow(),
            }
            analysis_update = {
                "sentiment": completed.sentiment,
                "updatedAt": datetime.utcnow(),
                "analyzedAt": datetime.utcnow(),
            }

            optional_fields = {
                "sectorId": completed.sector_id,
                "sectorSymbol": completed.sector_symbol,
                "contentType": completed.content_type,
                "technicalQuality": completed.technical_quality,
                "marketAnalysis": completed.market_analysis,
                "impactPotential": completed.impact_potential,
                "relevanceConfidence": completed.relevance_confidence,
                "minerHotkey": completed.miner_hotkey,
            }
            for k, v in optional_fields.items():
                if v is not None:
                    v = _strip_nul(v)
                    analysis_create[k] = v
                    analysis_update[k] = v

            # V2: Store full ArticleIntelligence in analysisData JSONB.
            # Strip NUL bytes once here: ad flows into the jsonb upsert AND the downstream
            # clustering/narrative-matching writes, all of which Postgres rejects (22P05).
            ad = _strip_nul(completed.analysis_data)
            if ad and isinstance(ad, dict):
                analysis_create["analysisData"] = Json(ad)
                analysis_update["analysisData"] = Json(ad)
                # Extract V2 indexed fields
                v2_fields = {
                    "impactLevel": ad.get("impact_potential"),
                    "factualConfidence": ad.get("factual_confidence"),
                    "eventType": ad.get("event_fingerprint", {}).get("event_type"),
                    "contentHash": ad.get("event_fingerprint", {}).get("content_hash"),
                    "primaryGeo": ad.get("primary_geo"),
                    "overallSentimentScore": ad.get("overall_sentiment_score"),
                }
                event_date_str = ad.get("event_fingerprint", {}).get("event_date")
                if event_date_str:
                    try:
                        v2_fields["eventDate"] = datetime.strptime(event_date_str, "%Y-%m-%d")
                    except (ValueError, TypeError):
                        pass
                for k, v in v2_fields.items():
                    if v is not None:
                        analysis_create[k] = v
                        analysis_update[k] = v

                # Store narrative embedding for matching and centroid drift
                narr_emb_raw = ad.get("narrative_embedding")
                if narr_emb_raw and isinstance(narr_emb_raw, list) and len(narr_emb_raw) == 384:
                    analysis_create["narrativeEmbedding"] = narr_emb_raw
                    analysis_update["narrativeEmbedding"] = narr_emb_raw

            await prisma.newsarticleanalysis.upsert(
                where={"articleId": completed.article_id},
                data={
                    "create": analysis_create,
                    "update": analysis_update,
                },
            )

            # Update scoring status to completed (only if still leased to this validator).
            result = await prisma.newsarticlescoring.update_many(
                where={
                    "articleId": completed.article_id,
                    "validatorHotkey": validator_hotkey,
                    "status": "in_progress",
                },
                data={"status": "completed"},
            )
            updated_count += result

            # V2: Event clustering + narrative matching (non-blocking)
            if ad and isinstance(ad, dict):
                try:
                    from services.event_clustering import cluster_article
                    from services.narrative_matcher import match_article_narratives
                    await cluster_article(prisma, completed.article_id, ad)
                    narrative_kws = ad.get("narrative_keywords", [])
                    sector = ad.get("topic_signature", {}).get("primary_sector_id")
                    narr_emb = ad.get("narrative_embedding")
                    if narr_emb and isinstance(narr_emb, list) and len(narr_emb) == 384:
                        pass  # valid embedding
                    else:
                        narr_emb = None
                    await match_article_narratives(
                        prisma, completed.article_id, narrative_kws, sector,
                        narrative_embedding=narr_emb,
                    )
                except Exception as clustering_err:
                    logger.warning(f"Event/narrative processing failed for article {completed.article_id}: {clustering_err}")

            # Record a signed verdict (Phase 1+: optional; skipped if fields/sig missing).
            # Only record a verdict if this validator actually held the lease for this
            # item (the update_many above matches only in_progress rows owned by us).
            # This enforces per-miner attribution: a validator cannot earn points for
            # items it was never leased.
            if result:
                await write_verdict(
                    resource_type="news",
                    resource_id=str(completed.article_id),
                    validator_hotkey=validator_hotkey,
                    miner_hotkey=completed.miner_hotkey,
                    miner_signature=completed.miner_signature,
                    nonce=completed.nonce,
                    miner_analysis_hash=completed.miner_analysis_hash,
                    validator_verdict=completed.validator_verdict,
                    categorical_key=completed.categorical_key,
                    points_awarded=completed.points_awarded,
                    epoch=completed.epoch,
                )

        logger.info(f"Validator {validator_hotkey} completed {updated_count} news articles")
        return SubmissionResponse(
            success=True,
            message=f"Successfully completed {updated_count} news articles",
            count=updated_count,
        )

    except Exception as e:
        logger.error(f"Error submitting completed news articles: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to submit completed news articles: {str(e)}",
        )


# ============================================================================
# Reward Routes
# ============================================================================

@app.post(
    "/rewards",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def submit_rewards(
    submission: RewardBulkCreate,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Submit rewards for miners.
    
    Creates reward records for the specified hotkeys with their points.
    
    Only accessible by validators.
    """
    try:
        created_count = 0
        
        for reward in submission.rewards:
            await prisma.reward.create(
                data={
                    "startBlock": reward.start_block,
                    "stopBlock": reward.stop_block,
                    "hotkey": reward.hotkey,
                    "points": reward.points,
                }
            )
            created_count += 1
        
        logger.info(f"Validator {validator_hotkey} submitted {created_count} rewards")
        return SubmissionResponse(
            success=True,
            message=f"Successfully created {created_count} rewards",
            count=created_count,
        )
    
    except Exception as e:
        logger.error(f"Error submitting rewards: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to submit rewards: {str(e)}",
        )


@app.get(
    "/rewards",
    response_model=List[Reward],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_rewards(
    hotkey: Optional[str] = None,
    limit: int = 100,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Get rewards, optionally filtered by hotkey.
    
    Only accessible by validators.
    """
    try:
        where = {"hotkey": hotkey} if hotkey else {}
        rewards = await prisma.reward.find_many(
            where=where,
            take=limit,
            order={"id": "desc"},
        )
        
        return [
            Reward(
                id=r.id,
                startBlock=r.startBlock,
                stopBlock=r.stopBlock,
                hotkey=r.hotkey,
                points=r.points,
                createdAt=r.createdAt,
            )
            for r in rewards
        ]
    
    except Exception as e:
        logger.error(f"Error getting rewards: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get rewards: {str(e)}",
        )


# ============================================================================
# Penalty Routes
# ============================================================================

@app.post(
    "/penalties",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def submit_penalties(
    submission: PenaltyBulkCreate,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Submit penalties for miners.
    
    Creates penalty records for the specified hotkeys with reasons.
    
    Only accessible by validators.
    """
    try:
        created_count = 0
        
        for penalty in submission.penalties:
            await prisma.penalty.create(
                data={
                    "hotkey": penalty.hotkey,
                    "reason": penalty.reason,
                    "timestamp": datetime.utcnow(),
                }
            )
            created_count += 1
        
        logger.info(f"Validator {validator_hotkey} submitted {created_count} penalties")
        return SubmissionResponse(
            success=True,
            message=f"Successfully created {created_count} penalties",
            count=created_count,
        )
    
    except Exception as e:
        logger.error(f"Error submitting penalties: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to submit penalties: {str(e)}",
        )


@app.get(
    "/penalties",
    response_model=List[Penalty],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_penalties(
    hotkey: Optional[str] = None,
    limit: int = 100,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Get penalties, optionally filtered by hotkey.
    
    Only accessible by validators.
    """
    try:
        where = {"hotkey": hotkey} if hotkey else {}
        penalties = await prisma.penalty.find_many(
            where=where,
            take=limit,
            order={"timestamp": "desc"},
        )
        
        return [
            Penalty(
                id=p.id,
                hotkey=p.hotkey,
                reason=p.reason,
                timestamp=p.timestamp,
            )
            for p in penalties
        ]
    
    except Exception as e:
        logger.error(f"Error getting penalties: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get penalties: {str(e)}",
        )


# ============================================================================
# Blacklisted Hotkeys Routes
# ============================================================================

@app.get(
    "/blacklist",
    response_model=List[BlacklistedHotkey],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_blacklisted_hotkeys(
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Get all blacklisted hotkeys.
    
    Only accessible by validators.
    """
    try:
        blacklisted = await prisma.blacklistedhotkey.find_many()
        return [
            BlacklistedHotkey(
                hotkey=b.hotkey,
                reason=b.reason,
                createdAt=b.createdAt,
            )
            for b in blacklisted
        ]
    
    except Exception as e:
        logger.error(f"Error getting blacklisted hotkeys: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get blacklisted hotkeys: {str(e)}",
        )


@app.post(
    "/blacklist",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def add_blacklisted_hotkeys(
    submission: BlacklistedHotkeyBulkCreate,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Add hotkeys to the blacklist.
    
    Only accessible by validators.
    """
    try:
        created_count = 0
        
        for hotkey in submission.hotkeys:
            # Use upsert to avoid duplicates
            await prisma.blacklistedhotkey.upsert(
                where={"hotkey": hotkey},
                data={
                    "create": {
                        "hotkey": hotkey,
                        "reason": submission.reason,
                    },
                    "update": {
                        "reason": submission.reason,
                    },
                },
            )
            created_count += 1
        
        logger.info(f"Validator {validator_hotkey} added {created_count} hotkeys to blacklist")
        return SubmissionResponse(
            success=True,
            message=f"Successfully added {created_count} hotkeys to blacklist",
            count=created_count,
        )
    
    except Exception as e:
        logger.error(f"Error adding blacklisted hotkeys: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to add blacklisted hotkeys: {str(e)}",
        )


@app.delete(
    "/blacklist/{hotkey}",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def remove_blacklisted_hotkey(
    hotkey: str,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Remove a hotkey from the blacklist.
    
    Only accessible by validators.
    """
    try:
        # Check if hotkey exists
        existing = await prisma.blacklistedhotkey.find_unique(where={"hotkey": hotkey})
        
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Hotkey {hotkey} not found in blacklist",
            )
        
        await prisma.blacklistedhotkey.delete(where={"hotkey": hotkey})
        
        logger.info(f"Validator {validator_hotkey} removed hotkey {hotkey} from blacklist")
        return SubmissionResponse(
            success=True,
            message=f"Successfully removed hotkey {hotkey} from blacklist",
            count=1,
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing blacklisted hotkey: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove blacklisted hotkey: {str(e)}",
        )


# ============================================================================
# Attestation Endpoint
# ============================================================================

@app.get(
    "/attestation",
    response_model=AttestationResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_attestation(epoch: int, validator_hotkey: str = Depends(get_validator_hotkey)):
    """Recompute this validator's per-miner budget for `epoch` from ScoreVerdict,
    build a Merkle root, sign it with the API sr25519 key, upsert, and return it."""
    # §4 scoped-test allowlist: when configured, only issue attestations for listed validators.
    if VERDICT_ALLOWLIST_HOTKEYS and validator_hotkey not in VERDICT_ALLOWLIST_HOTKEYS:
        raise HTTPException(status_code=403, detail="validator not in verdict allowlist")
    try:
        rows = await prisma.scoreverdict.find_many(
            where={"validatorHotkey": validator_hotkey, "epoch": int(epoch)},
        )
        leaf_dicts = [{
            "resource_type": r.resourceType,
            "resource_id": r.resourceId,
            "miner_hotkey": r.minerHotkey,
            "validator_verdict": r.validatorVerdict,
            "categorical_key": r.categoricalKey,
            "points_awarded": r.pointsAwarded,
        } for r in rows]

        per_miner = v.compute_budget(leaf_dicts)
        total = float(sum(per_miner.values()))
        root = ac.merkle_root(leaf_dicts)
        msg = ac.attestation_message(validator_hotkey, int(epoch), per_miner, total, root)
        keypair = ac.load_signing_key()
        signature = ac.sign_attestation(keypair, msg)

        await prisma.attestation.upsert(
            where={"uq_attestation_validator_epoch": {
                "validatorHotkey": validator_hotkey, "epoch": int(epoch)}},
            data={
                "create": {"validatorHotkey": validator_hotkey, "epoch": int(epoch),
                           "perMinerPoints": Json(per_miner), "totalPoints": total,
                           "merkleRoot": root, "signature": signature},
                "update": {"perMinerPoints": Json(per_miner), "totalPoints": total,
                           "merkleRoot": root, "signature": signature},
            },
        )
        return AttestationResponse(
            validator_hotkey=validator_hotkey, epoch=int(epoch),
            per_miner_points=per_miner, total_points=total,
            merkle_root=root, signature=signature,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"get_attestation error: {e}")
        raise HTTPException(status_code=500, detail=f"Attestation failed: {e}")


# ============================================================================
# Reports Endpoint
# ============================================================================

@app.post(
    "/reports",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def post_report(report: BroadcastReportCreate,
                      validator_hotkey: str = Depends(get_validator_hotkey)):
    """Record a receiver-flagged discrepancy (always written to the broadcast_report
    paper trail). On >= REPORT_CONSENSUS_THRESHOLD distinct reporters for (accused, epoch),
    raise an alarm — and auto-blacklist ONLY if REPORTS_AUTO_BLACKLIST is enabled (§3).
    Default is alarm-only: a deep-verify mismatch can be benign API-side data drift, and
    sybil reporters must not be able to knock out an honest validator automatically."""
    await prisma.broadcastreport.upsert(
        where={"uq_broadcast_report": {
            "reporterHotkey": validator_hotkey, "accusedHotkey": report.accused_hotkey,
            "epoch": int(report.epoch), "reason": report.reason}},
        data={"create": {
            "reporterHotkey": validator_hotkey, "accusedHotkey": report.accused_hotkey,
            "epoch": int(report.epoch), "reason": report.reason, "evidence": Json(report.evidence)},
            "update": {"evidence": Json(report.evidence)}},
    )

    distinct = await prisma.broadcastreport.find_many(
        where={"accusedHotkey": report.accused_hotkey, "epoch": int(report.epoch)},
        distinct=["reporterHotkey"],
    )
    reporter_count = len({r.reporterHotkey for r in distinct})
    if v.has_report_consensus(reporter_count, REPORT_CONSENSUS_THRESHOLD):
        if REPORTS_AUTO_BLACKLIST:
            await prisma.blacklistedhotkey.upsert(
                where={"hotkey": report.accused_hotkey},
                data={"create": {"hotkey": report.accused_hotkey,
                                 "reason": f"report_consensus:{report.reason}:epoch{report.epoch}"},
                      "update": {"reason": f"report_consensus:{report.reason}:epoch{report.epoch}"}},
            )
            logger.warning(f"Blacklisted {report.accused_hotkey[:12]}.. on "
                           f"{reporter_count} reports (reason={report.reason}, epoch={report.epoch})")
        else:
            logger.warning(
                f"[ALARM] Report consensus reached for {report.accused_hotkey[:12]}.. "
                f"({reporter_count} distinct reporters, reason={report.reason}, epoch={report.epoch}) "
                f"— NOT auto-blacklisting (alarm-only mode). Review manually before any action."
            )
    return SubmissionResponse(success=True, message="report recorded", count=reporter_count)


# ============================================================================
# Verdicts Endpoint
# ============================================================================

@app.get(
    "/verdicts",
    response_model=VerdictsResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_verdicts(validator: str, epoch: int,
                       validator_hotkey: str = Depends(get_validator_hotkey)):
    """Return the raw verdict leaves for (validator, epoch) so a receiver can rebuild
    the Merkle root and confirm it matches the signed attestation. Any validator may
    fetch any other validator's leaves (this is the trust-but-verify backstop)."""
    rows = await prisma.scoreverdict.find_many(
        where={"validatorHotkey": validator, "epoch": int(epoch)},
    )
    leaves = [VerdictLeaf(
        resource_type=r.resourceType, resource_id=r.resourceId,
        miner_hotkey=r.minerHotkey, validator_verdict=r.validatorVerdict,
        categorical_key=r.categoricalKey, points_awarded=r.pointsAwarded,
    ) for r in rows]
    return VerdictsResponse(validator_hotkey=validator, epoch=int(epoch),
                            verdicts=leaves, count=len(leaves))


# ============================================================================
# Diagnostics Endpoint (display-only penalty attribution for the dashboard)
# ============================================================================

@app.post(
    "/diagnostics/penalty-detail",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def submit_penalty_detail(
    submission: PenaltyDetailBulkCreate,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """Persist display-only penalty attribution for the miner dashboard.

    DECOUPLED from consensus by design: writes ONLY to `penalty_detail` and never
    touches score_verdict, attestation, get_verdicts, or merkle_root. Carries no
    signatures, hashes, or points — it is explanatory data; the authoritative number
    already reached the miner (signed) via the Score synapse. Best-effort: a bad row
    is skipped rather than failing the batch, and the flagging validator hotkey is
    taken from auth (not the payload) so it can't be spoofed per-row.
    """
    if not submission.items:
        return SubmissionResponse(success=True, message="no penalty detail rows", count=0)

    created = 0
    for it in submission.items:
        try:
            data = {
                "minerHotkey": it.miner_hotkey,
                # From the authenticated caller, not the row — un-spoofable attribution.
                "validatorHotkey": validator_hotkey,
                "epoch": int(it.epoch),
                "resourceType": it.resource_type,
                "resourceId": str(it.resource_id),
                "cause": it.cause,
                "postPreview": it.post_preview,
                "score": it.score,   # nullable scalar — None is fine (only Json? fields reject None)
            }
            # Optional Json? fields must be OMITTED when null — prisma-client-py
            # rejects an explicit None for a Json field ("value required but not
            # set"), which was silently dropping timeout-type rows.
            if it.failed_fields is not None:
                data["failedFields"] = Json(it.failed_fields)
            if it.miner_values is not None:
                data["minerValues"] = Json(it.miner_values)
            if it.validator_values is not None:
                data["validatorVals"] = Json(it.validator_values)
            await prisma.penaltydetail.create(data=data)
            created += 1
        except Exception as e:
            # Never let one malformed row drop the rest; this is explanatory data only.
            logger.warning(f"penalty-detail row skipped (non-fatal): {e}")
            continue

    logger.info(f"Validator {validator_hotkey} submitted {created}/{len(submission.items)} penalty-detail rows")
    return SubmissionResponse(success=True, message="penalty detail recorded", count=created)


@app.post(
    "/diagnostics/dispatch-status",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def submit_dispatch_status(
    submission: DispatchStatusBulkCreate,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """Store the latest per-miner adaptive-dispatch status from a validator (RFC 2026-06-28).

    DECOUPLED from consensus by design: display-only status for the miner dashboard
    (window, in-flight, liveness, cooldown, etc.), never an input to scoring,
    attestation, or weights. Latest-snapshot-per-validator only (no history), kept in
    a lightweight disk-backed store rather than Prisma. The validator hotkey is taken
    from auth, not the payload, so attribution can't be spoofed.
    """
    miners = [it.model_dump() for it in submission.miners]
    dispatch_status_store.set_status(
        validator_hotkey=validator_hotkey,
        updated=datetime.now(timezone.utc).isoformat(),
        miners=miners,
    )
    logger.info(f"Validator {validator_hotkey} submitted dispatch status for {len(miners)} miner(s)")
    return SubmissionResponse(success=True, message="dispatch status recorded", count=len(miners))


@app.post(
    "/diagnostics/miner-event",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def submit_miner_event(
    submission: MinerEventBulkCreate,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """Persist display-only per-miner dispatch/cooldown events for the miner dashboard.

    DECOUPLED from consensus by design: writes ONLY to `miner_event` and never touches
    score_verdict, attestation, get_verdicts, or merkle_root. Append-only event log
    (park/unpark, batch-size change, reward-zeroed) so a miner can see WHEN and WHY their
    dispatch state changed. Best-effort: a bad row is skipped rather than failing the
    batch, and the validator hotkey is taken from auth (not the payload) so it can't be
    spoofed per-row.
    """
    if not submission.events:
        return SubmissionResponse(success=True, message="no miner events", count=0)

    created = 0
    for ev in submission.events:
        try:
            data = {
                "minerHotkey": ev.miner_hotkey,
                # From the authenticated caller, not the row — un-spoofable attribution.
                "validatorHotkey": validator_hotkey,
                "eventType": ev.event_type,
                "occurredAt": ev.occurred_at,
                "streak": ev.streak,
                "shadow": ev.shadow,
                "epoch": ev.epoch,
                "reason": ev.reason,
            }
            # Optional Json? field must be OMITTED when null (prisma-client-py rejects None).
            if ev.detail is not None:
                data["detail"] = Json(ev.detail)
            await prisma.minerevent.create(data=data)
            created += 1
        except Exception as e:
            # Never let one malformed row drop the rest; this is explanatory data only.
            logger.warning(f"miner-event row skipped (non-fatal): {e}")
            continue

    logger.info(f"Validator {validator_hotkey} submitted {created}/{len(submission.events)} miner-event row(s)")
    return SubmissionResponse(success=True, message="miner events recorded", count=created)


# ============================================================================
# Dashboard Endpoints (read-only, restricted to local / box IPs)
# ============================================================================

from dashboard_routes import router as dashboard_router
app.include_router(dashboard_router)


# ============================================================================
# Main Entry Point
# ============================================================================

# ============================================================================
# MECHANISM PROFILE
# ============================================================================
# One signed, versioned object carrying every economic value. Validators resolve
# between `current` and `next` by block height, so config and code land together and a
# half-applied mechanism state cannot exist. Append-only: there is no edit or delete
# path, and rolling back is publishing the old body under a higher version.

_PROFILE_CACHE_SECONDS = 60
_profile_cache: dict = {"at": 0.0, "body": None}


def _profile_row_to_dict(row) -> dict:
    body = dict(row.body or {})
    body["signature"] = row.signature
    return body


async def _load_profiles() -> dict:
    """The two profiles a validator needs: what is in force, and what is staged.

    Both are decided by block height on the validator, so this only splits the history
    into the newest published version and the one before it.
    """
    rows = await prisma.mechanismprofile.find_many(
        order={"version": "desc"}, take=2)
    if not rows:
        return {"current": None, "next": None}
    if len(rows) == 1:
        return {"current": _profile_row_to_dict(rows[0]), "next": None}
    return {"current": _profile_row_to_dict(rows[1]),
            "next": _profile_row_to_dict(rows[0])}


@app.get(
    "/mechanism/profile",
    response_model=MechanismProfileResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_mechanism_profile(validator_hotkey: str = Depends(get_validator_hotkey)):
    """Serve the signed profiles to validators only.

    Authenticated deliberately. The signature makes the contents trustworthy, but it
    does not make them safe to publish: the grader model list would let a submitter
    tune its output to the models that grade it, which is what the rotation exists to
    prevent. The handoff spec calls for an unauthenticated read; that is the one part
    of it we do not follow.
    """
    now = time.time()
    if _profile_cache["body"] is not None and (now - _profile_cache["at"]) < _PROFILE_CACHE_SECONDS:
        return MechanismProfileResponse(**_profile_cache["body"])

    try:
        payload = await _load_profiles()
    except Exception as e:
        logger.error(f"Failed to load mechanism profiles: {e}")
        if _profile_cache["body"] is not None:
            return MechanismProfileResponse(**_profile_cache["body"])
        raise HTTPException(status_code=503, detail="profile unavailable")

    _profile_cache.update({"at": now, "body": payload})
    return MechanismProfileResponse(**payload)


@app.get(
    "/mechanism/anchor",
    response_model=MechanismAnchorResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_mechanism_anchor(validator_hotkey: str = Depends(get_validator_hotkey)):
    """Publish what capacity is, and what the crawl actually supplies.

    Capacity means the work that exists to be done, so the reference derives from
    ingest rather than from what miners produced: anchoring to trailing output would
    hold burn at a constant whatever anyone did. Published, never enforced.
    """
    try:
        profiles = await _load_profiles()
        current = profiles.get("current") or {}
        capacity = ((current.get("settlement") or {}).get("C"))

        since = datetime.now(timezone.utc) - timedelta(days=7)
        usable_ingest = await prisma.newsarticle.count(where={"createdAt": {"gte": since}})
        rewarded = await prisma.reward.find_many(where={"createdAt": {"gte": since}})
        points = float(sum(float(r.reward or 0) for r in rewarded))

        epochs = 7 * 72.0
        ingest_per_epoch = usable_ingest / epochs if epochs else None
        pts_per_article = (points / usable_ingest) if usable_ingest else None
        anchor = (ingest_per_epoch * pts_per_article
                  if ingest_per_epoch and pts_per_article else None)

        latest = await prisma.controllerproposal.find_first(order={"epoch": "desc"})
        return MechanismAnchorResponse(
            capacity=capacity,
            capacity_anchor=anchor,
            anchor_ratio=(capacity / anchor if capacity and anchor else None),
            usable_ingest_per_epoch=ingest_per_epoch,
            pts_per_article=pts_per_article,
            roi_ema=(float(latest.roiEma) if latest else None),
            last_step=({"epoch": latest.epoch, "direction": latest.direction,
                        "magnitude": latest.magnitude} if latest else None),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to build mechanism anchor: {e}")
        raise HTTPException(status_code=503, detail="anchor unavailable")


@app.post(
    "/mechanism/proposal",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def submit_controller_proposal(
    submission: ControllerProposalSubmission,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """Record that a validator's controller step rule armed.

    INFORMATIONAL ONLY. This never changes capacity: capacity moves when an operator
    publishes a profile carrying the new value, with the usual activation lead, so
    there is no path here that alters what anyone is paid.
    """
    try:
        await prisma.controllerproposal.create(data={
            "validatorHotkey": validator_hotkey,
            "epoch": int(submission.epoch),
            "roiEma": float(submission.roi_ema),
            "direction": int(submission.direction),
            "magnitude": float(submission.magnitude),
        })
        return SubmissionResponse(success=True, message="proposal recorded", count=1)
    except Exception as e:
        logger.error(f"Failed to record controller proposal: {e}")
        raise HTTPException(status_code=500, detail="could not record proposal")


if __name__ == "__main__":
    import uvicorn
    
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))
    
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=os.getenv("API_RELOAD", "false").lower() == "true",
        access_log=False,  # Disable uvicorn access logging entirely
    )
