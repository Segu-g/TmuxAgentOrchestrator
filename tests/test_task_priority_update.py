"""Tests for dynamic task priority update — v1.2.6.

Feature: AsyncPriorityTaskQueue.update_priority() + PATCH /tasks/{id}/priority

Design references:
- Python heapq docs "Priority Queue Implementation Notes"
  https://docs.python.org/3/library/heapq.html
- Postman Blog "HTTP PATCH Method: Partial Updates for RESTful APIs"
  https://blog.postman.com/http-patch-method/
- Liu & Layland (1973) "Scheduling Algorithms for Multiprogramming in a Hard
  Real-Time Environment", JACM 20(1)
- DESIGN.md §10.82 — v1.2.6 Dynamic Task Priority Update
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tmux_orchestrator.application.task_queue import AsyncPriorityTaskQueue
from tmux_orchestrator.domain.task import Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task(task_id: str, priority: int = 0, prompt: str = "test") -> Task:
    return Task(id=task_id, prompt=prompt, priority=priority)


def make_config(**kwargs):
    from tmux_orchestrator.config import OrchestratorConfig
    return OrchestratorConfig(**kwargs)


def make_tmux_mock():
    tmux = MagicMock()
    tmux.new_window.return_value = MagicMock()
    tmux.new_subpane.return_value = MagicMock()
    return tmux


_API_KEY = "test-priority-key"


def make_app():
    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.web.ws import WebSocketHub

    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(api_key=_API_KEY, task_queue_maxsize=100)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    hub = WebSocketHub(bus=bus)
    app = create_app(orchestrator=orch, hub=hub, api_key=_API_KEY)
    return app, orch


# ---------------------------------------------------------------------------
# 1. AsyncPriorityTaskQueue.update_priority — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_priority_returns_true_when_found() -> None:
    """update_priority returns True when the task is pending in the queue."""
    q = AsyncPriorityTaskQueue()
    t = make_task("t1", priority=10)
    await q.put((10, 0, t))

    result = q.update_priority("t1", 1)
    assert result is True


@pytest.mark.asyncio
async def test_update_priority_returns_false_when_not_found() -> None:
    """update_priority returns False for task IDs not in the queue."""
    q = AsyncPriorityTaskQueue()
    result = q.update_priority("nonexistent", 0)
    assert result is False


@pytest.mark.asyncio
async def test_update_priority_lower_value_dequeued_first() -> None:
    """After lowering a task's priority value, it is dequeued before higher-value tasks."""
    q = AsyncPriorityTaskQueue()
    t_a = make_task("t-a", priority=10, prompt="A")
    t_b = make_task("t-b", priority=5, prompt="B")
    t_c = make_task("t-c", priority=1, prompt="C")

    # Insert in arbitrary order
    await q.put((10, 0, t_a))
    await q.put((5, 1, t_b))
    await q.put((1, 2, t_c))

    # Promote t_a to priority=0 — should now be dispatched first
    q.update_priority("t-a", 0)

    first = await q.get()
    assert first[2].id == "t-a", f"Expected t-a first, got {first[2].id}"
    assert first[0] == 0


@pytest.mark.asyncio
async def test_update_priority_higher_value_dequeued_after() -> None:
    """After raising a task's priority value, it is dequeued after lower-value tasks."""
    q = AsyncPriorityTaskQueue()
    t_a = make_task("t-a", priority=1)
    t_b = make_task("t-b", priority=5)

    await q.put((1, 0, t_a))
    await q.put((5, 1, t_b))

    # Demote t_a to priority=100 — t_b should now come first
    q.update_priority("t-a", 100)

    first = await q.get()
    assert first[2].id == "t-b", f"Expected t-b first, got {first[2].id}"


@pytest.mark.asyncio
async def test_update_priority_already_dispatched_returns_false() -> None:
    """update_priority returns False after the task has been dequeued (dispatched)."""
    q = AsyncPriorityTaskQueue()
    t = make_task("t1", priority=5)
    await q.put((5, 0, t))

    # Dequeue (simulate dispatch)
    await q.get()

    # Now the task is no longer in _pending
    result = q.update_priority("t1", 0)
    assert result is False


@pytest.mark.asyncio
async def test_update_priority_multiple_updates_latest_wins() -> None:
    """Multiple successive updates: only the latest priority value is effective."""
    q = AsyncPriorityTaskQueue()
    t = make_task("t1", priority=10)
    await q.put((10, 0, t))

    q.update_priority("t1", 5)
    q.update_priority("t1", 3)
    q.update_priority("t1", 0)  # latest

    item = await q.get()
    assert item[0] == 0
    assert item[2].priority == 0


@pytest.mark.asyncio
async def test_update_priority_pending_dict_stays_consistent() -> None:
    """_pending always reflects the latest (priority, seq, task) after update."""
    q = AsyncPriorityTaskQueue()
    t = make_task("t1", priority=10)
    await q.put((10, 0, t))

    q.update_priority("t1", 3)

    assert "t1" in q._pending
    assert q._pending["t1"][0] == 3
    assert q._pending["t1"][2].priority == 3
    # Old seq should be in _deleted_seqs
    assert len(q._deleted_seqs) == 1


@pytest.mark.asyncio
async def test_update_priority_no_duplicate_dequeues() -> None:
    """Lazy deletion must not yield the same task twice."""
    q = AsyncPriorityTaskQueue()
    t = make_task("t1", priority=10)
    await q.put((10, 0, t))

    q.update_priority("t1", 2)

    # We should get exactly one item
    item = await q.get()
    assert item[2].id == "t1"
    assert item[0] == 2

    # Queue should now be empty (no stale duplicate)
    assert q.empty()
    assert q.qsize() == 0


@pytest.mark.asyncio
async def test_update_priority_qsize_correct_after_update() -> None:
    """qsize() returns the number of live tasks, not counting phantom deleted entries."""
    q = AsyncPriorityTaskQueue()
    t_a = make_task("t-a", priority=10)
    t_b = make_task("t-b", priority=5)
    await q.put((10, 0, t_a))
    await q.put((5, 1, t_b))

    assert q.qsize() == 2

    q.update_priority("t-a", 0)

    # Still 2 live tasks — the old heap entry is stale but _pending has exactly 2 entries
    assert q.qsize() == 2


# ---------------------------------------------------------------------------
# 9. Orchestrator.update_task_priority delegation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_update_task_priority_delegates_to_queue() -> None:
    """Orchestrator.update_task_priority delegates to the queue's update_priority method."""
    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.orchestrator import Orchestrator

    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(task_queue_maxsize=100)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    t = make_task("task-orch", priority=10)
    await orch._task_queue.put((10, 0, t))

    result = await orch.update_task_priority("task-orch", 1)
    assert result is True

    tasks = orch.list_tasks()
    entry = next((x for x in tasks if x["task_id"] == "task-orch"), None)
    assert entry is not None
    assert entry["priority"] == 1


# ---------------------------------------------------------------------------
# 10–12. REST endpoint tests via HTTPX
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_priority_endpoint_returns_200() -> None:
    """PATCH /tasks/{id}/priority returns 200 with correct JSON on success."""
    app, orch = make_app()

    t = make_task("task-rest-ok", priority=10)
    await orch._task_queue.put((10, 0, t))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch(
            "/tasks/task-rest-ok/priority",
            json={"priority": 1},
            headers={"X-Api-Key": _API_KEY},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == "task-rest-ok"
    assert body["priority"] == 1
    assert body["updated"] is True


@pytest.mark.asyncio
async def test_patch_priority_endpoint_returns_404_when_not_found() -> None:
    """PATCH /tasks/{id}/priority returns 404 when the task is not pending."""
    app, orch = make_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch(
            "/tasks/nonexistent-task/priority",
            json={"priority": 0},
            headers={"X-Api-Key": _API_KEY},
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_priority_endpoint_dispatch_order() -> None:
    """After priority update, list_tasks returns tasks in updated priority order."""
    app, orch = make_app()

    t_a = make_task("task-low", priority=10)
    t_b = make_task("task-mid", priority=5)
    t_c = make_task("task-high", priority=1)

    await orch._task_queue.put((10, 0, t_a))
    await orch._task_queue.put((5, 1, t_b))
    await orch._task_queue.put((1, 2, t_c))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Elevate task-low (priority 10) to priority 0
        resp = await client.patch(
            "/tasks/task-low/priority",
            json={"priority": 0},
            headers={"X-Api-Key": _API_KEY},
        )
        assert resp.status_code == 200

        # Now check the task list ordering
        tasks_resp = await client.get("/tasks", headers={"X-Api-Key": _API_KEY})
        assert tasks_resp.status_code == 200
        tasks = tasks_resp.json()

    queued = [t for t in tasks if t["status"] == "queued"]
    assert len(queued) == 3
    priorities = [t["priority"] for t in queued]
    # task-low should now have priority=0, appearing first
    assert priorities[0] == 0
    assert queued[0]["task_id"] == "task-low"
