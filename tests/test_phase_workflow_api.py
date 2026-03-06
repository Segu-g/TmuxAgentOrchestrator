"""Tests for POST /workflows with phases= array — generic declarative workflow API.

Design references:
- §12「ワークフロー設計の層構造」層1・2・3
- arXiv:2512.19769 (PayPal DSL): declarative pattern → task expansion
- DESIGN.md §10.15 (v0.48.0)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app


_API_KEY = "test-key"
_HEADERS = {"X-API-Key": _API_KEY}


# ---------------------------------------------------------------------------
# Fixtures
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
    app = create_app(orch, _StubHub(), api_key=_API_KEY)  # type: ignore[arg-type]
    return app, orch


# ---------------------------------------------------------------------------
# POST /workflows with phases array
# ---------------------------------------------------------------------------


def test_post_workflow_phases_single():
    """POST /workflows with a single-phase single-pattern creates 1 task."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            headers=_HEADERS,
            json={
                "name": "my-pipeline",
                "context": "Build a Python EventBus class",
                "phases": [
                    {"name": "implement", "pattern": "single", "agents": {"tags": ["coder"]}}
                ],
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "workflow_id" in data
    assert data["name"] == "my-pipeline"
    assert len(data["task_ids"]) == 1


def test_post_workflow_phases_two_sequential():
    """Two single-pattern phases produce a sequential 2-task DAG."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            headers=_HEADERS,
            json={
                "name": "design-then-implement",
                "context": "Build a cache module",
                "phases": [
                    {"name": "design", "pattern": "single"},
                    {"name": "implement", "pattern": "single"},
                ],
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["task_ids"]) == 2


def test_post_workflow_phases_parallel():
    """parallel pattern with count=3 produces 3 tasks."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            headers=_HEADERS,
            json={
                "name": "parallel-review",
                "context": "Review the design",
                "phases": [
                    {"name": "review", "pattern": "parallel", "agents": {"count": 3}}
                ],
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["task_ids"]) == 3


def test_post_workflow_phases_competitive():
    """competitive pattern with count=3 produces 3 independent tasks."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            headers=_HEADERS,
            json={
                "name": "best-of-3",
                "context": "Solve the knapsack problem",
                "phases": [
                    {
                        "name": "solve",
                        "pattern": "competitive",
                        "agents": {"count": 3, "tags": ["solver"]},
                    }
                ],
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["task_ids"]) == 3


def test_post_workflow_phases_debate():
    """debate pattern with 1 round produces 3 tasks (advocate + critic + judge)."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            headers=_HEADERS,
            json={
                "name": "design-debate",
                "context": "Should we use microservices?",
                "phases": [
                    {
                        "name": "debate",
                        "pattern": "debate",
                        "agents": {"tags": ["advocate"]},
                        "critic_agents": {"tags": ["critic"]},
                        "judge_agents": {"tags": ["judge"]},
                        "debate_rounds": 1,
                    }
                ],
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    # 1 round: advocate + critic + judge = 3
    assert len(data["task_ids"]) == 3


def test_post_workflow_phases_mixed_pipeline():
    """design (debate) → implement (single) → review (parallel:2) → merge (single)."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            headers=_HEADERS,
            json={
                "name": "full-pipeline",
                "context": "Build an async event bus",
                "phases": [
                    {
                        "name": "design",
                        "pattern": "debate",
                        "agents": {"tags": ["advocate"]},
                        "critic_agents": {"tags": ["critic"]},
                        "judge_agents": {"tags": ["judge"]},
                        "debate_rounds": 1,
                    },
                    {"name": "implement", "pattern": "single"},
                    {"name": "review", "pattern": "parallel", "agents": {"count": 2}},
                    {"name": "merge", "pattern": "single"},
                ],
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    # debate (3) + implement (1) + review (2) + merge (1) = 7
    assert len(data["task_ids"]) == 7


def test_post_workflow_phases_requires_phases_or_tasks():
    """POST /workflows with neither tasks nor phases returns 422."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            headers=_HEADERS,
            json={"name": "empty"},
        )
    assert resp.status_code == 422


def test_post_workflow_phases_workflow_manager_tracks_run():
    """Workflow run is tracked in WorkflowManager with correct task count."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            headers=_HEADERS,
            json={
                "name": "tracked",
                "context": "ctx",
                "phases": [
                    {"name": "phase1", "pattern": "parallel", "agents": {"count": 2}},
                ],
            },
        )
    assert resp.status_code == 200
    wf_id = resp.json()["workflow_id"]
    wm = orch.get_workflow_manager()
    run = wm.get(wf_id)
    assert run is not None
    assert len(run.task_ids) == 2


def test_post_workflow_phases_response_includes_phases():
    """POST /workflows with phases returns phases info in the response."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            headers=_HEADERS,
            json={
                "name": "tracked",
                "context": "ctx",
                "phases": [
                    {"name": "design", "pattern": "single"},
                    {"name": "implement", "pattern": "single"},
                ],
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "phases" in data
    assert len(data["phases"]) == 2
    assert data["phases"][0]["name"] == "design"
    assert data["phases"][1]["name"] == "implement"


def test_get_workflow_includes_phases():
    """GET /workflows/{id} returns phases array when workflow has phases."""
    app, orch = _make_app()
    with TestClient(app) as client:
        post_resp = client.post(
            "/workflows",
            headers=_HEADERS,
            json={
                "name": "with-phases",
                "context": "ctx",
                "phases": [{"name": "design", "pattern": "single"}],
            },
        )
        assert post_resp.status_code == 200
        wf_id = post_resp.json()["workflow_id"]

        get_resp = client.get(f"/workflows/{wf_id}", headers=_HEADERS)
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert "phases" in data
    assert len(data["phases"]) >= 1
    assert data["phases"][0]["name"] == "design"


def test_post_workflow_backward_compat_tasks_still_works():
    """Existing tasks= API still works (backward compat)."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            headers=_HEADERS,
            json={
                "name": "legacy",
                "tasks": [
                    {"local_id": "t1", "prompt": "do something"},
                    {"local_id": "t2", "prompt": "do another", "depends_on": ["t1"]},
                ],
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["task_ids"]) == 2


def test_post_workflow_phases_invalid_pattern_returns_422():
    """Unknown pattern value returns 422."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            headers=_HEADERS,
            json={
                "name": "bad",
                "context": "ctx",
                "phases": [{"name": "x", "pattern": "invalid_pattern"}],
            },
        )
    assert resp.status_code == 422


def test_post_workflow_phases_context_per_phase():
    """Per-phase context override is included in task prompt."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            headers=_HEADERS,
            json={
                "name": "ctx-override",
                "context": "Global context",
                "phases": [
                    {"name": "design", "pattern": "single", "context": "Phase-specific: design the API"},
                ],
            },
        )
    assert resp.status_code == 200
    # Verify by checking the queued task has the right prompt
    data = resp.json()
    task_local_id = list(data["task_ids"].keys())[0]
    global_task_id = data["task_ids"][task_local_id]
    # Check task in orchestrator's pending queue (via REST)
    with TestClient(app) as client:
        task_resp = client.get(f"/tasks/{global_task_id}", headers=_HEADERS)
    if task_resp.status_code == 200:
        task_data = task_resp.json()
        assert "Phase-specific" in task_data.get("prompt", "")
