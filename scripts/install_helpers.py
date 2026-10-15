#!/usr/bin/env python3
"""Installer helpers: repair legacy .env residue and fetch the pinned Chrome for Testing build."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import stat
import sys
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

USER_AGENT = "mdwb-installer (+https://github.com/Dicklesworthstone/markdown_web_browser)"
CFT_URL = (
    "https://storage.googleapis.com/chrome-for-testing-public/{build}/linux64/chrome-linux64.zip"
)
DEFAULT_CFT_ROOT = Path.home() / ".cache/mdwb/cft"
ESC = "\x1b["
BARE_BUILD = re.compile(r"chrome-\d+\.\d+\.\d+\.\d+")


def repair_legacy_env(path: Path, build: str) -> tuple[Path, int] | None:
    """Remove the log lines that installers before a4585d6 wrote into ``.env``.

    Their detect_cft_version printed colored progress to stdout and the caller captured it as
    CFT_VERSION, so every rerun appended the residue again. Only that residue is touched: lines
    starting with an ANSI escape, the bare build lines it left behind, and an escape-laden
    CFT_VERSION value, which becomes the pinned build. Any other file stays byte-for-byte.
    Returns the backup path and the number of lines removed, or None when nothing changed.
    """
    original = path.read_bytes()
    lines = original.decode("utf-8").splitlines(keepends=True)
    if not any(line.startswith((ESC, "CFT_VERSION=" + ESC)) for line in lines):
        return None
    if path.is_symlink():
        raise RuntimeError(f"{path} is a symlink; remove lines starting with ESC[ by hand")
    kept = []
    for line in lines:
        body = line.rstrip("\r\n")
        if body.startswith(ESC) or BARE_BUILD.fullmatch(body):
            continue
        if body.startswith("CFT_VERSION=" + ESC):
            line = f"CFT_VERSION=chrome-{build}\n"
        kept.append(line)
    mode = stat.S_IMODE(path.stat().st_mode)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = path.with_name(f"{path.name}.bak-legacy-cft-{stamp}")
    with backup.open("xb") as stream:
        stream.write(original)
    backup.chmod(mode)
    fd, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "wb") as stream:
        stream.write("".join(kept).encode("utf-8"))
    os.chmod(staged, mode)
    os.replace(staged, path)
    return backup, len(lines) - len(kept)


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=600) as response:
        return response.read()


def install_cft(
    build: str,
    sha256: str,
    root: Path = DEFAULT_CFT_ROOT,
    fetch: Callable[[str], bytes] = download,
) -> Path:
    """Place the verified linux64 archive at ``root/<build>/chrome-linux64/chrome``.

    The archive is hashed in memory and nothing is extracted unless it matches ``sha256``.
    Extraction goes to ``root/.<build>.partial`` and is renamed into place, so an interrupted
    run never looks installed; a leftover staging dir is reported rather than reused.
    """
    final = root / build
    executable = final / "chrome-linux64" / "chrome"
    if executable.is_file():
        return executable
    if final.exists():
        raise RuntimeError(f"{final} exists without chrome-linux64/chrome; move it aside")
    staging = root / f".{build}.partial"
    if staging.exists():
        raise RuntimeError(f"{staging} is left from an interrupted install; move it aside")
    archive = fetch(CFT_URL.format(build=build))
    observed = hashlib.sha256(archive).hexdigest()
    if observed != sha256:
        raise RuntimeError(
            f"Chrome for Testing {build} archive checksum mismatch ({observed}); "
            "nothing was extracted"
        )
    root.mkdir(parents=True, exist_ok=True)
    staging.mkdir()
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for member in bundle.infolist():
            extracted = Path(bundle.extract(member, staging))
            mode = (member.external_attr >> 16) & 0o777
            if mode and not member.is_dir():
                extracted.chmod(mode)
    if not (staging / "chrome-linux64" / "chrome").is_file():
        raise RuntimeError(f"archive has no chrome-linux64/chrome; extracted to {staging}")
    staging.rename(final)
    return executable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    repair = commands.add_parser("repair-legacy-env", help="remove pre-a4585d6 installer residue")
    repair.add_argument("--env", type=Path, default=Path(".env"))
    repair.add_argument("--build", required=True)
    fetch = commands.add_parser("install-cft", help="fetch the pinned linux64 CfT archive")
    fetch.add_argument("--build", required=True)
    fetch.add_argument("--sha256", required=True)
    fetch.add_argument("--root", type=Path, default=DEFAULT_CFT_ROOT)
    args = parser.parse_args(argv)
    try:
        if args.command == "repair-legacy-env":
            result = repair_legacy_env(args.env, args.build)
            if result is not None:
                backup, removed = result
                print(
                    f"Repaired {args.env}: removed {removed} installer log lines and set "
                    f"CFT_VERSION=chrome-{args.build}; original kept at {backup}"
                )
        else:
            executable = install_cft(args.build, args.sha256, args.root)
            print(f"Chrome for Testing executable: {executable}")
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
