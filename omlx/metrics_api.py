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

import hmac
import math
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
    """Render a sample value per text-format 0.0.4 special-float rules."""

    v = float(value)
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    if v.is_integer():
        return str(int(v))
    return repr(v)


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
        "omlx_request_errors_total",
        "counter",
        "Requests that failed outside the completion path (disconnects, "
        "internal errors), by reason.",
        [
            (f'{{reason="{_escape_label(reason)}"}}', count)
            for reason, count in sorted(data["request_errors"].items())
        ],
    )
    _render_family(
        lines,
        "omlx_stats_clears_total",
        "counter",
        "Manual session-stat wipes via the admin API; each zeroes the"
        " counters above, so treat the series as discontinuous there.",
        [("", data["scrape_clears"])],
    )
    _render_family(
        lines,
        "omlx_prompt_cache_requests_hit_total",
        "counter",
        "Requests whose prompt reused cached prefix tokens.",
        [("", data["prompt_cache_requests_hit"])],
    )
    _render_family(
        lines,
        "omlx_prompt_cache_requests_miss_total",
        "counter",
        "Requests whose prompt had no cached prefix.",
        [("", data["prompt_cache_requests_miss"])],
    )
    _render_family(
        lines,
        "omlx_spec_accepted_tokens_total",
        "counter",
        "Draft tokens accepted by speculative verify (tau = accepted/drafted).",
        [("", data["spec_accepted_tokens"])],
    )
    _render_family(
        lines,
        "omlx_spec_drafted_tokens_total",
        "counter",
        "Draft tokens proposed by speculative decoding.",
        [("", data["spec_drafted_tokens"])],
    )
    _render_family(
        lines,
        "omlx_spec_verify_cycles_total",
        "counter",
        "Speculative verify cycles run.",
        [("", data["spec_cycles"])],
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

    Read at request time so rotating the token needs no restart. The
    comparison is constant-time; a TypeError from non-ascii header bytes
    is a failed comparison, not a 500.
    """

    expected = os.environ.get("OMLX_METRICS_TOKEN")
    if not expected:
        return
    # Compare bytes: compare_digest(str, str) refuses non-ascii outright,
    # which would lock out scrapers even when the configured token itself
    # is non-ascii. Both sides map through utf-8 so any configured token
    # matches exactly the header string starlette decoded from the wire.
    try:
        matched = hmac.compare_digest(
            authorization.encode("utf-8", "replace"),
            f"Bearer {expected}".encode("utf-8", "replace"),
        )
    except (TypeError, UnicodeEncodeError):
        matched = False
    if not matched:
        raise HTTPException(status_code=401, detail="Invalid scrape token.")


@metrics_router.get("/metrics", dependencies=[Depends(_require_scrape_token)])
async def scrape_metrics() -> PlainTextResponse:
    return PlainTextResponse(render_metrics_text(), media_type=_CONTENT_TYPE)


def register_metrics_routes(app: Any) -> None:
    """Mount GET /metrics on any deployment, gated or not."""

    app.include_router(metrics_router)
