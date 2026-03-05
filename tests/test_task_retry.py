"""Tests for per-task retry semantics (v0.26.0).

Design reference:
- AWS SQS maxReceiveCount / Redrive policy — re-enqueue before dead-lettering
  (https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)
- Netflix Hystrix retry — transient-failure tolerance
  (https://github.com/Netflix/Hystrix)
- Polly .NET resilience library — retry policies
  (https://github.com/App-vNext/Polly)
- Erlang OTP supervisor restart strategies — restart_one_for_one
  (https://www.erlang.org/docs/24/design_principles/sup_princ)
- DESIGN.md §10.21 (v0.26.0)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app
from tmux_orchestrator.workflow_manager import WorkflowManager


# ---------------------------------------------------------------------------
# Test helpers / fixtures
# ---------------------------------------------------------------------------


def make_config(**kwargs) -> OrchestratorConfig:
    defaults = dict(
        session_name="test",
        agents=[],
        p2p_permissions=[],
        task_timeout=30,
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


class FailingAgent(Agent):
    """Agent that publishes an error RESULT a configurable number of times,
    then succeeds on subsequent dispatches.

    fail_count controls how many failures to emit before switching to success.
    """

    def __init__(
        self,
        agent_id: str,
        bus: Bus,
        *,
        fail_count: int = 1,
    ) -> None:
        super().__init__(agent_id, bus)
        self.dispatched: list[Task] = []
        self.dispatched_event: asyncio.Event = asyncio.Event()
        self._fail_count = fail_count
        self._dispatch_count = 0

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
        self._dispatch_count += 1
        self.dispatched_event.set()
        await asyncio.sleep(0)

        if self._dispatch_count <= self._fail_count:
            await self.bus.publish(Message(
                type=MessageType.RESULT,
                from_id=self.id,
                payload={
                    "task_id": task.id,
                    "output": None,
                    "error": "simulated failure",
                },
            ))
        else:
            await self.bus.publish(Message(
                type=MessageType.RESULT,
                from_id=self.id,
                payload={
                    "task_id": task.id,
                    "output": "success output",
                    "error": None,
                },
            ))
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


class AlwaysFailingAgent(Agent):
    """Agent that always publishes an error RESULT."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self.dispatched: list[Task] = []
        self.dispatched_event: asyncio.Event = asyncio.Event()
        self._dispatch_count = 0

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
        self._dispatch_count += 1
        self.dispatched_event.set()
        await asyncio.sleep(0)
        await self.bus.publish(Message(
            type=MessageType.RESULT,
            from_id=self.id,
            payload={
                "task_id": task.id,
                "output": None,
                "error": "permanent failure",
            },
        ))
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


async def _wait_for_status_event(
    bus: Bus, event_name: str, *, timeout: float = 3.0
) -> Message | None:
    """Subscribe to bus and wait for a STATUS event matching event_name."""
    q = await bus.subscribe("_test_watcher", broadcast=True)
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                msg = await asyncio.wait_for(q.get(), timeout=min(remaining, 0.2))
                q.task_done()
                if (
                    msg.type == MessageType.STATUS
                    and msg.payload.get("event") == event_name
                ):
                    return msg
            except asyncio.TimeoutError:
                pass
    finally:
        await bus.unsubscribe("_test_watcher")
    return None


async def _wait_for_n_status_events(
    bus: Bus, event_name: str, count: int, *, timeout: float = 5.0
) -> list[Message]:
    """Collect multiple STATUS events matching event_name."""
    q = await bus.subscribe("_test_watcher2", broadcast=True)
    collected: list[Message] = []
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while len(collected) < count and asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                msg = await asyncio.wait_for(q.get(), timeout=min(remaining, 0.2))
                q.task_done()
                if (
                    msg.type == MessageType.STATUS
                    and msg.payload.get("event") == event_name
                ):
                    collected.append(msg)
            except asyncio.TimeoutError:
                pass
    finally:
        await bus.unsubscribe("_test_watcher2")
    return collected


class _StubHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _make_app():
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    app = create_app(orch, _StubHub(), api_key="test-key")  # type: ignore[arg-type]
    return app, orch


# ---------------------------------------------------------------------------
# Unit tests: Task dataclass
# ---------------------------------------------------------------------------


def test_task_default_max_retries():
    """Task defaults to max_retries=0 (no retry)."""
    t = Task(id="t1", prompt="hello")
    assert t.max_retries == 0
    assert t.retry_count == 0


def test_task_max_retries_set():
    """Task accepts max_retries on creation."""
    t = Task(id="t2", prompt="hello", max_retries=3)
    assert t.max_retries == 3
    assert t.retry_count == 0


def test_task_to_dict_includes_retry_fields():
    """Task.to_dict() includes max_retries and retry_count."""
    t = Task(id="t3", prompt="test", max_retries=2, retry_count=1)
    d = t.to_dict()
    assert d["max_retries"] == 2
    assert d["retry_count"] == 1
    assert d["task_id"] == "t3"
    assert d["prompt"] == "test"


def test_task_to_dict_all_fields():
    """Task.to_dict() includes all core fields."""
    t = Task(
        id="t4",
        prompt="do work",
        priority=5,
        max_retries=1,
        retry_count=0,
        required_tags=["python"],
        target_agent="w1",
    )
    d = t.to_dict()
    assert d["task_id"] == "t4"
    assert d["prompt"] == "do work"
    assert d["priority"] == 5
    assert d["max_retries"] == 1
    assert d["retry_count"] == 0
    assert d["required_tags"] == ["python"]
    assert d["target_agent"] == "w1"


# ---------------------------------------------------------------------------
# Unit tests: WorkflowManager.on_task_retrying
# ---------------------------------------------------------------------------


def test_workflow_manager_on_task_retrying_keeps_running():
    """on_task_retrying on a running workflow keeps status 'running'."""
    wm = WorkflowManager()
    run = wm.submit("wf", ["t1", "t2"])
    wm.on_task_complete("t1")
    assert run.status == "running"
    wm.on_task_retrying("t2")
    assert run.status == "running"


def test_workflow_manager_on_task_retrying_unknown_is_noop():
    """on_task_retrying with unknown task ID is a no-op (does not raise)."""
    wm = WorkflowManager()
    run = wm.submit("wf", ["t1"])
    wm.on_task_retrying("nonexistent")  # must not raise
    assert run.status == "pending"


def test_workflow_manager_on_task_retrying_clears_failed_flag():
    """on_task_retrying removes the task from _failed so workflow is not stuck in 'failed'."""
    wm = WorkflowManager()
    run = wm.submit("wf", ["t1"])
    # Simulate premature failure (shouldn't normally happen but verify robustness)
    run._failed.add("t1")
    wm.on_task_retrying("t1")
    assert "t1" not in run._failed
    # Status should not be 'failed' now
    assert run.status != "failed"


def test_workflow_manager_failed_then_retrying_then_complete():
    """Workflow recovers from transient failure after retrying succeeds."""
    wm = WorkflowManager()
    run = wm.submit("wf", ["t1"])
    wm.on_task_retrying("t1")  # t1 is retrying
    assert run.status != "failed"
    wm.on_task_complete("t1")  # retry succeeded
    assert run.status == "complete"


# ---------------------------------------------------------------------------
# Integration tests: orchestrator retry logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_no_retries_fails_immediately():
    """Task with max_retries=0 (default) is dead-lettered on first failure."""
    bus = Bus()
    config = make_config(dlq_max_retries=5)
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=config)
    agent = AlwaysFailingAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("fail me", max_retries=0)
        # Wait for the task to be dispatched and failed
        await asyncio.wait_for(agent.dispatched_event.wait(), timeout=3.0)
        await asyncio.sleep(0.3)
        # Task should NOT be retried — only 1 dispatch
        assert agent._dispatch_count == 1
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_task_retried_correct_number_of_times():
    """Task with max_retries=2 is retried exactly 2 times before failing."""
    bus = Bus()
    config = make_config(dlq_max_retries=5)
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=config)
    agent = AlwaysFailingAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("retry me", max_retries=2)
        # Wait for all dispatches: 1 original + 2 retries = 3 total
        deadline = asyncio.get_running_loop().time() + 5.0
        while agent._dispatch_count < 3 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.3)
        assert agent._dispatch_count == 3  # 1 original + 2 retries
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_retry_count_increments():
    """retry_count on the Task object increments with each retry."""
    bus = Bus()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=config)

    retry_counts: list[int] = []

    class TrackingAgent(Agent):
        async def start(self) -> None:
            self.status = AgentStatus.IDLE
            self._run_task = asyncio.create_task(self._run_loop())

        async def stop(self) -> None:
            self.status = AgentStatus.STOPPED
            if self._run_task:
                self._run_task.cancel()

        async def _dispatch_task(self, task: Task) -> None:
            retry_counts.append(task.retry_count)
            await asyncio.sleep(0)
            if task.retry_count < task.max_retries:
                await self.bus.publish(Message(
                    type=MessageType.RESULT,
                    from_id=self.id,
                    payload={"task_id": task.id, "output": None, "error": "fail"},
                ))
            else:
                await self.bus.publish(Message(
                    type=MessageType.RESULT,
                    from_id=self.id,
                    payload={"task_id": task.id, "output": "done", "error": None},
                ))
            self._set_idle()

        async def handle_output(self, text: str) -> None:
            pass

        async def notify_stdin(self, notification: str) -> None:
            pass

    agent = TrackingAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        await orch.submit_task("track me", max_retries=2)
        deadline = asyncio.get_running_loop().time() + 5.0
        while len(retry_counts) < 3 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.2)
        assert retry_counts == [0, 1, 2]
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_task_retrying_status_event_published():
    """task_retrying STATUS event is published for each retry attempt."""
    bus = Bus()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=config)
    agent = AlwaysFailingAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        # Subscribe before submitting
        q = await bus.subscribe("_evt_watcher", broadcast=True)
        task = await orch.submit_task("retry event", max_retries=2)

        retrying_events: list[Message] = []
        deadline = asyncio.get_running_loop().time() + 5.0
        while len(retrying_events) < 2 and asyncio.get_running_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=0.2)
                q.task_done()
                if msg.payload.get("event") == "task_retrying":
                    retrying_events.append(msg)
            except asyncio.TimeoutError:
                pass
        await bus.unsubscribe("_evt_watcher")

        assert len(retrying_events) == 2
        # First retry event
        assert retrying_events[0].payload["task_id"] == task.id
        assert retrying_events[0].payload["retry_count"] == 1
        assert retrying_events[0].payload["max_retries"] == 2
        # Second retry event
        assert retrying_events[1].payload["retry_count"] == 2
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_task_retrying_event_includes_error():
    """task_retrying STATUS event payload includes the error that caused the retry."""
    bus = Bus()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=config)
    agent = AlwaysFailingAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        q = await bus.subscribe("_evt_err_watcher", broadcast=True)
        task = await orch.submit_task("retry with error", max_retries=1)

        retrying_msg = None
        deadline = asyncio.get_running_loop().time() + 4.0
        while retrying_msg is None and asyncio.get_running_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=0.2)
                q.task_done()
                if msg.payload.get("event") == "task_retrying":
                    retrying_msg = msg
            except asyncio.TimeoutError:
                pass
        await bus.unsubscribe("_evt_err_watcher")

        assert retrying_msg is not None
        assert retrying_msg.payload["error"] == "permanent failure"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_task_re_enqueued_with_same_priority():
    """On retry, the task is re-enqueued preserving its original priority."""
    bus = Bus()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=config)

    priorities_seen: list[int] = []

    class PriorityTracker(Agent):
        async def start(self) -> None:
            self.status = AgentStatus.IDLE
            self._run_task = asyncio.create_task(self._run_loop())

        async def stop(self) -> None:
            self.status = AgentStatus.STOPPED
            if self._run_task:
                self._run_task.cancel()

        async def _dispatch_task(self, task: Task) -> None:
            priorities_seen.append(task.priority)
            await asyncio.sleep(0)
            await self.bus.publish(Message(
                type=MessageType.RESULT,
                from_id=self.id,
                payload={"task_id": task.id, "output": None, "error": "fail"},
            ))
            self._set_idle()

        async def handle_output(self, text: str) -> None:
            pass

        async def notify_stdin(self, notification: str) -> None:
            pass

    agent = PriorityTracker("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        await orch.submit_task("priority check", priority=7, max_retries=2)
        deadline = asyncio.get_running_loop().time() + 5.0
        while len(priorities_seen) < 3 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.2)
        # All dispatches should have the same priority
        assert all(p == 7 for p in priorities_seen)
        assert len(priorities_seen) == 3
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_task_succeeds_on_retry():
    """Task that fails once then succeeds on retry is eventually completed."""
    bus = Bus()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=config)
    agent = FailingAgent("a1", bus, fail_count=1)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("flaky task", max_retries=2)
        # Wait for success
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            if task.id in orch._completed_tasks:
                break
            await asyncio.sleep(0.1)

        assert task.id in orch._completed_tasks
        assert agent._dispatch_count == 2  # failed once, succeeded once
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_task_exhausted_retries_goes_to_dlq():
    """Task exhausting max_retries is dead-lettered (not re-enqueued indefinitely)."""
    bus = Bus()
    # Set dlq_max_retries high so only task-level retries matter
    config = make_config(dlq_max_retries=50)
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=config)
    agent = AlwaysFailingAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("doomed task", max_retries=1)
        # Wait for 2 dispatches (1 original + 1 retry)
        deadline = asyncio.get_running_loop().time() + 5.0
        while agent._dispatch_count < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)

        # After max_retries exhausted, agent is only dispatched 2 times total
        assert agent._dispatch_count == 2
        # Task should NOT be in completed (it failed)
        assert task.id not in orch._completed_tasks
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# Workflow + retry integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_not_failed_during_retries():
    """Workflow remains 'running' while a task is retrying (not 'failed')."""
    bus = Bus()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=config)
    agent = AlwaysFailingAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        q = await bus.subscribe("_wf_watcher", broadcast=True)
        task = await orch.submit_task("wf task", max_retries=2)
        wm = orch.get_workflow_manager()
        run = wm.submit("test-wf", [task.id])

        # Wait for first retry event
        retrying_seen = False
        deadline = asyncio.get_running_loop().time() + 5.0
        while not retrying_seen and asyncio.get_running_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=0.2)
                q.task_done()
                if msg.payload.get("event") == "task_retrying":
                    retrying_seen = True
            except asyncio.TimeoutError:
                pass
        await bus.unsubscribe("_wf_watcher")

        assert retrying_seen
        # Workflow should not be in 'failed' state while retries are ongoing
        assert run.status != "failed"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_workflow_failed_after_retries_exhausted():
    """Workflow transitions to 'failed' only after all retries are exhausted."""
    bus = Bus()
    config = make_config(dlq_max_retries=50)
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=config)
    agent = AlwaysFailingAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("fail wf task", max_retries=1)
        wm = orch.get_workflow_manager()
        run = wm.submit("test-wf-fail", [task.id])

        # Wait for 2 dispatches (original + 1 retry)
        deadline = asyncio.get_running_loop().time() + 5.0
        while agent._dispatch_count < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)

        assert run.status == "failed"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_workflow_completes_when_retry_succeeds():
    """Workflow eventually completes when a retried task succeeds."""
    bus = Bus()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=config)
    # Fail once, then succeed
    agent = FailingAgent("a1", bus, fail_count=1)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("flaky wf task", max_retries=2)
        wm = orch.get_workflow_manager()
        run = wm.submit("test-wf-recover", [task.id])

        # Wait for workflow completion
        deadline = asyncio.get_running_loop().time() + 5.0
        while run.status not in ("complete", "failed") and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)

        assert run.status == "complete"
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# REST API tests
# ---------------------------------------------------------------------------


def test_post_task_with_max_retries():
    """POST /tasks with max_retries=3 returns the field in response."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/tasks",
            json={"prompt": "retryable task", "max_retries": 3},
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["max_retries"] == 3
    assert data["retry_count"] == 0
    assert "task_id" in data


def test_post_task_default_max_retries():
    """POST /tasks without max_retries defaults to 0."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/tasks",
            json={"prompt": "normal task"},
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["max_retries"] == 0
    assert data["retry_count"] == 0


def test_post_tasks_batch_with_max_retries():
    """POST /tasks/batch with max_retries passes it through for each task."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/tasks/batch",
            json={
                "tasks": [
                    {"prompt": "task A", "max_retries": 2},
                    {"prompt": "task B", "max_retries": 0},
                ]
            },
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200, resp.text
    tasks = resp.json()["tasks"]
    assert len(tasks) == 2
    assert tasks[0]["max_retries"] == 2
    assert tasks[0]["retry_count"] == 0
    assert tasks[1]["max_retries"] == 0
    assert tasks[1]["retry_count"] == 0


def test_post_workflow_with_max_retries():
    """POST /workflows with max_retries per task spec accepts and stores the value."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            json={
                "name": "retry-wf",
                "tasks": [
                    {"local_id": "step1", "prompt": "step 1", "max_retries": 3},
                    {"local_id": "step2", "prompt": "step 2", "depends_on": ["step1"], "max_retries": 1},
                ],
            },
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "workflow_id" in data
    assert "step1" in data["task_ids"]
    assert "step2" in data["task_ids"]


def test_get_tasks_empty():
    """GET /tasks returns empty list when no tasks submitted."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.get("/tasks", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_get_tasks_with_queued_task():
    """GET /tasks returns queued tasks."""
    app, orch = _make_app()
    with TestClient(app) as client:
        client.post(
            "/tasks",
            json={"prompt": "pending task", "max_retries": 2},
            headers={"X-API-Key": "test-key"},
        )
        resp = client.get("/tasks", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["prompt"] == "pending task"
    assert tasks[0]["status"] == "queued"


def test_get_tasks_pagination_skip():
    """GET /tasks respects skip parameter."""
    app, _ = _make_app()
    with TestClient(app) as client:
        # Submit 3 tasks
        for i in range(3):
            client.post(
                "/tasks",
                json={"prompt": f"task {i}"},
                headers={"X-API-Key": "test-key"},
            )
        # Skip 2
        resp = client.get("/tasks?skip=2", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1


def test_get_tasks_pagination_limit():
    """GET /tasks respects limit parameter."""
    app, _ = _make_app()
    with TestClient(app) as client:
        for i in range(5):
            client.post(
                "/tasks",
                json={"prompt": f"task {i}"},
                headers={"X-API-Key": "test-key"},
            )
        resp = client.get("/tasks?limit=2", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 2


def test_get_tasks_requires_auth():
    """GET /tasks returns 401 without authentication."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.get("/tasks")
    assert resp.status_code == 401


def test_get_task_by_id_returns_retry_fields():
    """GET /tasks/{id} response includes retry_count and max_retries."""
    app, orch = _make_app()
    with TestClient(app) as client:
        post_resp = client.post(
            "/tasks",
            json={"prompt": "task with retries", "max_retries": 5},
            headers={"X-API-Key": "test-key"},
        )
        task_id = post_resp.json()["task_id"]
        # Enrich _active_tasks with the Task object (normally done by dispatch loop)
        # For this test, manually seed it to verify the endpoint behavior
        from tmux_orchestrator.agents.base import Task
        orch._active_tasks[task_id] = Task(
            id=task_id,
            prompt="task with retries",
            max_retries=5,
            retry_count=2,
        )
        resp = client.get(f"/tasks/{task_id}", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["task_id"] == task_id
    assert data["max_retries"] == 5
    assert data["retry_count"] == 2


def test_get_task_by_id_not_found():
    """GET /tasks/{id} returns 404 for unknown task ID."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.get("/tasks/nonexistent-task-id", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 404
