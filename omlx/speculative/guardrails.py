# SPDX-License-Identifier: Apache-2.0
"""Speculative-decode guardrails (#40).

Speculation trades headroom for throughput: draft buffers and verify KV
allocate *on top of* the resident model. On constrained hosts that extra
pressure pushes work into swap and speculation loses to plain decode —
measured externally as 33 tok/s (spec ON) vs 37 tok/s (OFF) on a 24 GB
card at 128K context.

Policy (pure functions, no mlx dependency — unit-testable anywhere):

- Never speculate while the memory enforcer reports soft/hard pressure.
- Require a free-RAM floor so the draft buffers cannot tip the machine
  into swap. A missing snapshot fails OPEN: telemetry absence must not
  change behavior versus a build without this module (pre-existing
  behavior was ungated).
"""

from __future__ import annotations

# Free bytes that must remain available beyond current usage before we
# accept the overhead of draft buffers + verify-position KV. Sized to
# cover depth-k MTP drafts plus transient verify allocations on 16 GB
# class machines; tunable via env by callers if needed.
DEFAULT_SPEC_FREE_FLOOR_BYTES = 1536 * 1024 * 1024  # 1.5 GiB

_PRESSURE_BLOCK_LEVELS = frozenset({"soft", "hard"})


def free_bytes_from_vm_stats(vm_stats: object | None) -> int | None:
    """Best-effort free-RAM estimate from a mach vm_statistics64 snapshot.

    Uses free + inactive (inactive is reclaimable without swapping).
    Returns ``None`` when the snapshot is absent or incomplete — callers
    treat ``None`` as "cannot judge" and fail open.
    """
    if vm_stats is None:
        return None
    free = getattr(vm_stats, "free_bytes", None)
    inactive = getattr(vm_stats, "inactive_bytes", None)
    if free is None or inactive is None:
        return None
    return int(free) + int(inactive)


def should_engage_speculation(
    *,
    pressure_level: str | None = None,
    free_bytes: int | None = None,
    floor_bytes: int = DEFAULT_SPEC_FREE_FLOOR_BYTES,
) -> tuple[bool, str]:
    """Decide whether speculative decoding may engage for this request.

    Returns ``(engage, reason)``. ``reason`` explains any refusal and is
    written straight into the routing skip log.
    """
    level = (pressure_level or "").strip().lower()
    if level in _PRESSURE_BLOCK_LEVELS:
        return False, f"memory pressure {level}"

    if free_bytes is None:
        # Fail open: no telemetry behaves like the ungated build.
        return True, "no memory snapshot"

    if free_bytes < floor_bytes:
        return (
            False,
            f"free {free_bytes} below speculation floor {floor_bytes}",
        )
    return True, "headroom available"
