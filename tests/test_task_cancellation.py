"""Tests for task cancellation — v0.27.0 feature.

Covers:
- cancel_task() for queued tasks (tombstone path in _dispatch_loop)
- cancel_task() for in-progress tasks (Agent.interrupt() + _route_loop discard)
- WorkflowManager.cancel() and no-ops after cancellation
- cancel_workflow() orchestrator method
- DELETE /tasks/{task_id} REST endpoint
- DELETE /workflows/{workflow_id} REST endpoint
- Agent.interrupt() default implementation and ClaudeCodeAgent override

Design references:
- Kubernetes Pod deletion grace period (SIGTERM → grace period → SIGKILL)
- POSIX SIGTERM/SIGKILL model — cooperative interrupt before forced kill
- Java Future.cancel(mayInterruptIfRunning=true) — in-flight cancellation
- Go context.Context cancellation — propagated cancellation token
- DESIGN.md §10.22 (v0.27.0)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import tmux_orchestrator.web.app as web_app_mod
from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app
from tmux_orchestrator.workflow_manager import WorkflowManager


# ---------------------------------------------------------------------------
# Helpers / fixtures
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


def make_tmux_mock():
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    return tmux


class HoldingAgent(Agent):
    """Agent that holds BUSY indefinitely so we can test in-progress cancellation."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self.started_event: asyncio.Event = asyncio.Event()
        self.interrupt_called: bool = False
        self.dispatched: list[Task] = []

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
        self.started_event.set()
        # Hold BUSY — simulates a long-running claude session
        await asyncio.sleep(9999)

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass

    async def interrupt(self) -> bool:
        self.interrupt_called = True
        return True


class InstantAgent(Agent):
    """Agent that completes tasks immediately and publishes a RESULT."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self.dispatched: list[Task] = []
        self.dispatched_event: asyncio.Event = asyncio.Event()

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
        await asyncio.sleep(0)
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


# ---------------------------------------------------------------------------
# Agent.interrupt() default implementation
# ---------------------------------------------------------------------------


async def test_agent_base_interrupt_returns_false() -> None:
    """The default Agent.interrupt() implementation is a no-op that returns False."""
    bus = Bus()

    class MinimalAgent(Agent):
        async def start(self) -> None:
            self.status = AgentStatus.IDLE
        async def stop(self) -> None:
            self.status = AgentStatus.STOPPED
        async def _dispatch_task(self, task: Task) -> None:
            pass
        async def handle_output(self, text: str) -> None:
            pass
        async def notify_stdin(self, notification: str) -> None:
            pass

    agent = MinimalAgent("m1", bus)
    result = await agent.interrupt()
    assert result is False


# ---------------------------------------------------------------------------
# cancel_task() — unknown ID
# ---------------------------------------------------------------------------


async def test_cancel_unknown_task_id_returns_false() -> None:
    """cancel_task() returns False for a task ID that was never submitted."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    await orch.start()
    try:
        result = await orch.cancel_task("no-such-task-id-xyz")
        assert result is False
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# cancel_task() — queued tasks
# ---------------------------------------------------------------------------


async def test_cancel_queued_task_returns_true() -> None:
    """cancel_task() returns True and removes a queued task from the queue."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    # No agents — tasks stay queued
    await orch.start()
    try:
        task = await orch.submit_task("queued task")
        assert any(t["task_id"] == task.id for t in orch.list_tasks())

        result = await orch.cancel_task(task.id)

        assert result is True
        assert not any(t["task_id"] == task.id for t in orch.list_tasks())
    finally:
        await orch.stop()


async def test_cancel_queued_task_publishes_status_event() -> None:
    """cancel_task() for a queued task publishes STATUS task_cancelled with was_running=False."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    events_q = await bus.subscribe("test-obs", broadcast=True)

    await orch.start()
    try:
        task = await orch.submit_task("queued cancel test")
        await asyncio.sleep(0.05)
        await orch.cancel_task(task.id)

        found = False
        deadline = asyncio.get_event_loop().time() + 1.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                msg = events_q.get_nowait()
                if (
                    msg.type == MessageType.STATUS
                    and msg.payload.get("event") == "task_cancelled"
                    and msg.payload.get("task_id") == task.id
                ):
                    assert msg.payload.get("was_running") is False
                    found = True
                    break
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.02)
        assert found, "Expected task_cancelled STATUS event for queued task"
    finally:
        await bus.unsubscribe("test-obs")
        await orch.stop()


async def test_cancel_does_not_affect_other_queued_tasks() -> None:
    """Cancelling one queued task leaves other queued tasks intact."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    await orch.start()
    try:
        t1 = await orch.submit_task("keep this one")
        t2 = await orch.submit_task("cancel this one")
        t3 = await orch.submit_task("keep this too")

        result = await orch.cancel_task(t2.id)
        assert result is True

        remaining = {t["task_id"] for t in orch.list_tasks()}
        assert t1.id in remaining
        assert t2.id not in remaining
        assert t3.id in remaining
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# cancel_task() — in-progress tasks
# ---------------------------------------------------------------------------


async def test_cancel_inprogress_task_returns_true() -> None:
    """cancel_task() returns True for a task currently running on an agent."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = HoldingAgent("h1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("long task")
        await asyncio.wait_for(agent.started_event.wait(), timeout=2.0)

        result = await orch.cancel_task(task.id)
        assert result is True
    finally:
        await orch.stop()


async def test_cancel_inprogress_adds_to_cancelled_set() -> None:
    """cancel_task() for an in-progress task adds task_id to _cancelled_task_ids."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = HoldingAgent("h1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("in-progress task")
        await asyncio.wait_for(agent.started_event.wait(), timeout=2.0)

        await orch.cancel_task(task.id)

        assert task.id in orch._cancelled_task_ids
    finally:
        await orch.stop()


async def test_cancel_inprogress_calls_interrupt() -> None:
    """cancel_task() for an in-progress task calls agent.interrupt()."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = HoldingAgent("h1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("interrupt test")
        await asyncio.wait_for(agent.started_event.wait(), timeout=2.0)

        await orch.cancel_task(task.id)

        assert agent.interrupt_called is True
    finally:
        await orch.stop()


async def test_cancel_inprogress_publishes_status_event_was_running() -> None:
    """cancel_task() for in-progress publishes task_cancelled with was_running=True."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = HoldingAgent("h1", bus)
    orch.register_agent(agent)

    events_q = await bus.subscribe("test-obs2", broadcast=True)

    await orch.start()
    try:
        task = await orch.submit_task("in-progress cancel test")
        await asyncio.wait_for(agent.started_event.wait(), timeout=2.0)

        await orch.cancel_task(task.id)

        found = False
        deadline = asyncio.get_event_loop().time() + 1.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                msg = events_q.get_nowait()
                if (
                    msg.type == MessageType.STATUS
                    and msg.payload.get("event") == "task_cancelled"
                    and msg.payload.get("task_id") == task.id
                ):
                    assert msg.payload.get("was_running") is True
                    found = True
                    break
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.02)
        assert found, "Expected task_cancelled STATUS event with was_running=True"
    finally:
        await bus.unsubscribe("test-obs2")
        await orch.stop()


async def test_cancelled_result_is_discarded_in_route_loop() -> None:
    """When a cancelled in-progress task's RESULT arrives, _route_loop discards it."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = HoldingAgent("h1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("discard test")
        await asyncio.wait_for(agent.started_event.wait(), timeout=2.0)

        # Cancel while in-progress
        await orch.cancel_task(task.id)

        # Simulate the agent eventually publishing a RESULT (after C-c)
        await bus.publish(Message(
            type=MessageType.RESULT,
            from_id=agent.id,
            payload={"task_id": task.id, "output": "partial output"},
        ))

        # Give the route loop time to process
        await asyncio.sleep(0.1)

        # The task_id should NOT be in _completed_tasks (result was discarded)
        assert task.id not in orch._completed_tasks
        # The task_id should be cleaned up from _cancelled_task_ids
        assert task.id not in orch._cancelled_task_ids
    finally:
        await orch.stop()


async def test_cancelled_result_no_workflow_callback() -> None:
    """A discarded RESULT for a cancelled task does not trigger workflow callbacks."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = HoldingAgent("h1", bus)
    orch.register_agent(agent)

    # Register a workflow with a single task
    wm = orch.get_workflow_manager()

    await orch.start()
    try:
        task = await orch.submit_task("workflow task")
        # Register workflow before dispatch
        wm.submit("test-wf", [task.id])

        await asyncio.wait_for(agent.started_event.wait(), timeout=2.0)

        # Cancel the in-progress task
        await orch.cancel_task(task.id)

        # Simulate RESULT arriving after cancellation
        await bus.publish(Message(
            type=MessageType.RESULT,
            from_id=agent.id,
            payload={"task_id": task.id, "output": "partial"},
        ))
        await asyncio.sleep(0.1)

        # Workflow should NOT be marked complete (result was discarded)
        run = next(r for r in wm._runs.values())
        assert run.status != "complete"
        assert task.id not in run._completed
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# WorkflowManager.cancel()
# ---------------------------------------------------------------------------


def test_workflow_manager_cancel_sets_status() -> None:
    """WorkflowManager.cancel() sets workflow status to 'cancelled'."""
    wm = WorkflowManager()
    run = wm.submit("test-workflow", ["task-1", "task-2", "task-3"])

    task_ids = wm.cancel(run.id)

    assert wm.get(run.id).status == "cancelled"
    assert set(task_ids) == {"task-1", "task-2", "task-3"}


def test_workflow_manager_cancel_sets_completed_at() -> None:
    """WorkflowManager.cancel() sets completed_at timestamp."""
    wm = WorkflowManager()
    run = wm.submit("test-workflow", ["task-a"])
    assert run.completed_at is None

    wm.cancel(run.id)

    assert wm.get(run.id).completed_at is not None


def test_workflow_manager_cancel_unknown_id_returns_empty() -> None:
    """WorkflowManager.cancel() returns empty list for unknown workflow_id."""
    wm = WorkflowManager()
    result = wm.cancel("no-such-workflow-id")
    assert result == []


def test_workflow_manager_on_task_complete_noop_after_cancel() -> None:
    """on_task_complete() is a no-op when the workflow is already cancelled."""
    wm = WorkflowManager()
    run = wm.submit("test-workflow", ["t1", "t2"])
    wm.cancel(run.id)

    # Should not change status back to complete
    wm.on_task_complete("t1")
    wm.on_task_complete("t2")

    assert wm.get(run.id).status == "cancelled"
    assert "t1" not in wm.get(run.id)._completed
    assert "t2" not in wm.get(run.id)._completed


def test_workflow_manager_on_task_failed_noop_after_cancel() -> None:
    """on_task_failed() is a no-op when the workflow is already cancelled."""
    wm = WorkflowManager()
    run = wm.submit("test-workflow", ["t1"])
    wm.cancel(run.id)

    wm.on_task_failed("t1")

    assert wm.get(run.id).status == "cancelled"
    assert "t1" not in wm.get(run.id)._failed


# ---------------------------------------------------------------------------
# cancel_workflow() — orchestrator method
# ---------------------------------------------------------------------------


async def test_cancel_workflow_cancels_all_queued_tasks() -> None:
    """cancel_workflow() cancels all queued tasks in a workflow."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    wm = orch.get_workflow_manager()

    await orch.start()
    try:
        t1 = await orch.submit_task("wf task 1")
        t2 = await orch.submit_task("wf task 2")
        run = wm.submit("test-wf", [t1.id, t2.id])

        result = await orch.cancel_workflow(run.id)

        assert result is not None
        assert set(result["cancelled"]) == {t1.id, t2.id}
        assert result["already_done"] == []
        assert wm.get(run.id).status == "cancelled"
    finally:
        await orch.stop()


async def test_cancel_workflow_unknown_id_returns_none() -> None:
    """cancel_workflow() returns None for an unknown workflow_id."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    await orch.start()
    try:
        result = await orch.cancel_workflow("no-such-workflow-xyz")
        assert result is None
    finally:
        await orch.stop()


async def test_cancel_workflow_partially_done_tasks() -> None:
    """cancel_workflow() handles already-completed tasks gracefully in already_done."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    wm = orch.get_workflow_manager()

    await orch.start()
    try:
        t_queued = await orch.submit_task("still queued")
        # Register a fake "already done" task (not actually in queue/active)
        fake_done_id = "fake-completed-task-id"
        run = wm.submit("mixed-wf", [t_queued.id, fake_done_id])

        result = await orch.cancel_workflow(run.id)

        assert result is not None
        assert t_queued.id in result["cancelled"]
        assert fake_done_id in result["already_done"]
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_web_state():
    web_app_mod._credentials.clear()
    web_app_mod._sign_counts.clear()
    web_app_mod._sessions.clear()
    web_app_mod._pending_challenge = None
    yield


class _MockHub:
    async def start(self) -> None:
        pass
    async def stop(self) -> None:
        pass
    async def handle(self, ws) -> None:
        pass


class _MockOrchestratorForDelete:
    """Minimal mock orchestrator for REST DELETE endpoint tests."""

    def __init__(self) -> None:
        self._queued: dict[str, str] = {}        # task_id -> prompt (queued)
        self._in_progress: dict[str, str] = {}  # task_id -> prompt (in-progress)
        self._completed: set[str] = set()
        self._cancelled_workflow: dict[str, dict] | None = None
        self._task_started_at: dict[str, float] = {}
        self._completed_tasks: set[str] = set()
        self._director_pending: list = []
        self._dispatch_task = None
        self._workflows: dict[str, dict] = {}

    def list_agents(self) -> list:
        agents = []
        for tid in self._in_progress:
            agents.append({"id": f"agent-for-{tid}"})
        return agents

    def get_agent(self, agent_id: str):
        # Return a mock agent with a current task
        for tid, prompt in self._in_progress.items():
            if agent_id == f"agent-for-{tid}":
                mock_agent = MagicMock()
                mock_task = MagicMock()
                mock_task.id = tid
                mock_agent._current_task = mock_task
                return mock_agent
        return None

    def list_tasks(self) -> list:
        return [
            {"task_id": tid, "prompt": p, "priority": 0}
            for tid, p in self._queued.items()
        ]

    def list_dlq(self) -> list:
        return []

    def get_director(self):
        return None

    def flush_director_pending(self) -> list:
        return []

    @property
    def is_paused(self) -> bool:
        return False

    async def cancel_task(self, task_id: str) -> bool:
        if task_id in self._queued:
            del self._queued[task_id]
            return True
        if task_id in self._in_progress:
            del self._in_progress[task_id]
            return True
        return False

    async def cancel_workflow(self, workflow_id: str) -> dict | None:
        if workflow_id not in self._workflows:
            return None
        wf = self._workflows[workflow_id]
        return {
            "workflow_id": workflow_id,
            "cancelled": wf["task_ids"],
            "already_done": [],
        }

    def get_workflow_manager(self):
        return None


@pytest.fixture
def mock_orch_delete():
    orch = _MockOrchestratorForDelete()
    orch._queued["task-queued"] = "queued task"
    orch._in_progress["task-inprogress"] = "in-progress task"
    orch._workflows["wf-001"] = {"task_ids": ["task-queued", "task-other"]}
    return orch


@pytest.fixture
def app_delete(mock_orch_delete):
    return create_app(mock_orch_delete, _MockHub(), api_key="test-key")


@pytest.fixture
async def client_delete(app_delete):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_delete),
        base_url="http://localhost",
    ) as c:
        yield c


async def test_delete_task_queued_returns_200(client_delete) -> None:
    """DELETE /tasks/{id} on a queued task returns 200 with cancelled=true."""
    r = await client_delete.delete(
        "/tasks/task-queued",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["cancelled"] is True
    assert body["task_id"] == "task-queued"


async def test_delete_task_inprogress_returns_200(client_delete) -> None:
    """DELETE /tasks/{id} on an in-progress task returns 200 with cancelled=true."""
    r = await client_delete.delete(
        "/tasks/task-inprogress",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["cancelled"] is True
    assert body["task_id"] == "task-inprogress"


async def test_delete_task_unknown_returns_404(client_delete) -> None:
    """DELETE /tasks/{id} on an unknown task returns 404."""
    r = await client_delete.delete(
        "/tasks/no-such-task",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 404


async def test_delete_task_requires_auth(client_delete) -> None:
    """DELETE /tasks/{id} requires authentication — returns 401 without credentials."""
    r = await client_delete.delete("/tasks/task-queued")
    assert r.status_code == 401


async def test_delete_workflow_returns_200(client_delete) -> None:
    """DELETE /workflows/{id} on a known workflow returns 200 with cancelled list."""
    r = await client_delete.delete(
        "/workflows/wf-001",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["workflow_id"] == "wf-001"
    assert "cancelled" in body
    assert "already_done" in body


async def test_delete_workflow_unknown_returns_404(client_delete) -> None:
    """DELETE /workflows/{id} on an unknown workflow returns 404."""
    r = await client_delete.delete(
        "/workflows/no-such-workflow",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 404


async def test_delete_workflow_requires_auth(client_delete) -> None:
    """DELETE /workflows/{id} requires authentication."""
    r = await client_delete.delete("/workflows/wf-001")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# ClaudeCodeAgent.interrupt()
# ---------------------------------------------------------------------------


async def test_claude_code_agent_interrupt_sends_ctrl_c() -> None:
    """ClaudeCodeAgent.interrupt() calls pane.send_keys('C-c') and returns True."""
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    bus = Bus()
    tmux = make_tmux_mock()
    agent = ClaudeCodeAgent("cc1", bus, tmux)

    mock_pane = MagicMock()
    agent.pane = mock_pane

    result = await agent.interrupt()

    assert result is True
    mock_pane.send_keys.assert_called_once_with("C-c")


async def test_claude_code_agent_interrupt_no_pane_returns_false() -> None:
    """ClaudeCodeAgent.interrupt() returns False when no pane is attached."""
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    bus = Bus()
    tmux = make_tmux_mock()
    agent = ClaudeCodeAgent("cc2", bus, tmux)
    agent.pane = None  # No pane attached

    result = await agent.interrupt()

    assert result is False


# ---------------------------------------------------------------------------
# Retry cleanup — cancelled task with retries
# ---------------------------------------------------------------------------


async def test_cancel_inprogress_task_with_retries_cleans_up() -> None:
    """Cancelling a retryable in-progress task cleans up _active_tasks on RESULT discard."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = HoldingAgent("h2", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("retryable task", max_retries=3)
        await asyncio.wait_for(agent.started_event.wait(), timeout=2.0)

        # Cancel while in-progress
        await orch.cancel_task(task.id)

        # Simulate RESULT (agent responded to Ctrl-C with an error output)
        await bus.publish(Message(
            type=MessageType.RESULT,
            from_id=agent.id,
            payload={"task_id": task.id, "error": "cancelled", "output": None},
        ))
        await asyncio.sleep(0.1)

        # After discard: task not re-enqueued, _active_tasks cleaned up
        assert task.id not in orch._active_tasks
        assert task.id not in orch._cancelled_task_ids
        assert task.id not in orch._completed_tasks
    finally:
        await orch.stop()
