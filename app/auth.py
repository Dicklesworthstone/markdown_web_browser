"""API authentication and authorization middleware."""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterator, Optional

if TYPE_CHECKING:
    from app.store import Store

from fastapi import HTTPException, Request, status
from sqlmodel import Field, Session, SQLModel, select

from app.settings import Settings, settings as global_settings


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
    is_admin: bool = Field(default=False)


class ResourceOwner(SQLModel, table=True):
    """Durable access grants available before capture artifacts exist."""

    resource: str = Field(primary_key=True)
    owner_key: str = Field(index=True)


@dataclass
class AuthContext:
    """Authentication context for the current request."""

    api_key_id: int
    api_key_name: str
    api_key_prefix: str
    rate_limit: int | None
    owner: str | None
    is_admin: bool = False

    @property
    def owner_key(self) -> str | None:
        if not self.api_key_id:
            return None
        return f"owner:{self.owner}" if self.owner else f"key:{self.api_key_id}"


AUTH_CONTEXT: ContextVar[AuthContext | None] = ContextVar("mdwb_auth", default=None)


def current_owner() -> str | None:
    context = AUTH_CONTEXT.get()
    return context.owner_key if context else None


def claim_resource(store: Store, resource: str) -> None:
    owner = current_owner()
    if owner is not None:
        with store.session() as session:
            session.add(ResourceOwner(resource=resource, owner_key=owner))
            session.commit()


def owns_resource(store: Store, resource: str, owner: str) -> bool:
    with store.session() as session:
        grant = session.get(ResourceOwner, resource)
        return grant is not None and grant.owner_key == owner


def authenticate(api_key: str | None, store: Store, settings: Settings) -> AuthContext:
    if not settings.REQUIRE_API_KEY:
        return AuthContext(0, "anonymous", "none", None, None)
    if not api_key or not re.fullmatch(r"mdwb_[0-9a-f]{32}", api_key):
        raise HTTPException(
            status_code=401,
            detail=f"Provide a valid API key in {settings.API_KEY_HEADER}.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    with store.session() as session:
        record = verify_api_key(session, api_key)
        if record is None or record.id is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid or revoked API key",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        return AuthContext(
            record.id,
            record.name,
            record.key_prefix,
            record.rate_limit,
            record.owner,
            record.is_admin,
        )


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
    *,
    is_admin: bool = False,
) -> tuple[str, APIKey]:
    """Create a new API key and store it in the database.

    Returns:
        tuple: (plain_text_key, db_record)

    Note: The plain text key is returned only once during creation.
    It cannot be retrieved later, only the hash is stored.
    """
    if rate_limit is not None and rate_limit <= 0:
        raise ValueError("rate_limit must be positive")
    plain_key = generate_api_key()
    key_hash = hash_api_key(plain_key)
    key_prefix = plain_key[:12]  # mdwb_<first 7 hex chars>

    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=name,
        rate_limit=rate_limit,
        owner=owner,
        is_admin=is_admin,
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
    if not api_key or not re.fullmatch(r"mdwb_[0-9a-f]{32}", api_key):
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
_store_lock = threading.Lock()


def get_store() -> "Store":
    """Get or create the global Store instance.

    This ensures we only create one database engine for the entire application,
    enabling proper connection pooling and avoiding expensive engine initialization
    on every request.

    Thread-safe singleton implementation using double-checked locking pattern.

    Returns:
        Store: Global store instance with reusable database engine
    """
    global _global_store

    # Fast path - if already initialized, return immediately without locking
    if _global_store is not None:
        return _global_store

    # Slow path - need to initialize, acquire lock
    with _store_lock:
        # Double-check: another thread might have initialized while we waited for lock
        if _global_store is None:
            from app.store import Store

            _global_store = Store()

    return _global_store


def get_db_session() -> Iterator[Session]:
    """FastAPI dependency to get database session.

    Uses the global Store instance to ensure database engine is reused
    across all requests, enabling proper connection pooling.

    Usage:
        @app.get("/endpoint")
        def endpoint(session: Session = Depends(get_db_session)):
            ...
    """
    store = get_store()  # Use global instance instead of creating new one
    with store.session() as session:
        yield session


async def get_auth_context(
    request: Request,
) -> AuthContext:
    """FastAPI dependency to get authentication context from request.

    Validates API key and returns authentication context.
    Raises HTTPException if authentication fails.

    Usage:
        @app.get("/protected")
        async def protected_endpoint(auth: AuthContext = Depends(get_auth_context)):
            return {"message": f"Authenticated as {auth.api_key_name}"}
    """
    context = getattr(request.state, "auth_context", None)
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )
    return context


# CLI helper function for generating keys
def cli_generate_key(
    name: str,
    rate_limit: int | None = None,
    owner: str | None = None,
    *,
    is_admin: bool = False,
) -> None:
    """CLI function to generate a new API key.

    Usage:
        python -c "from app.auth import cli_generate_key; cli_generate_key('my-app')"
    """
    store = get_store()  # Use global store instance

    with store.session() as session:
        plain_key, api_key = create_api_key(session, name, rate_limit, owner, is_admin=is_admin)

        print("\n✅ API Key created successfully!")
        print(f"\nKey ID: {api_key.id}")
        print(f"Name: {api_key.name}")
        print(f"Prefix: {api_key.key_prefix}")
        print(f"Rate Limit: {api_key.rate_limit or global_settings.RATE_LIMIT_PER_MINUTE}")
        print(f"Owner: {api_key.owner or 'None'}")
        print("\n🔑 API Key (save this, it won't be shown again):")
        print(f"\n  {plain_key}\n")
        print(
            f"Use this key in requests with header: {global_settings.API_KEY_HEADER}: {plain_key}\n"
        )
