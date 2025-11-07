#!/usr/bin/env python3
"""High-level CLI helpers for running the olmOCR pipeline."""
from __future__ import annotations

import csv
import logging
import math
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple, cast

import zstandard

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel

DEFAULT_MODEL = os.environ.get("OLMOCR_MODEL", "allenai/olmOCR-2-7B-1025-FP8")
DEFAULT_VLLM_PORT = int(os.environ.get("OLMOCR_VLLM_PORT", "30024"))
DEFAULT_SERVER = f"http://localhost:{DEFAULT_VLLM_PORT}/v1"
CONFIG_PATH = Path(os.environ.get("OLMOCR_CLI_CONFIG", str(Path.home() / ".olmocr-cli.toml")))
DEFAULT_RUN_WORKERS = 20
DEFAULT_IMAGE_WORKERS = 12

app = typer.Typer(add_completion=False)
console = Console()

CUDA_HOME = Path(os.getenv("OLMOCR_CUDA_HOME", "/usr/local/cuda-12.6"))
REPO_ROOT = Path(__file__).resolve().parent.parent
if os.environ.get("VIRTUAL_ENV"):
    VENV_DIR = Path(os.environ["VIRTUAL_ENV"]).resolve()
else:
    VENV_DIR = (REPO_ROOT / ".venv").resolve()
VENV_BIN = VENV_DIR / "bin"


def _join_path(*parts: str) -> str:
    return os.pathsep.join([p for p in parts if p])


DEFAULT_ENV_OVERRIDES = {
    "CUDA_HOME": str(CUDA_HOME),
    "PATH": _join_path(
        str(VENV_BIN) if VENV_BIN.exists() else "",
        str(CUDA_HOME / "bin"),
        os.environ.get("PATH", ""),
    ),
    "LD_LIBRARY_PATH": _join_path(str(CUDA_HOME / "lib64"), os.environ.get("LD_LIBRARY_PATH", "")),
    "CC": "/usr/bin/gcc-12",
    "CXX": "/usr/bin/g++-12",
    "CUDAHOSTCXX": "/usr/bin/g++-12",
    "TORCH_CUDA_ARCH_LIST": os.environ.get("TORCH_CUDA_ARCH_LIST", "8.9"),
    "VIRTUAL_ENV": str(VENV_DIR),
    "PYTHONWARNINGS": "ignore::FutureWarning,ignore::DeprecationWarning",
    "TORCH_NVML_BASED_WARNING_DISABLE": "1",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=False)],
)
logger = logging.getLogger("olmocr_cli")


def _build_env() -> dict:
    env = os.environ.copy()
    for key, value in DEFAULT_ENV_OVERRIDES.items():
        env[key] = value
    return env


_CONFIG_CACHE: Optional[dict] = None


def _load_config() -> dict:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    if not CONFIG_PATH.exists():
        _CONFIG_CACHE = {}
        return _CONFIG_CACHE
    try:
        _CONFIG_CACHE = tomllib.loads(CONFIG_PATH.read_text())
    except Exception as exc:
        console.print(f"[yellow]Warning:[/yellow] Failed to parse {CONFIG_PATH}: {exc}")
        _CONFIG_CACHE = {}
    return _CONFIG_CACHE


def _config_for(command: str) -> dict:
    cfg = _load_config()
    merged: dict = {}
    merged.update(cfg.get("global", {}))
    merged.update(cfg.get(command, {}))
    return merged


def _load_completed_paths(workspace: Path) -> set[str]:
    index_path = workspace / "work_index_list.csv.zstd"
    done_dir = workspace / "done_flags"
    if not index_path.exists() or not done_dir.exists():
        return set()

    done_hashes = {
        name[len("done_") : -len(".flag")]
        for name in os.listdir(done_dir)
        if name.startswith("done_") and name.endswith(".flag")
    }
    if not done_hashes:
        return set()

    try:
        with open(index_path, "rb") as f:
            data = zstandard.ZstdDecompressor().decompress(f.read())
    except Exception as exc:
        logger.warning("Failed to read %s: %s", index_path, exc)
        return set()

    completed: set[str] = set()
    reader = csv.reader(data.decode("utf-8", errors="replace").splitlines())
    for row in reader:
        if not row:
            continue
        group_hash, *paths = row
        if group_hash in done_hashes:
            completed.update(paths)
    return completed


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except FileNotFoundError:
        return False


def _auto_tensor_parallel_size() -> int:
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if visible:
        count = len([gpu for gpu in visible.split(",") if gpu.strip()])
    else:
        try:
            result = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, check=True)
            count = len([line for line in result.stdout.splitlines() if line.strip()])
        except Exception:
            count = 1
    return 2 if count >= 2 else 1


def _normalize_server_url(url: str) -> str:
    cleaned = url.rstrip("/")
    if not cleaned.endswith("/v1"):
        cleaned = f"{cleaned}/v1"
    return cleaned


def _probe_server(url: str, timeout: float = 0.5) -> bool:
    models_url = f"{url}/models" if not url.endswith("/models") else url
    req = urllib.request.Request(models_url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - local
            return resp.status == 200
    except Exception:
        return False


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    parts: List[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def _build_serve_command(
    *,
    model: str,
    port: int,
    tensor_parallel_size: int,
    workers: int,
    gpu_memory_utilization: Optional[float],
    max_model_len: int,
    extra_args: Optional[str],
) -> List[str]:
    cmd: List[str] = [
        "vllm",
        "serve",
        model,
        "--port",
        str(port),
        "--disable-log-requests",
        "--uvicorn-log-level",
        "warning",
        "--served-model-name",
        "olmocr",
        "--tensor-parallel-size",
        str(tensor_parallel_size),
        "--scheduling-policy",
        "priority",
        "--limit-mm-per-prompt",
        '{"video": 0}',
        "--max-model-len",
        str(max_model_len),
    ]
    if workers > 1:
        cmd.extend(["--data-parallel-size", str(workers)])
    if gpu_memory_utilization is not None:
        cmd.extend(["--gpu-memory-utilization", str(gpu_memory_utilization)])
    if extra_args:
        cmd.extend(shlex.split(extra_args))
    return cmd


def _base_from_server_url(server_url: str) -> str:
    trimmed = server_url.rstrip("/")
    if trimmed.endswith("/v1"):
        return trimmed[: -len("/v1")]
    return trimmed


METRIC_REQUEST_RE = re.compile(r"vllm_engine_request_stats\{status=\"(?P<status>[^\"]+)\"\}\s+(?P<value>[0-9.]+)")


def _fetch_server_metrics(server_url: str) -> Optional[Tuple[float, float]]:
    base = _base_from_server_url(server_url)
    metrics_url = f"{base}/metrics"
    req = urllib.request.Request(metrics_url)
    try:
        with urllib.request.urlopen(req, timeout=1.0) as resp:  # nosec - local/internal
            body = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    running = waiting = 0.0
    for match in METRIC_REQUEST_RE.finditer(body):
        status = match.group("status")
        value = float(match.group("value"))
        if status == "running":
            running = value
        elif status == "waiting":
            waiting = value
    return running, waiting


def _convert_single_image(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
            return
    except FileNotFoundError:
        pass
    cmd = ["img2pdf", str(src), "-o", str(dest)]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _convert_images_parallel(
    files: List[Path],
    input_root: Path,
    converted_root: Path,
    max_workers: int,
) -> Tuple[List[Path], dict]:
    if not files:
        return [], {}
    max_workers = max(1, min(max_workers, len(files)))
    pdf_paths: List[Path] = []
    mapping: dict = {}
    tasks = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for img in files:
            rel = img.relative_to(input_root)
            pdf_path = converted_root / rel.with_suffix(".pdf")
            pdf_paths.append(pdf_path)
            mapping[pdf_path] = img
            tasks.append(executor.submit(_convert_single_image, img, pdf_path))
        for future in as_completed(tasks):
            future.result()
    return pdf_paths, mapping


def _build_serve_command(
    *,
    model: str,
    port: int,
    tensor_parallel_size: int,
    workers: int,
    gpu_memory_utilization: Optional[float],
    max_model_len: int,
    extra_args: Optional[str],
) -> List[str]:
    cmd: List[str] = [
        "vllm",
        "serve",
        model,
        "--port",
        str(port),
        "--disable-log-requests",
        "--uvicorn-log-level",
        "warning",
        "--served-model-name",
        "olmocr",
        "--tensor-parallel-size",
        str(tensor_parallel_size),
        "--scheduling-policy",
        "priority",
        "--limit-mm-per-prompt",
        '{"video": 0}',
        "--max-model-len",
        str(max_model_len),
    ]
    if workers > 1:
        cmd.extend(["--data-parallel-size", str(workers)])
    if gpu_memory_utilization is not None:
        cmd.extend(["--gpu-memory-utilization", str(gpu_memory_utilization)])
    if extra_args:
        cmd.extend(shlex.split(extra_args))
    return cmd


NOISY_SUBSTRINGS = [
    "FutureWarning: The pynvml package is deprecated",
    "argument '--disable-log-requests' is deprecated",
    "Import error msg: No module named 'intel_extension_for_pytorch'",
    "Using 'pin_memory=False' as WSL is detected",
    "Automatically detected platform cuda.",
    "Chunked prefill is enabled with max_num_batched_tokens",
    "The image processor of type `Qwen2VLImageProcessor` is now loaded as a fast processor",
    "Detected the chat template content format to be 'openai'",
    "TORCH_NCCL_AVOID_RECORD_STREAMS is the default now",
    "ProcessGroupNCCL.cpp",
]


@app.command("show-env")
def show_env() -> None:
    env = _build_env()
    rows = [f"[bold]{k}[/bold]=[cyan]{v}[/cyan]" for k, v in env.items() if k in DEFAULT_ENV_OVERRIDES]
    console.print(Panel("\n".join(rows), title="olmOCR CUDA environment"))


@app.command(help="Run PDFs/images/manifests with tuned defaults.")
def run(
    workspace: Path = typer.Option(Path("./localworkspace"), help="Workspace directory for queue/results."),
    pdf: List[Path] = typer.Option(..., help="PDFs, images, directories, or manifest .txt files."),
    markdown: bool = typer.Option(True, help="Emit markdown alongside structured output."),
    workers: Optional[int] = typer.Option(None, help="Concurrent olmOCR workers (default 20, overridable via config).", show_default=False),
    tensor_parallel_size: Optional[int] = typer.Option(None, help="Override tensor parallel degree (configurable).", show_default=False),
    extra_args: Optional[str] = typer.Option(None, help="Extra args forwarded to `olmocr.pipeline`.", show_default=False),
    server_url: Optional[str] = typer.Option(None, help="Reuse an existing OpenAI-compatible endpoint (configurable).", show_default=False),
    server_model: Optional[str] = typer.Option(None, help="Model name to request on remote server (default 'olmocr').", show_default=False),
    resume: Optional[bool] = typer.Option(None, help="Skip already-completed work items if done_flags exist (default true).", show_default=False),
    visible_gpus: Optional[str] = typer.Option(None, help="Comma-separated GPU IDs (sets CUDA_VISIBLE_DEVICES).", show_default=False),
) -> None:
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    manifests_dir = workspace / ".manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    config_defaults = _config_for("run")
    workers = workers if workers is not None else config_defaults.get("workers", DEFAULT_RUN_WORKERS)
    tensor_parallel_size = (
        tensor_parallel_size if tensor_parallel_size is not None else config_defaults.get("tensor_parallel_size")
    )
    extra_args = extra_args if extra_args is not None else config_defaults.get("extra_args")
    server_url = server_url if server_url is not None else config_defaults.get("server_url")
    server_model or config_defaults.get("server_model", "olmocr")
    resume = resume if resume is not None else config_defaults.get("resume", True)
    visible_gpus = visible_gpus if visible_gpus is not None else config_defaults.get("visible_gpus")

    completed_paths: set[str] = _load_completed_paths(workspace) if resume else set()

    env = _build_env()
    if visible_gpus:
        env["CUDA_VISIBLE_DEVICES"] = visible_gpus

    resume_manifest_dir = manifests_dir / "resume"
    resolved_inputs: List[Path] = []
    skipped_inputs = 0
    for item in pdf:
        item = item.resolve()
        if item.is_dir():
            files = sorted(
                [
                    f
                    for f in item.rglob("*")
                    if f.is_file() and f.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg"}
                ]
            )
            if not files:
                raise typer.BadParameter(f"No PDF/PNG/JPG files found under {item}")
            if completed_paths:
                filtered = [f for f in files if str(f) not in completed_paths]
                skipped = len(files) - len(filtered)
                if skipped:
                    console.print(f"Resume: skipping {skipped} completed files from {item}")
                files = filtered
            if not files:
                console.print(f"All files under {item} already completed; skipping directory.")
                continue
            manifest = manifests_dir / f"{item.name}.txt"
            manifest.write_text("\n".join(str(f) for f in files))
            resolved_inputs.append(manifest)
            logger.info("Expanded directory %s into manifest %s (%d files)", item, manifest, len(files))
        elif item.is_file() and item.suffix.lower() == ".txt":
            orig_lines = [line.strip() for line in item.read_text().splitlines() if line.strip()]
            lines = list(orig_lines)
            if completed_paths:
                filtered_lines = [line for line in lines if line not in completed_paths]
                skipped = len(lines) - len(filtered_lines)
                if skipped:
                    console.print(f"Resume: skipping {skipped} entries already completed in manifest {item}")
                lines = filtered_lines
            if not lines:
                console.print(f"All entries in manifest {item} already completed; skipping.")
                continue
            if completed_paths and lines != orig_lines:
                resume_manifest_dir.mkdir(parents=True, exist_ok=True)
                resume_manifest = resume_manifest_dir / f"{item.stem}-resume.txt"
                resume_manifest.write_text("\n".join(lines))
                resolved_inputs.append(resume_manifest)
            else:
                resolved_inputs.append(item)
        else:
            if completed_paths and str(item) in completed_paths:
                skipped_inputs += 1
                continue
            resolved_inputs.append(item)

    if completed_paths and skipped_inputs:
        console.print(f"Resume: skipped {skipped_inputs} standalone inputs already marked done.")

    if not resolved_inputs:
        console.print("All inputs already completed according to the workspace done flags. Nothing to run.")
        return

    env = _build_env()
    python_exe = VENV_BIN / "python"
    if not python_exe.exists():
        python_exe = Path(sys.executable)

    base_cmd: List[str] = [str(python_exe), "-m", "olmocr.pipeline", str(workspace)]
    if markdown:
        base_cmd.append("--markdown")
    effective_workers = workers

    total_items = len(resolved_inputs)
    user_server = server_url or os.environ.get("OLMOCR_SERVER_URL")
    normalized_user_server = _normalize_server_url(user_server) if user_server else None
    detected_server: Optional[str] = None

    if normalized_user_server:
        if _probe_server(normalized_user_server):
            detected_server = normalized_user_server
            logger.info("Found reachable server %s (user-provided).", detected_server)
        else:
            console.print(f"[yellow]Warning:[/yellow] Provided server {normalized_user_server} is unreachable; falling back to internal launch.")
            normalized_user_server = None

    if not normalized_user_server:
        if _probe_server(DEFAULT_SERVER):
            detected_server = DEFAULT_SERVER
            logger.info("Detected running vLLM at %s; reusing it.", detected_server)

    use_external_server = detected_server is not None
    if use_external_server:
        server_for_cli = cast(str, detected_server)
        base_cmd.extend(["--server", server_for_cli])
        base_cmd.extend(["--model", server_model_name])
        metrics = _fetch_server_metrics(server_for_cli)
        if metrics:
            running, waiting = metrics
            console.print(
                f"Remote server load → running: {running:.0f}, waiting: {waiting:.0f} requests"
            )
            if waiting > 0:
                suggested = max(1, workers - math.ceil(waiting))
                if suggested < effective_workers:
                    console.print(
                        f"[yellow]Throttling local workers from {effective_workers} to {suggested} to avoid queueing on remote server.[/yellow]"
                    )
                    effective_workers = suggested

    pdf_and_extra: List[str] = ["--pdfs", *[str(path) for path in resolved_inputs]]
    if extra_args:
        pdf_and_extra.extend(shlex.split(extra_args))

    base_cmd.extend(["--workers", str(effective_workers)])

    tensor_flag_in_extra = extra_args and "--tensor-parallel-size" in extra_args
    explicit_tp = tensor_parallel_size
    auto_tp = explicit_tp or _auto_tensor_parallel_size()
    if not use_external_server and explicit_tp is None and auto_tp > 1 and _is_wsl():
        logger.warning("WSL detected—defaulting tensor-parallel-size to 1 to avoid NCCL issues. Use --tensor-parallel-size to override.")
        auto_tp = 1

    def build_cmd(tp_value: int) -> List[str]:
        cmd = list(base_cmd)
        if not use_external_server and not tensor_flag_in_extra:
            cmd.extend(["--tensor-parallel-size", str(tp_value)])
        cmd.extend(pdf_and_extra)
        return cmd

    def launch(tp_value: int, announce: bool) -> subprocess.CompletedProcess:
        cmd = build_cmd(tp_value)
        if announce:
            console.rule("olmOCR pipeline")
            console.print("Command:", " ".join(cmd))
        else:
            console.print(f"Retrying with tensor-parallel-size={tp_value}:", " ".join(cmd))
        return _run_with_filtered_output(cmd, env, total_items if total_items else None)

    current_tp = auto_tp
    process = launch(current_tp, announce=True)
    can_retry = (
        process.returncode != 0
        and not use_external_server
        and not tensor_flag_in_extra
        and current_tp > 1
    )
    if can_retry:
        logger.warning("olmOCR run failed with tensor-parallel-size=%d; retrying with 1. See olmocr-pipeline-debug.log for details.", current_tp)
        current_tp = 1
        process = launch(current_tp, announce=False)

    if process.returncode != 0:
        raise typer.Exit(code=process.returncode)


@app.command(help="Process an image directory and emit .md files alongside the images.")
def images(
    input_folder: Path = typer.Argument(..., help="Directory tree containing PNG/JPG files."),
    workspace: Path = typer.Option(Path("./localworkspace/images"), help="Workspace for manifests/results."),
    markdown: bool = typer.Option(True, help="Keep markdown output enabled."),
    workers: Optional[int] = typer.Option(None, help="Concurrent workers (default 12, overridable via config).", show_default=False),
    tensor_parallel_size: Optional[int] = typer.Option(None, help="Override tensor parallel size (configurable).", show_default=False),
    extra_args: Optional[str] = typer.Option(None, help="Advanced flags forwarded to the pipeline.", show_default=False),
    server_url: Optional[str] = typer.Option(None, help="Existing server URL to reuse (configurable).", show_default=False),
    server_model: Optional[str] = typer.Option(None, help="Model name to request on remote server (default 'olmocr').", show_default=False),
    preconvert: Optional[bool] = typer.Option(None, help="Pre-convert images to PDFs in parallel before running (default True).", show_default=False),
    resume: Optional[bool] = typer.Option(None, help="Skip already-completed images if done flags exist (default true).", show_default=False),
    visible_gpus: Optional[str] = typer.Option(None, help="Comma-separated GPU IDs (sets CUDA_VISIBLE_DEVICES).", show_default=False),
) -> None:
    input_folder = input_folder.resolve()
    if not input_folder.is_dir():
        raise typer.BadParameter(f"{input_folder} is not a directory")

    config_defaults = _config_for("images")
    workers = workers if workers is not None else config_defaults.get("workers", DEFAULT_IMAGE_WORKERS)
    tensor_parallel_size = (
        tensor_parallel_size if tensor_parallel_size is not None else config_defaults.get("tensor_parallel_size")
    )
    extra_args = extra_args if extra_args is not None else config_defaults.get("extra_args")
    server_url = server_url if server_url is not None else config_defaults.get("server_url")
    server_model = server_model or config_defaults.get("server_model", "olmocr")
    preconvert = preconvert if preconvert is not None else config_defaults.get("preconvert", True)
    preconvert_workers = config_defaults.get("preconvert_workers", max(2, os.cpu_count() or 4))
    resume = resume if resume is not None else config_defaults.get("resume", True)
    visible_gpus = visible_gpus if visible_gpus is not None else config_defaults.get("visible_gpus")
    completed_paths: set[str] = _load_completed_paths(workspace) if resume else set()

    manifest = workspace / ".manifests" / f"{input_folder.name}.txt"
    workspace.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)

    files = sorted(
        [
            f
            for f in input_folder.rglob("*")
            if f.is_file() and f.suffix.lower() in {".png", ".jpg", ".jpeg"}
        ]
    )
    if not files:
        raise typer.BadParameter(f"No PNG/JPG files found under {input_folder}")

    converted_root = workspace / ".converted" / input_folder.name
    pdf_mapping: dict[Path, Path] = {}
    if preconvert and files:
        console.print(f"Pre-converting {len(files)} images to PDF (workers={preconvert_workers})…")
        pdf_paths, pdf_mapping = _convert_images_parallel(files, input_folder, converted_root, preconvert_workers)
        manifest_paths = pdf_paths
        markdown_root = converted_root
    else:
        manifest_paths = files
        markdown_root = input_folder

    if completed_paths:
        before = len(manifest_paths)
        manifest_paths = [p for p in manifest_paths if str(p) not in completed_paths]
        if pdf_mapping:
            allowed = {str(p) for p in manifest_paths}
            pdf_mapping = {pdf: original for pdf, original in pdf_mapping.items() if str(pdf) in allowed}
        skipped = before - len(manifest_paths)
        if skipped:
            console.print(f"Resume: skipping {skipped} already-completed image entries.")
    if not manifest_paths:
        console.print("All requested images already completed according to done flags; nothing to do.")
        return

    manifest.write_text("\n".join(str(f) for f in manifest_paths))
    logger.info("Generated manifest %s with %d entries", manifest, len(manifest_paths))

    run(
        workspace=workspace,
        pdf=[manifest],
        markdown=markdown,
        workers=workers,
        tensor_parallel_size=tensor_parallel_size,
        extra_args=extra_args,
        server_url=server_url,
        server_model=server_model,
        visible_gpus=visible_gpus,
    )

    if pdf_mapping:
        source_iter = [(pdf, pdf_mapping[pdf]) for pdf in manifest_paths]
    else:
        source_iter = [(img, img) for img in manifest_paths]

    for source_path, original_img in source_iter:
        rel = source_path.relative_to(markdown_root)
        md_source = workspace / "markdown" / rel.with_suffix(".md")
        target_md = original_img.with_suffix(".md")
        if md_source.exists():
            target_md.parent.mkdir(parents=True, exist_ok=True)
            target_md.write_text(md_source.read_text())
            logger.info("Wrote %s", target_md)


@app.command(help="Start a persistent vLLM server with the tuned environment.")
def serve(
    port: int = typer.Option(DEFAULT_VLLM_PORT, help="Port for the OpenAI-compatible endpoint."),
    tensor_parallel_size: int = typer.Option(1, help="Tensor parallel degree."),
    workers: int = typer.Option(1, help="Data parallel degree (vLLM --data-parallel-size)."),
    gpu_memory_utilization: Optional[float] = typer.Option(None, help="vLLM --gpu-memory-utilization."),
    max_model_len: int = typer.Option(16384, help="vLLM --max-model-len."),
    extra_args: Optional[str] = typer.Option(None, help="Additional args to `vllm serve`."),
    model: str = typer.Option(DEFAULT_MODEL, help="Model tag to serve."),
) -> None:
    env = _build_env()
    cmd = _build_serve_command(
        model=model,
        port=port,
        tensor_parallel_size=tensor_parallel_size,
        workers=workers,
        gpu_memory_utilization=gpu_memory_utilization,
        max_model_len=max_model_len,
        extra_args=extra_args,
    )

    console.rule("vLLM serve")
    console.print("Command:", " ".join(cmd))
    try:
        subprocess.run(cmd, env=env, check=True)
    except KeyboardInterrupt:
        console.print("Stopping vLLM server...")


@app.command(help="Watchdog that keeps a tuned vLLM server running (auto-starts if missing).")
def daemon(
    port: int = typer.Option(DEFAULT_VLLM_PORT, help="Port for the managed server."),
    tensor_parallel_size: int = typer.Option(1, help="Tensor parallel degree."),
    workers: int = typer.Option(1, help="Data parallel degree (vLLM --data-parallel-size)."),
    gpu_memory_utilization: Optional[float] = typer.Option(None, help="vLLM --gpu-memory-utilization."),
    max_model_len: int = typer.Option(16384, help="vLLM --max-model-len."),
    extra_args: Optional[str] = typer.Option(None, help="Additional args to `vllm serve`."),
    model: str = typer.Option(DEFAULT_MODEL, help="Model tag to serve."),
    poll_interval: float = typer.Option(5.0, help="Seconds between health checks."),
    server_url: Optional[str] = typer.Option(None, help="Override the health-check URL."),
) -> None:
    env = _build_env()
    poll_interval = max(1.0, poll_interval)
    target_url = _normalize_server_url(server_url or f"http://localhost:{port}/v1")
    serve_cmd = _build_serve_command(
        model=model,
        port=port,
        tensor_parallel_size=tensor_parallel_size,
        workers=workers,
        gpu_memory_utilization=gpu_memory_utilization,
        max_model_len=max_model_len,
        extra_args=extra_args,
    )

    console.print(f"Daemon monitoring {target_url} (Ctrl+C to stop).")
    child: Optional[subprocess.Popen] = None
    restart_count = 0

    try:
        while True:
            server_up = _probe_server(target_url)
            child_alive = child is not None and child.poll() is None

            if server_up:
                if child and child.poll() is not None:
                    console.print("Managed vLLM process exited but server is already running elsewhere; daemon will not restart it.")
                    child = None
            else:
                if not child_alive:
                    restart_count += 1
                    console.print(f"Starting vLLM server (attempt #{restart_count})…")
                    child = subprocess.Popen(serve_cmd, env=env)
                else:
                    console.print("Server warming up; waiting for health check…")

            time.sleep(poll_interval)
    except KeyboardInterrupt:
        console.print("Daemon stopping…")
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()


QUEUE_RE = re.compile(r"Queue remaining: (\d+)")


def _run_with_filtered_output(cmd: List[str], env: dict, total_items: Optional[int]) -> subprocess.CompletedProcess:
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        bufsize=1,
    )
    stdout = process.stdout
    if stdout is None:  # pragma: no cover - defensive guard
        raise RuntimeError("stdout pipe was not created")
    start_time = time.time()
    suppressed_attempts = 0
    last_processed = -1
    while True:
        line = stdout.readline()
        if not line:
            break
        stripped = line.rstrip()
        if "Please wait for vllm server to become ready" in stripped:
            suppressed_attempts += 1
            continue
        if "vllm server is ready." in stripped and suppressed_attempts:
            console.print(f"vLLM server warmed up after ~{suppressed_attempts}s (startup polls suppressed).")
            suppressed_attempts = 0
        if any(pattern in stripped for pattern in NOISY_SUBSTRINGS):
            continue
        match = QUEUE_RE.search(stripped)
        if match and total_items is not None:
            remaining = int(match.group(1))
            processed = max(0, total_items - remaining)
            if processed != last_processed:
                elapsed = max(time.time() - start_time, 1e-6)
                rate = processed / elapsed if processed else 0
                eta = "?"
                if rate > 0 and remaining >= 0:
                    eta = _format_duration(remaining / rate)
                console.print(
                    f"Progress: {processed}/{total_items} done | elapsed {_format_duration(elapsed)} | ETA {eta}"
                )
                last_processed = processed
            continue
        console.print(stripped)

    process.wait()
    if suppressed_attempts:
        console.print(f"Suppressed {suppressed_attempts} vLLM health-check lines before exit.")
    return subprocess.CompletedProcess(cmd, process.returncode)


if __name__ == "__main__":
    app()
