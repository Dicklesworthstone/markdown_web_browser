"""Rate limiting middleware using token bucket algorithm."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import (
    check_job_owner_access,
    extract_api_key_from_request,
    get_store,
    is_auth_exempt_path,
    resolve_request_auth,
)
from app.settings import Settings, effective_rate_limit_per_minute


def extract_rate_limit_key(request: Request) -> str:
    """Extract rate limit key from request.

    Uses auth context (if authenticated), API key header/Bearer token, or falls back to client IP.

    Args:
        request: FastAPI Request object

    Returns:
        Rate limit key string (api_key_id:*, api_key:*, or ip:*)
    """
    if hasattr(request.state, "auth_context") and getattr(
        request.state.auth_context, "api_key_id", 0
    ):
        return f"api_key_id:{request.state.auth_context.api_key_id}"

    api_key, _ = extract_api_key_from_request(request)
    if api_key and api_key.startswith("mdwb_") and len(api_key) == 37:
        return f"api_key:{api_key[:12]}"

    if hasattr(request.state, "auth_context"):
        return f"api_key_id:{request.state.auth_context.api_key_id}"

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
        elapsed = max(0.0, now - self.last_refill)

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
        utilization = 1.0 - (self.tokens / self.capacity) if self.capacity > 0 else 0.0
        return {
            "tokens": self.tokens,
            "capacity": self.capacity,
            "refill_rate": self.refill_rate,
            "utilization": utilization,
        }


class RateLimiter:
    """Token-bucket rate limiter with optional SQLite-backed cross-worker synchronization."""

    def __init__(
        self,
        requests_per_minute: int = 60,
        burst_capacity: int | None = None,
        *,
        store: Any | None = None,
    ):
        if requests_per_minute <= 0:
            raise ValueError(f"requests_per_minute must be positive, got {requests_per_minute}")

        self.requests_per_minute = requests_per_minute
        self.requests_per_second = requests_per_minute / 60.0
        self.burst_capacity = burst_capacity or requests_per_minute
        self.buckets: Dict[str, TokenBucket] = {}
        self.store = store
        self._lock = threading.Lock()

    def _create_bucket(
        self,
        *,
        requests_per_minute: int | None = None,
        burst_capacity: int | None = None,
    ) -> TokenBucket:
        rpm = requests_per_minute if (requests_per_minute and requests_per_minute > 0) else self.requests_per_minute
        cap = burst_capacity if (burst_capacity and burst_capacity > 0) else (
            rpm if requests_per_minute else self.burst_capacity
        )
        return TokenBucket(
            capacity=cap,
            tokens=float(cap),
            refill_rate=rpm / 60.0,
            last_refill=time.time(),
        )

    def _get_bucket(
        self,
        key: str,
        *,
        requests_per_minute: int | None = None,
    ) -> TokenBucket:
        rpm = requests_per_minute if (requests_per_minute and requests_per_minute > 0) else self.requests_per_minute
        cap = rpm if requests_per_minute else self.burst_capacity
        rps = rpm / 60.0
        if key not in self.buckets:
            self.buckets[key] = self._create_bucket(requests_per_minute=rpm, burst_capacity=cap)
        else:
            bucket = self.buckets[key]
            if bucket.capacity != cap or abs(bucket.refill_rate - rps) > 1e-9:
                bucket._refill()
                bucket.capacity = cap
                bucket.refill_rate = rps
                bucket.tokens = min(float(cap), bucket.tokens)
        return self.buckets[key]

    def _consume_shared_store(
        self,
        store: Any,
        key: str,
        tokens: int,
        *,
        rpm: int,
        cap: int,
    ) -> tuple[bool, TokenBucket] | None:
        engine = getattr(store, "engine", None)
        if engine is None:
            return None
        rps = rpm / 60.0
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA busy_timeout=50")
                try:
                    with conn.begin():
                        try:
                            row = conn.exec_driver_sql(
                                "SELECT capacity, tokens, refill_rate, last_refill FROM rate_limit_buckets WHERE key = ?",
                                (key,),
                            ).fetchone()
                        except Exception:
                            conn.exec_driver_sql(
                                """
                                CREATE TABLE IF NOT EXISTS rate_limit_buckets (
                                    key TEXT PRIMARY KEY,
                                    capacity INTEGER NOT NULL,
                                    tokens REAL NOT NULL,
                                    refill_rate REAL NOT NULL,
                                    last_refill REAL NOT NULL
                                )
                                """
                            )
                            row = None
                        now = time.time()
                        if row is None:
                            bucket = TokenBucket(
                                capacity=cap,
                                tokens=float(cap),
                                refill_rate=rps,
                                last_refill=now,
                            )
                        else:
                            bucket = TokenBucket(
                                capacity=cap,
                                tokens=min(float(cap), float(row[1])),
                                refill_rate=rps,
                                last_refill=float(row[3]),
                            )
                        allowed = bucket.consume(tokens)
                        conn.exec_driver_sql(
                            """
                            INSERT INTO rate_limit_buckets (key, capacity, tokens, refill_rate, last_refill)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(key) DO UPDATE SET
                                capacity = excluded.capacity,
                                tokens = excluded.tokens,
                                refill_rate = excluded.refill_rate,
                                last_refill = excluded.last_refill
                            """,
                            (
                                key,
                                bucket.capacity,
                                bucket.tokens,
                                bucket.refill_rate,
                                bucket.last_refill,
                            ),
                        )
                        self.buckets[key] = bucket
                        return allowed, bucket
                finally:
                    try:
                        conn.exec_driver_sql("PRAGMA busy_timeout=30000")
                    except Exception:
                        pass
        except Exception:
            return None

    def check_rate_limit(
        self,
        key: str,
        tokens: int = 1,
        *,
        requests_per_minute: int | None = None,
        store: Any | None = None,
    ) -> tuple[bool, Dict[str, Any]]:
        """Check if request is allowed under rate limit."""
        rpm = requests_per_minute if (requests_per_minute and requests_per_minute > 0) else self.requests_per_minute
        cap = rpm if requests_per_minute else self.burst_capacity
        active_store = store or self.store

        with self._lock:
            shared_res = (
                self._consume_shared_store(active_store, key, tokens, rpm=rpm, cap=cap)
                if active_store is not None
                else None
            )
            if shared_res is not None:
                allowed, bucket = shared_res
            else:
                bucket = self._get_bucket(key, requests_per_minute=rpm)
                allowed = bucket.consume(tokens)

            stats = bucket.get_stats()
            stats["limit"] = rpm
            stats["remaining"] = int(bucket.tokens)
            stats["reset"] = int(time.time() + bucket.time_until_available(cap))

            if not allowed:
                stats["retry_after"] = int(bucket.time_until_available(1)) + 1

            return allowed, stats

    def reset(self, *, store: Any | None = None) -> None:
        """Clear all in-memory and persisted rate-limit buckets."""
        with self._lock:
            self.buckets.clear()
            active_store = store or self.store
            engine = getattr(active_store, "engine", None) if active_store is not None else None
            if engine is not None:
                try:
                    with engine.begin() as conn:
                        conn.exec_driver_sql("DELETE FROM rate_limit_buckets")
                except Exception:
                    pass

    def cleanup_stale_buckets(self, max_age_seconds: float = 3600) -> int:
        """Remove buckets that haven't been used recently."""
        now = time.time()
        with self._lock:
            stale_keys = [
                key
                for key, bucket in self.buckets.items()
                if now - bucket.last_refill > max_age_seconds
            ]

            for key in stale_keys:
                del self.buckets[key]

            return len(stale_keys)


_global_limiter: Optional[RateLimiter] = None
_limiter_lock = threading.Lock()


def get_rate_limiter(settings: Settings | None = None) -> RateLimiter:
    """Get or create the global rate limiter instance."""
    global _global_limiter

    if _global_limiter is not None:
        return _global_limiter

    with _limiter_lock:
        if _global_limiter is None:
            requests_per_minute = effective_rate_limit_per_minute(settings)
            _global_limiter = RateLimiter(requests_per_minute=requests_per_minute)

    return _global_limiter


def reset_rate_limiter() -> None:
    """Reset the global rate limiter and any SQLite-backed bucket rows."""
    global _global_limiter
    try:
        store = get_store()
    except Exception:
        store = None
    if _global_limiter is not None:
        _global_limiter.reset(store=store)
    elif store is not None:
        engine = getattr(store, "engine", None)
        if engine is not None:
            try:
                with engine.begin() as conn:
                    conn.exec_driver_sql("DELETE FROM rate_limit_buckets")
            except Exception:
                pass


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware enforcing API key authentication and token-bucket rate limiting."""

    def __init__(self, app, limiter: Optional[RateLimiter] = None):
        super().__init__(app)
        self._custom_limiter = limiter is not None
        self.limiter = limiter or get_rate_limiter()

    async def dispatch(self, request: Request, call_next):
        """Authenticate request (when required) and apply rate limiting."""
        path = request.url.path
        exempt = is_auth_exempt_path(path)
        is_health_or_stream = (
            path.startswith("/health")
            or path.startswith("/static/")
            or path.startswith("/web/")
            or path.endswith("/stream")
            or path.endswith("/events")
        )

        try:
            await asyncio.to_thread(resolve_request_auth, request)
        except HTTPException as exc:
            if not exempt:
                return Response(
                    content=json.dumps({"detail": exc.detail}),
                    status_code=exc.status_code,
                    headers=dict(exc.headers or {}),
                    media_type="application/json",
                )

        rate_limit_key = self._get_rate_limit_key(request)

        auth_ctx = getattr(request.state, "auth_context", None)
        key_rate_limit = getattr(auth_ctx, "rate_limit", None) if auth_ctx is not None else None
        if key_rate_limit is not None and key_rate_limit > 0:
            effective_rpm: int | None = int(key_rate_limit)
        elif not self._custom_limiter:
            effective_rpm = effective_rate_limit_per_minute()
        else:
            effective_rpm = None

        active_store = None
        if not self._custom_limiter and not is_health_or_stream:
            try:
                active_store = get_store()
            except Exception:
                active_store = None

        allowed, stats = await asyncio.to_thread(
            self.limiter.check_rate_limit,
            rate_limit_key,
            requests_per_minute=effective_rpm,
            store=active_store,
        )

        headers = {
            "X-RateLimit-Limit": str(stats["limit"]),
            "X-RateLimit-Remaining": str(stats["remaining"]),
            "X-RateLimit-Reset": str(stats["reset"]),
        }

        if not allowed:
            headers["Retry-After"] = str(stats["retry_after"])
            return Response(
                content='{"detail": "Rate limit exceeded. Please try again later."}',
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers=headers,
                media_type="application/json",
            )

        if not exempt and path.startswith("/jobs/"):
            parts = [seg for seg in path.split("/") if seg]
            if len(parts) >= 2 and parts[0] == "jobs":
                try:
                    await asyncio.to_thread(check_job_owner_access, request, parts[1])
                except HTTPException as exc:
                    err_headers = dict(headers)
                    err_headers.update(dict(exc.headers or {}))
                    return Response(
                        content=json.dumps({"detail": exc.detail}),
                        status_code=exc.status_code,
                        headers=err_headers,
                        media_type="application/json",
                    )

        response = await call_next(request)

        for key, value in headers.items():
            response.headers[key] = value

        return response

    def _get_rate_limit_key(self, request: Request) -> str:
        """Extract rate limit key from request."""
        return extract_rate_limit_key(request)


async def check_rate_limit(
    request: Request,
    tokens: int = 1,
) -> Dict[str, Any]:
    """FastAPI dependency to manually check rate limits in endpoints."""
    limiter = get_rate_limiter()
    key = extract_rate_limit_key(request)
    auth_ctx = getattr(request.state, "auth_context", None)
    key_rate_limit = getattr(auth_ctx, "rate_limit", None) if auth_ctx is not None else None
    effective_rpm = (
        int(key_rate_limit)
        if (key_rate_limit is not None and key_rate_limit > 0)
        else effective_rate_limit_per_minute()
    )
    try:
        active_store = get_store()
    except Exception:
        active_store = None

    allowed, stats = limiter.check_rate_limit(
        key,
        tokens,
        requests_per_minute=effective_rpm,
        store=active_store,
    )

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
