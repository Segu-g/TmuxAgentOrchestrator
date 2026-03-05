"""Tests for task cancellation — POST /tasks/{id}/cancel.

Design reference:
- DESIGN.md §11 — task cancellation
- Microsoft Azure "Asynchronous Request-Reply pattern":
  https://learn.microsoft.com/en-us/azure/architecture/patterns/async-request-reply
  "A client can send an HTTP DELETE request on the URL provided by Location header."
- REST best practice: cancel is a verb action on a resource sub-path;
  returns 200 with {cancelled: true, status: "pending"|"already_dispatched"}.

Semantics:
- POST /tasks/{id}/cancel cancels a task that is still PENDING in the queue.
- If the task is not in the queue (not found), returns 404.
- If the task was already dispatched (not in queue), returns 200 with
  {cancelled: false, status: "already_dispatched"}.
- Orchestrator.cancel_task(task_id) is the core method.
  Returns True if task was removed from the queue, False if not found in queue.
- Cancelled tasks are NOT moved to DLQ — they are discarded.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

import tmux_orchestrator.web.app as web_app_mod
from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(**kwargs) -> OrchestratorConfig:
    defaults = dict(
        session_name="test",
        agents=[],
        p2p_permissions=[],
        task_timeout=10,
        watchdog_poll=9999.0,
        recovery_poll=9999.0,
    )
    defaults.update(kwargs)
    return OrchestratorConfig(**defaults)


class SlowDummyAgent(Agent):
    """An agent that takes a while to process tasks, so we can cancel before dispatch."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self.started_event = asyncio.Event()

    async def start(self) -> None:
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()

    async def _dispatch_task(self, task: Task) -> None:
        self.started_event.set()
        # Hold BUSY for a long time so we can test cancellation of queued tasks
        await asyncio.sleep(100)

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


class InstantDummyAgent(Agent):
    """An agent that completes tasks immediately."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self.dispatched: list[Task] = []

    async def start(self) -> None:
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()

    async def _dispatch_task(self, task: Task) -> None:
        self.dispatched.append(task)
        await self.bus.publish(Message(
            type=MessageType.RESULT,
            from_id=self.id,
            payload={"task_id": task.id, "output": "done"},
        ))
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


def make_tmux_mock():
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    return tmux


# ---------------------------------------------------------------------------
# Orchestrator-level tests
# ---------------------------------------------------------------------------


async def test_cancel_pending_task_removes_from_queue() -> None:
    """cancel_task returns True and removes the task from the queue."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    # No agents — tasks will stay in queue
    await orch.start()
    try:
        task = await orch.submit_task("pending task")
        # Task should be in the queue
        assert any(t["task_id"] == task.id for t in orch.list_tasks())

        result = await orch.cancel_task(task.id)
        assert result is True
        # Task should no longer be in the queue
        assert not any(t["task_id"] == task.id for t in orch.list_tasks())
    finally:
        await orch.stop()


async def test_cancel_unknown_task_returns_false() -> None:
    """cancel_task returns False for a task ID not in the queue."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    await orch.start()
    try:
        result = await orch.cancel_task("nonexistent-task-id")
        assert result is False
    finally:
        await orch.stop()


async def test_cancel_dispatched_task_returns_false() -> None:
    """cancel_task returns False for a task that was already dispatched."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = SlowDummyAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("slow task")
        # Wait for the agent to start processing
        await asyncio.wait_for(agent.started_event.wait(), timeout=2.0)
        # Task is now dispatched (no longer in queue)
        result = await orch.cancel_task(task.id)
        assert result is False
    finally:
        await orch.stop()


async def test_cancel_publishes_status_event() -> None:
    """cancel_task publishes a task_cancelled STATUS event."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    events_q = await bus.subscribe("test-events", broadcast=True)

    await orch.start()
    try:
        task = await orch.submit_task("task to cancel")
        # Wait briefly for queue processing
        await asyncio.sleep(0.05)
        await orch.cancel_task(task.id)

        # Drain the event queue looking for task_cancelled
        found_cancelled = False
        deadline = asyncio.get_event_loop().time() + 1.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                msg = events_q.get_nowait()
                if (
                    msg.type == MessageType.STATUS
                    and msg.payload.get("event") == "task_cancelled"
                    and msg.payload.get("task_id") == task.id
                ):
                    found_cancelled = True
                    break
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.02)
        assert found_cancelled, "Expected task_cancelled STATUS event"
    finally:
        await bus.unsubscribe("test-events")
        await orch.stop()


async def test_cancel_multiple_tasks_selectively() -> None:
    """Cancelling one task does not affect other pending tasks."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    await orch.start()
    try:
        task1 = await orch.submit_task("task one")
        task2 = await orch.submit_task("task two")
        task3 = await orch.submit_task("task three")

        # Cancel the middle task
        result = await orch.cancel_task(task2.id)
        assert result is True

        remaining = [t["task_id"] for t in orch.list_tasks()]
        assert task1.id in remaining
        assert task2.id not in remaining
        assert task3.id in remaining
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# REST endpoint tests
# ---------------------------------------------------------------------------


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def handle(self, ws) -> None:
        pass


class _MockOrchestratorForCancel:
    """Minimal mock orchestrator for REST endpoint tests."""

    def __init__(self) -> None:
        self._pending: dict[str, str] = {}  # task_id -> prompt
        # Simulate in-flight tasks (dispatched, awaiting RESULT)
        self._task_started_at: dict[str, float] = {}
        self._completed_tasks: set[str] = set()
        self._director_pending: list = []
        self._dispatch_task = None

    def list_agents(self) -> list:
        return []

    def list_tasks(self) -> list:
        return [
            {"task_id": tid, "prompt": p, "priority": 0}
            for tid, p in self._pending.items()
        ]

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
        return False

    async def cancel_task(self, task_id: str) -> bool:
        if task_id in self._pending:
            del self._pending[task_id]
            return True
        return False


@pytest.fixture(autouse=True)
def reset_state():
    web_app_mod._credentials.clear()
    web_app_mod._sign_counts.clear()
    web_app_mod._sessions.clear()
    web_app_mod._pending_challenge = None
    yield


@pytest.fixture
def mock_orch():
    orch = _MockOrchestratorForCancel()
    orch._pending["task-001"] = "pending task"
    # task-002 is in-flight (dispatched but no RESULT yet)
    orch._task_started_at["task-002"] = 12345.0
    return orch


@pytest.fixture
def app(mock_orch):
    return create_app(mock_orch, _MockHub(), api_key="test-key")


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        yield c


async def test_rest_cancel_pending_task(client):
    """POST /tasks/{id}/cancel removes a pending task and returns 200."""
    r = await client.post(
        "/tasks/task-001/cancel",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["cancelled"] is True
    assert body["task_id"] == "task-001"
    assert body["status"] == "cancelled"


async def test_rest_cancel_already_dispatched(client):
    """POST /tasks/{id}/cancel on a dispatched task returns 200 with cancelled=false."""
    r = await client.post(
        "/tasks/task-002/cancel",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["cancelled"] is False
    assert body["task_id"] == "task-002"
    assert body["status"] == "already_dispatched"


async def test_rest_cancel_unknown_task(client):
    """POST /tasks/{id}/cancel on a nonexistent task returns 404."""
    r = await client.post(
        "/tasks/nonexistent/cancel",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 404


async def test_rest_cancel_requires_auth(client):
    """POST /tasks/{id}/cancel requires authentication."""
    r = await client.post("/tasks/task-001/cancel")
    assert r.status_code == 401
