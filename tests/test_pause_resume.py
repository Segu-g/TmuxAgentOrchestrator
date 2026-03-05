"""Tests for orchestrator queue pause/resume REST endpoints and task priority update.

Feature: POST /orchestrator/pause, POST /orchestrator/resume,
         GET /orchestrator/status, PATCH /tasks/{task_id}

Design reference:
- Google Cloud Tasks queues.pause API
- Oracle WebLogic Server "Pause queue message operations at runtime"
- Python heapq priority queue implementation notes
- Liu & Layland (1973) "Scheduling Algorithms for Multiprogramming in a Hard
  Real-Time Environment", JACM 20(1)
- DESIGN.md §11 (v0.19.0)
"""
from __future__ import annotations

import asyncio
import heapq
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tmux_orchestrator.agents.base import AgentStatus
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app


# ---------------------------------------------------------------------------
# Helpers shared with other test modules
# ---------------------------------------------------------------------------


def make_tmux_mock():
    tmux = MagicMock()
    tmux.new_window.return_value = MagicMock()
    tmux.new_subpane.return_value = MagicMock()
    return tmux


def make_config(**kwargs):
    from tmux_orchestrator.config import OrchestratorConfig
    return OrchestratorConfig(**kwargs)


class DummyAgent:
    """Minimal agent stub that records dispatched tasks and exposes status."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        self.id = agent_id
        self.bus = bus
        self.status = AgentStatus.IDLE
        self.role = "worker"
        self.tags: list[str] = []
        self._task_queue: asyncio.Queue = asyncio.Queue()
        self.dispatched: list = []
        self.dispatched_event: asyncio.Event = asyncio.Event()
        self._run_task: asyncio.Task | None = None
        self._current_task = None
        self.worktree_path = None
        self.pane = None
        self._busy_since: float | None = None
        self.task_timeout: int | None = None

    async def start(self) -> None:
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._run_task:
            self._run_task.cancel()
            await asyncio.gather(self._run_task, return_exceptions=True)
        self.status = AgentStatus.IDLE

    async def send_task(self, task) -> None:
        await self._task_queue.put(task)

    async def notify_stdin(self, text: str) -> None:
        pass

    async def _run_loop(self) -> None:
        while True:
            try:
                task = await self._task_queue.get()
                self.status = AgentStatus.BUSY
                self._current_task = task
                self.dispatched.append(task)
                self.dispatched_event.set()
                await asyncio.sleep(0.05)
                self.status = AgentStatus.IDLE
                self._current_task = None
                from tmux_orchestrator.bus import Message, MessageType
                await self.bus.publish(Message(
                    type=MessageType.RESULT,
                    from_id=self.id,
                    payload={"task_id": task.id, "output": "done", "error": None},
                ))
            except asyncio.CancelledError:
                break


# ---------------------------------------------------------------------------
# Unit tests: Orchestrator.pause / resume / is_paused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_stops_dispatch() -> None:
    """Tasks submitted while paused are NOT dispatched until resume."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()

    try:
        orch.pause()
        assert orch.is_paused
        await orch.submit_task("queued while paused")
        await asyncio.sleep(0.3)  # dispatch loop runs but stays paused
        assert len(agent.dispatched) == 0
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_resume_drains_queue() -> None:
    """Resuming after pause dispatches the queued tasks."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()

    try:
        orch.pause()
        await orch.submit_task("task while paused")
        await asyncio.sleep(0.2)
        assert len(agent.dispatched) == 0

        orch.resume()
        assert not orch.is_paused
        await asyncio.wait_for(agent.dispatched_event.wait(), timeout=2.0)
        assert len(agent.dispatched) == 1
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_pause_idempotent() -> None:
    """Pausing an already-paused orchestrator is safe."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    await orch.start()
    try:
        orch.pause()
        orch.pause()  # second call should not raise
        assert orch.is_paused
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_resume_idempotent() -> None:
    """Resuming a non-paused orchestrator is safe."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    await orch.start()
    try:
        assert not orch.is_paused
        orch.resume()  # should not raise
        assert not orch.is_paused
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# Unit tests: Orchestrator.update_task_priority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_task_priority_found() -> None:
    """update_task_priority returns True and mutates the task in the queue."""
    bus = Bus()
    tmux = make_tmux_mock()
    # Use a large queue so tasks are not dispatched instantly
    config = make_config(task_queue_maxsize=100)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    # Do NOT start the orchestrator — we want the tasks to sit in the queue.
    # Submit tasks directly (no dispatch loop running).
    orch._task_seq = 0

    from tmux_orchestrator.agents.base import Task
    t1 = Task(id="task-1", prompt="low priority", priority=5)
    t2 = Task(id="task-2", prompt="normal priority", priority=3)
    await orch._task_queue.put((5, 1, t1))
    await orch._task_queue.put((3, 2, t2))

    result = await orch.update_task_priority("task-1", new_priority=1)
    assert result is True

    # Verify priority is reflected in list_tasks()
    tasks = orch.list_tasks()
    task1_entry = next(t for t in tasks if t["task_id"] == "task-1")
    assert task1_entry["priority"] == 1


@pytest.mark.asyncio
async def test_update_task_priority_not_found() -> None:
    """update_task_priority returns False for unknown task IDs."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    result = await orch.update_task_priority("nonexistent-task", new_priority=0)
    assert result is False


@pytest.mark.asyncio
async def test_update_task_priority_heap_invariant() -> None:
    """After priority update, the heap is valid and highest-priority task is first."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(task_queue_maxsize=100)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    from tmux_orchestrator.agents.base import Task
    # Enqueue three tasks with priorities 5, 3, 7
    tasks_data = [("t1", "first", 5), ("t2", "second", 3), ("t3", "third", 7)]
    for idx, (tid, prompt, prio) in enumerate(tasks_data, start=1):
        t = Task(id=tid, prompt=prompt, priority=prio)
        await orch._task_queue.put((prio, idx, t))

    # Promote t3 (priority 7) to priority 0 — it should now be dispatched first
    updated = await orch.update_task_priority("t3", new_priority=0)
    assert updated is True

    tasks = orch.list_tasks()
    # Sorted by priority; t3 should be first
    assert tasks[0]["task_id"] == "t3"
    assert tasks[0]["priority"] == 0


@pytest.mark.asyncio
async def test_update_task_priority_publishes_event() -> None:
    """update_task_priority publishes a task_priority_updated STATUS event."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(task_queue_maxsize=100)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    from tmux_orchestrator.bus import MessageType
    events = []
    q = await bus.subscribe("test-listener", broadcast=True)

    async def collect():
        while True:
            msg = await q.get()
            if msg.type == MessageType.STATUS:
                events.append(msg.payload)
            q.task_done()

    collector = asyncio.create_task(collect())

    from tmux_orchestrator.agents.base import Task
    t = Task(id="task-ev", prompt="p", priority=10)
    await orch._task_queue.put((10, 1, t))

    await orch.update_task_priority("task-ev", new_priority=2)
    await asyncio.sleep(0.05)
    collector.cancel()
    await asyncio.gather(collector, return_exceptions=True)
    await bus.unsubscribe("test-listener")

    priority_events = [e for e in events if e.get("event") == "task_priority_updated"]
    assert len(priority_events) == 1
    assert priority_events[0]["task_id"] == "task-ev"
    assert priority_events[0]["priority"] == 2


# ---------------------------------------------------------------------------
# Web endpoint tests via httpx
# ---------------------------------------------------------------------------

_API_KEY = "test-key-xyz"


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def handle(self, ws) -> None:
        pass


class _MockOrchestrator:
    _agents: dict = {}
    _director_pending: list = []
    _dispatch_task = None
    _paused: bool = False
    _task_started_at: dict = {}
    _completed_tasks: set = set()

    def list_agents(self) -> list:
        return []

    def list_tasks(self) -> list:
        return []

    def get_agent(self, agent_id: str):
        return None

    def get_director(self):
        return None

    def flush_director_pending(self) -> list:
        return []

    def list_dlq(self) -> list:
        return []

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    async def update_task_priority(self, task_id: str, new_priority: int) -> bool:
        return task_id == "known-task"

    async def cancel_task(self, task_id: str) -> bool:
        return False


@pytest.fixture
def app():
    return create_app(_MockOrchestrator(), _MockHub(), api_key=_API_KEY)


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"X-API-Key": _API_KEY},
    ) as c:
        yield c


async def test_pause_endpoint(client) -> None:
    r = await client.post("/orchestrator/pause")
    assert r.status_code == 200
    assert r.json() == {"paused": True}


async def test_resume_endpoint(client) -> None:
    r = await client.post("/orchestrator/resume")
    assert r.status_code == 200
    assert r.json() == {"paused": False}


async def test_orchestrator_status_endpoint(client) -> None:
    r = await client.get("/orchestrator/status")
    assert r.status_code == 200
    data = r.json()
    assert "paused" in data
    assert "queue_depth" in data
    assert "agent_count" in data
    assert "dlq_depth" in data


async def test_orchestrator_status_reflects_pause(client, app) -> None:
    """GET /orchestrator/status shows paused=true after POST /orchestrator/pause."""
    await client.post("/orchestrator/pause")
    r = await client.get("/orchestrator/status")
    assert r.json()["paused"] is True
    await client.post("/orchestrator/resume")
    r = await client.get("/orchestrator/status")
    assert r.json()["paused"] is False


async def test_patch_task_priority_found(client) -> None:
    r = await client.patch("/tasks/known-task", json={"priority": 1})
    assert r.status_code == 200
    assert r.json()["updated"] is True
    assert r.json()["priority"] == 1


async def test_patch_task_priority_not_found(client) -> None:
    r = await client.patch("/tasks/unknown-task", json={"priority": 1})
    assert r.status_code == 200
    assert r.json()["updated"] is False


async def test_pause_endpoint_requires_auth(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        r = await c.post("/orchestrator/pause")
        assert r.status_code == 401


async def test_resume_endpoint_requires_auth(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        r = await c.post("/orchestrator/resume")
        assert r.status_code == 401


async def test_orchestrator_status_requires_auth(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        r = await c.get("/orchestrator/status")
        assert r.status_code == 401


async def test_patch_task_requires_auth(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        r = await c.patch("/tasks/some-task", json={"priority": 0})
        assert r.status_code == 401
