#!/usr/bin/env python3

"""
Authentication utilities for API using Bittensor wallet signatures.
"""

import os

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
    load_dotenv(dotenv_path)
except ImportError:
    # dotenv not available, rely on system environment variables
    pass
import time
import logging
from typing import Optional, Dict, Any, List

import bittensor as bt
try:
    from bittensor_wallet import Keypair
except ImportError:
    # Fallback: bittensor_wallet might be part of bittensor package
    try:
        from bittensor.wallet import Keypair
    except ImportError:
        # If still not available, we'll handle it in verify_signature
        Keypair = None

from fastapi import Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Import live whitelist functions that fetch from metagraph
try:
    from hotkey_whitelist import get_all_whitelisted_hotkeys
except ImportError:
    logger.error("Failed to import hotkey_whitelist module")
    get_all_whitelisted_hotkeys = None

def get_cached_whitelisted_hotkeys() -> List[str]:
    """
    Get whitelisted hotkeys from metagraph (with 2-minute caching in hotkey_whitelist).
    
    This function delegates to hotkey_whitelist.get_all_whitelisted_hotkeys() which
    already has 2-minute caching for both miner and validator hotkeys. No additional
    caching layer is needed here since the underlying data is already cached.
    
    Note: JSON files are updated by hotkey_whitelist.py for inspection/debugging,
    but are NOT used for authentication. Only live metagraph data is used.
    
    Returns:
        List of whitelisted hotkey SS58 addresses
    """
    if not get_all_whitelisted_hotkeys:
        logger.error("hotkey_whitelist module not available. Cannot authenticate without metagraph access.")
        return []
    
    try:
        # Get hotkeys from live metagraph (2-minute cache handled by hotkey_whitelist.py)
        # This also updates the JSON files automatically via hotkey_whitelist.py
        return get_all_whitelisted_hotkeys()
    except Exception as e:
        logger.error(f"Failed to get whitelisted hotkeys from metagraph: {e}")
        return []

class AuthRequest(BaseModel):
    """Authentication request model"""
    ss58_address: str
    signature: str
    message: str
    timestamp: float

class AuthConfig:
    """Authentication configuration"""
    def __init__(self):
        self.enabled = os.getenv("AUTH_ENABLED", "true").lower() == "true"
        # Keep a snapshot for diagnostics; actual checks always go through
        # the cached metagraph whitelist for up‑to‑date data.
        self.allowed_hotkeys = self._parse_allowed_hotkeys()
        self.signature_timeout = int(os.getenv("AUTH_SIGNATURE_TIMEOUT", "300"))  # 5 minutes
        
    def _parse_allowed_hotkeys(self) -> List[str]:
        """Parse allowed hotkeys from environment variable and cached metagraph whitelist"""
        # First, get hotkeys from environment variable (for manual override)
        hotkeys_str = os.getenv("ALLOWED_HOTKEYS", "")
        env_hotkeys = []
        if hotkeys_str:
            env_hotkeys = [key.strip() for key in hotkeys_str.split(",") if key.strip()]
            logger.info(f"Loaded {len(env_hotkeys)} hotkeys from ALLOWED_HOTKEYS env var")
        
        # Then, get hotkeys from cached whitelist (2-minute cache, refreshed from metagraph)
        whitelist_hotkeys = get_cached_whitelisted_hotkeys()
        logger.info(f"Loaded {len(whitelist_hotkeys)} hotkeys from cached metagraph whitelist")
        
        # Combine both sources (use set to avoid duplicates)
        all_hotkeys = list(set(env_hotkeys + whitelist_hotkeys))
        logger.info(f"Total {len(all_hotkeys)} allowed hotkeys for authentication")
        
        if not all_hotkeys:
            logger.warning("No allowed hotkeys configured. Authentication will reject all requests.")
        
        return all_hotkeys
    
    def is_hotkey_allowed(self, hotkey: str) -> bool:
        """
        Check if a hotkey is in the allowed list.
        
        This method always consults the cached metagraph whitelist (via
        hotkey_whitelist.get_all_whitelisted_hotkeys) so that new miners
        and validators are picked up automatically without restarting the
        API process. Environment overrides from ALLOWED_HOTKEYS are also
        applied on every check.
        """
        # Rebuild allowed_hotkeys from env + cached metagraph on each check.
        # hotkey_whitelist itself maintains a 2‑minute cache, so this does
        # not hit the chain on every request.
        current_allowed = self._parse_allowed_hotkeys()
        self.allowed_hotkeys = current_allowed
        
        is_allowed = hotkey in current_allowed
        
        if not is_allowed:
            logger.warning(
                f"Hotkey {hotkey} not found in whitelist. "
                f"Whitelist contains {len(current_allowed)} hotkeys."
            )
        
        return is_allowed
    
    def refresh_whitelist(self):
        """Refresh the whitelist from metagraph (call this periodically)"""
        # Refresh the allowed_hotkeys list
        # Note: The underlying hotkey_whitelist caches will auto-refresh when expired
        self.allowed_hotkeys = self._parse_allowed_hotkeys()

def create_auth_message(timestamp: Optional[float] = None) -> str:
    """Create a standardized authentication message"""
    if timestamp is None:
        timestamp = time.time()
    return f"talisman-ai-auth:{int(timestamp)}"

def sign_message(wallet: bt.wallet, message: str) -> str:
    """Sign a message with the wallet's hotkey"""
    signature = wallet.hotkey.sign(message)
    return signature.hex()

def verify_signature(hotkey: str, signature_hex: str, message: str) -> bool:
    """Verify a signature against a hotkey and message"""
    if Keypair is None:
        logger.error("Keypair class not available. Cannot verify signatures.")
        return False
    
    try:
        # Create keypair from hotkey address
        keypair = Keypair(ss58_address=hotkey)
        
        # Convert signature from hex
        signature = bytes.fromhex(signature_hex)
        
        # Convert message to bytes
        message_bytes = bytes(message, 'utf-8')
        
        # Verify signature
        is_valid = keypair.verify(message_bytes, signature)
        return is_valid
        
    except Exception as e:
        logger.error(f"Error verifying signature: {e}")
        return False

def verify_auth_request(auth_request: AuthRequest, auth_config: AuthConfig) -> bool:
    """Verify an authentication request"""
    try:
        # Check if authentication is enabled
        if not auth_config.enabled:
            logger.debug("Authentication disabled, allowing request")
            return True
        
        # Check if hotkey is allowed
        if not auth_config.is_hotkey_allowed(auth_request.ss58_address):
            logger.warning(f"ss58_address {auth_request.ss58_address} not in allowed list")
            return False
        
        # Check timestamp (prevent replay attacks)
        current_time = time.time()
        time_diff = abs(current_time - auth_request.timestamp)
        
        if time_diff > auth_config.signature_timeout:
            logger.warning(f"Authentication request timestamp too old: {time_diff}s > {auth_config.signature_timeout}s")
            return False
        
        # Verify signature
        is_valid = verify_signature(
            auth_request.ss58_address,
            auth_request.signature,
            auth_request.message
        )
        
        if not is_valid:
            logger.warning(f"Invalid signature for ss58_address {auth_request.ss58_address}")
            return False
        
        # Verify message format (should contain timestamp)
        expected_message = create_auth_message(auth_request.timestamp)
        if auth_request.message != expected_message:
            logger.warning(f"Invalid message format. Expected: {expected_message}, Got: {auth_request.message}")
            return False
        
        logger.debug(f"Authentication successful for ss58_address {auth_request.ss58_address}")
        return True
        
    except Exception as e:
        logger.error(f"Error during authentication verification: {e}")
        return False

class AuthenticatedClient:
    """Client class for making authenticated requests"""
    
    def __init__(self, wallet: bt.wallet):
        self.wallet = wallet
        self.ss58_address = wallet.hotkey.ss58_address
        
    def create_auth_headers(self) -> Dict[str, str]:
        """Create authentication headers for HTTP requests"""
        timestamp = time.time()
        message = create_auth_message(timestamp)
        signature = sign_message(self.wallet, message)
        
        return {
            "X-Auth-SS58Address": self.ss58_address,
            "X-Auth-Signature": signature,
            "X-Auth-Message": message,
            "X-Auth-Timestamp": str(timestamp)
        }
    
    def create_auth_data(self) -> Dict[str, Any]:
        """Create authentication data for websocket or JSON payloads"""
        timestamp = time.time()
        message = create_auth_message(timestamp)
        signature = sign_message(self.wallet, message)
        
        return {
            "auth": {
                "ss58_address": self.ss58_address,
                "signature": signature,
                "message": message,
                "timestamp": timestamp
            }
        }

def extract_auth_from_headers(request: Request) -> Optional[AuthRequest]:
    """Extract authentication data from HTTP headers"""
    try:
        ss58_address = request.headers.get("X-Auth-SS58Address")
        signature = request.headers.get("X-Auth-Signature")
        message = request.headers.get("X-Auth-Message")
        timestamp_str = request.headers.get("X-Auth-Timestamp")
        
        if not all([ss58_address, signature, message, timestamp_str]):
            return None
        
        timestamp = float(timestamp_str)
        
        return AuthRequest(
            ss58_address=ss58_address,
            signature=signature,
            message=message,
            timestamp=timestamp
        )
        
    except Exception as e:
        logger.error(f"Error extracting auth from headers: {e}")
        return None

# Global auth config instance
auth_config = AuthConfig()

