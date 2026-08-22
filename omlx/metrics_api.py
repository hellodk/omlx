# SPDX-License-Identifier: Apache-2.0
"""Prometheus / VictoriaMetrics text exposition for oMLX internals.

One payload, two scrapers: Prometheus and VictoriaMetrics vmagent both
consume text exposition format 0.0.4 natively, so this module hand-renders
the format from live ``ServerMetrics`` counters instead of taking on a
vendor SDK dependency.

Values come from the request-completion path that already runs at every
serving endpoint; nothing here is synthesized. SLO burn rates are
deliberately absent until the tracker is actually fed (see issue #6).
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import PlainTextResponse

from .server_metrics import HISTOGRAM_BUCKETS, ServerMetrics, get_server_metrics

metrics_router = APIRouter(tags=["observability"])

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape_label(value: str) -> str:
    """Prometheus label escaping: backslash first, then quote and newline."""

    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _format_value(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _render_family(
    lines: list[str],
    name: str,
    mtype: str,
    help_text: str,
    samples: list[tuple[str, float]],
) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {mtype}")
    for suffix, value in samples:
        lines.append(f"{name}{suffix} {_format_value(value)}")


def _render_histogram(
    lines: list[str],
    name: str,
    data: dict[str, Any],
) -> None:
    """Cumulative bucket rendering per text-format 0.0.4 semantics."""

    lines.append(
        f"# HELP {name} Latency distribution observed per completed request."
    )
    lines.append(f"# TYPE {name} histogram")
    cumulative = 0
    for bound, bucket_count in zip(HISTOGRAM_BUCKETS, data["counts"]):
        cumulative += bucket_count
        lines.append(f'{name}_bucket{{le="{bound}"}} {_format_value(cumulative)}')
    lines.append(f'{name}_bucket{{le="+Inf"}} {_format_value(data["count"])}')
    lines.append(f"{name}_sum {_format_value(data['sum'])}")
    lines.append(f"{name}_count {_format_value(data['count'])}")


def render_metrics_text(metrics: ServerMetrics | None = None) -> str:
    """Render the full exposition from one consistent counter snapshot."""

    metrics = metrics or get_server_metrics()
    data = metrics.export_counters()
    totals = data["totals"]

    prefill_dur = totals["prefill_duration"]
    gen_dur = totals["generation_duration"]
    processed = totals["prompt_tokens"] - totals["cached_tokens"]
    avg_prefill_tps = processed / prefill_dur if prefill_dur > 0 else 0.0
    avg_gen_tps = (
        totals["completion_tokens"] / gen_dur if gen_dur > 0 else 0.0
    )
    cache_pct = (
        totals["cached_tokens"] / totals["prompt_tokens"] * 100
        if totals["prompt_tokens"] > 0
        else 0.0
    )

    lines: list[str] = []

    _render_family(
        lines,
        "omlx_requests_total",
        "counter",
        "Completed requests since server start.",
        [("", totals["requests"])],
    )
    _render_family(
        lines,
        "omlx_prompt_tokens_total",
        "counter",
        "Prompt tokens processed (including cache hits).",
        [("", totals["prompt_tokens"])],
    )
    _render_family(
        lines,
        "omlx_completion_tokens_total",
        "counter",
        "Completion tokens generated.",
        [("", totals["completion_tokens"])],
    )
    _render_family(
        lines,
        "omlx_cached_tokens_total",
        "counter",
        "Prompt tokens served from cache.",
        [("", totals["cached_tokens"])],
    )
    _render_family(
        lines,
        "omlx_model_requests_total",
        "counter",
        "Completed requests per model.",
        [
            (f'{{model="{_escape_label(model)}"}}', counters["requests"])
            for model, counters in sorted(data["per_model"].items())
        ],
    )
    _render_histogram(
        lines,
        "omlx_prefill_duration_seconds",
        data["histograms"]["prefill_duration_seconds"],
    )
    _render_histogram(
        lines,
        "omlx_generation_duration_seconds",
        data["histograms"]["generation_duration_seconds"],
    )
    _render_family(
        lines,
        "omlx_preflight_rejections_total",
        "counter",
        "Requests rejected before scheduling, by reason.",
        [
            (f'{{reason="{_escape_label(reason)}"}}', count)
            for reason, count in sorted(data["preflight_rejections"].items())
        ],
    )
    _render_family(
        lines,
        "omlx_average_prefill_tokens_per_second",
        "gauge",
        "Session-average prompt processing throughput.",
        [("", avg_prefill_tps)],
    )
    _render_family(
        lines,
        "omlx_average_generation_tokens_per_second",
        "gauge",
        "Session-average generation throughput.",
        [("", avg_gen_tps)],
    )
    _render_family(
        lines,
        "omlx_cache_efficiency_percent",
        "gauge",
        "Share of prompt tokens served from cache.",
        [("", cache_pct)],
    )
    _render_family(
        lines,
        "omlx_uptime_seconds",
        "gauge",
        "Seconds since server start.",
        [("", data["uptime_seconds"])],
    )

    return "\n".join(lines) + "\n"


def _require_scrape_token(authorization: str = Header(default="")) -> None:
    """Bearer gate, active only when OMLX_METRICS_TOKEN is configured.

    Read at request time so rotating the token needs no restart.
    """

    expected = os.environ.get("OMLX_METRICS_TOKEN")
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid scrape token.")


@metrics_router.get("/metrics", dependencies=[Depends(_require_scrape_token)])
async def scrape_metrics() -> PlainTextResponse:
    return PlainTextResponse(render_metrics_text(), media_type=_CONTENT_TYPE)


def register_metrics_routes(app: Any) -> None:
    """Mount GET /metrics on any deployment, gated or not."""

    app.include_router(metrics_router)
