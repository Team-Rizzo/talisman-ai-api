"""
Rate-limited X API validation service.

Handles X API rate limits: 15 tweets every 15 minutes.
When rate limited, waits for reset and continues.

Optional X API backend, only used when VALIDATION_BACKEND=x. Currently disabled by default.
"""

import os
import time
import threading
from typing import Optional, Dict, Tuple
from collections import deque
from datetime import datetime, timedelta

from x_validation import create_x_client, validate_post_with_x_api


class XAPIRateLimiter:
    """
    Rate limiter for X API validation.
    Tracks requests in a sliding 15-minute window.
    """
    
    def __init__(self):
        self.lock = threading.Lock()
        self.request_times = deque()  # Timestamps of requests
        self.max_requests = 15
        self.window_seconds = 15 * 60  # 15 minutes
        self.x_client = None
        self._init_client()
    
    def _init_client(self):
        """Initialize X API client."""
        try:
            self.x_client = create_x_client()
            print("[X_RATE_LIMITER] X API client initialized")
        except Exception as e:
            print(f"[X_RATE_LIMITER] ✗ Failed to initialize X API client: {e}")
            self.x_client = None
    
    def _clean_old_requests(self):
        """Remove requests outside the 15-minute window."""
        now = time.time()
        while self.request_times and (now - self.request_times[0]) > self.window_seconds:
            self.request_times.popleft()
    
    def _wait_for_reset(self):
        """Wait until the rate limit window resets."""
        if not self.request_times:
            return
        
        oldest_request = self.request_times[0]
        now = time.time()
        time_until_reset = self.window_seconds - (now - oldest_request)
        
        if time_until_reset > 0:
            wait_seconds = time_until_reset + 1  # Add 1 second buffer
            reset_time = datetime.fromtimestamp(now + wait_seconds)
            print(f"[X_RATE_LIMITER] Rate limit reached. Waiting {wait_seconds:.1f}s until reset at {reset_time.strftime('%H:%M:%S')}...")
            time.sleep(wait_seconds)
            # Clean up old requests after waiting
            self._clean_old_requests()
    
    def validate_post(self, post: Dict) -> Tuple[bool, Optional[Dict]]:
        """
        Validate a post using X API with rate limiting.
        
        Args:
            post: Post dictionary with post_id, content, author, date, etc.
            
        Returns:
            Tuple of (is_valid: bool, error_dict: Optional[Dict])
        """
        if self.x_client is None:
            # Try to reinitialize client
            self._init_client()
            if self.x_client is None:
                return False, {
                    "code": "x_api_unavailable",
                    "message": "X API client unavailable",
                    "post_id": post.get("post_id"),
                    "details": {}
                }
        
        with self.lock:
            # Clean old requests outside the window
            self._clean_old_requests()
            
            # Check if we're at the limit
            if len(self.request_times) >= self.max_requests:
                # Wait for reset
                self._wait_for_reset()
            
            # Record this request
            now = time.time()
            self.request_times.append(now)
            
            remaining = self.max_requests - len(self.request_times)
            print(f"[X_RATE_LIMITER] Validating post_id={post.get('post_id')} (remaining: {remaining}/{self.max_requests})")
        
        # Perform validation (outside lock to avoid blocking other threads)
        try:
            is_valid, error_dict = validate_post_with_x_api(post, self.x_client)
            return is_valid, error_dict
        except Exception as e:
            print(f"[X_RATE_LIMITER] ✗ Validation error: {e}")
            return False, {
                "code": "x_api_error",
                "message": f"Validation error: {e}",
                "post_id": post.get("post_id"),
                "details": {}
            }


# Global rate limiter instance
_rate_limiter: Optional[XAPIRateLimiter] = None
_rate_limiter_lock = threading.Lock()


def get_rate_limiter() -> XAPIRateLimiter:
    """Get or create the global rate limiter instance."""
    global _rate_limiter
    with _rate_limiter_lock:
        if _rate_limiter is None:
            _rate_limiter = XAPIRateLimiter()
        return _rate_limiter

