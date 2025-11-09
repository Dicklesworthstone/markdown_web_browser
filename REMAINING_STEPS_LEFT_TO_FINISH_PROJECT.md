# REMAINING_STEPS_LEFT_TO_FINISH_PROJECT.md

## Executive Summary

The markdown_web_browser project has **excellent infrastructure** but is **missing its core functionality**. The codebase has comprehensive testing, monitoring, CLI tooling, API endpoints, database models, and supporting services - but lacks the actual browser automation and screenshot capture engine that would make it functional.

**Current State**: ~70% complete infrastructure, 0% core capture functionality
**Critical Path**: Implement browser capture → Add image processing → Complete OCR integration → Enable end-to-end flow

---

## 🚨 CRITICAL BLOCKERS (Must Fix First)

### 1. Browser Capture Implementation - THE CORE GAP
**Location**: `app/capture.py`
**Status**: Function references exist but implementation is missing
**Impact**: Nothing works without this

The `_perform_viewport_sweeps()` function is called but not defined. This is the heart of the entire system.

**Required Implementation**:
```python
async def _perform_viewport_sweeps(
    context: BrowserContext,
    config: CaptureConfig,
    viewport_overlap_px: int,
    tile_overlap_px: int,
    target_long_side_px: int,
    settle_ms: int,
    max_steps: int,
    mask_selectors: list[str]
) -> tuple[list[Path], str, list[dict], list[dict]]:
    """
    Must implement:
    1. Navigate to URL with page.goto()
    2. Execute deterministic scroll policy
    3. Capture viewport-sized screenshots at each position
    4. Apply blocklist CSS and masks
    5. Extract DOM snapshot for links/headings
    6. Return (image_paths, dom_html, validation_failures, seam_markers)
    """
    # Currently returns empty stub - needs full implementation
```

**Specific Missing Components**:
- Scroll stabilization with IntersectionObserver
- Screenshot capture at each viewport position
- SPA height-shrink detection and retry
- Canvas/WebGL content detection
- Lazy-load trigger mechanisms
- DOM watermark injection for seam detection

### 2. Image Processing Pipeline
**Location**: `app/tiler.py`
**Status**: Structure exists but pyvips integration incomplete

**Missing**:
- Actual pyvips operations for slicing/resizing
- Viewport image stitching with overlap
- 1288px longest-side enforcement
- PNG optimization with oxipng
- SSIM computation for overlap detection
- Tile hash generation and validation

### 3. Browser Context Management
**Location**: `app/capture.py::_get_browser_context()`
**Status**: Basic structure, missing profile persistence

**Needs**:
```python
async def _get_browser_context(
    browser: Browser,
    config: CaptureConfig,
    profile_id: str | None
) -> BrowserContext:
    """
    Implement:
    - Persistent profile loading from .cache/profiles/{profile_id}
    - Storage state persistence
    - Cookie/auth management
    - Device emulation settings
    - Viewport configuration (1280x2000)
    """
```

---

## 📦 MAJOR FEATURES TO IMPLEMENT

### 4. Local OCR Integration (M3 Milestone)
**Files**: `app/ocr_client.py`, new `app/local_ocr.py`
**Dependencies**: vllm, sglang, olmocr packages

**Implementation Tasks**:
- Create vLLM/SGLang server adapter
- Implement local model loading and management
- Add GPU detection and allocation
- Create fallback routing (remote → local)
- Add FP8 quantization support
- Implement batching for local inference

### 5. Content-Addressed Caching (Section 19.6)
**Files**: `app/store.py`, `app/cache.py` (new)
**Status**: Partially implemented, needs completion

**Missing**:
- Full cache key computation with all parameters
- Cache invalidation logic
- Deduplication on identical captures
- tar.zst bundle generation
- Git LFS integration for versioning
- Artifact purge TTL implementation

### 6. Job Queue System (Section 2)
**Files**: new `app/queue.py`, enhance `app/jobs.py`
**Current**: Basic asyncio tasks
**Target**: Arq or RQ integration

**Requirements**:
- Worker pool management
- Job priority queuing
- Retry orchestration
- Dead letter queue
- Throughput monitoring
- Auto-scaling logic

### 7. Depth-1 Crawl Mode (Section 15, bd-n5c)
**Files**: new `app/crawler.py`
**Status**: Not started

**Implementation**:
```python
class CrawlOrchestrator:
    def __init__(self, max_depth: int = 1):
        self.domain_allowlist: set[str]
        self.visited: set[str]
        self.queue: PriorityQueue

    async def crawl(self, seed_url: str):
        """
        - Extract links from seed capture
        - Filter by domain allowlist
        - Queue for capture with priority
        - Track visited URLs
        - Respect robots.txt
        """
```

---

## 🔧 INFRASTRUCTURE GAPS

### 8. Authentication & Profiles (M2)
**Files**: new `app/auth.py`, enhance `app/capture.py`
**Status**: Profile ID plumbing exists, implementation missing

**Required**:
- OAuth2 flow implementation
- Profile storage and isolation
- Browser context persistence
- Cookie management
- Auth token refresh
- Profile sandbox security

### 9. Semantic Post-Processing (Section 15, bd-we4)
**Files**: new `app/post_process.py`
**Status**: Not started

**Design**:
- LLM-based content correction
- Table/list structure repair
- Math notation fixes
- Provenance tracking
- Optional toggle in config
- Quality scoring

### 10. Production Deployment
**Files**: `docker/`, `k8s/`, deployment configs
**Status**: Development only

**Missing**:
- Dockerfile with multi-stage build
- Kubernetes manifests
- Helm charts
- Load balancer config
- SSL/TLS termination
- Monitoring sidecars
- Log aggregation

---

## 🧪 TESTING REQUIREMENTS

### 11. End-to-End Capture Tests
**Files**: `tests/test_e2e_capture.py` (new)
**Status**: No real browser tests exist

**Test Cases Needed**:
```python
async def test_real_capture_flow():
    """Actually captures a real webpage"""

async def test_viewport_sweep_determinism():
    """Ensures identical captures on repeat"""

async def test_spa_height_shrink():
    """Handles dynamic height changes"""

async def test_lazy_load_triggering():
    """Scrolls trigger image loads"""
```

### 12. Browser Automation Tests
**Files**: `tests/test_browser_automation.py` (new)

- Playwright fixture setup
- Mock page interactions
- Screenshot comparison tests
- Scroll policy validation
- Blocklist CSS injection tests
- Profile persistence tests

### 13. Image Processing Tests
**Files**: Enhance `tests/test_tiling.py`

- pyvips operation tests
- Tile boundary validation
- SSIM overlap detection
- PNG optimization verification
- Hash computation tests

---

## 📚 DOCUMENTATION TO CREATE

### 14. Core Documentation
**Required Files**:
- `docs/architecture.md` - System design and data flow
- `docs/blocklist.md` - Selector blocklist governance
- `docs/models.yaml` - OCR model policy configuration
- `docs/deployment.md` - Production deployment guide
- `docs/api.md` - Complete API reference
- `docs/troubleshooting.md` - Common issues and solutions

### 15. Gallery & Examples
**Location**: `docs/gallery/`
**Content**: Before/after capture examples

- News article capture
- Dashboard with tables
- SPA with dynamic content
- PDF comparison
- Multi-language content
- Scientific papers with math

---

## 🎯 IMPLEMENTATION PRIORITY

### Phase 1: Core Functionality (Week 1-2)
1. **Implement `_perform_viewport_sweeps()`** - Without this, nothing works
2. **Add basic screenshot capture** - Even without tiling
3. **Wire up pyvips for image processing** - Enable tile generation
4. **Complete browser context management** - Profile persistence

### Phase 2: Make It Work (Week 3-4)
5. **Complete OCR client integration** - Connect to hosted API
6. **Implement basic stitching** - Get end-to-end flow working
7. **Add DOM link extraction** - Enable Links appendix
8. **Create basic end-to-end tests** - Verify functionality

### Phase 3: Production Ready (Week 5-6)
9. **Add local OCR support** - vLLM/SGLang integration
10. **Implement caching layer** - Content-addressed storage
11. **Add job queue system** - Arq/RQ for scaling
12. **Create deployment configs** - Docker/K8s setup

### Phase 4: Advanced Features (Week 7-8)
13. **Implement crawl mode** - Depth-1 expansion
14. **Add semantic post-processing** - LLM corrections
15. **Complete auth system** - OAuth and profiles
16. **Optimize performance** - Concurrency tuning

---

## 🔍 DETAILED IMPLEMENTATION GUIDES

### Browser Capture Implementation Guide

```python
# app/capture.py - Missing implementation
async def _perform_viewport_sweeps(
    context: BrowserContext,
    config: CaptureConfig,
    viewport_overlap_px: int = 120,
    tile_overlap_px: int = 120,
    target_long_side_px: int = 1288,
    settle_ms: int = 350,
    max_steps: int = 200,
    mask_selectors: list[str] = None,
) -> tuple[list[Path], str, list[dict], list[dict]]:
    """
    Core capture implementation - THE MOST CRITICAL MISSING PIECE
    """
    page = await context.new_page()

    # 1. Navigate and wait for stability
    await page.goto(config.url, wait_until="networkidle")
    await page.wait_for_timeout(settle_ms)

    # 2. Inject scroll observer
    await page.evaluate("""
        window.scrollObserver = {
            positions: [],
            observe: function() {
                // Track scroll positions
            }
        }
    """)

    # 3. Execute viewport sweep
    screenshots = []
    viewport_height = page.viewport_size['height']
    scroll_position = 0
    previous_height = 0
    shrink_retries = 0

    for step in range(max_steps):
        # Capture current viewport
        screenshot_path = config.output_dir / f"viewport_{step:04d}.png"
        await page.screenshot(
            path=screenshot_path,
            clip={'x': 0, 'y': 0, 'width': 1280, 'height': 2000},
            animations='disabled',
            mask=mask_selectors
        )
        screenshots.append(screenshot_path)

        # Check for height changes (SPA detection)
        current_height = await page.evaluate("document.documentElement.scrollHeight")
        if current_height < previous_height:
            shrink_retries += 1
            if shrink_retries > 1:
                break
        previous_height = current_height

        # Scroll to next position with overlap
        scroll_position += (viewport_height - viewport_overlap_px)
        await page.evaluate(f"window.scrollTo(0, {scroll_position})")
        await page.wait_for_timeout(settle_ms)

        # Check if reached bottom
        at_bottom = await page.evaluate(
            "window.innerHeight + window.scrollY >= document.documentElement.scrollHeight"
        )
        if at_bottom:
            break

    # 4. Extract DOM snapshot
    dom_html = await page.content()

    # 5. Extract links and headings
    links_data = await page.evaluate("""
        () => {
            const links = Array.from(document.querySelectorAll('a')).map(a => ({
                href: a.href,
                text: a.textContent,
                rel: a.rel,
                target: a.target
            }));
            const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6')).map(h => ({
                level: parseInt(h.tagName[1]),
                text: h.textContent,
                id: h.id
            }));
            return {links, headings};
        }
    """)

    # 6. Generate seam markers
    seam_markers = []
    for i in range(len(screenshots) - 1):
        seam_markers.append({
            "prev_tile": i,
            "next_tile": i + 1,
            "overlap_px": viewport_overlap_px,
            "hash": hashlib.sha256(f"{i}_{i+1}".encode()).hexdigest()[:8]
        })

    # 7. Validation
    validation_failures = []
    if shrink_retries > 0:
        validation_failures.append({
            "type": "spa_shrink",
            "count": shrink_retries
        })

    await page.close()
    return screenshots, dom_html, validation_failures, seam_markers
```

### pyvips Integration Guide

```python
# app/tiler.py - Needs implementation
import pyvips

def create_tiles_from_viewport_images(
    viewport_images: list[Path],
    output_dir: Path,
    target_long_side: int = 1288,
    overlap_px: int = 120
) -> list[TileRecord]:
    """
    Process viewport screenshots into OCR-ready tiles
    """
    tiles = []

    for idx, image_path in enumerate(viewport_images):
        # Load with pyvips for efficiency
        image = pyvips.Image.new_from_file(str(image_path))

        # Calculate scaling to target size
        width = image.width
        height = image.height
        long_side = max(width, height)

        if long_side > target_long_side:
            scale = target_long_side / long_side
            image = image.resize(scale)

        # Generate tile record
        tile_path = output_dir / f"tile_{idx:04d}.png"
        image.pngsave(
            str(tile_path),
            compression=9,
            interlace=False
        )

        # Compute hash for deduplication
        with open(tile_path, 'rb') as f:
            tile_hash = hashlib.sha256(f.read()).hexdigest()

        tiles.append(TileRecord(
            index=idx,
            path=tile_path,
            width=image.width,
            height=image.height,
            offset_y=idx * (2000 - overlap_px),
            scale_factor=scale if long_side > target_long_side else 1.0,
            sha256=tile_hash
        ))

    return tiles
```

---

## 🚀 QUICK START IMPLEMENTATION PATH

### Day 1-2: Get Basic Capture Working
1. Implement minimal `_perform_viewport_sweeps()` with single screenshot
2. Test with simple webpage
3. Verify image saved to disk

### Day 3-4: Add Viewport Sweeping
1. Implement scroll loop
2. Capture multiple viewports
3. Handle scroll to bottom

### Day 5-6: Wire Up OCR
1. Process images into tiles
2. Submit to hosted OCR API
3. Get back Markdown

### Day 7-8: Complete Pipeline
1. Implement stitching
2. Add DOM link extraction
3. Generate final output

### Day 9-10: Testing & Validation
1. Create end-to-end test
2. Verify deterministic captures
3. Test with various page types

---

## 📊 COMPLETION METRICS

### Current Status (November 2024)
- ✅ Infrastructure: 70% complete
- ✅ API/Database: 85% complete
- ✅ CLI/Tools: 80% complete
- ✅ Testing Framework: 75% complete
- ✅ Monitoring: 90% complete
- ❌ **Core Capture: 0% complete**
- ❌ Image Processing: 15% complete
- ❌ Local OCR: 0% complete
- ❌ Advanced Features: 5% complete

### Target Completion
- Week 2: Core capture functional (MVP working)
- Week 4: Production ready (all tests passing)
- Week 6: Advanced features (crawl, post-process)
- Week 8: Full deployment (Docker/K8s/docs)

---

## 🎯 SUCCESS CRITERIA

The project will be considered complete when:

1. **Core Functionality Works**
   - Can capture any webpage and produce Markdown
   - Deterministic captures (same input → same output)
   - Handles SPAs, lazy loading, dynamic content

2. **Production Ready**
   - All tests passing (>80% coverage)
   - Deployment configs complete
   - Documentation comprehensive
   - Performance meets SLOs

3. **Advanced Features Operational**
   - Local OCR inference working
   - Crawl mode functional
   - Semantic post-processing available
   - Auth/profiles implemented

4. **Launch Ready**
   - Gallery examples created
   - Demo site deployed
   - Agent starter scripts working
   - Dataset published

---

## 📝 NOTES FOR IMPLEMENTERS

### Critical Path Dependencies
1. **Nothing works without browser capture** - This is the #1 priority
2. **pyvips is required for tiling** - Must be installed at system level
3. **OCR API keys needed** - Can't test end-to-end without them
4. **Chrome for Testing required** - Specific version pinning needed

### Common Pitfalls to Avoid
- Don't try to implement advanced features before core works
- Test with real websites early and often
- Ensure deterministic captures before optimizing
- Profile memory usage with large pages
- Handle errors gracefully (pages will fail)

### Resources Needed
- GPU for local OCR (optional but recommended)
- Sufficient disk for cache (>50GB recommended)
- Memory for browser contexts (>8GB RAM)
- Network bandwidth for OCR API calls

---

## 🔄 NEXT IMMEDIATE STEPS

1. **TODAY**: Implement `_perform_viewport_sweeps()` basic version
2. **TOMORROW**: Add scroll loop and multiple captures
3. **THIS WEEK**: Complete image processing pipeline
4. **NEXT WEEK**: Wire up OCR and test end-to-end
5. **WEEK 3**: Add caching and optimization
6. **WEEK 4**: Production deployment preparation

---

*This document represents the complete remaining work as of November 2024. The project has excellent supporting infrastructure but critically lacks its core browser automation and capture functionality. Implementing the browser capture engine is the absolute highest priority.*