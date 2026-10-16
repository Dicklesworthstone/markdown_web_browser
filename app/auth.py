"""API authentication and authorization middleware."""

from __future__ import annotations

import hashlib
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterator, Optional

if TYPE_CHECKING:
    from app.store import Store

import sys
from fastapi import Depends, Header, HTTPException, Request, status
from sqlmodel import Field, Session, SQLModel, select

from app.settings import (
    Settings,
    effective_api_key_header,
    effective_require_api_key,
    settings as global_settings,
)


class APIKey(SQLModel, table=True):
    """API key for authentication."""

    __tablename__ = "api_keys"

    id: int | None = Field(default=None, primary_key=True)
    key_hash: str = Field(index=True, unique=True)
    key_prefix: str = Field(index=True)  # First 12 chars for display (mdwb_XXXXXXX)
    name: str  # Human-readable name for the key
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_used_at: datetime | None = None
    is_active: bool = Field(default=True)
    rate_limit: int | None = Field(default=None)  # Requests per minute, None = no limit
    owner: str | None = None  # Optional owner identifier


@dataclass
class AuthContext:
    """Authentication context for the current request."""

    api_key_id: int
    api_key_name: str
    api_key_prefix: str
    rate_limit: int | None
    owner: str | None


def hash_api_key(api_key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_api_key() -> str:
    """Generate a new random API key.

    Format: mdwb_<32 random hex chars>
    Example: mdwb_a1b2c3d4e5f67890abcdef1234567890
    """
    random_part = secrets.token_hex(16)  # 32 hex chars
    return f"mdwb_{random_part}"


def create_api_key(
    session: Session,
    name: str,
    rate_limit: int | None = None,
    owner: str | None = None,
) -> tuple[str, APIKey]:
    """Create a new API key and store it in the database.

    Returns:
        tuple: (plain_text_key, db_record)

    Note: The plain text key is returned only once during creation.
    It cannot be retrieved later, only the hash is stored.
    """
    plain_key = generate_api_key()
    key_hash = hash_api_key(plain_key)
    key_prefix = plain_key[:12]  # mdwb_<first 7 hex chars>

    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=name,
        rate_limit=rate_limit,
        owner=owner,
    )

    session.add(api_key)
    session.commit()
    session.refresh(api_key)

    return plain_key, api_key


def verify_api_key(
    session: Session,
    api_key: str,
    update_threshold_seconds: int = 3600,
) -> Optional[APIKey]:
    """Verify an API key and return the corresponding record.

    Updates last_used_at timestamp only if it hasn't been updated recently,
    to avoid database writes on every request (performance optimization).

    Args:
        session: Database session
        api_key: API key to verify
        update_threshold_seconds: Only update timestamp if last update was
            more than this many seconds ago (default: 3600 = 1 hour)

    Returns:
        APIKey if valid and active, None otherwise
    """
    if not api_key or not api_key.startswith("mdwb_"):
        return None

    key_hash = hash_api_key(api_key)

    statement = select(APIKey).where(
        APIKey.key_hash == key_hash,
        APIKey.is_active == True,  # noqa: E712
    )

    result = session.exec(statement).first()

    if result:
        # Update last used timestamp only if it's been a while since last update
        # This prevents a database write on every request (huge performance win)
        now = datetime.now(timezone.utc)

        # Handle timezone-aware/naive datetime comparison
        # SQLite doesn't preserve timezone info, so we need to ensure compatibility
        last_used = result.last_used_at
        if last_used is not None and last_used.tzinfo is None:
            # Database returned naive datetime - assume UTC
            last_used = last_used.replace(tzinfo=timezone.utc)

        should_update = (
            last_used is None or (now - last_used).total_seconds() > update_threshold_seconds
        )

        if should_update:
            result.last_used_at = now
            session.add(result)
            session.commit()
            session.refresh(result)

    return result


def revoke_api_key(session: Session, key_id: int) -> bool:
    """Revoke an API key by setting is_active to False.

    Returns:
        True if key was found and revoked, False otherwise
    """
    statement = select(APIKey).where(APIKey.id == key_id)
    api_key = session.exec(statement).first()

    if not api_key:
        return False

    api_key.is_active = False
    session.add(api_key)
    session.commit()

    return True


# Global store instance for database connection pooling
# Creating the engine is expensive, so we create it once and reuse it
_global_store: Optional["Store"] = None
_explicit_store: Optional["Store"] = None
_store_lock = threading.Lock()


def set_store(store: Optional["Store"]) -> None:
    """Explicitly bind a Store instance for authentication lookups."""
    global _explicit_store, _global_store
    with _store_lock:
        _explicit_store = store
        if store is not None:
            _global_store = store


def get_store() -> "Store":
    """Get or create the active Store instance.

    Prefers an explicitly bound store or ``app.main.store`` when running inside
    the FastAPI application (so TestClient fixtures that swap ``app.main.store``
    are honored), falling back to a singleton ``Store``.
    """
    global _global_store

    if _explicit_store is not None:
        return _explicit_store

    main_mod = sys.modules.get("app.main")
    if main_mod is not None:
        main_store = getattr(main_mod, "store", None)
        if main_store is not None and hasattr(main_store, "session"):
            return main_store

    # Fast path - if already initialized, return immediately without locking
    if _global_store is not None:
        return _global_store

    # Slow path - need to initialize, acquire lock
    with _store_lock:
        if _global_store is None:
            from app.store import Store

            _global_store = Store()

    return _global_store


def get_db_session() -> Iterator[Session]:
    """FastAPI dependency to get database session."""
    store = get_store()
    with store.session() as session:
        yield session


_AUTH_EXEMPT_EXACT_PATHS = {
    "/",
    "/browser",
    "/health",
    "/healthz",
    "/health/beads",
    "/health/live",
    "/health/ready",
    "/docs",
    "/redoc",
    "/openapi.json",
}


def is_auth_exempt_path(path: str) -> bool:
    """Return True if a request path is exempt from mandatory API key auth."""
    if path in _AUTH_EXEMPT_EXACT_PATHS:
        return True
    if path.startswith("/static/") or path.startswith("/web/"):
        return True
    if path.startswith("/jobs/share/"):
        return True
    return False


def extract_api_key_from_request(
    request: Request | None,
    active_settings: Settings | None = None,
    *,
    explicit_key: str | None = None,
) -> tuple[str | None, str]:
    """Extract an API key candidate and configured header name from a request.

    Checks:
    1. ``explicit_key`` argument (if passed directly to ``get_auth_context``)
    2. Configured ``API_KEY_HEADER`` (default ``X-API-Key``)
    3. ``X-API-Key`` header (when a custom header name is configured)
    4. ``Authorization: Bearer <key>`` or ``Authorization: ApiKey <key>``
    5. ``mdwb_api_key`` cookie or ``?api_key=`` query param (for browser SSE / img requests)
    """
    header_name = effective_api_key_header(active_settings)
    if explicit_key:
        return explicit_key.strip(), header_name
    if request is None:
        return None, header_name

    # 1. Configured header
    val = request.headers.get(header_name)
    if val and val.strip():
        return val.strip(), header_name

    # 2. Standard X-API-Key fallback
    if header_name.lower() != "x-api-key":
        val = request.headers.get("X-API-Key")
        if val and val.strip():
            return val.strip(), header_name

    # 3. Authorization header (used by mdwb_cli.py and olmocr_cli.py)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.strip():
        parts = auth_header.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() in {"bearer", "apikey", "token"}:
            candidate = parts[1].strip()
            if candidate:
                return candidate, header_name

    # 4. Cookie / query param for browser UI (EventSource / tile image tags)
    cookie_key = request.cookies.get("mdwb_api_key")
    if cookie_key and cookie_key.strip():
        return cookie_key.strip(), header_name

    query_key = request.query_params.get("api_key")
    if query_key and query_key.strip():
        return query_key.strip(), header_name

    return None, header_name


def resolve_request_auth(
    request: Request,
    *,
    session: Session | None = None,
    x_api_key: str | None = None,
    settings: Settings | None = None,
) -> AuthContext:
    """Validate request authentication and populate ``request.state.auth_context``."""
    if hasattr(request.state, "auth_context") and isinstance(
        request.state.auth_context, AuthContext
    ) and x_api_key is None and settings is None:
        return request.state.auth_context

    active_settings = settings or global_settings
    require_key = effective_require_api_key(active_settings)
    candidate_key, header_name = extract_api_key_from_request(
        request,
        active_settings,
        explicit_key=x_api_key,
    )

    if not require_key and not candidate_key:
        ctx = AuthContext(
            api_key_id=0,
            api_key_name="anonymous",
            api_key_prefix="none",
            rate_limit=None,
            owner=None,
        )
        request.state.auth_context = ctx
        return ctx

    if not candidate_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing API key. Provide {header_name} header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if not candidate_key.startswith("mdwb_") or len(candidate_key) != 37:
        if not require_key:
            ctx = AuthContext(
                api_key_id=0,
                api_key_name="anonymous",
                api_key_prefix="none",
                rate_limit=None,
                owner=None,
            )
            request.state.auth_context = ctx
            return ctx
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key format",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    def _lookup_ctx(db_session: Session) -> Optional[AuthContext]:
        rec = verify_api_key(db_session, candidate_key)
        if not rec:
            return None
        if rec.id is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Database error: API key missing ID",
            )
        return AuthContext(
            api_key_id=rec.id,
            api_key_name=rec.name,
            api_key_prefix=rec.key_prefix,
            rate_limit=rec.rate_limit,
            owner=rec.owner,
        )

    if session is not None:
        ctx_found = _lookup_ctx(session)
    else:
        store = get_store()
        with store.session() as db_session:
            ctx_found = _lookup_ctx(db_session)

    if ctx_found is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    request.state.auth_context = ctx_found
    return ctx_found


async def get_auth_context(
    request: Request,
    session: Session = Depends(get_db_session),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    settings: Settings | None = None,
) -> AuthContext:
    """FastAPI dependency to get authentication context from request."""
    explicit = x_api_key if isinstance(x_api_key, str) else None
    return resolve_request_auth(
        request,
        session=session,
        x_api_key=explicit,
        settings=settings,
    )


def get_request_owner(request: Request) -> str | None:
    """Return the authenticated owner bound to ``request.state.auth_context``, if any."""
    ctx = getattr(request.state, "auth_context", None)
    if isinstance(ctx, AuthContext):
        return ctx.owner
    return None


def can_access_owner(request_owner: str | None, resource_owner: str | None) -> bool:
    """Return True iff ``request_owner`` is permitted to access ``resource_owner``."""
    return request_owner == resource_owner


def ensure_owner_access(request: Request, resource_owner: str | None) -> None:
    """Raise 403 Forbidden if the request's owner does not match ``resource_owner``."""
    req_owner = get_request_owner(request)
    if not can_access_owner(req_owner, resource_owner):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: resource belongs to a different owner",
        )


_RESERVED_JOB_PATH_SEGMENTS = {"batch", "crawl", "search", "stream", "demo", "share"}


def resolve_job_owner(job_id: str) -> tuple[bool, str | None]:
    """Return (exists, owner) for ``job_id`` across ``app.main.JOB_MANAGER`` and ``Store``."""
    main_mod = sys.modules.get("app.main")
    if main_mod is not None:
        jm = getattr(main_mod, "JOB_MANAGER", None)
        if jm is not None:
            get_owner_fn = getattr(jm, "get_owner", None)
            if callable(get_owner_fn):
                try:
                    exists, owner = get_owner_fn(job_id)
                    if exists:
                        return True, owner
                except Exception:
                    pass
            snapshots = getattr(jm, "_snapshots", None)
            if isinstance(snapshots, dict) and job_id in snapshots:
                snap = snapshots[job_id]
                if isinstance(snap, dict):
                    return True, snap.get("owner")
    try:
        store = get_store()
        record = store.fetch_run(job_id)
        if record is not None:
            return True, getattr(record, "owner", None)
    except Exception:
        pass
    return False, None


def check_job_owner_access(request: Request, job_id: str) -> None:
    """Raise 403 Forbidden if ``job_id`` exists and belongs to a different owner."""
    if not job_id or job_id in _RESERVED_JOB_PATH_SEGMENTS:
        return
    req_owner = get_request_owner(request)
    exists, job_owner = resolve_job_owner(job_id)
    if exists and not can_access_owner(req_owner, job_owner):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: resource belongs to a different owner",
        )


# CLI helper function for generating keys
def cli_generate_key(name: str, rate_limit: int | None = None, owner: str | None = None) -> None:
    """CLI function to generate a new API key.

    Usage:
        python -c "from app.auth import cli_generate_key; cli_generate_key('my-app')"
    """
    store = get_store()  # Use global store instance

    with store.session() as session:
        plain_key, api_key = create_api_key(session, name, rate_limit, owner)

        print("\n✅ API Key created successfully!")
        print(f"\nKey ID: {api_key.id}")
        print(f"Name: {api_key.name}")
        print(f"Prefix: {api_key.key_prefix}")
        print(f"Rate Limit: {api_key.rate_limit or 'None (unlimited)'}")
        print(f"Owner: {api_key.owner or 'None'}")
        print("\n🔑 API Key (save this, it won't be shown again):")
        print(f"\n  {plain_key}\n")
        print(f"Use this key in requests with header: X-API-Key: {plain_key}\n")
