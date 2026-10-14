from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from scripts import check_metrics

runner = CliRunner()


class StubConfig:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def __call__(self, key: str, cast=None, default=None):  # noqa: ANN001
        value = self.values.get(key, default)
        if cast and value is not None:
            return cast(value)
        return value


def test_run_check_probes_primary_and_exporter(monkeypatch):
    called: list[str] = []

    def fake_probe(url: str, timeout: float) -> float:  # noqa: ANN001
        called.append(url)
        return 12.5

    monkeypatch.setattr(check_metrics, "_probe", fake_probe)
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://api", "PROMETHEUS_PORT": 9100}),
    )

    result = runner.invoke(check_metrics.cli, ["--timeout", "1.0"])

    assert result.exit_code == 0
    assert called == ["http://api/metrics", "http://localhost:9100/metrics"]


def test_run_check_json_output(monkeypatch):
    monkeypatch.setattr(check_metrics, "_probe", lambda url, timeout: 12.3)
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://api", "PROMETHEUS_PORT": 9200}),
    )

    result = runner.invoke(check_metrics.cli, ["--json", "--no-include-exporter"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["ok_count"] == 1
    assert payload["failed_count"] == 0
    assert "generated_at" in payload
    assert payload["targets"] == [{"url": "http://api/metrics", "ok": True, "duration_ms": 12.3}]
    assert payload["total_duration_ms"] == 12.3


def test_exporter_url_override(monkeypatch):
    called: list[str] = []

    def fake_probe(url: str, timeout: float) -> float:  # noqa: ANN001
        called.append(url)
        return 9.1

    monkeypatch.setattr(check_metrics, "_probe", fake_probe)
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://api", "PROMETHEUS_PORT": 9100}),
    )

    result = runner.invoke(
        check_metrics.cli,
        [
            "--exporter-url",
            "https://prom.internal/metrics",
        ],
    )

    assert result.exit_code == 0
    assert called == ["http://api/metrics", "https://prom.internal/metrics"]


def test_exporter_url_trailing_slash_trimmed(monkeypatch):
    called: list[str] = []

    def fake_probe(url: str, timeout: float) -> float:  # noqa: ANN001
        called.append(url)
        return 6.6

    monkeypatch.setattr(check_metrics, "_probe", fake_probe)
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://api", "PROMETHEUS_PORT": 9100}),
    )

    result = runner.invoke(check_metrics.cli, ["--exporter-url", "https://prom.example/metrics/"])

    assert result.exit_code == 0
    assert called == ["http://api/metrics", "https://prom.example/metrics"]


def test_exporter_host_port_override_and_timeout(monkeypatch):
    calls: list[tuple[str, float]] = []

    def fake_probe(url: str, timeout: float) -> float:  # noqa: ANN001
        calls.append((url, timeout))
        return 7.7

    monkeypatch.setattr(check_metrics, "_probe", fake_probe)
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://ignored", "PROMETHEUS_PORT": 9000}),
    )

    result = runner.invoke(
        check_metrics.cli,
        [
            "--api-base",
            "http://override",
            "--exporter-host",
            "metrics",
            "--exporter-port",
            "9400",
            "--timeout",
            "2.5",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        ("http://override/metrics", 2.5),
        ("http://metrics:9400/metrics", 2.5),
    ]


def test_run_check_skips_exporter_when_port_zero(monkeypatch):
    called: list[str] = []

    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://api", "PROMETHEUS_PORT": 0}),
    )

    def fake_probe(url: str, timeout: float) -> float:  # noqa: ANN001
        called.append(url)
        return 4.2

    monkeypatch.setattr(check_metrics, "_probe", fake_probe)

    result = runner.invoke(check_metrics.cli, [])

    assert result.exit_code == 0
    assert called == ["http://api/metrics"]


def test_exporter_url_ignored_when_include_disabled(monkeypatch):
    called: list[str] = []

    def fake_probe(url: str, timeout: float) -> float:  # noqa: ANN001
        called.append(url)
        return 5.0

    monkeypatch.setattr(check_metrics, "_probe", fake_probe)
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://api", "PROMETHEUS_PORT": 9100}),
    )

    result = runner.invoke(
        check_metrics.cli,
        [
            "--no-include-exporter",
            "--exporter-url",
            "https://metrics.example/metrics",
        ],
    )

    assert result.exit_code == 0
    assert called == ["http://api/metrics"]


def test_api_base_trailing_slash_is_stripped(monkeypatch):
    called: list[str] = []

    def fake_probe(url: str, timeout: float) -> float:  # noqa: ANN001
        called.append(url)
        return 4.4

    monkeypatch.setattr(check_metrics, "_probe", fake_probe)
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://ignored", "PROMETHEUS_PORT": 0}),
    )

    result = runner.invoke(
        check_metrics.cli,
        [
            "--api-base",
            "http://custom/",
            "--no-include-exporter",
        ],
    )

    assert result.exit_code == 0
    assert called == ["http://custom/metrics"]


def test_run_check_reports_failures(monkeypatch):
    def fake_probe(url: str, timeout: float) -> None:  # noqa: ANN001
        raise RuntimeError("boom")

    monkeypatch.setattr(check_metrics, "_probe", fake_probe)
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://api", "PROMETHEUS_PORT": 9000}),
    )

    result = runner.invoke(check_metrics.cli, ["--no-include-exporter"])

    assert result.exit_code == 1


def test_run_check_weekly_success(tmp_path, monkeypatch):
    summary_path = tmp_path / "weekly.json"
    summary_path.write_text(
        json.dumps(
            {
                "categories": [
                    {
                        "name": "Docs",
                        "slo": {
                            "capture_ok": True,
                            "capture_p99_ms": 1000,
                            "capture_budget_ms": 2000,
                            "ocr_ok": True,
                            "ocr_p99_ms": 1000,
                            "ocr_budget_ms": 2000,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(check_metrics, "_probe", lambda url, timeout: 1.0)
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://api", "PROMETHEUS_PORT": 0}),
    )

    result = runner.invoke(
        check_metrics.cli,
        ["--no-include-exporter", "--check-weekly", "--weekly-summary", str(summary_path)],
    )

    assert result.exit_code == 0
    assert "[OK] Weekly SLO" in result.output


def test_run_check_weekly_failure(tmp_path, monkeypatch):
    summary_path = tmp_path / "weekly.json"
    summary_path.write_text(
        json.dumps(
            {
                "categories": [
                    {
                        "name": "Apps",
                        "slo": {
                            "capture_ok": False,
                            "capture_p99_ms": 5000,
                            "capture_budget_ms": 4000,
                            "ocr_ok": True,
                            "ocr_p99_ms": 1000,
                            "ocr_budget_ms": 2000,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(check_metrics, "_probe", lambda url, timeout: 1.0)
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://api", "PROMETHEUS_PORT": 0}),
    )

    result = runner.invoke(
        check_metrics.cli,
        ["--no-include-exporter", "--check-weekly", "--weekly-summary", str(summary_path)],
    )

    assert result.exit_code == 1
    assert "Weekly SLO violations" in result.output


def test_run_check_json_weekly_success(tmp_path, monkeypatch):
    summary_path = tmp_path / "weekly.json"
    summary_path.write_text(
        json.dumps(
            {
                "categories": [
                    {
                        "name": "Docs",
                        "slo": {
                            "capture_ok": True,
                            "capture_p99_ms": 1000,
                            "capture_budget_ms": 2000,
                            "ocr_ok": True,
                            "ocr_p99_ms": 1000,
                            "ocr_budget_ms": 2000,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(check_metrics, "_probe", lambda url, timeout: 1.0)
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://api", "PROMETHEUS_PORT": 0}),
    )

    result = runner.invoke(
        check_metrics.cli,
        [
            "--json",
            "--no-include-exporter",
            "--check-weekly",
            "--weekly-summary",
            str(summary_path),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["weekly"]["status"] == "ok"
    assert payload["weekly"]["summary_path"] == str(summary_path)
    assert payload["weekly"]["failures"] == []


def test_run_check_json_weekly_missing_file(tmp_path, monkeypatch):
    summary_path = tmp_path / "missing.json"
    monkeypatch.setattr(check_metrics, "_probe", lambda url, timeout: 1.0)
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://api", "PROMETHEUS_PORT": 0}),
    )

    result = runner.invoke(
        check_metrics.cli,
        [
            "--json",
            "--no-include-exporter",
            "--check-weekly",
            "--weekly-summary",
            str(summary_path),
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["weekly"]["status"] == "error"
    assert payload["weekly"]["summary_path"] == str(summary_path)
    assert payload["weekly"]["failures"]
    assert "weekly summary not found" in payload["weekly"]["failures"][0]


def test_run_check_json_reports_failures(monkeypatch):
    def fake_probe(url: str, timeout: float) -> None:  # noqa: ANN001
        raise RuntimeError("boom")

    monkeypatch.setattr(check_metrics, "_probe", fake_probe)
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://api", "PROMETHEUS_PORT": 9000}),
    )

    result = runner.invoke(check_metrics.cli, ["--json", "--no-include-exporter"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["failed_count"] == 1
    assert payload["targets"][0]["ok"] is False


def test_run_check_json_mixed_results(monkeypatch):
    durations: dict[str, float] = {"http://api/metrics": 8.0}

    def fake_probe(url: str, timeout: float) -> float:  # noqa: ANN001
        if url == "http://api/metrics":
            return durations[url]
        raise RuntimeError("exporter down")

    monkeypatch.setattr(check_metrics, "_probe", fake_probe)
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://api", "PROMETHEUS_PORT": 9400}),
    )

    result = runner.invoke(
        check_metrics.cli, ["--json", "--exporter-host", "prom", "--timeout", "1.0"]
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["ok_count"] == 1
    assert payload["failed_count"] == 1
    assert payload["targets"][0] == {
        "url": "http://api/metrics",
        "ok": True,
        "duration_ms": durations["http://api/metrics"],
    }
    assert payload["targets"][1]["ok"] is False


def test_run_check_json_reports_total_duration(monkeypatch):
    def fake_probe(url: str, timeout: float) -> float:  # noqa: ANN001
        return {
            "http://api/metrics": 5.5,
            "http://localhost:9100/metrics": 7.5,
        }[url]

    monkeypatch.setattr(check_metrics, "_probe", fake_probe)
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig({"API_BASE_URL": "http://api", "PROMETHEUS_PORT": 9100}),
    )

    result = runner.invoke(check_metrics.cli, ["--json", "--timeout", "1.5"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["total_duration_ms"] == pytest.approx(13.0)


def test_run_check_console_outputs_duration(monkeypatch):
    monkeypatch.setattr(
        check_metrics, "_load_config", lambda: StubConfig({"API_BASE_URL": "http://api"})
    )

    def fake_probe(url: str, timeout: float) -> float:  # noqa: ANN001
        assert url == "http://api/metrics"
        return 15.5

    monkeypatch.setattr(check_metrics, "_probe", fake_probe)

    result = runner.invoke(check_metrics.cli, ["--timeout", "2.0", "--no-include-exporter"])

    assert result.exit_code == 0
    assert "[OK] http://api/metrics (15.5 ms)" in result.output


def test_metrics_key_uses_custom_header_only_for_api(monkeypatch):
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="mdwb_jobs_total 1\n")

    real_client = httpx.Client
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(
        check_metrics.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    monkeypatch.setattr(
        check_metrics,
        "_load_config",
        lambda: StubConfig(
            {
                "API_BASE_URL": "http://configured",
                "API_KEY_HEADER": "X-Research-Key",
                "MDWB_API_KEY": "fixture-secret",
            }
        ),
    )
    result = runner.invoke(
        check_metrics.cli,
        ["--api-base", "http://override", "--exporter-url", "http://exporter/metrics", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert [str(request.url) for request in requests] == [
        "http://override/metrics",
        "http://exporter/metrics",
    ]
    assert requests[0].headers["X-Research-Key"] == "fixture-secret"
    assert "X-Research-Key" not in requests[1].headers
    assert all(
        request.headers["User-Agent"] == "OpenAI File Downloader, XaiImageApiFetch/1.0"
        for request in requests
    )
    assert "fixture-secret" not in result.output


def test_metrics_auth_failure_remains_failure(monkeypatch):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "API key required"})

    real_client = httpx.Client
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(
        check_metrics.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    monkeypatch.setattr(
        check_metrics, "_load_config", lambda: StubConfig({"API_BASE_URL": "http://api"})
    )
    result = runner.invoke(check_metrics.cli, ["--no-include-exporter", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["targets"][0]["ok"] is False
    assert "401" in payload["targets"][0]["error"]


@pytest.mark.parametrize(
    "data",
    [
        [],
        {},
        {"categories": []},
        {"categories": "invalid"},
        {"categories": ["invalid"]},
        {"categories": [{"name": "", "slo": {}}]},
        {"categories": [{"name": "Docs", "slo": []}]},
        {"categories": [{"name": "Docs", "slo": {"capture_ok": True, "ocr_ok": True}}]},
    ],
)
def test_weekly_malformed_or_missing_measurements_fail_aggregate_status(
    tmp_path, monkeypatch, data
):
    summary_path = tmp_path / "weekly.json"
    summary_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(check_metrics, "_probe", lambda url, timeout: 1.0)
    monkeypatch.setattr(
        check_metrics, "_load_config", lambda: StubConfig({"API_BASE_URL": "http://api"})
    )
    result = runner.invoke(
        check_metrics.cli,
        [
            "--json",
            "--no-include-exporter",
            "--check-weekly",
            "--weekly-summary",
            str(summary_path),
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["failed_count"] == 0
    assert payload["weekly"]["status"] == "error"
    assert payload["weekly"]["failures"]


@pytest.mark.parametrize(
    "mutation", ["false_flag", "exceeded", "nan", "negative", "bool", "duplicate"]
)
def test_weekly_invalid_flags_or_measurements_fail(tmp_path, monkeypatch, mutation):
    slo = {
        "capture_ok": True,
        "capture_p99_ms": 1000,
        "capture_budget_ms": 2000,
        "ocr_ok": True,
        "ocr_p99_ms": 1000,
        "ocr_budget_ms": 2000,
    }
    if mutation == "false_flag":
        slo["capture_ok"] = False
    elif mutation == "exceeded":
        slo["capture_p99_ms"] = 3000
    elif mutation == "nan":
        slo["capture_p99_ms"] = float("nan")
    elif mutation == "negative":
        slo["ocr_budget_ms"] = -1
    elif mutation == "bool":
        slo["capture_p99_ms"] = True
    categories = [{"name": "Docs", "slo": slo}]
    if mutation == "duplicate":
        categories.append({"name": "Docs", "slo": slo})
    summary_path = tmp_path / "weekly.json"
    summary_path.write_text(json.dumps({"categories": categories}), encoding="utf-8")
    monkeypatch.setattr(check_metrics, "_probe", lambda url, timeout: 1.0)
    monkeypatch.setattr(
        check_metrics, "_load_config", lambda: StubConfig({"API_BASE_URL": "http://api"})
    )
    result = runner.invoke(
        check_metrics.cli,
        [
            "--json",
            "--no-include-exporter",
            "--check-weekly",
            "--weekly-summary",
            str(summary_path),
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["weekly"]["status"] == "error"
    assert payload["weekly"]["failures"]
