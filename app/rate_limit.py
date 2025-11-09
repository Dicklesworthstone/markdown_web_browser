"""Rate limiting middleware using token bucket algorithm."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Optional

from fastapi import Header, HTTPException, Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware

from app.settings import Settings, settings as global_settings


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
        return {
            "tokens": self.tokens,
            "capacity": self.capacity,
            "refill_rate": self.refill_rate,
            "utilization": 1.0 - (self.tokens / self.capacity),
        }


class RateLimiter:
    """In-memory rate limiter using token buckets.

    For production with multiple workers, consider using Redis.
    """

    def __init__(self, requests_per_minute: int = 60, burst_capacity: int | None = None):
        """Initialize rate limiter.

        Args:
            requests_per_minute: Sustained rate limit
            burst_capacity: Maximum burst capacity (defaults to requests_per_minute)
        """
        self.requests_per_minute = requests_per_minute
        self.requests_per_second = requests_per_minute / 60.0

        # Allow bursts up to the per-minute limit
        self.burst_capacity = burst_capacity or requests_per_minute

        # Storage: key -> TokenBucket
        self.buckets: Dict[str, TokenBucket] = {}

        # Global bucket for unauthenticated requests
        self.global_bucket = self._create_bucket()

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

    def check_rate_limit(self, key: str, tokens: int = 1) -> tuple[bool, Dict[str, any]]:
        """Check if request is allowed under rate limit.

        Returns:
            (allowed, stats) tuple where stats contains rate limit info
        """
        bucket = self._get_bucket(key)
        allowed = bucket.consume(tokens)

        stats = bucket.get_stats()
        stats["limit"] = self.requests_per_minute
        stats["remaining"] = int(bucket.tokens)
        stats["reset"] = int(time.time() + bucket.time_until_available(self.burst_capacity))

        if not allowed:
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


def get_rate_limiter(settings: Settings | None = None) -> RateLimiter:
    """Get or create the global rate limiter instance."""
    global _global_limiter

    if _global_limiter is None:
        active_settings = settings or global_settings
        # Default to 60 requests per minute
        # In production, this should be configurable per API key
        _global_limiter = RateLimiter(requests_per_minute=60)

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
        # Try to get API key from header
        api_key = request.headers.get("X-API-Key", None)
        if api_key:
            # Use API key prefix as the rate limit key
            return f"api_key:{api_key[:12]}"

        # Try to get from auth context (if auth middleware ran)
        if hasattr(request.state, "auth_context"):
            return f"api_key_id:{request.state.auth_context.api_key_id}"

        # Fall back to client IP
        client_host = request.client.host if request.client else "unknown"
        return f"ip:{client_host}"


# Dependency for manual rate limit checking in endpoints
async def check_rate_limit(
    request: Request,
    tokens: int = 1,
) -> Dict[str, any]:
    """FastAPI dependency to manually check rate limits in endpoints.

    Usage:
        @app.get("/expensive-operation")
        async def expensive_op(rate_info: Dict = Depends(check_rate_limit)):
            # This endpoint consumes 1 token
            ...

    For operations that should consume multiple tokens:
        @app.post("/batch-operation")
        async def batch_op(rate_info: Dict = Depends(lambda r: check_rate_limit(r, tokens=5))):
            # This endpoint consumes 5 tokens
            ...
    """
    limiter = get_rate_limiter()

    # Get rate limit key
    api_key = request.headers.get("X-API-Key", None)
    if api_key:
        key = f"api_key:{api_key[:12]}"
    elif hasattr(request.state, "auth_context"):
        key = f"api_key_id:{request.state.auth_context.api_key_id}"
    else:
        client_host = request.client.host if request.client else "unknown"
        key = f"ip:{client_host}"

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
