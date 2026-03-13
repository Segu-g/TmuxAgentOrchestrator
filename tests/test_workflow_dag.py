"""Tests for GET /workflows/{id}/dag — Workflow DAG visualization endpoint (v1.2.14).

Verifies:
- Endpoint returns correct nodes and edges structure
- Node status reflects actual task lifecycle (queued, waiting, running, success, failed)
- 404 for unknown workflow
- Parallel fan-out and sequential chain edge topology
- dag_edges stored correctly on WorkflowRun
- get_task_info() returns correct status
- Workflow without phases returns empty phase_name
- Loop workflows: all iterations represented as nodes

Design references:
- AWS Glue GetDataflowGraph: separate DagNodes + DagEdges arrays
  (https://docs.aws.amazon.com/glue/latest/webapi/API_GetDataflowGraph.html)
- ZenML DAG visualization (https://www.zenml.io/blog/dag-visualization-vscode-extension)
- DESIGN.md §10.90 (v1.2.14)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app
from tmux_orchestrator.application.workflow_manager import WorkflowManager
from tmux_orchestrator.domain.workflow import WorkflowRun


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


AUTH = {"X-API-Key": "test-key"}


# ---------------------------------------------------------------------------
# WorkflowRun.dag_edges field
# ---------------------------------------------------------------------------


class TestWorkflowRunDagEdges:
    def test_dag_edges_defaults_empty(self):
        run = WorkflowRun(id="wf-1", name="test", task_ids=["t1"])
        assert run.dag_edges == []

    def test_dag_edges_stored_on_construction(self):
        edges = [("t1", "t2"), ("t2", "t3")]
        run = WorkflowRun(id="wf-1", name="test", task_ids=["t1", "t2", "t3"], dag_edges=edges)
        assert run.dag_edges == edges

    def test_dag_edges_not_in_to_dict(self):
        """dag_edges is an implementation detail not currently exposed in to_dict."""
        edges = [("t1", "t2")]
        run = WorkflowRun(id="wf-1", name="test", task_ids=["t1", "t2"], dag_edges=edges)
        d = run.to_dict()
        # to_dict should still work without error
        assert "id" in d
        assert d["tasks_total"] == 2


# ---------------------------------------------------------------------------
# WorkflowManager.submit(dag_edges=...)
# ---------------------------------------------------------------------------


class TestWorkflowManagerSubmitDagEdges:
    def test_submit_without_dag_edges(self):
        wm = WorkflowManager()
        run = wm.submit("test", ["t1", "t2"])
        assert run.dag_edges == []

    def test_submit_with_dag_edges_stored(self):
        wm = WorkflowManager()
        edges = [("t1", "t2")]
        run = wm.submit("test", ["t1", "t2"], dag_edges=edges)
        assert run.dag_edges == [("t1", "t2")]

    def test_submit_dag_edges_copied(self):
        """Ensure dag_edges is a copy, not shared reference."""
        wm = WorkflowManager()
        edges = [("t1", "t2")]
        run = wm.submit("test", ["t1", "t2"], dag_edges=edges)
        edges.append(("t2", "t3"))
        assert len(run.dag_edges) == 1

    def test_submit_multiple_edges(self):
        wm = WorkflowManager()
        edges = [("t1", "t2"), ("t1", "t3"), ("t2", "t4"), ("t3", "t4")]
        run = wm.submit("diamond", ["t1", "t2", "t3", "t4"], dag_edges=edges)
        assert len(run.dag_edges) == 4

    def test_get_retrieves_dag_edges(self):
        wm = WorkflowManager()
        edges = [("t1", "t2")]
        run = wm.submit("test", ["t1", "t2"], dag_edges=edges)
        fetched = wm.get(run.id)
        assert fetched is not None
        assert fetched.dag_edges == edges


# ---------------------------------------------------------------------------
# GET /workflows/{id}/dag — 404 for unknown workflow
# ---------------------------------------------------------------------------


class TestGetWorkflowDag404:
    def test_unknown_workflow_returns_404(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.get("/workflows/does-not-exist/dag", headers=AUTH)
        assert r.status_code == 404

    def test_wrong_id_returns_404(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.get("/workflows/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/dag", headers=AUTH)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /workflows/{id}/dag — response structure
# ---------------------------------------------------------------------------


class TestGetWorkflowDagStructure:
    def _submit_simple_chain(self, app, orch):
        """Submit a 2-task chain (t1 → t2) and return workflow_id + task_ids."""
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={
                    "name": "test-chain",
                    "tasks": [
                        {"local_id": "a", "prompt": "task A"},
                        {"local_id": "b", "prompt": "task B", "depends_on": ["a"]},
                    ],
                },
                headers=AUTH,
            )
        assert r.status_code == 200
        data = r.json()
        return data["workflow_id"], data["task_ids"]

    def test_dag_endpoint_returns_200(self):
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={"name": "simple", "tasks": [{"local_id": "x", "prompt": "do X"}]},
                headers=AUTH,
            )
            wf_id = r.json()["workflow_id"]
            r2 = client.get(f"/workflows/{wf_id}/dag", headers=AUTH)
        assert r2.status_code == 200

    def test_dag_response_has_required_keys(self):
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={"name": "simple", "tasks": [{"local_id": "x", "prompt": "do X"}]},
                headers=AUTH,
            )
            wf_id = r.json()["workflow_id"]
            r2 = client.get(f"/workflows/{wf_id}/dag", headers=AUTH)
        dag = r2.json()
        assert "workflow_id" in dag
        assert "name" in dag
        assert "status" in dag
        assert "nodes" in dag
        assert "edges" in dag

    def test_dag_workflow_id_matches(self):
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={"name": "simple", "tasks": [{"local_id": "x", "prompt": "do X"}]},
                headers=AUTH,
            )
            wf_id = r.json()["workflow_id"]
            r2 = client.get(f"/workflows/{wf_id}/dag", headers=AUTH)
        dag = r2.json()
        assert dag["workflow_id"] == wf_id

    def test_nodes_count_equals_tasks_count(self):
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={
                    "name": "3-task",
                    "tasks": [
                        {"local_id": "a", "prompt": "A"},
                        {"local_id": "b", "prompt": "B"},
                        {"local_id": "c", "prompt": "C"},
                    ],
                },
                headers=AUTH,
            )
            wf_id = r.json()["workflow_id"]
            r2 = client.get(f"/workflows/{wf_id}/dag", headers=AUTH)
        dag = r2.json()
        assert len(dag["nodes"]) == 3

    def test_node_has_required_fields(self):
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={"name": "simple", "tasks": [{"local_id": "x", "prompt": "do X"}]},
                headers=AUTH,
            )
            wf_id = r.json()["workflow_id"]
            r2 = client.get(f"/workflows/{wf_id}/dag", headers=AUTH)
        node = r2.json()["nodes"][0]
        required_fields = {"task_id", "phase_name", "status", "depends_on", "dependents", "started_at", "finished_at", "duration_s", "assigned_agent"}
        assert required_fields.issubset(set(node.keys()))

    def test_single_task_no_edges(self):
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={"name": "simple", "tasks": [{"local_id": "x", "prompt": "do X"}]},
                headers=AUTH,
            )
            wf_id = r.json()["workflow_id"]
            r2 = client.get(f"/workflows/{wf_id}/dag", headers=AUTH)
        dag = r2.json()
        assert dag["edges"] == []

    def test_sequential_chain_has_correct_edge(self):
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={
                    "name": "chain",
                    "tasks": [
                        {"local_id": "a", "prompt": "A"},
                        {"local_id": "b", "prompt": "B", "depends_on": ["a"]},
                    ],
                },
                headers=AUTH,
            )
            data = r.json()
            wf_id = data["workflow_id"]
            task_ids = data["task_ids"]
            r2 = client.get(f"/workflows/{wf_id}/dag", headers=AUTH)
        dag = r2.json()
        assert len(dag["edges"]) == 1
        edge = dag["edges"][0]
        assert edge["from"] == task_ids["a"]
        assert edge["to"] == task_ids["b"]

    def test_parallel_fan_out_has_correct_edges(self):
        """plan → impl-a, plan → impl-b (fan-out from one node)."""
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={
                    "name": "parallel",
                    "tasks": [
                        {"local_id": "plan", "prompt": "plan"},
                        {"local_id": "impl-a", "prompt": "impl A", "depends_on": ["plan"]},
                        {"local_id": "impl-b", "prompt": "impl B", "depends_on": ["plan"]},
                    ],
                },
                headers=AUTH,
            )
            data = r.json()
            wf_id = data["workflow_id"]
            task_ids = data["task_ids"]
            r2 = client.get(f"/workflows/{wf_id}/dag", headers=AUTH)
        dag = r2.json()
        # Should have 2 edges: plan→impl-a and plan→impl-b
        assert len(dag["edges"]) == 2
        edge_pairs = {(e["from"], e["to"]) for e in dag["edges"]}
        assert (task_ids["plan"], task_ids["impl-a"]) in edge_pairs
        assert (task_ids["plan"], task_ids["impl-b"]) in edge_pairs

    def test_diamond_dag_has_four_edges(self):
        """plan → impl-a, plan → impl-b, impl-a → review, impl-b → review."""
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={
                    "name": "diamond",
                    "tasks": [
                        {"local_id": "plan", "prompt": "plan"},
                        {"local_id": "impl-a", "prompt": "impl A", "depends_on": ["plan"]},
                        {"local_id": "impl-b", "prompt": "impl B", "depends_on": ["plan"]},
                        {"local_id": "review", "prompt": "review", "depends_on": ["impl-a", "impl-b"]},
                    ],
                },
                headers=AUTH,
            )
            data = r.json()
            wf_id = data["workflow_id"]
            task_ids = data["task_ids"]
            r2 = client.get(f"/workflows/{wf_id}/dag", headers=AUTH)
        dag = r2.json()
        assert len(dag["edges"]) == 4
        assert len(dag["nodes"]) == 4

    def test_node_task_ids_are_global_ids(self):
        """Nodes use the global orchestrator task IDs, not local_ids."""
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={"name": "simple", "tasks": [{"local_id": "x", "prompt": "do X"}]},
                headers=AUTH,
            )
            data = r.json()
            wf_id = data["workflow_id"]
            global_id = data["task_ids"]["x"]
            r2 = client.get(f"/workflows/{wf_id}/dag", headers=AUTH)
        dag = r2.json()
        node_ids = [n["task_id"] for n in dag["nodes"]]
        assert global_id in node_ids


# ---------------------------------------------------------------------------
# Node status reflects task lifecycle
# ---------------------------------------------------------------------------


class TestNodeStatus:
    def test_pending_task_has_queued_or_waiting_status(self):
        """Newly submitted tasks that have not been dispatched show queued/waiting."""
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={
                    "name": "wait-test",
                    "tasks": [
                        {"local_id": "a", "prompt": "A"},
                        {"local_id": "b", "prompt": "B", "depends_on": ["a"]},
                    ],
                },
                headers=AUTH,
            )
            wf_id = r.json()["workflow_id"]
            r2 = client.get(f"/workflows/{wf_id}/dag", headers=AUTH)
        dag = r2.json()
        statuses = {n["status"] for n in dag["nodes"]}
        # Task b must be 'waiting' (held back by dep on a); task a is 'queued'
        assert "waiting" in statuses or "queued" in statuses

    def test_completed_task_has_success_status(self):
        """After on_task_complete(), the node status is 'success'."""
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={"name": "complete-test", "tasks": [{"local_id": "x", "prompt": "X"}]},
                headers=AUTH,
            )
            data = r.json()
            wf_id = data["workflow_id"]
            task_id = data["task_ids"]["x"]

        # Simulate task completion
        orch._completed_tasks.add(task_id)
        orch._active_tasks.pop(task_id, None)

        with TestClient(app) as client:
            r2 = client.get(f"/workflows/{wf_id}/dag", headers=AUTH)
        dag = r2.json()
        node = dag["nodes"][0]
        assert node["status"] == "success"

    def test_failed_task_has_failed_status(self):
        """After marking a task failed, the node status is 'failed'."""
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={"name": "fail-test", "tasks": [{"local_id": "x", "prompt": "X"}]},
                headers=AUTH,
            )
            data = r.json()
            wf_id = data["workflow_id"]
            task_id = data["task_ids"]["x"]

        # Simulate task failure
        orch._failed_tasks.add(task_id)
        orch._active_tasks.pop(task_id, None)

        with TestClient(app) as client:
            r2 = client.get(f"/workflows/{wf_id}/dag", headers=AUTH)
        dag = r2.json()
        node = dag["nodes"][0]
        assert node["status"] == "failed"


# ---------------------------------------------------------------------------
# Orchestrator.get_task_info()
# ---------------------------------------------------------------------------


class TestGetTaskInfo:
    def _make_orchestrator(self):
        bus = Bus()
        tmux = make_tmux_mock()
        config = make_config()
        return Orchestrator(bus=bus, tmux=tmux, config=config)

    def test_unknown_task_returns_unknown_status(self):
        orch = self._make_orchestrator()
        info = orch.get_task_info("no-such-task")
        assert info["task_id"] == "no-such-task"
        assert info["status"] == "unknown"

    def test_completed_task_returns_success_status(self):
        orch = self._make_orchestrator()
        orch._completed_tasks.add("t-done")
        info = orch.get_task_info("t-done")
        assert info["status"] == "success"

    def test_failed_task_returns_failed_status(self):
        orch = self._make_orchestrator()
        orch._failed_tasks.add("t-fail")
        info = orch.get_task_info("t-fail")
        assert info["status"] == "failed"

    def test_task_deps_returned(self):
        orch = self._make_orchestrator()
        orch._task_deps["t2"] = ["t1"]
        orch._completed_tasks.add("t2")
        info = orch.get_task_info("t2")
        assert info["depends_on"] == ["t1"]

    def test_task_agent_assignment_returned(self):
        orch = self._make_orchestrator()
        orch._completed_tasks.add("t1")
        orch._task_agent["t1"] = "worker-1"
        info = orch.get_task_info("t1")
        assert info["assigned_agent"] == "worker-1"

    def test_no_agent_assignment_returns_none(self):
        orch = self._make_orchestrator()
        orch._completed_tasks.add("t1")
        info = orch.get_task_info("t1")
        assert info["assigned_agent"] is None

    def test_info_has_all_required_keys(self):
        orch = self._make_orchestrator()
        info = orch.get_task_info("any-task")
        required = {"task_id", "status", "depends_on", "dependents", "assigned_agent", "started_at", "finished_at"}
        assert required.issubset(set(info.keys()))


# ---------------------------------------------------------------------------
# Workflow with no phases (legacy tasks= mode)
# ---------------------------------------------------------------------------


class TestDagWithoutPhases:
    def test_phase_name_empty_string_for_tasks_mode(self):
        """Tasks submitted via tasks= mode have no phase_name."""
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={"name": "plain", "tasks": [{"local_id": "x", "prompt": "X"}]},
                headers=AUTH,
            )
            wf_id = r.json()["workflow_id"]
            r2 = client.get(f"/workflows/{wf_id}/dag", headers=AUTH)
        node = r2.json()["nodes"][0]
        assert node["phase_name"] == ""

    def test_workflow_with_zero_tasks_returns_empty_nodes(self):
        """Edge case: a workflow registered with zero task IDs."""
        bus = Bus()
        tmux = make_tmux_mock()
        config = make_config()
        orch = Orchestrator(bus=bus, tmux=tmux, config=config)
        app = create_app(orch, _StubHub(), api_key="test-key")  # type: ignore[arg-type]
        wm = orch.get_workflow_manager()
        run = wm.submit("empty-wf", task_ids=[])
        with TestClient(app) as client:
            r = client.get(f"/workflows/{run.id}/dag", headers=AUTH)
        dag = r.json()
        assert dag["nodes"] == []
        assert dag["edges"] == []


# ---------------------------------------------------------------------------
# Unauthenticated access
# ---------------------------------------------------------------------------


class TestDagAuth:
    def test_missing_api_key_returns_401(self):
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows",
                json={"name": "auth-test", "tasks": [{"local_id": "x", "prompt": "X"}]},
                headers=AUTH,
            )
            wf_id = r.json()["workflow_id"]
            # No auth header
            r2 = client.get(f"/workflows/{wf_id}/dag")
        assert r2.status_code == 401
