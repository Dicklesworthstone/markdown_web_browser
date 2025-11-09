"""Pydantic DTOs shared across endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.embeddings import EMBEDDING_DIM


class JobCreateRequest(BaseModel):
    """Payload clients submit to kick off a capture job."""

    url: str = Field(description="Target URL to capture")
    profile_id: str | None = Field(default=None, description="Browser profile identifier")
    viewport_width: int | None = Field(default=None, ge=1, description="Override viewport width")
    viewport_height: int | None = Field(default=None, ge=1, description="Override viewport height")
    device_scale_factor: int | None = Field(default=None, ge=1, description="Override device scale factor")
    color_scheme: str | None = Field(default=None, description="Override color scheme (light|dark)")
    long_side_px: int | None = Field(default=None, ge=1, description="Override tile longest side policy")
    reuse_cache: bool = Field(default=True, description="Reuse cached captures when an identical configuration exists")


class ReplayRequest(BaseModel):
    """Payload for replaying a stored manifest."""

    manifest: dict[str, Any] = Field(description="Manifest JSON to replay")

    @field_validator("manifest")
    @classmethod
    def _require_url(cls, value: dict[str, Any]) -> dict[str, Any]:
        url = value.get("url")
        if not isinstance(url, str) or not url.strip():
            msg = "Manifest must include a non-empty 'url' field"
            raise ValueError(msg)
        return value


class JobSnapshotResponse(BaseModel):
    """Lightweight job view for polling and SSE streaming."""

    id: str
    state: str
    url: str
    progress: dict[str, int] | None = Field(default=None, description="Tile progress (done vs total)")
    manifest_path: str | None = Field(default=None, description="Filesystem path to manifest.json if persisted")
    manifest: ManifestMetadata | dict[str, Any] | None = Field(
        default=None,
        description="Latest manifest payload if available",
    )
    error: str | None = Field(default=None, description="Failure message when state=FAILED")
    profile_id: str | None = Field(default=None, description="Profile identifier requested for the capture")
    cache_hit: bool | None = Field(default=None, description="True when the job reused cached artifacts")


class ConcurrencyWindow(BaseModel):
    """Min/max concurrency envelope for OCR/autopilot settings."""

    min: int = Field(ge=0, description="Minimum parallel OCR requests")
    max: int = Field(ge=0, description="Maximum parallel OCR requests")


class ViewportSettings(BaseModel):
    """Viewport and device-scale metadata."""

    width: int = Field(ge=1)
    height: int = Field(ge=1)
    device_scale_factor: int = Field(ge=1)
    color_scheme: str = Field(description="CSS color-scheme applied during capture")


class ManifestEnvironment(BaseModel):
    """Environment metadata echoed into manifest.json files."""

    cft_version: str = Field(description="Chrome for Testing label+build")
    cft_label: str = Field(description="Chrome for Testing track label")
    server_runtime: str = Field(
        default="uvicorn",
        description="ASGI server runtime handling the job (e.g., uvicorn or granian)",
    )
    playwright_channel: str = Field(description="Playwright browser channel")
    playwright_version: str | None = Field(default=None, description="Resolved Playwright version at runtime")
    browser_transport: str = Field(description="Browser transport (cdp or bidi)")
    viewport: ViewportSettings = Field(description="Viewport used during capture")
    viewport_overlap_px: int = Field(ge=0, description="Overlap between viewport sweeps")
    tile_overlap_px: int = Field(ge=0, description="Overlap between pyvips OCR tiles")
    scroll_settle_ms: int = Field(ge=0, description="Settle delay between sweeps")
    max_viewport_sweeps: int = Field(ge=1, description="Safety cap for sweep count")
    screenshot_style_hash: str = Field(description="Hash of screenshot mask/style bundle")
    screenshot_mask_selectors: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Selectors masked during screenshot capture",
    )
    ocr_model: str = Field(description="olmOCR model identifier")
    ocr_use_fp8: bool = Field(description="Whether FP8 acceleration is enabled")
    ocr_concurrency: ConcurrencyWindow = Field(description="Concurrency envelope for OCR requests")


class ManifestWarning(BaseModel):
    """Structured warning emitted during capture."""

    code: str = Field(description="Stable identifier (e.g., canvas-heavy)")
    message: str = Field(description="Human-friendly details")
    count: float = Field(ge=0, description="Observed count/ratio triggering the warning")
    threshold: float = Field(ge=0, description="Configured threshold for the warning")


class ManifestTimings(BaseModel):
    """Timing metrics captured for each job."""

    capture_ms: int | None = Field(default=None, ge=0)
    ocr_ms: int | None = Field(default=None, ge=0)
    stitch_ms: int | None = Field(default=None, ge=0)
    total_ms: int | None = Field(default=None, ge=0)


class ManifestSweepStats(BaseModel):
    """Viewport sweep counters recorded for diagnostics."""

    sweep_count: int = Field(ge=0, description="Number of viewport sweeps performed")
    total_scroll_height: int = Field(ge=0, description="Final scroll height observed")
    shrink_events: int = Field(ge=0, description="How often the page height shrank mid-run")
    retry_attempts: int = Field(ge=0, description="Viewport sweep retries triggered by shrink events")
    overlap_pairs: int = Field(ge=0, description="Adjacent tile pairs compared for overlap matching")
    overlap_match_ratio: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Ratio of overlap pairs that matched (duplicate seams indicator)",
    )


class ManifestOCRBatch(BaseModel):
    """Per-request OCR telemetry persisted in manifests."""

    tile_ids: list[str]
    latency_ms: int = Field(ge=0)
    status_code: int = Field(ge=0)
    request_id: str | None = Field(default=None)
    payload_bytes: int | None = Field(default=None, ge=0)
    attempts: int = Field(default=1, ge=1)


class ManifestOCRQuota(BaseModel):
    """Quota accounting for hosted OCR usage."""

    limit: int | None = Field(default=None, ge=1)
    used: int | None = Field(default=None, ge=0)
    threshold_ratio: float = Field(default=0.7, ge=0.0, le=1.0)
    warning_triggered: bool = Field(default=False)


class ManifestMetadata(BaseModel):
    """Top-level manifest payload stub until capture pipeline is wired."""

    environment: ManifestEnvironment
    timings: ManifestTimings = Field(default_factory=ManifestTimings)
    tiles_total: int | None = Field(
        default=None,
        ge=0,
        description="Total OCR tiles emitted for the run",
    )
    long_side_px: int | None = Field(
        default=None,
        ge=0,
        description="Longest side (px) enforced during tiling",
    )
    sweep_stats: ManifestSweepStats | None = Field(
        default=None,
        description="Viewport sweep counters and overlap ratio metadata",
    )
    overlap_match_ratio: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Shortcut for sweep_stats.overlap_match_ratio when summarizing",
    )
    blocklist_version: str | None = Field(
        default=None,
        description="Version label for the selector blocklist used during capture",
    )
    blocklist_hits: dict[str, int] = Field(
        default_factory=dict,
        description="Selectors hidden during capture mapped to hit counts",
    )
    warnings: list[ManifestWarning] = Field(
        default_factory=list,
        description="Structured warnings emitted by capture heuristics",
    )
    validation_failures: list[str] = Field(
        default_factory=list,
        description="Tile validation failures (checksums, PNG decode, dimensions)",
    )
    ocr_batches: list[ManifestOCRBatch] = Field(
        default_factory=list,
        description="Per-request OCR telemetry (request IDs, latency, payload sizes)",
    )
    ocr_quota: ManifestOCRQuota | None = Field(
        default=None,
        description="Snapshot of hosted OCR daily quota usage",
    )
    profile_id: str | None = Field(
        default=None,
        description="Browser profile identifier used for the capture, when specified",
    )
    cache_hit: bool | None = Field(
        default=None,
        description="True when artifacts were reused from cache instead of running a new capture",
    )
    cache_key: str | None = Field(
        default=None,
        description="Deterministic hash used to look up cached captures",
    )
    dom_assists: list[dict[str, Any]] = Field(
        default_factory=list,
        description="DOM overlays injected to repair low-confidence OCR spans",
    )


class EmbeddingSearchRequest(BaseModel):
    """Payload for querying sqlite-vec section embeddings."""

    vector: list[float] = Field(description="Normalized embedding vector", min_length=EMBEDDING_DIM, max_length=EMBEDDING_DIM)
    top_k: int = Field(default=5, ge=1, le=50)

    @field_validator("vector")
    @classmethod
    def _validate_vector(cls, value: list[float]) -> list[float]:
        if len(value) != EMBEDDING_DIM:
            msg = f"Expected embedding length {EMBEDDING_DIM}, received {len(value)}"
            raise ValueError(msg)
        return value


class SectionEmbeddingMatch(BaseModel):
    """Single section similarity result."""

    section_id: str
    tile_start: int | None = None
    tile_end: int | None = None
    similarity: float
    distance: float


class EmbeddingSearchResponse(BaseModel):
    """Response envelope for embeddings jump-to-section queries."""

    total_sections: int
    matches: list[SectionEmbeddingMatch]


class WebhookRegistrationRequest(BaseModel):
    """Webhook callback registration payload."""

    url: str = Field(description="Callback URL to invoke on job events")
    events: list[str] | None = Field(
        default=None,
        description="States that should trigger the webhook (defaults to DONE/FAILED)",
    )


class WebhookSubscription(BaseModel):
    """Persisted webhook metadata returned by the API."""

    url: str
    events: list[str]
    created_at: datetime


class WebhookDeleteRequest(BaseModel):
    """Request body for deleting webhook registrations."""

    id: int | None = Field(default=None, description="Webhook record ID to delete")
    url: str | None = Field(default=None, description="Webhook URL to delete")

    @model_validator(mode="after")
    def _require_selector(self) -> WebhookDeleteRequest:
        if self.id is None and not self.url:
            raise ValueError("Provide id or url to delete a webhook")
        return self
