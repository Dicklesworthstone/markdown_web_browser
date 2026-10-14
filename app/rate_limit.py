"""Rate limiting middleware using token bucket algorithm."""

from __future__ import annotations

import threading
import time
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Field, SQLModel

from app.settings import Settings


class RateLimitBucket(SQLModel, table=True):
    """Shared token state; independent API workers consume the same bucket."""

    key: str = Field(primary_key=True)
    tokens: float
    updated_at: float


def consume_shared(engine: Engine, key: str, limit: int) -> tuple[bool, dict[str, int]]:
    if limit <= 0:
        raise ValueError("Rate limit must be positive")
    with engine.begin() as conn:
        # Serialize read/refill/consume across connections and processes.
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        now = time.time()
        row = conn.execute(
            text("SELECT tokens, updated_at FROM ratelimitbucket WHERE key = :key"),
            {"key": key},
        ).first()
        tokens = (
            float(limit)
            if row is None
            else min(
                float(limit),
                row[0] + max(0, now - row[1]) * limit / 60.0,
            )
        )
        allowed = tokens >= 1
        if allowed:
            tokens -= 1
        conn.execute(
            text(
                "INSERT INTO ratelimitbucket (key, tokens, updated_at) "
                "VALUES (:key, :tokens, :now) ON CONFLICT(key) DO UPDATE SET "
                "tokens = excluded.tokens, updated_at = excluded.updated_at"
            ),
            {"key": key, "tokens": tokens, "now": now},
        )
        return allowed, {
            "limit": limit,
            "remaining": int(tokens),
            "reset": math.ceil(now + (limit - tokens) * 60.0 / limit),
            "retry_after": max(1, math.ceil((1 - tokens) * 60.0 / limit)),
        }


def extract_rate_limit_key(request: Request) -> str:
    """Extract rate limit key from request.

    Uses API key (if valid), auth context, or falls back to client IP.

    Args:
        request: FastAPI Request object

    Returns:
        Rate limit key string (api_key:*, api_key_id:*, or ip:*)
    """
    # Only verified identity may select a credential bucket.
    if getattr(request.state, "auth_context", None) and request.state.auth_context.api_key_id:
        return f"api_key_id:{request.state.auth_context.api_key_id}"

    # Fall back to client IP
    client_host = request.client.host if request.client else "unknown"
    return f"ip:{client_host}"


@dataclass
class TokenBucket:
    """Token bucket for rate limiting.

    Tokens are added at a constant rate (refill_rate per second).
    Each request consumes 1 token. When bucket is empty, requests are rejected.
    """

    capacity: int  # Maximum tokens
    tokens: float  # Current tokens
    refill_rate: float  # Tokens per second
    last_refill: float  # Timestamp of last refill

    def consume(self, tokens: int = 1) -> bool:
        """Try to consume tokens. Returns True if successful, False if insufficient tokens."""
        self._refill()

        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self.last_refill

        # Add tokens based on elapsed time
        new_tokens = elapsed * self.refill_rate
        self.tokens = min(self.capacity, self.tokens + new_tokens)
        self.last_refill = now

    def time_until_available(self, tokens: int = 1) -> float:
        """Calculate seconds until enough tokens are available."""
        self._refill()

        if self.tokens >= tokens:
            return 0.0

        needed = tokens - self.tokens
        return needed / self.refill_rate

    def get_stats(self) -> Dict[str, float]:
        """Get current bucket statistics."""
        self._refill()
        # Prevent division by zero if capacity is 0
        utilization = 1.0 - (self.tokens / self.capacity) if self.capacity > 0 else 0.0
        return {
            "tokens": self.tokens,
            "capacity": self.capacity,
            "refill_rate": self.refill_rate,
            "utilization": utilization,
        }


class RateLimiter:
    """In-memory rate limiter using token buckets.

    For production with multiple workers, consider using Redis.
    """

    def __init__(self, requests_per_minute: int = 60, burst_capacity: int | None = None):
        """Initialize rate limiter.

        Args:
            requests_per_minute: Sustained rate limit (must be positive)
            burst_capacity: Maximum burst capacity (defaults to requests_per_minute)

        Raises:
            ValueError: If requests_per_minute is not positive
        """
        if requests_per_minute <= 0:
            raise ValueError(f"requests_per_minute must be positive, got {requests_per_minute}")

        self.requests_per_minute = requests_per_minute
        self.requests_per_second = requests_per_minute / 60.0

        # Allow bursts up to the per-minute limit
        self.burst_capacity = burst_capacity or requests_per_minute

        # Storage: key -> TokenBucket
        self.buckets: Dict[str, TokenBucket] = {}

    def _create_bucket(self) -> TokenBucket:
        """Create a new token bucket with configured limits."""
        return TokenBucket(
            capacity=self.burst_capacity,
            tokens=self.burst_capacity,  # Start full
            refill_rate=self.requests_per_second,
            last_refill=time.time(),
        )

    def _get_bucket(self, key: str) -> TokenBucket:
        """Get or create a token bucket for the given key."""
        if key not in self.buckets:
            self.buckets[key] = self._create_bucket()
        return self.buckets[key]

    def check_rate_limit(self, key: str, tokens: int = 1) -> tuple[bool, Dict[str, Any]]:
        """Check if request is allowed under rate limit.

        Returns:
            (allowed, stats) tuple where stats contains rate limit info:
                - limit: Maximum requests per minute
                - remaining: Current tokens available
                - reset: Unix timestamp when bucket will be completely full
                - retry_after: (if not allowed) Seconds to wait before retrying
        """
        bucket = self._get_bucket(key)
        allowed = bucket.consume(tokens)

        stats = bucket.get_stats()
        stats["limit"] = self.requests_per_minute
        stats["remaining"] = int(bucket.tokens)
        # Reset = when bucket will be completely full (burst capacity restored)
        # Note: You may still have tokens available before this time
        stats["reset"] = int(time.time() + bucket.time_until_available(self.burst_capacity))

        if not allowed:
            # Retry after = when next single token will be available
            stats["retry_after"] = int(bucket.time_until_available(1)) + 1

        return allowed, stats

    def cleanup_stale_buckets(self, max_age_seconds: float = 3600) -> int:
        """Remove buckets that haven't been used recently.

        Returns:
            Number of buckets removed
        """
        now = time.time()
        stale_keys = [
            key
            for key, bucket in self.buckets.items()
            if now - bucket.last_refill > max_age_seconds
        ]

        for key in stale_keys:
            del self.buckets[key]

        return len(stale_keys)


# Global rate limiter instance (for single-process deployments)
# For multi-worker deployments, use Redis-backed rate limiting
_global_limiter: Optional[RateLimiter] = None
_limiter_lock = threading.Lock()


def get_rate_limiter(settings: Settings | None = None) -> RateLimiter:
    """Get or create the global rate limiter instance.

    Thread-safe singleton implementation using double-checked locking pattern.

    Args:
        settings: Optional settings to configure rate limiter.
                  Only used on first initialization, ignored afterwards.

    Returns:
        RateLimiter: Global rate limiter instance
    """
    global _global_limiter

    # Fast path - if already initialized, return immediately without locking
    if _global_limiter is not None:
        return _global_limiter

    # Slow path - need to initialize, acquire lock
    with _limiter_lock:
        # Double-check: another thread might have initialized while we waited for lock
        if _global_limiter is None:
            from app.settings import settings as global_settings

            active_settings = settings or global_settings

            # Get rate limit from settings, default to 60 requests per minute
            requests_per_minute = getattr(active_settings, "RATE_LIMIT_PER_MINUTE", 60)

            _global_limiter = RateLimiter(requests_per_minute=requests_per_minute)

    return _global_limiter


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware for rate limiting.

    Adds rate limit headers to all responses:
    - X-RateLimit-Limit: Maximum requests per minute
    - X-RateLimit-Remaining: Remaining requests in current window
    - X-RateLimit-Reset: Unix timestamp when limit resets
    """

    def __init__(self, app, limiter: Optional[RateLimiter] = None):
        super().__init__(app)
        self.limiter = limiter or get_rate_limiter()

    async def dispatch(self, request: Request, call_next):
        """Process request and apply rate limiting."""

        # Get rate limit key (API key or IP address)
        rate_limit_key = self._get_rate_limit_key(request)

        # Check rate limit
        allowed, stats = self.limiter.check_rate_limit(rate_limit_key)

        # Add rate limit headers
        headers = {
            "X-RateLimit-Limit": str(stats["limit"]),
            "X-RateLimit-Remaining": str(stats["remaining"]),
            "X-RateLimit-Reset": str(stats["reset"]),
        }

        if not allowed:
            # Rate limit exceeded
            headers["Retry-After"] = str(stats["retry_after"])

            return Response(
                content='{"detail": "Rate limit exceeded. Please try again later."}',
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers=headers,
                media_type="application/json",
            )

        # Process request
        response = await call_next(request)

        # Add rate limit headers to response
        for key, value in headers.items():
            response.headers[key] = value

        return response

    def _get_rate_limit_key(self, request: Request) -> str:
        """Extract rate limit key from request.

        Uses API key if available, otherwise falls back to client IP.
        """
        return extract_rate_limit_key(request)


# Dependency for manual rate limit checking in endpoints
async def check_rate_limit(
    request: Request,
    tokens: int = 1,
) -> Dict[str, Any]:
    """FastAPI dependency to manually check rate limits in endpoints.

    Usage for single token (default):
        @app.get("/expensive-operation")
        async def expensive_op(rate_info: Dict = Depends(check_rate_limit)):
            # This endpoint consumes 1 token
            ...

    For operations that consume multiple tokens, create a wrapper function:
        async def check_rate_limit_5(request: Request) -> Dict[str, Any]:
            return await check_rate_limit(request, tokens=5)

        @app.post("/batch-operation")
        async def batch_op(rate_info: Dict = Depends(check_rate_limit_5)):
            # This endpoint consumes 5 tokens
            ...

    Or use functools.partial:
        from functools import partial

        @app.post("/batch-operation")
        async def batch_op(
            rate_info: Dict = Depends(partial(check_rate_limit, tokens=5))
        ):
            # This endpoint consumes 5 tokens
            ...
    """
    limiter = get_rate_limiter()

    # Get rate limit key
    key = extract_rate_limit_key(request)

    # Check rate limit
    allowed, stats = limiter.check_rate_limit(key, tokens)

    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please try again later.",
            headers={
                "Retry-After": str(stats["retry_after"]),
                "X-RateLimit-Limit": str(stats["limit"]),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(stats["reset"]),
            },
        )

    return stats
