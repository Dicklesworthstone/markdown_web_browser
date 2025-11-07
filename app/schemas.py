"""Pydantic DTOs shared across endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.embeddings import EMBEDDING_DIM


class JobCreateRequest(BaseModel):
    """Payload clients submit to kick off a capture job."""

    url: str = Field(description="Target URL to capture")
    profile_id: str | None = Field(default=None, description="Browser profile identifier")


class JobSnapshotResponse(BaseModel):
    """Lightweight job view for polling and SSE streaming."""

    id: str
    state: str
    url: str
    manifest: "ManifestMetadata | None" = Field(default=None, description="Latest manifest payload if available")


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
    count: int = Field(ge=0, description="Observed count triggering the warning")
    threshold: int = Field(ge=0, description="Configured threshold for the warning")


class ManifestTimings(BaseModel):
    """Timing metrics captured for each job."""

    capture_ms: int | None = Field(default=None, ge=0)
    ocr_ms: int | None = Field(default=None, ge=0)
    stitch_ms: int | None = Field(default=None, ge=0)
    total_ms: int | None = Field(default=None, ge=0)


class ManifestMetadata(BaseModel):
    """Top-level manifest payload stub until capture pipeline is wired."""

    environment: ManifestEnvironment
    timings: ManifestTimings = Field(default_factory=ManifestTimings)


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
    warnings: list[ManifestWarning] = Field(default_factory=list)
