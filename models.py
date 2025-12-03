"""
Pydantic models for API v2 requests and responses.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Dict, Optional, List
from dateutil.parser import isoparse
from validation_utils import norm_text, norm_author


def parse_unix(ts) -> Optional[int]:
    """
    Parse timestamp from various formats into unix seconds.
    
    Accepts:
    - ISO-8601 strings (e.g., "2024-01-01T12:00:00Z")
    - Unix timestamps as int or float (seconds)
    - Unix timestamps in milliseconds (values > 1e12 are auto-converted)
    
    Returns:
        Unix timestamp in seconds, or None if parsing fails
    """
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        v = float(ts)
        if v > 1e12:  # Likely milliseconds
            v /= 1000.0
        return int(v)
    if isinstance(ts, str):
        try:
            return int(isoparse(ts).timestamp())
        except Exception:
            return None
    return None


class PostSubmission(BaseModel):
    """
    Submission model from miners (same as v1).
    
    Data normalization happens automatically via field validators:
    - content: Normalized text (NFC unicode, normalized whitespace)
    - date: Parsed to unix seconds (handles ISO strings, milliseconds, etc.)
    - author: Normalized (lowercase, stripped)
    
    This ensures the validator receives clean, consistent data.
    """
    miner_hotkey: str = Field(..., description="Bittensor miner hotkey (string id)")
    post_id: str
    content: str
    date: int = Field(..., gt=0, description="Unix timestamp in seconds (must be positive, valid timestamp)")
    author: str
    account_age: int
    retweets: int
    likes: int
    responses: int
    followers: int = Field(default=0, description="Author's follower count")
    tokens: Dict[str, float]  # e.g., {"omron": 0.7, "data_universe": 0.4, "bounty_hunter": 0.0} - relevance scores 0.0 to 1.0
    sentiment: float = Field(..., ge=-1.0, le=1.0, description="Sentiment score from -1.0 (very bearish) to 1.0 (very bullish)")
    score: float = Field(..., ge=0.0, le=1.0, description="Post score from miner (0.0 to 1.0) - calculated by miner using score_post_entry")

    @field_validator('content', mode='before')
    @classmethod
    def normalize_content(cls, v) -> str:
        """Normalize text content for consistent comparison."""
        if not isinstance(v, str):
            raise ValueError("content must be a string")
        normalized = norm_text(v)
        if not normalized:
            raise ValueError("content cannot be empty after normalization")
        return normalized

    @field_validator('date', mode='before')
    @classmethod
    def parse_date(cls, v) -> int:
        """
        Parse date from various formats (ISO string, unix seconds, milliseconds) 
        into unix seconds.
        """
        parsed = parse_unix(v)
        if parsed is None:
            raise ValueError(f"date must be a valid timestamp (got {type(v).__name__}: {v})")
        if parsed <= 0:
            raise ValueError(f"date must be positive (got {parsed})")
        return parsed

    @field_validator('author', mode='before')
    @classmethod
    def normalize_author(cls, v) -> str:
        """Normalize author username (lowercase, strip)."""
        if not isinstance(v, str):
            raise ValueError("author must be a string")
        normalized = norm_author(v)
        if not normalized:
            raise ValueError("author cannot be empty after normalization")
        return normalized

    @field_validator('tokens')
    @classmethod
    def validate_token_values(cls, v: Dict[str, float]) -> Dict[str, float]:
        """Validate that all token relevance scores are in [0.0, 1.0] range."""
        if not isinstance(v, dict):
            raise ValueError("tokens must be a dictionary")
        if not v:
            raise ValueError("tokens cannot be empty - post must have relevance to at least one subnet to be accepted")
        for key, value in v.items():
            if not isinstance(value, (int, float)):
                raise ValueError(f"Token value for '{key}' must be a number, got {type(value).__name__}")
            if not (0.0 <= float(value) <= 1.0):
                raise ValueError(f"Token value for '{key}' must be between 0.0 and 1.0, got {value}")
        # Check that at least one token has relevance > 0.0
        max_relevance = max(float(value) for value in v.values())
        if max_relevance <= 0.0:
            raise ValueError("Post must have relevance to at least one subnet (at least one token value must be > 0.0)")
        return v


# ---- Validation Payload (v2): individual post validation ----
class ValidationPayload(BaseModel):
    """Payload returned to validators when they GET a validation request."""
    validation_id: str  # Unique ID for this validation request
    miner_hotkey: str
    post: Dict  # The post to validate (all fields from PostSubmission)
    selected_at: int  # Unix timestamp when this post was selected for validation


class ValidationPayloadsResponse(BaseModel):
    """Response containing multiple validation payloads."""
    available: bool
    payloads: List[ValidationPayload]  # One per hotkey (all available hotkeys)
    count: int  # Number of payloads returned


class ValidationResult(BaseModel):
    """Result submitted by validators after validating a post."""
    validator_hotkey: str
    validation_id: str  # The validation_id from the ValidationPayload
    miner_hotkey: str
    success: bool  # True if validation passed, False if validation failed
    failure_reason: Optional[Dict] = None  # Failure reason if success=False


class ValidationResultsPayload(BaseModel):
    """Payload containing multiple validation results."""
    validator_hotkey: str
    results: List[ValidationResult]  # Multiple validation results (one per payload received)

