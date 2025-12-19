"""
TAO Price Cache Service.

Fetches TAO/USD price from TaoStats every 15 minutes and caches it in memory.
Validators read from the cached value instead of hitting external APIs directly.
"""

import os
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Configuration from environment
TAO_PRICE_REFRESH_SECONDS = int(os.getenv("TAO_PRICE_REFRESH_SECONDS", "900"))  # 15 minutes
TAO_PRICE_STALE_SECONDS = int(os.getenv("TAO_PRICE_STALE_SECONDS", "3600"))  # 1 hour
TAOSTATS_URL = os.getenv("TAOSTATS_URL", "https://taostats.io/api/price/price")
TAOSTATS_TIMEOUT = float(os.getenv("TAOSTATS_TIMEOUT", "10.0"))


@dataclass
class TaoPriceCache:
    """In-memory cache for TAO/USD price."""
    price_usd: Optional[float] = None
    last_updated: Optional[datetime] = None
    source: str = "taostats"
    error: Optional[str] = None


# Global cache instance
_cache = TaoPriceCache()
_refresh_task: Optional[asyncio.Task] = None


async def fetch_tao_price() -> float:
    """
    Fetch TAO/USD price from TaoStats.
    
    Returns:
        The TAO price in USD.
        
    Raises:
        Exception if fetch fails.
    """
    async with httpx.AsyncClient(timeout=TAOSTATS_TIMEOUT) as client:
        response = await client.get(TAOSTATS_URL)
        response.raise_for_status()
        data = response.json()
        
        # TaoStats returns: {"data": [{"price": "123.45", "last_updated": "..."}]}
        price_str = data["data"][0]["price"]
        return float(price_str)


async def refresh_price() -> None:
    """Refresh the cached TAO price from TaoStats."""
    global _cache
    
    max_retries = 3
    retry_delays = [1, 2, 4]  # Exponential backoff
    
    for attempt in range(max_retries):
        try:
            price = await fetch_tao_price()
            _cache.price_usd = price
            _cache.last_updated = datetime.now(timezone.utc)
            _cache.error = None
            logger.info(f"TAO price updated: ${price:.2f}")
            return
        except Exception as e:
            _cache.error = str(e)
            if attempt < max_retries - 1:
                delay = retry_delays[attempt]
                logger.warning(f"TAO price fetch failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {delay}s...")
                await asyncio.sleep(delay)
            else:
                logger.error(f"TAO price fetch failed after {max_retries} attempts: {e}")
                # Keep the last good value if we have one


async def _refresh_loop() -> None:
    """Background loop that refreshes price every TAO_PRICE_REFRESH_SECONDS."""
    while True:
        try:
            await refresh_price()
        except Exception as e:
            logger.error(f"Unexpected error in price refresh loop: {e}")
        
        await asyncio.sleep(TAO_PRICE_REFRESH_SECONDS)


def start_refresh_task() -> asyncio.Task:
    """Start the background refresh task. Call this at app startup."""
    global _refresh_task
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.create_task(_refresh_loop())
        logger.info(f"TAO price refresh task started (interval: {TAO_PRICE_REFRESH_SECONDS}s)")
    return _refresh_task


def stop_refresh_task() -> None:
    """Stop the background refresh task. Call this at app shutdown."""
    global _refresh_task
    if _refresh_task and not _refresh_task.done():
        _refresh_task.cancel()
        logger.info("TAO price refresh task stopped")


def get_cached_price() -> TaoPriceCache:
    """Get the current cached price data."""
    return _cache


def is_price_stale() -> bool:
    """Check if the cached price is stale (older than TAO_PRICE_STALE_SECONDS)."""
    if _cache.last_updated is None:
        return True
    
    age = (datetime.now(timezone.utc) - _cache.last_updated).total_seconds()
    return age > TAO_PRICE_STALE_SECONDS

