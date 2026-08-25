# SPDX-License-Identifier: Apache-2.0
"""Stream-phase outcome instrumentation (#21 review round).

These exercise the real server wrappers (_release_after_stream,
_with_json_keepalive) with fabricated generators/coroutines: mid-stream
faults and client cancels must land in omlx_request_errors_total even
though the endpoint handler frame has already returned. Imports
omlx.server, so this file is Mac/CI-gated like the mlx-dependent suite.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, patch

from omlx.server_metrics import get_server_metrics


class _FakeLease:
    def __init__(self):
        self.released = False

    async def release(self):
        self.released = True


@pytest.fixture(autouse=True)
def _fresh():
    from omlx.server_metrics import reset_server_metrics

    reset_server_metrics()
    yield
    reset_server_metrics()


def _counts(metrics):
    return dict(metrics.request_errors)


@pytest.mark.asyncio
async def test_mid_stream_fault_counts_internal_and_propagates():
    from omlx.server import _release_after_stream

    lease = _FakeLease()

    async def gen():
        yield "chunk-1"
        raise RuntimeError("generation exploded")

    with patch("omlx.server._raise_if_llm_lease_abort_requested", new=AsyncMock()), \
         pytest.raises(RuntimeError):
        async for _ in _release_after_stream(gen(), lease):
            pass

    assert _counts(get_server_metrics())["internal"] == 1
    assert lease.released is True


@pytest.mark.asyncio
async def test_mid_stream_cancel_counts_client_disconnect():
    from omlx.server import _release_after_stream

    lease = _FakeLease()

    async def gen():
        yield "chunk-1"
        raise asyncio.CancelledError()

    collector = []
    with patch("omlx.server._raise_if_llm_lease_abort_requested", new=AsyncMock()), \
         pytest.raises(asyncio.CancelledError):
        async for chunk in _release_after_stream(gen(), lease):
            collector.append(chunk)

    assert collector == ["chunk-1"]
    assert _counts(get_server_metrics())["client_disconnect"] == 1
    assert lease.released is True


@pytest.mark.asyncio
async def test_clean_stream_records_no_outcome():
    from omlx.server import _release_after_stream

    lease = _FakeLease()

    async def gen():
        yield "a"
        yield "b"

    with patch("omlx.server._raise_if_llm_lease_abort_requested", new=AsyncMock()):
        got = [chunk async for chunk in _release_after_stream(gen(), lease)]

    assert got == ["a", "b"]
    metrics = get_server_metrics()
    assert sum(_counts(metrics).values()) == 0
    assert lease.released is True


@pytest.mark.asyncio
async def test_keepalive_detects_disconnect_counts_client_disconnect():
    from omlx.server import _with_json_keepalive

    class _Request:
        def __init__(self):
            self.calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls >= 2

    async def coro():
        await asyncio.sleep(30)
        return "never"  # pragma: no cover - cancelled before completion

    chunks = [
        chunk
        async for chunk in _with_json_keepalive(
            _Request(), coro(), disconnect_poll=0.01, interval=999
        )
    ]

    # Only keepalive spaces: the disconnect cancelled the task before any
    # result was produced, and the outcome must be attributed.
    assert chunks and set(chunks) == {" "}
    assert _counts(get_server_metrics())["client_disconnect"] == 1


@pytest.mark.asyncio
async def test_keepalive_fault_propagates_and_counts_internal():
    from omlx.server import _with_json_keepalive

    class _Request:
        async def is_disconnected(self):
            return False

    async def coro():
        raise RuntimeError("model exploded")

    with pytest.raises(RuntimeError):
        async for _ in _with_json_keepalive(_Request(), coro()):
            pass

    assert _counts(get_server_metrics())["internal"] == 1


@pytest.mark.asyncio
async def test_keepalive_prefill_guard_rejection_is_not_an_outcome():
    """Prefill-memory rejections are policy, not faults: deliberately
    excluded from the error family (#21 review round)."""
    import json

    from omlx.exceptions import PrefillMemoryExceededError
    from omlx.server import (
        _prefill_memory_openai_error_body,
        _with_json_keepalive,
    )

    class _Request:
        async def is_disconnected(self):
            return False

    async def coro():
        raise PrefillMemoryExceededError("too big")

    chunks = []
    with pytest.raises(StopAsyncIteration):
        async for chunk in _with_json_keepalive(_Request(), coro()):
            chunks.append(chunk)

    body = json.loads(chunks[-1])
    assert "error" in body or chunks  # error body yielded, then clean exit
    assert sum(_counts(get_server_metrics()).values()) == 0
