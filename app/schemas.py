"""Pydantic DTOs shared across endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from app.embeddings import EMBEDDING_DIM


class JobCreateRequest(BaseModel):
    """Payload clients submit to kick off a capture job."""

    url: str = Field(description="Target URL to capture")

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("URL cannot be empty")

        # Basic URL format validation
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("URL must have a valid scheme and domain")

        if parsed.scheme.lower() not in ("http", "https"):
            raise ValueError("URL scheme must be http or https")

        return value

    profile_id: str | None = Field(default=None, description="Browser profile identifier")
    viewport_width: int | None = Field(
        default=None, ge=1, le=32767, description="Override viewport width"
    )
    viewport_height: int | None = Field(
        default=None, ge=1, le=32767, description="Override viewport height"
    )
    device_scale_factor: int | None = Field(
        default=None, ge=1, le=10, description="Override device scale factor"
    )
    color_scheme: str | None = Field(default=None, description="Override color scheme (light|dark)")

    @field_validator("color_scheme")
    @classmethod
    def _validate_color_scheme(cls, value: str | None) -> str | None:
        if value is None:
            return value

        value = value.strip().lower()
        if value not in ("light", "dark"):
            raise ValueError("color_scheme must be 'light' or 'dark'")

        return value

    long_side_px: int | None = Field(
        default=None, ge=1, le=16384, description="Override tile longest side policy"
    )
    reuse_cache: bool = Field(
        default=True, description="Reuse cached captures when an identical configuration exists"
    )
    semantic_post_enabled: bool = Field(
        default=False,
        description=(
            "Run the optional LLM semantic post-processing pass on the stitched "
            "Markdown. Requires SEMANTIC_POST_ENDPOINT (and optional model/api_key) "
            "to be set in the environment; no-op otherwise."
        ),
    )
    semantic_post_max_chars: int | None = Field(
        default=None,
        ge=1,
        le=200_000,
        description="Cap the Markdown characters sent to the semantic fixer (default 50000).",
    )


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
    progress: dict[str, int] | None = Field(
        default=None, description="Tile progress (done vs total)"
    )
    manifest_path: str | None = Field(
        default=None, description="Filesystem path to manifest.json if persisted"
    )
    manifest: ManifestMetadata | dict[str, Any] | None = Field(
        default=None,
        description="Latest manifest payload if available",
    )
    error: str | None = Field(default=None, description="Failure message when state=FAILED")
    profile_id: str | None = Field(
        default=None, description="Profile identifier requested for the capture"
    )
    cache_hit: bool | None = Field(
        default=None, description="True when the job reused cached artifacts"
    )


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
    playwright_version: str | None = Field(
        default=None, description="Resolved Playwright version at runtime"
    )
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
    ocr_provider: str | None = Field(
        default=None,
        description="OCR provider family (for example olmocr or glm-ocr)",
    )
    ocr_use_fp8: bool = Field(description="Whether FP8 acceleration is enabled")
    ocr_concurrency: ConcurrencyWindow = Field(description="Concurrency envelope for OCR requests")
    ocr_backend_id: str = Field(
        default="olmocr-remote-openai",
        description="Resolved backend identifier used for OCR requests",
    )
    ocr_backend_mode: str = Field(
        default="openai-compatible",
        description="Backend protocol mode (for example openai-compatible or maas)",
    )
    ocr_hardware_path: str = Field(
        default="remote",
        description="Hardware execution path (remote, local-auto, gpu, cpu)",
    )
    ocr_fallback_chain: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Ordered backend fallback chain considered by runtime policy",
    )


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
    semantic_post_ms: int | None = Field(
        default=None,
        ge=0,
        description="Wall-clock time spent in the optional LLM semantic post-processor.",
    )


class ManifestSweepStats(BaseModel):
    """Viewport sweep counters recorded for diagnostics."""

    sweep_count: int = Field(ge=0, description="Number of viewport sweeps performed")
    total_scroll_height: int = Field(ge=0, description="Final scroll height observed")
    shrink_events: int = Field(ge=0, description="How often the page height shrank mid-run")
    retry_attempts: int = Field(
        ge=0, description="Viewport sweep retries triggered by shrink events"
    )
    overlap_pairs: int = Field(
        ge=0, description="Adjacent tile pairs compared for overlap matching"
    )
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


class ManifestDeduplicationStats(BaseModel):
    """Deduplication statistics for tile overlap removal."""

    total_events: int = Field(ge=0, description="Total deduplication attempts")
    lines_removed: int = Field(ge=0, description="Total lines removed across all tiles")
    exact_matches: int = Field(ge=0, description="Deduplication via exact matching")
    sequence_matches: int = Field(ge=0, description="Deduplication via sequence matching")
    fuzzy_matches: int = Field(ge=0, description="Deduplication via fuzzy matching")
    no_matches: int = Field(ge=0, description="Tiles with overlap but no confident match")
    avg_similarity: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Average similarity score for successful matches",
    )


class ManifestMetadata(BaseModel):
    """Top-level manifest metadata for a capture run (CfT/Playwright/timings/OCR telemetry)."""

    environment: ManifestEnvironment
    timings: ManifestTimings = Field(default_factory=ManifestTimings)
    backend_id: str | None = Field(
        default=None,
        description="Resolved OCR backend identifier for this run",
    )
    backend_mode: str | None = Field(
        default=None,
        description="Resolved OCR backend mode for this run",
    )
    hardware_path: str | None = Field(
        default=None,
        description="Hardware path selected for OCR execution",
    )
    backend_reason_codes: list[str] = Field(
        default_factory=list,
        description="Normalized OCR policy reason codes explaining backend selection",
    )
    backend_reevaluate_after_s: int | None = Field(
        default=None,
        ge=1,
        description="Suggested interval for policy re-evaluation/failover checks",
    )
    fallback_chain: list[str] = Field(
        default_factory=list,
        description="Ordered fallback backend IDs evaluated by OCR runtime policy",
    )
    hardware_capabilities: dict[str, Any] | None = Field(
        default=None,
        description="Detected host CPU/GPU capability snapshot used by runtime policy",
    )
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
    ocr_local_service: dict[str, Any] | None = Field(
        default=None,
        description="Local OCR lifecycle metadata (reuse/autostart/restart diagnostics)",
    )
    ocr_failover_events: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Structured OCR backend failover transitions and circuit-breaker events",
    )
    profile_id: str | None = Field(
        default=None,
        description="Browser profile identifier used for the capture, when specified",
    )
    cache_hit: bool | None = Field(
        default=None,
        description="True when artifacts were reused from cache instead of running a new capture",
    )
    cache_seed: str | None = Field(
        default=None,
        description="Backend-agnostic cache fingerprint seed derived from capture/OCR inputs",
    )
    cache_key: str | None = Field(
        default=None,
        description="Deterministic hash used to look up cached captures (includes runtime backend path)",
    )
    dom_assists: list[dict[str, Any]] = Field(
        default_factory=list,
        description="DOM overlays injected to repair low-confidence OCR spans",
    )
    dom_assist_summary: dict[str, Any] | None = Field(
        default=None,
        description="Aggregated DOM-assist counts/reasons for quick diagnostics",
    )
    semantic_post_summary: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional LLM semantic post-processor summary (status, applied, "
            "elapsed_ms, provider, reason). Null when the pass was not requested."
        ),
    )
    seam_markers: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Seam hash metadata keyed by tile index/position to trace stitched boundaries",
    )
    seam_marker_events: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Logged seam-fallback decisions with tile pair metadata",
    )
    dedup_events: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Tile overlap deduplication events with removal counts and methods",
    )
    dedup_summary: ManifestDeduplicationStats | None = Field(
        default=None,
        description="Aggregated deduplication statistics for quick diagnostics",
    )

    @model_validator(mode="after")
    def _normalize_backend_fields(self) -> ManifestMetadata:
        """Backfill top-level backend fields from the environment block.

        This keeps contract-v2 manifests explicit while preserving compatibility with
        older payload shapes that only include environment metadata.
        """

        if self.backend_id is None:
            self.backend_id = self.environment.ocr_backend_id
        if self.backend_mode is None:
            self.backend_mode = self.environment.ocr_backend_mode
        if self.hardware_path is None:
            self.hardware_path = self.environment.ocr_hardware_path
        if not self.fallback_chain:
            fallback = list(self.environment.ocr_fallback_chain)
            if not fallback and self.backend_id:
                fallback = [self.backend_id]
            self.fallback_chain = fallback
        return self


class EmbeddingSearchRequest(BaseModel):
    """Payload for querying sqlite-vec section embeddings."""

    vector: list[float] = Field(
        description="Normalized embedding vector",
        min_length=EMBEDDING_DIM,
        max_length=EMBEDDING_DIM,
    )
    top_k: int = Field(default=5, ge=1, le=50)


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

    @field_validator("url")
    @classmethod
    def _validate_webhook_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Webhook URL cannot be empty")

        # Basic URL format validation
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("Webhook URL must have a valid scheme and domain")

        if parsed.scheme.lower() not in ("http", "https"):
            raise ValueError("Webhook URL scheme must be http or https")

        return value

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

    @field_validator("url")
    @classmethod
    def _validate_delete_url(cls, value: str | None) -> str | None:
        if value is None:
            return value

        value = value.strip()
        if not value:
            raise ValueError("Webhook URL cannot be empty if provided")

        # Basic URL format validation
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("Webhook URL must have a valid scheme and domain")

        if parsed.scheme.lower() not in ("http", "https"):
            raise ValueError("Webhook URL scheme must be http or https")

        return value

    @model_validator(mode="after")
    def _require_selector(self) -> WebhookDeleteRequest:
        if self.id is None and not self.url:
            raise ValueError("Provide id or url to delete a webhook")
        return self


class BatchJobRequest(BaseModel):
    """Submit N URLs in one round-trip; each becomes its own job.

    Agents use this for "process this list of pages" workflows so they don't
    pay N×RTT overhead. The response is a list of per-URL outcomes mirroring
    the single-URL POST /jobs contract (id, state, cache_hit, etc.).
    """

    urls: list[str] = Field(..., min_length=1, max_length=200, description="URLs to capture in parallel")
    profile_id: str | None = Field(default=None)
    reuse_cache: bool = True
    ocr_policy: str | None = None
    tags: list[str] = Field(
        default_factory=list,
        description="Free-form labels (e.g. 'dataset:2026-q1', 'team:research') for later /jobs filtering",
    )


class JobTagRequest(BaseModel):
    """Append a single tag to a job's tag set."""

    tag: str = Field(..., min_length=1, max_length=200, description="Tag label to append")


class JobTagResponse(BaseModel):
    """Response for POST /jobs/{id}/tag."""

    job_id: str
    tags: list[str] = Field(default_factory=list)


class BatchJobItem(BaseModel):
    """Single-URL outcome inside a batch submission."""

    url: str
    job_id: str | None = None
    state: str | None = None
    cache_hit: bool = False
    error: str | None = None


class BatchJobResponse(BaseModel):
    """Response for POST /jobs/batch."""

    submitted: int
    cache_hits: int
    queued: int
    failed: int
    items: list[BatchJobItem]


class JobListItem(BaseModel):
    """Compact summary entry for GET /jobs."""

    id: str
    url: str
    state: str
    created_at: str | None = None
    finished_at: str | None = None
    cache_hit: bool = False
    profile_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    error: str | None = None
    manifest_path: str | None = None


class JobListResponse(BaseModel):
    """Filtered list of jobs for GET /jobs."""

    items: list[JobListItem]
    total: int
    filtered: int
    filters: dict[str, str]


class StructuredSection(BaseModel):
    """A heading + its associated body, parsed from the stitched Markdown."""

    level: int = Field(ge=1, le=6, description="Heading level 1-6")
    heading: str
    body: str
    anchor: str | None = Field(default=None, description="GitHub-style slug for cross-referencing")
    tile_indices: list[int] = Field(default_factory=list)


class StructuredLink(BaseModel):
    """A normalized outbound link with provenance."""

    href: str
    text: str | None = None
    title: str | None = None
    source: str = Field(description="dom | ocr | both")
    delta: str | None = Field(default=None, description="match | mismatch | null")


class StructuredResult(BaseModel):
    """Machine-friendly, parsed result for /jobs/{id}/result.json.

    Sections + structured links let agents extract a table-of-contents or
    answer "give me the headings" without re-parsing Markdown. Headings come
    from the stitched output; links are the DOM/OCR blend.
    """

    job_id: str
    url: str
    state: str
    word_count: int
    char_count: int
    sections: list[StructuredSection]
    links: list[StructuredLink]
    cache_hit: bool = False
    profile_id: str | None = None


class CrawlRequest(BaseModel):
    """Request to crawl a seed URL with depth-1 expansion via the existing capture pipeline.

    Agents use this to bulk-discover and capture multiple pages sharing a domain in one
    request, reusing the same OCR/concurrency settings across the entire crawl.
    """

    url: HttpUrl = Field(..., description="Seed URL to crawl (becomes depth 0)")
    max_pages: int = Field(default=10, ge=1, le=200, description="Hard cap on captured pages")
    max_depth: int = Field(default=1, ge=0, le=2, description="0=seed only, 1=seed+discovered, 2=two hops")
    domain_allowlist: list[str] = Field(
        default_factory=list,
        description="Restrict expansion to these domains (and subdomains). Empty = follow any link.",
    )
    respect_robots_txt: bool = Field(default=True, description="Honor robots.txt Disallow rules")
    crawl_delay_ms: int = Field(default=500, ge=0, le=60_000, description="Delay between requests (ms)")
    reuse_cache: bool = Field(default=True, description="Reuse content-addressed captures")
    profile_id: str | None = Field(default=None, description="Auth profile id (passed to each child job)")
    ocr_policy: str | None = Field(default=None, description="OCR policy key for each child")


class CrawlResponse(BaseModel):
    """Initial response for /jobs/crawl: the crawl id and queued seed."""

    crawl_id: str
    seed_url: str
    status: str
    started_at: str
    child_job_ids: list[str] = Field(default_factory=list)
    queued_urls: list[str] = Field(default_factory=list)


class CrawlUrlResult(BaseModel):
    """Per-URL outcome inside a crawl."""

    url: str
    status: str
    job_id: str | None
    depth: int
    discovered_links: int


class CrawlStatusResponse(BaseModel):
    """Live status of a crawl, including per-URL outcomes as they complete."""

    crawl_id: str
    seed_url: str
    status: str
    started_at: str
    finished_at: str | None
    max_pages: int
    max_depth: int
    visited: int
    completed: int
    failed: int
    pending: int
    queued_urls: list[str]
    results: list[CrawlUrlResult]


class JobRerunResponse(BaseModel):
    """Response for POST /jobs/{id}/rerun.

    A new job_id is created (so tags/cache/profile_id/profile persist) but the
    new run is independent (failure of original does not block re-run; success
    of re-run does not retroactively mark original as DONE).
    """

    original_job_id: str
    new_job_id: str
    url: str
    reuse_cache: bool = False


class JobDiffRequest(BaseModel):
    """Request for POST /jobs/{id}/diff."""

    other_job_id: str = Field(..., min_length=1, max_length=128)
    include_links: bool = Field(default=True, description="Also diff link sets")
    max_chars_per_section: int = Field(default=2000, ge=0, le=50_000)


class JobDiffSection(BaseModel):
    """One section-level diff entry."""

    heading: str
    anchor: str | None = None
    state: str = Field(description="added | removed | changed | unchanged")
    a_chars: int = 0
    b_chars: int = 0
    a_excerpt: str = ""
    b_excerpt: str = ""


class JobDiffResponse(BaseModel):
    """Response for POST /jobs/{id}/diff.

    Two captures compared at the section (heading) level. `added` = present in B
    but not A; `removed` = present in A but not B; `changed` = same heading
    with different body; `unchanged` = same heading + same body hash.
    """

    a_job_id: str
    b_job_id: str
    a_word_count: int
    b_word_count: int
    a_url: str
    b_url: str
    sections: list[JobDiffSection]
    links_a_only: list[str] = Field(default_factory=list)
    links_b_only: list[str] = Field(default_factory=list)
    links_common: list[str] = Field(default_factory=list)


class JobSearchRequest(BaseModel):
    """Request for POST /jobs/search.

    Full-text search across stored Markdown. Case-insensitive substring + simple
    boolean (split on whitespace; "word1 word2" matches both; quoted "exact
    phrase" matches literal substring). Returns up to ``limit`` matches ranked
    by word density.
    """

    query: str = Field(..., min_length=1, max_length=512)
    state: str | None = Field(default=None, description="Restrict to a given state (default: any)")
    limit: int = Field(default=25, ge=1, le=200)
    url_contains: str | None = None
    tag: str | None = None
    context_chars: int = Field(default=80, ge=0, le=400)


class JobSearchHit(BaseModel):
    """A single search hit."""

    job_id: str
    url: str
    state: str
    matched_line: str
    line_number: int
    score: float = Field(ge=0.0, description="Higher is better; density-based")


class JobSearchResponse(BaseModel):
    """Response for POST /jobs/search."""

    query: str
    matches: list[JobSearchHit]
    total_scanned: int
    returned: int


class EmbeddingTextRequest(BaseModel):
    """Request for POST /embeddings/text.

    Returns a deterministic 1536-dim float32 vector computed from the input
    text without shipping a model. Useful for agents that want to score
    candidate queries against a job's stored embeddings.
    """

    text: str = Field(..., min_length=1, max_length=20_000, description="Text to embed")
    model: str = Field(
        default="hash-bucket-v1",
        description=(
            "Embedder to use. Only 'hash-bucket-v1' is built-in; this is a "
            "hash-bucketed projection that gives a stable 1536-dim vector "
            "without any model weights. Agents that bring their own embedder "
            "should call /jobs/{id}/embeddings/search directly with the vector."
        ),
    )


class EmbeddingTextResponse(BaseModel):
    """Response for POST /embeddings/text."""

    model: str
    dim: int
    vector: list[float]
    text_chars: int


class Slosummary(BaseModel):
    """Per-category SLO summary, returned by /metrics/slo.json + /jobs/{id}/slo."""

    p50_total_ms: int | None = None
    p95_total_ms: int | None = None
    p50_capture_ms: int | None = None
    p95_capture_ms: int | None = None
    p50_ocr_ms: int | None = None
    p95_ocr_ms: int | None = None
    budget_ms: int | None = None
    budget_breaches: int = 0
    status: str = "unknown"
    count: int = 0


class JobTagMultiRequest(BaseModel):
    """Request to append multiple tags to a job in one call."""

    tags: list[str] = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Tags to append (deduplicated + sorted on persist).",
    )


class JobTagDeleteResponse(BaseModel):
    """Response for DELETE /jobs/{id}/tag/{tag}."""

    job_id: str
    removed: bool
    tags: list[str] = Field(default_factory=list)


class BatchStatusRequest(BaseModel):
    """Request for POST /jobs/batch/status."""

    job_ids: list[str] = Field(..., min_length=1, max_length=500)


class BatchStatusItem(BaseModel):
    """Compact status record for a single job in a batch status query."""

    job_id: str
    state: str
    cache_hit: bool = False
    url: str = ""
    progress: dict[str, int] | None = None
    error: str | None = None
    tags: list[str] = Field(default_factory=list)


class BatchStatusResponse(BaseModel):
    """Response for POST /jobs/batch/status."""

    count: int
    statuses: list[BatchStatusItem]


class JobArtifactsFile(BaseModel):
    """Single file inside /jobs/{id}/artifacts."""

    path: str
    absolute: str
    size: int
    kind: str


class JobArtifactsResponse(BaseModel):
    """Response for GET /jobs/{id}/artifacts."""

    job_id: str
    artifact_root: str
    file_count: int
    total_bytes: int
    files: list[JobArtifactsFile]
    synthetic: dict[str, str]


class JobLinksResponse(BaseModel):
    """Per-source link breakdown for GET /jobs/{id}/links."""

    job_id: str
    by_source: dict[str, list[dict]] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    total: int
    anchors: list[dict] = Field(default_factory=list)


class JobEventsJsonResponse(BaseModel):
    """Single-shot JSON dump of the event log."""

    job_id: str
    count: int
    events: list[dict] = Field(default_factory=list)


class JobShareResponse(BaseModel):
    """Response for POST /jobs/{id}/share."""

    job_id: str
    token: str
    expires_at: str
    share_url: str


class JobSharePublicResponse(BaseModel):
    """Public view of a job accessible via a share token (no API key needed).

    Mirrors a subset of JobSnapshotResponse so an external agent can use a
    shared run without enrolling in the host system.
    """

    job_id: str
    url: str
    state: str
    cache_hit: bool = False
    profile_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: str | None = None
    finished_at: str | None = None
    manifest: dict[str, Any] | None = None
    share_expires_at: str | None = None


class EmbeddingStoreRequest(BaseModel):
    """Request for POST /jobs/{id}/embeddings/store."""

    model: str = Field(
        default="hash-bucket-v1",
        description="Embedder to use (hash-bucket-v1 | openai-compatible | sentence-transformers)",
    )
    sections: list[str] | None = Field(
        default=None,
        description=(
            "Override the section list to embed (default: re-derive from "
            "result.json). Useful for incremental re-embedding."
        ),
    )
    replace: bool = Field(
        default=False,
        description="If true, drop existing embeddings before re-storing",
    )


class EmbeddingStoreResponse(BaseModel):
    """Response for POST /jobs/{id}/embeddings/store."""

    job_id: str
    model: str
    stored: int
    replaced: int
    dim: int
