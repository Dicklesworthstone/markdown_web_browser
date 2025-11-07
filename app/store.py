"""Persistence helpers for artifacts, manifests, and sqlite-vec metadata."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import tarfile

import sqlite_vec
import zstandard as zstd
from sqlalchemy import event, text
from sqlmodel import Field, Session, SQLModel, create_engine

from app.tiler import TileSlice

from .embeddings import EMBEDDING_DIM, EmbeddingMatch, search_embeddings
from .settings import load_config

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
    cft_label: str | None = None
    cft_version: str | None = None
    playwright_version: str | None = None
    browser_transport: str | None = None
    screenshot_style_hash: str | None = None
    long_side_px: int | None = None
    device_scale_factor: int | None = None
    tiles_total: int | None = None
    capture_ms: int | None = None
    ocr_ms: int | None = None
    stitch_ms: int | None = None


class LinkRecord(SQLModel, table=True):
    """Persisted DOM/OCR links for quick agent retrieval."""

    __tablename__ = "links"

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(foreign_key="runs.id")
    href: str
    text: str
    rel: str | None = None
    source: str = Field(default="dom", description="dom|ocr|hybrid")


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

    @classmethod
    def from_url(cls, *, url: str, started_at: datetime, config: StorageConfig) -> RunPaths:
        host, slug = _split_url(url)
        timestamp = started_at.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)
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
        )

    def ensure_directories(self) -> None:
        self.tiles_dir.mkdir(parents=True, exist_ok=True)

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
        )


class Store:
    """Facade around SQLite + filesystem persistence."""

    def __init__(self, config: StorageConfig | None = None) -> None:
        self.config = config or StorageConfig.from_env()
        self.config.cache_root.mkdir(parents=True, exist_ok=True)
        self.engine = _create_engine(self.config.db_path)
        SQLModel.metadata.create_all(self.engine)
        self._ensure_vec_table()

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

    @contextmanager
    def session(self) -> Iterator[Session]:
        with Session(self.engine) as session:
            yield session

    def allocate_run(self, *, job_id: str, url: str, started_at: datetime) -> RunPaths:
        paths = RunPaths.from_url(url=url, started_at=started_at, config=self.config)
        paths.ensure_directories()
        record = RunRecord(
            id=job_id,
            url=url,
            started_at=started_at,
            cache_path=str(paths.root),
            manifest_path=str(paths.manifest_path),
        )
        with self.session() as session:
            session.add(record)
            session.commit()
        return paths

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
            manifest_path = Path(record.manifest_path)
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(manifest_dict, indent=2), encoding="utf-8")
            _apply_manifest_metadata(record, manifest_dict)
            session.add(record)
            session.commit()
            return manifest_path

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
                    "source_y_offset": tile.source_y_offset,
                    "viewport_y_offset": tile.viewport_y_offset,
                    "overlap_px": tile.overlap_px,
                    "top_overlap_sha256": tile.top_overlap_sha256,
                    "bottom_overlap_sha256": tile.bottom_overlap_sha256,
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
        if not paths.links_path.exists():
            return []
        return json.loads(paths.links_path.read_text(encoding="utf-8"))

    def resolve_artifact(self, job_id: str, relative_path: str) -> Path:
        paths = self._paths_for_job(job_id)
        target = (paths.root / relative_path).resolve()
        root = paths.root.resolve()
        if not str(target).startswith(str(root)):
            raise FileNotFoundError(relative_path)
        if not target.exists():
            raise FileNotFoundError(target)
        return target

    def insert_links(self, *, job_id: str, links: Iterable[Mapping[str, str]]) -> None:
        records = [
            LinkRecord(
                run_id=job_id,
                href=item.get("href", ""),
                text=item.get("text", ""),
                rel=item.get("rel"),
                source=item.get("source", "dom"),
            )
            for item in links
        ]
        if not records:
            return
        with self.session() as session:
            session.add_all(records)
            session.commit()

    def fetch_run(self, job_id: str) -> RunRecord | None:
        with self.session() as session:
            return session.get(RunRecord, job_id)

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

    def search_section_embeddings(
        self,
        *,
        job_id: str,
        vector: Sequence[float],
        top_k: int,
    ) -> tuple[int, list[EmbeddingMatch]]:
        with self.session() as session:
            return search_embeddings(session=session, run_id=job_id, query_vector=vector, top_k=top_k)


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


def _create_engine(db_path: Path):
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, connection_record) -> None:  # type: ignore[override]
        dbapi_connection.enable_load_extension(True)
        sqlite_vec.load(dbapi_connection)

    return engine


def _apply_manifest_metadata(record: RunRecord, manifest: Mapping[str, object] | Any) -> None:
    manifest_dict = _manifest_to_dict(manifest)

    def _set(attr: str, value: Any) -> None:
        if value is not None:
            setattr(record, attr, value)

    def _set_int(attr: str, value: Any) -> None:
        coerced = _coerce_int(value)
        if coerced is not None:
            setattr(record, attr, coerced)

    environment = manifest_dict.get("environment")
    if isinstance(environment, Mapping):
        _set("cft_version", environment.get("cft_version"))
        _set("cft_label", environment.get("cft_label"))
        _set("playwright_version", environment.get("playwright_version"))
        _set("browser_transport", environment.get("browser_transport"))
        _set("screenshot_style_hash", environment.get("screenshot_style_hash"))
        _set("ocr_model", environment.get("ocr_model"))
        _set("ocr_provider", environment.get("ocr_provider"))
        viewport = environment.get("viewport")
        if isinstance(viewport, Mapping):
            _set_int("device_scale_factor", viewport.get("device_scale_factor"))
    else:  # legacy flat manifests
        _set("cft_version", manifest_dict.get("cft_version"))
        _set("cft_label", manifest_dict.get("cft_label"))
        _set("playwright_version", manifest_dict.get("playwright_version"))
        _set("browser_transport", manifest_dict.get("browser_transport"))
        _set("screenshot_style_hash", manifest_dict.get("screenshot_style_hash"))
        _set("ocr_model", manifest_dict.get("model"))
        _set("ocr_provider", manifest_dict.get("ocr_provider"))
        _set_int("device_scale_factor", manifest_dict.get("device_scale_factor"))

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


def _manifest_to_dict(manifest: Mapping[str, object] | Any) -> dict[str, Any]:
    if isinstance(manifest, Mapping):
        return dict(manifest)
    if hasattr(manifest, "model_dump"):
        return manifest.model_dump()  # type: ignore[no-any-return]
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
    "RunPaths",
    "StorageConfig",
    "Store",
    "build_store",
]
