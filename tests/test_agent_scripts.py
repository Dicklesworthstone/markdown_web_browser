from __future__ import annotations

from pathlib import Path

import pytest

from scripts.agents import shared
from scripts import mdwb_cli


SAMPLE_MD = """
# Example Article

Welcome to the **Markdown Web Browser** demo. We focus on deterministic captures.
Second sentence explains why reproducibility matters. Third sentence adds more context!

## Next Steps

- [ ] Wire nightly smoke results into dashboards.
- [x] Add SKIP_LIBVIPS_CHECK flag to run_checks.
- Prioritize TODO: add agent starter scripts.
- Note: Validate CfT label/build in manifests.

### Action Items

1. Update docs/ops.
2. Share sample scripts with other agents.
"""


def test_summarize_markdown_truncates_to_sentences():
    summary = shared.summarize_markdown(SAMPLE_MD, sentences=2)
    assert "Markdown Web Browser demo." in summary
    assert "We focus on deterministic captures." in summary
    assert "Second sentence explains why reproducibility matters." not in summary
    assert "Third sentence adds more context" not in summary


def test_extract_todos_prefers_checkboxes_and_heading_context():
    todos = shared.extract_todos(SAMPLE_MD, max_tasks=8)
    assert todos[0].startswith("Wire nightly smoke")
    assert any(task.startswith("Prioritize TODO") for task in todos)
    assert any("Validate CfT" in task for task in todos)
    assert any("Update docs/ops" in task for task in todos)


def test_capture_markdown_validates_missing_job_id(monkeypatch):
    settings = mdwb_cli.APISettings(base_url="http://localhost", api_key=None, warning_log_path=Path("ops/warnings.jsonl"))

    def fake_submit_job(**kwargs):  # noqa: ANN001
        return {"state": "BROWSER_STARTING"}

    monkeypatch.setattr(shared, "submit_job", fake_submit_job)
    with pytest.raises(RuntimeError, match="job id"):
        shared.capture_markdown(
            url="https://example.com",
            job_id=None,
            settings=settings,
            http2=True,
            profile=None,
            ocr_policy=None,
        )
