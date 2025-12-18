#!/usr/bin/env python3
"""
Talisman AI API - FastAPI Application

This API provides endpoints for validators to:
- Get unscored tweets for scoring
- Submit rewards, penalties, and completed tweets
- Manage blacklisted hotkeys

Only validators with valid signatures are allowed to access the API.
"""

import os
import logging
from datetime import datetime
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from prisma import Prisma

# Local imports
from models import (
    Tweet, TweetWithAuthor, Account, TweetAnalysis,
    Scoring, ScoringUpdate,
    Penalty, PenaltyCreate, PenaltyBulkCreate,
    Reward, RewardCreate, RewardBulkCreate,
    BlacklistedHotkey, BlacklistedHotkeyCreate, BlacklistedHotkeyBulkCreate,
    TweetsForScoringResponse, CompletedTweetsSubmission,
    SubmissionResponse, ErrorResponse,
)
from utils.auth import (
    AuthRequest,
    auth_config,
    extract_auth_from_headers,
    verify_auth_request,
)
from hotkey_whitelist import (
    is_validator_hotkey,
    initialize_whitelists,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Initialize Prisma client
prisma = Prisma()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup/shutdown events."""
    # Startup
    logger.info("Starting Talisman AI API...")
    
    # Initialize whitelist caches
    try:
        initialize_whitelists()
        logger.info("Whitelists initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize whitelists: {e}")
    
    # Connect to database
    try:
        await prisma.connect()
        logger.info("Connected to database")
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        raise
    
    yield
    
    # Shutdown
    logger.info("Shutting down Talisman AI API...")
    await prisma.disconnect()
    logger.info("Disconnected from database")


# Create FastAPI application
app = FastAPI(
    title="Talisman AI API",
    description="API for Talisman AI subnet validators to score tweets and manage rewards/penalties",
    version="1.0.0",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# Authentication Dependencies
# ============================================================================

async def get_validator_hotkey(request: Request) -> str:
    """
    Dependency to authenticate validator and return their hotkey.
    
    Only validators are allowed to access the API. This function:
    1. Extracts auth data from request headers
    2. Verifies the signature
    3. Confirms the hotkey belongs to a validator
    4. Returns the validator's hotkey
    
    Raises HTTPException if authentication fails.
    """
    # If auth is disabled (local/testing), allow requests without headers.
    # We still try to read a hotkey from headers if present for attribution.
    if not auth_config.enabled:
        auth_request = extract_auth_from_headers(request)
        if auth_request and auth_request.ss58_address:
            return auth_request.ss58_address
        return "unauthenticated"

    # Extract auth from headers (required when auth is enabled)
    auth_request = extract_auth_from_headers(request)
    if auth_request is None:
        logger.warning("Missing authentication headers")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication headers. Required: X-Auth-SS58Address, X-Auth-Signature, X-Auth-Message, X-Auth-Timestamp",
        )
    
    # Verify auth request
    if not verify_auth_request(auth_request, auth_config):
        logger.warning(f"Authentication failed for hotkey: {auth_request.ss58_address}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication. Signature verification failed.",
        )
    
    # Check if hotkey is a validator
    if not is_validator_hotkey(auth_request.ss58_address):
        logger.warning(f"Non-validator hotkey attempted access: {auth_request.ss58_address}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Only validators are allowed to access this API.",
        )
    
    logger.info(f"Validator authenticated: {auth_request.ss58_address}")
    return auth_request.ss58_address


# ============================================================================
# Health Check
# ============================================================================

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


# ============================================================================
# Tweet Routes
# ============================================================================

@app.get(
    "/tweets/unscored",
    response_model=TweetsForScoringResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_unscored_tweets(
    limit: int = 3,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Get tweets that need scoring.
    
    Returns up to `limit` tweets (default 3) that either:
    - Have no scoring records at all, or
    - Have no TweetAnalysis record
    
    Excludes tweets that already have an 'in_progress' or 'completed' scoring.
    Creates a new scoring record (set to 'in_progress') for tweets without one.
    
    Only accessible by validators.
    """
    try:
        # Soft-lease TTL in seconds (default: 15 minutes). We reuse scoring.startTime as lease time.
        lease_ttl_seconds = int(os.getenv("SCORING_LEASE_TTL_SECONDS", "900"))

        # Reclaim + claim must be atomic to avoid double-leasing under concurrency.
        async with prisma.tx() as tx:
            # 1) Reclaim expired leases: in_progress older than TTL → pending (unassigned).
            await tx.execute_raw(
                """
                UPDATE scoring
                SET status = 'pending',
                    start_time = NULL,
                    validator_hotkey = NULL
                WHERE status = 'in_progress'
                  AND start_time IS NOT NULL
                  AND start_time < (NOW() AT TIME ZONE 'utc') - (MAKE_INTERVAL(secs => $1));
                """,
                lease_ttl_seconds,
            )

            # 2) Atomically claim up to `limit` pending rows using row locks.
            claimed = await tx.query_raw(
                """
                WITH picked AS (
                    SELECT id, tweet_id
                    FROM scoring
                    WHERE status = 'pending'
                    ORDER BY created_at ASC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                )
                UPDATE scoring s
                SET status = 'in_progress',
                    start_time = (NOW() AT TIME ZONE 'utc'),
                    validator_hotkey = $2
                FROM picked
                WHERE s.id = picked.id
                RETURNING picked.tweet_id;
                """,
                limit,
                validator_hotkey,
            )

        tweet_ids = [row["tweet_id"] for row in (claimed or [])]
        if not tweet_ids:
            return TweetsForScoringResponse(tweets=[], count=0)

        # Fetch the claimed tweets + nested author/analysis for response.
        tweets = await prisma.tweet.find_many(
            where={"id": {"in": tweet_ids}},
            include={"author": True, "analysis": True},
        )

        # Preserve claim order (find_many doesn't guarantee ordering by input list).
        tweets_by_id = {t.id: t for t in tweets}
        ordered = [tweets_by_id.get(tid) for tid in tweet_ids if tid in tweets_by_id]

        tweets_with_authors = []
        for tweet in ordered:
            author_model = None
            analysis_model = None

            if tweet.author:
                author_model = Account(
                    id=tweet.author.id,
                    name=tweet.author.name,
                    screenName=tweet.author.screenName,
                    userName=tweet.author.userName,
                    location=tweet.author.location,
                    description=tweet.author.description,
                    verified=tweet.author.verified,
                    isBlueVerified=tweet.author.isBlueVerified,
                    followersCount=tweet.author.followersCount,
                    followingCount=tweet.author.followingCount,
                    statusesCount=tweet.author.statusesCount,
                    profileImageUrl=tweet.author.profileImageUrl,
                    createdAt=tweet.author.createdAt,
                )

            if tweet.analysis:
                analysis_model = TweetAnalysis(
                    id=tweet.analysis.id,
                    tweetId=tweet.analysis.tweetId,
                    sentiment=tweet.analysis.sentiment,
                    subnetId=tweet.analysis.subnetId,
                    subnetName=tweet.analysis.subnetName,
                    contentType=tweet.analysis.contentType,
                    analyzedAt=tweet.analysis.analyzedAt,
                )

            tweet_data = TweetWithAuthor(
                id=tweet.id,
                type=tweet.type,
                url=tweet.url,
                text=tweet.text,
                lang=tweet.lang,
                retweetCount=tweet.retweetCount,
                replyCount=tweet.replyCount,
                likeCount=tweet.likeCount,
                quoteCount=tweet.quoteCount,
                viewCount=tweet.viewCount,
                bookmarkCount=tweet.bookmarkCount,
                isReply=tweet.isReply,
                inReplyToId=tweet.inReplyToId,
                conversationId=tweet.conversationId,
                authorId=tweet.authorId,
                createdAt=tweet.createdAt,
                receivedAt=tweet.receivedAt,
                author=author_model,
                analysis=analysis_model,
            )
            tweets_with_authors.append(tweet_data)

        logger.info(f"Leased {len(tweets_with_authors)} tweet(s) to validator {validator_hotkey}")
        return TweetsForScoringResponse(tweets=tweets_with_authors, count=len(tweets_with_authors))

    except Exception as e:
        logger.error(f"Error getting unscored tweets: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get unscored tweets: {str(e)}",
        )


@app.post(
    "/tweets/completed",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def submit_completed_tweets(
    submission: CompletedTweetsSubmission,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Submit completed scored tweets.
    
    Updates the scoring status to 'completed' and stores the sentiment in TweetAnalysis.
    Only tweets assigned to the requesting validator can be completed.
    
    Only accessible by validators.
    """
    try:
        updated_count = 0
        
        for completed in submission.completed_tweets:
            # Create or update TweetAnalysis with the sentiment
            await prisma.tweetanalysis.upsert(
                where={"tweetId": completed.tweet_id},
                data={
                    "create": {
                        "tweetId": completed.tweet_id,
                        "sentiment": completed.sentiment,
                        "analyzedAt": datetime.utcnow(),
                    },
                    "update": {
                        "sentiment": completed.sentiment,
                        "updatedAt": datetime.utcnow(),
                    },
                },
            )
            
            # Update scoring status to completed (only if still leased to this validator).
            result = await prisma.scoring.update_many(
                where={
                    "tweetId": completed.tweet_id,
                    "validatorHotkey": validator_hotkey,
                    "status": "in_progress",
                },
                data={"status": "completed"},
            )
            updated_count += result
        
        logger.info(f"Validator {validator_hotkey} completed {updated_count} tweets")
        return SubmissionResponse(
            success=True,
            message=f"Successfully completed {updated_count} tweets",
            count=updated_count,
        )
    
    except Exception as e:
        logger.error(f"Error submitting completed tweets: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to submit completed tweets: {str(e)}",
        )


# ============================================================================
# Reward Routes
# ============================================================================

@app.post(
    "/rewards",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def submit_rewards(
    submission: RewardBulkCreate,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Submit rewards for miners.
    
    Creates reward records for the specified hotkeys with their points.
    
    Only accessible by validators.
    """
    try:
        created_count = 0
        
        for reward in submission.rewards:
            await prisma.reward.create(
                data={
                    "startBlock": reward.start_block,
                    "stopBlock": reward.stop_block,
                    "hotkey": reward.hotkey,
                    "points": reward.points,
                }
            )
            created_count += 1
        
        logger.info(f"Validator {validator_hotkey} submitted {created_count} rewards")
        return SubmissionResponse(
            success=True,
            message=f"Successfully created {created_count} rewards",
            count=created_count,
        )
    
    except Exception as e:
        logger.error(f"Error submitting rewards: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to submit rewards: {str(e)}",
        )


@app.get(
    "/rewards",
    response_model=List[Reward],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_rewards(
    hotkey: Optional[str] = None,
    limit: int = 100,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Get rewards, optionally filtered by hotkey.
    
    Only accessible by validators.
    """
    try:
        where = {"hotkey": hotkey} if hotkey else {}
        rewards = await prisma.reward.find_many(
            where=where,
            take=limit,
            order={"id": "desc"},
        )
        
        return [
            Reward(
                id=r.id,
                startBlock=r.startBlock,
                stopBlock=r.stopBlock,
                hotkey=r.hotkey,
                points=r.points,
                createdAt=r.createdAt,
            )
            for r in rewards
        ]
    
    except Exception as e:
        logger.error(f"Error getting rewards: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get rewards: {str(e)}",
        )


# ============================================================================
# Penalty Routes
# ============================================================================

@app.post(
    "/penalties",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def submit_penalties(
    submission: PenaltyBulkCreate,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Submit penalties for miners.
    
    Creates penalty records for the specified hotkeys with reasons.
    
    Only accessible by validators.
    """
    try:
        created_count = 0
        
        for penalty in submission.penalties:
            await prisma.penalty.create(
                data={
                    "hotkey": penalty.hotkey,
                    "reason": penalty.reason,
                    "timestamp": datetime.utcnow(),
                }
            )
            created_count += 1
        
        logger.info(f"Validator {validator_hotkey} submitted {created_count} penalties")
        return SubmissionResponse(
            success=True,
            message=f"Successfully created {created_count} penalties",
            count=created_count,
        )
    
    except Exception as e:
        logger.error(f"Error submitting penalties: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to submit penalties: {str(e)}",
        )


@app.get(
    "/penalties",
    response_model=List[Penalty],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_penalties(
    hotkey: Optional[str] = None,
    limit: int = 100,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Get penalties, optionally filtered by hotkey.
    
    Only accessible by validators.
    """
    try:
        where = {"hotkey": hotkey} if hotkey else {}
        penalties = await prisma.penalty.find_many(
            where=where,
            take=limit,
            order={"timestamp": "desc"},
        )
        
        return [
            Penalty(
                id=p.id,
                hotkey=p.hotkey,
                reason=p.reason,
                timestamp=p.timestamp,
            )
            for p in penalties
        ]
    
    except Exception as e:
        logger.error(f"Error getting penalties: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get penalties: {str(e)}",
        )


# ============================================================================
# Blacklisted Hotkeys Routes
# ============================================================================

@app.get(
    "/blacklist",
    response_model=List[BlacklistedHotkey],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_blacklisted_hotkeys(
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Get all blacklisted hotkeys.
    
    Only accessible by validators.
    """
    try:
        blacklisted = await prisma.blacklistedhotkey.find_many()
        return [
            BlacklistedHotkey(
                hotkey=b.hotkey,
                reason=b.reason,
                createdAt=b.createdAt,
            )
            for b in blacklisted
        ]
    
    except Exception as e:
        logger.error(f"Error getting blacklisted hotkeys: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get blacklisted hotkeys: {str(e)}",
        )


@app.post(
    "/blacklist",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def add_blacklisted_hotkeys(
    submission: BlacklistedHotkeyBulkCreate,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Add hotkeys to the blacklist.
    
    Only accessible by validators.
    """
    try:
        created_count = 0
        
        for hotkey in submission.hotkeys:
            # Use upsert to avoid duplicates
            await prisma.blacklistedhotkey.upsert(
                where={"hotkey": hotkey},
                data={
                    "create": {
                        "hotkey": hotkey,
                        "reason": submission.reason,
                    },
                    "update": {
                        "reason": submission.reason,
                    },
                },
            )
            created_count += 1
        
        logger.info(f"Validator {validator_hotkey} added {created_count} hotkeys to blacklist")
        return SubmissionResponse(
            success=True,
            message=f"Successfully added {created_count} hotkeys to blacklist",
            count=created_count,
        )
    
    except Exception as e:
        logger.error(f"Error adding blacklisted hotkeys: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to add blacklisted hotkeys: {str(e)}",
        )


@app.delete(
    "/blacklist/{hotkey}",
    response_model=SubmissionResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def remove_blacklisted_hotkey(
    hotkey: str,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """
    Remove a hotkey from the blacklist.
    
    Only accessible by validators.
    """
    try:
        # Check if hotkey exists
        existing = await prisma.blacklistedhotkey.find_unique(where={"hotkey": hotkey})
        
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Hotkey {hotkey} not found in blacklist",
            )
        
        await prisma.blacklistedhotkey.delete(where={"hotkey": hotkey})
        
        logger.info(f"Validator {validator_hotkey} removed hotkey {hotkey} from blacklist")
        return SubmissionResponse(
            success=True,
            message=f"Successfully removed hotkey {hotkey} from blacklist",
            count=1,
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing blacklisted hotkey: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove blacklisted hotkey: {str(e)}",
        )


# ============================================================================
# Main Entry Point
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))
    
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=os.getenv("API_RELOAD", "false").lower() == "true",
    )
