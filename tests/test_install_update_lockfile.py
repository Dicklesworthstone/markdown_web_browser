"""install.sh must keep updating a checkout whose uv.lock a newer uv rewrote.

Fleet 2026-11-25: `acfs update` failed MDWB (exit 128) on every host with "cannot pull with rebase: You have
unstaged changes". The launcher ran `uv run` without --frozen, so uv 0.13 rewrote the committed uv.lock (format
revision 3 -> 5) on first use, and each later `git pull --ff-only` refused the dirty tree.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parents[1] / "install.sh"
IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def git(cwd: Path, *args: str, env: dict) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True,
                          timeout=60).stdout.strip()


def fixture(tmp_path: Path):
    """origin (bare) with uv.lock at revision 3, an installed clone, then one new upstream commit."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[pull]\n\trebase = true\n[init]\n\tdefaultBranch = main\n")  # as on the fleet hosts
    env = os.environ | IDENTITY | {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
    origin, seed, install = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "markdown_web_browser"
    git(tmp_path, "init", "-q", "--bare", str(origin), env=env)
    git(tmp_path, "init", "-q", str(seed), env=env)
    (seed / "uv.lock").write_text("version = 1\nrevision = 3\n")
    (seed / "README.md").write_text("v1\n")
    git(seed, "add", ".", env=env)
    git(seed, "commit", "-q", "-m", "v1", env=env)
    git(seed, "push", "-q", str(origin), "HEAD:main", env=env)
    git(tmp_path, "clone", "-q", str(origin), str(install), env=env)
    (seed / "README.md").write_text("v2\n")
    git(seed, "commit", "-q", "-am", "v2", env=env)
    git(seed, "push", "-q", str(origin), "HEAD:main", env=env)
    return env, origin, seed, install


def run_setup_repository(env: dict, install: Path, origin: Path) -> subprocess.CompletedProcess:
    script = 'set --; source "$T_INSTALL_SH"; INSTALL_DIR="$T_DIR"; REPO_URL="$T_URL"; setup_repository'
    return subprocess.run(["bash", "-c", script], cwd=install.parent, capture_output=True, text=True, timeout=120,
                          env=env | {"T_INSTALL_SH": str(INSTALL_SH), "T_DIR": str(install), "T_URL": str(origin)})


def test_update_proceeds_when_uv_rewrote_only_the_lock(tmp_path):
    env, origin, seed, install = fixture(tmp_path)
    (install / "uv.lock").write_text("version = 1\nrevision = 5\n")  # what uv 0.13 does on first `uv run`
    result = run_setup_repository(env, install, origin)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(install, "rev-parse", "HEAD", env=env) == git(seed, "rev-parse", "HEAD", env=env)
    assert (install / "uv.lock").read_text() == "version = 1\nrevision = 3\n"
    stashes = git(install, "stash", "list", env=env)
    assert "mdwb-installer: uv.lock rewritten by uv" in stashes
    assert git(install, "show", "stash@{0}:uv.lock", env=env) == "version = 1\nrevision = 5"  # nothing lost


def test_other_local_edits_are_never_stashed(tmp_path):
    env, origin, _, install = fixture(tmp_path)
    (install / "uv.lock").write_text("version = 1\nrevision = 5\n")
    (install / "README.md").write_text("a local edit the owner made\n")
    run_setup_repository(env, install, origin)
    assert git(install, "stash", "list", env=env) == ""
    assert (install / "README.md").read_text() == "a local edit the owner made\n"
    assert (install / "uv.lock").read_text() == "version = 1\nrevision = 5\n"


def test_clean_checkout_updates_without_stashing(tmp_path):
    env, origin, seed, install = fixture(tmp_path)
    result = run_setup_repository(env, install, origin)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(install, "rev-parse", "HEAD", env=env) == git(seed, "rev-parse", "HEAD", env=env)
    assert git(install, "stash", "list", env=env) == ""


def test_every_uv_run_in_the_installer_is_frozen():
    text = INSTALL_SH.read_text()
    runs = [line for line in text.splitlines() if "uv run " in line]
    assert runs, "expected uv run calls in install.sh"
    assert all("uv run --frozen " in line for line in runs), [line for line in runs if "--frozen" not in line]
