"""
Pydantic models for Alpharidge AI API.

These models correspond to the Prisma schema and are used for
request/response validation and serialization.
"""

from datetime import datetime
from typing import Optional, List, Any, Dict
from pydantic import BaseModel, Field


# ============================================================================
# Account Models (Twitter/X user accounts)
# ============================================================================

class AccountBase(BaseModel):
    """Base account model with common fields."""
    id: int  # BigInt in Prisma
    name: Optional[str] = None
    screen_name: str = Field(alias="screenName")
    user_name: Optional[str] = Field(None, alias="userName")
    location: Optional[str] = None
    description: Optional[str] = None
    verified: bool = False
    is_blue_verified: bool = Field(False, alias="isBlueVerified")
    followers_count: int = Field(0, alias="followersCount")
    following_count: int = Field(0, alias="followingCount")
    statuses_count: int = Field(0, alias="statusesCount")
    profile_image_url: Optional[str] = Field(None, alias="profileImageUrl")
    
    class Config:
        populate_by_name = True


class AccountCreate(BaseModel):
    """Model for creating a new account."""
    id: int
    screen_name: str
    name: Optional[str] = None
    user_name: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    verified: bool = False
    is_blue_verified: bool = False
    followers_count: int = 0
    following_count: int = 0
    statuses_count: int = 0
    profile_image_url: Optional[str] = None


class Account(AccountBase):
    """Full account model for responses."""
    created_at: Optional[datetime] = Field(None, alias="createdAt")
    
    class Config:
        populate_by_name = True


# ============================================================================
# Tweet Analysis Models (Sentiment/classification - separate from raw tweet)
# ============================================================================

class TweetAnalysisBase(BaseModel):
    """Base tweet analysis model."""
    sentiment: Optional[str] = None  # very_bullish, bullish, neutral, bearish, very_bearish
    asset_id: Optional[int] = Field(None, alias="assetId")
    asset_symbol: Optional[str] = Field(None, alias="assetSymbol")
    content_type: Optional[str] = Field(None, alias="contentType")
    
    class Config:
        populate_by_name = True


class TweetAnalysisCreate(BaseModel):
    """Model for creating tweet analysis."""
    tweet_id: int
    sentiment: Optional[str] = None
    asset_id: Optional[int] = None
    asset_symbol: Optional[str] = None
    content_type: Optional[str] = None
    analysis_data: Optional[dict] = None


class TweetAnalysis(TweetAnalysisBase):
    """Full tweet analysis model for responses."""
    id: int
    tweet_id: int = Field(alias="tweetId")
    analyzed_at: datetime = Field(alias="analyzedAt")
    
    class Config:
        populate_by_name = True


# ============================================================================
# Tweet Models
# ============================================================================

class TweetBase(BaseModel):
    """Base tweet model with common fields."""
    id: int  # BigInt in Prisma
    type: str = "tweet"
    url: Optional[str] = None
    text: Optional[str] = None
    lang: Optional[str] = None
    
    # Engagement metrics
    retweet_count: int = Field(0, alias="retweetCount")
    reply_count: int = Field(0, alias="replyCount")
    like_count: int = Field(0, alias="likeCount")
    quote_count: int = Field(0, alias="quoteCount")
    view_count: int = Field(0, alias="viewCount")
    bookmark_count: int = Field(0, alias="bookmarkCount")
    
    # Reply/conversation info
    is_reply: bool = Field(False, alias="isReply")
    in_reply_to_id: Optional[int] = Field(None, alias="inReplyToId")
    conversation_id: Optional[int] = Field(None, alias="conversationId")
    
    # Author
    author_id: Optional[int] = Field(None, alias="authorId")
    
    # Timestamps
    created_at: Optional[datetime] = Field(None, alias="createdAt")
    received_at: datetime = Field(alias="receivedAt")
    
    class Config:
        populate_by_name = True


class TweetCreate(BaseModel):
    """Model for creating a new tweet."""
    id: int
    type: str = "tweet"
    url: Optional[str] = None
    text: Optional[str] = None
    lang: Optional[str] = None
    author_id: Optional[int] = None
    created_at: Optional[datetime] = None
    retweet_count: int = 0
    reply_count: int = 0
    like_count: int = 0
    quote_count: int = 0
    view_count: int = 0
    bookmark_count: int = 0
    is_reply: bool = False
    in_reply_to_id: Optional[int] = None
    conversation_id: Optional[int] = None


class Tweet(TweetBase):
    """Full tweet model for responses."""
    pass


class TweetWithAuthor(Tweet):
    """Tweet model with nested author (account) information."""
    author: Optional[Account] = None
    analysis: Optional[TweetAnalysis] = None


# ============================================================================
# Scoring Models
# ============================================================================

class ScoringBase(BaseModel):
    """Base scoring model with common fields."""
    id: int
    tweet_id: int = Field(alias="tweetId")
    status: str = "pending"  # pending, in_progress, completed
    
    class Config:
        populate_by_name = True


class ScoringCreate(BaseModel):
    """Model for creating a scoring entry."""
    tweet_id: int
    status: str = "pending"
    validator_hotkey: Optional[str] = None


class ScoringUpdate(BaseModel):
    """Model for updating scoring status."""
    status: str
    validator_hotkey: Optional[str] = None


class Scoring(ScoringBase):
    """Full scoring model for responses."""
    start_time: Optional[datetime] = Field(None, alias="startTime")
    validator_hotkey: Optional[str] = Field(None, alias="validatorHotkey")
    score: Optional[float] = None
    created_at: datetime = Field(alias="createdAt")
    
    class Config:
        populate_by_name = True


class ScoringWithTweet(Scoring):
    """Scoring model with nested tweet information."""
    tweet: TweetWithAuthor


# ============================================================================
# Penalty Models
# ============================================================================

class PenaltyBase(BaseModel):
    """Base penalty model with common fields."""
    hotkey: str
    reason: str  # Required in Prisma schema
    
    class Config:
        populate_by_name = True


class PenaltyCreate(BaseModel):
    """Model for creating a penalty."""
    hotkey: str
    reason: str  # Required in Prisma schema


class Penalty(PenaltyBase):
    """Full penalty model for responses."""
    id: int
    timestamp: datetime


class PenaltyBulkCreate(BaseModel):
    """Model for creating multiple penalties at once."""
    penalties: List[PenaltyCreate]


# ============================================================================
# Reward Models
# ============================================================================

class RewardBase(BaseModel):
    """Base reward model with common fields."""
    start_block: int = Field(alias="startBlock")
    stop_block: int = Field(alias="stopBlock")
    hotkey: str
    points: float
    
    class Config:
        populate_by_name = True


class RewardCreate(BaseModel):
    """Model for creating a reward."""
    start_block: int
    stop_block: int
    hotkey: str
    points: float


class Reward(RewardBase):
    """Full reward model for responses."""
    id: int
    created_at: datetime = Field(alias="createdAt")
    
    class Config:
        populate_by_name = True


class RewardBulkCreate(BaseModel):
    """Model for creating multiple rewards at once."""
    rewards: List[RewardCreate]


# ============================================================================
# Blacklisted Hotkey Models
# ============================================================================

class BlacklistedHotkeyBase(BaseModel):
    """Base blacklisted hotkey model."""
    hotkey: str
    reason: Optional[str] = None


class BlacklistedHotkeyCreate(BaseModel):
    """Model for creating a blacklisted hotkey."""
    hotkey: str
    reason: Optional[str] = None


class BlacklistedHotkey(BlacklistedHotkeyBase):
    """Full blacklisted hotkey model for responses."""
    created_at: datetime = Field(alias="createdAt")
    
    class Config:
        populate_by_name = True


class BlacklistedHotkeyBulkCreate(BaseModel):
    """Model for creating multiple blacklisted hotkeys at once."""
    hotkeys: List[str]
    reason: Optional[str] = None


# ============================================================================
# Response Models
# ============================================================================

class TweetsForScoringResponse(BaseModel):
    """Response model for getting tweets for scoring."""
    tweets: List[TweetWithAuthor]
    count: int


class CompletedTweetSubmission(BaseModel):
    """Model for submitting a completed scored tweet."""
    tweet_id: int
    sentiment: str
    asset_id: Optional[int] = None
    asset_symbol: Optional[str] = None
    content_type: Optional[str] = None
    technical_quality: Optional[str] = None
    market_analysis: Optional[str] = None
    impact_potential: Optional[str] = None
    relevance_confidence: Optional[str] = None
    # --- verifiable-points verdict fields (optional during migration) ---
    epoch: Optional[int] = None
    miner_hotkey: Optional[str] = None
    miner_signature: Optional[str] = None
    nonce: Optional[str] = None
    miner_analysis_hash: Optional[str] = None
    validator_verdict: Optional[str] = None        # "valid" | "invalid"
    categorical_key: Optional[str] = None
    points_awarded: Optional[float] = None


class CompletedTweetsSubmission(BaseModel):
    """Model for submitting multiple completed scored tweets."""
    completed_tweets: List[CompletedTweetSubmission]


class SubmissionResponse(BaseModel):
    """Generic response for submission endpoints."""
    success: bool
    message: str
    count: int = 0


class ErrorResponse(BaseModel):
    """Error response model."""
    detail: str


class AttestationResponse(BaseModel):
    """Signed per-(validator, epoch) attestation returned by GET /attestation."""
    validator_hotkey: str
    epoch: int
    per_miner_points: Dict[str, float]
    total_points: float
    merkle_root: str
    signature: str


class VerdictLeaf(BaseModel):
    """A single verdict leaf for recompute via GET /verdicts."""
    resource_type: str
    resource_id: str
    miner_hotkey: str
    validator_verdict: str
    categorical_key: str
    points_awarded: float


class VerdictsResponse(BaseModel):
    validator_hotkey: str
    epoch: int
    verdicts: List[VerdictLeaf]
    count: int


class BroadcastReportCreate(BaseModel):
    accused_hotkey: str
    epoch: int
    reason: str                 # bad_signature | budget_exceeded | attribution_mismatch | content_divergence
    evidence: Dict[str, Any] = {}


# ============================================================================
# Penalty Detail Models (display-only attribution for the miner dashboard)
# ============================================================================
# DECOUPLED from the consensus pipeline by design: these carry no signatures,
# hashes, or points and are persisted to the standalone `penalty_detail` table
# only. Never folded into score_verdict / attestation / Merkle.

class PenaltyDetailItem(BaseModel):
    """One penalty-attribution row from the validator, explaining why a single
    sampled/timed-out item was penalized. Display-only."""
    miner_hotkey: str
    epoch: int
    resource_type: str
    resource_id: str
    cause: str                  # classification_mismatch | timeout | missing_classification | needs_update
    failed_fields: Optional[List[str]] = None
    miner_values: Optional[Dict[str, Any]] = None
    validator_values: Optional[Dict[str, Any]] = None
    post_preview: Optional[str] = None
    score: Optional[float] = None   # numeric behind the rejection (composite vs 0.65, cosine vs 0.40)


class PenaltyDetailBulkCreate(BaseModel):
    """Batch of penalty-detail rows flushed best-effort by the validator."""
    items: List[PenaltyDetailItem]


class DispatchStatusItem(BaseModel):
    """Per-miner adaptive-dispatch status as seen by one validator. Display-only,
    decoupled from consensus — explanatory data for the miner dashboard."""
    hotkey: str
    uid: int
    alive: bool
    window: float
    inflight: int
    consec_to: int
    batch_size: Optional[int] = None
    covered_epoch: int
    on_cooldown: bool
    cooldown_remaining_s: int
    # New telemetry (default None so a pre-upgrade validator still validates):
    consec_inv: Optional[int] = None            # consecutive low-faithfulness batches (X/N to faith park)
    consec_fail: Optional[int] = None           # consecutive validation-fails (X/N to failstreak park)
    inv_level: Optional[int] = None             # cooldown escalation level (1 -> 60s, 2 -> 600s)
    last_faith: Optional[float] = None          # last observed min-faithfulness (vs the 0.5 floor)
    emission_mult: Optional[float] = None        # reputation emission multiplier (~0 below cliff, ~1, up to ~1.3)
    median_latency_s: Optional[float] = None     # median per-article return time (vs the on-time threshold)


class DispatchStatusBulkCreate(BaseModel):
    """Snapshot of per-miner adaptive-dispatch status flushed best-effort by a validator."""
    miners: List[DispatchStatusItem]


class MinerEventItem(BaseModel):
    """One per-miner dispatch/cooldown event from a validator (display-only). Records a
    discrete transition (park/unpark/batch-size change/reward-zeroed) with its own
    occurred_at so buffered events keep accurate timing regardless of flush cadence."""
    miner_hotkey: str
    event_type: str                             # parked | unparked | batch_size_changed | reward_zeroed | chronic_timeout
    occurred_at: datetime
    streak: Optional[str] = None                # faith | failstreak | timeout (for a park)
    shadow: Optional[bool] = None               # park emitted in shadow mode (logged, not enforced)
    epoch: Optional[int] = None
    reason: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None     # numerics: from/to, last_faith, cooldown_s, level, points, penalties


class MinerEventBulkCreate(BaseModel):
    """Batch of miner events flushed best-effort by a validator."""
    events: List[MinerEventItem]


# ============================================================================
# TAO Price Models
# ============================================================================

class TaoPriceResponse(BaseModel):
    """Response model for TAO/USD price endpoint."""
    price_usd: float
    last_updated: datetime
    source: str
    stale: bool


# ============================================================================
# Axon Check Models
# ============================================================================

class AxonCheckRequest(BaseModel):
    """Request model for axon reachability check."""
    ip: str
    port: int


class AxonCheckResponse(BaseModel):
    """Response model for axon reachability check."""
    reachable: bool
    error: Optional[str] = None


class ReputationSnapshotRow(BaseModel):
    """One per-hotkey reputation row (display/monitoring only)."""
    miner_hotkey: str
    reputation: float
    samples: int = 0
    gate: Optional[float] = None
    projected_share: Optional[float] = None


class ReputationSnapshotRequest(BaseModel):
    """A validator's reputation snapshot for one epoch."""
    epoch: int
    snapshots: List[ReputationSnapshotRow]


# ============================================================================
# Telegram Models
# ============================================================================

class TelegramGroup(BaseModel):
    """Telegram group model."""
    id: str
    telegram_id: int = Field(alias="telegramId")
    title: str
    is_monitored: bool = Field(False, alias="isMonitored")
    is_muted: bool = Field(False, alias="isMuted")
    muted_until: Optional[str] = Field(None, alias="mutedUntil")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")

    class Config:
        populate_by_name = True


class TelegramMessageAnalysis(BaseModel):
    """Telegram message analysis model."""
    id: int
    message_id: str = Field(alias="messageId")
    sentiment: Optional[str] = None
    asset_id: Optional[int] = Field(None, alias="assetId")
    asset_symbol: Optional[str] = Field(None, alias="assetSymbol")
    content_type: Optional[str] = Field(None, alias="contentType")
    technical_quality: Optional[str] = Field(None, alias="technicalQuality")
    market_analysis: Optional[str] = Field(None, alias="marketAnalysis")
    impact_potential: Optional[str] = Field(None, alias="impactPotential")
    relevance_confidence: Optional[str] = Field(None, alias="relevanceConfidence")
    analyzed_at: str = Field(alias="analyzedAt")

    class Config:
        populate_by_name = True


class TelegramMessage(BaseModel):
    """Telegram message model."""
    id: str
    telegram_id: int = Field(alias="telegramId")
    group_id: str = Field(alias="groupId")
    sender_id: int = Field(alias="senderId")
    sender_username: Optional[str] = Field(None, alias="senderUsername")
    sender_name: str = Field(alias="senderName")
    content: str
    reply_to_id: Optional[int] = Field(None, alias="replyToId")
    created_at: str = Field(alias="createdAt")

    class Config:
        populate_by_name = True


class TelegramMessageWithContext(TelegramMessage):
    """Telegram message model with group and analysis context."""
    group: Optional[TelegramGroup] = None
    analysis: Optional[TelegramMessageAnalysis] = None


class TelegramMessageForScoring(TelegramMessageWithContext):
    """
    Telegram message with context messages for scoring.
    
    Contains the main message plus context from:
    - The message being replied to (if any) with its classification
    - Previous messages in the conversation for context
    """
    context_messages: List["TelegramMessageWithContext"] = Field(
        default_factory=list, alias="contextMessages"
    )
    inherited_asset_id: Optional[int] = Field(None, alias="inheritedAssetId")
    inherited_asset_symbol: Optional[str] = Field(None, alias="inheritedAssetSymbol")

    class Config:
        populate_by_name = True


class TelegramMessagesForScoringResponse(BaseModel):
    """Response model for getting telegram messages for scoring."""
    messages: List[TelegramMessageForScoring]
    count: int


class CompletedTelegramMessageSubmission(BaseModel):
    """Model for submitting a completed scored telegram message."""
    message_id: str
    sentiment: str
    asset_id: Optional[int] = None
    asset_symbol: Optional[str] = None
    content_type: Optional[str] = None
    technical_quality: Optional[str] = None
    market_analysis: Optional[str] = None
    impact_potential: Optional[str] = None
    relevance_confidence: Optional[str] = None
    # --- verifiable-points verdict fields (optional during migration) ---
    epoch: Optional[int] = None
    miner_hotkey: Optional[str] = None
    miner_signature: Optional[str] = None
    nonce: Optional[str] = None
    miner_analysis_hash: Optional[str] = None
    validator_verdict: Optional[str] = None        # "valid" | "invalid"
    categorical_key: Optional[str] = None
    points_awarded: Optional[float] = None


class CompletedTelegramMessagesSubmission(BaseModel):
    """Model for submitting multiple completed scored telegram messages."""
    completed_messages: List[CompletedTelegramMessageSubmission]


# ============================================================================
# News Article Models
# ============================================================================

class NewsArticleForScoring(BaseModel):
    """News article model for scoring responses."""
    id: int
    url: str
    title: str
    summary: Optional[str] = None
    content: Optional[str] = None
    # Raw HTML, served only when SERVE_RAW_HTML is enabled (off by default to
    # avoid synapse/bandwidth bloat). When present, miners/validators may run
    # trafilatura on the real DOM for analyzer-side re-extraction.
    raw_html: Optional[str] = None
    published: Optional[str] = None
    source: str
    topic: Optional[str] = None
    extra: Optional[Any] = None

    class Config:
        populate_by_name = True


class NewsArticlesForScoringResponse(BaseModel):
    """Response model for getting news articles for scoring."""
    articles: List[NewsArticleForScoring]
    count: int


class CompletedNewsArticleSubmission(BaseModel):
    """Model for submitting a completed scored news article."""
    article_id: int
    sentiment: str
    sector_id: Optional[int] = None
    sector_symbol: Optional[str] = None
    content_type: Optional[str] = None
    technical_quality: Optional[str] = None
    market_analysis: Optional[str] = None
    impact_potential: Optional[str] = None
    relevance_confidence: Optional[str] = None
    # --- verifiable-points verdict fields (optional during migration) ---
    epoch: Optional[int] = None
    miner_hotkey: Optional[str] = None
    miner_signature: Optional[str] = None
    nonce: Optional[str] = None
    miner_analysis_hash: Optional[str] = None
    validator_verdict: Optional[str] = None        # "valid" | "invalid"
    categorical_key: Optional[str] = None
    points_awarded: Optional[float] = None
    # V2: full ArticleIntelligence as JSONB (stored in news_article_analysis.analysisData)
    analysis_data: Optional[dict] = None


class CompletedNewsArticlesSubmission(BaseModel):
    """Model for submitting multiple completed scored news articles."""
    completed_articles: List[CompletedNewsArticleSubmission]


# ---- Mechanism profile ------------------------------------------------------------

class MechanismProfileResponse(BaseModel):
    """The profile in force and the one staged behind it, each signed.

    A validator resolves between them by block height, so both are served together and
    neither is described as active here.
    """
    current: Optional[dict] = None
    next: Optional[dict] = None


class MechanismAnchorResponse(BaseModel):
    """Why burn is what it is: capacity, what the crawl supplies, and the ratio.

    Published rather than enforced. Capacity moves only by a profile publish.
    """
    capacity: Optional[float] = None
    capacity_anchor: Optional[float] = None
    anchor_ratio: Optional[float] = None
    usable_ingest_per_epoch: Optional[float] = None
    pts_per_article: Optional[float] = None
    roi_ema: Optional[float] = None
    last_step: Optional[dict] = None


class ControllerProposalSubmission(BaseModel):
    """A validator's observation that the controller's step rule has armed.

    Informational. Capacity changes when an operator publishes a profile, never here.
    """
    epoch: int
    roi_ema: float
    direction: int = Field(..., ge=-1, le=1)
    magnitude: float = Field(..., ge=0.0, le=1.0)
