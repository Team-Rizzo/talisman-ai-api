"""
TwitterAPI.io validation module.

Validates tweets using TwitterAPI.io.

Default production backend: used when VALIDATION_BACKEND=twitterapi or unset.
"""

import os
import re
import requests
from typing import Dict, Optional, Tuple
from dateutil.parser import isoparse, parse as dateutil_parse
from validation_utils import norm_text, metric_tol, metric_inflated

try:
    from dotenv import load_dotenv
    dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
    load_dotenv(dotenv_path)
except ImportError:
    pass


def strip_urls(s: str) -> str:
    """
    Strip URLs from text for comparison.
    
    Twitter/X APIs sometimes include URLs in text and sometimes don't,
    depending on the endpoint. We strip URLs from both texts before comparison
    to avoid false mismatches.
    
    Args:
        s: Text string that may contain URLs
        
    Returns:
        Text with URLs removed
    """
    # Pattern to match URLs (http://, https://, www., or t.co short links)
    url_pattern = r'https?://[^\s]+|www\.[^\s]+|t\.co/[^\s]+'
    s = re.sub(url_pattern, '', s)
    # Clean up any extra whitespace left after removing URLs
    s = re.sub(r'\s+', ' ', s).strip()
    return s


class TwitterAPIValidator:
    """
    Validates tweets using TwitterAPI.io.
    
    TwitterAPI.io may return data in various response shapes (e.g., `tweets` array,
    bare `data` object, or nested `data` list). The parsing logic below handles
    these variants defensively to avoid false negatives.
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("TWITTER_API_IO_KEY")
        if not self.api_key:
            raise ValueError("TWITTER_API_IO_KEY not set")
        self.headers = {"X-API-Key": self.api_key}
        self.tweet_url = "https://api.twitterapi.io/twitter/tweets"
        self.user_url = "https://api.twitterapi.io/twitter/user/batch_info_by_ids"
    
    def validate_post(self, post: Dict) -> Tuple[bool, Optional[Dict]]:
        """Validate a single post using TwitterAPI.io."""
        post_id = post.get("post_id")
        if not post_id:
            return False, {
                "code": "missing_post_id",
                "message": "post_id is required",
                "post_id": None,
                "details": {}
            }
        
        try:
            # Fetch tweet
            params = {"tweet_ids": str(post_id)}
            response = requests.get(self.tweet_url, headers=self.headers, params=params, timeout=10)
            
            if response.status_code != 200:
                return False, {
                    "code": "api_error",
                    "message": f"API error {response.status_code}: {response.text[:200]}",
                    "post_id": post_id,
                    "details": {}
                }
            
            data = response.json()
            
            # Parse tweet - TwitterAPI.io returns {'tweets': [...]}
            tweet_data = None
            if isinstance(data, dict):
                if "tweets" in data and isinstance(data["tweets"], list) and len(data["tweets"]) > 0:
                    tweet_data = data["tweets"][0]
                elif "id" in data:
                    tweet_data = data
                elif "data" in data:
                    tweet_data = data["data"] if isinstance(data["data"], dict) else (data["data"][0] if isinstance(data["data"], list) and len(data["data"]) > 0 else None)
            elif isinstance(data, list) and len(data) > 0:
                tweet_data = data[0]
            
            if not tweet_data:
                return False, {
                    "code": "post_not_found",
                    "message": "Post not found or inaccessible",
                    "post_id": post_id,
                    "details": {}
                }
            
            tweet_id = str(tweet_data.get("id", ""))
            if tweet_id != str(post_id):
                return False, {
                    "code": "post_not_found",
                    "message": "Post not found or inaccessible",
                    "post_id": post_id,
                    "details": {}
                }
            
            # Extract author info - TwitterAPI.io includes author in tweet or we need to fetch
            author = tweet_data.get("author")
            author_id = tweet_data.get("author_id") or (author.get("id") if isinstance(author, dict) else None)
            
            # If author not included, fetch it
            if not author and author_id:
                params = {"userIds": str(author_id)}
                response = requests.get(self.user_url, headers=self.headers, params=params, timeout=10)
                
                if response.status_code != 200:
                    return False, {
                        "code": "api_error",
                        "message": f"User API error {response.status_code}",
                        "post_id": post_id,
                        "details": {}
                    }
                
                user_data = response.json()
                if isinstance(user_data, dict):
                    if "users" in user_data and isinstance(user_data["users"], list) and len(user_data["users"]) > 0:
                        author = user_data["users"][0]
                    elif "id" in user_data:
                        author = user_data
                    elif "data" in user_data:
                        author = user_data["data"] if isinstance(user_data["data"], dict) else (user_data["data"][0] if isinstance(user_data["data"], list) and len(user_data["data"]) > 0 else None)
                elif isinstance(user_data, list) and len(user_data) > 0:
                    author = user_data[0]
            
            if not author:
                return False, {
                    "code": "author_not_found",
                    "message": "Author not found",
                    "post_id": post_id,
                    "details": {}
                }
            
            # Validate text
            miner_text = post.get("content") or ""
            live_text = tweet_data.get("text") or ""
            
            # Normalize both texts and strip URLs before comparison
            # URLs are sometimes included/excluded depending on API endpoint
            miner_normalized = norm_text(strip_urls(miner_text))
            live_normalized = norm_text(strip_urls(live_text))
            
            # Check if texts match (exact match or miner text is a prefix of live text)
            # The miner might get truncated text from X API search results, so we allow prefix matches
            # Accept if: exact match OR (prefix match AND miner text is substantial >= 100 chars)
            texts_match = False
            if miner_normalized == live_normalized:
                texts_match = True
            elif len(miner_normalized) > 0 and live_normalized.startswith(miner_normalized):
                # Miner text is a prefix - accept if it's substantial (>= 100 chars)
                # This handles cases where X API truncates tweets in search results
                if len(miner_normalized) >= 100:
                    texts_match = True
                # No logging for successful matches to reduce console spam
            
            if not texts_match:
                # Log error summary only (detailed logs removed to reduce console spam)
                print(f"[TwitterAPIValidator] ✗ Text mismatch for post_id={post_id} (miner_len={len(miner_normalized)}, live_len={len(live_normalized)})")
                return False, {
                    "code": "text_mismatch",
                    "message": "content does not match live post text",
                    "post_id": post_id,
                    "details": {
                        "miner": miner_text[:100],
                        "live": live_text[:100],
                        "miner_normalized": miner_normalized[:100],
                        "live_normalized": live_normalized[:100]
                    }
                }
            
            # Validate author
            miner_author_raw = post.get("author") or ""
            miner_author = miner_author_raw.strip().lower()
            # Remove @ prefix if present (some APIs include it, some don't)
            if miner_author.startswith("@"):
                miner_author = miner_author[1:]
            
            # Try multiple possible field names for username (TwitterAPI.io uses "userName" with capital N)
            live_author_raw = (
                author.get("userName") or  # TwitterAPI.io uses capital N
                author.get("username") or 
                author.get("screen_name") or 
                author.get("name") or 
                ""
            )
            live_author = live_author_raw.strip().lower()
            # Remove @ prefix if present
            if live_author.startswith("@"):
                live_author = live_author[1:]
            
            if miner_author != live_author:
                # Log error summary only (detailed logs removed to reduce console spam)
                print(f"[TwitterAPIValidator] ✗ Author mismatch for post_id={post_id} (miner='{miner_author}', live='{live_author}')")
                return False, {
                    "code": "author_mismatch",
                    "message": "author does not match",
                    "post_id": post_id,
                    "details": {
                        "miner": miner_author_raw,
                        "live": live_author_raw,
                        "miner_normalized": miner_author,
                        "live_normalized": live_author
                    }
                }
            
            # Validate timestamp
            miner_ts = int(post.get("date") or post.get("timestamp") or 0)
            if miner_ts == 0:
                return False, {
                    "code": "timestamp_missing",
                    "message": "timestamp is missing",
                    "post_id": post_id,
                    "details": {}
                }
            
            # Try multiple possible field names for created_at (TwitterAPI.io might use camelCase)
            created_at = (
                tweet_data.get("created_at") or
                tweet_data.get("createdAt") or
                tweet_data.get("created_at_str") or
                tweet_data.get("date") or
                None
            )
            
            if created_at is None:
                # Log error only (to reduce console spam)
                print(f"[TwitterAPIValidator] ✗ No created_at field found for post_id={post_id}")
                return False, {
                    "code": "missing_created_at",
                    "message": "live post missing created_at",
                    "post_id": post_id,
                    "details": {"available_fields": list(tweet_data.keys()) if isinstance(tweet_data, dict) else []}
                }
            
            # Parse timestamp - handle both ISO format (X API) and Twitter format (TwitterAPI.io)
            if isinstance(created_at, str):
                try:
                    # Try ISO format first (X API uses ISO 8601: "2025-11-24T16:54:12+00:00")
                    try:
                        live_ts = int(isoparse(created_at).timestamp())
                    except (ValueError, TypeError):
                        # Fall back to flexible parser (handles Twitter format: "Mon Nov 24 16:54:12 +0000 2025")
                        # This handles TwitterAPI.io and other non-ISO formats
                        live_ts = int(dateutil_parse(created_at).timestamp())
                except Exception as e:
                    print(f"[TwitterAPIValidator] ✗ Failed to parse created_at for post_id={post_id}: {e}")
                    return False, {
                        "code": "invalid_created_at",
                        "message": f"Failed to parse created_at: {created_at}",
                        "post_id": post_id,
                        "details": {"created_at": created_at, "error": str(e)}
                    }
            elif isinstance(created_at, (int, float)):
                live_ts = int(created_at)
            else:
                print(f"[TwitterAPIValidator] ✗ created_at has unexpected type for post_id={post_id}: {type(created_at).__name__}")
                return False, {
                    "code": "invalid_created_at",
                    "message": f"created_at has unexpected type: {type(created_at).__name__}",
                    "post_id": post_id,
                    "details": {"created_at": str(created_at), "type": type(created_at).__name__}
                }
            
            if miner_ts != live_ts:
                return False, {
                    "code": "timestamp_mismatch",
                    "message": "timestamp must match exactly",
                    "post_id": post_id,
                    "details": {"miner": miner_ts, "live": live_ts, "diff_seconds": abs(live_ts - miner_ts)}
                }
            
            # Validate metrics - TwitterAPI.io uses different field names
            pm = tweet_data.get("public_metrics", {}) or {}
            live_likes = int(pm.get("like_count", 0) or tweet_data.get("likeCount", 0) or 0)
            live_rts = int(pm.get("retweet_count", 0) or tweet_data.get("retweetCount", 0) or 0)
            live_replies = int(pm.get("reply_count", 0) or tweet_data.get("replyCount", 0) or 0)
            
            m_likes = int(post.get("likes") or 0)
            m_rts = int(post.get("retweets") or 0)
            m_replies = int((post.get("replies") if post.get("replies") is not None else post.get("responses")) or 0)
            
            if metric_inflated(m_likes, live_likes):
                return False, {
                    "code": "metric_inflation_likes",
                    "message": "likes overstated beyond tolerance",
                    "post_id": post_id,
                    "details": {"miner": m_likes, "live": live_likes, "tolerance": metric_tol(live_likes)}
                }
            
            if metric_inflated(m_rts, live_rts):
                return False, {
                    "code": "metric_inflation_retweets",
                    "message": "retweets overstated beyond tolerance",
                    "post_id": post_id,
                    "details": {"miner": m_rts, "live": live_rts, "tolerance": metric_tol(live_rts)}
                }
            
            if metric_inflated(m_replies, live_replies):
                return False, {
                    "code": "metric_inflation_replies",
                    "message": "replies overstated beyond tolerance",
                    "post_id": post_id,
                    "details": {"miner": m_replies, "live": live_replies, "tolerance": metric_tol(live_replies)}
                }
            
            # Validate followers
            # Miners submit standardized format: "followers": int (regardless of which API they use)
            # TwitterAPI.io returns followers directly on author object: author.followers
            # X API (if used) returns: author.public_metrics.followers_count
            # We check TwitterAPI.io format first since that's what this validator uses
            author_metrics = author.get("public_metrics", {}) or {}
            # Try multiple possible field names (TwitterAPI.io uses "followers" directly)
            followers = int(
                author.get("followers") or  # TwitterAPI.io uses this directly
                author_metrics.get("followers_count", 0) or  # X API format (if author came from X API)
                author.get("followersCount", 0) or  # Alternative camelCase
                0
            )
            m_followers = int(post.get("followers") or 0)
            
            # Only log follower comparison on failure (to reduce console spam)
            if metric_inflated(m_followers, followers):
                exceeded_by = m_followers - (followers + metric_tol(followers))
                print(f"[TwitterAPIValidator] ✗ Follower inflation detected: miner={m_followers}, live={followers}, tolerance={metric_tol(followers)}, exceeded_by={exceeded_by}")
                return False, {
                    "code": "metric_inflation_followers",
                    "message": "followers overstated beyond tolerance",
                    "post_id": post_id,
                    "details": {"miner": m_followers, "live": followers, "tolerance": metric_tol(followers)}
                }
            
            return True, None
            
        except Exception as e:
            return False, {
                "code": "validation_error",
                "message": f"Validation error: {e}",
                "post_id": post_id,
                "details": {}
            }


# Global validator instance
_validator: Optional[TwitterAPIValidator] = None
_validator_lock = __import__("threading").Lock()


def get_validator() -> TwitterAPIValidator:
    """Get or create the global validator instance."""
    global _validator
    with _validator_lock:
        if _validator is None:
            _validator = TwitterAPIValidator()
        return _validator

