"""
X API validation module for API-side tweet verification.

This module validates tweets using the X API to verify they are real before
sending them to validators for LLM validation.

This module replicates the validation logic from the validator's grader.py
to ensure consistent validation behavior.

Optional X API backend, only used when VALIDATION_BACKEND=x. Currently disabled by default.
"""

import os
import tweepy
from typing import Dict, Optional, Tuple
from datetime import datetime
from validation_utils import norm_text, metric_tol, metric_inflated

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
    load_dotenv(dotenv_path)
except ImportError:
    # dotenv not available, rely on system environment variables
    pass


def create_x_client():
    """Create an X API client using bearer token from environment."""
    bearer_token = os.getenv("X_BEARER_TOKEN")
    if not bearer_token or bearer_token == "null":
        raise ValueError("X_BEARER_TOKEN not set - X API is required for validation")
    try:
        return tweepy.Client(bearer_token=bearer_token)
    except Exception as e:
        raise RuntimeError(f"Failed to initialize X API client: {e}") from e


def validate_post_with_x_api(post: Dict, x_client: tweepy.Client) -> Tuple[bool, Optional[Dict]]:
    """
    Validate a post using X API.
    
    Args:
        post: Post dictionary with post_id, content, author, date, likes, retweets, responses, followers
        x_client: Tweepy client instance
        
    Returns:
        Tuple of (is_valid: bool, error_dict: Optional[Dict])
        - If valid: (True, None)
        - If invalid: (False, error_dict with code, message, post_id, details)
    """
    post_id = post.get("post_id")
    if not post_id:
        return False, {
            "code": "missing_post_id",
            "message": "post_id is required",
            "post_id": None,
            "details": {}
        }
    
    # Fetch from X API
    try:
        resp = x_client.get_tweet(
            id=str(post_id),
            expansions=["author_id"],
            tweet_fields=["created_at", "public_metrics", "text"],
            user_fields=["username", "name", "created_at", "public_metrics"],
        )
    except tweepy.TooManyRequests:
        # Rate limit hit - the rate limiter should handle this, but if we still hit it, return error
        return False, {
            "code": "x_api_rate_limit",
            "message": "X API rate limit exceeded",
            "post_id": post_id,
            "details": {}
        }
    except tweepy.NotFound:
        # Tweet not found or inaccessible
        return False, {
            "code": "post_not_found",
            "message": "Post not found or inaccessible",
            "post_id": post_id,
            "details": {}
        }
    except tweepy.Unauthorized:
        # Unauthorized - API token issue
        return False, {
            "code": "x_api_error",
            "message": "X API unauthorized - check bearer token",
            "post_id": post_id,
            "details": {}
        }
    except tweepy.Forbidden:
        # Forbidden - tweet might be protected/deleted
        return False, {
            "code": "post_not_found",
            "message": "Post not accessible (may be protected or deleted)",
            "post_id": post_id,
            "details": {}
        }
    except Exception as e:
        return False, {
            "code": "x_api_error",
            "message": f"X API error: {e}",
            "post_id": post_id,
            "details": {}
        }
    
    if not resp or not getattr(resp, "data", None):
        return False, {
            "code": "x_api_no_response",
            "message": "X API gave no response",
            "post_id": post_id,
            "details": {}
        }
    
    tweet_data = resp.data
    includes = getattr(resp, "includes", {}) or {}
    users = {u.id: u for u in includes.get("users", [])}
    author = users.get(tweet_data.author_id)
    
    # 1) Text must match exactly after normalization
    miner_text = (post.get("content") or "")
    live_text = tweet_data.text or ""
    if norm_text(miner_text) != norm_text(live_text):
        return False, {
            "code": "text_mismatch",
            "message": "content does not match live post text (after normalization)",
            "post_id": post_id,
            "details": {
                "miner": miner_text[:100],
                "live": live_text[:100],
                "preview_len": 100
            }
        }
    
    # 2) Author must match (lowercase usernames)
    miner_author = (post.get("author") or "").strip().lower()
    live_author = (author.username if author else "").strip().lower()
    if miner_author != live_author:
        return False, {
            "code": "author_mismatch",
            "message": "author does not match",
            "post_id": post_id,
            "details": {
                "miner": post.get("author", ""),
                "live": author.username if author else ""
            }
        }
    
    # 3) Timestamp must match exactly (Unix seconds)
    miner_ts = post.get("date") or post.get("timestamp")
    if miner_ts is None:
        return False, {
            "code": "timestamp_missing",
            "message": "timestamp is missing",
            "post_id": post_id,
            "details": {}
        }
    miner_ts = int(miner_ts)
    if not tweet_data.created_at:
        return False, {
            "code": "missing_created_at",
            "message": "live post missing created_at from X API",
            "post_id": post_id,
            "details": {}
        }
    live_ts = int(tweet_data.created_at.timestamp())
    if miner_ts != live_ts:
        return False, {
            "code": "timestamp_mismatch",
            "message": "timestamp must match exactly",
            "post_id": post_id,
            "details": {
                "miner": miner_ts,
                "live": live_ts,
                "diff_seconds": abs(live_ts - miner_ts)
            }
        }
    
    # 4) Engagement/author metrics may NOT be overstated beyond tolerance
    pm = getattr(tweet_data, "public_metrics", {}) or {}
    live_likes = int(pm.get("like_count", 0) or 0)
    live_rts = int(pm.get("retweet_count", 0) or 0)
    live_replies = int(pm.get("reply_count", 0) or 0)
    m_likes = int(post.get("likes") or 0)
    m_rts = int(post.get("retweets") or 0)
    m_replies = int((post.get("replies") if post.get("replies") is not None else post.get("responses")) or 0)
    
    if metric_inflated(m_likes, live_likes):
        return False, {
            "code": "metric_inflation_likes",
            "message": "likes overstated beyond tolerance",
            "post_id": post_id,
            "details": {
                "miner": m_likes,
                "live": live_likes,
                "tolerance": metric_tol(live_likes)
            }
        }
    if metric_inflated(m_rts, live_rts):
        return False, {
            "code": "metric_inflation_retweets",
            "message": "retweets overstated beyond tolerance",
            "post_id": post_id,
            "details": {
                "miner": m_rts,
                "live": live_rts,
                "tolerance": metric_tol(live_rts)
            }
        }
    if metric_inflated(m_replies, live_replies):
        return False, {
            "code": "metric_inflation_replies",
            "message": "replies overstated beyond tolerance",
            "post_id": post_id,
            "details": {
                "miner": m_replies,
                "live": live_replies,
                "tolerance": metric_tol(live_replies)
            }
        }
    
    # X API (tweepy) stores followers in author.public_metrics.followers_count
    author_metrics = getattr(author, "public_metrics", {}) or {} if author else {}
    followers = int(author_metrics.get("followers_count", 0) or 0)
    m_followers = int(post.get("followers") or 0)
    
    if metric_inflated(m_followers, followers):
        print(f"[XAPIValidator] ✗ Follower inflation detected: miner={m_followers}, live={followers}, tolerance={metric_tol(followers)}, exceeded_by={m_followers - (followers + metric_tol(followers))}")
        return False, {
            "code": "metric_inflation_followers",
            "message": "followers overstated beyond tolerance",
            "post_id": post_id,
            "details": {
                "miner": m_followers,
                "live": followers,
                "tolerance": metric_tol(followers)
            }
        }
    
    # All checks passed
    return True, None

