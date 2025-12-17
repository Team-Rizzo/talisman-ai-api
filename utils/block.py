"""Block utilities for API v2."""
import os
import bittensor as bt
import time
import threading
import sys

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
    load_dotenv(dotenv_path)
except ImportError:
    # dotenv not available, rely on system environment variables
    pass

NETWORK = os.getenv("BT_NETWORK", "test")
_block_cache = None
_block_cache_time = 0
_subtensor_instance = None
_block_lock = threading.Lock()

def get_current_block() -> int:
    """
    Get current block number with 12s cache.
    
    Behavior:
      1. If cached value exists and is <12s old, return it (avoids RPC load).
      2. Otherwise, query the Bittensor chain for the current block.
      3. If the chain call fails and a cached value exists, return the stale cache.
      4. If no cache exists and the chain is unreachable, return an *estimated* block
         based on `time.time() / 12` (1 block ≈ 12 seconds).
    
    Callers (rate limiting, scoring, window boundaries) should be aware that the
    returned block may be slightly stale or estimated during network issues.
    """
    global _block_cache, _block_cache_time, _subtensor_instance
    
    with _block_lock:
        current_time = time.time()
        cache_age = current_time - _block_cache_time if _block_cache_time else float('inf')
        
        # Use cached value if it's less than 12 seconds old
        if _block_cache is not None and cache_age < 12:
            return _block_cache
        
        # Try to get fresh block number with error handling and timeout
        try:
            # Reuse subtensor instance if available, otherwise create new one
            if _subtensor_instance is None:
                _subtensor_instance = bt.subtensor(network=NETWORK)
            
            # Try to get block with error handling
            # Note: bt.subtensor.get_current_block() may hang on network issues
            # We rely on the cache to prevent repeated slow calls
            try:
                new_block = _subtensor_instance.get_current_block()
                _block_cache = new_block
                _block_cache_time = time.time()
                return _block_cache
            except Exception as e:
                # If network call fails or times out, use cached value if available
                print(f"[BLOCK] Failed to fetch current block: {e}, using cached value", file=sys.stderr)
                if _block_cache is not None:
                    # Extend cache time slightly to avoid rapid retries
                    return _block_cache
                raise
        except Exception as e:
            # If network call fails, use cached value if available
            if _block_cache is not None:
                # Extend cache time slightly to avoid rapid retries
                return _block_cache
            # If no cache available, return a reasonable default (current time-based estimate)
            # This is a fallback - should rarely happen on first call
            estimated_block = int(time.time() / 12)  # Rough estimate: 1 block per 12 seconds
            _block_cache = estimated_block
            _block_cache_time = time.time()
            return estimated_block
