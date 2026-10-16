"""Persistence helpers for artifacts, manifests, and sqlite-vec metadata."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
import hashlib
import json
import logging
import queue
import re
from enum import Enum
from pathlib import Path
import sqlite3
import sys
import tarfile
import threading
import types
from typing import Any, AsyncIterator, Callable, Generator, Iterable, Mapping, Sequence

import sqlite_vec
import zstandard as zstd
from sqlalchemy import Column, desc, event, or_, text
from sqlalchemy.dialects.sqlite import JSON as SQLITE_JSON
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import Field, Session, SQLModel, col, create_engine, select

from app.tiler import TileSlice

LOGGER = logging.getLogger(__name__)

try:  # pragma: no cover - imported for typing/runtime parity
    from app.warning_log import summarize_seam_markers
except ImportError:  # pragma: no cover - typing fallback

    def summarize_seam_markers(
        markers: Any,
        *,
        events: Any = None,
        sample_limit: int = 3,
    ) -> dict[str, Any] | None:
        return None


from .embeddings import EMBEDDING_DIM, EmbeddingMatch, search_embeddings  # noqa: E402
from .settings import load_config  # noqa: E402

# Import models so they're registered with SQLModel.metadata
from app.auth import APIKey  # noqa: E402, F401 - imported for table registration

DEFAULT_JOB_STATE = "BROWSER_STARTING"

_TIMESTAMP_FORMAT = "%Y-%m-%d_%H%M%S"
_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


class RunRecord(SQLModel, table=True):
    """Metadata for each capture run tracked in SQLite."""

    __tablename__ = "runs"

    id: str = Field(primary_key=True)
    url: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str = Field(default=DEFAULT_JOB_STATE)
    cache_path: str
    manifest_path: str
    ocr_provider: str | None = None
    ocr_model: str | None = None
    backend_id: str | None = None
    backend_mode: str | None = None
    hardware_path: str | None = None
    fallback_chain: list[str] | None = Field(default=None, sa_column=Column(SQLITE_JSON))
    cft_label: str | None = None
    cft_version: str | None = None
    playwright_version: str | None = None
    browser_transport: str | None = None
    server_runtime: str | None = None
    screenshot_style_hash: str | None = None
    long_side_px: int | None = None
    device_scale_factor: int | None = None
    tiles_total: int | None = None
    capture_ms: int | None = None
    ocr_ms: int | None = None
    stitch_ms: int | None = None
    sweep_shrink_events: int | None = None
    sweep_retry_attempts: int | None = None
    sweep_overlap_pairs: int | None = None
    overlap_match_ratio: float | None = None
    validation_failure_count: int | None = None
    profile_id: str | None = None
    owner: str | None = Field(default=None, index=True)
    cache_key: str | None = None
    cache_hit: bool = Field(default=False)
    cache_source_job_id: str | None = None
    cache_invalidated: bool = Field(default=False)
    seam_marker_count: int | None = None
    seam_hash_count: int | None = None
    seam_markers_summary: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(SQLITE_JSON),
    )
    tags: list[str] | None = Field(
        default=None,
        sa_column=Column(SQLITE_JSON),
        description="Free-form labels (e.g. 'dataset:2026-q1') for agent queries",
    )
    embedding_model: str | None = None
    embedding_dim: int | None = None
    purge_at: datetime | None = None


class LinkRecord(SQLModel, table=True):
    """Persisted DOM/OCR links for quick agent retrieval."""

    __tablename__ = "links"

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(foreign_key="runs.id")
    href: str
    text: str
    rel: str | None = None
    source: str = Field(default="dom", description="dom|ocr|hybrid")


class WebhookRecord(SQLModel, table=True):
    """Webhook registrations stored alongside run metadata."""

    __tablename__ = "webhooks"

    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(foreign_key="runs.id")
    url: str
    events: list[str] = Field(default_factory=list, sa_column=Column(SQLITE_JSON))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class StorageConfig:
    """Resolved filesystem + database locations."""

    cache_root: Path
    db_path: Path

    @classmethod
    def from_env(cls) -> StorageConfig:
        cfg = load_config()
        cache_root = Path(cfg("CACHE_ROOT", default=".cache"))
        db_path = Path(cfg("RUNS_DB_PATH", default="runs.db"))
        return cls(cache_root=cache_root, db_path=db_path)


@dataclass(frozen=True)
class RunPaths:
    """Filesystem locations for a specific capture run."""

    root: Path
    artifacts_dir: Path
    tiles_dir: Path
    manifest_path: Path
    markdown_path: Path
    links_path: Path
    dom_snapshot_path: Path
    artifacts_manifest_path: Path
    cache_key: str | None = None

    @classmethod
    def from_url(
        cls,
        *,
        url: str,
        started_at: datetime,
        config: StorageConfig,
        cache_key: str | None = None,
    ) -> RunPaths:
        host, slug = _split_url(url)
        timestamp = started_at.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)
        if cache_key:
            bucket, normalized_key = _cache_segments(cache_key)
            run_root = (
                config.cache_root / host / slug / "cache" / bucket / normalized_key / timestamp
            )
        else:
            run_root = config.cache_root / host / slug / timestamp
        artifacts = run_root / "artifact"
        tiles = artifacts / "tiles"
        return cls(
            root=run_root,
            artifacts_dir=artifacts,
            tiles_dir=tiles,
            manifest_path=run_root / "manifest.json",
            markdown_path=run_root / "out.md",
            links_path=run_root / "links.json",
            dom_snapshot_path=artifacts / "dom.html",
            artifacts_manifest_path=artifacts / "artifacts.json",
            cache_key=cache_key,
        )

    def ensure_directories(self) -> None:
        self.tiles_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_manifest_path.parent.mkdir(parents=True, exist_ok=True)

    def bundle_path(self) -> Path:
        return self.root / "bundle.tar.zst"

    @property
    def dom_overlay_path(self) -> Path:
        return self.artifacts_dir / "dom_overlay.json"

    @classmethod
    def from_record(cls, record: RunRecord | Any) -> RunPaths:
        raw_root = getattr(record, "cache_path", None) or getattr(record, "artifact_root", None)
        raw_manifest = getattr(record, "manifest_path", None)
        if not raw_root and raw_manifest:
            raw_root = str(Path(raw_manifest).parent)
        root = Path(raw_root or ".")
        manifest_path = Path(raw_manifest) if raw_manifest else (root / "manifest.json")
        return cls(
            root=root,
            artifacts_dir=root / "artifact",
            tiles_dir=root / "artifact" / "tiles",
            manifest_path=manifest_path,
            markdown_path=root / "out.md",
            links_path=root / "links.json",
            dom_snapshot_path=root / "artifact" / "dom.html",
            artifacts_manifest_path=root / "artifact" / "artifacts.json",
            cache_key=getattr(record, "cache_key", None),
        )


class Store:
    """Facade around SQLite + filesystem persistence."""

    def __init__(self, config: StorageConfig | None = None, cache_ttl_hours: int = 24) -> None:
        self.config = config or StorageConfig.from_env()
        self.cache_ttl_hours = cache_ttl_hours
        self.config.cache_root.mkdir(parents=True, exist_ok=True)
        self.engine = _create_engine(self.config.db_path)
        self.async_engine = _create_async_engine(self.config.db_path)
        self._async_sessionmaker = async_sessionmaker(
            self.async_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        self._io_semaphore = asyncio.Semaphore(32)
        self._active_task_sessions: dict[int, int] = {}
        self.session_sharing_violations: int = 0
        SQLModel.metadata.create_all(self.engine)
        self._ensure_vec_table()
        self._ensure_run_columns()
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        """Create workload-driven indexes on runs, links, webhooks, and embedding metadata."""
        index_ddls = (
            "CREATE INDEX IF NOT EXISTS idx_runs_status_started_at ON runs (status, started_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_runs_cache_lookup ON runs (cache_key, status, started_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_runs_owner_started_at ON runs (owner, started_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_runs_profile_started_at ON runs (profile_id, started_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_runs_cache_path ON runs (cache_path)",
            "CREATE INDEX IF NOT EXISTS idx_links_run_id ON links (run_id)",
            "CREATE INDEX IF NOT EXISTS idx_webhooks_job_id ON webhooks (job_id)",
            "CREATE INDEX IF NOT EXISTS idx_section_embedding_meta_run_model ON section_embedding_meta (run_id, model)",
            "CREATE INDEX IF NOT EXISTS idx_section_embedding_meta_model_dim ON section_embedding_meta (model, dim)",
        )
        with self.engine.begin() as conn:
            for ddl in index_ddls:
                conn.exec_driver_sql(ddl)

    def _ensure_vec_table(self) -> None:
        ddl = text(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS section_embeddings USING vec0(
                run_id TEXT NOT NULL,
                section_id TEXT NOT NULL,
                tile_start INTEGER,
                tile_end INTEGER,
                embedding FLOAT[{dim}]
            )
            """.format(dim=EMBEDDING_DIM)
        )
        with self.engine.begin() as conn:
            conn.exec_driver_sql(ddl.text)
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS section_embedding_meta (
                    run_id TEXT NOT NULL,
                    section_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    PRIMARY KEY (run_id, section_id)
                )
                """
            )
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

    def _ensure_run_columns(self) -> None:
        """Add newly introduced run columns when upgrading existing databases."""

        expected_types = {
            "backend_id": "TEXT",
            "backend_mode": "TEXT",
            "hardware_path": "TEXT",
            "fallback_chain": "JSON",
            "sweep_shrink_events": "INTEGER",
            "sweep_retry_attempts": "INTEGER",
            "sweep_overlap_pairs": "INTEGER",
            "overlap_match_ratio": "REAL",
            "validation_failure_count": "INTEGER",
            "profile_id": "TEXT",
            "owner": "TEXT",
            "cache_key": "TEXT",
            "cache_hit": "INTEGER",
            "cache_source_job_id": "TEXT",
            "cache_invalidated": "INTEGER",
            "server_runtime": "TEXT",
            "seam_marker_count": "INTEGER",
            "seam_hash_count": "INTEGER",
            "seam_markers_summary": "JSON",
            "tags": "JSON",
            "embedding_model": "TEXT",
            "embedding_dim": "INTEGER",
            "purge_at": "TEXT",
        }
        # Valid SQLite column types for validation
        valid_types = {"INTEGER", "TEXT", "REAL", "JSON", "BLOB", "NUMERIC"}

        with self.engine.begin() as conn:
            existing = {
                row[1]  # column name
                for row in conn.exec_driver_sql("PRAGMA table_info(runs)")
            }
            for column, ddl in expected_types.items():
                if column not in existing:
                    # Validate column name and type to prevent SQL injection
                    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", column):
                        raise ValueError(f"Invalid column name: {column}")
                    if ddl not in valid_types:
                        raise ValueError(f"Invalid column type: {ddl}")

                    # Safe to use f-string now that inputs are validated
                    conn.exec_driver_sql(f"ALTER TABLE runs ADD COLUMN {column} {ddl}")

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        with Session(self.engine) as session:
            yield session

    @asynccontextmanager
    async def async_session(self) -> AsyncIterator[AsyncSession]:
        """Yield a task-owned AsyncSession that never shares state across coroutines."""
        current_task = asyncio.current_task()
        task_id = id(current_task) if current_task is not None else threading.get_ident()
        async with self._async_sessionmaker() as session:
            sess_id = id(session)
            existing_owner = self._active_task_sessions.get(sess_id)
            if existing_owner is not None and existing_owner != task_id:
                self.session_sharing_violations += 1
                raise RuntimeError("Concurrent tasks must not share an AsyncSession")
            self._active_task_sessions[sess_id] = task_id
            try:
                yield session
            except BaseException:
                try:
                    await session.rollback()
                except Exception:
                    pass
                raise
            finally:
                self._active_task_sessions.pop(sess_id, None)

    @property
    def db_path(self) -> Path:
        return self.config.db_path

    async def aclose(self) -> None:
        """Await async engine teardown and dispose synchronous connection pool."""
        await self.async_engine.dispose()
        self.engine.dispose()

    async def dispose(self) -> None:
        """Alias for aclose()."""
        await self.aclose()

    def allocate_run(
        self,
        *,
        job_id: str,
        url: str,
        started_at: datetime,
        profile_id: str | None = None,
        owner: str | None = None,
        cache_key: str | None = None,
        tags: list[str] | None = None,
        purge_at: datetime | None = None,
    ) -> RunPaths:
        paths = RunPaths.from_url(
            url=url, started_at=started_at, config=self.config, cache_key=cache_key
        )
        paths.ensure_directories()
        effective_purge_at = purge_at
        if effective_purge_at is None and self.cache_ttl_hours > 0:
            effective_purge_at = started_at.astimezone(timezone.utc) + timedelta(
                hours=self.cache_ttl_hours
            )
        record = RunRecord(
            id=job_id,
            url=url,
            started_at=started_at,
            cache_path=str(paths.root),
            manifest_path=str(paths.manifest_path),
            profile_id=profile_id,
            owner=owner,
            cache_key=cache_key,
            cache_hit=False,
            tags=list(tags) if tags else None,
            purge_at=effective_purge_at,
        )
        with self.session() as session:
            session.add(record)
            session.commit()
        return paths

    def set_tags(self, *, job_id: str, tags: list[str]) -> None:
        """Replace the tag set on a run (idempotent)."""
        with self.session() as session:
            run = session.get(RunRecord, job_id)
            if run is None:
                return
            run.tags = list(tags)
            session.add(run)
            session.commit()

    def add_tag(self, *, job_id: str, tag: str) -> None:
        """Append a single tag to a run's tag set."""
        with self.session() as session:
            run = session.get(RunRecord, job_id)
            if run is None:
                return
            current = list(run.tags or [])
            if tag not in current:
                current.append(tag)
            run.tags = current
            session.add(run)
            session.commit()

    def list_runs(
        self,
        *,
        state: str | None = None,
        profile_id: str | None = None,
        owner: str | None = None,
        enforce_owner: bool = False,
        url_contains: str | None = None,
        cache_hit: bool | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RunRecord], int]:
        """Filter runs by any combination of (state, profile, owner, url substring, tag)."""
        with self.session() as session:
            stmt = select(RunRecord)
            count_stmt = select(RunRecord)
            if enforce_owner or owner is not None:
                if owner is not None:
                    stmt = stmt.where(RunRecord.owner == owner)
                    count_stmt = count_stmt.where(RunRecord.owner == owner)
                else:
                    stmt = stmt.where(RunRecord.owner == None)  # noqa: E711
                    count_stmt = count_stmt.where(RunRecord.owner == None)  # noqa: E711
            if state:
                stmt = stmt.where(RunRecord.status == state)
                count_stmt = count_stmt.where(RunRecord.status == state)
            if profile_id:
                stmt = stmt.where(RunRecord.profile_id == profile_id)
                count_stmt = count_stmt.where(RunRecord.profile_id == profile_id)
            if url_contains:
                stmt = stmt.where(col(RunRecord.url).contains(url_contains))
                count_stmt = count_stmt.where(col(RunRecord.url).contains(url_contains))
            if tag:
                try:
                    stmt = stmt.where(
                        text("EXISTS (SELECT 1 FROM json_each(runs.tags) WHERE json_each.value = :tag)").bindparams(tag=tag)
                    )
                    count_stmt = count_stmt.where(
                        text("EXISTS (SELECT 1 FROM json_each(runs.tags) WHERE json_each.value = :tag)").bindparams(tag=tag)
                    )
                except Exception:
                    pass
            if cache_hit is not None:
                stmt = stmt.where(RunRecord.cache_hit == bool(cache_hit))
                count_stmt = count_stmt.where(RunRecord.cache_hit == bool(cache_hit))
            stmt = stmt.order_by(desc(col(RunRecord.started_at))).offset(max(0, offset)).limit(max(1, limit))
            rows = list(session.exec(stmt))
            total = len(list(session.exec(count_stmt)))
            return rows, total

    def find_cache_hit(
        self,
        cache_key: str | None,
        *,
        owner: str | None = None,
    ) -> RunRecord | None:
        if not cache_key or self.cache_ttl_hours <= 0:
            return None

        # Calculate TTL cutoff time (make naive for SQLite comparison since SQLite strips timezone)
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=self.cache_ttl_hours)
        cutoff_time = cutoff_time.replace(tzinfo=None)

        with self.session() as session:
            owner_clause = (
                RunRecord.owner == owner
                if owner is not None
                else RunRecord.owner == None  # noqa: E711
            )
            statement = (
                select(RunRecord)
                .where(
                    RunRecord.cache_key == cache_key,
                    owner_clause,
                    RunRecord.status == "DONE",
                    col(RunRecord.finished_at).is_not(None),
                    # Only return cache hits within TTL window
                    col(RunRecord.finished_at) >= cutoff_time,
                )
                .order_by(desc(col(RunRecord.finished_at)))
            )
            for record in session.exec(statement):
                if bool(getattr(record, "cache_invalidated", False)):
                    continue
                if record.validation_failure_count not in (None, 0):
                    continue
                try:
                    manifest_path = Path(record.manifest_path)
                except TypeError:
                    continue
                if not manifest_path.exists():
                    continue
                try:
                    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(manifest_data, Mapping):
                    continue
                if manifest_data.get("validation_failures"):
                    continue
                if manifest_data.get("error"):
                    continue
                sweep_stats = manifest_data.get("sweep_stats")
                if isinstance(sweep_stats, Mapping) and bool(
                    sweep_stats.get("truncated_by_max_steps")
                ):
                    continue
                return record
        return None

    def register_cached_run(
        self,
        *,
        job_id: str,
        source: RunRecord,
        owner: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        finished_at = source.finished_at or datetime.now(timezone.utc)
        cloned = RunRecord(
            id=job_id,
            url=source.url,
            started_at=finished_at,
            finished_at=finished_at,
            status="DONE",
            cache_path=source.cache_path,
            manifest_path=source.manifest_path,
            ocr_provider=source.ocr_provider,
            ocr_model=source.ocr_model,
            backend_id=source.backend_id,
            backend_mode=source.backend_mode,
            hardware_path=source.hardware_path,
            fallback_chain=list(source.fallback_chain) if source.fallback_chain else None,
            cft_label=source.cft_label,
            cft_version=source.cft_version,
            playwright_version=source.playwright_version,
            browser_transport=source.browser_transport,
            server_runtime=source.server_runtime,
            screenshot_style_hash=source.screenshot_style_hash,
            long_side_px=source.long_side_px,
            device_scale_factor=source.device_scale_factor,
            tiles_total=source.tiles_total,
            capture_ms=source.capture_ms,
            ocr_ms=source.ocr_ms,
            stitch_ms=source.stitch_ms,
            sweep_shrink_events=source.sweep_shrink_events,
            sweep_retry_attempts=source.sweep_retry_attempts,
            sweep_overlap_pairs=source.sweep_overlap_pairs,
            overlap_match_ratio=source.overlap_match_ratio,
            validation_failure_count=source.validation_failure_count,
            profile_id=source.profile_id,
            owner=owner if owner is not None else source.owner,
            cache_key=source.cache_key,
            cache_hit=True,
            cache_source_job_id=source.id,
            cache_invalidated=False,
            seam_marker_count=source.seam_marker_count,
            seam_hash_count=source.seam_hash_count,
            seam_markers_summary=(
                dict(source.seam_markers_summary)
                if isinstance(source.seam_markers_summary, Mapping)
                else None
            ),
            tags=list(tags) if tags is not None else (list(source.tags) if source.tags else None),
            embedding_model=source.embedding_model,
            embedding_dim=source.embedding_dim,
            purge_at=source.purge_at
            or (
                finished_at.astimezone(timezone.utc) + timedelta(hours=self.cache_ttl_hours)
                if self.cache_ttl_hours > 0
                else None
            ),
        )
        with self.session() as session:
            session.add(cloned)
            conn = session.connection()
            conn.exec_driver_sql(
                "DELETE FROM section_embeddings WHERE run_id = :job_id",
                {"job_id": job_id},
            )
            conn.exec_driver_sql(
                "DELETE FROM section_embedding_meta WHERE run_id = :job_id",
                {"job_id": job_id},
            )
            source_rows = conn.exec_driver_sql(
                "SELECT section_id, tile_start, tile_end, embedding "
                "FROM section_embeddings WHERE run_id = :source_id",
                {"source_id": source.id},
            ).fetchall()
            for section_id, tile_start, tile_end, embedding in source_rows:
                conn.exec_driver_sql(
                    "INSERT INTO section_embeddings "
                    "(run_id, section_id, tile_start, tile_end, embedding) "
                    "VALUES (:run_id, :section_id, :tile_start, :tile_end, :embedding)",
                    {
                        "run_id": job_id,
                        "section_id": section_id,
                        "tile_start": tile_start,
                        "tile_end": tile_end,
                        "embedding": embedding,
                    },
                )
            meta_rows = conn.exec_driver_sql(
                "SELECT section_id, model, dim "
                "FROM section_embedding_meta WHERE run_id = :source_id",
                {"source_id": source.id},
            ).fetchall()
            for section_id, model, dim in meta_rows:
                conn.exec_driver_sql(
                    "INSERT OR REPLACE INTO section_embedding_meta "
                    "(run_id, section_id, model, dim) "
                    "VALUES (:run_id, :section_id, :model, :dim)",
                    {
                        "run_id": job_id,
                        "section_id": section_id,
                        "model": model,
                        "dim": dim,
                    },
                )
            conn.exec_driver_sql(
                "DELETE FROM links WHERE run_id = :job_id",
                {"job_id": job_id},
            )
            conn.exec_driver_sql(
                "INSERT INTO links (run_id, href, text, rel, source) "
                "SELECT :job_id, href, text, rel, source FROM links WHERE run_id = :source_id",
                {"job_id": job_id, "source_id": source.id},
            )
            session.commit()

    def invalidate_cache_key(self, target: str) -> int:
        """Invalidate cached runs matching a URL or cache_key without deleting artifact files."""
        raw = str(target or "").strip()
        if not raw:
            return 0
        normalized_target = _normalize_cache_url(raw)
        invalidated = 0
        with self.session() as session:
            rows = list(session.exec(select(RunRecord)))
            for record in rows:
                if bool(getattr(record, "cache_invalidated", False)):
                    continue
                matched = (
                    record.cache_key == raw
                    or record.url == raw
                    or _normalize_cache_url(record.url) == normalized_target
                )
                if not matched:
                    continue
                record.cache_invalidated = True
                session.add(record)
                invalidated += 1
            if invalidated > 0:
                session.commit()
        return invalidated

    def clear_cache(self) -> int:
        """Mark all cached runs as invalidated without deleting artifact files."""
        cleared = 0
        with self.session() as session:
            rows = list(session.exec(select(RunRecord)))
            for record in rows:
                if bool(getattr(record, "cache_invalidated", False)):
                    continue
                record.cache_invalidated = True
                session.add(record)
                cleared += 1
            if cleared > 0:
                session.commit()
        return cleared

    def cache_stats(self) -> dict[str, Any]:
        """Compute live cache statistics across persisted runs."""
        enabled = bool(self.cache_ttl_hours > 0)
        ttl_seconds = max(0, int(self.cache_ttl_hours * 3600))
        cutoff_time = (
            datetime.now(timezone.utc) - timedelta(hours=max(0, self.cache_ttl_hours))
        ).replace(tzinfo=None)
        hits = 0
        misses = 0
        invalidated = 0
        active_keys: set[str] = set()
        with self.session() as session:
            rows = list(session.exec(select(RunRecord)))
            for record in rows:
                is_invalidated = bool(getattr(record, "cache_invalidated", False))
                if is_invalidated:
                    invalidated += 1
                if record.status != "DONE":
                    continue
                if bool(record.cache_hit):
                    hits += 1
                else:
                    misses += 1
                if not enabled or is_invalidated or bool(record.cache_hit):
                    continue
                if not record.cache_key:
                    continue
                if record.validation_failure_count not in (None, 0):
                    continue
                finished = record.finished_at
                if finished is None:
                    continue
                finished_naive = finished.replace(tzinfo=None) if finished.tzinfo else finished
                if finished_naive < cutoff_time:
                    continue
                try:
                    if not Path(record.manifest_path).exists():
                        continue
                except TypeError:
                    continue
                active_keys.add(record.cache_key)
        return {
            "entries": len(active_keys),
            "hits": hits,
            "misses": misses,
            "invalidated": invalidated,
            "ttl_seconds": ttl_seconds,
            "enabled": enabled,
        }

    def register_webhook(self, *, job_id: str, url: str, events: Sequence[str]) -> WebhookRecord:
        normalized_events = list(events)
        with self.session() as session:
            run = session.get(RunRecord, job_id)
            if not run:
                raise KeyError(f"Run {job_id} not found")
            record = WebhookRecord(job_id=job_id, url=url, events=normalized_events)
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def list_webhooks(self, job_id: str) -> list[WebhookRecord]:
        with self.session() as session:
            run = session.get(RunRecord, job_id)
            if not run:
                raise KeyError(f"Run {job_id} not found")
            statement = (
                select(WebhookRecord)
                .where(WebhookRecord.job_id == job_id)
                .order_by(col(WebhookRecord.created_at))
            )
            return list(session.exec(statement).all())

    def delete_webhooks(
        self,
        *,
        job_id: str,
        webhook_id: int | None = None,
        url: str | None = None,
    ) -> int:
        if webhook_id is None and not url:
            raise ValueError("Provide webhook_id or url for deletion")
        with self.session() as session:
            run = session.get(RunRecord, job_id)
            if not run:
                raise KeyError(f"Run {job_id} not found")
            statement = select(WebhookRecord).where(WebhookRecord.job_id == job_id)
            if webhook_id is not None:
                statement = statement.where(WebhookRecord.id == webhook_id)
            if url:
                statement = statement.where(WebhookRecord.url == url)
            records = list(session.exec(statement))
            if not records:
                return 0
            for record in records:
                session.delete(record)
            session.commit()
            return len(records)

    def delete_webhook(
        self,
        job_id: str,
        *,
        webhook_id: int | None = None,
        url: str | None = None,
    ) -> int:
        if webhook_id is None and not url:
            raise ValueError("Provide a webhook id or url to delete")
        with self.session() as session:
            run = session.get(RunRecord, job_id)
            if not run:
                raise KeyError(f"Run {job_id} not found")
            statement = select(WebhookRecord).where(WebhookRecord.job_id == job_id)
            if webhook_id is not None:
                statement = statement.where(WebhookRecord.id == webhook_id)
            if url:
                statement = statement.where(WebhookRecord.url == url)
            records = session.exec(statement).all()
            for record in records:
                session.delete(record)
            session.commit()
            return len(records)

    def dom_snapshot_path(self, *, job_id: str) -> Path:
        """Return the filesystem path for a run's DOM snapshot."""

        with self.session() as session:
            record = session.get(RunRecord, job_id)
            if not record:
                raise KeyError(f"Run {job_id} not found")
            return RunPaths.from_record(record).dom_snapshot_path

    def update_status(
        self,
        *,
        job_id: str,
        status: str | Enum,
        finished_at: datetime | None = None,
    ) -> None:
        with self.session() as session:
            record = session.get(RunRecord, job_id)
            if not record:
                raise KeyError(f"Run {job_id} not found")
            record.status = _coerce_state(status)
            if finished_at:
                record.finished_at = finished_at
            session.add(record)
            session.commit()

    def write_manifest(self, *, job_id: str, manifest: Mapping[str, object] | Any) -> Path:
        with self.session() as session:
            record = session.get(RunRecord, job_id)
            if not record:
                raise KeyError(f"Run {job_id} not found")
            manifest_dict = _manifest_to_dict(manifest)
            _normalize_manifest_cache_key(manifest_dict)
            manifest_path = Path(record.manifest_path)
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(manifest_dict, indent=2), encoding="utf-8")
            _apply_manifest_metadata(record, manifest_dict)
            session.add(record)
            session.commit()
            return manifest_path

    def write_dom_snapshot(self, *, job_id: str, html: bytes | None) -> Path | None:
        if not html:
            return None
        record = self.fetch_run(job_id)
        if not record:
            raise KeyError(f"Run {job_id} not found")
        paths = RunPaths.from_record(record)
        snapshot_path = paths.dom_snapshot_path
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_bytes(html)
        return snapshot_path

    def write_dom_overlay(
        self, *, job_id: str, overlay: Sequence[Mapping[str, object]] | None
    ) -> Path | None:
        if overlay is None:
            return None
        record = self.fetch_run(job_id)
        if not record:
            raise KeyError(f"Run {job_id} not found")
        paths = RunPaths.from_record(record)
        overlay_path = paths.dom_overlay_path
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [dict(item) for item in overlay]
        overlay_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return overlay_path

    def read_dom_overlay(self, job_id: str) -> list[dict[str, Any]]:
        record = self.fetch_run(job_id)
        if not record:
            raise KeyError(f"Run {job_id} not found")
        paths = RunPaths.from_record(record)
        overlay_path = paths.dom_overlay_path
        if not overlay_path.exists():
            return []
        try:
            raw = json.loads(overlay_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return [dict(item) for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []

    def write_tiles(
        self,
        *,
        job_id: str,
        tiles: Sequence[TileSlice],
        abandoned_tiles: Sequence[TileSlice] | None = None,
    ) -> list[dict[str, Any]]:
        if not tiles and not abandoned_tiles:
            return []
        record = self.fetch_run(job_id)
        if not record:
            raise KeyError(f"Run {job_id} not found")
        paths = RunPaths.from_record(record)
        paths.ensure_directories()
        if abandoned_tiles:
            abandoned_dir = paths.artifacts_dir / "abandoned"
            abandoned_dir.mkdir(parents=True, exist_ok=True)
            for idx, ab_tile in enumerate(abandoned_tiles):
                ab_path = abandoned_dir / f"abandoned_{idx:04d}_tile_{ab_tile.index:04d}.png"
                ab_path.write_bytes(ab_tile.png_bytes)
        if not tiles:
            return []
        artifacts: list[dict[str, Any]] = []
        for tile in tiles:
            tile_path = paths.tiles_dir / f"tile_{tile.index:04d}.png"
            tile_path.write_bytes(tile.png_bytes)
            artifacts.append(
                {
                    "index": tile.index,
                    "path": str(tile_path.relative_to(paths.root)),
                    "sha256": tile.sha256,
                    "width": tile.width,
                    "height": tile.height,
                    "scale": tile.scale,
                    "scale_x": tile.scale_x,
                    "scale_y": tile.scale_y,
                    "device_scale_factor": tile.device_scale_factor,
                    "source_y_offset": tile.source_y_offset,
                    "viewport_y_offset": tile.viewport_y_offset,
                    "css_y_offset": tile.css_y_offset,
                    "css_height": tile.css_height,
                    "source_crop_box": list(tile.source_crop_box),
                    "source_width": tile.source_width,
                    "source_height": tile.source_height,
                    "coordinate_space": tile.coordinate_space,
                    "overlap_px": tile.overlap_px,
                    "top_overlap_px": tile.top_overlap_px,
                    "bottom_overlap_px": tile.bottom_overlap_px,
                    "top_overlap_sha256": tile.top_overlap_sha256,
                    "bottom_overlap_sha256": tile.bottom_overlap_sha256,
                }
            )

        self._write_artifacts_manifest(paths, artifacts)
        return artifacts

    def _write_artifacts_manifest(
        self, paths: RunPaths, artifacts: Sequence[Mapping[str, Any]]
    ) -> None:
        payload = [dict(item) for item in artifacts]
        paths.artifacts_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        paths.artifacts_manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def write_links(self, *, job_id: str, links: Sequence[Mapping[str, object]]) -> Path:
        paths = self._paths_for_job(job_id)
        payload = [dict(link) for link in links]
        paths.links_path.parent.mkdir(parents=True, exist_ok=True)
        paths.links_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            self.insert_links(job_id=job_id, links=payload, replace=True)
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.warning("Persisting LinkRecord rows for %s failed: %s", job_id, exc)
        return paths.links_path

    def write_markdown(
        self,
        *,
        job_id: str,
        content: str,
        embed_model: str | None = None,
    ) -> Path:
        paths = self._paths_for_job(job_id)
        paths.markdown_path.parent.mkdir(parents=True, exist_ok=True)
        paths.markdown_path.write_text(content, encoding="utf-8")
        if content and content.strip():
            try:
                self.index_markdown(job_id=job_id, markdown=content, model=embed_model)
            except Exception as exc:  # pragma: no cover - defensive logging
                LOGGER.warning("Auto-indexing markdown for %s failed: %s", job_id, exc)
        return paths.markdown_path

    def index_markdown(
        self,
        *,
        job_id: str,
        markdown: str,
        model: str | None = None,
        tiles_total: int | None = None,
    ) -> int:
        """Extract sections from ``markdown`` and persist their embeddings for ``job_id``."""
        from app.embedders import get_embedder
        from .embeddings import DEFAULT_EMBEDDING_MODEL, SectionEmbedding

        if not markdown or not markdown.strip():
            return 0
        target_model = model or DEFAULT_EMBEDDING_MODEL
        embedder = get_embedder(target_model)
        sections = _extract_sections_for_embedding(
            markdown,
            job_id=job_id,
            tiles_total=tiles_total,
        )
        if not sections:
            return 0
        section_embeddings: list[SectionEmbedding] = []
        for entry in sections:
            vec = embedder.embed(entry["text"])
            section_embeddings.append(
                SectionEmbedding(
                    section_id=entry["section_id"],
                    tile_start=entry["tile_start"],
                    tile_end=entry["tile_end"],
                    vector=vec,
                    model=target_model,
                    dim=len(vec),
                )
            )
        self.upsert_embeddings(
            run_id=job_id,
            sections=section_embeddings,
            model=target_model,
        )
        return len(section_embeddings)

    def read_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        paths = self._paths_for_job(job_id)
        manifest_path = paths.artifacts_manifest_path
        if manifest_path.exists():
            try:
                return json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        artifacts: list[dict[str, Any]] = []
        if paths.tiles_dir.exists():
            for tile_path in sorted(paths.tiles_dir.glob("tile_*.png")):
                index = _parse_tile_index(tile_path.name)
                artifacts.append(
                    {
                        "index": index,
                        "path": str(tile_path.relative_to(paths.root)),
                    }
                )
        return artifacts

    def read_manifest(self, job_id: str) -> dict[str, Any]:
        record = self.fetch_run(job_id)
        if not record:
            raise KeyError(f"Run {job_id} not found")
        manifest_path = Path(record.manifest_path)
        if not manifest_path.exists():
            raise FileNotFoundError(manifest_path)
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    def read_markdown(self, job_id: str) -> str:
        paths = self._paths_for_job(job_id)
        if not paths.markdown_path.exists():
            raise FileNotFoundError(paths.markdown_path)
        return paths.markdown_path.read_text(encoding="utf-8")

    def read_links(self, job_id: str) -> list[dict[str, Any]]:
        paths = self._paths_for_job(job_id)
        if paths.links_path.exists():
            raw = json.loads(paths.links_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [dict(item) for item in raw if isinstance(item, Mapping)]
            if isinstance(raw, Mapping):
                anchors = raw.get("anchors") or raw.get("links") or []
                if isinstance(anchors, list):
                    return [dict(item) for item in anchors if isinstance(item, Mapping)]
            return []
        with self.session() as session:
            stmt = (
                select(LinkRecord)
                .where(LinkRecord.run_id == job_id)
                .order_by(col(LinkRecord.id).asc())
            )
            rows = list(session.exec(stmt))
            if not rows:
                return []
            return [
                {
                    "href": row.href,
                    "text": row.text,
                    "rel": row.rel.split() if isinstance(row.rel, str) and row.rel.strip() else [],
                    "source": row.source,
                }
                for row in rows
            ]

    def resolve_artifact(self, job_id: str, relative_path: str) -> Path:
        paths = self._paths_for_job(job_id)
        root = paths.root.resolve()
        clean_rel = str(relative_path or "").lstrip("/")
        if clean_rel == "bundle.tar.zst" and not (paths.root / "bundle.tar.zst").exists():
            if paths.manifest_path.exists():
                return self.build_bundle(job_id=job_id)
        for base in (paths.root, paths.artifacts_dir):
            target = (base / clean_rel).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                raise FileNotFoundError(relative_path)
            if target.exists() and target.is_file():
                return target
        raise FileNotFoundError(paths.root / clean_rel)

    def insert_links(
        self,
        *,
        job_id: str,
        links: Iterable[Mapping[str, object]],
        replace: bool = True,
    ) -> None:
        def _coerce_rel(value: object | None) -> str | None:
            if value is None:
                return None
            if isinstance(value, str):
                return value
            if isinstance(value, (list, tuple, set)):
                tokens = [str(token).strip() for token in value if token]
                return " ".join(token for token in tokens if token)
            return str(value)

        records = [
            LinkRecord(
                run_id=job_id,
                href=str(item.get("href", "")),
                text=str(item.get("text") or ""),
                rel=_coerce_rel(item.get("rel")),
                source=str(item.get("source") or "dom"),
            )
            for item in links
            if isinstance(item, Mapping)
        ]
        with self.session() as session:
            if replace:
                session.connection().exec_driver_sql(
                    "DELETE FROM links WHERE run_id = :job_id",
                    {"job_id": job_id},
                )
            if records:
                session.add_all(records)
            session.commit()

    def cache_path_references(self, job_id: str) -> list[str]:
        """Return other job IDs that share the same cache_path or manifest_path."""
        with self.session() as session:
            record = session.get(RunRecord, job_id)
            if record is None:
                return []
            stmt = select(RunRecord.id).where(
                col(RunRecord.id) != job_id,
                or_(
                    col(RunRecord.cache_path) == record.cache_path,
                    col(RunRecord.manifest_path) == record.manifest_path,
                    col(RunRecord.cache_source_job_id) == job_id,
                ),
            )
            return sorted(str(jid) for jid in session.exec(stmt) if jid)

    def inventory_artifacts(self, job_id: str) -> dict[str, Any]:
        """Enumerate and verify on-disk artifacts for ``job_id`` using RunPaths as authority."""
        record = self.fetch_run(job_id)
        if record is None:
            raise KeyError(f"Run {job_id} not found")
        paths = RunPaths.from_record(record)
        root = paths.root
        if not paths.manifest_path.exists():
            raise FileNotFoundError(paths.manifest_path)
        try:
            manifest_data = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Corrupt manifest JSON for {job_id}: {exc}") from exc
        if not isinstance(manifest_data, Mapping):
            raise ValueError(f"Corrupt manifest payload for {job_id}: expected JSON object")

        expected_tile_shas: dict[str, str] = {}
        if paths.artifacts_manifest_path.exists():
            try:
                raw_artifacts = json.loads(paths.artifacts_manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Corrupt artifacts.json for {job_id}: {exc}") from exc
            if isinstance(raw_artifacts, list):
                for item in raw_artifacts:
                    if not isinstance(item, Mapping):
                        continue
                    rel = str(item.get("path") or "").replace("\\", "/").strip()
                    if not rel:
                        continue
                    tile_file = root / rel
                    if not tile_file.exists():
                        raise FileNotFoundError(f"Missing expected tile artifact: {rel}")
                    sha = str(item.get("sha256") or "").strip()
                    if sha:
                        expected_tile_shas[rel] = sha

        if paths.links_path.exists():
            try:
                json.loads(paths.links_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Corrupt links.json for {job_id}: {exc}") from exc

        files: list[dict[str, Any]] = []
        for p in sorted(root.rglob("*")) if root.exists() else []:
            if not p.is_file():
                continue
            rel_path = p.relative_to(root).as_posix()
            data = p.read_bytes()
            size = len(data)
            sha256 = hashlib.sha256(data).hexdigest()
            if rel_path in expected_tile_shas and expected_tile_shas[rel_path] != sha256:
                raise ValueError(
                    f"Corrupt artifact SHA256 mismatch for {rel_path}: "
                    f"expected {expected_tile_shas[rel_path]}, got {sha256}"
                )
            if "tile" in p.parts or p.name.startswith("tile_"):
                kind = "tile"
            elif rel_path == "manifest.json":
                kind = "manifest"
            elif rel_path == "out.md":
                kind = "markdown"
            elif rel_path == "links.json":
                kind = "links"
            elif rel_path.endswith("dom.html"):
                kind = "dom_snapshot"
            elif rel_path.endswith("dom_overlay.json"):
                kind = "dom_overlay"
            elif rel_path.endswith("bundle.tar.zst"):
                kind = "bundle"
            else:
                kind = p.suffix.lstrip(".") or "file"

            if rel_path == "manifest.json":
                rel_url = f"/jobs/{job_id}/manifest.json"
            elif rel_path == "out.md":
                rel_url = f"/jobs/{job_id}/result.md"
            elif rel_path == "links.json":
                rel_url = f"/jobs/{job_id}/links.json"
            elif rel_path == "artifact/dom.html":
                rel_url = f"/jobs/{job_id}/dom.html"
            else:
                rel_url = f"/jobs/{job_id}/artifact/{rel_path}"

            files.append(
                {
                    "path": rel_path,
                    "url": rel_url,
                    "absolute": rel_url,
                    "size": size,
                    "sha256": sha256,
                    "kind": kind,
                }
            )

        purge_at_raw = getattr(record, "purge_at", None)
        if isinstance(purge_at_raw, datetime):
            if purge_at_raw.tzinfo is None:
                purge_at_raw = purge_at_raw.replace(tzinfo=timezone.utc)
            purge_at_iso: str | None = purge_at_raw.astimezone(timezone.utc).isoformat()
        elif isinstance(purge_at_raw, str) and purge_at_raw.strip():
            purge_at_iso = purge_at_raw.strip()
        else:
            base_dt = getattr(record, "finished_at", None) or getattr(record, "started_at", None)
            if isinstance(base_dt, datetime) and self.cache_ttl_hours > 0:
                if base_dt.tzinfo is None:
                    base_dt = base_dt.replace(tzinfo=timezone.utc)
                purge_at_iso = (
                    base_dt.astimezone(timezone.utc) + timedelta(hours=self.cache_ttl_hours)
                ).isoformat()
            else:
                purge_at_iso = None

        return {
            "job_id": job_id,
            "artifact_root": f"/jobs/{job_id}/artifact",
            "file_count": len(files),
            "total_bytes": sum(f["size"] for f in files),
            "files": files,
            "cache_hit": bool(getattr(record, "cache_hit", False)),
            "cache_source_job_id": getattr(record, "cache_source_job_id", None),
            "shared_with_job_ids": self.cache_path_references(job_id),
            "purge_at": purge_at_iso,
        }

    def fetch_run(self, job_id: str) -> RunRecord | None:
        return self._fetch_run(job_id)

    def _fetch_run(self, job_id: str) -> RunRecord | None:
        with self.session() as session:
            return session.get(RunRecord, job_id)

    def delete_run(self, job_id: str) -> bool:
        """Delete the RunRecord (and any related rows) for a job.

        Returns True if a row was deleted, False if there was nothing to delete.
        Cached artifacts on disk are NOT removed by this method; this only
        cleans up the SQLite rows (embeddings across all dims, links, webhooks, runs).
        """
        from .embeddings import delete_embeddings as _delete_embeddings

        with self.session() as session:
            row = session.get(RunRecord, job_id)
            if row is None:
                return False
            _delete_embeddings(session=session, run_id=job_id)
            conn = session.connection()
            conn.exec_driver_sql(
                "DELETE FROM links WHERE run_id = :job_id",
                {"job_id": job_id},
            )
            conn.exec_driver_sql(
                "DELETE FROM webhooks WHERE job_id = :job_id",
                {"job_id": job_id},
            )
            conn.exec_driver_sql(
                "DELETE FROM runs WHERE id = :job_id",
                {"job_id": job_id},
            )
            session.commit()
            return True

    def _paths_for_job(self, job_id: str) -> RunPaths:
        record = self.fetch_run(job_id)
        if not record:
            raise KeyError(f"Run {job_id} not found")
        paths = RunPaths.from_record(record)
        paths.ensure_directories()
        return paths

    def build_bundle(
        self,
        *,
        job_id: str,
        include_tiles: bool = True,
        compression_level: int = 7,
    ) -> Path:
        record = self.fetch_run(job_id)
        if not record:
            raise KeyError(f"Run {job_id} not found")
        paths = RunPaths.from_record(record)
        paths.ensure_directories()
        bundle_path = paths.bundle_path()
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        arcname = str(paths.root.relative_to(paths.root.parent))

        def _filter(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
            rel = Path(member.name)
            if member.name.endswith("bundle.tar.zst"):
                return None
            if not include_tiles and "artifact" in rel.parts and "tiles" in rel.parts:
                return None
            return member

        compressor = zstd.ZstdCompressor(level=compression_level)
        with open(bundle_path, "wb") as bundle_handle:
            with compressor.stream_writer(bundle_handle) as zstd_stream:
                with tarfile.open(mode="w|", fileobj=zstd_stream) as tar:
                    tar.add(paths.root, arcname=arcname, filter=_filter)
        return bundle_path

    @staticmethod
    def load_bundle(bundle_path: Path) -> dict[str, Any]:
        """Inspect and verify a ``bundle.tar.zst`` archive for independent replay."""
        if not bundle_path.exists():
            raise FileNotFoundError(bundle_path)
        files: dict[str, bytes] = {}
        try:
            dctx = zstd.ZstdDecompressor()
            with open(bundle_path, "rb") as fh:
                with dctx.stream_reader(fh) as reader:
                    with tarfile.open(mode="r|", fileobj=reader) as tar:
                        for member in tar:
                            if not member.isfile():
                                continue
                            extracted = tar.extractfile(member)
                            if extracted is None:
                                continue
                            parts = Path(member.name).parts
                            rel = "/".join(parts[1:]) if len(parts) > 1 else member.name
                            files[rel] = extracted.read()
        except Exception as exc:
            raise ValueError(f"Corrupt bundle archive {bundle_path}: {exc}") from exc

        if "manifest.json" not in files:
            raise ValueError(f"Bundle {bundle_path} is missing manifest.json")
        try:
            manifest = json.loads(files["manifest.json"].decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"Corrupt manifest.json inside bundle {bundle_path}: {exc}") from exc
        if not isinstance(manifest, Mapping):
            raise ValueError(f"Invalid manifest.json inside bundle {bundle_path}")

        if "artifact/artifacts.json" in files:
            try:
                artifacts_list = json.loads(files["artifact/artifacts.json"].decode("utf-8"))
            except Exception as exc:
                raise ValueError(f"Corrupt artifacts.json inside bundle {bundle_path}: {exc}") from exc
            if isinstance(artifacts_list, list):
                for item in artifacts_list:
                    if not isinstance(item, Mapping):
                        continue
                    rel = str(item.get("path") or "").replace("\\", "/").strip()
                    expected_sha = str(item.get("sha256") or "").strip()
                    if rel and rel in files and expected_sha:
                        actual_sha = hashlib.sha256(files[rel]).hexdigest()
                        if actual_sha != expected_sha:
                            raise ValueError(
                                f"Bundle tile SHA256 mismatch for {rel}: "
                                f"expected {expected_sha}, got {actual_sha}"
                            )

        markdown = files["out.md"].decode("utf-8") if "out.md" in files else ""
        links: list[dict[str, Any]] = []
        if "links.json" in files:
            try:
                raw_links = json.loads(files["links.json"].decode("utf-8"))
                if isinstance(raw_links, list):
                    links = [dict(x) for x in raw_links if isinstance(x, Mapping)]
            except Exception as exc:
                raise ValueError(f"Corrupt links.json inside bundle {bundle_path}: {exc}") from exc

        inventory = [
            {
                "path": rel,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for rel, data in sorted(files.items())
        ]
        return {
            "manifest": dict(manifest),
            "markdown": markdown,
            "links": links,
            "files": inventory,
        }

    def upsert_embeddings(
        self,
        *,
        run_id: str,
        sections,
        model: str | None = None,
    ) -> None:
        """Persist section embeddings for a run (idempotently replaces prior run rows)."""
        from .embeddings import DEFAULT_EMBEDDING_MODEL, upsert_embeddings as _upsert

        section_list = list(sections)
        if not section_list:
            return
        with self.session() as session:
            _upsert(
                session=session,
                run_id=run_id,
                sections=section_list,
                model=model,
                replace_run=True,
            )
            run = session.get(RunRecord, run_id)
            if run is not None:
                first = section_list[0]
                resolved_model = getattr(first, "model", None) or model or DEFAULT_EMBEDDING_MODEL
                resolved_dim = getattr(first, "dim", None) or len(first.vector)
                run.embedding_model = str(resolved_model)
                run.embedding_dim = int(resolved_dim)
                session.add(run)
                session.commit()

    def delete_section_embeddings(
        self,
        *,
        job_id: str,
        model: str | None = None,
    ) -> int:
        """Drop all section embeddings for a job (optionally scoped to ``model``)."""
        from .embeddings import delete_embeddings as _delete_embeddings

        with self.session() as session:
            deleted = _delete_embeddings(session=session, run_id=job_id, model=model)
            if deleted > 0:
                run = session.get(RunRecord, job_id)
                if run is not None:
                    run.embedding_model = None
                    run.embedding_dim = None
                    session.add(run)
                    session.commit()
            return deleted

    def search_section_embeddings(
        self,
        *,
        job_id: str,
        vector: Sequence[float],
        top_k: int,
        model: str | None = None,
    ) -> tuple[int, list[EmbeddingMatch]]:
        with self.session() as session:
            return search_embeddings(
                session=session,
                run_id=job_id,
                query_vector=vector,
                top_k=top_k,
                model=model,
            )

    def search_embeddings_multi(
        self,
        *,
        vector: Sequence[float],
        top_k: int,
        run_ids: Sequence[str] | None = None,
        model: str | None = None,
    ) -> tuple[int, list[EmbeddingMatch]]:
        from .embeddings import search_embeddings_across_runs

        with self.session() as session:
            return search_embeddings_across_runs(
                session=session,
                query_vector=vector,
                top_k=top_k,
                run_ids=run_ids,
                model=model,
            )


    async def afetch_run(self, job_id: str) -> RunRecord | None:
        if "fetch_run" in self.__dict__:
            return await asyncio.to_thread(self.fetch_run, job_id)
        async with self.async_session() as session:
            return await session.get(RunRecord, job_id)

    async def alist_runs(
        self,
        *,
        state: str | None = None,
        profile_id: str | None = None,
        owner: str | None = None,
        enforce_owner: bool = False,
        url_contains: str | None = None,
        cache_hit: bool | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RunRecord], int]:
        if "list_runs" in self.__dict__:
            return await asyncio.to_thread(
                partial(
                    self.list_runs,
                    state=state,
                    profile_id=profile_id,
                    owner=owner,
                    enforce_owner=enforce_owner,
                    url_contains=url_contains,
                    cache_hit=cache_hit,
                    tag=tag,
                    limit=limit,
                    offset=offset,
                )
            )
        async with self.async_session() as session:
            stmt = select(RunRecord).order_by(desc(col(RunRecord.started_at)))
            if state:
                stmt = stmt.where(col(RunRecord.status) == state)
            if profile_id is not None:
                stmt = stmt.where(col(RunRecord.profile_id) == profile_id)
            if enforce_owner:
                if owner is None:
                    stmt = stmt.where(col(RunRecord.owner).is_(None))
                else:
                    stmt = stmt.where(col(RunRecord.owner) == owner)
            elif owner is not None:
                stmt = stmt.where(col(RunRecord.owner) == owner)
            if cache_hit is True:
                stmt = stmt.where(col(RunRecord.cache_hit).is_(True))
            elif cache_hit is False:
                stmt = stmt.where(col(RunRecord.cache_hit).is_(False))
            rows = list((await session.scalars(stmt)).all())
            if url_contains:
                needle = url_contains.lower()
                rows = [r for r in rows if needle in (r.url or "").lower()]
            if tag:
                rows = [r for r in rows if tag in (r.tags or [])]
            total = len(rows)
            sliced = rows[max(0, offset) : max(0, offset) + max(1, limit)]
            return sliced, total

    async def acache_stats(self) -> dict[str, int]:
        if "cache_stats" in self.__dict__:
            return await asyncio.to_thread(self.cache_stats)
        async with self.async_session() as session:
            rows = list((await session.scalars(select(RunRecord))).all())
            done_rows = [r for r in rows if r.status == "DONE"]
            hits = sum(1 for r in done_rows if bool(getattr(r, "cache_hit", False)))
            misses = sum(1 for r in done_rows if not bool(getattr(r, "cache_hit", False)))
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=self.cache_ttl_hours)).replace(
                tzinfo=None
            )
            fresh_keys = {
                r.cache_key
                for r in done_rows
                if r.cache_key
                and not bool(getattr(r, "cache_invalidated", False))
                and not bool(getattr(r, "cache_hit", False))
                and r.started_at is not None
                and (
                    r.started_at.replace(tzinfo=None)
                    if r.started_at.tzinfo
                    else r.started_at
                )
                >= cutoff
            }
            return {
                "entries": len(fresh_keys),
                "hits": hits,
                "misses": misses,
                "ttl_seconds": int(self.cache_ttl_hours * 3600),
                "enabled": 1 if self.cache_ttl_hours > 0 else 0,
            }

    async def ainvalidate_cache_key(self, target: str) -> int:
        if "invalidate_cache_key" in self.__dict__:
            return await asyncio.to_thread(self.invalidate_cache_key, target)
        cleaned = str(target or "").strip()
        if not cleaned:
            return 0
        normalized_url = _normalize_cache_url(cleaned) if "://" in cleaned else None
        async with self.async_session() as session:
            rows = list((await session.scalars(select(RunRecord))).all())
            invalidated = 0
            for r in rows:
                match_key = r.cache_key == cleaned
                match_url = normalized_url is not None and _normalize_cache_url(r.url) == normalized_url
                if (match_key or match_url) and not bool(getattr(r, "cache_invalidated", False)):
                    r.cache_invalidated = True
                    session.add(r)
                    invalidated += 1
            if invalidated:
                await session.commit()
            return invalidated

    async def aclear_cache(self) -> int:
        if "clear_cache" in self.__dict__:
            return await asyncio.to_thread(self.clear_cache)
        async with self.async_session() as session:
            rows = list((await session.scalars(select(RunRecord))).all())
            cleared = 0
            for r in rows:
                if r.status == "DONE" and not bool(getattr(r, "cache_invalidated", False)):
                    r.cache_invalidated = True
                    session.add(r)
                    cleared += 1
            if cleared:
                await session.commit()
            return cleared

    async def acache_path_references(self, job_id: str) -> list[str]:
        if "cache_path_references" in self.__dict__:
            return await asyncio.to_thread(self.cache_path_references, job_id)
        async with self.async_session() as session:
            record = await session.get(RunRecord, job_id)
            if record is None:
                return []
            stmt = select(RunRecord.id).where(
                col(RunRecord.id) != job_id,
                or_(
                    col(RunRecord.cache_path) == record.cache_path,
                    col(RunRecord.manifest_path) == record.manifest_path,
                    col(RunRecord.cache_source_job_id) == job_id,
                ),
            )
            res = await session.scalars(stmt)
            return sorted(str(jid) for jid in res.all() if jid)

    async def alist_webhooks(self, job_id: str) -> list[WebhookRecord]:
        if "list_webhooks" in self.__dict__:
            return await asyncio.to_thread(self.list_webhooks, job_id)
        async with self.async_session() as session:
            run = await session.get(RunRecord, job_id)
            if run is None:
                msg = f"Unknown job_id {job_id}"
                raise KeyError(msg)
            stmt = (
                select(WebhookRecord)
                .where(col(WebhookRecord.job_id) == job_id)
                .order_by(col(WebhookRecord.id))
            )
            return list((await session.scalars(stmt)).all())

    async def aupsert_embeddings(
        self,
        *,
        run_id: str,
        sections: Sequence[Any],
        model: str | None = None,
    ) -> None:
        if "upsert_embeddings" in self.__dict__:
            await asyncio.to_thread(
                partial(self.upsert_embeddings, run_id=run_id, sections=sections, model=model)
            )
            return
        from .embeddings import DEFAULT_EMBEDDING_MODEL, upsert_embeddings as _upsert

        section_list = list(sections)
        if not section_list:
            return
        async with self.async_session() as session:
            await session.run_sync(
                lambda sync_s: _upsert(
                    session=sync_s,
                    run_id=run_id,
                    sections=section_list,
                    model=model,
                    replace_run=True,
                )
            )
            run = await session.get(RunRecord, run_id)
            if run is not None:
                first = section_list[0]
                resolved_model = getattr(first, "model", None) or model or DEFAULT_EMBEDDING_MODEL
                resolved_dim = getattr(first, "dim", None) or len(first.vector)
                run.embedding_model = str(resolved_model)
                run.embedding_dim = int(resolved_dim)
                session.add(run)
            await session.commit()

    async def adelete_section_embeddings(
        self,
        *,
        job_id: str,
        model: str | None = None,
    ) -> int:
        if "delete_section_embeddings" in self.__dict__:
            return await asyncio.to_thread(
                partial(self.delete_section_embeddings, job_id=job_id, model=model)
            )
        from .embeddings import delete_embeddings as _delete_embeddings

        async with self.async_session() as session:
            deleted = await session.run_sync(
                lambda sync_s: _delete_embeddings(session=sync_s, run_id=job_id, model=model)
            )
            if deleted > 0:
                run = await session.get(RunRecord, job_id)
                if run is not None:
                    run.embedding_model = None
                    run.embedding_dim = None
                    session.add(run)
            await session.commit()
            return int(deleted)

    async def asearch_section_embeddings(
        self,
        *,
        job_id: str,
        vector: Sequence[float],
        top_k: int,
        model: str | None = None,
    ) -> tuple[int, list[EmbeddingMatch]]:
        if "search_section_embeddings" in self.__dict__:
            return await asyncio.to_thread(
                partial(
                    self.search_section_embeddings,
                    job_id=job_id,
                    vector=vector,
                    top_k=top_k,
                    model=model,
                )
            )
        async with self.async_session() as session:
            return await session.run_sync(
                lambda sync_s: search_embeddings(
                    session=sync_s,
                    run_id=job_id,
                    query_vector=vector,
                    top_k=top_k,
                    model=model,
                )
            )

    async def asearch_embeddings_multi(
        self,
        *,
        vector: Sequence[float],
        top_k: int,
        run_ids: Sequence[str] | None = None,
        model: str | None = None,
    ) -> tuple[int, list[EmbeddingMatch]]:
        from .embeddings import search_embeddings_across_runs

        async with self.async_session() as session:
            return await session.run_sync(
                lambda sync_s: search_embeddings_across_runs(
                    session=sync_s,
                    query_vector=vector,
                    top_k=top_k,
                    run_ids=run_ids,
                    model=model,
                )
            )

    async def aread_manifest(self, job_id: str) -> dict[str, Any]:
        async with self._io_semaphore:
            return await asyncio.to_thread(self.read_manifest, job_id)

    async def aread_markdown(self, job_id: str) -> str:
        async with self._io_semaphore:
            return await asyncio.to_thread(self.read_markdown, job_id)

    async def aread_links(self, job_id: str) -> list[dict[str, object]]:
        async with self._io_semaphore:
            return await asyncio.to_thread(self.read_links, job_id)

    async def aread_artifacts(self, job_id: str) -> list[dict[str, object]]:
        async with self._io_semaphore:
            return await asyncio.to_thread(self.read_artifacts, job_id)

    async def aresolve_artifact(self, job_id: str, relative_path: str) -> Path:
        async with self._io_semaphore:
            return await asyncio.to_thread(self.resolve_artifact, job_id, relative_path)

    async def ainventory_artifacts(self, job_id: str) -> dict[str, Any]:
        async with self._io_semaphore:
            return await asyncio.to_thread(self.inventory_artifacts, job_id)

    async def adelete_run(self, job_id: str) -> bool:
        async with self._io_semaphore:
            return await asyncio.to_thread(self.delete_run, job_id)


def build_store(config: StorageConfig | None = None) -> Store:
    """Convenience wrapper used by FastAPI startup hooks."""

    return Store(config=config)


def _split_url(url: str) -> tuple[str, str]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or "unknown-host"
    path = parsed.path or "home"
    slug = _SLUG_PATTERN.sub("-", path.lower()).strip("-") or "home"
    # Add a short hash of the full URL so distinct query strings do not collide.
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return host.replace(":", "-"), f"{slug}-{digest}"


def _normalize_cache_url(url: str) -> str:
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    raw = str(url or "").strip()
    if not raw or "://" not in raw:
        return raw
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "http").lower()
    netloc = (parsed.netloc or "").lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    canonical_query = urlencode(sorted(query_pairs))
    return urlunparse(
        parsed._replace(
            scheme=scheme,
            netloc=netloc,
            path=path,
            query=canonical_query,
            fragment="",
        )
    )


def _build_runtime_cache_key(
    *,
    cache_seed: str,
    backend_id: str,
    backend_mode: str,
    hardware_path: str,
    fallback_chain: Sequence[str],
    served_ocr_model: str | None = None,
    ocr_model: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "cache_seed": cache_seed,
        "ocr_backend_id": backend_id,
        "ocr_backend_mode": backend_mode,
        "ocr_hardware_path": hardware_path,
        "ocr_fallback_chain": [entry for entry in fallback_chain if entry],
    }
    if (
        served_ocr_model
        and ocr_model
        and str(served_ocr_model).strip()
        and str(ocr_model).strip()
        and str(served_ocr_model).strip() != str(ocr_model).strip()
    ):
        payload["served_ocr_model"] = str(served_ocr_model).strip()
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _normalize_manifest_cache_key(manifest_dict: dict[str, Any]) -> None:
    """Recompute cache key from runtime backend fields when cache seed is present."""

    cache_seed_raw = manifest_dict.get("cache_seed")
    backend_id_raw = manifest_dict.get("backend_id")
    backend_mode_raw = manifest_dict.get("backend_mode")
    hardware_path_raw = manifest_dict.get("hardware_path")
    if not (
        isinstance(cache_seed_raw, str)
        and cache_seed_raw.strip()
        and isinstance(backend_id_raw, str)
        and backend_id_raw.strip()
        and isinstance(backend_mode_raw, str)
        and backend_mode_raw.strip()
        and isinstance(hardware_path_raw, str)
        and hardware_path_raw.strip()
    ):
        return
    cache_seed = cache_seed_raw.strip()
    backend_id = backend_id_raw.strip()
    backend_mode = backend_mode_raw.strip()
    hardware_path = hardware_path_raw.strip()
    chain_raw = manifest_dict.get("fallback_chain")
    chain: list[str] = []
    if isinstance(chain_raw, (list, tuple)):
        chain = [str(entry).strip() for entry in chain_raw if str(entry).strip()]
    if not chain:
        chain = [backend_id]
    served_ocr_model = manifest_dict.get("served_ocr_model")
    ocr_model = manifest_dict.get("ocr_model")
    manifest_dict["cache_key"] = _build_runtime_cache_key(
        cache_seed=cache_seed,
        backend_id=backend_id,
        backend_mode=backend_mode,
        hardware_path=hardware_path,
        fallback_chain=chain,
        served_ocr_model=str(served_ocr_model) if isinstance(served_ocr_model, str) else None,
        ocr_model=str(ocr_model) if isinstance(ocr_model, str) else None,
    )


def _cache_segments(cache_key: str) -> tuple[str, str]:
    """Return (bucket, normalized_key) segments for cache directories."""

    normalized = re.sub(r"[^a-z0-9]", "-", cache_key.lower()).strip("-")
    if not normalized:
        normalized = "cache"
    bucket = normalized[:2].ljust(2, "0")
    return bucket, normalized


class _AsyncSqliteCursor:
    """Async cursor adapter backed by a dedicated SQLite connection worker thread."""

    def __init__(self, conn: _AsyncSqliteConnection, raw_cursor: sqlite3.Cursor) -> None:
        self._conn = conn
        self._cursor = raw_cursor

    @property
    def description(self) -> Any:
        return self._cursor.description

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    async def execute(self, sql: str, parameters: Any = None) -> _AsyncSqliteCursor:
        if parameters is None:
            await self._conn._submit(partial(self._cursor.execute, sql))
        else:
            await self._conn._submit(partial(self._cursor.execute, sql, parameters))
        return self

    async def executemany(self, sql: str, seq_of_parameters: Any) -> _AsyncSqliteCursor:
        await self._conn._submit(partial(self._cursor.executemany, sql, seq_of_parameters))
        return self

    async def fetchall(self) -> list[Any]:
        return await self._conn._submit(self._cursor.fetchall)

    async def fetchone(self) -> Any:
        return await self._conn._submit(self._cursor.fetchone)

    async def fetchmany(self, size: int | None = None) -> list[Any]:
        if size is None:
            return await self._conn._submit(self._cursor.fetchmany)
        return await self._conn._submit(partial(self._cursor.fetchmany, size))

    async def close(self) -> None:
        await self._conn._submit(self._cursor.close)


class _AsyncSqliteConnection(threading.Thread):
    """Thread-isolated async SQLite connection compatible with SQLAlchemy sqlite+aiosqlite."""

    def __init__(self, connector: Callable[[], sqlite3.Connection]) -> None:
        super().__init__(daemon=True)
        self._connector = connector
        self._conn: sqlite3.Connection | None = None
        self._tx: queue.Queue[tuple[asyncio.Future[Any], Callable[[], Any]] | None] = queue.Queue()
        self._running = True

    def run(self) -> None:
        while self._running:
            item = self._tx.get()
            if item is None:
                break
            fut, fn = item
            try:
                res = fn()
            except BaseException as exc:
                try:
                    if not fut.done():
                        fut.get_loop().call_soon_threadsafe(fut.set_exception, exc)
                except RuntimeError:
                    pass
            else:
                try:
                    if not fut.done():
                        fut.get_loop().call_soon_threadsafe(fut.set_result, res)
                except RuntimeError:
                    pass

    def _submit(self, fn: Callable[[], Any]) -> asyncio.Future[Any]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._tx.put_nowait((fut, fn))
        return fut

    def __await__(self) -> Generator[Any, None, _AsyncSqliteConnection]:
        async def _connect() -> _AsyncSqliteConnection:
            self.start()

            def _open() -> _AsyncSqliteConnection:
                conn = self._connector()
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.execute("PRAGMA journal_mode=WAL").fetchone()
                conn.execute("PRAGMA synchronous=NORMAL").fetchone()
                conn.execute("PRAGMA busy_timeout=30000").fetchone()
                self._conn = conn
                return self

            return await self._submit(_open)

        return _connect().__await__()

    @property
    def isolation_level(self) -> str | None:
        return self._conn.isolation_level if self._conn is not None else None

    async def cursor(self) -> _AsyncSqliteCursor:
        assert self._conn is not None
        raw_cur = await self._submit(self._conn.cursor)
        return _AsyncSqliteCursor(self, raw_cur)

    async def execute(self, sql: str, parameters: Any = None) -> _AsyncSqliteCursor:
        cur = await self.cursor()
        return await cur.execute(sql, parameters)

    async def create_function(self, *args: Any, **kwargs: Any) -> None:
        assert self._conn is not None
        await self._submit(partial(self._conn.create_function, *args, **kwargs))

    async def commit(self) -> None:
        assert self._conn is not None
        await self._submit(self._conn.commit)

    async def rollback(self) -> None:
        assert self._conn is not None
        await self._submit(self._conn.rollback)

    async def close(self) -> None:
        if not self._running:
            return

        def _close() -> None:
            try:
                if self._conn is not None:
                    self._conn.close()
            finally:
                self._running = False

        await self._submit(_close)
        self._tx.put_nowait(None)


def _ensure_aiosqlite_driver() -> None:
    if "aiosqlite" in sys.modules:
        return
    mod = types.ModuleType("aiosqlite")
    for attr in (
        "DatabaseError",
        "Error",
        "IntegrityError",
        "NotSupportedError",
        "OperationalError",
        "ProgrammingError",
        "sqlite_version",
        "sqlite_version_info",
    ):
        setattr(mod, attr, getattr(sqlite3, attr))
    setattr(
        mod,
        "connect",
        lambda *args, **kwargs: _AsyncSqliteConnection(partial(sqlite3.connect, *args, **kwargs)),
    )
    sys.modules["aiosqlite"] = mod


def _create_async_engine(db_path: Path) -> AsyncEngine:
    _ensure_aiosqlite_driver()
    return create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={
            "check_same_thread": False,
            "timeout": 30.0,
        },
    )


def _create_engine(db_path: Path):
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={
            "check_same_thread": False,
            "timeout": 30.0,  # Longer timeout for concurrent writes
        },
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, connection_record) -> None:
        dbapi_connection.enable_load_extension(True)
        sqlite_vec.load(dbapi_connection)
        # Enable WAL mode for concurrent read/write operations
        result = dbapi_connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if result and result[0].lower() != "wal":
            LOGGER.warning("Failed to enable WAL mode, got: %s", result[0])
        dbapi_connection.execute("PRAGMA synchronous=NORMAL").fetchone()
        # Set busy timeout to 30 seconds
        dbapi_connection.execute("PRAGMA busy_timeout=30000").fetchone()

    return engine


def _parse_tile_index(filename: str) -> int:
    try:
        stem = Path(filename).stem
        return int(stem.split("_")[-1])
    except Exception:
        return -1


_PROVENANCE_TILE_RE = re.compile(r"<!--\s*source:\s*tile_(\d+)", re.IGNORECASE)
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_SECTION_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify_section(text: str) -> str:
    slug = _SECTION_SLUG_RE.sub("-", text.lower()).strip("-")
    return slug or "section"


def _extract_sections_for_embedding(
    markdown: str,
    *,
    job_id: str,
    tiles_total: int | None = None,
) -> list[dict[str, Any]]:
    """Split stitched Markdown into sections with tile provenance spans for embedding."""
    if not markdown or not markdown.strip():
        return []

    current_tile = 0
    seen_any_tile = False
    raw_sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    preamble_lines: list[str] = []
    preamble_tiles: list[int] = []

    for line in markdown.splitlines():
        prov_match = _PROVENANCE_TILE_RE.search(line)
        if prov_match:
            try:
                current_tile = int(prov_match.group(1))
                seen_any_tile = True
            except ValueError:
                pass
            continue

        stripped = line.strip()
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue

        heading_match = _MD_HEADING_RE.match(line)
        if heading_match:
            if current is not None:
                raw_sections.append(current)
            elif preamble_lines:
                preamble_text = "\n".join(preamble_lines).strip()
                if preamble_text:
                    raw_sections.append(
                        {
                            "heading": "Introduction",
                            "anchor": "introduction",
                            "lines": list(preamble_lines),
                            "tiles": list(preamble_tiles) or ([current_tile] if seen_any_tile else [0]),
                        }
                    )
                preamble_lines.clear()
            heading_text = heading_match.group(2).strip()
            current = {
                "heading": heading_text,
                "anchor": _slugify_section(heading_text),
                "lines": [],
                "tiles": [current_tile] if seen_any_tile else [],
            }
        else:
            if current is None:
                if stripped:
                    preamble_lines.append(line)
                    if seen_any_tile and current_tile not in preamble_tiles:
                        preamble_tiles.append(current_tile)
            else:
                current["lines"].append(line)
                if stripped and seen_any_tile and current_tile not in current["tiles"]:
                    current["tiles"].append(current_tile)

    if current is not None:
        raw_sections.append(current)
    elif preamble_lines:
        raw_sections.append(
            {
                "heading": "",
                "anchor": "document",
                "lines": list(preamble_lines),
                "tiles": list(preamble_tiles) or ([current_tile] if seen_any_tile else [0]),
            }
        )

    out: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    max_tile_default = max(0, (tiles_total - 1) if isinstance(tiles_total, int) and tiles_total > 0 else current_tile)

    for idx, sec in enumerate(raw_sections):
        heading = str(sec.get("heading") or "").strip()
        body = _HTML_COMMENT_RE.sub("", "\n".join(sec.get("lines") or [])).strip()
        combined = f"{heading}\n{body}".strip() if heading else body
        if not combined:
            continue
        base_anchor = str(sec.get("anchor") or f"section-{idx}").strip() or f"section-{idx}"
        count = seen_ids.get(base_anchor, 0)
        seen_ids[base_anchor] = count + 1
        section_id = base_anchor if count == 0 else f"{base_anchor}-{count + 1}"

        tiles = sec.get("tiles") or []
        if tiles:
            tile_start = int(min(tiles))
            tile_end = int(max(tiles))
        else:
            tile_start = 0
            tile_end = max_tile_default

        out.append(
            {
                "section_id": section_id,
                "heading": heading,
                "text": combined,
                "tile_start": tile_start,
                "tile_end": max(tile_start, tile_end),
            }
        )

    return out


def _apply_manifest_metadata(record: RunRecord, manifest: Mapping[str, object] | Any) -> None:
    manifest_dict = _manifest_to_dict(manifest)

    def _set(attr: str, value: Any) -> None:
        if value is not None:
            setattr(record, attr, value)

    def _set_int(attr: str, value: Any) -> None:
        coerced = _coerce_int(value)
        if coerced is not None:
            setattr(record, attr, coerced)

    def _set_chain(value: Any) -> None:
        if isinstance(value, (list, tuple)):
            chain = [str(entry) for entry in value if str(entry).strip()]
            if chain:
                record.fallback_chain = chain

    environment = manifest_dict.get("environment")
    if isinstance(environment, Mapping):
        _set("cft_version", environment.get("cft_version"))
        _set("cft_label", environment.get("cft_label"))
        _set("server_runtime", environment.get("server_runtime"))
        _set("playwright_version", environment.get("playwright_version"))
        _set("browser_transport", environment.get("browser_transport"))
        _set("screenshot_style_hash", environment.get("screenshot_style_hash"))
        _set("ocr_model", environment.get("ocr_model"))
        _set("ocr_provider", environment.get("ocr_provider"))
        _set("backend_id", environment.get("ocr_backend_id"))
        _set("backend_mode", environment.get("ocr_backend_mode"))
        _set("hardware_path", environment.get("ocr_hardware_path"))
        _set_chain(environment.get("ocr_fallback_chain"))
        viewport = environment.get("viewport")
        if isinstance(viewport, Mapping):
            _set_int("device_scale_factor", viewport.get("device_scale_factor"))
    else:  # legacy flat manifests
        _set("cft_version", manifest_dict.get("cft_version"))
        _set("cft_label", manifest_dict.get("cft_label"))
        _set("server_runtime", manifest_dict.get("server_runtime"))
        _set("playwright_version", manifest_dict.get("playwright_version"))
        _set("browser_transport", manifest_dict.get("browser_transport"))
        _set("screenshot_style_hash", manifest_dict.get("screenshot_style_hash"))
        _set("ocr_model", manifest_dict.get("model"))
        _set("ocr_provider", manifest_dict.get("ocr_provider"))
        _set_int("device_scale_factor", manifest_dict.get("device_scale_factor"))
    _set("backend_id", manifest_dict.get("backend_id"))
    _set("backend_mode", manifest_dict.get("backend_mode"))
    _set("hardware_path", manifest_dict.get("hardware_path"))
    _set_chain(manifest_dict.get("fallback_chain"))
    if record.backend_id is None:
        provider = (
            str(record.ocr_provider).strip().lower()
            if isinstance(record.ocr_provider, str)
            else None
        )
        if provider not in {"olmocr", "glm-ocr"}:
            provider = None
        model = str(record.ocr_model).strip().lower() if isinstance(record.ocr_model, str) else ""
        inferred_provider = "glm-ocr" if "glm-ocr" in model else (provider or "olmocr")
        inferred_backend = f"{inferred_provider}-remote-openai"
        record.backend_id = inferred_backend
        record.backend_mode = record.backend_mode or "openai-compatible"
        record.hardware_path = record.hardware_path or "remote"
        if not record.fallback_chain:
            record.fallback_chain = [inferred_backend]
    _set("profile_id", manifest_dict.get("profile_id"))
    _set("cache_key", manifest_dict.get("cache_key"))
    _set("embedding_model", manifest_dict.get("embedding_model"))
    _set_int("embedding_dim", manifest_dict.get("embedding_dim"))

    timings = manifest_dict.get("timings")
    if isinstance(timings, Mapping):
        _set_int("capture_ms", timings.get("capture_ms"))
        _set_int("ocr_ms", timings.get("ocr_ms"))
        _set_int("stitch_ms", timings.get("stitch_ms"))
    else:
        _set_int("capture_ms", manifest_dict.get("capture_ms"))
        _set_int("ocr_ms", manifest_dict.get("ocr_ms"))
        _set_int("stitch_ms", manifest_dict.get("stitch_ms"))

    _set_int("tiles_total", manifest_dict.get("tiles_total"))
    _set_int("long_side_px", manifest_dict.get("long_side_px"))
    sweep_stats = manifest_dict.get("sweep_stats")
    if isinstance(sweep_stats, Mapping):
        _set_int("sweep_shrink_events", sweep_stats.get("shrink_events"))
        _set_int("sweep_retry_attempts", sweep_stats.get("retry_attempts"))
        _set_int("sweep_overlap_pairs", sweep_stats.get("overlap_pairs"))
        if sweep_stats.get("overlap_match_ratio") is not None:
            _set("overlap_match_ratio", sweep_stats.get("overlap_match_ratio"))
    if manifest_dict.get("overlap_match_ratio") is not None:
        _set("overlap_match_ratio", manifest_dict.get("overlap_match_ratio"))
    validation_failures = manifest_dict.get("validation_failures")
    if isinstance(validation_failures, list):
        _set_int("validation_failure_count", len(validation_failures))
    seam_summary: dict[str, Any] | None = None
    seam_markers = manifest_dict.get("seam_markers")
    if isinstance(seam_markers, list):
        _set_int("seam_marker_count", len(seam_markers))
        hashes = {
            entry.get("hash")
            for entry in seam_markers
            if isinstance(entry, Mapping) and entry.get("hash")
        }
        if hashes:
            _set_int("seam_hash_count", len(hashes))
        seam_summary = summarize_seam_markers(seam_markers)
    else:
        _set_int("seam_marker_count", manifest_dict.get("seam_marker_count"))
        _set_int("seam_hash_count", manifest_dict.get("seam_hash_count"))
    if seam_summary is None:
        raw_summary = manifest_dict.get("seam_markers_summary")
        if isinstance(raw_summary, Mapping):
            seam_summary = dict(raw_summary)
    record.seam_markers_summary = seam_summary


def _manifest_to_dict(manifest: Mapping[str, object] | Any) -> dict[str, Any]:
    if isinstance(manifest, Mapping):
        return dict(manifest)
    if hasattr(manifest, "model_dump"):
        return manifest.model_dump()
    if is_dataclass(manifest):
        return asdict(manifest)
    return {}


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _coerce_state(value: str | Enum) -> str:
    if isinstance(value, Enum):
        return value.value
    return str(value)


__all__ = [
    "RunRecord",
    "LinkRecord",
    "WebhookRecord",
    "RunPaths",
    "StorageConfig",
    "Store",
    "build_store",
]
