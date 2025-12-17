"""
Shared validation utilities for normalization and metric tolerance logic.

This module provides shared implementations of normalization and metric validation
functions used across the API layer. These implementations must match the behavior
in talisman_ai_subnet/talisman_ai/utils/normalization.py to ensure consistent
normalization across miner, validator, and API layers.
"""

import unicodedata
import re
import math

# Constants
POST_METRIC_TOLERANCE = 0.1  # 10% relative (with a floor of 1) for overstatement checks


def norm_text(s: str) -> str:
    """
    Normalize text for comparison to handle encoding differences, line endings, and whitespace.
    
    This ensures that minor formatting differences don't cause false mismatches:
    - Unicode normalization (NFC) handles different encodings of the same characters
    - Converts all line endings to \n
    - Collapses multiple whitespace characters to single spaces
    - Trims leading/trailing whitespace
    
    This implementation must match talisman.utils.normalization.norm_text
    to ensure consistent normalization across all layers.
    
    Args:
        s: Raw text string
        
    Returns:
        Normalized text string ready for comparison
    """
    s = unicodedata.normalize("NFC", s or "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def norm_author(s: str) -> str:
    """
    Normalize author username: lowercase and strip whitespace.
    
    This implementation must match talisman.utils.normalization.norm_author
    to ensure consistent normalization across all layers.
    
    Args:
        s: Raw author username string
        
    Returns:
        Normalized author username (lowercase, stripped)
    """
    return (s or "").strip().lower()


def metric_tol(live: int) -> int:
    """
    Tolerance for likes/retweets/replies/followers overstatement: max(1, ceil(10% of live)).
    
    Args:
        live: The live/actual metric value
        
    Returns:
        Tolerance value (minimum 1, or 10% of live rounded up)
    """
    return 1 if live == 0 else max(1, math.ceil(live * POST_METRIC_TOLERANCE))


def metric_inflated(miner: int, live: int) -> bool:
    """
    Check if miner-reported metric is inflated beyond tolerance.
    
    True if miner value > live + tolerance. Understatement is allowed.
    
    Args:
        miner: The metric value reported by the miner
        live: The live/actual metric value
        
    Returns:
        True if miner value exceeds live value plus tolerance, False otherwise
    """
    return miner > live + metric_tol(live)

