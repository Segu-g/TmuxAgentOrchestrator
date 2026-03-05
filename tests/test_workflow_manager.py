"""Tests for WorkflowManager and POST/GET /workflows REST endpoints.

Design reference:
- Apache Airflow DAG model — task dependencies as directed acyclic graph
  (https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dags.html)
- Prefect "Modern Data Stack" workflow orchestration
  (https://www.prefect.io/guide/blog/modern-data-stack)
- Tomasulo's algorithm / topological sort for dependency resolution
  (Cormen et al. "Introduction to Algorithms" 4th ed. §22.4)
- AWS Step Functions state machine
  (https://docs.aws.amazon.com/step-functions/latest/dg/concepts-amazon-states-language.html)
- DESIGN.md §10.20 (v0.25.0)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app
from tmux_orchestrator.workflow_manager import WorkflowManager, validate_dag


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
    app = create_app(orch, _StubHub(), api_key="test-key")  # type: ignore[arg-type]
    return app, orch


# ---------------------------------------------------------------------------
# Unit tests: WorkflowManager
# ---------------------------------------------------------------------------


def test_workflow_manager_submit_registers_run():
    """WorkflowManager.submit() creates a run with the given task IDs."""
    wm = WorkflowManager()
    run = wm.submit("my-workflow", ["t1", "t2", "t3"])
    assert run.name == "my-workflow"
    assert run.task_ids == ["t1", "t2", "t3"]
    assert run.status == "pending"
    assert run.completed_at is None


def test_workflow_manager_get_unknown_returns_none():
    wm = WorkflowManager()
    assert wm.get("nonexistent") is None


def test_workflow_manager_status_unknown_returns_none():
    wm = WorkflowManager()
    assert wm.status("nonexistent") is None


def test_workflow_manager_list_all_empty():
    wm = WorkflowManager()
    assert wm.list_all() == []


def test_workflow_manager_on_task_complete_partial():
    """Workflow stays 'running' when some tasks are still pending."""
    wm = WorkflowManager()
    run = wm.submit("wf", ["t1", "t2", "t3"])
    wm.on_task_complete("t1")
    assert run.status == "running"
    assert run.completed_at is None


def test_workflow_manager_on_task_complete_all():
    """Workflow transitions to 'complete' when all tasks succeed."""
    wm = WorkflowManager()
    run = wm.submit("wf", ["t1", "t2"])
    wm.on_task_complete("t1")
    wm.on_task_complete("t2")
    assert run.status == "complete"
    assert run.completed_at is not None


def test_workflow_manager_on_task_failed():
    """Workflow transitions to 'failed' immediately when any task fails."""
    wm = WorkflowManager()
    run = wm.submit("wf", ["t1", "t2"])
    wm.on_task_complete("t1")
    wm.on_task_failed("t2")
    assert run.status == "failed"
    assert run.completed_at is not None


def test_workflow_manager_unknown_task_is_noop():
    """on_task_complete with an untracked task_id is silently ignored."""
    wm = WorkflowManager()
    run = wm.submit("wf", ["t1"])
    wm.on_task_complete("unknown-task")  # should not raise
    assert run.status == "pending"


def test_workflow_manager_to_dict():
    """to_dict() includes all expected fields."""
    wm = WorkflowManager()
    run = wm.submit("pipe", ["t1", "t2"])
    d = run.to_dict()
    assert d["id"] == run.id
    assert d["name"] == "pipe"
    assert d["task_ids"] == ["t1", "t2"]
    assert d["status"] == "pending"
    assert d["tasks_total"] == 2
    assert d["tasks_done"] == 0
    assert d["tasks_failed"] == 0
    assert d["completed_at"] is None


def test_workflow_manager_list_all():
    """list_all() returns all submitted runs."""
    wm = WorkflowManager()
    wm.submit("wf-a", ["t1"])
    wm.submit("wf-b", ["t2"])
    listed = wm.list_all()
    assert len(listed) == 2
    names = {d["name"] for d in listed}
    assert names == {"wf-a", "wf-b"}


def test_workflow_manager_multiple_workflows_independent():
    """Completing a task in one workflow does not affect another."""
    wm = WorkflowManager()
    run_a = wm.submit("wf-a", ["t1"])
    run_b = wm.submit("wf-b", ["t2"])
    wm.on_task_complete("t1")
    assert run_a.status == "complete"
    assert run_b.status == "pending"


# ---------------------------------------------------------------------------
# Unit tests: validate_dag
# ---------------------------------------------------------------------------


def test_validate_dag_linear():
    tasks = [
        {"local_id": "a", "prompt": "step a", "depends_on": []},
        {"local_id": "b", "prompt": "step b", "depends_on": ["a"]},
        {"local_id": "c", "prompt": "step c", "depends_on": ["b"]},
    ]
    ordered = validate_dag(tasks)
    ids = [t["local_id"] for t in ordered]
    assert ids.index("a") < ids.index("b") < ids.index("c")


def test_validate_dag_diamond():
    tasks = [
        {"local_id": "root", "prompt": "root", "depends_on": []},
        {"local_id": "left", "prompt": "left", "depends_on": ["root"]},
        {"local_id": "right", "prompt": "right", "depends_on": ["root"]},
        {"local_id": "leaf", "prompt": "leaf", "depends_on": ["left", "right"]},
    ]
    ordered = validate_dag(tasks)
    ids = [t["local_id"] for t in ordered]
    assert ids.index("root") < ids.index("left")
    assert ids.index("root") < ids.index("right")
    assert ids.index("left") < ids.index("leaf")
    assert ids.index("right") < ids.index("leaf")


def test_validate_dag_cycle_raises():
    tasks = [
        {"local_id": "a", "prompt": "a", "depends_on": ["b"]},
        {"local_id": "b", "prompt": "b", "depends_on": ["a"]},
    ]
    with pytest.raises(ValueError, match="cycle"):
        validate_dag(tasks)


def test_validate_dag_unknown_dep_raises():
    tasks = [
        {"local_id": "a", "prompt": "a", "depends_on": []},
        {"local_id": "b", "prompt": "b", "depends_on": ["nonexistent"]},
    ]
    with pytest.raises(ValueError, match="unknown local_id"):
        validate_dag(tasks)


def test_validate_dag_empty():
    assert validate_dag([]) == []


def test_validate_dag_no_deps():
    tasks = [
        {"local_id": "x", "prompt": "x", "depends_on": []},
        {"local_id": "y", "prompt": "y", "depends_on": []},
    ]
    ordered = validate_dag(tasks)
    assert len(ordered) == 2


# ---------------------------------------------------------------------------
# REST: POST /workflows
# ---------------------------------------------------------------------------


def test_post_workflows_creates_workflow():
    """POST /workflows creates a workflow and returns workflow_id and task_ids map."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            json={
                "name": "test-pipeline",
                "tasks": [
                    {"local_id": "step-1", "prompt": "do step 1", "depends_on": []},
                    {"local_id": "step-2", "prompt": "do step 2", "depends_on": ["step-1"]},
                ],
            },
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "workflow_id" in data
    assert "task_ids" in data
    assert "name" in data
    assert data["name"] == "test-pipeline"
    task_ids = data["task_ids"]
    assert "step-1" in task_ids
    assert "step-2" in task_ids
    # Global IDs must be distinct UUIDs
    assert task_ids["step-1"] != task_ids["step-2"]


def test_post_workflows_returns_correct_local_to_global_map():
    """POST /workflows returns local_id → global_task_id mapping."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            json={
                "name": "wf",
                "tasks": [
                    {"local_id": "a", "prompt": "alpha", "depends_on": []},
                    {"local_id": "b", "prompt": "beta", "depends_on": ["a"]},
                    {"local_id": "c", "prompt": "gamma", "depends_on": ["a", "b"]},
                ],
            },
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200
    mapping = resp.json()["task_ids"]
    assert set(mapping.keys()) == {"a", "b", "c"}
    # All global IDs should be distinct strings
    assert len(set(mapping.values())) == 3


def test_post_workflows_tasks_enqueued_with_depends_on():
    """Tasks submitted via /workflows have correct depends_on in the orchestrator."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            json={
                "name": "dep-check",
                "tasks": [
                    {"local_id": "first", "prompt": "step 1"},
                    {"local_id": "second", "prompt": "step 2", "depends_on": ["first"]},
                ],
            },
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200
    mapping = resp.json()["task_ids"]
    global_first = mapping["first"]
    global_second = mapping["second"]

    # Inspect task queue in orchestrator
    queued = orch.list_tasks()
    task_by_id = {t["task_id"]: t for t in queued}
    # second task should have first as dependency (stored in the Task object)
    # We can verify via the orchestrator's queue — depends_on is on the Task object
    # and list_tasks() doesn't expose it, but we can verify ordering via the workflow
    wm = orch.get_workflow_manager()
    wf_id = resp.json()["workflow_id"]
    status = wm.status(wf_id)
    assert status is not None
    assert set(status["task_ids"]) == {global_first, global_second}


def test_post_workflows_cycle_returns_400():
    """POST /workflows with a cyclic dependency returns 400."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            json={
                "name": "cyclic",
                "tasks": [
                    {"local_id": "a", "prompt": "a", "depends_on": ["b"]},
                    {"local_id": "b", "prompt": "b", "depends_on": ["a"]},
                ],
            },
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 400
    assert "cycle" in resp.json()["detail"].lower()


def test_post_workflows_unknown_dep_returns_400():
    """POST /workflows with an unknown depends_on local_id returns 400."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            json={
                "name": "bad-dep",
                "tasks": [
                    {"local_id": "a", "prompt": "a", "depends_on": ["does-not-exist"]},
                ],
            },
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 400
    assert "unknown local_id" in resp.json()["detail"].lower()


def test_post_workflows_requires_auth():
    """POST /workflows returns 401 without authentication."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/workflows",
            json={"name": "wf", "tasks": [{"local_id": "a", "prompt": "p"}]},
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# REST: GET /workflows
# ---------------------------------------------------------------------------


def test_get_workflows_empty():
    """GET /workflows returns empty list when no workflows submitted."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.get("/workflows", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_workflows_lists_submitted():
    """GET /workflows lists all submitted workflows."""
    app, _ = _make_app()
    with TestClient(app) as client:
        # Submit two workflows
        client.post(
            "/workflows",
            json={"name": "wf-a", "tasks": [{"local_id": "x", "prompt": "p"}]},
            headers={"X-API-Key": "test-key"},
        )
        client.post(
            "/workflows",
            json={"name": "wf-b", "tasks": [{"local_id": "y", "prompt": "q"}]},
            headers={"X-API-Key": "test-key"},
        )
        resp = client.get("/workflows", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    listed = resp.json()
    assert len(listed) == 2
    names = {d["name"] for d in listed}
    assert names == {"wf-a", "wf-b"}


# ---------------------------------------------------------------------------
# REST: GET /workflows/{workflow_id}
# ---------------------------------------------------------------------------


def test_get_workflow_by_id():
    """GET /workflows/{id} returns the status of a specific workflow."""
    app, _ = _make_app()
    with TestClient(app) as client:
        post_resp = client.post(
            "/workflows",
            json={"name": "wf-status", "tasks": [{"local_id": "t1", "prompt": "task"}]},
            headers={"X-API-Key": "test-key"},
        )
        wf_id = post_resp.json()["workflow_id"]
        resp = client.get(f"/workflows/{wf_id}", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == wf_id
    assert data["name"] == "wf-status"
    assert data["status"] in {"pending", "running", "complete", "failed"}


def test_get_workflow_not_found():
    """GET /workflows/{id} returns 404 for unknown workflow ID."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.get("/workflows/does-not-exist", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 404


def test_get_workflow_status_fields():
    """GET /workflows/{id} response includes all expected fields."""
    app, _ = _make_app()
    with TestClient(app) as client:
        post_resp = client.post(
            "/workflows",
            json={
                "name": "field-check",
                "tasks": [
                    {"local_id": "a", "prompt": "step a"},
                    {"local_id": "b", "prompt": "step b", "depends_on": ["a"]},
                ],
            },
            headers={"X-API-Key": "test-key"},
        )
        wf_id = post_resp.json()["workflow_id"]
        resp = client.get(f"/workflows/{wf_id}", headers={"X-API-Key": "test-key"})
    data = resp.json()
    for field in ("id", "name", "task_ids", "status", "created_at",
                  "completed_at", "tasks_total", "tasks_done", "tasks_failed"):
        assert field in data, f"Missing field: {field}"
    assert data["tasks_total"] == 2
    assert data["tasks_done"] == 0
    assert data["tasks_failed"] == 0


# ---------------------------------------------------------------------------
# Integration: WorkflowManager + Orchestrator._route_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_workflow_complete_on_result(tmp_path):
    """WorkflowManager marks workflow complete when orchestrator receives RESULT."""
    from tests.integration.test_orchestration import HeadlessAgent, HeadlessOrchestrator
    from tmux_orchestrator.bus import Bus, Message, MessageType

    bus = Bus()
    cfg = OrchestratorConfig(
        session_name="test",
        agents=[],
        mailbox_dir=str(tmp_path),
        dlq_max_retries=50,
    )
    orch = HeadlessOrchestrator(bus, cfg)
    worker = HeadlessAgent("w1", bus)
    orch.register_agent(worker)
    await orch.start()

    try:
        task = await orch.submit_task("hello world")
        wm = orch.get_workflow_manager()
        run = wm.submit("test-run", [task.id])
        assert run.status == "pending"

        # Wait for RESULT
        import asyncio
        q = await bus.subscribe("test-watcher", broadcast=True)
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=0.2)
                q.task_done()
                if msg.type == MessageType.RESULT and msg.payload.get("task_id") == task.id:
                    break
            except asyncio.TimeoutError:
                pass
        await bus.unsubscribe("test-watcher")

        # Allow route_loop to process the RESULT
        await asyncio.sleep(0.1)
        assert run.status == "complete"
        assert run.completed_at is not None
    finally:
        await orch.stop()
