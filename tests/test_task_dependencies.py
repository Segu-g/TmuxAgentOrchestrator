"""Tests for first-class task-level depends_on (v0.29.0).

Covers:
- Task with no deps: immediately queued
- Task with one dep (not yet complete): held in _waiting_tasks
- Task released when dep completes
- Task with two deps: not released until BOTH complete
- Dep failure cascades: waiting task gets dependency_failed error
- Cascade: A->B->C, A fails -> B and C both fail
- Already-completed dep: task submitted after dep is done -> queued immediately
- _completed_task_ids populated correctly on success
- GET /tasks/{id} shows depends_on and blocking
- GET /tasks list shows waiting tasks with status="waiting"
- REST POST /tasks with depends_on
- REST POST /tasks/batch with sibling local_id cross-references
- Batch: mix of free and dependent tasks
- Cancellation of a dep: waiting task cancellation
- _task_dependents cleaned up after resolution
- Dep failure: already-failed dep causes immediate failure at submit time
- Task.to_dict() includes depends_on field
- Multiple tasks waiting on same dep
- Cancellation of a waiting task cleans up reverse-lookup
- Status field propagated in GET /tasks list

Design references:
- GNU Make dependency resolution — prerequisite targets
- Dask task graphs — compute graph with deferred execution
- Apache Spark DAG scheduler — stage dependency tracking
- POSIX make prerequisites — dependency-driven build
- DESIGN.md §10.24 (v0.29.0)
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


def make_tmux_mock():
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    return tmux


class DummyAgent(Agent):
    """Minimal agent that records dispatched tasks and becomes IDLE immediately."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self.dispatched: list[Task] = []
        self.dispatched_event: asyncio.Event = asyncio.Event()
        self._complete_event: asyncio.Event = asyncio.Event()

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
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


class FailingAgent(Agent):
    """Agent that always fails tasks (publishes RESULT with error)."""

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
        # Publish failure result
        await self.bus.publish(Message(
            type=MessageType.RESULT,
            from_id=self.id,
            payload={"task_id": task.id, "error": "task_failed", "output": None},
        ))
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


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
# Unit tests: Orchestrator core dependency logic
# ---------------------------------------------------------------------------


async def test_no_deps_immediately_queued() -> None:
    """A task with no depends_on is queued immediately."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())
    task = await orch.submit_task("hello")
    assert task.id not in orch._waiting_tasks
    assert orch._task_queue.qsize() == 1


async def test_one_dep_not_met_held_in_waiting() -> None:
    """A task whose dep is not complete is held in _waiting_tasks."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())
    task = await orch.submit_task("child", depends_on=["nonexistent-dep"])
    assert task.id in orch._waiting_tasks
    assert orch._task_queue.qsize() == 0


async def test_waiting_task_released_when_dep_completes() -> None:
    """A held task is moved to the queue when its dep completes."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        # Submit parent
        parent = await orch.submit_task("parent")
        # Submit child that depends on parent
        child = await orch.submit_task("child", depends_on=[parent.id])
        assert child.id in orch._waiting_tasks

        # Wait for parent to complete
        await asyncio.wait_for(agent.dispatched_event.wait(), timeout=2.0)

        # Simulate parent success
        await orch.bus.publish(Message(
            type=MessageType.RESULT,
            from_id="a1",
            payload={"task_id": parent.id, "error": None, "output": "done"},
        ))

        # Give the route loop time to process and release child
        await asyncio.sleep(0.2)

        assert child.id not in orch._waiting_tasks
        assert parent.id in orch._completed_tasks
    finally:
        await orch.stop()


async def test_two_deps_both_must_complete() -> None:
    """A task with two deps is not released until both complete."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    dep1 = await orch.submit_task("dep1")
    dep2 = await orch.submit_task("dep2")
    child = await orch.submit_task("child", depends_on=[dep1.id, dep2.id])

    assert child.id in orch._waiting_tasks

    # Simulate first dep completing
    orch._completed_tasks.add(dep1.id)
    await orch._on_dep_satisfied(dep1.id)

    # Child should still be waiting (dep2 not done)
    assert child.id in orch._waiting_tasks

    # Now complete second dep
    orch._completed_tasks.add(dep2.id)
    await orch._on_dep_satisfied(dep2.id)

    # Now child should be released
    assert child.id not in orch._waiting_tasks
    assert orch._task_queue.qsize() >= 1  # parent tasks + child


async def test_already_completed_dep_queued_immediately() -> None:
    """A task submitted after its dep is already done is queued immediately."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    # Manually mark a dep as completed
    fake_dep_id = "already-done-task"
    orch._completed_tasks.add(fake_dep_id)

    task = await orch.submit_task("child", depends_on=[fake_dep_id])
    assert task.id not in orch._waiting_tasks
    assert orch._task_queue.qsize() == 1


async def test_completed_tasks_populated_on_success() -> None:
    """_completed_tasks is populated when a RESULT with no error arrives."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("hello")
        await asyncio.wait_for(agent.dispatched_event.wait(), timeout=2.0)

        # Publish success result
        await orch.bus.publish(Message(
            type=MessageType.RESULT,
            from_id="a1",
            payload={"task_id": task.id, "error": None, "output": "done"},
        ))
        await asyncio.sleep(0.2)

        assert task.id in orch._completed_tasks
    finally:
        await orch.stop()


async def test_dep_failure_cascades_to_waiting_task() -> None:
    """When a dep finally fails, waiting tasks are also failed with dependency_failed."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    # Capture STATUS events
    events = []
    queue = await bus.subscribe("__test_watcher__", broadcast=True)

    async def collect():
        while True:
            msg = await queue.get()
            if msg.type == MessageType.STATUS:
                events.append(msg.payload)
            queue.task_done()

    collector = asyncio.create_task(collect())

    # Submit dep and child
    dep_id = "dep-that-will-fail"
    child = await orch.submit_task("child", depends_on=[dep_id])
    assert child.id in orch._waiting_tasks

    # Simulate dep failure (add to failed_tasks + cascade)
    orch._failed_tasks.add(dep_id)
    await orch._on_dep_failed(dep_id)
    await asyncio.sleep(0.1)

    collector.cancel()
    await bus.unsubscribe("__test_watcher__")

    # Child should be removed from waiting
    assert child.id not in orch._waiting_tasks
    # Child should be in failed tasks
    assert child.id in orch._failed_tasks

    # A task_dependency_failed event should have been published
    dep_failed_events = [e for e in events if e.get("event") == "task_dependency_failed"]
    assert any(e["task_id"] == child.id for e in dep_failed_events)
    assert any(e.get("failed_dep") == dep_id for e in dep_failed_events)


async def test_cascade_a_b_c_all_fail() -> None:
    """A->B->C chain: A fails -> B and C both fail (cascade)."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    events = []
    queue = await bus.subscribe("__test_watcher2__", broadcast=True)

    async def collect():
        while True:
            msg = await queue.get()
            if msg.type == MessageType.STATUS:
                events.append(msg.payload)
            queue.task_done()

    collector = asyncio.create_task(collect())

    # A depends on nothing, B depends on A, C depends on B
    task_a_id = "task-a"
    task_b = await orch.submit_task("task_b", depends_on=[task_a_id])
    task_c = await orch.submit_task("task_c", depends_on=[task_b.id])

    assert task_b.id in orch._waiting_tasks
    assert task_c.id in orch._waiting_tasks

    # A fails
    orch._failed_tasks.add(task_a_id)
    await orch._on_dep_failed(task_a_id)
    await asyncio.sleep(0.1)

    collector.cancel()
    await bus.unsubscribe("__test_watcher2__")

    # Both B and C should be failed and removed from waiting
    assert task_b.id not in orch._waiting_tasks
    assert task_c.id not in orch._waiting_tasks
    assert task_b.id in orch._failed_tasks
    assert task_c.id in orch._failed_tasks

    dep_failed_events = [e for e in events if e.get("event") == "task_dependency_failed"]
    failed_task_ids = {e["task_id"] for e in dep_failed_events}
    assert task_b.id in failed_task_ids
    assert task_c.id in failed_task_ids


async def test_task_dependents_cleaned_up_after_resolution() -> None:
    """_task_dependents entries are cleaned up after a dep completes."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    dep_id = "parent-task"
    child = await orch.submit_task("child", depends_on=[dep_id])

    assert dep_id in orch._task_dependents
    assert child.id in orch._task_dependents[dep_id]

    # Complete the dep
    orch._completed_tasks.add(dep_id)
    await orch._on_dep_satisfied(dep_id)

    # dep_id entry should be removed from _task_dependents
    assert dep_id not in orch._task_dependents


async def test_multiple_tasks_waiting_on_same_dep() -> None:
    """Multiple waiting tasks on the same dep are all released when it completes."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    dep_id = "shared-parent"
    child1 = await orch.submit_task("child1", depends_on=[dep_id])
    child2 = await orch.submit_task("child2", depends_on=[dep_id])
    child3 = await orch.submit_task("child3", depends_on=[dep_id])

    assert len(orch._task_dependents.get(dep_id, [])) == 3

    orch._completed_tasks.add(dep_id)
    await orch._on_dep_satisfied(dep_id)

    assert child1.id not in orch._waiting_tasks
    assert child2.id not in orch._waiting_tasks
    assert child3.id not in orch._waiting_tasks


async def test_cancel_waiting_task_removes_from_reverse_lookup() -> None:
    """Cancelling a waiting task cleans up _task_dependents."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    dep_id = "dep-task"
    child = await orch.submit_task("child", depends_on=[dep_id])

    assert child.id in orch._waiting_tasks
    assert dep_id in orch._task_dependents

    ok = await orch.cancel_task(child.id)
    assert ok is True
    assert child.id not in orch._waiting_tasks
    # dep_id entry may remain but should no longer contain child.id
    deps_for_dep = orch._task_dependents.get(dep_id, [])
    assert child.id not in deps_for_dep


async def test_already_failed_dep_causes_immediate_failure_on_submit() -> None:
    """Submitting a task whose dep has already failed causes immediate failure."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    events = []
    queue = await bus.subscribe("__test_watcher3__", broadcast=True)

    async def collect():
        while True:
            msg = await queue.get()
            if msg.type == MessageType.STATUS:
                events.append(msg.payload)
            queue.task_done()

    collector = asyncio.create_task(collect())

    # Mark dep as already failed
    failed_dep_id = "already-failed-dep"
    orch._failed_tasks.add(failed_dep_id)

    child = await orch.submit_task("child", depends_on=[failed_dep_id])
    await asyncio.sleep(0.05)

    collector.cancel()
    await bus.unsubscribe("__test_watcher3__")

    # Child should not be in queue or waiting — it's immediately failed
    assert child.id not in orch._waiting_tasks
    assert orch._task_queue.qsize() == 0
    assert child.id in orch._failed_tasks

    dep_failed = [e for e in events if e.get("event") == "task_dependency_failed"]
    assert any(e["task_id"] == child.id for e in dep_failed)


async def test_task_to_dict_includes_depends_on() -> None:
    """Task.to_dict() includes the depends_on field."""
    task = Task(id="t1", prompt="hello", depends_on=["dep1", "dep2"])
    d = task.to_dict()
    assert "depends_on" in d
    assert d["depends_on"] == ["dep1", "dep2"]


async def test_task_blocking_helper() -> None:
    """_task_blocking returns the list of waiting tasks for a given dep."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    dep_id = "my-dep"
    child1 = await orch.submit_task("c1", depends_on=[dep_id])
    child2 = await orch.submit_task("c2", depends_on=[dep_id])

    blocking = orch._task_blocking(dep_id)
    assert child1.id in blocking
    assert child2.id in blocking


async def test_list_tasks_includes_waiting_with_status() -> None:
    """list_tasks() includes waiting tasks with status='waiting'."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    dep_id = "missing-dep"
    child = await orch.submit_task("child", depends_on=[dep_id])

    tasks = orch.list_tasks()
    waiting = [t for t in tasks if t["task_id"] == child.id]
    assert len(waiting) == 1
    assert waiting[0]["status"] == "waiting"
    assert dep_id in waiting[0]["depends_on"]


async def test_full_dispatch_with_deps() -> None:
    """End-to-end: parent completes, child is dispatched afterwards."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        parent = await orch.submit_task("parent")
        child = await orch.submit_task("child", depends_on=[parent.id])

        assert child.id in orch._waiting_tasks

        # Wait for both tasks to eventually be dispatched (parent first, then child)
        for _ in range(40):
            await asyncio.sleep(0.1)
            dispatched_ids = {t.id for t in agent.dispatched}
            if parent.id in dispatched_ids:
                # Parent dispatched — simulate success result if not yet done
                # (DummyAgent does not publish RESULT to the bus; we need to
                # manually emit one so _route_loop can release the child)
                if parent.id not in orch._completed_tasks:
                    await orch.bus.publish(Message(
                        type=MessageType.RESULT,
                        from_id="a1",
                        payload={"task_id": parent.id, "error": None, "output": "done"},
                    ))
            if child.id in dispatched_ids:
                break

        dispatched_ids = {t.id for t in agent.dispatched}
        assert parent.id in dispatched_ids, "Parent was never dispatched"
        assert child.id in dispatched_ids, "Child was never dispatched after parent completed"
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# REST API tests: POST /tasks, POST /tasks/batch, GET /tasks, GET /tasks/{id}
# ---------------------------------------------------------------------------


async def test_rest_post_tasks_with_depends_on() -> None:
    """POST /tasks accepts depends_on and task is held in _waiting_tasks."""
    app, orch = _make_app()
    client = TestClient(app, raise_server_exceptions=True)

    # First submit a free task to get its ID
    r1 = client.post("/tasks", json={"prompt": "parent"}, headers={"X-API-Key": "test-key"})
    assert r1.status_code == 200
    parent_id = r1.json()["task_id"]

    # Submit child with depends_on
    r2 = client.post(
        "/tasks",
        json={"prompt": "child", "depends_on": [parent_id]},
        headers={"X-API-Key": "test-key"},
    )
    assert r2.status_code == 200
    body = r2.json()
    child_id = body["task_id"]
    assert body["depends_on"] == [parent_id]
    assert child_id in orch._waiting_tasks


async def test_rest_get_task_shows_waiting_status() -> None:
    """GET /tasks/{id} returns status='waiting' for a task waiting on deps."""
    app, orch = _make_app()
    client = TestClient(app, raise_server_exceptions=True)

    r1 = client.post("/tasks", json={"prompt": "parent"}, headers={"X-API-Key": "test-key"})
    parent_id = r1.json()["task_id"]

    r2 = client.post(
        "/tasks",
        json={"prompt": "child", "depends_on": [parent_id]},
        headers={"X-API-Key": "test-key"},
    )
    child_id = r2.json()["task_id"]

    r3 = client.get(f"/tasks/{child_id}", headers={"X-API-Key": "test-key"})
    assert r3.status_code == 200
    data = r3.json()
    assert data["status"] == "waiting"
    assert parent_id in data["depends_on"]


async def test_rest_get_task_shows_blocking() -> None:
    """GET /tasks/{id} includes blocking list for a task that has waiters."""
    app, orch = _make_app()
    client = TestClient(app, raise_server_exceptions=True)

    r1 = client.post("/tasks", json={"prompt": "parent"}, headers={"X-API-Key": "test-key"})
    parent_id = r1.json()["task_id"]

    r2 = client.post(
        "/tasks",
        json={"prompt": "child", "depends_on": [parent_id]},
        headers={"X-API-Key": "test-key"},
    )
    child_id = r2.json()["task_id"]

    r3 = client.get(f"/tasks/{parent_id}", headers={"X-API-Key": "test-key"})
    assert r3.status_code == 200
    data = r3.json()
    assert "blocking" in data
    assert child_id in data["blocking"]


async def test_rest_get_tasks_list_shows_waiting() -> None:
    """GET /tasks list includes tasks with status='waiting'."""
    app, orch = _make_app()
    client = TestClient(app, raise_server_exceptions=True)

    r1 = client.post("/tasks", json={"prompt": "parent"}, headers={"X-API-Key": "test-key"})
    parent_id = r1.json()["task_id"]

    r2 = client.post(
        "/tasks",
        json={"prompt": "child", "depends_on": [parent_id]},
        headers={"X-API-Key": "test-key"},
    )
    child_id = r2.json()["task_id"]

    r3 = client.get("/tasks", headers={"X-API-Key": "test-key"})
    assert r3.status_code == 200
    tasks = r3.json()
    task_map = {t["task_id"]: t for t in tasks}

    assert child_id in task_map
    assert task_map[child_id]["status"] == "waiting"
    assert parent_id in task_map[child_id].get("depends_on", [])


async def test_rest_batch_with_local_id_sibling_deps() -> None:
    """POST /tasks/batch with local_id sibling cross-references resolves correctly."""
    app, orch = _make_app()
    client = TestClient(app, raise_server_exceptions=True)

    r = client.post(
        "/tasks/batch",
        json={
            "tasks": [
                {"local_id": "step1", "prompt": "step 1"},
                {"local_id": "step2", "prompt": "step 2", "depends_on": ["step1"]},
                {"local_id": "step3", "prompt": "step 3", "depends_on": ["step2"]},
            ]
        },
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert len(tasks) == 3

    step1_id = tasks[0]["task_id"]
    step2_id = tasks[1]["task_id"]
    step3_id = tasks[2]["task_id"]

    # step1 should be queued (no deps)
    assert step1_id not in orch._waiting_tasks

    # step2 depends on step1 (resolved from local_id "step1")
    assert step2_id in orch._waiting_tasks
    assert tasks[1]["depends_on"] == [step1_id]

    # step3 depends on step2
    assert step3_id in orch._waiting_tasks
    assert tasks[2]["depends_on"] == [step2_id]


async def test_rest_batch_mix_free_and_dependent() -> None:
    """POST /tasks/batch mix: free tasks queued, dependent tasks held."""
    app, orch = _make_app()
    client = TestClient(app, raise_server_exceptions=True)

    r = client.post(
        "/tasks/batch",
        json={
            "tasks": [
                {"prompt": "free task 1"},
                {"prompt": "free task 2"},
                {"local_id": "parent", "prompt": "parent task"},
                {"prompt": "dependent", "depends_on": ["parent"]},
            ]
        },
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert len(tasks) == 4

    parent_id = tasks[2]["task_id"]
    dep_id = tasks[3]["task_id"]

    # Three free tasks queued
    assert orch._task_queue.qsize() == 3
    # Dependent task waiting
    assert dep_id in orch._waiting_tasks
    assert tasks[3]["depends_on"] == [parent_id]


async def test_rest_batch_with_global_task_id_dep() -> None:
    """POST /tasks/batch: depends_on referencing a global task ID (not sibling)."""
    app, orch = _make_app()
    client = TestClient(app, raise_server_exceptions=True)

    # Submit an independent task first
    r0 = client.post("/tasks", json={"prompt": "global parent"}, headers={"X-API-Key": "test-key"})
    global_parent_id = r0.json()["task_id"]

    r = client.post(
        "/tasks/batch",
        json={
            "tasks": [
                {"prompt": "child", "depends_on": [global_parent_id]},
            ]
        },
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    child_id = tasks[0]["task_id"]

    assert child_id in orch._waiting_tasks
    assert tasks[0]["depends_on"] == [global_parent_id]
