"""Tests for supervised_task() — restart on failure, cancel propagation."""
from __future__ import annotations

import asyncio

import pytest

from tmux_orchestrator.supervision import supervised_task


async def test_supervised_task_clean_exit():
    """A coroutine that returns normally completes without restart."""
    calls: list[int] = []

    async def factory():
        calls.append(1)

    await supervised_task(factory, "clean")
    assert calls == [1]


async def test_supervised_task_restarts_on_exception():
    """A failing coroutine is restarted up to max_restarts times."""
    calls: list[int] = []

    async def factory():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("transient error")

    # Will succeed on 3rd attempt (max_restarts=5 is plenty)
    await supervised_task(factory, "transient", max_restarts=5)
    assert len(calls) == 3


async def test_supervised_task_raises_after_max_restarts():
    """After max_restarts exhausted, the exception propagates."""
    calls: list[int] = []

    async def factory():
        calls.append(1)
        raise RuntimeError("permanent")

    with pytest.raises(RuntimeError, match="permanent"):
        await supervised_task(factory, "permanent", max_restarts=2)
    assert len(calls) == 3  # initial + 2 restarts


async def test_supervised_task_on_permanent_failure_called():
    """on_permanent_failure callback is invoked before re-raising."""
    failures: list[tuple[str, Exception]] = []

    async def on_failure(name, exc):
        failures.append((name, exc))

    async def factory():
        raise OSError("disk")

    with pytest.raises(OSError):
        await supervised_task(
            factory, "disk-task", max_restarts=1, on_permanent_failure=on_failure
        )
    assert len(failures) == 1
    assert failures[0][0] == "disk-task"
    assert isinstance(failures[0][1], OSError)


async def test_supervised_task_cancel_propagates():
    """CancelledError is never caught — it propagates immediately."""
    calls: list[int] = []

    async def factory():
        calls.append(1)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await supervised_task(factory, "cancel", max_restarts=5)
    assert len(calls) == 1  # not restarted


async def test_supervised_task_external_cancel():
    """External cancellation of the supervised task propagates correctly."""
    started = asyncio.Event()

    async def factory():
        started.set()
        await asyncio.sleep(9999)

    task = asyncio.create_task(supervised_task(factory, "ext-cancel", max_restarts=5))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
