"""Tests for POST /workflows/peer-review — 3-agent parallel Peer Review workflow.

DAG topology:
  impl-a ──┐
           ├──▶ reviewer
  impl-b ──┘

Design references:
- AgentReview: EMNLP 2024 arXiv:2406.12708
- arXiv:2505.16339 "Rethinking Code Review Workflows with LLM Assistance" (2025)
- DESIGN.md §10.99 (v1.2.24)
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app


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


AUTH = {"X-API-Key": "test-key"}


# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------


class TestPeerReviewWorkflowSchema:
    def test_missing_feature_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post("/workflows/peer-review", json={}, headers=AUTH)
        assert r.status_code == 422

    def test_empty_feature_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post("/workflows/peer-review", json={"feature": ""}, headers=AUTH)
        assert r.status_code == 422

    def test_whitespace_feature_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review", json={"feature": "   "}, headers=AUTH
            )
        assert r.status_code == 422

    def test_missing_auth_returns_401(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post("/workflows/peer-review", json={"feature": "a stack"})
        assert r.status_code == 401

    def test_wrong_api_key_returns_401(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a stack"},
                headers={"X-API-Key": "wrong"},
            )
        assert r.status_code == 401

    def test_valid_request_returns_200(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a stack data structure"},
                headers=AUTH,
            )
        assert r.status_code == 200

    def test_valid_request_with_all_fields(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={
                    "feature": "a LRU cache",
                    "language": "python",
                    "impl_a_tags": ["dev"],
                    "impl_b_tags": ["dev"],
                    "reviewer_tags": ["reviewer"],
                    "agent_timeout": 600,
                    "scratchpad_prefix": "test_prefix",
                    "reply_to": None,
                },
                headers=AUTH,
            )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Response structure tests
# ---------------------------------------------------------------------------


class TestPeerReviewWorkflowResponse:
    def test_response_has_workflow_id(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a stack"},
                headers=AUTH,
            )
        data = r.json()
        assert "workflow_id" in data
        assert isinstance(data["workflow_id"], str)
        assert len(data["workflow_id"]) > 0

    def test_response_has_name(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a stack"},
                headers=AUTH,
            )
        data = r.json()
        assert "name" in data
        assert "peer-review" in data["name"]
        assert "a stack" in data["name"]

    def test_response_has_task_ids(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a queue"},
                headers=AUTH,
            )
        data = r.json()
        assert "task_ids" in data
        task_ids = data["task_ids"]
        assert "impl_a" in task_ids
        assert "impl_b" in task_ids
        assert "reviewer" in task_ids

    def test_response_has_scratchpad_prefix(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a deque"},
                headers=AUTH,
            )
        data = r.json()
        assert "scratchpad_prefix" in data
        assert isinstance(data["scratchpad_prefix"], str)
        assert len(data["scratchpad_prefix"]) > 0

    def test_three_distinct_task_ids(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a linked list"},
                headers=AUTH,
            )
        data = r.json()
        task_ids = data["task_ids"]
        ids = list(task_ids.values())
        assert len(ids) == 3
        assert len(set(ids)) == 3, "All three task IDs must be distinct"

    def test_custom_scratchpad_prefix_used(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a counter", "scratchpad_prefix": "my_custom_prefix"},
                headers=AUTH,
            )
        data = r.json()
        assert data["scratchpad_prefix"] == "my_custom_prefix"

    def test_auto_generated_prefix_contains_peerreview(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a tree"},
                headers=AUTH,
            )
        data = r.json()
        # Auto-generated prefix should start with "peerreview_"
        assert data["scratchpad_prefix"].startswith("peerreview_")


# ---------------------------------------------------------------------------
# Task submission + dependency tests (via GET /tasks REST API)
# ---------------------------------------------------------------------------


class TestPeerReviewWorkflowTaskSubmission:
    def test_three_tasks_in_tasks_list(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a binary search"},
                headers=AUTH,
            )
            data = r.json()
            task_ids = set(data["task_ids"].values())
            tasks_resp = client.get("/tasks", headers=AUTH)
        all_task_ids = {t["task_id"] for t in tasks_resp.json()}
        assert task_ids.issubset(all_task_ids)

    def test_reviewer_depends_on_both_implementations(self):
        """Reviewer task must declare depends_on both impl_a and impl_b IDs."""
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a min-heap"},
                headers=AUTH,
            )
            data = r.json()
            impl_a_id = data["task_ids"]["impl_a"]
            impl_b_id = data["task_ids"]["impl_b"]
            reviewer_id = data["task_ids"]["reviewer"]

            tasks_resp = client.get("/tasks", headers=AUTH)
        tasks = {t["task_id"]: t for t in tasks_resp.json()}
        reviewer_task = tasks[reviewer_id]
        assert impl_a_id in reviewer_task["depends_on"]
        assert impl_b_id in reviewer_task["depends_on"]

    def test_impl_a_has_queued_status(self):
        """impl-a has no dependencies so it is immediately queued (not waiting)."""
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a hash map"},
                headers=AUTH,
            )
            data = r.json()
            impl_a_id = data["task_ids"]["impl_a"]
            tasks_resp = client.get("/tasks", headers=AUTH)
        tasks = {t["task_id"]: t for t in tasks_resp.json()}
        assert tasks[impl_a_id]["status"] == "queued"

    def test_impl_b_has_queued_status(self):
        """impl-b has no dependencies so it is immediately queued (not waiting)."""
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a circular buffer"},
                headers=AUTH,
            )
            data = r.json()
            impl_b_id = data["task_ids"]["impl_b"]
            tasks_resp = client.get("/tasks", headers=AUTH)
        tasks = {t["task_id"]: t for t in tasks_resp.json()}
        assert tasks[impl_b_id]["status"] == "queued"

    def test_workflow_registered_in_workflow_manager(self):
        """The workflow run must be registered in the WorkflowManager."""
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a sorting algorithm"},
                headers=AUTH,
            )
        data = r.json()
        wf_id = data["workflow_id"]
        wm = orch.get_workflow_manager()
        run = wm.get(wf_id)
        assert run is not None
        assert run.id == wf_id


# ---------------------------------------------------------------------------
# Prompt content tests (via GET /tasks REST API)
# ---------------------------------------------------------------------------


class TestPeerReviewWorkflowPrompts:
    def _get_task_map(self, feature: str = "a stack", **extra) -> tuple:
        """Submit workflow; return (task_map, scratchpad_prefix) via GET /tasks."""
        app, _ = _make_app()
        payload = {"feature": feature, **extra}
        with TestClient(app) as client:
            r = client.post("/workflows/peer-review", json=payload, headers=AUTH)
            data = r.json()
            tasks_resp = client.get("/tasks", headers=AUTH)
        tasks = {t["task_id"]: t for t in tasks_resp.json()}
        return data["task_ids"], tasks, data["scratchpad_prefix"]

    def test_impl_a_prompt_mentions_feature(self):
        ids, tasks, _ = self._get_task_map("a priority queue")
        assert "priority queue" in tasks[ids["impl_a"]]["prompt"]

    def test_impl_b_prompt_mentions_feature(self):
        ids, tasks, _ = self._get_task_map("a doubly linked list")
        assert "doubly linked list" in tasks[ids["impl_b"]]["prompt"]

    def test_reviewer_prompt_mentions_feature(self):
        ids, tasks, _ = self._get_task_map("a trie")
        assert "trie" in tasks[ids["reviewer"]]["prompt"]

    def test_impl_a_prompt_mentions_idiomatic_approach(self):
        ids, tasks, _ = self._get_task_map("a stack")
        prompt = tasks[ids["impl_a"]]["prompt"].lower()
        assert "idiomatic" in prompt or "readable" in prompt

    def test_impl_b_prompt_mentions_performance_approach(self):
        ids, tasks, _ = self._get_task_map("a queue")
        prompt = tasks[ids["impl_b"]]["prompt"].lower()
        assert "performance" in prompt or "concise" in prompt

    def test_reviewer_prompt_mentions_winner_declaration(self):
        ids, tasks, _ = self._get_task_map("a deque")
        prompt = tasks[ids["reviewer"]]["prompt"]
        assert "winner" in prompt.lower() or "WINNER" in prompt

    def test_reviewer_prompt_contains_scratchpad_key_for_impl_a(self):
        ids, tasks, prefix = self._get_task_map("a set")
        assert f"{prefix}_impl_a" in tasks[ids["reviewer"]]["prompt"]

    def test_reviewer_prompt_contains_scratchpad_key_for_impl_b(self):
        ids, tasks, prefix = self._get_task_map("a dictionary")
        assert f"{prefix}_impl_b" in tasks[ids["reviewer"]]["prompt"]

    def test_impl_a_prompt_contains_scratchpad_write_key(self):
        ids, tasks, prefix = self._get_task_map("a counter")
        assert f"{prefix}_impl_a" in tasks[ids["impl_a"]]["prompt"]

    def test_impl_b_prompt_contains_scratchpad_write_key(self):
        ids, tasks, prefix = self._get_task_map("a ring buffer")
        assert f"{prefix}_impl_b" in tasks[ids["impl_b"]]["prompt"]

    def test_reviewer_prompt_contains_review_scratchpad_key(self):
        ids, tasks, prefix = self._get_task_map("a bloom filter")
        assert f"{prefix}_review" in tasks[ids["reviewer"]]["prompt"]

    def test_impl_a_prompt_mentions_language(self):
        ids, tasks, _ = self._get_task_map("a stack", language="go")
        assert "go" in tasks[ids["impl_a"]]["prompt"].lower()

    def test_impl_b_prompt_mentions_language(self):
        ids, tasks, _ = self._get_task_map("a stack", language="rust")
        assert "rust" in tasks[ids["impl_b"]]["prompt"].lower()

    def test_reviewer_prompt_mentions_review_axes(self):
        ids, tasks, _ = self._get_task_map("a B-tree")
        prompt = tasks[ids["reviewer"]]["prompt"].lower()
        assert "correctness" in prompt or "readability" in prompt


# ---------------------------------------------------------------------------
# Tags / routing tests
# ---------------------------------------------------------------------------


class TestPeerReviewWorkflowTags:
    def test_impl_a_tags_applied(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a stack", "impl_a_tags": ["dev", "senior"]},
                headers=AUTH,
            )
            data = r.json()
            tasks_resp = client.get("/tasks", headers=AUTH)
        tasks = {t["task_id"]: t for t in tasks_resp.json()}
        assert set(tasks[data["task_ids"]["impl_a"]]["required_tags"]) == {"dev", "senior"}

    def test_impl_b_tags_applied(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a queue", "impl_b_tags": ["optimiser"]},
                headers=AUTH,
            )
            data = r.json()
            tasks_resp = client.get("/tasks", headers=AUTH)
        tasks = {t["task_id"]: t for t in tasks_resp.json()}
        assert "optimiser" in tasks[data["task_ids"]["impl_b"]]["required_tags"]

    def test_reviewer_tags_applied(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a map", "reviewer_tags": ["senior_reviewer"]},
                headers=AUTH,
            )
            data = r.json()
            tasks_resp = client.get("/tasks", headers=AUTH)
        tasks = {t["task_id"]: t for t in tasks_resp.json()}
        assert "senior_reviewer" in tasks[data["task_ids"]["reviewer"]]["required_tags"]

    def test_empty_tags_means_any_agent(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/peer-review",
                json={"feature": "a tree"},
                headers=AUTH,
            )
            data = r.json()
            tasks_resp = client.get("/tasks", headers=AUTH)
        tasks = {t["task_id"]: t for t in tasks_resp.json()}
        # When tags are empty, required_tags is absent or empty list
        task_a = tasks[data["task_ids"]["impl_a"]]
        assert task_a.get("required_tags", []) == []


# ---------------------------------------------------------------------------
# Idempotency / multiple submissions
# ---------------------------------------------------------------------------


class TestPeerReviewWorkflowMultipleSubmissions:
    def test_two_submissions_produce_different_workflow_ids(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r1 = client.post(
                "/workflows/peer-review",
                json={"feature": "a stack"},
                headers=AUTH,
            )
            r2 = client.post(
                "/workflows/peer-review",
                json={"feature": "a stack"},
                headers=AUTH,
            )
        assert r1.json()["workflow_id"] != r2.json()["workflow_id"]

    def test_two_submissions_produce_different_scratchpad_prefixes(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r1 = client.post(
                "/workflows/peer-review",
                json={"feature": "a queue"},
                headers=AUTH,
            )
            r2 = client.post(
                "/workflows/peer-review",
                json={"feature": "a queue"},
                headers=AUTH,
            )
        assert r1.json()["scratchpad_prefix"] != r2.json()["scratchpad_prefix"]


# ---------------------------------------------------------------------------
# PeerReviewWorkflowSubmit schema unit tests
# ---------------------------------------------------------------------------


class TestPeerReviewWorkflowSubmitModel:
    def test_default_language_is_python(self):
        from tmux_orchestrator.web.schemas import PeerReviewWorkflowSubmit

        m = PeerReviewWorkflowSubmit(feature="a stack")
        assert m.language == "python"

    def test_default_tags_empty(self):
        from tmux_orchestrator.web.schemas import PeerReviewWorkflowSubmit

        m = PeerReviewWorkflowSubmit(feature="a queue")
        assert m.impl_a_tags == []
        assert m.impl_b_tags == []
        assert m.reviewer_tags == []

    def test_default_reply_to_none(self):
        from tmux_orchestrator.web.schemas import PeerReviewWorkflowSubmit

        m = PeerReviewWorkflowSubmit(feature="a deque")
        assert m.reply_to is None

    def test_default_agent_timeout_300(self):
        from tmux_orchestrator.web.schemas import PeerReviewWorkflowSubmit

        m = PeerReviewWorkflowSubmit(feature="a tree")
        assert m.agent_timeout == 300

    def test_empty_feature_raises_validation_error(self):
        import pydantic

        from tmux_orchestrator.web.schemas import PeerReviewWorkflowSubmit

        with pytest.raises(pydantic.ValidationError):
            PeerReviewWorkflowSubmit(feature="")

    def test_whitespace_feature_raises_validation_error(self):
        import pydantic

        from tmux_orchestrator.web.schemas import PeerReviewWorkflowSubmit

        with pytest.raises(pydantic.ValidationError):
            PeerReviewWorkflowSubmit(feature="   ")
