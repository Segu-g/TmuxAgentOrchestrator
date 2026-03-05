"""Tests for Task.inherit_priority + priority propagation (v0.32.0).

Covers:
- inherit_priority=True (default): child gets parent's priority when parent is higher priority
- inherit_priority=False: child keeps own priority regardless
- Multiple parents: child gets min() of all parent priorities
- Parent with lower priority than child: child keeps own (min of own, parents = own)
- Workflow: priorities propagate through topological order
- _task_priorities populated correctly at submission
- REST POST /tasks with inherit_priority=False
- REST POST /workflows with per-task inherit_priority
- GET /tasks/{id} shows inherit_priority field
- Task with no depends_on: inherit_priority has no effect

Design references:
- Liu & Layland "Scheduling Algorithms for Multiprogramming in a Hard Real-Time
  Environment" JACM 20(1) (1973) — Priority Inheritance Protocol
- Sha, Rajkumar, Lehoczky "Priority Inheritance Protocols: An Approach to Real-Time
  Synchronization" IEEE Transactions on Computers (1990)
- Apache Airflow priority_weight upstream/downstream rules
  (https://airflow.apache.org/docs/apache-airflow/stable/administration-and-deployment/priority-weight.html)
- GeeksforGeeks "Difference Between Priority Inversion and Priority Inheritance" (2024)
- DESIGN.md §10.27 (v0.32.0)
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
# Unit tests: core priority inheritance logic
# ---------------------------------------------------------------------------


async def test_task_inherit_priority_field_default() -> None:
    """Task.inherit_priority defaults to True."""
    task = Task(id="t1", prompt="test")
    assert task.inherit_priority is True


async def test_task_inherit_priority_field_false() -> None:
    """Task.inherit_priority can be set to False."""
    task = Task(id="t1", prompt="test", inherit_priority=False)
    assert task.inherit_priority is False


async def test_no_deps_inherit_priority_no_effect() -> None:
    """Task with no depends_on keeps its own priority regardless of inherit_priority."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())
    task = await orch.submit_task("hello", priority=10, inherit_priority=True)
    assert task.priority == 10
    assert task.inherit_priority is True


async def test_inherit_priority_false_keeps_own_priority() -> None:
    """When inherit_priority=False, child keeps own priority even if parent is higher priority."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    # Parent with high priority (lower number)
    parent = await orch.submit_task("parent", priority=1)
    assert parent.priority == 1

    # Child with low priority but inherit_priority=False
    child = await orch.submit_task(
        "child", priority=10, depends_on=[parent.id], inherit_priority=False
    )
    assert child.priority == 10
    assert child.inherit_priority is False


async def test_inherit_priority_true_inherits_higher_priority_parent() -> None:
    """When inherit_priority=True and parent has higher priority (lower number), child inherits it."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    # Parent submitted with high priority (1 = high)
    parent = await orch.submit_task("parent", priority=1)

    # Child with low priority (10 = low) should inherit parent's priority=1
    child = await orch.submit_task(
        "child", priority=10, depends_on=[parent.id], inherit_priority=True
    )
    assert child.priority == 1


async def test_inherit_priority_true_parent_lower_priority_child_keeps_own() -> None:
    """When parent has lower priority than child, min() = child priority (child keeps own)."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    # Parent with LOW priority (high number = lower priority)
    parent = await orch.submit_task("parent", priority=10)

    # Child with HIGH priority (lower number)
    child = await orch.submit_task(
        "child", priority=1, depends_on=[parent.id], inherit_priority=True
    )
    # min(1, 10) = 1 — child keeps its own higher priority
    assert child.priority == 1


async def test_inherit_priority_multiple_parents_takes_min() -> None:
    """Child with multiple parents gets min() of all parent priorities."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    parent_a = await orch.submit_task("parent-a", priority=3)
    parent_b = await orch.submit_task("parent-b", priority=7)
    parent_c = await orch.submit_task("parent-c", priority=5)

    child = await orch.submit_task(
        "child",
        priority=10,
        depends_on=[parent_a.id, parent_b.id, parent_c.id],
        inherit_priority=True,
    )
    # min(10, 3, 7, 5) = 3
    assert child.priority == 3


async def test_inherit_priority_multiple_parents_own_is_best() -> None:
    """Child own priority is better than all parents — child keeps own."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    parent_a = await orch.submit_task("parent-a", priority=5)
    parent_b = await orch.submit_task("parent-b", priority=8)

    child = await orch.submit_task(
        "child",
        priority=1,
        depends_on=[parent_a.id, parent_b.id],
        inherit_priority=True,
    )
    # min(1, 5, 8) = 1 — child's own priority
    assert child.priority == 1


async def test_task_priorities_populated_at_submit() -> None:
    """_task_priorities is populated for all submitted tasks."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    t1 = await orch.submit_task("t1", priority=5)
    t2 = await orch.submit_task("t2", priority=3)
    t3 = await orch.submit_task("t3", priority=7)

    assert orch._task_priorities[t1.id] == 5
    assert orch._task_priorities[t2.id] == 3
    assert orch._task_priorities[t3.id] == 7


async def test_task_priorities_reflects_effective_priority() -> None:
    """_task_priorities stores the effective (post-inheritance) priority."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    parent = await orch.submit_task("parent", priority=2)
    child = await orch.submit_task(
        "child", priority=10, depends_on=[parent.id], inherit_priority=True
    )
    # Child's effective priority is min(10, 2) = 2
    assert child.priority == 2
    assert orch._task_priorities[child.id] == 2


async def test_inherit_priority_default_is_true() -> None:
    """submit_task() default: inherit_priority=True."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    parent = await orch.submit_task("parent", priority=1)
    child = await orch.submit_task("child", priority=10, depends_on=[parent.id])
    # Default inherit_priority=True means child inherits priority 1
    assert child.priority == 1


async def test_inherit_priority_unknown_dep_skipped() -> None:
    """If a dep ID is not in _task_priorities, it is silently skipped."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    # Submit child with a dep that was never submitted (unknown)
    child = await orch.submit_task(
        "child",
        priority=10,
        depends_on=["unknown-id"],
        inherit_priority=True,
    )
    # No parent priority available → child keeps own priority
    assert child.priority == 10


async def test_task_to_dict_includes_inherit_priority() -> None:
    """Task.to_dict() includes the inherit_priority field."""
    task = Task(id="t1", prompt="test", inherit_priority=False)
    d = task.to_dict()
    assert "inherit_priority" in d
    assert d["inherit_priority"] is False

    task2 = Task(id="t2", prompt="test2", inherit_priority=True)
    d2 = task2.to_dict()
    assert d2["inherit_priority"] is True


# ---------------------------------------------------------------------------
# Workflow support
# ---------------------------------------------------------------------------


async def test_workflow_priority_propagation() -> None:
    """In a workflow A→B→C, if A has priority=1 then B and C inherit it."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    task_a = await orch.submit_task("A", priority=1)
    task_b = await orch.submit_task(
        "B", priority=10, depends_on=[task_a.id], inherit_priority=True
    )
    task_c = await orch.submit_task(
        "C", priority=10, depends_on=[task_b.id], inherit_priority=True
    )

    assert task_a.priority == 1
    assert task_b.priority == 1   # inherits from A
    assert task_c.priority == 1   # inherits from B (which stored priority=1)


async def test_workflow_no_inheritance_chain() -> None:
    """When inherit_priority=False throughout, priorities do not propagate."""
    bus = Bus()
    orch = Orchestrator(bus=bus, tmux=make_tmux_mock(), config=make_config())

    task_a = await orch.submit_task("A", priority=1)
    task_b = await orch.submit_task(
        "B", priority=10, depends_on=[task_a.id], inherit_priority=False
    )
    task_c = await orch.submit_task(
        "C", priority=10, depends_on=[task_b.id], inherit_priority=False
    )

    assert task_a.priority == 1
    assert task_b.priority == 10  # kept own
    assert task_c.priority == 10  # kept own


# ---------------------------------------------------------------------------
# REST: POST /tasks
# ---------------------------------------------------------------------------


def test_rest_post_tasks_inherit_priority_true():
    """REST POST /tasks with inherit_priority=True (default) propagates priority."""
    app, orch = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        # Submit parent first
        r = client.post(
            "/tasks",
            json={"prompt": "parent", "priority": 1},
            headers={"X-API-Key": "test-key"},
        )
        assert r.status_code == 200
        parent_id = r.json()["task_id"]

        # Submit child that inherits priority from parent
        r2 = client.post(
            "/tasks",
            json={
                "prompt": "child",
                "priority": 10,
                "depends_on": [parent_id],
                "inherit_priority": True,
            },
            headers={"X-API-Key": "test-key"},
        )
        assert r2.status_code == 200
        data = r2.json()
        assert data["priority"] == 1
        assert data["inherit_priority"] is True


def test_rest_post_tasks_inherit_priority_false():
    """REST POST /tasks with inherit_priority=False keeps own priority."""
    app, orch = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        # Submit parent
        r = client.post(
            "/tasks",
            json={"prompt": "parent", "priority": 1},
            headers={"X-API-Key": "test-key"},
        )
        assert r.status_code == 200
        parent_id = r.json()["task_id"]

        # Submit child with inherit_priority=False
        r2 = client.post(
            "/tasks",
            json={
                "prompt": "child",
                "priority": 10,
                "depends_on": [parent_id],
                "inherit_priority": False,
            },
            headers={"X-API-Key": "test-key"},
        )
        assert r2.status_code == 200
        data = r2.json()
        assert data["priority"] == 10
        assert data["inherit_priority"] is False


def test_rest_post_tasks_inherit_priority_default():
    """REST POST /tasks without inherit_priority defaults to True."""
    app, orch = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.post(
            "/tasks",
            json={"prompt": "task", "priority": 5},
            headers={"X-API-Key": "test-key"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["inherit_priority"] is True


# ---------------------------------------------------------------------------
# REST: POST /workflows
# ---------------------------------------------------------------------------


def test_rest_post_workflow_inherit_priority_propagates():
    """REST POST /workflows: per-task inherit_priority is applied during submission."""
    app, orch = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.post(
            "/workflows",
            json={
                "name": "test-wf",
                "tasks": [
                    {"local_id": "A", "prompt": "task A", "priority": 1},
                    {
                        "local_id": "B",
                        "prompt": "task B",
                        "priority": 10,
                        "depends_on": ["A"],
                        "inherit_priority": True,
                    },
                ],
            },
            headers={"X-API-Key": "test-key"},
        )
        assert r.status_code == 200
        data = r.json()
        task_ids = data["task_ids"]
        b_id = task_ids["B"]

        # Check priority of B via _task_priorities
        assert orch._task_priorities[b_id] == 1


def test_rest_post_workflow_inherit_priority_false():
    """REST POST /workflows: per-task inherit_priority=False keeps own priority."""
    app, orch = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.post(
            "/workflows",
            json={
                "name": "test-wf",
                "tasks": [
                    {"local_id": "A", "prompt": "task A", "priority": 1},
                    {
                        "local_id": "B",
                        "prompt": "task B",
                        "priority": 10,
                        "depends_on": ["A"],
                        "inherit_priority": False,
                    },
                ],
            },
            headers={"X-API-Key": "test-key"},
        )
        assert r.status_code == 200
        data = r.json()
        task_ids = data["task_ids"]
        b_id = task_ids["B"]

        # B should keep priority=10 because inherit_priority=False
        assert orch._task_priorities[b_id] == 10


# ---------------------------------------------------------------------------
# REST: GET /tasks/{task_id}
# ---------------------------------------------------------------------------


def test_rest_get_task_shows_inherit_priority_for_waiting_task():
    """GET /tasks/{id} includes inherit_priority field for a waiting task."""
    app, orch = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        # Submit a parent task
        r1 = client.post(
            "/tasks",
            json={"prompt": "parent", "priority": 1},
            headers={"X-API-Key": "test-key"},
        )
        parent_id = r1.json()["task_id"]

        # Submit a dependent (waiting) task
        r2 = client.post(
            "/tasks",
            json={
                "prompt": "child",
                "priority": 10,
                "depends_on": [parent_id],
                "inherit_priority": False,
            },
            headers={"X-API-Key": "test-key"},
        )
        child_id = r2.json()["task_id"]

        # GET /tasks/{child_id}
        r3 = client.get(
            f"/tasks/{child_id}",
            headers={"X-API-Key": "test-key"},
        )
        assert r3.status_code == 200
        data = r3.json()
        assert data["status"] == "waiting"
        assert "inherit_priority" in data
        assert data["inherit_priority"] is False


def test_rest_get_task_shows_inherit_priority_for_queued_task():
    """GET /tasks/{id} includes inherit_priority for a queued task."""
    app, orch = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.post(
            "/tasks",
            json={"prompt": "standalone", "priority": 5},
            headers={"X-API-Key": "test-key"},
        )
        task_id = r.json()["task_id"]

        r2 = client.get(
            f"/tasks/{task_id}",
            headers={"X-API-Key": "test-key"},
        )
        assert r2.status_code == 200
        data = r2.json()
        assert "inherit_priority" in data
        # Default is True
        assert data["inherit_priority"] is True
