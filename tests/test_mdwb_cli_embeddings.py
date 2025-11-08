from __future__ import annotations

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from scripts import mdwb_cli

runner = CliRunner()


class StubResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):  # noqa: ANN001
        return self._payload


class StubClient:
    def __init__(self, response: StubResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, path: str, json=None):  # noqa: ANN001
        self.calls.append((path, json or {}))
        return self.response


def _fake_settings():
    return mdwb_cli.APISettings(base_url="http://localhost", api_key=None, warning_log_path=Path("ops/warnings.jsonl"))


def test_embeddings_search_pretty(monkeypatch):
    response = StubResponse(
        200,
        payload={
            "total_sections": 10,
            "matches": [
                {"section_id": "s1", "tile_start": 0, "tile_end": 3, "similarity": 0.91, "distance": 0.09},
            ],
        },
    )
    stub = StubClient(response)
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings: stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(
        mdwb_cli.cli,
        ["jobs", "embeddings", "search", "job123", "--vector", "0.1 0.2", "--top-k", "3"],
    )

    assert result.exit_code == 0, result.output
    assert stub.calls == [("/jobs/job123/embeddings/search", {"vector": [0.1, 0.2], "top_k": 3})]
    assert "Embedding Matches" in result.output
    assert "s1" in result.output


def test_embeddings_search_json(monkeypatch, tmp_path: Path):
    vector_path = tmp_path / "vector.json"
    vector_path.write_text("[0.5, 0.75]", encoding="utf-8")
    response = StubResponse(200, payload={"total_sections": 1, "matches": []})
    stub = StubClient(response)
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings: stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(
        mdwb_cli.cli,
        ["jobs", "embeddings", "search", "run-1", "--vector-file", str(vector_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert '"total_sections": 1' in result.output
    assert stub.calls[0][1]["vector"] == [0.5, 0.75]


def test_embeddings_search_requires_vector(monkeypatch):
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())
    result = runner.invoke(mdwb_cli.cli, ["jobs", "embeddings", "search", "job123"])
    assert result.exit_code != 0
    assert "Provide --vector or --vector-file" in result.output
