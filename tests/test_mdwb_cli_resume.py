from __future__ import annotations

import json

from typer.testing import CliRunner

from scripts import mdwb_cli

runner = CliRunner()


def test_resume_status_json(tmp_path):
    manager = mdwb_cli.ResumeManager(tmp_path)
    manager.mark_complete("https://example.com/article")

    result = runner.invoke(
        mdwb_cli.cli,
        [
            "resume",
            "status",
            "--root",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["done"] == 1
    assert payload["entries"] == ["https://example.com/article"]


def test_resume_status_hash_only(tmp_path):
    resume_root = tmp_path
    done_dir = resume_root / "done_flags"
    done_dir.mkdir()
    hash_value = mdwb_cli._resume_hash("https://hash-only.example")
    (done_dir / f"done_{hash_value}.flag").write_text("ts", encoding="utf-8")

    result = runner.invoke(
        mdwb_cli.cli,
        [
            "resume",
            "status",
            "--root",
            str(resume_root),
            "--limit",
            "0",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["entries"][0] == f"hash:{hash_value}"
