# API v2 - Probabilistic Validation System

## Overview

API v2 implements a probabilistic validation system where:
- Miners submit posts just like in v1, with adjustable submission rate limits (block-based windows)
- When a miner submits a post, there's a configurable chance (default 20%) of that submission being chosen for validation
- If chosen, the submission is packaged in a payload that validators can GET
- The payload contains:
  - The post to validate
  - Note: Average scores are calculated server-side when validation results are submitted, not included in the payload
- If validation succeeds, the average score for the current block window is set as the reward
- If validation fails, a 0 score is set
- Validators then POST the validation result back to the API

## Key Differences from v1

### v1 (Batch-based)
- Batches are created periodically (every BATCH_INTERVAL_SECONDS)
- All posts are sampled and grouped into batches
- Validators GET entire batches and vote on miners (VALID/INVALID per hotkey)
- Scores are calculated from average scores of posts in the batch

### v2 (Probabilistic)
- Posts are selected for validation individually on submission (probabilistic)
- Validators GET individual validation payloads (one post at a time)
- Validators POST individual validation results (success/failure)
- Reward is the average score for the current block window (if success) or 0 (if failure)
- Average score is calculated dynamically when validation results are submitted

## Validation Backends

By default, the API validates posts using **TwitterAPI.io**, with optional support for the official **X API**. Collectively, these are referred to as the **external tweet validation backend**:

- **VALIDATION_BACKEND** (env var):
  - `"twitterapi"` (default): use `TwitterAPI.io` via `TWITTER_API_IO_KEY`.
  - `"x"`: use the X API via `tweepy` and `X_BEARER_TOKEN`.
  - Any other value falls back to `"twitterapi"`.
- Both backends share the same normalization and metric‑inflation rules, and both write results into the same database fields:
  - `submissions.x_validated`
  - `submissions.x_validation_result`
  - `submissions.x_validation_error`

You can switch backends by changing `VALIDATION_BACKEND` and setting the appropriate API credentials in `.env`.

## Endpoints

### GET `/v2/status`
Get current API status including block number and window information.

**Useful for miners** to synchronize their block tracking with the API. No authentication required (public endpoint).

**Response:**
```json
{
  "status": "ok",
  "current_block": 12345,
  "window_start_block": 12300,
  "window_end_block": 12399,
  "next_window_start_block": 12400,
  "blocks_per_window": 100,
  "blocks_until_next_window": 55,
  "current_window": 123
}
```

### POST `/v2/submit`
Miners submit posts for validation.

**Request:**
```json
{
  "miner_hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
  "post_id": "1234567890",
  "content": "Post content...",
  "date": 1234567890,
  "author": "username",
  "account_age": 365,
  "retweets": 10,
  "likes": 50,
  "responses": 5,
  "followers": 1000,
  "tokens": {"subnet_name": 0.85},
  "sentiment": 0.5,
  "score": 0.75
}
```

**Response (if selected and external tweet validation passed):**
```json
{
  "status": "new",
  "message": "Submission accepted and selected for validation",
  "selected_for_validation": true,
  "x_validation_passed": true,
  "validation_id": "uuid-here",
  "current_block": 12345,
  "window_start_block": 12300,
  "window_end_block": 12399,
  "next_window_start_block": 12400,
  "blocks_per_window": 100,
  "current_window": 123,
  "rate_limit": {
    "current_count": 3,
    "max_submissions": 5,
    "remaining": 2
  }
}
```

**Response (if selected but external tweet validation failed):**
```json
{
  "status": "new",
  "message": "Submission accepted but external tweet validation failed",
  "selected_for_validation": true,
  "x_validation_passed": false,
  "x_validation_error": {
    "code": "post_not_found",
    "message": "Post not found or inaccessible",
    "post_id": "1234567890",
    "details": {}
  },
  "current_block": 12345,
  "window_start_block": 12300,
  "window_end_block": 12399,
  "next_window_start_block": 12400,
  "blocks_per_window": 100,
  "current_window": 123,
  "rate_limit": {
    "current_count": 3,
    "max_submissions": 5,
    "remaining": 2
  }
}
```

**Rate Limits:**
- Maximum `MAX_SUBMISSION_RATE` submissions per `BLOCKS_PER_WINDOW` blocks per miner (default: 5 per 100 blocks ≈ 20 minutes)
- Configurable via `MAX_SUBMISSION_RATE` environment variable (range: 1-5 recommended)
- Block-based windows ensure consistent rate limiting across the network

**Rate Limit Response (429 Too Many Requests):**
When the rate limit is exceeded, the response includes structured details:
```json
{
  "detail": {
    "error": "rate_limit_exceeded",
    "message": "Submission rate limit exceeded. You have used 5/5 submissions...",
    "rate_limit": {
      "current_count": 5,
      "max_submissions": 5,
      "remaining": 0,
      "current_block": 12345,
      "window_start_block": 12300,
      "window_end_block": 12399,
      "next_window_start_block": 12400,
      "blocks_until_reset": 55,
      "estimated_seconds_until_reset": 660,
      "blocks_per_window": 100,
      "current_window": 123
    }
  }
}
```
HTTP headers are also set: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset-Block`, `X-RateLimit-Reset-Seconds`.

**Note:** Rate‑limit and block/window metadata is included in *all* `/v2/submit` responses (success, duplicate, and error) so miners can stay synchronized.

**Validation Selection:**
- Each submission has a `VALIDATION_PROBABILITY` chance (default: 20%) of being selected
- Configurable via `VALIDATION_PROBABILITY` environment variable (0.0 to 1.0)

### GET `/v2/validation`
Validators retrieve pending validation payloads.

**Returns:** Up to `VALIDATIONS_PER_REQUEST` unassigned validation payloads (default: 5)

**Assignment Mechanism:**
- Returns the oldest unassigned submissions that passed external tweet validation
- Submissions are assigned atomically using `SELECT FOR UPDATE SKIP LOCKED` to prevent race conditions
- Results are sorted by `accepted_at` (oldest first) to maintain order
- Each validator gets unique assignments (no duplicate assignments)

**Response (if available):**
```json
{
  "available": true,
  "payloads": [
    {
      "validation_id": "uuid-here",
      "miner_hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
      "post": {
        "miner_hotkey": "...",
        "post_id": "...",
        "content": "...",
        "date": 1234567890,
        "author": "username",
        "account_age": 365,
        "retweets": 10,
        "likes": 50,
        "responses": 5,
        "followers": 1000,
        "tokens": {"subnet_name": 0.85},
        "sentiment": 0.5,
        "score": 0.75,
        "post_url": "https://x.com/username/status/1234567890"
      },
      "selected_at": 1234567890
    }
  ],
  "count": 1
}
```

**Note:** The validation payload does not include `average_score`. Average scores are calculated server-side when validation results are submitted. If validators need to see average scores, they can use the `/v2/scores` endpoint.

**Response (if none available):**
```json
{
  "available": false,
  "payloads": [],
  "count": 0
}
```

### GET `/v2/scores`
Validators can retrieve average scores for all hotkeys in the **previous completed block window**.

**Returns:** A dictionary mapping miner_hotkey -> average_score for all hotkeys that have submissions in the **previous completed block window**.

**Response:**
```json
{
  "scores": {
    "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY": 0.72,
    "5FHneW46xGXgsDmGUYz5XQJfNHk3iYxKXrNqYrNqYrNqYrNqYrNq": 0.65
  },
  "count": 2,
  "blocks_per_window": 100,
  "block_window_start": 12300,
  "block_window_end": 12399,
  "current_block": 12345,
  "calculated_at": 1234567890,
  "calculated_at_block": 12345,
  "window_type": "previous"
}
```

**Note:** This endpoint is optional for validators. Scores are calculated and cached per completed window; validators can use this endpoint as the on‑chain source of miner scores when setting rewards.

### POST `/v2/validation_result`
Validators submit validation results after validating posts.

**Accepts multiple validation results** (one per payload received from GET /v2/validation).

**Request:**
```json
{
  "validator_hotkey": "5E2Wu8SspFHdKe1BRvfM5CpSxcjQfzpQxYKGVEYK52G4mbDv",
  "results": [
    {
      "validator_hotkey": "5E2Wu8SspFHdKe1BRvfM5CpSxcjQfzpQxYKGVEYK52G4mbDv",
      "validation_id": "uuid-here-1",
      "miner_hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
      "success": true,
      "failure_reason": null
    },
    {
      "validator_hotkey": "5E2Wu8SspFHdKe1BRvfM5CpSxcjQfzpQxYKGVEYK52G4mbDv",
      "validation_id": "uuid-here-2",
      "miner_hotkey": "5FHneW46xGXgsDmGUYz5XQJfNHk3iYxKXrNqYrNqYrNqYrNqYrNq",
      "success": false,
      "failure_reason": {
        "code": "validation_failed",
        "message": "Post does not meet quality standards"
      }
    }
  ]
}
```

**Response:**
```json
{
  "status": "ok",
  "message": "Processed 2 validation result(s)",
  "successful": 2,
  "failed": 0
}
```

**Reward Logic (conceptual):**
- The API records **success/failure and optional failure reasons** for each validation.
- Miner scores are computed **per block window** and exposed via `GET /v2/scores` (and the `windows` / `miner_window_scores` tables).
- Validators should derive their own on‑chain rewards from these window scores; the API does **not** currently store per‑validation `reward_score` fields.

## Configuration

Environment variables:

- `VALIDATION_PROBABILITY`: Probability of a submission being selected for validation (default: 0.2 = 20%)
- `MAX_SUBMISSION_RATE`: Maximum submissions per `BLOCKS_PER_WINDOW` blocks per miner (default: 5)
- `BLOCKS_PER_WINDOW`: Number of blocks per rate limit window (default: 100 blocks ≈ 20 minutes)
- `DB_NAME`: Database name (default: `miner_api`)
- `DATABASE_URL`: Full PostgreSQL connection URL (overrides individual DB_* vars)
- `DB_HOST`: Database host
- `DB_PORT`: Database port
- `DB_USER`: Database user 
- `DB_PASSWORD`: Database password
- `AUTH_ENABLED`: Enable authentication (default: true)
- `VALIDATION_BACKEND`: `"twitterapi"` (default) or `"x"`; controls which external API is used for tweet validation.
- `TWITTER_API_IO_KEY`: API key for TwitterAPI.io (required when `VALIDATION_BACKEND=twitterapi`).
- `X_BEARER_TOKEN`: Bearer token for the X API (required when `VALIDATION_BACKEND=x`).
- `BT_NETWORK`: Bittensor network (e.g., `test`, `finney`).
- `SUBNET_UID`: Subnet UID for metagraph queries (used by `hotkey_whitelist`).
- `BLACKLISTED_HOTKEY_PREFIXES`: Comma‑separated list of miner hotkey prefixes to block (e.g. `5CknhHw,5DU772f`).
- `SECONDS_PER_BLOCK`: Average seconds per Bittensor block (default: 12.0). Used to estimate time until rate‑limit reset.

## Block Number Semantics

The API relies on `block_utils.get_current_block()` for rate limiting, window boundaries, and scoring. This function:

1. Returns a **fresh block number** from the Bittensor chain when available.
2. If the chain was queried within the last 12 seconds, returns the **cached value** (to reduce RPC load).
3. If the chain is **unreachable** and no cache exists, returns an **estimated block** based on `time.time() / 12`.

This fallback ensures the API remains operational even during chain connectivity issues, but operators should be aware that rate limits and window boundaries may drift slightly in degraded conditions.

## Database Schema

### `submissions`
Stores all miner submissions with:
- All post fields (content, author, tokens, sentiment, score, etc.)
- `accepted_at`: Timestamp when submission was accepted
- `accepted_block`: Block number when submission was accepted
- `selected_for_validation`: Whether this post was selected (0 or 1)
- `validation_id`: UUID if selected for validation and external tweet validation passed
- `x_validated`: Whether external tweet validation was performed (0 or 1)
- `x_validation_result`: External tweet validation result (1 = passed, 0 = failed, NULL = not validated)
- `x_validated_at`: Timestamp when external tweet validation was performed
- `x_validation_error`: JSON-encoded error details if external tweet validation failed
- `post_url`: URL to the post on X/Twitter (if available)

### `validator_assignments`
Tracks which validation payloads are assigned to which validators:
- `validation_id`: Primary key (links to submissions.validation_id)
- `validator_hotkey`: Validator that was assigned this validation
- `assigned_at`: Timestamp when validation was assigned
- `completed_at`: Timestamp when validation was completed (NULL if pending)

### `validation_results`
Stores validator results:
- `validation_id`: Primary key (links to submissions.validation_id)
- `validator_hotkey`: Validator that performed validation
- `miner_hotkey`: Miner that submitted the post
- `post_id`: The post that was validated
- `success`: 1 if validation passed, 0 if failed
- `failure_reason`: JSON-encoded failure reason (if failed)
- `validated_at`: Timestamp when validation completed

> **Note:** Per‑window miner scores are stored in the `windows` and `miner_window_scores` tables (see `db_schema_for_dashboard.md` for dashboard‑oriented schema details). The `validation_results` table does not currently store per‑validation reward scores.

## How It Works

1. **Submission**: Miner submits a post via `/v2/submit`
   - Post is stored in database with `accepted_at` timestamp and `accepted_block` block number
   - Rate limiting is enforced per miner: maximum `MAX_SUBMISSION_RATE` submissions per `BLOCKS_PER_WINDOW` blocks
   - With probability `VALIDATION_PROBABILITY`, post is selected for validation
   - If selected:
     - **External API Validation**: Post is validated against either TwitterAPI.io (default) or the X API (depending on `VALIDATION_BACKEND`) to verify it's a real tweet
     - If external validation passes:
       - `validation_id` is generated and stored
       - Post becomes available for validator validation (assigned via `validator_assignments` table)
     - If external validation fails:
       - Post is marked as validation failed (`x_validation_result = 0`)
       - No `validation_id` is created
       - Post is NOT available for validator validation
       - Response includes external validation error details
   - Response includes block/window information for miner synchronization

2. **Validation Request**: Validator GETs `/v2/validation`
   - System finds unassigned submissions that passed X validation
   - Returns up to `VALIDATIONS_PER_REQUEST` submissions (default: 5)
   - Submissions are assigned to this validator atomically (prevents duplicate assignments)
   - Payload includes the post data but **not** the average score
   - Note: If validators need average scores, they can use `/v2/scores` endpoint

3. **Validation**: Validator validates the post (checks against X API, verifies content matches, etc.)

4. **Result Submission**: Validator POSTs `/v2/validation_result`
   - The API records success/failure and optional failure reasons for each `validation_id`.
   - Assignment is marked as completed in `validator_assignments` table.
   - Miner scores for rewards are derived from the per‑window averages returned by `/v2/scores`.

## Authentication & Access Control

The API uses a **three‑layer access control model**:

1. **Signature verification** (`auth_utils`): Every authenticated request must include valid Bittensor wallet signature headers. The signature is verified against the caller's hotkey.
2. **Whitelist check** (`hotkey_whitelist`): The hotkey must be present in the metagraph‑derived whitelist (miners or validators, depending on endpoint).
3. **Blacklist check** (miners only, in `/v2/submit`): Even if a miner passes auth and whitelist, they can be blocked by prefix via `BLACKLISTED_HOTKEY_PREFIXES`.

### Signature headers

Uses Bittensor wallet signatures via headers:
- `X-Auth-SS58Address`: Hotkey SS58 address
- `X-Auth-Signature`: Hex-encoded signature
- `X-Auth-Message`: Message that was signed
- `X-Auth-Timestamp`: Timestamp of signature

Miners must be registered in the metagraph.
Validators must have validator permit and stake >= threshold.

### Server-side auth wiring

The API server (`main.py`) uses `verify_miner_auth()` and `verify_validator_auth()` helper functions that:
1. Extract auth from headers via `auth_utils.extract_auth_from_headers()`
2. Verify signature and timestamp via `auth_utils.verify_auth_request()`
3. Check role-specific whitelist (`is_miner_hotkey` or `is_validator_hotkey`)

New endpoints should follow this pattern.

### Metagraph dependency

Authentication relies on live metagraph data via `hotkey_whitelist.get_all_whitelisted_hotkeys()`. If the metagraph is unavailable at startup, `AuthConfig` will have an empty `allowed_hotkeys` list and all requests will be rejected.

**Operational note**: In emergency situations, operators can temporarily whitelist specific hotkeys via the `ALLOWED_HOTKEYS` environment variable (comma-separated SS58 addresses).

## Whitelists and Blacklists

Miner and validator access is controlled via whitelists and blacklists:

- **Whitelists (metagraph‑driven)**:
  - `hotkey_whitelist.get_miner_hotkeys()` returns the current set of **miner hotkeys** from the Bittensor metagraph (plus any `MANUAL_MINER_HOTKEYS` when `ALLOW_MANUAL_HOTKEYS=true`).
  - `hotkey_whitelist.get_validator_hotkeys()` returns **validator hotkeys** with validator permit and stake ≥ `STAKE_THRESHOLD` (plus any `MANUAL_VALIDATOR_HOTKEYS` when `ALLOW_MANUAL_HOTKEYS=true`).
  - `AuthConfig` in `auth_utils.py` builds `allowed_hotkeys` from these whitelists (and `ALLOWED_HOTKEYS` overrides) for signature verification.
- **Blacklists**:
  - `BLACKLISTED_HOTKEY_PREFIXES` is an optional comma‑separated list of miner hotkey prefixes to block at the **/v2/submit** layer.
  - If not set, a default list of known bad actors is used (defined in `hotkey_whitelist.py`).
  - To disable all prefix blocking, set `BLACKLISTED_HOTKEY_PREFIXES=""` (empty string).
  - `hotkey_whitelist.is_blacklisted(hotkey)` checks if a miner hotkey starts with any of these prefixes.
  - `/v2/submit` calls `is_blacklisted()` to reject blacklisted miners with a 403 error.

Manual hotkeys (`MANUAL_MINER_HOTKEYS`, `MANUAL_VALIDATOR_HOTKEYS`) are intended for **local testing only**. They are only active when `ALLOW_MANUAL_HOTKEYS=true` and are still subject to the blacklist.