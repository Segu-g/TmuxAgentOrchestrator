"""Tests for Task TTL (Time-to-Live / expiry) — v0.33.0.

Covers:
- Task.ttl / submitted_at / expires_at field population
- default_task_ttl applied from OrchestratorConfig
- TTL expiry in _dispatch_loop (queued tasks)
- TTL expiry via _ttl_reaper_loop (waiting tasks)
- Cascade dependency failure on expiry
- No expiry for tasks dispatched before TTL elapses
- Already-cancelled tasks not double-expired
- REST API: expires_at in GET /tasks, GET /tasks/{id}
- REST API: POST /tasks with ttl
- REST API: POST /tasks/batch with per-task TTL
- REST API: POST /workflows with per-task TTL
- ttl_reaper_loop starts with orchestrator, stops on shutdown
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app


# ---------------------------------------------------------------------------
# Helpers (reuse DummyAgent pattern from test_orchestrator.py)
# ---------------------------------------------------------------------------


class DummyAgent(Agent):
    """Minimal agent that records dispatched tasks."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self.dispatched: list[Task] = []
        self.dispatched_event: asyncio.Event = asyncio.Event()
        self._slow = False  # when True, _dispatch_task never completes

    async def start(self) -> None:
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(
            self._run_loop(), name=f"{self.id}-loop"
        )

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()

    async def _dispatch_task(self, task: Task) -> None:
        self.dispatched.append(task)
        self.dispatched_event.set()
        if self._slow:
            # Simulate a slow task — hold BUSY, never complete
            await asyncio.sleep(60)
        else:
            await asyncio.sleep(0)
            self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


def make_config(**kwargs) -> OrchestratorConfig:
    defaults = dict(
        session_name="test",
        agents=[],
        p2p_permissions=[],
        task_timeout=10,
        watchdog_poll=9999,  # disable watchdog in tests
        recovery_poll=9999,  # disable recovery in tests
        ttl_reaper_poll=0.05,  # fast reaper for tests
    )
    defaults.update(kwargs)
    return OrchestratorConfig(**defaults)


def make_tmux_mock():
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    return tmux


# ---------------------------------------------------------------------------
# 1. Task dataclass TTL field population
# ---------------------------------------------------------------------------


async def test_task_ttl_none_no_expires_at():
    """A Task with ttl=None has expires_at=None."""
    task = Task(id="t1", prompt="hello", ttl=None)
    assert task.ttl is None
    assert task.expires_at is None


async def test_task_ttl_set_expires_at():
    """A Task with ttl=5 computes expires_at = submitted_at + 5."""
    before = time.time()
    task = Task(id="t1", prompt="hello", ttl=5.0, submitted_at=before, expires_at=before + 5.0)
    assert task.expires_at == pytest.approx(before + 5.0, abs=0.01)


async def test_task_submitted_at_defaults_to_now():
    """submitted_at defaults to time.time() at construction time."""
    before = time.time()
    task = Task(id="t1", prompt="hello")
    after = time.time()
    assert before <= task.submitted_at <= after


async def test_submit_task_sets_expires_at():
    """submit_task() sets expires_at = submitted_at + ttl when ttl is given."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    await orch.start()
    try:
        before = time.time()
        task = await orch.submit_task("do something", ttl=10.0)
        after = time.time()
        assert task.ttl == 10.0
        assert task.expires_at is not None
        assert before + 10.0 <= task.expires_at <= after + 10.0
    finally:
        await orch.stop()


async def test_submit_task_ttl_none_no_expires_at():
    """submit_task() with ttl=None produces expires_at=None."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    await orch.start()
    try:
        task = await orch.submit_task("do something", ttl=None)
        assert task.ttl is None
        assert task.expires_at is None
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# 2. default_task_ttl from OrchestratorConfig
# ---------------------------------------------------------------------------


async def test_default_task_ttl_applied():
    """default_task_ttl is applied to tasks that do not specify ttl."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(default_task_ttl=30.0)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    await orch.start()
    try:
        before = time.time()
        task = await orch.submit_task("inherit default ttl")
        assert task.ttl == 30.0
        assert task.expires_at is not None
        assert task.expires_at >= before + 30.0
    finally:
        await orch.stop()


async def test_per_task_ttl_overrides_default():
    """Explicit per-task ttl overrides default_task_ttl."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(default_task_ttl=30.0)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    await orch.start()
    try:
        task = await orch.submit_task("custom ttl", ttl=5.0)
        assert task.ttl == 5.0
    finally:
        await orch.stop()


async def test_default_task_ttl_none_means_no_expiry():
    """default_task_ttl=None means tasks without explicit TTL never expire."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(default_task_ttl=None)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    await orch.start()
    try:
        task = await orch.submit_task("no expiry")
        assert task.ttl is None
        assert task.expires_at is None
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# 3. TTL expiry in _dispatch_loop (queued tasks)
# ---------------------------------------------------------------------------


async def test_expired_task_not_dispatched():
    """A task with an already-elapsed TTL is expired, not dispatched.

    Strategy: keep the agent BUSY with a slow task, then submit a task with
    a tiny TTL.  The dispatch loop will dequeue it while the agent is BUSY and
    re-queue it.  By the time the agent finishes the blocker, the TTL has
    elapsed and the task is expired instead of dispatched.
    """
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus)
    agent._slow = True  # keep agent BUSY so TTL task must wait in queue
    orch.register_agent(agent)

    status_sub = await bus.subscribe("test-not-dispatched", broadcast=True)

    await orch.start()
    try:
        # Fill the agent
        blocker = await orch.submit_task("blocker")
        await asyncio.wait_for(agent.dispatched_event.wait(), timeout=2.0)
        agent.dispatched_event.clear()

        # Submit task with TTL so short it will expire while agent is BUSY
        task = await orch.submit_task("expired task", ttl=0.05)

        # Wait past TTL so the task expires
        await asyncio.sleep(0.3)

        # The TTL task should NOT have been dispatched (only the blocker was)
        dispatched_ids = [t.id for t in agent.dispatched]
        assert task.id not in dispatched_ids

        # The task should now be in _failed_tasks
        assert task.id in orch._failed_tasks
    finally:
        await orch.stop()
        await bus.unsubscribe("test-not-dispatched")


async def test_task_expired_event_published():
    """task_expired STATUS event is published when a queued task expires."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus)
    agent._slow = True  # keep agent BUSY so task queues
    orch.register_agent(agent)

    status_sub = await bus.subscribe("test-events", broadcast=True)

    await orch.start()
    try:
        # Fill the agent first
        blocker = await orch.submit_task("blocker")
        await asyncio.wait_for(agent.dispatched_event.wait(), timeout=2.0)

        # Now submit a task with a tiny TTL
        expired_task = await orch.submit_task("will expire soon", ttl=0.05)
        await asyncio.sleep(0.3)  # wait for dispatch loop to see it

        # Drain events
        task_expired_found = False
        while not status_sub.empty():
            msg = status_sub.get_nowait()
            if (msg.payload.get("event") == "task_expired"
                    and msg.payload.get("task_id") == expired_task.id):
                task_expired_found = True
                break

        assert task_expired_found or expired_task.id in orch._failed_tasks
    finally:
        await orch.stop()
        await bus.unsubscribe("test-events")


# ---------------------------------------------------------------------------
# 4. TTL reaper (waiting tasks)
# ---------------------------------------------------------------------------


async def test_waiting_task_expired_by_reaper():
    """A task in _waiting_tasks with an elapsed TTL is expired by the reaper."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(ttl_reaper_poll=0.05)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    await orch.start()
    try:
        # Submit a phantom dependency that will never complete
        phantom_dep_id = "phantom-dep-00000000"

        # Submit a task that depends on the phantom, with a short TTL
        waiting_task = await orch.submit_task(
            "waiting for phantom",
            depends_on=[phantom_dep_id],
            ttl=0.1,
        )

        # Should be in _waiting_tasks initially
        assert waiting_task.id in orch._waiting_tasks

        # Wait for the reaper to pick it up
        await asyncio.sleep(0.5)

        # Should now be in _failed_tasks, not in _waiting_tasks
        assert waiting_task.id not in orch._waiting_tasks
        assert waiting_task.id in orch._failed_tasks
    finally:
        await orch.stop()


async def test_reaper_publishes_task_expired_event():
    """_ttl_reaper_loop publishes task_expired event with from_reaper=True."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(ttl_reaper_poll=0.05)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    status_sub = await bus.subscribe("test-reaper-events", broadcast=True)

    await orch.start()
    try:
        # Submit a task that waits for a phantom dep, with short TTL
        waiting_task = await orch.submit_task(
            "waiting with ttl",
            depends_on=["phantom-dep-reaper"],
            ttl=0.1,
        )

        # Wait for reaper
        await asyncio.sleep(0.5)

        # Find the task_expired event
        expired_found = False
        while not status_sub.empty():
            msg = status_sub.get_nowait()
            if (msg.payload.get("event") == "task_expired"
                    and msg.payload.get("task_id") == waiting_task.id):
                expired_found = True
                assert msg.payload.get("from_reaper") is True
                break

        assert expired_found
    finally:
        await orch.stop()
        await bus.unsubscribe("test-reaper-events")


# ---------------------------------------------------------------------------
# 5. Cascade dependency failure on expiry
# ---------------------------------------------------------------------------


async def test_expiry_cascades_to_dependents():
    """When task A expires, task B (depends_on A) gets dependency_failed."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(ttl_reaper_poll=0.05)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    status_sub = await bus.subscribe("test-cascade", broadcast=True)

    await orch.start()
    try:
        # A depends on phantom, will expire
        task_a = await orch.submit_task(
            "task A (will expire)",
            depends_on=["phantom-dep-cascade"],
            ttl=0.1,
        )
        # B depends on A
        task_b = await orch.submit_task(
            "task B (depends on A)",
            depends_on=[task_a.id],
        )

        # Both should be in _waiting_tasks initially
        assert task_a.id in orch._waiting_tasks
        assert task_b.id in orch._waiting_tasks

        # Wait for reaper to expire A, which cascades to B
        await asyncio.sleep(0.5)

        assert task_a.id in orch._failed_tasks
        assert task_b.id in orch._failed_tasks

        # Check that task_dependency_failed event was published for B
        dep_failed_found = False
        while not status_sub.empty():
            msg = status_sub.get_nowait()
            if (msg.payload.get("event") == "task_dependency_failed"
                    and msg.payload.get("task_id") == task_b.id
                    and msg.payload.get("failed_dep") == task_a.id):
                dep_failed_found = True
                break

        assert dep_failed_found
    finally:
        await orch.stop()
        await bus.unsubscribe("test-cascade")


# ---------------------------------------------------------------------------
# 6. Task dispatched before TTL elapses completes normally
# ---------------------------------------------------------------------------


async def test_task_with_ttl_completes_normally():
    """A task with a generous TTL that gets dispatched completes normally."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        # Submit with a long TTL — should dispatch before expiry
        task = await orch.submit_task("complete before expiry", ttl=30.0)
        await asyncio.wait_for(agent.dispatched_event.wait(), timeout=2.0)

        # Task was dispatched (not expired)
        assert any(t.id == task.id for t in agent.dispatched)
        # Not in failed tasks
        assert task.id not in orch._failed_tasks
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# 7. Already-cancelled tasks not double-expired
# ---------------------------------------------------------------------------


async def test_cancelled_task_not_expired_by_reaper():
    """A task in _cancelled_task_ids is skipped by the TTL reaper."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(ttl_reaper_poll=0.05)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    await orch.start()
    try:
        # Submit a waiting task with short TTL
        waiting_task = await orch.submit_task(
            "waiting with short ttl",
            depends_on=["phantom-dep-cancel"],
            ttl=0.1,
        )
        assert waiting_task.id in orch._waiting_tasks

        # Cancel it before it expires
        cancelled = await orch.cancel_task(waiting_task.id)
        assert cancelled
        assert waiting_task.id not in orch._waiting_tasks

        # Even after reaper runs, it should not be in _failed_tasks
        # (cancellation already removed it from _waiting_tasks)
        await asyncio.sleep(0.3)
        assert waiting_task.id not in orch._failed_tasks
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# 8. TTL reaper lifecycle (starts and stops with orchestrator)
# ---------------------------------------------------------------------------


async def test_ttl_reaper_starts_with_orchestrator():
    """_ttl_reaper_task is created when orchestrator starts."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    assert orch._ttl_reaper_task is None
    await orch.start()
    try:
        assert orch._ttl_reaper_task is not None
        assert not orch._ttl_reaper_task.done()
    finally:
        await orch.stop()


async def test_ttl_reaper_stops_on_shutdown():
    """_ttl_reaper_task is cancelled when orchestrator stops."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    await orch.start()
    reaper_task = orch._ttl_reaper_task
    await orch.stop()

    assert reaper_task is not None
    assert reaper_task.done()


# ---------------------------------------------------------------------------
# 9. list_tasks() includes expires_at
# ---------------------------------------------------------------------------


async def test_list_tasks_includes_expires_at():
    """list_tasks() includes expires_at field for TTL tasks."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus)
    agent._slow = True
    orch.register_agent(agent)

    await orch.start()
    try:
        # Fill the agent so the task stays queued
        blocker = await orch.submit_task("blocker")
        await asyncio.wait_for(agent.dispatched_event.wait(), timeout=2.0)

        # Submit a task with TTL
        task = await orch.submit_task("queued with ttl", ttl=60.0)
        await asyncio.sleep(0.1)

        tasks = orch.list_tasks()
        ttl_task = next((t for t in tasks if t["task_id"] == task.id), None)
        assert ttl_task is not None
        assert ttl_task["ttl"] == 60.0
        assert ttl_task["expires_at"] is not None
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# 10. REST API tests
# ---------------------------------------------------------------------------


class _MockOrchestrator:
    """Minimal mock orchestrator for FastAPI app creation in tests."""
    _dispatch_task = None
    _active_tasks: dict = {}
    _failed_tasks: set = set()
    _waiting_tasks: dict = {}
    _task_dependents: dict = {}

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
        return False

    def get_rate_limiter_status(self) -> dict:
        return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}

    def reconfigure_rate_limiter(self, *, rate: float, burst: int) -> dict:
        return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}

    def get_agent_context_stats(self, agent_id: str) -> dict | None:
        return None

    def all_agent_context_stats(self) -> list:
        return []

    def get_agent_history(self, agent_id: str, limit: int = 50) -> list | None:
        return None

    def get_workflow_manager(self):
        from tmux_orchestrator.workflow_manager import WorkflowManager
        return WorkflowManager()

    @property
    def _webhook_manager(self):
        from tmux_orchestrator.webhook_manager import WebhookManager
        return WebhookManager()

    def get_group_manager(self):
        from tmux_orchestrator.group_manager import GroupManager
        return GroupManager()

    def _task_blocking(self, task_id: str) -> list:
        return []

    def get_waiting_task(self, task_id: str):
        return None

    async def submit_task(self, prompt: str, **kwargs) -> Task:
        ttl = kwargs.get("ttl")
        submitted_at = time.time()
        expires_at = (submitted_at + ttl) if ttl is not None else None
        return Task(
            id="test-task-id",
            prompt=prompt,
            ttl=ttl,
            submitted_at=submitted_at,
            expires_at=expires_at,
        )


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def handle(self, ws) -> None:
        pass


async def test_rest_post_tasks_with_ttl():
    """POST /tasks with ttl returns expires_at in the response."""
    mock_orch = _MockOrchestrator()
    app = create_app(mock_orch, _MockHub(), api_key="testkey")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/tasks",
            json={"prompt": "hello", "ttl": 10.0},
            headers={"X-API-Key": "testkey"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ttl"] == 10.0
        assert data["expires_at"] is not None


async def test_rest_post_tasks_no_ttl():
    """POST /tasks without ttl returns expires_at=None."""
    mock_orch = _MockOrchestrator()
    app = create_app(mock_orch, _MockHub(), api_key="testkey")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/tasks",
            json={"prompt": "hello"},
            headers={"X-API-Key": "testkey"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ttl"] is None
        assert data["expires_at"] is None


async def test_rest_post_tasks_batch_with_ttl():
    """POST /tasks/batch with per-task ttl returns expires_at in each item."""
    mock_orch = _MockOrchestrator()
    app = create_app(mock_orch, _MockHub(), api_key="testkey")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/tasks/batch",
            json={"tasks": [
                {"prompt": "task 1", "ttl": 30.0},
                {"prompt": "task 2", "ttl": None},
            ]},
            headers={"X-API-Key": "testkey"},
        )
        assert resp.status_code == 200
        data = resp.json()
        tasks = data["tasks"]
        assert len(tasks) == 2
        assert tasks[0]["ttl"] == 30.0
        assert tasks[0]["expires_at"] is not None
        assert tasks[1]["ttl"] is None
        assert tasks[1]["expires_at"] is None


async def test_rest_get_tasks_includes_expires_at():
    """GET /tasks response per-task includes expires_at field."""
    mock_orch = _MockOrchestrator()

    # Override list_tasks to return a task with expires_at
    submitted_at = time.time()
    mock_orch.list_tasks = lambda: [{
        "priority": 0,
        "task_id": "t-with-ttl",
        "prompt": "test",
        "status": "queued",
        "depends_on": [],
        "submitted_at": submitted_at,
        "ttl": 60.0,
        "expires_at": submitted_at + 60.0,
    }]

    app = create_app(mock_orch, _MockHub(), api_key="testkey")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/tasks", headers={"X-API-Key": "testkey"})
        assert resp.status_code == 200
        tasks = resp.json()
        ttl_task = next((t for t in tasks if t["task_id"] == "t-with-ttl"), None)
        assert ttl_task is not None
        assert ttl_task["ttl"] == 60.0
        assert ttl_task["expires_at"] is not None


async def test_rest_post_workflows_with_ttl():
    """POST /workflows with per-task ttl passes ttl through to submit_task."""
    # Track what ttl values were passed to submit_task
    submitted_ttls: list[Any] = []

    class _TrackingOrchestrator(_MockOrchestrator):
        async def submit_task(self, prompt: str, **kwargs) -> Task:
            submitted_ttls.append(kwargs.get("ttl"))
            return await super().submit_task(prompt, **kwargs)

    mock_orch = _TrackingOrchestrator()
    app = create_app(mock_orch, _MockHub(), api_key="testkey")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/workflows",
            json={
                "name": "ttl-workflow",
                "tasks": [
                    {"local_id": "a", "prompt": "step A", "ttl": 20.0},
                    {"local_id": "b", "prompt": "step B", "depends_on": ["a"], "ttl": 30.0},
                ]
            },
            headers={"X-API-Key": "testkey"},
        )
        assert resp.status_code == 200

    assert 20.0 in submitted_ttls
    assert 30.0 in submitted_ttls
