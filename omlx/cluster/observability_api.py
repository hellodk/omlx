# SPDX-License-Identifier: Apache-2.0
"""Read-only SLO / error-budget / incident endpoints for every deployment.

These four routes used to live on the gated cluster router, which meant a
single-node host — distributed inference never enabled — got 404s where its
dashboard expected data. They now own one dedicated router that is registered
unconditionally with admin auth only.

The tracker factories stay in :mod:`omlx.cluster.routes` and are imported
lazily inside the handlers: importing this module must stay cheap because it
loads at server startup whether or not the cluster stack ever runs.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

slo_router = APIRouter(prefix="/admin/api/cluster", tags=["cluster-observability"])


@slo_router.get("/slos")
async def cluster_slos():
    """Return current SLO compliance status for all defined objectives."""

    from .routes import get_slo_tracker

    tracker = await asyncio.to_thread(get_slo_tracker)
    return tracker.status_dict()


@slo_router.get("/error-budget")
async def cluster_error_budget():
    """Return per-SLO error budget status and deployment readiness."""

    from .routes import get_error_budget_tracker

    tracker = await asyncio.to_thread(get_error_budget_tracker)
    return tracker.budget_status()


@slo_router.get("/incidents")
async def cluster_incidents(since: int = Query(default=0, ge=0)):
    """Return incidents after the caller's cursor, plus the new cursor.

    The ``since`` cursor makes monotonic merge a server-enforced property: a
    poll can only ever add records the browser has not seen, so no refresh can
    wipe error state. Dismissal (below) is the only removal path.
    """

    try:
        store = get_cluster_incidents()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    incidents = [incident.to_dict() for incident in store.list(since_seq=since)]
    for item in incidents:
        item["message"] = _redact_diagnostic(item["message"])
    return {
        "incidents": incidents,
        "latest_seq": store.latest_seq(),
        # Identity of the seq numbering. A corrupt-log reset restarts seq at
        # 1 under a new epoch; a client holding an old cursor must detect the
        # change and restart from 0 instead of going silent forever.
        "epoch": store.epoch,
    }


@slo_router.post("/incidents/{incident_id}/dismiss")
async def dismiss_cluster_incident(incident_id: str):
    """Mark one incident dismissed — server-owned, so it survives reloads."""

    try:
        store = get_cluster_incidents()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not store.dismiss(incident_id):
        raise HTTPException(status_code=404, detail="Unknown incident.")
    return {"ok": True}


def get_cluster_incidents():
    from .incidents import get_cluster_incidents as _get

    return _get()


def _redact_diagnostic(message: str) -> str:
    from .routes import _redact_diagnostic as _redact

    return _redact(message)


def register_observability_routes(app: Any, require_admin: Any) -> None:
    """Mount the read-only observability surfaces with admin auth only."""

    app.include_router(slo_router, dependencies=[Depends(require_admin)])
