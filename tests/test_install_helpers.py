from __future__ import annotations

import hashlib
import io
import stat
import zipfile
from pathlib import Path

import pytest

from scripts import install_helpers
from scripts.install_helpers import install_cft, repair_legacy_env

BUILD = "130.0.6723.69"
DETECTING = "\x1b[0;34mDetecting Playwright Chromium version...\x1b[0m"
DETECTED = "\x1b[0;32m✓ Detected Playwright Chromium: chrome-143.0.7499.4\x1b[0m"


def _legacy_env() -> str:
    # Shape of a .env after several pre-a4585d6 installer reruns (ts2, 2026-10-15).
    residue = f"{DETECTED}\nchrome-143.0.7499.4\n" * 3
    return (
        "# --- Required for mdwb CLI + ops automation ---\n"
        "API_BASE_URL=http://localhost:8000\n"
        "CFT_LABEL=Stable-1\n"
        "PLAYWRIGHT_CHANNEL=cft\n"
        f"{residue}"
        "OLMOCR_API_KEY=sk-test\n"
        f"CFT_VERSION={DETECTING}\n"
    )


def test_repair_legacy_env_strips_installer_residue_and_keeps_backup(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(_legacy_env(), encoding="utf-8")
    env.chmod(0o640)
    original = env.read_bytes()

    result = repair_legacy_env(env, BUILD)

    assert result is not None
    backup, removed = result
    assert removed == 6
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o640
    assert stat.S_IMODE(env.stat().st_mode) == 0o640
    assert env.read_text(encoding="utf-8") == (
        "# --- Required for mdwb CLI + ops automation ---\n"
        "API_BASE_URL=http://localhost:8000\n"
        "CFT_LABEL=Stable-1\n"
        "PLAYWRIGHT_CHANNEL=cft\n"
        "OLMOCR_API_KEY=sk-test\n"
        f"CFT_VERSION=chrome-{BUILD}\n"
    )
    assert repair_legacy_env(env, BUILD) is None


def test_repair_legacy_env_leaves_clean_env_byte_for_byte(tmp_path: Path):
    env = tmp_path / ".env"
    clean = b"CFT_VERSION=chrome-131.0.6778.85\nPLAYWRIGHT_CHANNEL=cft\nchrome-note=kept\n"
    env.write_bytes(clean)

    assert repair_legacy_env(env, BUILD) is None
    assert env.read_bytes() == clean
    assert sorted(path.name for path in tmp_path.iterdir()) == [".env"]


def test_repair_legacy_env_refuses_symlinked_env(tmp_path: Path):
    target = tmp_path / "real.env"
    target.write_text(_legacy_env(), encoding="utf-8")
    env = tmp_path / ".env"
    env.symlink_to(target)

    with pytest.raises(RuntimeError, match="symlink"):
        repair_legacy_env(env, BUILD)
    assert env.is_symlink()
    assert target.read_text(encoding="utf-8") == _legacy_env()


def _cft_archive() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        for name, mode, body in (
            ("chrome-linux64/chrome", 0o755, b"#!/bin/sh\necho 'Google Chrome for Testing'\n"),
            ("chrome-linux64/resources.pak", 0o644, b"data"),
        ):
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | mode) << 16
            bundle.writestr(info, body)
    return stream.getvalue()


def test_install_cft_extracts_verified_archive_with_modes(tmp_path: Path):
    archive = _cft_archive()
    urls: list[str] = []

    def fetch(url: str) -> bytes:
        urls.append(url)
        return archive

    executable = install_cft(BUILD, hashlib.sha256(archive).hexdigest(), tmp_path, fetch)

    assert executable == tmp_path / BUILD / "chrome-linux64" / "chrome"
    assert urls == [install_helpers.CFT_URL.format(build=BUILD)]
    assert stat.S_IMODE(executable.stat().st_mode) == 0o755
    assert stat.S_IMODE((executable.parent / "resources.pak").stat().st_mode) == 0o644
    assert sorted(path.name for path in tmp_path.iterdir()) == [BUILD]


def test_install_cft_refuses_digest_mismatch_before_extracting(tmp_path: Path):
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        install_cft(BUILD, "0" * 64, tmp_path / "cft", lambda _url: _cft_archive())
    assert not (tmp_path / "cft").exists()


def test_install_cft_reuses_present_build_without_fetching(tmp_path: Path):
    executable = tmp_path / BUILD / "chrome-linux64" / "chrome"
    executable.parent.mkdir(parents=True)
    executable.write_text("present")

    def fetch(_url: str) -> bytes:
        raise AssertionError("must not download an installed build")

    assert install_cft(BUILD, "0" * 64, tmp_path, fetch) == executable


@pytest.mark.parametrize("leftover", [BUILD, f".{BUILD}.partial"])
def test_install_cft_reports_incomplete_dirs_without_fetching(tmp_path: Path, leftover: str):
    (tmp_path / leftover).mkdir()

    def fetch(_url: str) -> bytes:
        raise AssertionError("must not download over an incomplete install")

    with pytest.raises(RuntimeError, match="move it aside"):
        install_cft(BUILD, "0" * 64, tmp_path, fetch)
    assert sorted(path.name for path in tmp_path.iterdir()) == [leftover]
