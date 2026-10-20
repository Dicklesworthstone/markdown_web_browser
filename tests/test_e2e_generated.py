from __future__ import annotations

import difflib
import json
import asyncio
import hashlib
import math
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Generator

import pyvips
import pytest
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from app.settings import load_config
from app.store import RunPaths
from tests.rich_flowlogger import FlowLogger, create_console


class EvidenceFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def read(self, root: Path) -> bytes:
        path = (root / self.path).resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"Evidence is not a file: {path}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError(f"Evidence digest mismatch: {path}")
        return data


class GeneratedCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    scenario_author: str = Field(min_length=1)
    fixture: EvidenceFile
    baseline: EvidenceFile
    baseline_manifest: EvidenceFile
    baseline_job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    baseline_tiles: list[EvidenceFile] = Field(min_length=1, max_length=1000)
    request: dict[str, object] = Field(default_factory=dict)
    tolerance: FiniteFloat = Field(default=0.02, ge=0, le=0.02, strict=True)
    ssim_min: FiniteFloat = Field(default=0.99, ge=0.99, le=1, strict=True)
    min_sweeps: int = Field(default=2, ge=2, strict=True)
    timeout_seconds: FiniteFloat = Field(default=300, gt=0, le=600, strict=True)


def _load_cases(path: Path, *, enabled: bool) -> list[GeneratedCase]:
    if not enabled:
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("Enabled generated E2E requires a nonempty case list")
    cases = [GeneratedCase.model_validate(entry) for entry in data]
    if len({case.name for case in cases}) != len(cases):
        raise ValueError("Generated case names must be unique")
    for case in cases:
        if {"url", "reuse_cache"}.intersection(case.request):
            raise ValueError("Generated cases cannot supply a URL or enable cache reuse")
        case.fixture.read(path.parent).decode("utf-8")
        baseline = case.baseline.read(path.parent).decode("utf-8")
        if not baseline.strip():
            raise ValueError("Generated baseline Markdown must not be empty")
        _canonical_provenance_ids(baseline, case.baseline_job_id)
        manifest = json.loads(case.baseline_manifest.read(path.parent))
        if not isinstance(manifest, dict):
            raise ValueError("Baseline manifest must be an object")
        _check_identity(manifest, manifest)
        if manifest.get("tiles_total") != len(case.baseline_tiles):
            raise ValueError("Baseline tile count disagrees with manifest")
        for tile in case.baseline_tiles:
            _decode_tile(tile.read(path.parent))
    return cases


_IDENTITY_FIELDS = (
    "cft_label",
    "cft_version",
    "browser_transport",
    "playwright_version",
    "screenshot_style_hash",
    "capture_pipeline_version",
    "fontconfig_sha256",
    "viewport_width",
    "viewport_height",
    "device_scale_factor",
    "long_side_px",
    "selected_ocr_model",
    "served_ocr_models",
    "requested_ocr_policy",
    "backend_id",
)


def _check_identity(expected: dict, observed: dict) -> None:
    for name in _IDENTITY_FIELDS:
        if name not in expected or name not in observed:
            raise ValueError(f"Missing capture identity: {name}")
        if type(expected[name]) is not type(observed[name]) or expected[name] != observed[name]:
            raise ValueError(f"Capture identity changed: {name}")
    for name in (
        "cft_label",
        "cft_version",
        "playwright_version",
        "selected_ocr_model",
        "backend_id",
    ):
        if not isinstance(observed[name], str) or not observed[name].strip():
            raise ValueError(f"Unverified capture identity: {name}")
    if observed.get("cft_pin_verified") is not True or observed["browser_transport"] != "cdp":
        raise ValueError("Generated qualification requires verified CfT/CDP identity")
    if not observed["served_ocr_models"]:
        raise ValueError("Provider-reported model identity is missing")


def _decode_tile(data: bytes) -> pyvips.Image:
    image = pyvips.Image.new_from_buffer(data, "").copy_memory()
    if image.bands not in (1, 3) or image.format != "uchar":
        raise ValueError("SSIM inputs must be opaque 8-bit gray/RGB images")
    if min(image.width, image.height) < 1 or max(image.width, image.height) > 1288:
        raise ValueError("SSIM tile dimensions must be between1 and1288 pixels")
    return image


def _tile_ssim(baseline: bytes, candidate: bytes) -> float:
    """Local Gaussian SSIM, Wang et al.2004, K=.01/.03, L=255.

    Use pyvips convolution on each color band and average valid11x11 windows;
    shorter final tiles use the largest fitting odd window. Strips under3px
    require exact decoded pixels because spatial statistics are unavailable.
    no resampling, edge padding or changed image geometry can hide a mismatch.
    """
    reference, actual = _decode_tile(baseline), _decode_tile(candidate)
    if (reference.width, reference.height, reference.bands) != (
        actual.width,
        actual.height,
        actual.bands,
    ):
        raise ValueError("SSIM tile geometry differs")
    window = min(11, reference.width, reference.height)
    if window < 3:
        return 1.0 if (reference != actual).max() == 0 else 0.0
    radius = (window - 1) // 2
    weights = [
        [math.exp(-(x * x + y * y) / (2 * 1.5**2)) for x in range(-radius, radius + 1)]
        for y in range(-radius, radius + 1)
    ]
    kernel = pyvips.Image.new_from_list(weights, scale=sum(map(sum, weights)))
    scores = []
    for band in range(reference.bands):
        left, right = reference[band].cast("double"), actual[band].cast("double")
        mean_left, mean_right = left.conv(kernel), right.conv(kernel)
        var_left = (left * left).conv(kernel) - mean_left * mean_left
        var_right = (right * right).conv(kernel) - mean_right * mean_right
        covariance = (left * right).conv(kernel) - mean_left * mean_right
        c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
        score = ((2 * mean_left * mean_right + c1) * (2 * covariance + c2)) / (
            (mean_left * mean_left + mean_right * mean_right + c1) * (var_left + var_right + c2)
        )
        scores.append(
            score.crop(
                radius, radius, reference.width - 2 * radius, reference.height - 2 * radius
            ).avg()
        )
    result = sum(scores) / len(scores)
    if not math.isfinite(result):
        raise ValueError("SSIM result is not finite")
    return result


def _canonical_provenance_ids(markdown: str, job_id: str) -> str:
    """Validate provenance ownership; neutralize only its execution nonce.

    Keep content, geometry, hashes and body links unchanged. This prevents a
    fresh random job ID from being mistaken for an extraction-quality change.
    """
    comments = re.compile(r"<!-- source: [^\n]* -->")
    highlight = re.compile(r"(highlight=/jobs/)([0-9a-f]{32})(/artifact/highlight\?)")

    def canonical(match: re.Match[str]) -> str:
        comment = match.group()
        references = list(highlight.finditer(comment))
        if len(references) != 1 or references[0].group(2) != job_id:
            raise ValueError("Provenance highlight does not belong to the recorded job")
        return highlight.sub(r"\1<execution>\3", comment)

    result = []
    fence: tuple[str, int] | None = None
    for line in markdown.splitlines(keepends=True):
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if opening:
            marker, rest = opening.groups()
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1] and not rest.strip():
                fence = None
            result.append(line)
        elif fence is None and line.startswith("<!-- source: "):
            result.append(comments.sub(canonical, line))
        else:
            result.append(line)
    return "".join(result)


def _markdown_diff(
    baseline: str, candidate: str, *, baseline_job_id: str, job_id: str
) -> tuple[float, float]:
    if not baseline.strip() or not candidate.strip():
        raise ValueError("Expected and candidate Markdown must be nonempty")
    raw = 1 - difflib.SequenceMatcher(None, baseline, candidate).ratio()
    expected = _canonical_provenance_ids(baseline, baseline_job_id)
    actual = _canonical_provenance_ids(candidate, job_id)
    return raw, 1 - difflib.SequenceMatcher(None, expected, actual).ratio()


@contextmanager
def _serve_fixture(data: bytes) -> Generator[str, None, None]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 0.1
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fixture"
    finally:
        stop.set()
        thread.join(2)
        server.server_close()
        if thread.is_alive():
            raise RuntimeError("Fixture listener failed to stop")


_CONFIG = load_config()
ENABLED = _CONFIG("MDWB_RUN_E2E_GENERATED", default=False, cast=bool)
CASES_PATH = Path(
    _CONFIG("MDWB_GENERATED_E2E_CASES", default="tests/fixtures/e2e_generated/cases.json")
)
CASES = _load_cases(CASES_PATH, enabled=ENABLED) if __name__ != "__main__" else []


@dataclass(frozen=True)
class CaptureCandidate:
    job_id: str
    markdown: str
    manifest: dict[str, Any]
    tiles: list[Path]
    events: list[dict[str, Any]]
    paths: RunPaths


async def _fresh_candidate(
    fixture: bytes,
    *,
    request_options: dict[str, object],
    root: Path,
    timeout_seconds: float,
    monkeypatch: pytest.MonkeyPatch,
    receipt_meta: dict[str, object],
) -> CaptureCandidate:
    from app import capture, jobs, warning_log
    from app.jobs import JobManager
    from app.schemas import JobCreateRequest
    from app.settings import get_settings
    from app.store import StorageConfig, Store

    fixture.decode("utf-8")
    if {"url", "reuse_cache"}.intersection(request_options):
        raise ValueError("Generated captures cannot override the fixture URL or cache policy")
    active = get_settings()
    active = replace(
        active,
        REQUIRE_API_KEY=False,
        NETWORK_TRUST_LOCAL_OPERATOR=True,
        NETWORK_PRIVATE_CIDRS=("127.0.0.1/32",),
        NETWORK_TRUSTED_OWNERS=(),
        logging=replace(active.logging, warning_log_path=root / "warnings.jsonl"),
        storage=replace(
            active.storage,
            cache_root=root / "candidate",
            db_path=root / "runs.db",
            profiles_root=root / "profiles",
        ),
    )
    monkeypatch.setattr(capture, "get_settings", lambda: active)
    monkeypatch.setattr(jobs, "global_settings", active)
    monkeypatch.setattr(warning_log, "get_settings", lambda: active)
    store = Store(StorageConfig(root / "candidate", root / "runs.db"))
    manager = JobManager(store=store)
    job_id: str | None = None
    result: CaptureCandidate | None = None
    try:
        with _serve_fixture(fixture) as url:
            request = JobCreateRequest.model_validate(
                {**request_options, "url": url, "reuse_cache": False}
            )
            snapshot = await manager.create_job(request)
            job_id = snapshot["id"]
            async with asyncio.timeout(timeout_seconds):
                await manager._tasks[job_id]
        record = store.fetch_run(job_id)
        if record is None or record.status != "DONE":
            raise RuntimeError("Fresh generated capture did not persist DONE")
        paths = RunPaths.from_record(record)
        result = CaptureCandidate(
            job_id,
            store.read_markdown(job_id),
            store.read_manifest(job_id),
            sorted(paths.tiles_dir.glob("tile_*.png")),
            manager.get_events(job_id),
            paths,
        )
        return result
    finally:
        try:
            with (root / "candidate-evidence.json").open("x", encoding="utf-8") as handle:
                json.dump(
                    {
                        **receipt_meta,
                        "job_id": job_id,
                        "fixture_sha256": hashlib.sha256(fixture).hexdigest(),
                        "candidate_markdown_sha256": hashlib.sha256(
                            result.markdown.encode()
                        ).hexdigest()
                        if result
                        else None,
                        "manifest": result.manifest if result else None,
                        "events": manager.get_events(job_id) if job_id else [],
                        "snapshot": manager.get_snapshot(job_id) if job_id else None,
                        "artifacts_root": str(result.paths.root) if result else None,
                        "scope": "fresh local fixture/real configured OCR; candidate generation is not baseline approval or production qualification",
                    },
                    handle,
                    indent=2,
                )
        finally:
            await manager.stop_watchdog()
            store.engine.dispose()


def _case_id(case: GeneratedCase | None) -> str:
    return case.name if case else "disabled"


@pytest.mark.skipif(not ENABLED, reason="Generated real-provider E2E is explicitly opt-in")
@pytest.mark.parametrize("case", CASES or [None], ids=_case_id)
async def test_generated_markdown_diff(
    case: GeneratedCase | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if case is None:
        pytest.fail("Enabled generated E2E has no configured cases")
    baseline = case.baseline.read(CASES_PATH.parent).decode("utf-8")
    expected_manifest = json.loads(case.baseline_manifest.read(CASES_PATH.parent))
    fixture = case.fixture.read(CASES_PATH.parent)
    baseline_tiles = [tile.read(CASES_PATH.parent) for tile in case.baseline_tiles]
    captured = await _fresh_candidate(
        fixture,
        request_options=case.request,
        root=tmp_path,
        timeout_seconds=case.timeout_seconds,
        monkeypatch=monkeypatch,
        receipt_meta={
            "case": case.name,
            "scenario_author": case.scenario_author,
            "baseline_job_id": case.baseline_job_id,
            "cases_sha256": hashlib.sha256(CASES_PATH.read_bytes()).hexdigest(),
        },
    )
    job_id, candidate, manifest, tiles, events, paths = (
        captured.job_id,
        captured.markdown,
        captured.manifest,
        captured.tiles,
        captured.events,
        captured.paths,
    )
    _check_identity(expected_manifest, manifest)
    if manifest.get("cache_hit") is not False:
        pytest.fail("Generated candidate reused cache")
    stages = {e["snapshot"]["state"] for e in events if e["event"] == "snapshot"}
    required = {
        "NAVIGATING",
        "SCROLLING",
        "TILING",
        "OCR_WAITING",
        "STITCHING",
        "PERSISTING",
        "DONE",
    }
    if required - stages:
        pytest.fail(
            f"Generated candidate lacks actual pipeline stages: {sorted(required - stages)}"
        )
    if manifest["sweep_stats"]["sweep_count"] < case.min_sweeps:
        pytest.fail("Generated scenario did not exercise viewport scrolling")
    for timing in ("capture_ms", "ocr_ms", "stitch_ms"):
        if type(manifest.get(timing)) is not int or manifest[timing] < 0:
            pytest.fail(f"Missing real pipeline timing: {timing}")
    if not manifest.get("ocr_batches") or any(
        not b.get("request_id") for b in manifest["ocr_batches"]
    ):
        pytest.fail("Generated candidate lacks OCR request IDs")
    if len(tiles) != len(baseline_tiles) or len(tiles) != manifest["tiles_total"]:
        pytest.fail("Generated tile cardinality changed")
    for index, (expected, path) in enumerate(zip(baseline_tiles, tiles, strict=True)):
        score = _tile_ssim(expected, path.read_bytes())
        if score < case.ssim_min:
            pytest.fail(f"Tile{index} SSIM{score:.6f} below{case.ssim_min}")
    if not candidate.strip():
        pytest.fail("Generated candidate Markdown is empty")
    tolerance = case.tolerance

    console = create_console()
    case_name = case.name
    logger = FlowLogger(console, f"Generated diff — {case_name}")
    logger.banner("Generative E2E guardrail")
    logger.step(
        "Inputs",
        description="Compare immutable expected output with a fresh capture/OCR candidate.",
        inputs={
            "case": (case_name, "cases.json"),
            "baseline": str(case.baseline.path),
            "candidate": str(paths.markdown_path),
            "tolerance": tolerance,
        },
    )
    raw_diff, diff = _markdown_diff(
        baseline, candidate, baseline_job_id=case.baseline_job_id, job_id=job_id
    )
    ratio = 1 - diff
    logger.step(
        "Diff metrics",
        description="Compute similarity and diff ratio.",
        outputs={
            "similarity": f"{ratio:.4f}",
            "raw_diff_ratio": f"{raw_diff:.4f}",
            "diff_ratio": f"{diff:.4f}",
            "status": "PASS" if diff <= tolerance else "FAIL",
        },
    )

    if diff > tolerance:
        snippet = _diff_snippet(baseline, candidate)
        logger.step(
            "Diff excerpt",
            description="Unified diff excerpt (truncated).",
            syntax_blocks=[("diff", snippet)],
        )

    logger.finish(
        {
            "case": case_name,
            "diff_ratio": f"{diff:.4f}",
            "tolerance": f"{tolerance:.4f}",
        }
    )

    if diff > tolerance:
        pytest.fail(f"Diff {diff:.2%} exceeds tolerance {tolerance:.2%} for {case_name}")


def _diff_snippet(baseline: str, candidate: str, *, limit: int = 80) -> str:
    diff_iter = difflib.unified_diff(
        baseline.splitlines(),
        candidate.splitlines(),
        fromfile="baseline",
        tofile="candidate",
        lineterm="",
    )
    lines: list[str] = []
    for idx, line in enumerate(diff_iter):
        if idx >= limit:
            lines.append("... (diff truncated)")
            break
        lines.append(line)
    return "\n".join(lines) if lines else "(no diff output)"


@pytest.fixture
def generated_case_files(tmp_path: Path) -> tuple[Path, dict]:
    """Contract fixture only; these bytes are not hosted OCR/corpus proof."""
    manifest = {
        "cft_label": "unit-fixture",
        "cft_version": "130.0.6723.69",
        "browser_transport": "cdp",
        "playwright_version": "1.57.0",
        "screenshot_style_hash": "0" * 64,
        "capture_pipeline_version": "unit-fixture",
        "fontconfig_sha256": "1" * 64,
        "viewport_width": 64,
        "viewport_height": 64,
        "device_scale_factor": 1,
        "long_side_px": 64,
        "selected_ocr_model": "unit-model",
        "served_ocr_models": ["unit-model"],
        "requested_ocr_policy": None,
        "backend_id": "unit-service",
        "cft_pin_verified": True,
        "tiles_total": 1,
    }
    files = {
        "fixture.html": b"<body style='height:300px'>Unit fixture only</body>",
        "baseline.md": b"# Known expected content\n",
        "manifest.json": json.dumps(manifest).encode(),
        "tile.png": pyvips.Image.black(32, 32, bands=3).pngsave_buffer(),
    }
    evidence = {}
    for name, data in files.items():
        path = tmp_path / name
        path.write_bytes(data)
        evidence[name] = {"path": name, "sha256": hashlib.sha256(data).hexdigest()}
    case = {
        "name": "known-fixture",
        "scenario_author": "unit-fixture, not a generated corpus",
        "fixture": evidence["fixture.html"],
        "baseline": evidence["baseline.md"],
        "baseline_manifest": evidence["manifest.json"],
        "baseline_job_id": "0" * 32,
        "baseline_tiles": [evidence["tile.png"]],
    }
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([case]))
    return path, case


def test_generated_case_loader_requires_real_inputs_when_enabled(tmp_path, generated_case_files):
    path, _ = generated_case_files
    assert _load_cases(path, enabled=True)[0].name == "known-fixture"
    assert _load_cases(tmp_path / "missing.json", enabled=False) == []
    with pytest.raises(FileNotFoundError):
        _load_cases(tmp_path / "missing.json", enabled=True)
    path.write_text("[]")
    with pytest.raises(ValueError, match="nonempty"):
        _load_cases(path, enabled=True)


@pytest.mark.parametrize("tolerance", [float("nan"), float("inf"), -0.1, 0.020001, True, "0.02"])
def test_generated_guard_rejects_invalid_or_widened_budget(generated_case_files, tolerance):
    path, case = generated_case_files
    path.write_text(json.dumps([{**case, "tolerance": tolerance}]))
    with pytest.raises(ValueError):
        _load_cases(path, enabled=True)


@pytest.mark.parametrize(
    "mutation",
    ["missing", "digest", "empty", "identity", "geometry", "cache", "candidate", "owner"],
)
def test_generated_guard_refuses_unverifiable_baselines(generated_case_files, mutation):
    path, case = generated_case_files
    if mutation == "missing":
        case["baseline"]["path"] = "absent.md"
    elif mutation == "digest":
        (path.parent / "baseline.md").write_text("Changed expected output")
    elif mutation == "empty":
        (path.parent / "baseline.md").write_bytes(b"")
        case["baseline"]["sha256"] = hashlib.sha256(b"").hexdigest()
    elif mutation == "identity":
        content = json.loads((path.parent / "manifest.json").read_bytes())
        content.pop("served_ocr_models")
        payload = json.dumps(content).encode()
        (path.parent / "manifest.json").write_bytes(payload)
        case["baseline_manifest"]["sha256"] = hashlib.sha256(payload).hexdigest()
    elif mutation == "geometry":
        payload = pyvips.Image.black(1289, 16).pngsave_buffer()
        (path.parent / "tile.png").write_bytes(payload)
        case["baseline_tiles"][0]["sha256"] = hashlib.sha256(payload).hexdigest()
    elif mutation == "cache":
        case["request"] = {"reuse_cache": True}
    elif mutation == "owner":
        payload = f"<!-- source: tile_0000, highlight=/jobs/{'f' * 32}/artifact/highlight?tile=x -->".encode()
        (path.parent / "baseline.md").write_bytes(payload)
        case["baseline"]["sha256"] = hashlib.sha256(payload).hexdigest()
    else:
        case["candidate"] = str(path.parent / "baseline.md")
    path.write_text(json.dumps([case]))
    with pytest.raises((ValueError, FileNotFoundError)):
        _load_cases(path, enabled=True)


@pytest.mark.parametrize("size", [(32, 32), (32, 7), (32, 2)])
def test_ssim_has_positive_and_analytic_damage_controls(size):
    width, height = size
    black = pyvips.Image.black(width, height, bands=3)
    white = (black + 255).cast("uchar")
    baseline, candidate = black.pngsave_buffer(), white.pngsave_buffer()
    assert _tile_ssim(baseline, baseline) == pytest.approx(1.0, abs=1e-12)
    if height >= 3:
        # Constant images have zero variance/covariance: this analytic value
        # is independent of the convolution implementation.
        c1 = (0.01 * 255) ** 2
        assert _tile_ssim(baseline, candidate) == pytest.approx(c1 / (255**2 + c1), abs=1e-8)
    else:
        assert _tile_ssim(baseline, candidate) == 0
    with pytest.raises(ValueError, match="geometry"):
        _tile_ssim(baseline, pyvips.Image.black(width + 1, height, bands=3).pngsave_buffer())


def test_fixture_listener_serves_actual_bytes_and_stops():
    import httpx

    data = b"<body>Known generated input7319</body>"
    with _serve_fixture(data) as url:
        with httpx.Client(
            trust_env=False,
            timeout=2,
            headers={"User-Agent": "mdwb (+https://github.com/Dicklesworthstone/markdown_web_browser)"},
        ) as client:
            response = client.get(url)
            assert response.status_code == 200 and response.content == data
    with pytest.raises(httpx.ConnectError):
        with httpx.Client(trust_env=False, timeout=2) as client:
            client.get(url)


def test_markdown_guard_neutralizes_only_verified_execution_ids():
    old, new = "0" * 32, "f" * 32
    baseline = f"<!-- source: tile_0000, y=0, sha256={'a' * 64}, highlight=/jobs/{old}/artifact/highlight?tile=x -->\n\n# Invoice7319\n"
    candidate = baseline.replace(old, new)
    raw, qualified = _markdown_diff(baseline, candidate, baseline_job_id=old, job_id=new)
    assert raw > 0.02 and qualified == 0
    # Publish the loss side too: content/hash/geometry edits still count and
    # another job's provenance must refuse, rather than being neutralized.
    for altered in (
        candidate.replace("Invoice7319", "Incorrect extracted content" * 10),
        candidate.replace("a" * 64, "b" * 64),
        candidate.replace("y=0", "y=99999999999999999999999999"),
    ):
        assert _markdown_diff(baseline, altered, baseline_job_id=old, job_id=new)[1] > 0.02
    with pytest.raises(ValueError, match="recorded job"):
        _markdown_diff(baseline, candidate, baseline_job_id=old, job_id="e" * 32)
    with pytest.raises(ValueError, match="recorded job"):
        _markdown_diff(
            baseline, candidate.replace("highlight=", "missing="), baseline_job_id=old, job_id=new
        )


def test_markdown_guard_preserves_body_links_and_refuses_empty_content():
    old, new = "0" * 32, "f" * 32
    baseline = f"[Body link](/jobs/{old}/artifact/highlight)"
    candidate = baseline.replace(old, new)
    assert _markdown_diff(baseline, candidate, baseline_job_id=old, job_id=new)[1] > 0.02
    with pytest.raises(ValueError, match="nonempty"):
        _markdown_diff("", "", baseline_job_id=old, job_id=new)
    fenced = f"```html\n<!-- source: tile_0000, highlight=/jobs/{old}/artifact/highlight?tile=x -->\n```\n"
    assert (
        _markdown_diff(fenced, fenced.replace(old, new), baseline_job_id=old, job_id=new)[1] > 0.02
    )


def test_candidate_generator_refuses_existing_output_and_invalid_timeout(
    tmp_path, monkeypatch, capsys
):
    import sys

    fixture = tmp_path / "fixture.html"
    fixture.write_text("<body>Candidate generation refusal control</body>")
    out = tmp_path / "retained"
    out.mkdir()
    sentinel = out / "sentinel.txt"
    sentinel.write_text("retain these bytes")
    command = [
        "tests.test_e2e_generated",
        "--fixture",
        str(fixture),
        "--out",
        str(out),
    ]
    for extra, message in (
        ([], "existing artifacts"),
        (["--timeout", "nan"], "timeout must be finite"),
    ):
        monkeypatch.setattr(sys, "argv", command + extra)
        with pytest.raises(SystemExit) as stopped:
            _generate_candidate_cli()
        assert stopped.value.code == 2 and message in capsys.readouterr().err
        assert sentinel.read_text() == "retain these bytes"
        assert list(out.iterdir()) == [sentinel]


def _generate_candidate_cli() -> None:
    """Create new candidate bytes for review; never promote them to expected."""
    import argparse
    from app.schemas import JobCreateRequest

    parser = argparse.ArgumentParser(
        description="Capture an authored HTML fixture through real OCR and retain a new candidate bundle; baseline approval is separate."
    )
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument(
        "--out", type=Path, required=True, help="New output directory; existing paths are refused"
    )
    parser.add_argument("--viewport-height", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument(
        "--scenario-author", default="operator candidate generation; baseline approval pending"
    )
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 600:
        parser.error("timeout must be finite and between0 and600 seconds")
    JobCreateRequest(
        url="http://127.0.0.1/", viewport_height=args.viewport_height, reuse_cache=False
    )
    fixture = args.fixture.read_bytes()
    fixture.decode("utf-8")
    if args.out.exists() or args.out.is_symlink():
        parser.error("output must be a new path; existing artifacts are never replaced")
    args.out.mkdir(parents=True)
    with pytest.MonkeyPatch.context() as monkeypatch:
        captured = asyncio.run(
            _fresh_candidate(
                fixture,
                request_options={"viewport_height": args.viewport_height},
                root=args.out,
                timeout_seconds=args.timeout,
                monkeypatch=monkeypatch,
                receipt_meta={"scenario_author": args.scenario_author, "baseline_approved": False},
            )
        )
    print(
        json.dumps(
            {
                "job_id": captured.job_id,
                "candidate_root": str(captured.paths.root),
                "sweep_count": captured.manifest["sweep_stats"]["sweep_count"],
                "served_models": captured.manifest["served_ocr_models"],
                "baseline_approved": False,
            }
        )
    )


if __name__ == "__main__":
    _generate_candidate_cli()
