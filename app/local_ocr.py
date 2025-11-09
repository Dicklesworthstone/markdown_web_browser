"""Local OCR inference using vLLM or SGLang for on-premises deployment."""

from __future__ import annotations

import base64
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

LOGGER = logging.getLogger(__name__)


@dataclass
class GPUInfo:
    """Information about available GPUs."""

    count: int
    names: List[str]
    memory_total: List[int]  # MB per GPU
    driver_version: str
    cuda_version: str


def detect_gpus() -> Optional[GPUInfo]:
    """Detect available NVIDIA GPUs using nvidia-smi.

    Returns:
        GPUInfo if GPUs are available, None otherwise
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version,cuda_version",
             "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=True,
        )

        lines = result.stdout.strip().split("\n")
        if not lines or not lines[0]:
            return None

        names = []
        memory_total = []
        driver_version = None
        cuda_version = None

        for line in lines:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                names.append(parts[0])
                memory_total.append(int(float(parts[1])))
                driver_version = parts[2]
                cuda_version = parts[3]

        return GPUInfo(
            count=len(names),
            names=names,
            memory_total=memory_total,
            driver_version=driver_version or "unknown",
            cuda_version=cuda_version or "unknown",
        )

    except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
        return None


class VLLMServer:
    """vLLM server manager for local OCR inference."""

    def __init__(
        self,
        model: str = "allenai/olmocr-2-7b-1025-fp8",
        host: str = "0.0.0.0",
        port: int = 8001,
        gpu_memory_utilization: float = 0.9,
        max_model_len: int = 4096,
        tensor_parallel_size: Optional[int] = None,
    ):
        """Initialize vLLM server configuration.

        Args:
            model: HuggingFace model identifier
            host: Server host
            port: Server port
            gpu_memory_utilization: GPU memory fraction to use (0.0-1.0)
            max_model_len: Maximum sequence length
            tensor_parallel_size: Number of GPUs for tensor parallelism (auto-detect if None)
        """
        self.model = model
        self.host = host
        self.port = port
        self.gpu_memory_utilization = gpu_memory_utilization
        self.max_model_len = max_model_len

        # Auto-detect GPU configuration
        gpu_info = detect_gpus()
        if gpu_info:
            self.tensor_parallel_size = tensor_parallel_size or gpu_info.count
            LOGGER.info(
                f"Detected {gpu_info.count} GPUs: {', '.join(gpu_info.names)}"
            )
        else:
            self.tensor_parallel_size = 1
            LOGGER.warning("No GPUs detected, using CPU mode")

    def start_server(self) -> subprocess.Popen:
        """Start vLLM server in a subprocess.

        Returns:
            Popen object for the server process
        """
        cmd = [
            "python", "-m", "vllm.entrypoints.openai.api_server",
            "--model", self.model,
            "--host", self.host,
            "--port", str(self.port),
            "--gpu-memory-utilization", str(self.gpu_memory_utilization),
            "--max-model-len", str(self.max_model_len),
            "--tensor-parallel-size", str(self.tensor_parallel_size),
            "--trust-remote-code",  # Required for olmOCR models
        ]

        LOGGER.info(f"Starting vLLM server: {' '.join(cmd)}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        LOGGER.info(f"vLLM server started with PID {process.pid}")
        return process

    async def health_check(self, timeout: int = 5) -> bool:
        """Check if vLLM server is healthy.

        Args:
            timeout: Request timeout in seconds

        Returns:
            True if server is healthy, False otherwise
        """
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"http://{self.host}:{self.port}/health")
                return response.status_code == 200
        except Exception:
            return False


class LocalOCRClient:
    """Client for local OCR inference via vLLM or SGLang."""

    def __init__(
        self,
        endpoint: str = "http://localhost:8001/v1/completions",
        model: str = "allenai/olmocr-2-7b-1025-fp8",
        timeout: int = 30,
    ):
        """Initialize local OCR client.

        Args:
            endpoint: Local inference server endpoint
            model: Model identifier
            timeout: Request timeout in seconds
        """
        self.endpoint = endpoint
        self.model = model
        self.timeout = timeout

    async def process_tile(
        self,
        tile_bytes: bytes,
        prompt: Optional[str] = None,
    ) -> str:
        """Process a single tile with local OCR.

        Args:
            tile_bytes: PNG image bytes
            prompt: Optional custom prompt (uses default if not provided)

        Returns:
            Markdown text extracted from image
        """
        # Encode image to base64
        image_b64 = base64.b64encode(tile_bytes).decode("utf-8")

        # Default olmOCR prompt
        if prompt is None:
            prompt = "Convert this image to markdown. Preserve all text, structure, and formatting."

        # Create request payload (OpenAI-compatible format)
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": [image_b64],
            "max_tokens": 2048,
            "temperature": 0.0,  # Deterministic output
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.endpoint, json=payload)
            response.raise_for_status()

            data = response.json()

            # Extract text from response
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0].get("text", "")

            raise ValueError(f"Unexpected response format: {data}")

    async def process_batch(
        self,
        tiles: List[bytes],
        batch_size: int = 3,
    ) -> List[str]:
        """Process multiple tiles in batches.

        Args:
            tiles: List of PNG image bytes
            batch_size: Number of tiles to process concurrently

        Returns:
            List of Markdown texts
        """
        import asyncio

        results = []

        for i in range(0, len(tiles), batch_size):
            batch = tiles[i : i + batch_size]

            # Process batch concurrently
            tasks = [self.process_tile(tile) for tile in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in batch_results:
                if isinstance(result, Exception):
                    LOGGER.error(f"OCR failed for tile: {result}")
                    results.append("")  # Empty result for failed tile
                else:
                    results.append(result)

        return results


async def start_local_ocr_server(
    model: str = "allenai/olmocr-2-7b-1025-fp8",
    host: str = "0.0.0.0",
    port: int = 8001,
    wait_for_ready: bool = True,
    ready_timeout: int = 300,
) -> subprocess.Popen:
    """Start local OCR server and optionally wait for it to be ready.

    Args:
        model: Model identifier
        host: Server host
        port: Server port
        wait_for_ready: Wait for server to be ready before returning
        ready_timeout: Maximum time to wait for server (seconds)

    Returns:
        Process handle for the server

    Example:
        # In a startup script
        process = await start_local_ocr_server()

        # Use the server
        client = LocalOCRClient()
        markdown = await client.process_tile(tile_bytes)

        # Cleanup
        process.terminate()
        process.wait()
    """
    import asyncio

    server = VLLMServer(
        model=model,
        host=host,
        port=port,
    )

    process = server.start_server()

    if wait_for_ready:
        LOGGER.info("Waiting for vLLM server to be ready...")

        for attempt in range(ready_timeout):
            await asyncio.sleep(1)

            if await server.health_check():
                LOGGER.info(f"vLLM server ready after {attempt + 1} seconds")
                return process

        # Timeout
        process.terminate()
        process.wait()
        raise TimeoutError(
            f"vLLM server not ready after {ready_timeout} seconds"
        )

    return process


# Convenience function for running local OCR from command line
def cli_start_server():
    """CLI entry point for starting local OCR server.

    Usage:
        python -m app.local_ocr
    """
    import asyncio
    import argparse

    parser = argparse.ArgumentParser(description="Start local OCR server")
    parser.add_argument(
        "--model",
        default="allenai/olmocr-2-7b-1025-fp8",
        help="Model to load",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Server host")
    parser.add_argument("--port", type=int, default=8001, help="Server port")
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Don't wait for server to be ready",
    )

    args = parser.parse_args()

    async def main():
        process = await start_local_ocr_server(
            model=args.model,
            host=args.host,
            port=args.port,
            wait_for_ready=not args.no_wait,
        )

        print(f"\n✅ vLLM server running on http://{args.host}:{args.port}")
        print(f"   Model: {args.model}")
        print(f"   PID: {process.pid}\n")
        print("Press Ctrl+C to stop\n")

        try:
            process.wait()
        except KeyboardInterrupt:
            print("\n⚠️  Shutting down...")
            process.terminate()
            process.wait()
            print("✓ Server stopped\n")

    asyncio.run(main())


if __name__ == "__main__":
    cli_start_server()
