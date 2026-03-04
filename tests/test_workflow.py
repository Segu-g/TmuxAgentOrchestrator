"""Tests for Workflow primitive and Task.depends_on."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tmux_orchestrator.agents.base import AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.workflow import Workflow, _topological_sort, WorkflowStep


# ---------------------------------------------------------------------------
# Task.depends_on field
# ---------------------------------------------------------------------------


def test_task_depends_on_defaults_empty():
    t = Task(id="t1", prompt="hello")
    assert t.depends_on == []


def test_task_depends_on_set():
    t = Task(id="t2", prompt="p", depends_on=["t1"])
    assert t.depends_on == ["t1"]


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------


def test_topo_sort_linear():
    a = WorkflowStep("a")
    b = WorkflowStep("b", after=[a])
    c = WorkflowStep("c", after=[b])
    result = _topological_sort([a, b, c])
    assert result.index(a) < result.index(b) < result.index(c)


def test_topo_sort_diamond():
    root = WorkflowStep("root")
    left = WorkflowStep("left", after=[root])
    right = WorkflowStep("right", after=[root])
    leaf = WorkflowStep("leaf", after=[left, right])
    result = _topological_sort([root, left, right, leaf])
    assert result.index(root) < result.index(left)
    assert result.index(root) < result.index(right)
    assert result.index(left) < result.index(leaf)
    assert result.index(right) < result.index(leaf)


def test_topo_sort_cycle_raises():
    a = WorkflowStep("a")
    b = WorkflowStep("b", after=[a])
    a.after = [b]  # create cycle
    with pytest.raises(ValueError, match="cycle"):
        _topological_sort([a, b])


def test_topo_sort_foreign_dep_raises():
    a = WorkflowStep("a")
    b = WorkflowStep("b", after=[WorkflowStep("not-in-workflow")])
    with pytest.raises(ValueError, match="not in this workflow"):
        _topological_sort([a, b])


# ---------------------------------------------------------------------------
# Orchestrator.submit_task with depends_on
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_orch(tmp_path):
    bus = Bus()
    config = OrchestratorConfig(
        session_name="test", agents=[], mailbox_dir=str(tmp_path), dlq_max_retries=2
    )
    return Orchestrator(bus=bus, tmux=MagicMock(), config=config)


@pytest.mark.asyncio
async def test_submit_task_stores_depends_on(simple_orch):
    task = await simple_orch.submit_task("t", depends_on=["dep-1", "dep-2"])
    assert task.depends_on == ["dep-1", "dep-2"]


# ---------------------------------------------------------------------------
# Dispatch blocked by unmet dependency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_held_until_dependency_complete(tmp_path):
    """Task with depends_on is not dispatched until dependency is in _completed_tasks."""
    from tests.integration.test_orchestration import HeadlessAgent, HeadlessOrchestrator

    bus = Bus()
    from tmux_orchestrator.config import OrchestratorConfig

    cfg = OrchestratorConfig(
        session_name="test",
        agents=[],
        mailbox_dir=str(tmp_path),
        dlq_max_retries=50,
    )
    orch = HeadlessOrchestrator(bus, cfg)
    worker = HeadlessAgent("worker-1", bus)
    orch.register_agent(worker)
    await orch.start()

    try:
        # Submit the "blocker" task first
        blocker = await orch.submit_task("step 1")
        # Submit a dependent task that waits for blocker
        dependent = await orch.submit_task("step 2", depends_on=[blocker.id])

        # Wait for blocker to complete
        collected: list[Message] = []
        q = await bus.subscribe("test-listener", broadcast=True)
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline and len(collected) < 2:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=0.2)
                q.task_done()
                if msg.type == MessageType.RESULT:
                    collected.append(msg)
            except asyncio.TimeoutError:
                pass
        await bus.unsubscribe("test-listener")

        # Both tasks should eventually complete
        result_task_ids = {m.payload["task_id"] for m in collected}
        assert blocker.id in result_task_ids
        assert dependent.id in result_task_ids

        # Blocker must have finished before dependent (ordering by order of result)
        blocker_idx = next(i for i, m in enumerate(collected) if m.payload["task_id"] == blocker.id)
        dependent_idx = next(i for i, m in enumerate(collected) if m.payload["task_id"] == dependent.id)
        assert blocker_idx < dependent_idx
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# Workflow builder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_run_submits_all_steps(tmp_path):
    from tests.integration.test_orchestration import HeadlessAgent, HeadlessOrchestrator
    from tmux_orchestrator.config import OrchestratorConfig

    bus = Bus()
    cfg = OrchestratorConfig(
        session_name="test", agents=[], mailbox_dir=str(tmp_path), dlq_max_retries=50
    )
    orch = HeadlessOrchestrator(bus, cfg)
    for i in range(2):
        orch.register_agent(HeadlessAgent(f"worker-{i}", bus))
    await orch.start()

    try:
        wf = Workflow(orch)
        fetch = wf.step("fetch")
        process = wf.step("process", after=[fetch])
        report = wf.step("report", after=[process])
        tasks = await wf.run()

        assert len(tasks) == 3
        assert tasks[1].depends_on == [tasks[0].id]
        assert tasks[2].depends_on == [tasks[1].id]
    finally:
        await orch.stop()


def test_workflow_step_no_after():
    orch = MagicMock()
    wf = Workflow(orch)
    s = wf.step("standalone")
    assert s.after == []
    assert len(wf._steps) == 1
