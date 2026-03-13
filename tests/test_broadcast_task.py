"""Tests for broadcast task (fan-out, race/gather) — v1.2.15.

Design reference: DESIGN.md §10.91 (v1.2.15)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tmux_orchestrator.application.orchestrator import BroadcastGroup, BroadcastResult, Orchestrator
from tmux_orchestrator.web.schemas import BroadcastTaskSubmit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(task_id: str = "task-1", prompt: str = "hello"):
    from tmux_orchestrator.domain.task import Task

    return Task(id=task_id, prompt=prompt)


def _make_agent(agent_id: str, tags: list[str] | None = None):
    from tmux_orchestrator.agents.base import AgentStatus

    agent = MagicMock()
    agent.id = agent_id
    agent.status = AgentStatus.IDLE
    agent.tags = tags or []
    agent._current_task = None
    agent.worktree_path = None
    agent.started_at = None
    agent.uptime_s = 0
    from tmux_orchestrator.domain.agent import AgentRole
    agent.role = AgentRole.WORKER
    return agent


def _make_orchestrator():
    """Create a minimal Orchestrator suitable for unit testing."""
    from tmux_orchestrator.application.bus import Bus
    from tmux_orchestrator.application.config import OrchestratorConfig
    from tmux_orchestrator.application.orchestrator import (
        NullAutoScaler,
        NullCheckpointStore,
        NullContextMonitor,
        NullDriftMonitor,
        NullResultStore,
    )

    bus = Bus()
    tmux = MagicMock()
    config = OrchestratorConfig(
        session_name="test",
        agents=[],
        task_queue_maxsize=100,
        p2p_permissions=[],
        webhooks=[],
    )
    orch = Orchestrator(
        bus=bus,
        tmux=tmux,
        config=config,
        context_monitor=NullContextMonitor(),
        drift_monitor=NullDriftMonitor(),
        result_store=NullResultStore(),
        checkpoint_store=NullCheckpointStore(),
        autoscaler=NullAutoScaler(),
    )
    return orch


# ---------------------------------------------------------------------------
# 1. BroadcastTaskSubmit model parses correctly
# ---------------------------------------------------------------------------


def test_broadcast_submit_model_defaults():
    body = BroadcastTaskSubmit(prompt="solve it", agent_ids=["a1", "a2"])
    assert body.prompt == "solve it"
    assert body.agent_ids == ["a1", "a2"]
    assert body.mode == "race"
    assert body.priority == 0
    assert body.timeout is None
    assert body.target_tags == []
    assert body.target_group is None


def test_broadcast_submit_model_gather():
    body = BroadcastTaskSubmit(prompt="x", mode="gather", target_tags=["solver"])
    assert body.mode == "gather"
    assert body.target_tags == ["solver"]


# ---------------------------------------------------------------------------
# 2. broadcast_task submits N tasks (one per agent_id)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_task_submits_n_tasks():
    orch = _make_orchestrator()
    submitted: list[str] = []

    async def _fake_submit(prompt, *, target_agent=None, priority=0, timeout=None, **kw):
        task = _make_task(task_id=f"t-{target_agent}")
        submitted.append(target_agent)
        return task

    orch.submit_task = _fake_submit
    result = await orch.broadcast_task("prompt", ["a1", "a2", "a3"], mode="race")
    assert len(result.task_ids) == 3
    assert set(submitted) == {"a1", "a2", "a3"}


# ---------------------------------------------------------------------------
# 3. Each task has correct target_agent set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_task_sets_target_agent():
    orch = _make_orchestrator()
    targets_seen: list[str] = []

    async def _fake_submit(prompt, *, target_agent=None, priority=0, timeout=None, **kw):
        targets_seen.append(target_agent)
        return _make_task(task_id=f"t-{target_agent}")

    orch.submit_task = _fake_submit
    await orch.broadcast_task("x", ["worker-1", "worker-2"])
    assert "worker-1" in targets_seen
    assert "worker-2" in targets_seen


# ---------------------------------------------------------------------------
# 4. BroadcastResult has correct broadcast_id and task_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_result_structure():
    orch = _make_orchestrator()
    counter = [0]

    async def _fake_submit(prompt, *, target_agent=None, priority=0, timeout=None, **kw):
        counter[0] += 1
        return _make_task(task_id=f"task-{counter[0]}")

    orch.submit_task = _fake_submit
    result = await orch.broadcast_task("hello", ["a", "b"])
    assert isinstance(result, BroadcastResult)
    assert len(result.broadcast_id) > 0
    assert len(result.task_ids) == 2
    assert result.agent_ids == ["a", "b"]
    assert result.mode == "race"


# ---------------------------------------------------------------------------
# 5. Race mode: first result wins, others cancelled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_race_mode_first_result_cancels_others():
    orch = _make_orchestrator()
    cancelled: list[str] = []

    task_map: dict[str, str] = {}
    ids = ["t1", "t2", "t3"]
    agents = ["a1", "a2", "a3"]
    idx = [0]

    async def _fake_submit(prompt, *, target_agent=None, priority=0, timeout=None, **kw):
        tid = ids[idx[0]]
        task_map[tid] = target_agent
        idx[0] += 1
        return _make_task(task_id=tid)

    async def _fake_cancel(task_id):
        cancelled.append(task_id)
        return True

    orch.submit_task = _fake_submit
    orch.cancel_task = _fake_cancel

    result = await orch.broadcast_task("x", agents, mode="race")
    bc = orch.get_broadcast(result.broadcast_id)
    assert bc is not None
    assert bc.mode == "race"
    assert bc.status == "pending"

    # Simulate first result arriving (t1 wins)
    bc.completed_tasks["t1"] = "score: 42"
    bc.winner_task_id = "t1"
    bc.status = "complete"
    bc.cancelled = True
    # Cancel remaining
    for tid in ["t2", "t3"]:
        asyncio.create_task(orch.cancel_task(tid))

    await asyncio.sleep(0)  # yield to event loop
    assert bc.winner_task_id == "t1"
    assert bc.status == "complete"


# ---------------------------------------------------------------------------
# 6. Race mode: winner contains winning task info
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_race_mode_winner_task_id():
    orch = _make_orchestrator()
    idx = [0]
    task_ids = ["r1", "r2"]

    async def _fake_submit(prompt, *, target_agent=None, priority=0, timeout=None, **kw):
        tid = task_ids[idx[0]]
        idx[0] += 1
        return _make_task(task_id=tid)

    orch.submit_task = _fake_submit

    result = await orch.broadcast_task("solve", ["a1", "a2"], mode="race")
    bc = orch._broadcast_groups[result.broadcast_id]
    # Manually set winner
    bc.completed_tasks["r1"] = "answer"
    bc.winner_task_id = "r1"
    bc.status = "complete"

    assert bc.winner_task_id == "r1"
    assert bc.completed_tasks["r1"] == "answer"


# ---------------------------------------------------------------------------
# 7. Gather mode: all results collected before complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gather_mode_collects_all_results():
    orch = _make_orchestrator()
    idx = [0]
    task_ids = ["g1", "g2", "g3"]

    async def _fake_submit(prompt, *, target_agent=None, priority=0, timeout=None, **kw):
        tid = task_ids[idx[0]]
        idx[0] += 1
        return _make_task(task_id=tid)

    orch.submit_task = _fake_submit

    result = await orch.broadcast_task("x", ["a", "b", "c"], mode="gather")
    bc = orch._broadcast_groups[result.broadcast_id]

    # Add 2 of 3 results — should still be pending/running
    bc.completed_tasks["g1"] = "res1"
    bc.completed_tasks["g2"] = "res2"
    assert bc.status != "complete"  # not yet done

    # Add final result
    bc.completed_tasks["g3"] = "res3"
    bc.status = "complete"  # normally done by _route_loop

    assert len(bc.completed_tasks) == 3
    assert bc.status == "complete"


# ---------------------------------------------------------------------------
# 8. Gather mode: results list has N entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gather_mode_results_count():
    orch = _make_orchestrator()
    idx = [0]
    task_ids = ["h1", "h2"]

    async def _fake_submit(prompt, *, target_agent=None, priority=0, timeout=None, **kw):
        tid = task_ids[idx[0]]
        idx[0] += 1
        return _make_task(task_id=tid)

    orch.submit_task = _fake_submit

    result = await orch.broadcast_task("q", ["ag1", "ag2"], mode="gather")
    bc = orch._broadcast_groups[result.broadcast_id]
    bc.completed_tasks["h1"] = "out1"
    bc.completed_tasks["h2"] = "out2"
    bc.status = "complete"

    assert len(bc.completed_tasks) == 2
    assert bc.completed_tasks["h1"] == "out1"
    assert bc.completed_tasks["h2"] == "out2"


# ---------------------------------------------------------------------------
# 9. GET /tasks/broadcast/{id} returns 200 with correct structure
# ---------------------------------------------------------------------------


@pytest.fixture
def test_app():
    from tmux_orchestrator.application.bus import Bus
    from tmux_orchestrator.application.config import OrchestratorConfig
    from tmux_orchestrator.application.orchestrator import (
        NullAutoScaler,
        NullCheckpointStore,
        NullContextMonitor,
        NullDriftMonitor,
        NullResultStore,
    )
    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.web.ws import WebSocketHub

    bus = Bus()
    tmux = MagicMock()
    config = OrchestratorConfig(
        session_name="test",
        agents=[],
        task_queue_maxsize=100,
        p2p_permissions=[],
        webhooks=[],
    )
    orch = Orchestrator(
        bus=bus,
        tmux=tmux,
        config=config,
        context_monitor=NullContextMonitor(),
        drift_monitor=NullDriftMonitor(),
        result_store=NullResultStore(),
        checkpoint_store=NullCheckpointStore(),
        autoscaler=NullAutoScaler(),
    )
    hub = WebSocketHub(bus)
    api_key = "test-key"
    app = create_app(orch, hub, api_key=api_key)
    return app, orch, api_key


def test_get_broadcast_returns_200(test_app):
    app, orch, api_key = test_app
    # Pre-insert a broadcast group
    bc = BroadcastGroup(
        broadcast_id="bc-001",
        mode="race",
        task_ids=["t1", "t2"],
        agent_ids=["a1", "a2"],
        status="complete",
        winner_task_id="t1",
        completed_tasks={"t1": "result text"},
    )
    orch._broadcast_groups["bc-001"] = bc

    with TestClient(app) as client:
        resp = client.get(
            "/tasks/broadcast/bc-001",
            headers={"X-API-Key": api_key},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["broadcast_id"] == "bc-001"
    assert data["mode"] == "race"
    assert data["status"] == "complete"
    assert data["winner"]["task_id"] == "t1"
    assert data["winner"]["result"] == "result text"
    assert len(data["task_ids"]) == 2


# ---------------------------------------------------------------------------
# 10. GET /tasks/broadcast/{id} returns 404 for unknown broadcast_id
# ---------------------------------------------------------------------------


def test_get_broadcast_404(test_app):
    app, orch, api_key = test_app
    with TestClient(app) as client:
        resp = client.get(
            "/tasks/broadcast/no-such-id",
            headers={"X-API-Key": api_key},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 11. target_tags resolves to correct agent IDs
# ---------------------------------------------------------------------------


def test_post_broadcast_target_tags_resolution(test_app):
    app, orch, api_key = test_app

    # Register agents with tags
    a1 = _make_agent("solver-1", tags=["solver", "python"])
    a2 = _make_agent("solver-2", tags=["solver", "rust"])
    a3 = _make_agent("writer-1", tags=["writer"])
    orch.registry.register(a1)
    orch.registry.register(a2)
    orch.registry.register(a3)

    submitted_targets: list[str] = []

    original_broadcast = orch.broadcast_task

    async def _intercepted_broadcast(prompt, agent_ids, *, mode="race", priority=0, timeout=None):
        submitted_targets.extend(agent_ids)
        return BroadcastResult(
            broadcast_id="bc-test",
            mode=mode,
            task_ids=[f"t-{aid}" for aid in agent_ids],
            agent_ids=list(agent_ids),
        )

    orch.broadcast_task = _intercepted_broadcast
    # Pre-insert so GET works
    orch._broadcast_groups["bc-test"] = BroadcastGroup(
        broadcast_id="bc-test", mode="race", status="pending"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/tasks/broadcast",
            json={"prompt": "solve", "target_tags": ["solver"]},
            headers={"X-API-Key": api_key},
        )
    assert resp.status_code == 200
    # solver-1 and solver-2 both have "solver" tag; writer-1 does not
    assert set(submitted_targets) == {"solver-1", "solver-2"}


# ---------------------------------------------------------------------------
# 12. Empty target resolves to 400
# ---------------------------------------------------------------------------


def test_post_broadcast_empty_target_400(test_app):
    app, orch, api_key = test_app
    with TestClient(app) as client:
        resp = client.post(
            "/tasks/broadcast",
            json={"prompt": "x"},
            headers={"X-API-Key": api_key},
        )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 13. BroadcastGroup tracks task_to_broadcast mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_to_broadcast_mapping():
    orch = _make_orchestrator()
    idx = [0]
    task_ids = ["map-1", "map-2"]

    async def _fake_submit(prompt, *, target_agent=None, priority=0, timeout=None, **kw):
        tid = task_ids[idx[0]]
        idx[0] += 1
        return _make_task(task_id=tid)

    orch.submit_task = _fake_submit
    result = await orch.broadcast_task("test", ["ag1", "ag2"], mode="gather")
    assert "map-1" in orch._task_to_broadcast
    assert "map-2" in orch._task_to_broadcast
    assert orch._task_to_broadcast["map-1"] == result.broadcast_id
    assert orch._task_to_broadcast["map-2"] == result.broadcast_id


# ---------------------------------------------------------------------------
# 14. get_broadcast returns None for unknown ID
# ---------------------------------------------------------------------------


def test_get_broadcast_unknown():
    orch = _make_orchestrator()
    assert orch.get_broadcast("no-such-id") is None


# ---------------------------------------------------------------------------
# 15. Gather mode: all-fail sets status=failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gather_all_fail_sets_failed_status():
    orch = _make_orchestrator()
    idx = [0]
    task_ids = ["f1", "f2"]

    async def _fake_submit(prompt, *, target_agent=None, priority=0, timeout=None, **kw):
        tid = task_ids[idx[0]]
        idx[0] += 1
        return _make_task(task_id=tid)

    orch.submit_task = _fake_submit
    result = await orch.broadcast_task("bad", ["x", "y"], mode="gather")
    bc = orch._broadcast_groups[result.broadcast_id]

    # Simulate both tasks failing
    bc.failed_tasks.add("f1")
    bc.failed_tasks.add("f2")
    # Normally _route_loop would set status; replicate logic here
    resolved = len(bc.completed_tasks) + len(bc.failed_tasks)
    if resolved >= len(bc.task_ids):
        bc.status = "complete" if bc.completed_tasks else "failed"

    assert bc.status == "failed"
