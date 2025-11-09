from __future__ import annotations

import csv
import io
import json

import zstandard as zstd
from typer.testing import CliRunner

from scripts import mdwb_cli

runner = CliRunner()


def _rewrite_index(root, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in rows:
        writer.writerow(row)
    compressed = zstd.ZstdCompressor().compress(buffer.getvalue().encode("utf-8"))
    (root / "work_index_list.csv.zst").write_bytes(compressed)


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
    assert payload["completed_entries"] == ["https://example.com/article"]
    assert payload["pending_entries"] == []
    assert payload["orphan_flag_count"] == 0
    assert payload["orphan_flag_hashes"] == []


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
    assert payload["completed_entries"][0] == f"hash:{hash_value}"
    assert payload["pending_entries"] == []
    assert payload["orphan_flag_count"] == 1
    assert payload["orphan_flag_hashes"] == [hash_value]


def test_resume_status_counts_flags_missing_index(tmp_path):
    manager = mdwb_cli.ResumeManager(tmp_path)
    url_a = "https://example.com/a"
    url_b = "https://example.com/b"
    manager.mark_complete(url_a)
    manager.mark_complete(url_b)

    hash_a = mdwb_cli._resume_hash(url_a)
    _rewrite_index(tmp_path, [[hash_a, url_a]])

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
    assert payload["done"] == 2  # 1 indexed entry + 1 placeholder hash
    assert payload["total"] == 2  # missing hashes now count toward the total
    assert any(
        entry.startswith("hash:") or entry == url_b
        for entry in payload["entries"]
    )
    assert payload["orphan_flag_count"] == 1
    assert payload["orphan_flag_hashes"] == [mdwb_cli._resume_hash(url_b)]


def test_resume_status_human_output(tmp_path):
    manager = mdwb_cli.ResumeManager(tmp_path)
    url = "https://example.com/article"
    manager.mark_complete(url)

    result = runner.invoke(
        mdwb_cli.cli,
        [
            "resume",
            "status",
            "--root",
            str(tmp_path),
            "--limit",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert url in result.output
    assert "entries" in result.output.lower()


def test_resume_status_reports_orphan_flags(tmp_path):
    resume_root = tmp_path
    done_dir = resume_root / "done_flags"
    done_dir.mkdir()
    hash_value = mdwb_cli._resume_hash("https://example.com/orphan")
    (done_dir / f"done_{hash_value}.flag").write_text("ts", encoding="utf-8")

    result = runner.invoke(
        mdwb_cli.cli,
        [
            "resume",
            "status",
            "--root",
            str(resume_root),
        ],
    )

    assert result.exit_code == 0
    assert "orphan done flag" in result.output.lower()
    review_dir = resume_root / "done_flags_review"
    assert (review_dir / f"{hash_value}.flag").exists()


def test_resume_status_pending_list(tmp_path):
    resume_root = tmp_path
    manager = mdwb_cli.ResumeManager(resume_root)
    done_url = "https://example.com/done"
    pending_url = "https://example.com/pending"
    manager.mark_complete(done_url)
    hash_done = mdwb_cli._resume_hash(done_url)
    hash_pending = mdwb_cli._resume_hash(pending_url)
    _rewrite_index(resume_root, [[hash_done, done_url], [hash_pending, pending_url]])

    result = runner.invoke(
        mdwb_cli.cli,
        [
            "resume",
            "status",
            "--root",
            str(resume_root),
            "--pending",
            "--limit",
            "0",
        ],
    )

    assert result.exit_code == 0
    output = result.output
    assert "Pending entries" in output
    assert pending_url in output
    assert done_url in output
