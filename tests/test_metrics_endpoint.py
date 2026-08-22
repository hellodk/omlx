# SPDX-License-Identifier: Apache-2.0
"""Contracts for the Prometheus/VictoriaMetrics /metrics exposition endpoint.

Both Prometheus and VictoriaMetrics vmagent scrape the same text exposition
format 0.0.4, so one hand-rendered payload serves both — no vendor SDK, no
new runtime dependency. Every value here must come from live internal
counters; exporting constants would repeat the SLO-tracker mistake.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omlx import metrics_api
from omlx.server_metrics import reset_server_metrics

SERVER_PY = Path(__file__).resolve().parents[1] / "omlx/server.py"


@pytest.fixture(autouse=True)
def _fresh_metrics():
    reset_server_metrics()
    yield
    reset_server_metrics()


def _client() -> TestClient:
    app = FastAPI()
    metrics_api.register_metrics_routes(app)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Registration


def test_server_registers_metrics_routes_unconditionally():
    """Registration must precede the gated block and live outside it."""

    source = SERVER_PY.read_text()
    register_at = source.find("register_metrics_routes(app")
    gated_at = source.find("def _register_cluster_routes")

    assert register_at != -1, "server.py never registers the metrics router"
    assert gated_at != -1
    assert register_at < gated_at, (
        "metrics routes registered inside/below the gated cluster block"
    )


# ---------------------------------------------------------------------------
# Format & families


def _scrape(client: TestClient) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "text/plain; version=0.0.4"
    )
    return response.text


def test_core_families_have_help_type_and_live_values():
    from omlx.server_metrics import get_server_metrics

    get_server_metrics().record_request_complete(
        prompt_tokens=100,
        completion_tokens=50,
        cached_tokens=25,
        prefill_duration=0.2,
        generation_duration=1.0,
        model_id="qwen3-8b",
    )

    text = _scrape(_client())

    for family, mtype in (
        ("omlx_requests_total", "counter"),
        ("omlx_prompt_tokens_total", "counter"),
        ("omlx_completion_tokens_total", "counter"),
        ("omlx_cached_tokens_total", "counter"),
        ("omlx_average_prefill_tokens_per_second", "gauge"),
        ("omlx_average_generation_tokens_per_second", "gauge"),
        ("omlx_cache_efficiency_percent", "gauge"),
        ("omlx_uptime_seconds", "gauge"),
    ):
        assert f"# HELP {family} " in text, f"missing HELP for {family}"
        assert f"# TYPE {family} {mtype}" in text, f"missing TYPE for {family}"

    assert "\nomlx_requests_total 1\n" in text
    assert "\nomlx_prompt_tokens_total 100\n" in text
    assert "\nomlx_completion_tokens_total 50\n" in text
    assert "\nomlx_cached_tokens_total 25\n" in text


def test_per_model_counters_carry_model_label():
    from omlx.server_metrics import get_server_metrics

    metrics = get_server_metrics()
    metrics.record_request_complete(
        prompt_tokens=10, completion_tokens=5, model_id="model-a"
    )
    metrics.record_request_complete(
        prompt_tokens=20, completion_tokens=8, model_id="model-b"
    )
    metrics.record_request_complete(
        prompt_tokens=30, completion_tokens=9, model_id="model-b"
    )

    text = _scrape(_client())

    assert '\nomlx_model_requests_total{model="model-a"} 1\n' in text
    assert '\nomlx_model_requests_total{model="model-b"} 2\n' in text


def test_label_values_escape_special_characters():
    """Prometheus label escaping: backslash, quote, newline."""

    from omlx.server_metrics import get_server_metrics

    get_server_metrics().record_request_complete(
        prompt_tokens=1,
        completion_tokens=1,
        model_id='weird"\\model\nid',
    )

    text = _scrape(_client())

    assert '{model="weird\\"\\\\model\\nid"}' in text


def test_preflight_rejection_reasons_are_labeled():
    from omlx.server_metrics import get_server_metrics

    metrics = get_server_metrics()
    metrics.record_preflight_rejection("hard_limit")
    metrics.record_preflight_rejection("hard_limit")
    metrics.record_preflight_rejection("mystery_reason")

    text = _scrape(_client())

    assert '# TYPE omlx_preflight_rejections_total counter' in text
    assert '\nomlx_preflight_rejections_total{reason="hard_limit"} 2\n' in text
    # Unknown reasons land in "other" rather than vanishing.
    assert '\nomlx_preflight_rejections_total{reason="other"} 1\n' in text


# ---------------------------------------------------------------------------
# Histogram semantics


HISTOGRAM_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


def _parse_family(text: str, family: str) -> dict[str, float]:
    samples: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line:
            continue
        name, _, value = line.rpartition(" ")
        if name == family or name.startswith(family + "{"):
            samples[name] = float(value)
    return samples


def test_histogram_count_sum_and_buckets_track_real_records():
    from omlx.server_metrics import get_server_metrics

    metrics = get_server_metrics()
    durations = [0.02, 0.3, 7.0]
    for index, duration in enumerate(durations):
        metrics.record_request_complete(
            prompt_tokens=512,
            completion_tokens=64,
            prefill_duration=duration,
            generation_duration=duration,
            model_id=f"m{index}",
        )

    text = _scrape(_client())
    family = "omlx_prefill_duration_seconds"

    samples = _parse_family(text, family + "_count")
    assert samples[family + "_count"] == 3.0
    sums = _parse_family(text, family + "_sum")
    assert abs(sums[family + "_sum"] - sum(durations)) < 1e-6

    buckets = _parse_family(text, family + "_bucket")
    expected = {"le=\"0.05\"": 1, "le=\"0.5\"": 2, "le=\"10.0\"": 3}
    for label, want in expected.items():
        got = buckets.get(f'{family}_bucket{{{label}}}')
        assert got == want, f"{label}: expected {want}, scraped {got}"
    assert buckets[f'{family}_bucket{{le="+Inf"}}'] == 3.0

    # Cumulative invariant: buckets never decrease.
    ordered = sorted(
        (float(label.split('"')[1]), value)
        for label, value in buckets.items()
        if label.startswith('le="')
    )
    values = [value for _, value in ordered]
    assert values == sorted(values)


# ---------------------------------------------------------------------------
# Auth


def test_open_by_default_and_bearer_gated_when_configured(monkeypatch):
    """Token read at request time so rotation never needs a restart."""

    client = _client()

    monkeypatch.delenv("OMLX_METRICS_TOKEN", raising=False)
    assert client.get("/metrics").status_code == 200

    monkeypatch.setenv("OMLX_METRICS_TOKEN", "s3cret")
    assert client.get("/metrics").status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
