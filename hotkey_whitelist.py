"""
Hotkey whitelist for miners and validators.

This module provides functions to get whitelisted hotkeys for authentication.

- Validators: Automatically fetched from metagraph (with validator permit and stake >= threshold) and cached for 2 minutes
- Miners: Automatically fetched from metagraph via mg.hotkeys and cached for 2 minutes
"""

from typing import List, Dict
import logging
import time
import threading
import json
import os
from pathlib import Path

import bittensor as bt

logger = logging.getLogger(__name__)

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
    load_dotenv(dotenv_path)
except ImportError:
    # dotenv not available, rely on system environment variables
    pass

# Network configuration from environment variables
NETWORK = os.getenv("BT_NETWORK", "test")
NETUID = int(os.getenv("SUBNET_UID", "45"))
# Minimum stake required for validators.
# Make this env-driven so testnet/local can run without requiring large stake.
# Defaults to the previous hard-coded value (20000).
STAKE_THRESHOLD = int(os.getenv("STAKE_THRESHOLD", "20000"))

# Manual hotkeys for LOCAL/TESTNET TESTING ONLY (only active when ALLOW_MANUAL_HOTKEYS=true).
# Do NOT enable ALLOW_MANUAL_HOTKEYS in production environments.
# NOTE: Even when enabled, manual miners are still subject to the blacklist.
ALLOW_MANUAL_HOTKEYS = os.getenv("ALLOW_MANUAL_HOTKEYS", "false").lower() == "true"

def _parse_hotkey_list(env_key: str) -> List[str]:
    """
    Parse comma-separated SS58 hotkeys from an env var.
    Returns [] if unset/empty.
    """
    raw = os.getenv(env_key, "").strip()
    if not raw:
        return []
    return [hk.strip() for hk in raw.split(",") if hk.strip()]

# Allow overriding manual hotkeys via env. Keep the previous hard-coded defaults as fallback.
_DEFAULT_MANUAL_VALIDATOR_HOTKEYS = [
    "5E2Wu8SspFHdKe1BRvfM5CpSxcjQfzpQxYKGVEYK52G4mbDv",
]
_DEFAULT_MANUAL_MINER_HOTKEYS = [
    "5GUDZqzmvTQ1UiTUoFBS96USxsSjF6GQnnL4pnNhf7AmWL7c",
]

MANUAL_VALIDATOR_HOTKEYS = _parse_hotkey_list("MANUAL_VALIDATOR_HOTKEYS") or _DEFAULT_MANUAL_VALIDATOR_HOTKEYS
MANUAL_MINER_HOTKEYS = _parse_hotkey_list("MANUAL_MINER_HOTKEYS") or _DEFAULT_MANUAL_MINER_HOTKEYS

# Blacklist configuration
# Blacklisted hotkey prefixes are configured via BLACKLISTED_HOTKEY_PREFIXES environment variable (comma-separated)
# If not set, uses the default list of known bad actors below.
# Example override: BLACKLISTED_HOTKEY_PREFIXES=5CknhHw,5DU772f,5C7ig5d
# To clear all blacklists: BLACKLISTED_HOTKEY_PREFIXES=""
_DEFAULT_BLACKLISTED_PREFIXES = [
    "5CknhHw", "5DU772f", "5C7ig5d", "5D278dL", "5EARyFu",
    "5D8FHTQ", "5CXAz6P", "5CAe6Pk", "5Hgt5MT", "5CCoCsF",
    "5F4fqys", "5GsQgsD", "5Dd4Nxb", "5EqX25D", "5FjUZBQ",
    "5E5CzJE", "5FsG5uj", "5GCbPpU", "5CyxH84", "5DG9Bkh",
]
_BLACKLISTED_HOTKEY_PREFIXES_ENV = os.getenv("BLACKLISTED_HOTKEY_PREFIXES")
if _BLACKLISTED_HOTKEY_PREFIXES_ENV is not None:
    # Env var is set (even if empty string) - use it
    BLACKLISTED_HOTKEY_PREFIXES = [p.strip() for p in _BLACKLISTED_HOTKEY_PREFIXES_ENV.split(",") if p.strip()]
    if BLACKLISTED_HOTKEY_PREFIXES:
        logger.info(f"Using {len(BLACKLISTED_HOTKEY_PREFIXES)} blacklisted prefixes from BLACKLISTED_HOTKEY_PREFIXES env var")
    else:
        logger.info("BLACKLISTED_HOTKEY_PREFIXES set to empty - no hotkeys will be blacklisted")
else:
    # Env var not set - use defaults
    BLACKLISTED_HOTKEY_PREFIXES = _DEFAULT_BLACKLISTED_PREFIXES
    logger.info(f"Using {len(BLACKLISTED_HOTKEY_PREFIXES)} default blacklisted prefixes")


# Cache duration for both miners and validators
_CACHE_DURATION_SECONDS = 2 * 60  # 2 minutes

# Validator hotkeys configuration
# Fetched from metagraph (validators with permit and stake >= threshold)
_VALIDATOR_HOTKEYS_CACHE: List[str] = []
_VALIDATOR_DATA_CACHE: List[Dict[str, str]] = []  # Stores full validator info (name + hotkey)
_VALIDATOR_CACHE_TIMESTAMP: float = 0.0
_VALIDATOR_CACHE_LOCK = threading.Lock()

# File to store validator hotkeys for inspection
_VALIDATOR_HOTKEYS_FILE = Path(__file__).parent / "validator_hotkeys.json"

# Cached miner hotkeys
_MINER_HOTKEYS_CACHE: List[str] = []
_MINER_CACHE_TIMESTAMP: float = 0.0
_MINER_CACHE_LOCK = threading.Lock()

# File to store miner hotkeys for inspection
_MINER_HOTKEYS_FILE = Path(__file__).parent / "miner_hotkeys.json"


def _save_miner_hotkeys_to_file(hotkeys: List[str], timestamp: float):
    """
    Save miner hotkeys to JSON file for inspection.
    
    Format:
    {
        "last_updated": 1234567890.0,
        "last_updated_iso": "2024-01-01T12:00:00Z",
        "total_count": 42,
        "network": "test",
        "netuid": 76,
        "hotkeys": ["5E2Wu8SspFHdKe1BRvfM5CpSxcjQfzpQxYKGVEYK52G4mbDv", ...]
    }
    """
    try:
        from datetime import datetime, timezone
        data = {
            "last_updated": timestamp,
            "last_updated_iso": datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(),
            "total_count": len(hotkeys),
            "network": NETWORK,
            "netuid": NETUID,
            "hotkeys": sorted(hotkeys)  # Sort for easier inspection
        }
        with open(_MINER_HOTKEYS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        logger.debug(f"Saved {len(hotkeys)} miner hotkeys to {_MINER_HOTKEYS_FILE}")
    except Exception as e:
        logger.error(f"Failed to save miner hotkeys to file: {e}", exc_info=True)


def _save_validator_hotkeys_to_file(validators: List[Dict[str, str]], timestamp: float):
    """
    Save validator hotkeys to JSON file for inspection.
    
    Format:
    {
        "last_updated": 1234567890.0,
        "last_updated_iso": "2024-01-01T12:00:00Z",
        "total_count": 5,
        "network": "finney",
        "netuid": 45,
        "stake_threshold": 20000,
        "validators": [
            {"name": "Validator 1", "hotkey": "5E2Wu8SspFHdKe1BRvfM5CpSxcjQfzpQxYKGVEYK52G4mbDv"},
            ...
        ],
        "hotkeys": ["5E2Wu8SspFHdKe1BRvfM5CpSxcjQfzpQxYKGVEYK52G4mbDv", ...]
    }
    """
    try:
        from datetime import datetime, timezone
        hotkeys = [v["hotkey"] for v in validators]
        data = {
            "last_updated": timestamp,
            "last_updated_iso": datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(),
            "total_count": len(validators),
            "network": NETWORK,
            "netuid": NETUID,
            "stake_threshold": STAKE_THRESHOLD,
            "validators": validators,  # Keep name+hotkey pairs
            "hotkeys": sorted(hotkeys)  # Also include sorted list of just hotkeys
        }
        with open(_VALIDATOR_HOTKEYS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        logger.debug(f"Saved {len(validators)} validator hotkeys to {_VALIDATOR_HOTKEYS_FILE}")
    except Exception as e:
        logger.error(f"Failed to save validator hotkeys to file: {e}", exc_info=True)


def get_miner_hotkeys() -> List[str]:
    """
    Get list of whitelisted miner hotkeys.
    
    Fetches miner hotkeys directly from the metagraph using mg.hotkeys
    and caches the results for 2 minutes. The cache is automatically refreshed
    when it expires. Hotkeys are also saved to miner_hotkeys.json for inspection.
    Also includes manual miners for local testing.
    
    Returns:
        List of miner hotkey SS58 addresses
    """
    global _MINER_HOTKEYS_CACHE, _MINER_CACHE_TIMESTAMP
    
    with _MINER_CACHE_LOCK:
        current_time = time.time()
        # Check if cache is expired or empty
        if (not _MINER_HOTKEYS_CACHE or 
            current_time - _MINER_CACHE_TIMESTAMP >= _CACHE_DURATION_SECONDS):
            try:
                logger.info("Refreshing miner hotkeys cache from metagraph")
                sub = bt.Subtensor(network=NETWORK)
                mg = sub.metagraph(NETUID)
                # lite sync is fine; we don't need recency for miners
                mg.sync(subtensor=sub, lite=True)
                
                # Use mg.hotkeys to get the list of miner hotkeys
                hotkeys = mg.hotkeys
                # Filter out validators; we only want miners
                miner_hotkeys = [
                    hotkeys[uid] for uid in range(len(hotkeys))
                    if not bool(int(mg.validator_permit[uid]))
                ]
                
                _MINER_HOTKEYS_CACHE = miner_hotkeys
                _MINER_CACHE_TIMESTAMP = current_time
                logger.info(f"Cached {len(_MINER_HOTKEYS_CACHE)} miner hotkeys")
                
                # Save to file for inspection
                _save_miner_hotkeys_to_file(_MINER_HOTKEYS_CACHE, current_time)
            except Exception as e:
                logger.error(f"Failed to fetch miner hotkeys: {e}", exc_info=True)
                # If we have a stale cache, use it; otherwise return empty list
                if not _MINER_HOTKEYS_CACHE:
                    logger.warning("No cached miner hotkeys available, returning empty list")
                    return []
        
        # Combine metagraph miners with manual miners (only if ALLOW_MANUAL_HOTKEYS=true)
        metagraph_miners = _MINER_HOTKEYS_CACHE.copy()
        if ALLOW_MANUAL_HOTKEYS:
            all_miners = list(set(metagraph_miners + MANUAL_MINER_HOTKEYS))
        else:
            all_miners = metagraph_miners
        return all_miners


def get_validator_data() -> List[Dict[str, str]]:
    """
    Get list of validator data (name and hotkey pairs).
    
    Fetches validators from the metagraph (with validator permit and stake >= threshold)
    and caches the results for 2 minutes.
    
    Returns:
        List of dictionaries with "name" and "hotkey" keys
    """
    global _VALIDATOR_DATA_CACHE, _VALIDATOR_HOTKEYS_CACHE, _VALIDATOR_CACHE_TIMESTAMP
    
    with _VALIDATOR_CACHE_LOCK:
        current_time = time.time()
        # Check if cache is expired or empty
        if (not _VALIDATOR_DATA_CACHE or 
            current_time - _VALIDATOR_CACHE_TIMESTAMP >= _CACHE_DURATION_SECONDS):
            try:
                logger.info("Refreshing validator hotkeys cache from metagraph")
                sub = bt.Subtensor(network=NETWORK)
                mg = sub.metagraph(NETUID)
                # lite sync is fine; we don't need recency for validators
                mg.sync(subtensor=sub, lite=True)
                
                # Filter UIDs that have validator permit and stake >= threshold
                validator_hotkeys = [
                    hk for uid, hk in enumerate(mg.hotkeys)
                    if mg.validator_permit[uid] and mg.S[uid] >= STAKE_THRESHOLD
                ]
                
                # Populate both caches
                _VALIDATOR_HOTKEYS_CACHE = validator_hotkeys
                _VALIDATOR_DATA_CACHE = [
                    {"name": f"Validator {i+1}", "hotkey": hk}
                    for i, hk in enumerate(validator_hotkeys)
                ]
                _VALIDATOR_CACHE_TIMESTAMP = current_time
                logger.info(f"Cached {len(_VALIDATOR_DATA_CACHE)} validator hotkeys (stake >= {STAKE_THRESHOLD})")
                
                # Save to file for inspection
                _save_validator_hotkeys_to_file(_VALIDATOR_DATA_CACHE, current_time)
            except Exception as e:
                logger.error(f"Failed to fetch validator hotkeys: {e}", exc_info=True)
                # If we have a stale cache, use it; otherwise return empty list
                if not _VALIDATOR_DATA_CACHE:
                    logger.warning("No cached validator hotkeys available, returning empty list")
                    return []
        
        return _VALIDATOR_DATA_CACHE.copy()


def get_validator_hotkeys() -> List[str]:
    """
    Get list of whitelisted validator hotkeys.
    
    Fetches validators from the metagraph (with validator permit and stake >= threshold)
    and caches the results for 2 minutes.
    Also includes manual validators when ALLOW_MANUAL_HOTKEYS=true.
    
    Returns:
        List of validator hotkey SS58 addresses
    """
    global _VALIDATOR_HOTKEYS_CACHE
    
    # Ensure cache is populated by calling get_validator_data (which handles refresh)
    get_validator_data()
    
    # Combine metagraph validators with manual validators (only if ALLOW_MANUAL_HOTKEYS=true)
    metagraph_validators = _VALIDATOR_HOTKEYS_CACHE.copy()
    if ALLOW_MANUAL_HOTKEYS:
        all_validators = list(set(metagraph_validators + MANUAL_VALIDATOR_HOTKEYS))
    else:
        all_validators = metagraph_validators
    return all_validators


def get_all_whitelisted_hotkeys() -> List[str]:
    """
    Get combined list of all whitelisted hotkeys (miners + validators).
    
    Returns:
        List of all whitelisted hotkey SS58 addresses
    """
    miners = get_miner_hotkeys()
    validators = get_validator_hotkeys()
    # Use set to avoid duplicates, then convert back to list
    all_hotkeys = list(set(miners + validators))
    return all_hotkeys


def is_miner_hotkey(hotkey: str) -> bool:
    """Check if a hotkey is a miner hotkey"""
    return hotkey in get_miner_hotkeys()


def is_validator_hotkey(hotkey: str) -> bool:
    """Check if a hotkey is a validator hotkey"""
    return hotkey in get_validator_hotkeys()


def is_blacklisted(hotkey: str) -> bool:
    """
    Check if a hotkey is blacklisted based on prefix matching.
    
    Args:
        hotkey: Hotkey SS58 address to check
        
    Returns:
        True if the hotkey starts with any blacklisted prefix, False otherwise
    """
    return any(hotkey.startswith(prefix) for prefix in BLACKLISTED_HOTKEY_PREFIXES)


def get_allowed_miner_hotkeys() -> List[str]:
    """
    Get list of allowed miner hotkeys (whitelisted miners after removing blacklisted ones).
    
    Returns miners from get_miner_hotkeys() after filtering out any blacklisted hotkeys.
    
    Returns:
        List of allowed miner hotkey SS58 addresses
    """
    miners = get_miner_hotkeys()
    return [hotkey for hotkey in miners if not is_blacklisted(hotkey)]


def is_allowed_miner_hotkey(hotkey: str) -> bool:
    """
    Check if a hotkey is an allowed miner (whitelisted and not blacklisted).
    
    Args:
        hotkey: Hotkey SS58 address to check
        
    Returns:
        True if the hotkey is an allowed miner, False otherwise
    """
    return hotkey in get_allowed_miner_hotkeys()


def initialize_whitelists():
    """
    Initialize whitelist caches on startup.
    
    Pre-populates miner and validator hotkeys from metagraph
    so that the first request doesn't have to wait for loading.
    """
    logger.info("Initializing whitelist caches on startup...")
    
    # Initialize validator hotkeys (fetch from metagraph)
    # This will also save to validator_hotkeys.json automatically
    try:
        validators = get_validator_hotkeys()
        logger.info(f"Initialized {len(validators)} validator hotkeys on startup (stake >= {STAKE_THRESHOLD}, saved to {_VALIDATOR_HOTKEYS_FILE})")
    except Exception as e:
        logger.error(f"Failed to initialize validator hotkeys on startup: {e}", exc_info=True)
    
    # Initialize miner hotkeys (fetch from metagraph)
    # This will also save to miner_hotkeys.json automatically
    try:
        miners = get_miner_hotkeys()
        logger.info(f"Initialized {len(miners)} miner hotkeys on startup (saved to {_MINER_HOTKEYS_FILE})")
    except Exception as e:
        logger.error(f"Failed to initialize miner hotkeys on startup: {e}", exc_info=True)
        logger.warning("Miner hotkeys will be loaded on first request")
