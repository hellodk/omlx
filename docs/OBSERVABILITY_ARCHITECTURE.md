# oMLX Observability Architecture

Status: live on thecylon (2026-08-24). Owner: platform. Ticket: hellodk/omlx#26 (+ #20 alerts, #18 spans).

## Topology

```
Mac mini mm        (192.168.1.64,  8 GB)  ── omlx serve :8000 ──┐
Mac mini abhisheks (192.168.1.5,  16 GB)  ── omlx serve :8000 ──┤
                                                                │  pull /metrics every scrape_interval
k0s cluster (cylon 192.168.1.10, typhoon, raspberrypi)          │
  vmagent ─────────────── remote-write ──► VMSingle :8429 ◄─────┘
  victoria-logs-single :9428 ◄─ traefik ingress (omlx-logs.thecylon.local)
                                ◄─ log shippers on each Mac (planned #26 follow-up)
  otel-gateway ──► Tempo :3200          (traces; needs #18 span emitter)
  opik-backend OTEL ◄─────────────────  (LLM-call spans, same source; #18)
```

Serving nodes are bare-metal: the cluster *pulls* metrics from them over
the LAN (never Tailscale), and nodes *push* logs through the traefik
ingress. No node ever holds cluster credentials.

## Metrics

- Source: `GET /metrics` text exposition 0.0.4 (`omlx/metrics_api.py`),
  fed by `omlx/server_metrics.py` — counters only for outcomes,
  histograms for prefill/generation latency, gauges for uptime.
- Families: `omlx_requests_total`, `omlx_{prompt,completion,cached}_tokens_total`,
  `omlx_model_requests_total{model}`, `omlx_preflight_rejections_total{reason}`
  (hard_limit | admission_paused | memory_guard | capacity),
  `omlx_request_errors_total{reason}` (client_disconnect | generation |
  internal | timeout | other),
  `omlx_prompt_cache_requests_{hit,miss}_total`,
  `omlx_spec_{accepted,drafted}_tokens_total`, `omlx_spec_verify_cycles_total`,
  `omlx_spec_fallbacks_total`, `omlx_stats_clears_total`, `omlx_uptime_seconds`.
- Recording rules (`VMRule omlx-alerts`): cache hit-request ratio and spec
  tau per instance — dashboards and alerts consume these, never raw math.

## Alerts (VMRule `omlx-alerts`)

| Alert | Expr core | Severity |
|---|---|---|
| OMLXNodeDown | `up == 0 for 3m` | critical (watchdog) |
| OMLXHighInternalErrorRate | errors(non-disconnect)/requests > 5% for 5m | warning |
| OMLXMemoryGuardRejections | increase(memory_guard,15m) > 0 | warning |
| OMLXCapacityRejections | increase(capacity,15m) > 0 | critical |

Client disconnects are excluded from error-rate: a closed laptop is not
an incident. Stats-clears are exported so operators can annotate the one
counter discontinuity an admin action causes.

## Logs

- Node writes JSON lines to `/Users/dk/hydra-reports/omlx-serve.log`.
- Shipper (vector/promtail via launchd — follow-up) pushes to
  `http://omlx-logs.thecylon.local/insert/loki/v1/push` with labels
  `{service="omlx", host="<mini>"}`.
- Grafana Fleet dashboard panels query `{service="omlx"}`; incident
  records are filtered by marker.

## Traces

- Decision: keep Tempo as trace store (operator ships no VictoriaTraces
  CRD; otel-gateway→Tempo already terminates OTLP).
- Span emitter is ticketed (#18): env-gated OTLP, gen_ai semantic
  conventions, one span per request lifecycle at the lease choke points.
- Opik receives the same spans at its OTEL endpoint once emitters land —
  LLM-call traces become eval-ready without a second instrumentation pass.

## Dashboards

Grafana folder **oMLX** (sidecar, `grafana_folder` annotation):
- *oMLX / LLM Serving*: up, req/s, tok/s, TTFT & latency p50/p95 from
  histograms, cache efficiency (token + request basis), rejections by
  reason, errors by reason, spec tau + fallbacks, uptime table.
- *oMLX / Fleet & Incidents*: per-node up/uptime/clears, live serve log,
  incident records, per-model request totals.

Datasource rule: nothing in Grafana is flagged default; panels pin UIDs
(`VictoriaMetrics`, `VictoriaLogs`, `Tempo`).

## Change management

Everything here is code: this chart (`charts/victoria-metrics-k8s-stack`)
syncs via ArgoCD; alert/dashboard changes ship as commits. The serving
binaries deploy through hydra's domlx pipeline onto the minis.
