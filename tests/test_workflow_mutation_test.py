"""Tests for POST /workflows/mutation-test — 3-agent Mutation Testing workflow.

Pipeline (sequential):
  implementer → mutant-introducer → test-improver

Design references:
- AdverTest arXiv:2602.08146
- Meta ACH arXiv:2501.12862 (FSE 2025)
- DESIGN.md §10.100 (v1.2.25)
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

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


def _submit(client, payload: dict):
    return client.post("/workflows/mutation-test", json=payload, headers=AUTH)


def _get_tasks(client):
    r = client.get("/tasks", headers=AUTH)
    return {t["task_id"]: t for t in r.json()}


# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------


class TestMutationTestWorkflowSchema:
    def test_missing_feature_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert _submit(c, {}).status_code == 422

    def test_empty_feature_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert _submit(c, {"feature": ""}).status_code == 422

    def test_whitespace_feature_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert _submit(c, {"feature": "   "}).status_code == 422

    def test_missing_auth_returns_401(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            r = c.post("/workflows/mutation-test", json={"feature": "a stack"})
        assert r.status_code == 401

    def test_wrong_api_key_returns_401(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            r = c.post(
                "/workflows/mutation-test",
                json={"feature": "a stack"},
                headers={"X-API-Key": "wrong"},
            )
        assert r.status_code == 401

    def test_valid_request_returns_200(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert _submit(c, {"feature": "a stack"}).status_code == 200

    def test_num_mutations_below_1_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert _submit(c, {"feature": "a q", "num_mutations": 0}).status_code == 422

    def test_num_mutations_above_5_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert _submit(c, {"feature": "a q", "num_mutations": 6}).status_code == 422

    def test_num_mutations_valid_range(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            for n in (1, 3, 5):
                r = _submit(c, {"feature": "a queue", "num_mutations": n})
                assert r.status_code == 200, f"num_mutations={n} should be valid"

    def test_all_fields_accepted(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            r = _submit(c, {
                "feature": "a LRU cache",
                "language": "python",
                "num_mutations": 4,
                "agent_timeout": 600,
                "scratchpad_prefix": "test_prefix",
                "implementer_tags": ["dev"],
                "mutant_introducer_tags": ["mutant"],
                "test_improver_tags": ["tester"],
                "reply_to": None,
            })
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Response structure tests
# ---------------------------------------------------------------------------


class TestMutationTestWorkflowResponse:
    def test_response_has_workflow_id(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a stack"}).json()
        assert "workflow_id" in data
        assert len(data["workflow_id"]) > 0

    def test_response_name_contains_mutation_test(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a stack"}).json()
        assert "mutation-test" in data["name"]
        assert "a stack" in data["name"]

    def test_response_has_three_task_ids(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a queue"}).json()
        ids = data["task_ids"]
        assert "implementer" in ids
        assert "mutant_introducer" in ids
        assert "test_improver" in ids

    def test_response_task_ids_are_distinct(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a deque"}).json()
        vals = list(data["task_ids"].values())
        assert len(set(vals)) == 3

    def test_response_has_scratchpad_prefix(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a set"}).json()
        assert "scratchpad_prefix" in data
        assert len(data["scratchpad_prefix"]) > 0

    def test_auto_prefix_starts_with_muttest(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a heap"}).json()
        assert data["scratchpad_prefix"].startswith("muttest_")

    def test_custom_scratchpad_prefix_used(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {
                "feature": "a counter",
                "scratchpad_prefix": "my_prefix",
            }).json()
        assert data["scratchpad_prefix"] == "my_prefix"

    def test_response_has_num_mutations(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a tree", "num_mutations": 4}).json()
        assert data["num_mutations"] == 4

    def test_default_num_mutations_is_3(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a graph"}).json()
        assert data["num_mutations"] == 3

    def test_two_submissions_have_different_workflow_ids(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            d1 = _submit(c, {"feature": "a stack"}).json()
            d2 = _submit(c, {"feature": "a stack"}).json()
        assert d1["workflow_id"] != d2["workflow_id"]

    def test_two_submissions_have_different_prefixes(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            d1 = _submit(c, {"feature": "a queue"}).json()
            d2 = _submit(c, {"feature": "a queue"}).json()
        assert d1["scratchpad_prefix"] != d2["scratchpad_prefix"]


# ---------------------------------------------------------------------------
# Task dependency tests
# ---------------------------------------------------------------------------


class TestMutationTestWorkflowDependencies:
    def test_implementer_is_queued_no_deps(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a stack"}).json()
            tasks = _get_tasks(c)
        impl_id = data["task_ids"]["implementer"]
        assert tasks[impl_id]["status"] == "queued"

    def test_mutant_introducer_depends_on_implementer(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a min-heap"}).json()
            tasks = _get_tasks(c)
        impl_id = data["task_ids"]["implementer"]
        mutant_id = data["task_ids"]["mutant_introducer"]
        mutant_task = tasks[mutant_id]
        assert impl_id in mutant_task["depends_on"]

    def test_test_improver_depends_on_mutant_introducer(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a B-tree"}).json()
            tasks = _get_tasks(c)
        mutant_id = data["task_ids"]["mutant_introducer"]
        improver_id = data["task_ids"]["test_improver"]
        improver_task = tasks[improver_id]
        assert mutant_id in improver_task["depends_on"]

    def test_test_improver_is_in_waiting_status(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a hash table"}).json()
            tasks = _get_tasks(c)
        improver_id = data["task_ids"]["test_improver"]
        assert tasks[improver_id]["status"] == "waiting"

    def test_mutant_introducer_is_in_waiting_status(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a trie"}).json()
            tasks = _get_tasks(c)
        mutant_id = data["task_ids"]["mutant_introducer"]
        assert tasks[mutant_id]["status"] == "waiting"

    def test_three_tasks_present_in_tasks_list(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a binary search tree"}).json()
            tasks = _get_tasks(c)
        expected_ids = set(data["task_ids"].values())
        assert expected_ids.issubset(set(tasks.keys()))

    def test_workflow_registered(self):
        app, orch = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a skip list"}).json()
        wm = orch.get_workflow_manager()
        run = wm.get(data["workflow_id"])
        assert run is not None
        assert run.id == data["workflow_id"]


# ---------------------------------------------------------------------------
# Prompt content tests
# ---------------------------------------------------------------------------


class TestMutationTestWorkflowPrompts:
    def _get(self, feature: str, **kwargs) -> tuple:
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": feature, **kwargs}).json()
            tasks = _get_tasks(c)
        return data["task_ids"], tasks, data["scratchpad_prefix"]

    def test_implementer_prompt_mentions_feature(self):
        ids, tasks, _ = self._get("a priority queue")
        assert "priority queue" in tasks[ids["implementer"]]["prompt"]

    def test_mutant_introducer_prompt_mentions_feature(self):
        ids, tasks, _ = self._get("a doubly linked list")
        assert "doubly linked list" in tasks[ids["mutant_introducer"]]["prompt"]

    def test_test_improver_prompt_mentions_feature(self):
        ids, tasks, _ = self._get("a circular buffer")
        assert "circular buffer" in tasks[ids["test_improver"]]["prompt"]

    def test_implementer_prompt_mentions_language(self):
        ids, tasks, _ = self._get("a stack", language="go")
        assert "go" in tasks[ids["implementer"]]["prompt"].lower()

    def test_mutant_introducer_prompt_mentions_num_mutations(self):
        ids, tasks, _ = self._get("a queue", num_mutations=4)
        assert "4" in tasks[ids["mutant_introducer"]]["prompt"]

    def test_implementer_prompt_contains_impl_key(self):
        ids, tasks, prefix = self._get("a counter")
        assert f"{prefix}_impl" in tasks[ids["implementer"]]["prompt"]

    def test_mutant_introducer_prompt_contains_impl_key(self):
        ids, tasks, prefix = self._get("a set")
        assert f"{prefix}_impl" in tasks[ids["mutant_introducer"]]["prompt"]

    def test_mutant_introducer_prompt_contains_mutants_key(self):
        ids, tasks, prefix = self._get("a map")
        assert f"{prefix}_mutants" in tasks[ids["mutant_introducer"]]["prompt"]

    def test_test_improver_prompt_contains_impl_key(self):
        ids, tasks, prefix = self._get("a ring buffer")
        assert f"{prefix}_impl" in tasks[ids["test_improver"]]["prompt"]

    def test_test_improver_prompt_contains_mutants_key(self):
        ids, tasks, prefix = self._get("a bloom filter")
        assert f"{prefix}_mutants" in tasks[ids["test_improver"]]["prompt"]

    def test_test_improver_prompt_contains_improved_tests_key(self):
        ids, tasks, prefix = self._get("a graph")
        assert f"{prefix}_improved_tests" in tasks[ids["test_improver"]]["prompt"]

    def test_mutant_introducer_prompt_mentions_mutation_type(self):
        ids, tasks, _ = self._get("a tree")
        prompt = tasks[ids["mutant_introducer"]]["prompt"].lower()
        assert "mutation" in prompt or "mutant" in prompt or "bug" in prompt

    def test_test_improver_prompt_mentions_kill(self):
        ids, tasks, _ = self._get("a B-tree")
        prompt = tasks[ids["test_improver"]]["prompt"].lower()
        assert "kill" in prompt or "catch" in prompt or "detect" in prompt


# ---------------------------------------------------------------------------
# Tags tests
# ---------------------------------------------------------------------------


class TestMutationTestWorkflowTags:
    def test_implementer_tags_applied(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {
                "feature": "a stack",
                "implementer_tags": ["senior_dev"],
            }).json()
            tasks = _get_tasks(c)
        assert "senior_dev" in tasks[data["task_ids"]["implementer"]]["required_tags"]

    def test_mutant_introducer_tags_applied(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {
                "feature": "a queue",
                "mutant_introducer_tags": ["adversarial"],
            }).json()
            tasks = _get_tasks(c)
        assert "adversarial" in tasks[data["task_ids"]["mutant_introducer"]]["required_tags"]

    def test_test_improver_tags_applied(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {
                "feature": "a set",
                "test_improver_tags": ["tester"],
            }).json()
            tasks = _get_tasks(c)
        assert "tester" in tasks[data["task_ids"]["test_improver"]]["required_tags"]


# ---------------------------------------------------------------------------
# Pydantic model unit tests
# ---------------------------------------------------------------------------


class TestMutationTestWorkflowSubmitModel:
    def test_defaults(self):
        from tmux_orchestrator.web.schemas import MutationTestWorkflowSubmit

        m = MutationTestWorkflowSubmit(feature="a stack")
        assert m.language == "python"
        assert m.num_mutations == 3
        assert m.agent_timeout == 300
        assert m.implementer_tags == []
        assert m.mutant_introducer_tags == []
        assert m.test_improver_tags == []
        assert m.reply_to is None
        assert m.scratchpad_prefix == ""

    def test_empty_feature_raises(self):
        import pydantic
        from tmux_orchestrator.web.schemas import MutationTestWorkflowSubmit

        with pytest.raises(pydantic.ValidationError):
            MutationTestWorkflowSubmit(feature="")

    def test_num_mutations_out_of_range_raises(self):
        import pydantic
        from tmux_orchestrator.web.schemas import MutationTestWorkflowSubmit

        with pytest.raises(pydantic.ValidationError):
            MutationTestWorkflowSubmit(feature="a q", num_mutations=0)

        with pytest.raises(pydantic.ValidationError):
            MutationTestWorkflowSubmit(feature="a q", num_mutations=6)
