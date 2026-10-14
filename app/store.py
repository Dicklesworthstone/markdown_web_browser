"""Persistence helpers for artifacts, manifests, and sqlite-vec metadata."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
from enum import Enum
from pathlib import Path
from typing import Any, Generator, Iterable, Mapping, Sequence
from uuid import uuid4

import tarfile

import sqlite_vec
import zstandard as zstd
from sqlalchemy import Column, UniqueConstraint, event, text
from sqlalchemy.dialects.sqlite import JSON as SQLITE_JSON
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
from app.auth import APIKey, ResourceOwner, current_owner  # noqa: E402, F401
from app.rate_limit import RateLimitBucket  # noqa: E402, F401 - table registration

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
    owner_key: str | None = Field(default=None, index=True)
    cache_key: str | None = None
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


class EventCursorRecord(SQLModel, table=True):
    """Durable namespace and dense sequence for one owner's global feed."""

    __tablename__ = "event_cursors"
    scope_key: str = Field(primary_key=True)
    epoch: str
    sequence: int = -1


class JobEventRecord(SQLModel, table=True):
    """Append-only canonical events; initial admission can precede run allocation."""

    __tablename__ = "job_events"
    __table_args__ = (
        UniqueConstraint("job_id", "sequence"),
        UniqueConstraint("scope_key", "global_sequence"),
    )
    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(index=True)
    sequence: int
    scope_key: str = Field(index=True)
    global_sequence: int
    payload: dict[str, Any] = Field(sa_column=Column(SQLITE_JSON, nullable=False))


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

    @classmethod
    def from_record(cls, record: RunRecord) -> RunPaths:
        root = Path(record.cache_path)
        return cls(
            root=root,
            artifacts_dir=root / "artifact",
            tiles_dir=root / "artifact" / "tiles",
            manifest_path=Path(record.manifest_path),
            markdown_path=root / "out.md",
            links_path=root / "links.json",
            dom_snapshot_path=root / "artifact" / "dom.html",
            artifacts_manifest_path=root / "artifact" / "artifacts.json",
            cache_key=record.cache_key,
        )


class Store:
    """Facade around SQLite + filesystem persistence."""

    def __init__(self, config: StorageConfig | None = None, cache_ttl_hours: int = 24) -> None:
        self.config = config or StorageConfig.from_env()
        self.cache_ttl_hours = cache_ttl_hours
        self.config.cache_root.mkdir(parents=True, exist_ok=True)
        self.engine = _create_engine(self.config.db_path)
        SQLModel.metadata.create_all(self.engine)
        self._ensure_vec_table()
        self._ensure_run_columns()
        with self.engine.begin() as conn:
            columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(api_keys)")}
            if "is_admin" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE api_keys ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"
                )

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
            "tags": "JSON",
            "owner_key": "TEXT",
            "cache_key": "TEXT",
            "server_runtime": "TEXT",
            "seam_marker_count": "INTEGER",
            "seam_hash_count": "INTEGER",
            "seam_markers_summary": "JSON",
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

    def allocate_run(
        self,
        *,
        job_id: str,
        url: str,
        started_at: datetime,
        profile_id: str | None = None,
        cache_key: str | None = None,
        tags: list[str] | None = None,
    ) -> RunPaths:
        paths = RunPaths.from_url(
            url=url, started_at=started_at, config=self.config, cache_key=cache_key
        )
        paths.ensure_directories()
        record = RunRecord(
            id=job_id,
            url=url,
            started_at=started_at,
            cache_path=str(paths.root),
            manifest_path=str(paths.manifest_path),
            profile_id=profile_id,
            owner_key=current_owner(),
            cache_key=cache_key,
            tags=list(tags) if tags else None,
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
        url_contains: str | None = None,
        cache_hit: bool | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RunRecord], int]:
        """Filter runs by any combination of (state, profile, url substring, tag)."""
        with self.session() as session:
            stmt = select(RunRecord)
            count_stmt = select(RunRecord)
            owner = current_owner()
            if owner is not None:
                stmt = stmt.where(col(RunRecord.owner_key) == owner)
                count_stmt = count_stmt.where(col(RunRecord.owner_key) == owner)
            if state:
                stmt = stmt.where(col(RunRecord.status) == state)
                count_stmt = count_stmt.where(col(RunRecord.status) == state)
            if profile_id:
                stmt = stmt.where(col(RunRecord.profile_id) == profile_id)
                count_stmt = count_stmt.where(col(RunRecord.profile_id) == profile_id)
            if url_contains:
                stmt = stmt.where(col(RunRecord.url).contains(url_contains))
                count_stmt = count_stmt.where(col(RunRecord.url).contains(url_contains))
            if tag:
                try:
                    stmt = stmt.where(
                        text(
                            "EXISTS (SELECT 1 FROM json_each(runs.tags) WHERE json_each.value = :tag)"
                        ).bindparams(tag=tag)
                    )
                    count_stmt = count_stmt.where(
                        text(
                            "EXISTS (SELECT 1 FROM json_each(runs.tags) WHERE json_each.value = :tag)"
                        ).bindparams(tag=tag)
                    )
                except Exception:
                    pass
            if cache_hit is not None and cache_hit:
                stmt = stmt.where(col(RunRecord.cache_key).is_not(None))
                count_stmt = count_stmt.where(col(RunRecord.cache_key).is_not(None))
            stmt = (
                stmt.order_by(col(RunRecord.started_at).desc())
                .offset(max(0, offset))
                .limit(max(1, limit))
            )
            rows = list(session.exec(stmt))
            total = len(list(session.exec(count_stmt)))
            return rows, total

    def find_cache_hit(self, cache_key: str | None) -> RunRecord | None:
        if not cache_key:
            return None

        # Calculate TTL cutoff time (make naive for SQLite comparison since SQLite strips timezone)
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=self.cache_ttl_hours)
        cutoff_time = cutoff_time.replace(tzinfo=None)

        with self.session() as session:
            statement = (
                select(RunRecord)
                .where(
                    col(RunRecord.cache_key) == cache_key,
                    col(RunRecord.status) == "DONE",
                    col(RunRecord.finished_at).is_not(None),
                    # Only return cache hits within TTL window
                    col(RunRecord.finished_at) >= cutoff_time,
                )
                .order_by(col(RunRecord.finished_at).desc())
            )
            owner = current_owner()
            if owner is not None:
                statement = statement.where(col(RunRecord.owner_key) == owner)
            for record in session.exec(statement):
                try:
                    manifest_path = Path(record.manifest_path)
                except TypeError:
                    continue
                if manifest_path.exists():
                    return record
        return None

    def register_cached_run(self, *, job_id: str, source: RunRecord) -> None:
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
            owner_key=source.owner_key,
            cache_key=source.cache_key,
            seam_marker_count=source.seam_marker_count,
            seam_hash_count=source.seam_hash_count,
        )
        with self.session() as session:
            session.add(cloned)
            session.commit()

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
                .where(col(WebhookRecord.job_id) == job_id)
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
            statement = select(WebhookRecord).where(col(WebhookRecord.job_id) == job_id)
            if webhook_id is not None:
                statement = statement.where(col(WebhookRecord.id) == webhook_id)
            if url:
                statement = statement.where(col(WebhookRecord.url) == url)
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
            statement = select(WebhookRecord).where(col(WebhookRecord.job_id) == job_id)
            if webhook_id is not None:
                statement = statement.where(col(WebhookRecord.id) == webhook_id)
            if url:
                statement = statement.where(col(WebhookRecord.url) == url)
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

    def write_tiles(self, *, job_id: str, tiles: Sequence[TileSlice]) -> list[dict[str, Any]]:
        if not tiles:
            return []
        record = self.fetch_run(job_id)
        if not record:
            raise KeyError(f"Run {job_id} not found")
        paths = RunPaths.from_record(record)
        paths.ensure_directories()
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
                    "source_rect": tile.source_rect,
                    "coordinate_space": "CSS document offsets; screenshot physical-pixel source_rect",
                    "source_y_offset": tile.source_y_offset,
                    "viewport_y_offset": tile.viewport_y_offset,
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

    def write_links(
        self,
        *,
        job_id: str,
        links: Sequence[Mapping[str, object]] | Mapping[str, object],
    ) -> Path:
        paths = self._paths_for_job(job_id)
        payload = dict(links) if isinstance(links, Mapping) else [dict(link) for link in links]
        paths.links_path.parent.mkdir(parents=True, exist_ok=True)
        paths.links_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return paths.links_path

    def write_markdown(self, *, job_id: str, content: str) -> Path:
        paths = self._paths_for_job(job_id)
        paths.markdown_path.parent.mkdir(parents=True, exist_ok=True)
        paths.markdown_path.write_text(content, encoding="utf-8")
        return paths.markdown_path

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
        manifest_path = self._paths_for_job(job_id).manifest_path
        if not manifest_path.exists():
            raise FileNotFoundError(manifest_path)
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    def read_markdown(self, job_id: str) -> str:
        paths = self._paths_for_job(job_id)
        if not paths.markdown_path.exists():
            raise FileNotFoundError(paths.markdown_path)
        return paths.markdown_path.read_text(encoding="utf-8")

    def read_links(self, job_id: str) -> list[dict[str, Any]] | dict[str, Any]:
        paths = self._paths_for_job(job_id)
        if not paths.links_path.exists():
            return []
        payload = json.loads(paths.links_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list) and all(isinstance(link, dict) for link in payload):
            return payload
        raise ValueError(
            f"Invalid links payload in {paths.links_path}: expected an object or object list"
        )

    def resolve_artifact(self, job_id: str, relative_path: str) -> Path:
        paths = self._paths_for_job(job_id)
        target = (paths.root / relative_path).resolve()
        root = paths.root.resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise FileNotFoundError(relative_path)
        if not target.exists():
            raise FileNotFoundError(target)
        return target

    def insert_links(self, *, job_id: str, links: Iterable[Mapping[str, object]]) -> None:
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
                text=str(item.get("text", "")),
                rel=_coerce_rel(item.get("rel")),
                source=str(item.get("source", "dom")),
            )
            for item in links
        ]
        if not records:
            return
        with self.session() as session:
            session.add_all(records)
            session.commit()

    def fetch_run(self, job_id: str) -> RunRecord | None:
        return self._fetch_run(job_id)

    def append_job_event(
        self, *, job_id: str, owner: str | None, entry: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Allocate both cursors and commit the complete entry in one transaction.

        Called by the manager's bounded writer outside the API event loop.
        BEGIN IMMEDIATE serializes competing managers before reading counters.
        """
        # Freeze nested input and reject non-JSON/nonfinite values before a write.
        payload = json.loads(json.dumps(entry, allow_nan=False))
        with self.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            self._append_event_in_session(session, job_id=job_id, owner=owner, payload=payload)
            session.commit()
        return payload

    def _append_event_in_session(
        self, session: Session, *, job_id: str, owner: str | None, payload: dict[str, Any]
    ) -> None:
        scope_key = owner or ""
        grant = session.get(ResourceOwner, f"job:{job_id}")
        run = session.get(RunRecord, job_id)
        expected_owner = grant.owner_key if grant else run.owner_key if run else None
        if owner != expected_owner:
            raise PermissionError("Event owner does not match the durable job owner")
        previous = session.exec(
            select(JobEventRecord)
            .where(JobEventRecord.job_id == job_id)
            .order_by(col(JobEventRecord.sequence).desc())
            .limit(1)
        ).first()
        if previous is not None and previous.scope_key != scope_key:
            raise PermissionError("Event history owner cannot change")
        sequence = previous.sequence + 1 if previous else 0
        cursor = session.get(EventCursorRecord, scope_key)
        if cursor is None:
            cursor = EventCursorRecord(scope_key=scope_key, epoch=uuid4().hex)
        cursor.sequence += 1
        payload.update(job_id=job_id, sequence=sequence, cursor=f"{cursor.epoch}:{cursor.sequence}")
        if isinstance(payload.get("snapshot"), dict):
            payload["snapshot"]["sequence"] = sequence
        session.add(cursor)
        session.add(
            JobEventRecord(
                job_id=job_id,
                sequence=sequence,
                scope_key=scope_key,
                global_sequence=cursor.sequence,
                payload=payload,
            )
        )

    def recover_terminal_event(self, *, job_id: str, owner: str | None) -> dict[str, Any] | None:
        """Reconcile a terminal run/event crash window without inventing past stages."""
        with self.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            run = session.get(RunRecord, job_id)
            if run is None or run.owner_key != owner:
                return None
            latest = session.exec(
                select(JobEventRecord)
                .where(
                    JobEventRecord.job_id == job_id,
                    col(JobEventRecord.payload)["event"].as_string() == "snapshot",
                )
                .order_by(col(JobEventRecord.sequence).desc())
                .limit(1)
            ).first()
            if latest is None:
                return None
            snapshot = dict(latest.payload["snapshot"])
            recorded_state = snapshot.get("state")
            terminal = {"DONE", "FAILED", "CANCELLED"}
            if run.status not in terminal and recorded_state in {"FAILED", "CANCELLED"}:
                run.status = str(recorded_state)
                run.finished_at = run.finished_at or datetime.now(timezone.utc)
                session.add(run)
                session.commit()
                return None
            if run.status not in terminal or recorded_state == run.status:
                return None
            snapshot.update(
                id=run.id, url=run.url, state=run.status, manifest_path=run.manifest_path
            )
            if run.status == "DONE":
                snapshot["progress"] = {"done": run.tiles_total or 0, "total": run.tiles_total or 0}
            payload = {
                "event": "snapshot",
                "snapshot": snapshot,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": {
                    "reason": "terminal-record-recovery",
                    "previous_sequence": latest.sequence,
                },
            }
            self._append_event_in_session(session, job_id=job_id, owner=owner, payload=payload)
            session.commit()
            return payload

    def read_job_events(
        self, job_id: str, *, owner: str | None, limit: int = 500
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("Event history limit must be between 1 and 500")
        with self.session() as session:
            records = session.exec(
                select(JobEventRecord)
                .where(JobEventRecord.job_id == job_id, JobEventRecord.scope_key == (owner or ""))
                .order_by(col(JobEventRecord.sequence).desc())
                .limit(limit)
            ).all()
            return [record.payload for record in reversed(records)]

    def read_global_events(
        self, *, owner: str | None, limit: int = 500
    ) -> tuple[str, int, list[dict[str, Any]]]:
        if not 1 <= limit <= 500:
            raise ValueError("Event history limit must be between 1 and 500")
        scope_key = owner or ""
        with self.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            cursor = session.get(EventCursorRecord, scope_key)
            if cursor is None:
                cursor = EventCursorRecord(scope_key=scope_key, epoch=uuid4().hex)
                session.add(cursor)
                session.flush()
            records = session.exec(
                select(JobEventRecord)
                .where(JobEventRecord.scope_key == scope_key)
                .order_by(col(JobEventRecord.global_sequence).desc())
                .limit(limit)
            ).all()
            result = (
                cursor.epoch,
                cursor.sequence + 1,
                [record.payload for record in reversed(records)],
            )
            session.commit()
            return result

    def _fetch_run(self, job_id: str) -> RunRecord | None:
        with self.session() as session:
            record = session.get(RunRecord, job_id)
            owner = current_owner()
            if record is not None and owner is not None and record.owner_key != owner:
                return None
            return record

    def delete_run(self, job_id: str) -> bool:
        """Delete the RunRecord (and any related rows) for a job.

        Returns True if a row was deleted, False if there was nothing to delete.
        Cached artifacts on disk are NOT removed by this method (the route
        does that); this only handles the SQLite row.
        """
        from sqlalchemy import delete as _delete

        with self.session() as session:
            row = session.get(RunRecord, job_id)
            if row is None:
                return False
            owner = current_owner()
            if owner is not None and row.owner_key != owner:
                return False
            session.connection().exec_driver_sql(
                "DELETE FROM section_embeddings WHERE run_id = :id",
                {"id": job_id},
            )
            session.exec(_delete(LinkRecord).where(col(LinkRecord.run_id) == job_id))
            session.exec(_delete(WebhookRecord).where(col(WebhookRecord.job_id) == job_id))
            session.exec(
                _delete(ResourceOwner).where(col(ResourceOwner.resource) == f"job:{job_id}")
            )
            session.exec(_delete(RunRecord).where(col(RunRecord.id) == job_id))
            session.commit()
            return True

    def path_reference_count(self, cache_path: str) -> int:
        with self.session() as session:
            return len(
                list(session.exec(select(RunRecord).where(col(RunRecord.cache_path) == cache_path)))
            )

    def _paths_for_job(self, job_id: str) -> RunPaths:
        record = self.fetch_run(job_id)
        if not record:
            raise KeyError(f"Run {job_id} not found")
        paths = RunPaths.from_record(record)
        root = paths.root.resolve()
        if not root.is_relative_to(self.config.cache_root.resolve()):
            raise FileNotFoundError("Run root is outside configured cache")
        for path in (
            paths.manifest_path,
            paths.markdown_path,
            paths.links_path,
            paths.dom_snapshot_path,
            paths.artifacts_manifest_path,
            paths.tiles_dir,
        ):
            if not path.resolve().is_relative_to(root):
                raise FileNotFoundError("Run artifact path escapes its root")
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

    def upsert_embeddings(
        self,
        *,
        run_id: str,
        sections,
    ) -> None:
        """Persist section embeddings for a run (replaces existing same-section_id rows)."""
        from .embeddings import upsert_embeddings as _upsert

        with self.session() as session:
            _upsert(session=session, run_id=run_id, sections=list(sections))

    def delete_section_embeddings(
        self,
        *,
        job_id: str,
        model: str | None = None,
    ) -> int:
        """Drop all section embeddings for a job (optionally filtered by model prefix)."""
        with self.session() as session:
            stmt = "DELETE FROM section_embeddings WHERE run_id = :rid"
            params: dict = {"rid": job_id}
            if model:
                stmt = (
                    "DELETE FROM section_embeddings WHERE run_id = :rid AND section_id LIKE :prefix"
                )
                params["prefix"] = f"{job_id}-%"
            result = session.connection().exec_driver_sql(stmt, params)
            session.commit()
            return int(result.rowcount or 0)

    def search_section_embeddings(
        self,
        *,
        job_id: str,
        vector: Sequence[float],
        top_k: int,
    ) -> tuple[int, list[EmbeddingMatch]]:
        with self.session() as session:
            return search_embeddings(
                session=session, run_id=job_id, query_vector=vector, top_k=top_k
            )


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


def _build_runtime_cache_key(
    *,
    cache_seed: str,
    backend_id: str,
    backend_mode: str,
    hardware_path: str,
    fallback_chain: Sequence[str],
) -> str:
    payload = {
        "cache_seed": cache_seed,
        "ocr_backend_id": backend_id,
        "ocr_backend_mode": backend_mode,
        "ocr_hardware_path": hardware_path,
        "ocr_fallback_chain": [entry for entry in fallback_chain if entry],
    }
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
    manifest_dict["cache_key"] = _build_runtime_cache_key(
        cache_seed=cache_seed,
        backend_id=backend_id,
        backend_mode=backend_mode,
        hardware_path=hardware_path,
        fallback_chain=chain,
    )


def _cache_segments(cache_key: str) -> tuple[str, str]:
    """Return (bucket, normalized_key) segments for cache directories."""

    normalized = re.sub(r"[^a-z0-9]", "-", cache_key.lower()).strip("-")
    if not normalized:
        normalized = "cache"
    bucket = normalized[:2].ljust(2, "0")
    return bucket, normalized


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
        # Set busy timeout to 30 seconds
        dbapi_connection.execute("PRAGMA busy_timeout=30000").fetchone()

    return engine


def _parse_tile_index(filename: str) -> int:
    try:
        stem = Path(filename).stem
        return int(stem.split("_")[-1])
    except Exception:
        return -1


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
    _set("ocr_model", manifest_dict.get("selected_ocr_model"))
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
