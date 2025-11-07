#!/usr/bin/env python3
"""Validate required Markdown Web Browser environment variables."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List

from decouple import Config as DecoupleConfig, RepositoryEnv, UndefinedValueError

REQUIRED_VARS = {
    "API_BASE_URL": "FastAPI base URL for CLI + automation",
    "CFT_VERSION": "Chrome for Testing build pinned for captures",
    "CFT_LABEL": "Chrome for Testing label (Stable, Stable-1, etc.)",
    "PLAYWRIGHT_CHANNEL": "Playwright channel used for capture",
    "OLMOCR_SERVER": "Hosted olmOCR endpoint",
    "OLMOCR_MODEL": "OCR policy name declared in docs/models.yaml",
    "OCR_MIN_CONCURRENCY": "Minimum OCR concurrency",
    "OCR_MAX_CONCURRENCY": "Maximum OCR concurrency",
    "BLOCKLIST_PATH": "Path to selector blocklist JSON",
    "SCROLL_SHRINK_WARNING_THRESHOLD": "Warning threshold for scroll-height shrink retries",
    "OVERLAP_WARNING_RATIO": "Minimum acceptable overlap match ratio",
    "WARNING_LOG_PATH": "JSONL log file for capture warnings/blocklist hits",
}

OPTIONAL_VARS = {
    "MDWB_API_KEY": "Bearer token for API access",
    "OLMOCR_API_KEY": "Hosted olmOCR key",
    "OCR_LOCAL_URL": "Local olmOCR endpoint override",
    "CANVAS_WARNING_THRESHOLD": "Canvas warning threshold (defaults to 3)",
    "VIDEO_WARNING_THRESHOLD": "Video warning threshold (defaults to 2)",
}


@dataclass
class VarStatus:
    name: str
    value: str | None
    description: str
    required: bool


def load_config(env_path: Path) -> DecoupleConfig:
    if not env_path.exists():
        print(".env not found. Please copy .env.example and fill in the values.", file=sys.stderr)
        sys.exit(1)
    return DecoupleConfig(RepositoryEnv(str(env_path)))


def capture_status(config: DecoupleConfig) -> List[VarStatus]:
    statuses: List[VarStatus] = []
    for name, description in REQUIRED_VARS.items():
        try:
            value = config(name)
        except UndefinedValueError:
            value = None
        statuses.append(VarStatus(name, value, description, required=True))

    for name, description in OPTIONAL_VARS.items():
        try:
            value = config(name)
        except UndefinedValueError:
            value = None
        statuses.append(VarStatus(name, value, description, required=False))
    return statuses


def print_human(statuses: List[VarStatus]) -> int:
    missing = [var for var in statuses if var.required and not var.value]
    print("Markdown Web Browser environment check:\n")
    for var in statuses:
        flag = "OK " if var.value else ("MISS" if var.required else "OPT ")
        print(f"{flag:>4} {var.name:<18} {var.description}")
    if missing:
        print("\nMissing required variables:")
        for var in missing:
            print(f" - {var.name}: {var.description}")
    return 0 if not missing else 1


def print_json(statuses: List[VarStatus]) -> int:
    payload = [
        {
            "name": var.name,
            "value": var.value,
            "description": var.description,
            "required": var.required,
            "ok": bool(var.value),
        }
        for var in statuses
    ]
    print(json.dumps(payload, indent=2))
    missing = [var for var in statuses if var.required and not var.value]
    return 0 if not missing else 1


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Validate .env configuration")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")
    args = parser.parse_args()

    env_path = Path(args.env)
    config = load_config(env_path)
    statuses = capture_status(config)
    exit_code = print_json(statuses) if args.json else print_human(statuses)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
