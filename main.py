"""
FastAPI v2 - Probabilistic Validation System
- Miners submit posts with adjustable submission rate limits (per block window)
- 20% chance (adjustable) of submission being chosen for validation
- Validators GET validation payloads (post to validate)
- Validators GET scores endpoint to retrieve average scores per block window (global window for all hotkeys)
- Validators POST validation results (success/failure)
- Reward is set based on validation result (average score for current block window if success, 0 if failure)
"""

# Load environment variables from .env file
import os
try:
    from dotenv import load_dotenv
    dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
    load_dotenv(dotenv_path)
except ImportError:
    # dotenv not available, rely on system environment variables
    pass

from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import time
import json
import asyncio

from models import PostSubmission, ValidationPayload, ValidationPayloadsResponse, ValidationResult, ValidationResultsPayload
from database import (
    init_database,
    insert_submission,
    select_post_for_validation,
    get_pending_validations,
    record_validation_result,
    MAX_SUBMISSION_RATE,
    get_all_hotkey_scores_last_block_window,
    VALIDATIONS_PER_REQUEST,
    BLOCKS_PER_WINDOW,
    init_connection_pool,
    close_connection_pool,
    get_rate_limit_info,
    get_block_window_start,
    get_block_window_end,
)
from auth_utils import (
    auth_config,
    extract_auth_from_headers,
    verify_auth_request,
    AuthRequest
)
from hotkey_whitelist import is_miner_hotkey as check_miner, is_validator_hotkey as check_validator, initialize_whitelists, is_blacklisted
from block_utils import get_current_block

# Configuration
# Validation selection probability (configured via VALIDATION_PROBABILITY env var; default 20%)
_raw_validation_prob = float(os.getenv("VALIDATION_PROBABILITY", "0.20"))
if not (0.0 < _raw_validation_prob <= 1.0):
    print(f"[API v2] WARNING: VALIDATION_PROBABILITY={_raw_validation_prob} out of range (0,1], clamping to 0.20")
    _raw_validation_prob = 0.20
VALIDATION_PROBABILITY = _raw_validation_prob

# -------------------------
# Application Lifespan
# -------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_database()
    init_connection_pool()
    
    # Initialize whitelists (miner hotkeys from metagraph, validator hotkeys from file)
    print("[API v2] Initializing hotkey whitelists...")
    initialize_whitelists()
    
    # Refresh auth config to include the newly loaded whitelists
    auth_config.refresh_whitelist()
    print(f"[API v2] Auth whitelist refreshed: {len(auth_config.allowed_hotkeys)} total hotkeys")
    print(f"[API v2] Validation probability: {VALIDATION_PROBABILITY*100:.1f}%")
    print(f"[API v2] Max submission rate: {MAX_SUBMISSION_RATE} per {BLOCKS_PER_WINDOW} blocks (~{BLOCKS_PER_WINDOW * 12 / 60:.1f} minutes)")
    print(f"[API v2] Validations per request: {VALIDATIONS_PER_REQUEST}")
    
    yield
    # Shutdown
    close_connection_pool()
    print("[API v2] Shutting down")

app = FastAPI(
    title="Miner API (v2)", 
    description="Probabilistic validation system - miners submit posts, validators validate individual posts",
    lifespan=lifespan
)

# -------------------------
# Exception Handlers
# -------------------------
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Log validation errors for debugging."""
    errors = exc.errors()
    error_details = []
    for error in errors:
        error_details.append({
            "field": ".".join(str(loc) for loc in error.get("loc", [])),
            "message": error.get("msg"),
            "type": error.get("type"),
            "input": error.get("input")
        })
    
    print(f"[API v2] ✗ Validation error on {request.method} {request.url.path}")
    print(f"[API v2] Validation errors: {json.dumps(error_details, indent=2)}")
    
    return JSONResponse(
        status_code=422,
        content={
            "detail": error_details,
            "message": "Validation error - check field details"
        }
    )

# -------------------------
# Health Endpoints
# -------------------------
@app.get("/")
async def root():
    return {"message": "Miner API v2 is running", "status": "healthy", "version": "2.0"}


@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "2.0"}


@app.get("/v2/status")
async def get_status(request: Request):
    """
    Get current API status including block number and window information.
    
    Useful for miners to synchronize their block tracking with the API.
    No authentication required (public endpoint).
    
    Returns:
        - current_block: Current block number (API's view)
        - window_start_block: Start block of current window
        - window_end_block: End block of current window
        - next_window_start_block: Start block of next window
        - blocks_per_window: Number of blocks per window
        - blocks_until_next_window: Blocks until next window starts
    """
    # Run blocking call in executor to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    current_block = await loop.run_in_executor(None, get_current_block)
    window_start = get_block_window_start(current_block)
    window_end = get_block_window_end(current_block)
    next_window_start = window_start + BLOCKS_PER_WINDOW
    blocks_until_next_window = next_window_start - current_block
    
    return {
        "status": "ok",
        "current_block": current_block,
        "window_start_block": window_start,
        "window_end_block": window_end,
        "next_window_start_block": next_window_start,
        "blocks_per_window": BLOCKS_PER_WINDOW,
        "blocks_until_next_window": blocks_until_next_window,
        "current_window": current_block // BLOCKS_PER_WINDOW,
    }


# -------------------------
# V1 Deprecation - Catch-all route for /v1/ endpoints
# -------------------------
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def v1_deprecated(path: str, request: Request):
    """
    Catch-all route for all /v1/ endpoints.
    Returns a deprecation message asking users to update to v2.
    """
    return JSONResponse(
        status_code=410,  # 410 Gone - indicates the resource is no longer available
        content={
            "error": "deprecated",
            "message": "Please update to API v2. The v1 API is no longer supported.",
            "current_version": "2.0"
        }
    )


# -------------------------
# Authentication Helper Functions
# -------------------------
# Access control is three layers:
#   1. Signature verification (auth_utils) - validates signed headers
#   2. Whitelist check (hotkey_whitelist) - hotkey in metagraph miners/validators
#   3. Blacklist check (miners only) - prefix-based blocking in /v2/submit
# See README.md "Authentication & Access Control" for details.
# -------------------------
async def verify_miner_auth(request: Request) -> AuthRequest:
    """Verify authentication for miner endpoints"""
    if not auth_config.enabled:
        return None
    
    auth_request = extract_auth_from_headers(request)
    if not auth_request:
        raise HTTPException(status_code=401, detail="Authentication required for miners")
    
    if not verify_auth_request(auth_request, auth_config):
        raise HTTPException(status_code=403, detail="Authentication failed")
    
    # Verify the hotkey is actually a miner
    if not check_miner(auth_request.ss58_address):
        raise HTTPException(status_code=403, detail="Hotkey is not a registered miner")
    
    return auth_request

async def verify_validator_auth(request: Request) -> AuthRequest:
    """Verify authentication for validator endpoints"""
    if not auth_config.enabled:
        return None
    
    auth_request = extract_auth_from_headers(request)
    if not auth_request:
        raise HTTPException(status_code=401, detail="Authentication required for validators")
    
    if not verify_auth_request(auth_request, auth_config):
        raise HTTPException(status_code=403, detail="Authentication failed")
    
    # Verify the hotkey is actually a validator
    if not check_validator(auth_request.ss58_address):
        raise HTTPException(status_code=403, detail="Hotkey is not a registered validator")
    
    return auth_request

# -------------------------
# Submit Endpoint (idempotent per (miner_hotkey, post_id))
# -------------------------
@app.post("/v2/submit")
async def submit_post(
    post: PostSubmission,
    request: Request,
):
    """
    Submit a post for validation (unique on (miner_hotkey, post_id)).
    Returns status="new" for new submissions or status="duplicate" for duplicates.
    Requires miner authentication.
    
    With probability VALIDATION_PROBABILITY (default 20%), the submission will be
    selected for validation. If selected, it will be available for validators to GET.
    """
    # Verify miner authentication
    auth_request = await verify_miner_auth(request)
    
    # Verify that the authenticated hotkey matches the post's miner_hotkey
    if auth_request and auth_request.ss58_address != post.miner_hotkey:
        raise HTTPException(
            status_code=403, 
            detail=f"Authenticated hotkey {auth_request.ss58_address} does not match post miner_hotkey {post.miner_hotkey}"
        )
    
    # Check blacklist (configured via BLACKLISTED_HOTKEY_PREFIXES env var in hotkey_whitelist.py)
    if is_blacklisted(post.miner_hotkey):
        raise HTTPException(
            status_code=403,
            detail="Your key has been timed out due to consistent poor / inaccurate post submission"
        )
    
    print(f"[API v2] ========== POST /v2/submit ==========")
    print(f"[API v2] Received submission: post_id={post.post_id}, miner_hotkey={post.miner_hotkey}")
    if auth_request:
        print(f"[API v2] Authenticated hotkey: {auth_request.ss58_address}")
    print(f"[API v2] Post metadata: author={post.author}, date={post.date}, sentiment={post.sentiment}")
    print(f"[API v2] Tokens: {list(post.tokens.keys()) if post.tokens else 'None'}")

    now = int(time.time())
    print(f"[API v2] Inserting submission into database...")
    
    # Run database operations in executor to avoid blocking the event loop
    # get_current_block() is called inside insert_submission() with error handling
    loop = asyncio.get_event_loop()
    is_new, message, error_code, rate_limit_info = await loop.run_in_executor(None, insert_submission, post, now)
    print(f"[API v2] {message}")
    
    if error_code == "limit_exceeded":
        print(f"[API v2] ✗ Submission rejected: rate limit exceeded for hotkey={post.miner_hotkey}")
        
        # Build detailed error response with rate limit information
        if rate_limit_info:
            detail_message = (
                f"Submission rate limit exceeded. "
                f"You have used {rate_limit_info['current_count']}/{rate_limit_info['max_submissions']} submissions "
                f"in the current block window (blocks {rate_limit_info['window_start_block']}-{rate_limit_info['window_end_block']}). "
                f"Limit resets at block {rate_limit_info['next_window_start_block']} "
                f"(in ~{rate_limit_info['estimated_seconds_until_reset']:.0f} seconds / ~{rate_limit_info['estimated_seconds_until_reset']/60:.1f} minutes)."
            )
            # Include structured data in response (FastAPI will serialize dict to JSON)
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limit_exceeded",
                    "message": detail_message,
                    "rate_limit": {
                        "current_count": rate_limit_info['current_count'],
                        "max_submissions": rate_limit_info['max_submissions'],
                        "remaining": 0,
                        "current_block": rate_limit_info['current_block'],
                        "window_start_block": rate_limit_info['window_start_block'],
                        "window_end_block": rate_limit_info['window_end_block'],
                        "next_window_start_block": rate_limit_info['next_window_start_block'],
                        "blocks_until_reset": rate_limit_info['blocks_until_reset'],
                        "estimated_seconds_until_reset": int(rate_limit_info['estimated_seconds_until_reset']),
                        "blocks_per_window": rate_limit_info['blocks_per_window'],
                        "current_window": rate_limit_info['current_window'],
                    }
                },
                headers={
                    "X-RateLimit-Limit": str(rate_limit_info['max_submissions']),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset-Block": str(rate_limit_info['next_window_start_block']),
                    "X-RateLimit-Reset-Seconds": str(int(rate_limit_info['estimated_seconds_until_reset'])),
                }
            )
        else:
            # Fallback if rate_limit_info is not available
            raise HTTPException(
                status_code=429,
                detail=f"Submission rate limit exceeded. Maximum {MAX_SUBMISSION_RATE} submissions per {BLOCKS_PER_WINDOW} blocks (~{BLOCKS_PER_WINDOW * 12 / 60:.1f} minutes) allowed."
            )
    
    # Get rate limit info for synchronization (always include in response)
    rate_limit_info = await loop.run_in_executor(None, get_rate_limit_info, post.miner_hotkey)
    
    # Build base response with block/window info for synchronization
    base_response = {
        "current_block": rate_limit_info["current_block"],
        "window_start_block": rate_limit_info["window_start_block"],
        "window_end_block": rate_limit_info["window_end_block"],
        "next_window_start_block": rate_limit_info["next_window_start_block"],
        "blocks_per_window": rate_limit_info["blocks_per_window"],
        "current_window": rate_limit_info["current_window"],
        "rate_limit": {
            "current_count": rate_limit_info["current_count"],
            "max_submissions": rate_limit_info["max_submissions"],
            "remaining": max(0, rate_limit_info["max_submissions"] - rate_limit_info["current_count"]),
        }
    }
    
    if not is_new:
        print(f"[API v2] ⚠️  Duplicate submission ignored: post_id={post.post_id}")
        return {
            "status": "duplicate", 
            "message": "Post already exists, ignored",
            **base_response
        }
    
    # New submission - check if it should be selected for validation
    print(f"[API v2] Checking if post should be selected for validation (probability={VALIDATION_PROBABILITY*100:.1f}%)...")
    
    # Prepare post data dict for TwitterAPI.io validation
    post_data = {
        "content": post.content,
        "author": post.author,
        "date": post.date,
        "likes": post.likes,
        "retweets": post.retweets,
        "responses": post.responses,
        "followers": post.followers,
    }
    
    was_selected, validation_id, x_error = await loop.run_in_executor(
        None, 
        select_post_for_validation,
        post.miner_hotkey, 
        post.post_id,
        post_data,
        now, 
        VALIDATION_PROBABILITY
    )
    
    if was_selected:
        if validation_id:
            # Selected and TwitterAPI.io validation passed
            print(f"[API v2] ✓ Post selected for validation and TwitterAPI.io validation passed! validation_id={validation_id}")
            return {
                "status": "new", 
                "message": "Submission accepted and selected for validation",
                "selected_for_validation": True,
                "x_validation_passed": True,
                "validation_id": validation_id,
                **base_response
            }
        else:
            # Selected but TwitterAPI.io validation failed
            print(f"[API v2] ✗ Post selected but TwitterAPI.io validation failed: {x_error.get('code', 'unknown') if x_error else 'unknown'} - {x_error.get('message', 'N/A') if x_error else 'N/A'}")
            return {
                "status": "new", 
                "message": "Submission accepted but TwitterAPI.io validation failed",
                "selected_for_validation": True,
                "x_validation_passed": False,
                "x_validation_error": x_error,
                **base_response
            }
    else:
        print(f"[API v2] Post not selected for validation (random chance)")
        return {
            "status": "new", 
            "message": "Submission accepted",
            "selected_for_validation": False,
            **base_response
        }


# -------------------------
# Validation Payload Endpoint (for validators)
# -------------------------
@app.get("/v2/validation", response_model=ValidationPayloadsResponse)
async def get_validation_payloads(request: Request):
    """
    Validators GET validation payloads containing:
    - Up to VALIDATIONS_PER_REQUEST submissions (default: 5) assigned to this validator
    - Each payload includes the post to validate
    - Submissions are assigned on-demand (next unassigned submissions)
    
    Requires validator authentication.
    Returns empty list if no pending validations are available.
    
    Note: Average scores for hotkeys can be retrieved from GET /v2/scores
    """
    # Verify validator authentication
    auth_request = await verify_validator_auth(request)
    
    # Get validator hotkey from auth
    validator_hotkey = None
    if auth_request:
        validator_hotkey = auth_request.ss58_address
    
    if not validator_hotkey:
        raise HTTPException(status_code=401, detail="Validator authentication required")
    
    print(f"[API v2] ========== GET /v2/validation ==========")
    print(f"[API v2] Validator: {validator_hotkey}")
    
    now = int(time.time())
    # Run database operation in executor to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    payloads = await loop.run_in_executor(None, get_pending_validations, validator_hotkey, now)
    
    if not payloads:
        print(f"[API v2] No pending validations available for validator {validator_hotkey}")
        return ValidationPayloadsResponse(
            available=False,
            payloads=[],
            count=0
        )
    
    print(f"[API v2] Returning {len(payloads)} validation payload(s) for validator {validator_hotkey}")
    for i, payload in enumerate(payloads):
        print(f"[API v2]   [{i+1}] validation_id={payload['validation_id']}, miner_hotkey={payload['miner_hotkey']}")
    
    # Convert to ValidationPayload models
    validation_payloads = [
        ValidationPayload(
            validation_id=p["validation_id"],
            miner_hotkey=p["miner_hotkey"],
            post=p["post"],
            selected_at=p["selected_at"]
        )
        for p in payloads
    ]
    
    return ValidationPayloadsResponse(
        available=True,
        payloads=validation_payloads,
        count=len(validation_payloads)
    )


# -------------------------
# Scores Endpoint (for validators)
# -------------------------
@app.get("/v2/scores")
async def get_hotkey_scores(request: Request):
    """
    Validators GET average scores for all hotkeys from the previous completed block window.
    
    Returns a dictionary mapping miner_hotkey -> average_score.
    Only includes hotkeys that have submissions in the previous block window (default: 100 blocks, ~20 minutes).
    
    IMPORTANT: This endpoint returns scores from the PREVIOUS completed window, not the current window.
    This ensures that when a new window starts, validators can set rewards based on the completed
    previous window's scores, rather than an incomplete current window.
    
    The response includes block number metadata so validators can reference the specific block
    window that the scores were calculated for.
    
    Requires validator authentication.
    """
    # Verify validator authentication
    auth_request = await verify_validator_auth(request)
    
    if not auth_request:
        raise HTTPException(status_code=401, detail="Validator authentication required")
    
    print(f"[API v2] ========== GET /v2/scores ==========")
    print(f"[API v2] Validator: {auth_request.ss58_address}")
    
    now = int(time.time())
    # Run database operations in executor to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    scores, previous_window_end_block = await loop.run_in_executor(None, get_all_hotkey_scores_last_block_window)
    
    current_block = await loop.run_in_executor(None, get_current_block)
    
    # Calculate previous window boundaries
    current_window_start = get_block_window_start(current_block)
    previous_window_start = current_window_start - BLOCKS_PER_WINDOW
    previous_window_end = current_window_start - 1
    
    # If we're in the first window, previous_window_start will be negative
    if previous_window_start < 0:
        previous_window_start = 0
        previous_window_end = current_window_start - 1 if current_window_start > 0 else 0
    
    print(f"[API v2] Returning scores for {len(scores)} hotkey(s) (previous block window: {previous_window_start}-{previous_window_end}, current block: {current_block})")
    
    return {
        "scores": scores,
        "count": len(scores),
        "blocks_per_window": BLOCKS_PER_WINDOW,
        "block_window_start": previous_window_start,
        "block_window_end": previous_window_end,
        "current_block": current_block,
        "calculated_at": now,
        "calculated_at_block": current_block,
        "window_type": "previous"  # Indicate this is the previous window
    }


# -------------------------
# Validation Result Endpoint (for validators)
# -------------------------
@app.post("/v2/validation_result")
async def submit_validation_results(
    payload: ValidationResultsPayload,
    request: Request,
):
    """
    Validators submit validation results after validating posts.
    
    Accepts multiple validation results (one per payload received from GET /v2/validation).
    
    If validation succeeds (success=True), the average score is set as the reward.
    If validation fails (success=False), a 0 score is set.
    
    Requires validator authentication.
    """
    # Verify validator authentication
    auth_request = await verify_validator_auth(request)
    
    # Verify that the authenticated hotkey matches the payload's validator_hotkey
    if auth_request and auth_request.ss58_address != payload.validator_hotkey:
        raise HTTPException(
            status_code=403, 
            detail=f"Authenticated hotkey {auth_request.ss58_address} does not match payload validator_hotkey {payload.validator_hotkey}"
        )
    
    print(f"[API v2] ========== POST /v2/validation_result ==========")
    print(f"[API v2] Validator: {payload.validator_hotkey}, results count: {len(payload.results)}")
    if auth_request:
        print(f"[API v2] Authenticated hotkey: {auth_request.ss58_address}")
    
    now = int(time.time())
    successful_count = 0
    failed_count = 0
    
    # Run database operations in executor to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    
    # Process each validation result
    for i, result in enumerate(payload.results):
        print(f"[API v2] Processing result [{i+1}/{len(payload.results)}]: validation_id={result.validation_id}, miner_hotkey={result.miner_hotkey}, success={result.success}")
        
        if result.failure_reason:
            print(f"[API v2]   Failure reason: {result.failure_reason}")
        
        success = await loop.run_in_executor(
            None,
            record_validation_result,
            result.validator_hotkey,
            result.validation_id,
            result.miner_hotkey,
            result.success,
            result.failure_reason,
            now
        )
        
        if success:
            successful_count += 1
        else:
            failed_count += 1
            print(f"[API v2]   ✗ Error recording validation result for validation_id={result.validation_id}")
    
    if failed_count > 0:
        print(f"[API v2] ⚠️  {failed_count} result(s) failed to record")
    
    print(f"[API v2] ✓ Processed {len(payload.results)} result(s): {successful_count} successful, {failed_count} failed")
    
    return {
        "status": "ok",
        "message": f"Processed {len(payload.results)} validation result(s)",
        "successful": successful_count,
        "failed": failed_count
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

