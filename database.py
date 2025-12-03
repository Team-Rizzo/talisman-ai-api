"""
Database operations and connection management for API v2.
"""

import os
import psycopg2
import psycopg2.extras
import psycopg2.errors
from psycopg2 import pool
import threading
import json
import time
import sys
import uuid
from urllib.parse import quote_plus
from typing import Dict, List, Tuple, Optional

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
    load_dotenv(dotenv_path)
except ImportError:
    # dotenv not available, rely on system environment variables
    pass

from block_utils import get_current_block

# Scores state file path (persists scores per window)
# NOTE: This JSON file exists for backward compatibility and fast reads. The canonical
# source of per-window scores is the `windows` and `miner_window_scores` DB tables.
# External tools that still read this file will continue to work.
_SCORES_STATE_FILE = os.path.join(os.path.dirname(__file__), "scores_state.json")
SCORES_STATE_FILE = _SCORES_STATE_FILE  # Public API

# PostgreSQL connection configuration
# Can be configured via DATABASE_URL (full connection string) or individual components:
# - DB_HOST (default: localhost)
# - DB_PORT (default: 5432)
# - DB_USER (default: postgres)
# - DB_PASSWORD (default: postgres)
# - DB_NAME (default: miner_api_v2)
# If DATABASE_URL is set, it takes precedence over individual components

def get_database_url():
    """Construct PostgreSQL connection URL from environment variables."""
    # If DATABASE_URL is provided, use it directly
    if os.getenv("DATABASE_URL"):
        return os.getenv("DATABASE_URL")
    
    # Otherwise, construct from individual components
    db_host = os.getenv("DB_HOST", "localhost")
    db_port = os.getenv("DB_PORT", "5432")
    db_user = os.getenv("DB_USER", "postgres")
    db_password = os.getenv("DB_PASSWORD", "postgres")
    db_name = os.getenv("DB_NAME", "miner_api")
    
    # URL-encode password and username in case they contain special characters
    encoded_user = quote_plus(db_user)
    encoded_password = quote_plus(db_password)
    encoded_db_name = quote_plus(db_name)
    
    return f"postgresql://{encoded_user}:{encoded_password}@{db_host}:{db_port}/{encoded_db_name}"

DATABASE_URL = get_database_url()

# Connection pool (initialized on first use)
_connection_pool = None

# Thread-safe database access
_db_lock = threading.Lock()
db_lock = _db_lock  # Public API

# Tuning knobs (validated at import time)
MAX_SUBMISSION_RATE = int(os.getenv("MAX_SUBMISSION_RATE", "5"))
VALIDATIONS_PER_REQUEST = int(os.getenv("VALIDATIONS_PER_REQUEST", "5"))
BLOCKS_PER_WINDOW = int(os.getenv("BLOCKS_PER_WINDOW", "100"))
SECONDS_PER_BLOCK = float(os.getenv("SECONDS_PER_BLOCK", "12.0"))

if MAX_SUBMISSION_RATE <= 0:
    raise ValueError(f"MAX_SUBMISSION_RATE must be > 0, got {MAX_SUBMISSION_RATE}")
if VALIDATIONS_PER_REQUEST <= 0:
    raise ValueError(f"VALIDATIONS_PER_REQUEST must be > 0, got {VALIDATIONS_PER_REQUEST}")
if BLOCKS_PER_WINDOW <= 0:
    raise ValueError(f"BLOCKS_PER_WINDOW must be > 0, got {BLOCKS_PER_WINDOW}")
if SECONDS_PER_BLOCK <= 0:
    raise ValueError(f"SECONDS_PER_BLOCK must be > 0, got {SECONDS_PER_BLOCK}")

# Validation backend selection
# A "validation backend" is any object with a `validate_post(post_dict) -> (bool, Optional[Dict])` method.
# Currently supported: "twitterapi" (TwitterAPI.io) and "x" (X API via tweepy with rate limiting).
VALIDATION_BACKEND = os.getenv("VALIDATION_BACKEND", "twitterapi").lower()
if VALIDATION_BACKEND not in ("twitterapi", "x"):
    VALIDATION_BACKEND = "twitterapi"  # Default to twitterapi if invalid value

_BACKEND_NAME = "X API" if VALIDATION_BACKEND == "x" else "TwitterAPI.io"
print(f"[DB] Validation backend configured: {_BACKEND_NAME} (VALIDATION_BACKEND={VALIDATION_BACKEND!r})")

# Initialize validation backend at module level (lazy import to avoid errors if modules unavailable)
_validator_instance = None
_rate_limiter_instance = None

def _get_validator_instance():
    """Get or create the validator instance for the configured backend."""
    global _validator_instance, _rate_limiter_instance
    if VALIDATION_BACKEND == "x":
        if _rate_limiter_instance is None:
            from x_rate_limiter import get_rate_limiter
            _rate_limiter_instance = get_rate_limiter()
        return _rate_limiter_instance
    else:  # twitterapi (default)
        if _validator_instance is None:
            from twitterapi_validation import get_validator
            _validator_instance = get_validator()
        return _validator_instance


def init_connection_pool():
    """Initialize connection pool (called at startup)."""
    global _connection_pool
    if _connection_pool is None:
        minconn = int(os.getenv("DB_POOL_MIN", "5"))
        maxconn = int(os.getenv("DB_POOL_MAX", "20"))
        _connection_pool = pool.ThreadedConnectionPool(
            minconn=minconn,
            maxconn=maxconn,
            dsn=DATABASE_URL
        )
        print(f"[DB] Initialized connection pool (min={minconn}, max={maxconn})")


def close_connection_pool():
    """Close all connections in pool (called at shutdown)."""
    global _connection_pool
    if _connection_pool:
        _connection_pool.closeall()
        _connection_pool = None
        print("[DB] Closed connection pool")


def connect():
    """
    Get a database connection from the pool.
    We serialize writes with _db_lock for thread safety.
    Connections are returned to the pool after use via close_connection().
    """
    if _connection_pool is None:
        init_connection_pool()
    return _connection_pool.getconn()


def close_connection(conn):
    """Return connection to pool."""
    if _connection_pool and conn:
        try:
            _connection_pool.putconn(conn)
        except Exception as e:
            print(f"[DB] Error returning connection to pool: {e}", file=sys.stderr)
            # If pool is closed or connection is bad, try to close it directly
            try:
                conn.close()
            except Exception:
                pass


def get_cursor(conn):
    """
    Create a cursor with RealDictCursor factory for dictionary-like row access.
    """
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def generate_post_url(author: str, post_id: str) -> str:
    """
    Generate X (Twitter) post URL from author and post_id.
    Format: https://x.com/{author}/status/{post_id}
    """
    return f"https://x.com/{author}/status/{post_id}"


def init_database():
    """Initialize tables if they don't exist."""
    conn = None
    try:
        with _db_lock:
            conn = connect()
            c = get_cursor(conn)
            
            # Submissions table (stores all miner submissions)
            c.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                miner_hotkey    TEXT NOT NULL,
                post_id         TEXT NOT NULL,
                content         TEXT NOT NULL,
                date            INTEGER NOT NULL,
                author          TEXT NOT NULL,
                account_age     INTEGER NOT NULL,
                retweets        INTEGER NOT NULL,
                likes           INTEGER NOT NULL,
                responses       INTEGER NOT NULL,
                followers       INTEGER NOT NULL DEFAULT 0,
                tokens_json     TEXT NOT NULL,
                sentiment       REAL NOT NULL,
                score           REAL NOT NULL,
                accepted_at     INTEGER NOT NULL,
                accepted_block  INTEGER NOT NULL,
                selected_for_validation INTEGER DEFAULT 0,
                validation_id   TEXT DEFAULT NULL,
                x_validated     INTEGER DEFAULT 0,
                x_validation_result INTEGER DEFAULT NULL,
                x_validated_at  INTEGER DEFAULT NULL,
                x_validation_error TEXT DEFAULT NULL,
                post_url        TEXT DEFAULT NULL,
                PRIMARY KEY (miner_hotkey, post_id)
            );
            """)
            conn.commit()
            
            # ---------------------------------------------------------------
            # Legacy migrations (backward compat for existing deployments).
            # These are no-ops on fresh installs since columns/constraints
            # are already defined in the CREATE TABLE above.
            # ---------------------------------------------------------------
            try:
                c.execute("ALTER TABLE submissions ADD COLUMN accepted_block INTEGER")
                conn.commit()
            except psycopg2.errors.DuplicateColumn:
                conn.rollback()
                pass
            
            # Create unique constraint on validation_id (required for foreign key reference)
            # PostgreSQL allows multiple NULLs in a UNIQUE column, which is what we want
            try:
                c.execute("ALTER TABLE submissions ADD CONSTRAINT submissions_validation_id_unique UNIQUE (validation_id)")
                conn.commit()
            except (psycopg2.errors.DuplicateObject, psycopg2.errors.ProgrammingError) as e:
                conn.rollback()
                # Check if error is about constraint/object already existing
                if 'already exists' in str(e) or 'duplicate' in str(e).lower():
                    pass  # Constraint already exists, continue
                else:
                    raise  # Re-raise if it's a different error
            
            # Validator assignments (tracks which submissions are assigned to which validators)
            c.execute("""
            CREATE TABLE IF NOT EXISTS validator_assignments (
                validation_id   TEXT PRIMARY KEY,
                validator_hotkey TEXT NOT NULL,
                assigned_at     INTEGER NOT NULL,
                completed_at    INTEGER DEFAULT NULL,
                FOREIGN KEY (validation_id) REFERENCES submissions(validation_id)
            );
            """)
            conn.commit()
            
            # Validation results (stores validator results)
            c.execute("""
            CREATE TABLE IF NOT EXISTS validation_results (
                validation_id   TEXT PRIMARY KEY,
                validator_hotkey TEXT NOT NULL,
                miner_hotkey     TEXT NOT NULL,
                post_id          TEXT NOT NULL,
                success          INTEGER NOT NULL,
                failure_reason   TEXT DEFAULT NULL,
                validated_at     INTEGER NOT NULL
            );
            """)
            conn.commit()
            
            # Add window_id column to submissions if it doesn't exist (for dashboard)
            try:
                c.execute("ALTER TABLE submissions ADD COLUMN window_id BIGINT DEFAULT NULL")
                conn.commit()
            except psycopg2.errors.DuplicateColumn:
                conn.rollback()
                pass  # Column already exists
            
            # Windows table (global metadata per block window - for dashboard)
            c.execute("""
            CREATE TABLE IF NOT EXISTS windows (
                id                   BIGSERIAL PRIMARY KEY,
                window_start_block   INTEGER UNIQUE NOT NULL,
                window_end_block     INTEGER NOT NULL,
                blocks_per_window    INTEGER NOT NULL,
                start_time           INTEGER,
                end_time             INTEGER,
                calculated_at        INTEGER NOT NULL,
                submissions_count    INTEGER NOT NULL DEFAULT 0,
                distinct_miners_count INTEGER NOT NULL DEFAULT 0,
                status               TEXT NOT NULL DEFAULT 'finalized'
            );
            """)
            conn.commit()
            
            # Miner window scores table (per-miner stats per window - for dashboard)
            c.execute("""
            CREATE TABLE IF NOT EXISTS miner_window_scores (
                id                    BIGSERIAL PRIMARY KEY,
                window_id             BIGINT NOT NULL REFERENCES windows(id) ON DELETE CASCADE,
                miner_hotkey          TEXT NOT NULL,
                submissions_count     INTEGER NOT NULL,
                raw_avg_score         REAL NOT NULL,
                final_score           REAL NOT NULL,
                had_validator_failure INTEGER NOT NULL DEFAULT 0,
                had_x_failure         INTEGER NOT NULL DEFAULT 0,
                UNIQUE(window_id, miner_hotkey)
            );
            """)
            conn.commit()
            
            # Create indexes for performance
            c.execute("CREATE INDEX IF NOT EXISTS idx_submissions_hotkey ON submissions(miner_hotkey)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_submissions_selected ON submissions(selected_for_validation)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_submissions_validation_id ON submissions(validation_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_submissions_x_validated ON submissions(x_validated, x_validation_result)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_submissions_accepted_at ON submissions(accepted_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_submissions_accepted_block ON submissions(accepted_block)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_submissions_window_id ON submissions(window_id, miner_hotkey)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_validation_results_miner ON validation_results(miner_hotkey)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_validation_results_validator ON validation_results(validator_hotkey)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_validation_results_validator_id ON validation_results(validator_hotkey, validation_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_validator_assignments_validator ON validator_assignments(validator_hotkey)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_validator_assignments_completed ON validator_assignments(completed_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_miner_window_scores_hotkey ON miner_window_scores(miner_hotkey, window_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_miner_window_scores_window ON miner_window_scores(window_id, final_score DESC)")
            conn.commit()
    finally:
        if conn:
            close_connection(conn)


def _get_block_window_start(block: int) -> int:
    """Get the start block of the window containing the given block."""
    return (block // BLOCKS_PER_WINDOW) * BLOCKS_PER_WINDOW


def _get_block_window_end(block: int) -> int:
    """Get the end block of the window containing the given block."""
    window_start = _get_block_window_start(block)
    return window_start + BLOCKS_PER_WINDOW - 1


# Public API wrappers for private functions
def get_block_window_start(block: int) -> int:
    """Get the start block of the window containing the given block."""
    return _get_block_window_start(block)


def get_block_window_end(block: int) -> int:
    """Get the end block of the window containing the given block."""
    return _get_block_window_end(block)


# ============================================================================
# Scores State Management
# ============================================================================
# Persists scores to a JSON file for each completed window.
# This ensures:
# 1. Scores are calculated once when a window completes
# 2. Failed miners get 0 and this is immutable for that window
# 3. Fast reads - no DB query on every /v2/scores request
# ============================================================================

def _load_scores_state() -> Optional[Dict]:
    """Load scores state from file. Returns None if file doesn't exist or is invalid."""
    try:
        if os.path.exists(_SCORES_STATE_FILE):
            with open(_SCORES_STATE_FILE, 'r') as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[DB] Warning: Failed to load scores state: {e}")
    return None


def _save_scores_state(state: Dict) -> bool:
    """Save scores state to file. Returns True on success."""
    try:
        with open(_SCORES_STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
        return True
    except IOError as e:
        print(f"[DB] Error: Failed to save scores state: {e}")
        return False


def _calculate_window_scores_detailed(window_start: int, window_end: int) -> Dict:
    """
    Calculate detailed scores for a specific block window.
    
    Returns a dict with:
    - "per_miner": {hotkey: {submissions_count, raw_avg_score, final_score, had_validator_failure, had_x_failure}}
    - "global": {submissions_count, distinct_miners_count, start_time, end_time}
    - "scores": {hotkey: final_score} (for backward compatibility)
    
    Failed miners (any X or validator validation failure) get final_score = 0.0.
    """
    conn = None
    try:
        with _db_lock:
            conn = connect()
            c = get_cursor(conn)
            
            # Get average scores and counts for all hotkeys in the window
            c.execute("""
                SELECT miner_hotkey, AVG(score) as avg_score, COUNT(*) as count
                FROM submissions
                WHERE accepted_block >= %s AND accepted_block <= %s
                GROUP BY miner_hotkey
            """, (window_start, window_end))
            
            per_miner = {}
            for row in c.fetchall():
                if row["count"] > 0 and row["avg_score"] is not None:
                    per_miner[row["miner_hotkey"]] = {
                        "submissions_count": row["count"],
                        "raw_avg_score": float(row["avg_score"]),
                        "final_score": float(row["avg_score"]),
                        "had_validator_failure": False,
                        "had_x_failure": False,
                    }
            
            # Find failed hotkeys - validator validation failures
            c.execute("""
                SELECT DISTINCT vr.miner_hotkey
                FROM validation_results vr
                INNER JOIN submissions s ON vr.validation_id = s.validation_id
                WHERE s.accepted_block >= %s 
                  AND s.accepted_block <= %s
                  AND vr.success = 0
            """, (window_start, window_end))
            validator_failed_hotkeys = {row["miner_hotkey"] for row in c.fetchall()}
            
            # Find failed hotkeys - X validation failures
            c.execute("""
                SELECT DISTINCT miner_hotkey
                FROM submissions
                WHERE accepted_block >= %s 
                  AND accepted_block <= %s
                  AND x_validated = 1
                  AND x_validation_result = 0
            """, (window_start, window_end))
            x_failed_hotkeys = {row["miner_hotkey"] for row in c.fetchall()}
            
            # Mark failures and zero out scores
            for hotkey in validator_failed_hotkeys:
                if hotkey in per_miner:
                    per_miner[hotkey]["had_validator_failure"] = True
                    per_miner[hotkey]["final_score"] = 0.0
                    print(f"[DB] Hotkey {hotkey} has failed validator validation in window {window_start}-{window_end}, score = 0.0")
            
            for hotkey in x_failed_hotkeys:
                if hotkey in per_miner:
                    per_miner[hotkey]["had_x_failure"] = True
                    per_miner[hotkey]["final_score"] = 0.0
                    print(f"[DB] Hotkey {hotkey} has failed X validation in window {window_start}-{window_end}, score = 0.0")
            
            # Get global stats
            c.execute("""
                SELECT COUNT(*) as total_count,
                       COUNT(DISTINCT miner_hotkey) as distinct_miners,
                       MIN(accepted_at) as start_time,
                       MAX(accepted_at) as end_time
                FROM submissions
                WHERE accepted_block >= %s AND accepted_block <= %s
            """, (window_start, window_end))
            global_row = c.fetchone()
            
            global_stats = {
                "submissions_count": global_row["total_count"] if global_row else 0,
                "distinct_miners_count": global_row["distinct_miners"] if global_row else 0,
                "start_time": global_row["start_time"] if global_row else None,
                "end_time": global_row["end_time"] if global_row else None,
            }
            
            # Build backward-compatible scores dict
            scores = {hotkey: data["final_score"] for hotkey, data in per_miner.items()}
            
            return {
                "per_miner": per_miner,
                "global": global_stats,
                "scores": scores,
            }
    finally:
        if conn:
            close_connection(conn)


# Public API wrapper
def calculate_window_scores_detailed(window_start: int, window_end: int) -> Dict:
    """Calculate detailed scores for a specific block window."""
    return _calculate_window_scores_detailed(window_start, window_end)


def _calculate_window_scores(window_start: int, window_end: int) -> Dict[str, float]:
    """
    Calculate scores for a specific block window.
    
    Returns a dict mapping hotkey -> score.
    Failed miners (any X or LLM validation failure) get 0.0.
    
    This is a backward-compatible wrapper around _calculate_window_scores_detailed.
    """
    result = _calculate_window_scores_detailed(window_start, window_end)
    return result["scores"]


def get_rate_limit_info(miner_hotkey: str) -> Dict:
    """
    Get detailed rate limit information for a miner.
    
    Returns a dictionary with:
    - current_count: Number of submissions in current window
    - max_submissions: Maximum allowed submissions per window
    - current_block: Current block number
    - window_start_block: Start block of current window
    - window_end_block: End block of current window
    - next_window_start_block: Start block of next window (when limit resets)
    - blocks_until_reset: Number of blocks until reset
    - estimated_seconds_until_reset: Estimated seconds until reset (based on SECONDS_PER_BLOCK config, default 12s per block)
    - current_window: Current window number (current_block // BLOCKS_PER_WINDOW)
    """
    current_block = get_current_block()
    window_start = _get_block_window_start(current_block)
    window_end = _get_block_window_end(current_block)
    next_window_start = window_start + BLOCKS_PER_WINDOW
    
    conn = None
    try:
        # Read-only operation - no lock needed, PostgreSQL handles concurrency
        conn = connect()
        c = get_cursor(conn)
        c.execute("""
            SELECT COUNT(*) as count
            FROM submissions
            WHERE miner_hotkey = %s AND accepted_block >= %s
        """, (miner_hotkey, window_start))
        count_row = c.fetchone()
        current_count = count_row["count"] if count_row else 0
    finally:
        if conn:
            close_connection(conn)
    
    blocks_until_reset = next_window_start - current_block
    # Calculate estimated time until reset based on blocks remaining and average block time
    # Bittensor blocks are approximately 12 seconds apart, but this can vary slightly
    estimated_seconds_until_reset = blocks_until_reset * SECONDS_PER_BLOCK
    
    return {
        "current_count": current_count,
        "max_submissions": MAX_SUBMISSION_RATE,
        "current_block": current_block,
        "window_start_block": window_start,
        "window_end_block": window_end,
        "next_window_start_block": next_window_start,
        "blocks_until_reset": blocks_until_reset,
        "estimated_seconds_until_reset": estimated_seconds_until_reset,
        "blocks_per_window": BLOCKS_PER_WINDOW,
        "current_window": current_block // BLOCKS_PER_WINDOW,
    }


def insert_submission(post, now: int):
    """
    Insert a new submission. Duplicates are ignored (same post_id from same hotkey).
    Returns tuple (is_new: bool, message: str, error_code: Optional[str], rate_limit_info: Optional[Dict]).
    error_code can be:
    - "limit_exceeded" if the miner has exceeded submission rate limit (rate_limit_info will be populated)
    - "post_already_submitted" if the post_id was already submitted by this miner
    
    Args:
        post: Post submission object
        now: Current timestamp
    """
    # Get current block number (with error handling and fallback)
    current_block = get_current_block()
    
    conn = None
    try:
        with _db_lock:
            conn = connect()
            c = get_cursor(conn)
            try:
                # Check if this is a duplicate first
                c.execute("""
                    SELECT COUNT(*) as count
                    FROM submissions
                    WHERE miner_hotkey = %s AND post_id = %s
                """, (post.miner_hotkey, post.post_id))
                duplicate_row = c.fetchone()
                if duplicate_row and duplicate_row["count"] > 0:
                    # Duplicate post_id - ignore it completely
                    return False, f"[API] DUP  hotkey={post.miner_hotkey} post_id={post.post_id} (ignored)", None, None
                
                # Check submission rate limit (block-based, global window for all hotkeys)
                window_start = _get_block_window_start(current_block)
                c.execute("""
                    SELECT COUNT(*) as count
                    FROM submissions
                    WHERE miner_hotkey = %s AND accepted_block >= %s
                """, (post.miner_hotkey, window_start))
                count_row = c.fetchone()
                count = count_row["count"] if count_row else 0
                if count >= MAX_SUBMISSION_RATE:
                    # Build rate limit info directly (avoid deadlock by not calling get_rate_limit_info which also needs _db_lock)
                    window_end = _get_block_window_end(current_block)
                    next_window_start = window_start + BLOCKS_PER_WINDOW
                    blocks_until_reset = next_window_start - current_block
                    estimated_seconds_until_reset = blocks_until_reset * SECONDS_PER_BLOCK
                    rate_limit_info = {
                        "current_count": count,
                        "max_submissions": MAX_SUBMISSION_RATE,
                        "current_block": current_block,
                        "window_start_block": window_start,
                        "window_end_block": window_end,
                        "next_window_start_block": next_window_start,
                        "blocks_until_reset": blocks_until_reset,
                        "estimated_seconds_until_reset": estimated_seconds_until_reset,
                        "blocks_per_window": BLOCKS_PER_WINDOW,
                        "current_window": current_block // BLOCKS_PER_WINDOW,
                    }
                    return False, f"[API] LIMIT hotkey={post.miner_hotkey} post_id={post.post_id} (rate limit exceeded: {count}/{MAX_SUBMISSION_RATE} per {BLOCKS_PER_WINDOW} blocks)", "limit_exceeded", rate_limit_info
                
                # Generate X post URL from author and post_id
                post_url = generate_post_url(post.author, post.post_id)
                
                # Use canonical tokens JSON (sorted keys) for storage consistency
                tokens_json_storage = json.dumps(post.tokens, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                
                c.execute("""
                    INSERT INTO submissions (
                        miner_hotkey, post_id, content, date, author,
                        account_age, retweets, likes, responses, followers, tokens_json, sentiment, score, accepted_at, accepted_block, post_url
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (
                    post.miner_hotkey, post.post_id, post.content, post.date, post.author,
                    post.account_age, post.retweets, post.likes, post.responses, post.followers,
                    tokens_json_storage,
                    post.sentiment, post.score, now, current_block, post_url
                ))
                conn.commit()
                return True, f"[API] NEW  hotkey={post.miner_hotkey} post_id={post.post_id}", None, None
            except psycopg2.errors.UniqueViolation:
                # Duplicate post_id - ignore it completely
                conn.rollback()
                return False, f"[API] DUP  hotkey={post.miner_hotkey} post_id={post.post_id} (ignored)", None, None
            except Exception as e:
                # Catch any other database errors to prevent lock from being held indefinitely
                conn.rollback()
                print(f"[DB] Error in insert_submission: {e}", file=sys.stderr)
                raise  # Re-raise to let caller handle it
    finally:
        if conn:
            close_connection(conn)


def _validate_post_with_backend(post_dict: Dict) -> Tuple[bool, Optional[Dict]]:
    """
    Validate a post using the configured backend.
    
    Args:
        post_dict: Post dictionary with post_id, content, author, date, likes, retweets, responses, followers
    
    Returns:
        Tuple of (is_valid: bool, error_dict: Optional[Dict])
    """
    validator = _get_validator_instance()
    return validator.validate_post(post_dict)


def select_post_for_validation(miner_hotkey: str, post_id: str, post_data: Dict, now: int, validation_probability: float = 0.2) -> Tuple[bool, Optional[str], Optional[Dict]]:
    """
    Randomly select a post for validation based on probability.
    If selected, performs validation using the configured backend (TwitterAPI.io or X API),
    creates a validation_id and marks the post as selected.
    
    Args:
        miner_hotkey: The miner's hotkey
        post_id: The post ID
        post_data: Post dictionary with all post fields (for validation)
        now: Current timestamp
        validation_probability: Probability of selection (default 0.2)
    
    Returns:
        Tuple of (was_selected: bool, validation_id: Optional[str], x_validation_error: Optional[Dict])
        - If selected and validation passed: (True, validation_id, None)
        - If selected but validation failed: (True, None, error_dict)
        - If not selected: (False, None, None)
    """
    import random
    
    # Check if this post was already selected (read-only, no lock needed)
    conn = None
    try:
        conn = connect()
        c = get_cursor(conn)
        c.execute("""
            SELECT selected_for_validation, validation_id, x_validated, x_validation_result
            FROM submissions
            WHERE miner_hotkey = %s AND post_id = %s
        """, (miner_hotkey, post_id))
        row = c.fetchone()
        if row and row["selected_for_validation"] == 1:
            # Already selected
            if row["x_validated"] == 1 and row["x_validation_result"] == 1:
                # Validation passed, return existing validation_id
                return True, row["validation_id"], None
            else:
                # Validation failed or not done yet
                return True, None, None
    finally:
        if conn:
            close_connection(conn)
    
    # Random selection based on probability
    if random.random() >= validation_probability:
        return False, None, None
    
    # Selected! Now perform validation using configured backend
    backend_name = "X API" if VALIDATION_BACKEND == "x" else "TwitterAPI.io"
    print(f"[DB] Post selected for validation, performing {backend_name} validation...")
    
    # Prepare post dict for validation
    post_dict = {
        "post_id": post_id,
        "content": post_data.get("content"),
        "author": post_data.get("author"),
        "date": post_data.get("date"),
        "likes": post_data.get("likes"),
        "retweets": post_data.get("retweets"),
        "responses": post_data.get("responses"),
        "followers": post_data.get("followers"),
    }
    
    # Perform validation using configured backend (outside lock to avoid blocking)
    is_valid, error_dict = _validate_post_with_backend(post_dict)
    
    # Store validation result - re-check state after validation to prevent race conditions
    validation_id = None
    x_validation_error_json = None
    
    conn = None
    try:
        with _db_lock:
            conn = connect()
            c = get_cursor(conn)
            
            # Re-check if post was already processed by another thread
            c.execute("""
                SELECT selected_for_validation, validation_id, x_validated, x_validation_result
                FROM submissions
                WHERE miner_hotkey = %s AND post_id = %s
            """, (miner_hotkey, post_id))
            check_row = c.fetchone()
            
            if check_row and check_row["selected_for_validation"] == 1:
                if check_row["x_validated"] == 1 and check_row["x_validation_result"] == 1:
                    # Already processed by another thread - return existing validation_id
                    print(f"[DB] Post already processed by another thread, returning existing validation_id")
                    return True, check_row["validation_id"], None
                elif check_row["x_validated"] == 1 and check_row["x_validation_result"] == 0:
                    # Already failed validation in another thread
                    print(f"[DB] Post already failed {backend_name} validation in another thread")
                    return True, None, None
            
            if is_valid:
                print(f"[DB] ✓ {backend_name} validation PASSED for {miner_hotkey} post_id={post_id}")
                # Validation passed - create validation_id and proceed
                validation_id = str(uuid.uuid4())
                
                # Mark post as X validated (passed)
                c.execute("""
                    UPDATE submissions
                    SET x_validated = 1, x_validation_result = 1, x_validated_at = %s, x_validation_error = NULL
                    WHERE miner_hotkey = %s AND post_id = %s
                """, (now, miner_hotkey, post_id))
                conn.commit()
            else:
                print(f"[DB] ✗ {backend_name} validation FAILED for {miner_hotkey} post_id={post_id}: {error_dict.get('code', 'unknown')} - {error_dict.get('message', 'N/A')}")
                # Validation failed - mark as failed but don't create validation_id
                x_validation_error_json = json.dumps(error_dict, separators=(",", ":"), ensure_ascii=False) if error_dict else None
                
                c.execute("""
                    UPDATE submissions
                    SET x_validated = 1, x_validation_result = 0, x_validated_at = %s, x_validation_error = %s
                    WHERE miner_hotkey = %s AND post_id = %s
                """, (now, x_validation_error_json, miner_hotkey, post_id))
                
                conn.commit()
                return True, None, error_dict
    finally:
        if conn:
            close_connection(conn)
    
    # Only proceed if validation passed (validation_id is already set above)
    # Now mark post as selected
    conn = None
    try:
        with _db_lock:
            conn = connect()
            c = get_cursor(conn)
            
            # Re-check one more time to ensure we're still the one processing this
            c.execute("""
                SELECT accepted_at, selected_for_validation, validation_id
                FROM submissions
                WHERE miner_hotkey = %s AND post_id = %s
            """, (miner_hotkey, post_id))
            post_row = c.fetchone()
            if not post_row:
                return False, None, None
            
            # If already selected by another thread, return existing validation_id
            if post_row["selected_for_validation"] == 1 and post_row["validation_id"]:
                return True, post_row["validation_id"], None
            
            # Mark post as selected (atomic update)
            c.execute("""
                UPDATE submissions
                SET selected_for_validation = 1, validation_id = %s
                WHERE miner_hotkey = %s AND post_id = %s AND selected_for_validation = 0
            """, (validation_id, miner_hotkey, post_id))
            
            # Check if update actually affected a row (prevent race condition)
            if c.rowcount == 0:
                # Another thread already selected this post
                c.execute("""
                    SELECT validation_id
                    FROM submissions
                    WHERE miner_hotkey = %s AND post_id = %s
                """, (miner_hotkey, post_id))
                existing_row = c.fetchone()
                if existing_row and existing_row["validation_id"]:
                    return True, existing_row["validation_id"], None
                return False, None, None
            
            conn.commit()
    finally:
        if conn:
            close_connection(conn)
    
    return True, validation_id, None


def _persist_window_scores(window_start: int, window_end: int, detailed_result: Dict, current_block: int) -> Optional[int]:
    """
    Persist window scores to the windows and miner_window_scores tables.
    Also updates submissions.window_id for all submissions in this window.
    
    Args:
        window_start: Start block of the window
        window_end: End block of the window
        detailed_result: Result from _calculate_window_scores_detailed
        current_block: Current block number
    
    Returns:
        window_id if successful, None otherwise
    """
    now = int(time.time())
    per_miner = detailed_result["per_miner"]
    global_stats = detailed_result["global"]
    
    conn = None
    try:
        with _db_lock:
            conn = connect()
            c = get_cursor(conn)
            
            # Upsert into windows table
            c.execute("""
                INSERT INTO windows (
                    window_start_block, window_end_block, blocks_per_window,
                    start_time, end_time, calculated_at,
                    submissions_count, distinct_miners_count, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (window_start_block) DO UPDATE SET
                    end_time = EXCLUDED.end_time,
                    calculated_at = EXCLUDED.calculated_at,
                    submissions_count = EXCLUDED.submissions_count,
                    distinct_miners_count = EXCLUDED.distinct_miners_count
                RETURNING id
            """, (
                window_start, window_end, BLOCKS_PER_WINDOW,
                global_stats["start_time"], global_stats["end_time"], now,
                global_stats["submissions_count"], global_stats["distinct_miners_count"], "finalized"
            ))
            window_row = c.fetchone()
            window_id = window_row["id"]
            
            # Upsert miner_window_scores for each miner
            for hotkey, data in per_miner.items():
                c.execute("""
                    INSERT INTO miner_window_scores (
                        window_id, miner_hotkey, submissions_count,
                        raw_avg_score, final_score,
                        had_validator_failure, had_x_failure
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (window_id, miner_hotkey) DO UPDATE SET
                        submissions_count = EXCLUDED.submissions_count,
                        raw_avg_score = EXCLUDED.raw_avg_score,
                        final_score = EXCLUDED.final_score,
                        had_validator_failure = EXCLUDED.had_validator_failure,
                        had_x_failure = EXCLUDED.had_x_failure
                """, (
                    window_id, hotkey, data["submissions_count"],
                    data["raw_avg_score"], data["final_score"],
                    1 if data["had_validator_failure"] else 0,
                    1 if data["had_x_failure"] else 0
                ))
            
            # Update submissions.window_id for all submissions in this window
            c.execute("""
                UPDATE submissions
                SET window_id = %s
                WHERE accepted_block >= %s AND accepted_block <= %s
                  AND window_id IS NULL
            """, (window_id, window_start, window_end))
            
            conn.commit()
            print(f"[DB] Persisted window {window_start}-{window_end} (id={window_id}) with {len(per_miner)} miners")
            return window_id
    except Exception as e:
        print(f"[DB] Error persisting window scores: {e}", file=sys.stderr)
        if conn:
            conn.rollback()
        return None
    finally:
        if conn:
            close_connection(conn)


# Public API wrapper
def persist_window_scores(window_start: int, window_end: int, detailed_result: Dict, current_block: int) -> Optional[int]:
    """Persist window scores to the windows and miner_window_scores tables."""
    return _persist_window_scores(window_start, window_end, detailed_result, current_block)


def get_all_hotkey_scores_last_block_window() -> Tuple[Dict[str, float], int]:
    """
    Get average scores for all hotkeys in the previous completed block window.
    
    Uses a state file to persist scores. Once a window completes and scores are
    calculated, they are immutable for that window. This ensures:
    1. Failed miners get 0 and cannot recover within the same window
    2. Consistent scores across all validator requests
    3. Fast reads - no DB query on repeated requests for same window
    
    Also persists scores to the windows and miner_window_scores tables for dashboard use.
    
    Returns:
        Tuple of (scores_dict, previous_window_end_block)
    """
    current_block = get_current_block()
    current_window_start = _get_block_window_start(current_block)
    
    # Calculate previous window boundaries
    previous_window_start = current_window_start - BLOCKS_PER_WINDOW
    previous_window_end = current_window_start - 1
    
    # If we're still in the first window, return empty scores
    if previous_window_start < 0:
        return {}, current_block
    
    # Check state file for cached scores
    state = _load_scores_state()
    
    if state:
        cached_window_start = state.get("window_start")
        cached_window_end = state.get("window_end")
        
        # If cached scores are for the previous window, use them
        if cached_window_start == previous_window_start and cached_window_end == previous_window_end:
            scores = state.get("scores", {})
            print(f"[DB] Using cached scores for window {previous_window_start}-{previous_window_end} ({len(scores)} hotkeys)")
            return scores, previous_window_end
    
    # Calculate detailed scores for the previous window
    print(f"[DB] Calculating scores for window {previous_window_start}-{previous_window_end}")
    detailed_result = _calculate_window_scores_detailed(previous_window_start, previous_window_end)
    scores = detailed_result["scores"]
    
    # Persist to windows and miner_window_scores tables (for dashboard)
    _persist_window_scores(previous_window_start, previous_window_end, detailed_result, current_block)
    
    # Save to state file (for backward compatibility and fast reads)
    new_state = {
        "window_start": previous_window_start,
        "window_end": previous_window_end,
        "blocks_per_window": BLOCKS_PER_WINDOW,
        "calculated_at": int(time.time()),
        "calculated_at_block": current_block,
        "scores": scores,
    }
    
    if _save_scores_state(new_state):
        print(f"[DB] Saved scores for window {previous_window_start}-{previous_window_end} ({len(scores)} hotkeys)")
    
    return scores, previous_window_end


def get_pending_validations(validator_hotkey: str, now: int) -> List[Dict]:
    """
    Get and assign pending validation payloads for a specific validator.
    
    Assigns the next N unassigned submissions (that passed X validation) to this validator.
    Returns up to VALIDATIONS_PER_REQUEST submissions.
    
    Args:
        validator_hotkey: The validator's hotkey
        now: Current timestamp
    
    Returns:
        List of dicts, each with validation_id, miner_hotkey, post data, selected_at
        Returns empty list if no pending validations available
    """
    results = []
    conn = None
    try:
        # SELECT FOR UPDATE SKIP LOCKED handles concurrency at database level, no Python lock needed
        conn = connect()
        c = get_cursor(conn)
        
        # Find and lock unassigned submissions atomically
        # Use SELECT FOR UPDATE SKIP LOCKED to prevent race conditions
        # This locks rows as we select them, preventing other validators from selecting the same rows
        # Note: FOR UPDATE OF s only locks the submissions table, not the joined validator_assignments table
        c.execute("""
            SELECT s.miner_hotkey, s.post_id, s.content, s.date, s.author,
                   s.account_age, s.retweets, s.likes, s.responses, s.followers,
                   s.tokens_json, s.sentiment, s.score, s.accepted_at, s.validation_id,
                   s.post_url
            FROM submissions s
            LEFT JOIN validator_assignments va ON s.validation_id = va.validation_id
            WHERE s.selected_for_validation = 1 
              AND s.x_validated = 1
              AND s.x_validation_result = 1
              AND s.validation_id IS NOT NULL
              AND va.validation_id IS NULL
            ORDER BY s.accepted_at ASC
            LIMIT %s
            FOR UPDATE OF s SKIP LOCKED
        """, (VALIDATIONS_PER_REQUEST,))
        
        rows = c.fetchall()
        
        if not rows:
            return []
        
        # Assign these submissions to this validator
        # We use ON CONFLICT DO NOTHING + RETURNING to avoid UniqueViolation when multiple
        # validators (or processes) race to assign the same validation_id.
        # We then only return payloads for validation_ids actually inserted in this transaction.
        inserted_ids = set()
        for row in rows:
            validation_id = row["validation_id"]
            c.execute("""
                INSERT INTO validator_assignments (validation_id, validator_hotkey, assigned_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (validation_id) DO NOTHING
                RETURNING validation_id
            """, (validation_id, validator_hotkey, now))
            result = c.fetchone()
            if result:
                inserted_ids.add(result["validation_id"])
        
        conn.commit()
        
        # Only return payloads for validation_ids this validator actually claimed
        filtered_rows = [row for row in rows if row["validation_id"] in inserted_ids]
        if not filtered_rows:
            return []
        
        # Build results
        for row in filtered_rows:
            post = {
                "miner_hotkey": row["miner_hotkey"],
                "post_id": row["post_id"],
                "content": row["content"],
                "date": row["date"],
                "author": row["author"],
                "account_age": row["account_age"],
                "retweets": row["retweets"],
                "likes": row["likes"],
                "responses": row["responses"],
                "followers": row["followers"],
                "tokens": json.loads(row["tokens_json"]),
                "sentiment": row["sentiment"],
                "score": row["score"],
                "post_url": row["post_url"]
            }
            
            results.append({
                "validation_id": row["validation_id"],
                "miner_hotkey": row["miner_hotkey"],
                "post": post,
                "selected_at": row["accepted_at"]
            })
        
        print(f"[DB] Assigned {len(filtered_rows)} submission(s) to validator {validator_hotkey}")
    finally:
        if conn:
            close_connection(conn)
    
    return results


def record_validation_result(
    validator_hotkey: str,
    validation_id: str,
    miner_hotkey: str,
    success: bool,
    failure_reason: Optional[Dict] = None,
    now: int = None
) -> bool:
    """
    Record a validation result from a validator.
    
    Args:
        validator_hotkey: The validator's hotkey
        validation_id: The validation_id from the ValidationPayload
        miner_hotkey: The miner's hotkey
        success: True if validation passed, False if failed
        failure_reason: Optional failure reason dict (JSON-encoded)
        now: Unix timestamp (defaults to current time)
    
    Returns:
        True if successful, False if failed
    """
    if now is None:
        now = int(time.time())
    
    conn = None
    try:
        with _db_lock:
            conn = connect()
            c = get_cursor(conn)
            try:
                # Get post_id from validation_id
                c.execute("""
                    SELECT post_id
                    FROM submissions
                    WHERE validation_id = %s AND miner_hotkey = %s
                """, (validation_id, miner_hotkey))
                row = c.fetchone()
                if not row:
                    print(f"[DB] Error: Could not find post for validation_id={validation_id}, miner_hotkey={miner_hotkey}", file=sys.stderr)
                    return False
                post_id = row["post_id"]
                
                failure_reason_json = json.dumps(failure_reason, separators=(",", ":"), ensure_ascii=False) if failure_reason else None
                
                # Verify this validation_id was assigned to this validator
                c.execute("""
                    SELECT validation_id
                    FROM validator_assignments
                    WHERE validation_id = %s AND validator_hotkey = %s
                """, (validation_id, validator_hotkey))
                assignment = c.fetchone()
                if not assignment:
                    print(f"[DB] Error: validation_id={validation_id} not assigned to validator {validator_hotkey}", file=sys.stderr)
                    return False
                
                # Check if validation_id already has a result
                c.execute("""
                    SELECT validation_id
                    FROM validation_results
                    WHERE validation_id = %s
                """, (validation_id,))
                existing = c.fetchone()
                if existing:
                    # Update existing result
                    c.execute("""
                        UPDATE validation_results
                        SET validator_hotkey = %s, success = %s, failure_reason = %s,
                            validated_at = %s
                        WHERE validation_id = %s
                    """, (validator_hotkey, 1 if success else 0, failure_reason_json, now, validation_id))
                else:
                    # Insert new result
                    c.execute("""
                        INSERT INTO validation_results (
                            validation_id, validator_hotkey, miner_hotkey, post_id,
                            success, failure_reason, validated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (validation_id, validator_hotkey, miner_hotkey, post_id,
                          1 if success else 0, failure_reason_json, now))
                
                # Mark assignment as completed
                c.execute("""
                    UPDATE validator_assignments
                    SET completed_at = %s
                    WHERE validation_id = %s AND validator_hotkey = %s
                """, (now, validation_id, validator_hotkey))
                
                conn.commit()
                return True
            except Exception as e:
                print(f"[DB] Error recording validation result: {e}", file=sys.stderr)
                conn.rollback()
                return False
    finally:
        if conn:
            close_connection(conn)

