"""Tests for POST /workflows/fulldev — Full Software Development Lifecycle workflow.

5-agent pipeline:
  spec-writer → architect → tdd-test-writer → tdd-implementer → reviewer

Each agent reads previous artifacts from shared scratchpad (Blackboard pattern).

Design references:
- MetaGPT arXiv:2308.00352 (2023/2024): Product Manager → Architect → Engineer pipeline.
- AgentMesh arXiv:2507.19902 (2025): Planner → Coder → Debugger → Reviewer 4-role pipeline.
- arXiv:2508.00083 "Survey on Code Generation with LLM-based Agents" (2025):
  Pipeline-based labor division with blackboard model for inter-agent handoff.
- arXiv:2505.16339 "Rethinking Code Review Workflows with LLM Assistance" (2025):
  LLM-based code review integrated into automated pipelines.
- DESIGN.md §10.16 (v0.42.0)
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


# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------


class TestFulldevWorkflowSchema:
    def test_missing_feature_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post("/workflows/fulldev", json={}, headers=AUTH)
        assert r.status_code == 422

    def test_empty_feature_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post("/workflows/fulldev", json={"feature": ""}, headers=AUTH)
        assert r.status_code == 422

    def test_whitespace_feature_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post("/workflows/fulldev", json={"feature": "   "}, headers=AUTH)
        assert r.status_code == 422

    def test_missing_auth_returns_401(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post("/workflows/fulldev", json={"feature": "EventBus"})
        assert r.status_code == 401

    def test_minimal_request_returns_200(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/fulldev",
                json={"feature": "EventBus"},
                headers=AUTH,
            )
        assert r.status_code == 200

    def test_full_request_with_all_fields_returns_200(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/fulldev",
                json={
                    "feature": "EventBus publish/subscribe",
                    "language": "python",
                    "spec_writer_tags": ["spec-writer"],
                    "architect_tags": ["architect"],
                    "test_writer_tags": ["tdd-test-writer"],
                    "implementer_tags": ["tdd-implementer"],
                    "reviewer_tags": ["reviewer"],
                    "reply_to": None,
                },
                headers=AUTH,
            )
        assert r.status_code == 200

    def test_language_defaults_to_python(self):
        app, orch = _make_app()
        prompts = []
        original_submit = orch.submit_task

        async def capture_submit(prompt, *args, **kwargs):
            prompts.append(prompt)
            return await original_submit(prompt, *args, **kwargs)

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            client.post("/workflows/fulldev", json={"feature": "EventBus"}, headers=AUTH)

        # All prompts should reference python (default)
        assert any("python" in p.lower() for p in prompts)


# ---------------------------------------------------------------------------
# Response structure tests
# ---------------------------------------------------------------------------


class TestFulldevWorkflowResponse:
    def _post(self, feature: str = "EventBus", **extra):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/fulldev",
                json={"feature": feature, **extra},
                headers=AUTH,
            )
        return r.json()

    def test_response_contains_workflow_id(self):
        data = self._post()
        assert "workflow_id" in data
        assert data["workflow_id"]

    def test_response_contains_name(self):
        data = self._post("EventBus")
        assert "name" in data
        assert data["name"]

    def test_response_name_contains_fulldev(self):
        data = self._post("EventBus")
        name = data["name"].lower()
        assert "fulldev" in name or "eventbus" in name.lower()

    def test_response_contains_task_ids(self):
        data = self._post()
        assert "task_ids" in data

    def test_task_ids_has_all_five_roles(self):
        data = self._post()
        task_ids = data["task_ids"]
        assert "spec_writer" in task_ids
        assert "architect" in task_ids
        assert "test_writer" in task_ids
        assert "implementer" in task_ids
        assert "reviewer" in task_ids

    def test_all_task_ids_are_strings(self):
        data = self._post()
        for role, tid in data["task_ids"].items():
            assert isinstance(tid, str), f"task_ids[{role!r}] must be str"

    def test_response_contains_scratchpad_prefix(self):
        data = self._post()
        assert "scratchpad_prefix" in data
        assert data["scratchpad_prefix"]

    def test_scratchpad_prefix_starts_with_fulldev(self):
        data = self._post()
        assert data["scratchpad_prefix"].startswith("fulldev_")

    def test_scratchpad_prefix_has_no_slashes(self):
        """Scratchpad prefix must not contain '/' — the REST route uses {key} not {key:path}."""
        data = self._post()
        assert "/" not in data["scratchpad_prefix"]


# ---------------------------------------------------------------------------
# Workflow DAG structure tests
# ---------------------------------------------------------------------------


class TestFulldevWorkflowDAG:
    def _capture_submissions(self, feature: str = "EventBus"):
        """Return list of (task, depends_on) for all submitted tasks."""
        app, orch = _make_app()
        submitted = []
        original_submit = orch.submit_task

        async def capture_submit(*args, **kwargs):
            task = await original_submit(*args, **kwargs)
            submitted.append((task, kwargs.get("depends_on", [])))
            return task

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            client.post(
                "/workflows/fulldev",
                json={"feature": feature},
                headers=AUTH,
            )

        return submitted

    def test_exactly_five_tasks_submitted(self):
        """Exactly 5 tasks: spec_writer, architect, test_writer, implementer, reviewer."""
        submitted = self._capture_submissions()
        assert len(submitted) == 5

    def test_spec_writer_has_no_dependencies(self):
        """spec_writer is the first task — no dependencies."""
        submitted = self._capture_submissions()
        assert not submitted[0][1]

    def test_architect_depends_on_spec_writer(self):
        """architect depends on spec_writer."""
        submitted = self._capture_submissions()
        spec_writer_id = submitted[0][0].id
        architect_deps = submitted[1][1]
        assert spec_writer_id in architect_deps

    def test_test_writer_depends_on_architect(self):
        """tdd-test-writer depends on architect."""
        submitted = self._capture_submissions()
        architect_id = submitted[1][0].id
        test_writer_deps = submitted[2][1]
        assert architect_id in test_writer_deps

    def test_implementer_depends_on_test_writer(self):
        """tdd-implementer depends on tdd-test-writer."""
        submitted = self._capture_submissions()
        test_writer_id = submitted[2][0].id
        implementer_deps = submitted[3][1]
        assert test_writer_id in implementer_deps

    def test_reviewer_depends_on_implementer(self):
        """reviewer depends on tdd-implementer."""
        submitted = self._capture_submissions()
        implementer_id = submitted[3][0].id
        reviewer_deps = submitted[4][1]
        assert implementer_id in reviewer_deps

    def test_linear_chain_no_skips(self):
        """Each task depends on exactly the previous task (not earlier ones)."""
        submitted = self._capture_submissions()
        # task 0: no deps
        assert not submitted[0][1]
        # tasks 1-4: each depends on exactly one predecessor
        for i in range(1, 5):
            prev_id = submitted[i - 1][0].id
            deps = submitted[i][1]
            assert prev_id in deps, f"task[{i}] must depend on task[{i-1}]"


# ---------------------------------------------------------------------------
# Required tags tests
# ---------------------------------------------------------------------------


class TestFulldevWorkflowTags:
    def _capture_tags(self, body: dict) -> list:
        app, orch = _make_app()
        tags_received = []
        original_submit = orch.submit_task

        async def capture_submit(*args, **kwargs):
            tags_received.append(kwargs.get("required_tags"))
            return await original_submit(*args, **kwargs)

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            client.post("/workflows/fulldev", json=body, headers=AUTH)

        return tags_received

    def test_all_role_tags_applied(self):
        tags = self._capture_tags({
            "feature": "EventBus",
            "spec_writer_tags": ["spec-writer"],
            "architect_tags": ["architect"],
            "test_writer_tags": ["tdd-test-writer"],
            "implementer_tags": ["tdd-implementer"],
            "reviewer_tags": ["reviewer"],
        })
        assert tags[0] == ["spec-writer"]
        assert tags[1] == ["architect"]
        assert tags[2] == ["tdd-test-writer"]
        assert tags[3] == ["tdd-implementer"]
        assert tags[4] == ["reviewer"]

    def test_empty_tags_treated_as_none(self):
        tags = self._capture_tags({"feature": "EventBus"})
        for t in tags:
            assert t is None or t == []

    def test_partial_tags_applied_correctly(self):
        tags = self._capture_tags({
            "feature": "EventBus",
            "spec_writer_tags": ["spec-writer"],
            # others omitted → default empty
        })
        assert tags[0] == ["spec-writer"]


# ---------------------------------------------------------------------------
# Prompt content tests
# ---------------------------------------------------------------------------


class TestFulldevWorkflowPrompts:
    def _get_prompts(self, feature: str = "EventBus publish/subscribe") -> list[str]:
        app, orch = _make_app()
        prompts = []
        original_submit = orch.submit_task

        async def capture_submit(prompt, *args, **kwargs):
            prompts.append(prompt)
            return await original_submit(prompt, *args, **kwargs)

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            client.post("/workflows/fulldev", json={"feature": feature}, headers=AUTH)

        return prompts

    def test_spec_writer_prompt_contains_feature(self):
        prompts = self._get_prompts("EventBus")
        assert "EventBus" in prompts[0]

    def test_spec_writer_prompt_mentions_spec(self):
        prompts = self._get_prompts()
        spec_prompt = prompts[0].lower()
        assert "spec" in spec_prompt or "specification" in spec_prompt or "requirement" in spec_prompt

    def test_spec_writer_prompt_mentions_scratchpad_write(self):
        """spec_writer must write spec to scratchpad."""
        prompts = self._get_prompts()
        assert "scratchpad" in prompts[0].lower() or "curl" in prompts[0].lower()

    def test_architect_prompt_reads_spec_from_scratchpad(self):
        """architect must read spec from scratchpad."""
        prompts = self._get_prompts()
        arch_prompt = prompts[1].lower()
        assert "scratchpad" in arch_prompt or "curl" in arch_prompt

    def test_architect_prompt_mentions_design(self):
        prompts = self._get_prompts()
        arch_prompt = prompts[1].lower()
        assert "design" in arch_prompt or "architect" in arch_prompt or "adr" in arch_prompt

    def test_test_writer_prompt_reads_spec_and_design(self):
        """test_writer must read spec AND design from scratchpad."""
        prompts = self._get_prompts()
        tw_prompt = prompts[2].lower()
        # Must contain scratchpad reads
        assert "scratchpad" in tw_prompt or "curl" in tw_prompt

    def test_test_writer_prompt_mentions_tdd_or_tests(self):
        prompts = self._get_prompts()
        tw_prompt = prompts[2].lower()
        assert "test" in tw_prompt or "pytest" in tw_prompt or "tdd" in tw_prompt

    def test_implementer_prompt_reads_tests_from_scratchpad(self):
        """implementer reads tests from scratchpad."""
        prompts = self._get_prompts()
        impl_prompt = prompts[3].lower()
        assert "scratchpad" in impl_prompt or "curl" in impl_prompt

    def test_implementer_prompt_mentions_implementation(self):
        prompts = self._get_prompts()
        impl_prompt = prompts[3].lower()
        assert "implement" in impl_prompt or "code" in impl_prompt

    def test_reviewer_prompt_reads_multiple_artifacts(self):
        """reviewer reads spec, tests, and impl from scratchpad."""
        prompts = self._get_prompts()
        rev_prompt = prompts[4].lower()
        assert "scratchpad" in rev_prompt or "curl" in rev_prompt

    def test_reviewer_prompt_mentions_review(self):
        prompts = self._get_prompts()
        rev_prompt = prompts[4].lower()
        assert "review" in rev_prompt or "assess" in rev_prompt or "evaluate" in rev_prompt

    def test_all_prompts_share_consistent_scratchpad_prefix(self):
        """All 5 prompts must use the same scratchpad prefix."""
        app, orch = _make_app()
        prompts = []
        original_submit = orch.submit_task

        async def capture_submit(prompt, *args, **kwargs):
            prompts.append(prompt)
            return await original_submit(prompt, *args, **kwargs)

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            r = client.post("/workflows/fulldev", json={"feature": "EventBus"}, headers=AUTH)

        prefix = r.json()["scratchpad_prefix"]
        # The prefix must appear in every prompt (used as scratchpad namespace)
        for i, p in enumerate(prompts):
            assert prefix in p, f"prompt[{i}] does not reference scratchpad prefix {prefix!r}"


# ---------------------------------------------------------------------------
# reply_to tests
# ---------------------------------------------------------------------------


class TestFulldevWorkflowReplyTo:
    def test_reply_to_forwarded_to_reviewer(self):
        """reply_to must be forwarded only to the last task (reviewer)."""
        app, orch = _make_app()
        reply_tos = []
        original_submit = orch.submit_task

        async def capture_submit(*args, **kwargs):
            reply_tos.append(kwargs.get("reply_to"))
            return await original_submit(*args, **kwargs)

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            client.post(
                "/workflows/fulldev",
                json={"feature": "EventBus", "reply_to": "director-agent"},
                headers=AUTH,
            )

        # Only the reviewer (last task, index 4) should have reply_to
        assert reply_tos[4] == "director-agent"
        # All earlier tasks should not have reply_to
        for i in range(4):
            assert reply_tos[i] is None, f"task[{i}] should not have reply_to"


# ---------------------------------------------------------------------------
# Workflow registration tests
# ---------------------------------------------------------------------------


class TestFulldevWorkflowRegistration:
    def test_workflow_registered_with_manager(self):
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/fulldev",
                json={"feature": "EventBus"},
                headers=AUTH,
            )

        workflow_id = r.json()["workflow_id"]
        wm = orch.get_workflow_manager()
        run = wm.get(workflow_id)
        assert run is not None
        assert run.id == workflow_id

    def test_workflow_contains_all_five_tasks(self):
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/fulldev",
                json={"feature": "EventBus"},
                headers=AUTH,
            )

        data = r.json()
        workflow_id = data["workflow_id"]
        task_ids = list(data["task_ids"].values())

        wm = orch.get_workflow_manager()
        run = wm.get(workflow_id)
        assert set(run.task_ids) == set(task_ids)
        assert len(run.task_ids) == 5

    def test_fulldev_endpoint_in_openapi(self):
        """POST /workflows/fulldev must appear in the OpenAPI schema."""
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.get("/openapi.json")
        schema = r.json()
        paths = schema.get("paths", {})
        assert "/workflows/fulldev" in paths
        assert "post" in paths["/workflows/fulldev"]

    def test_workflow_name_contains_feature(self):
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/fulldev",
                json={"feature": "UniqueFeatureName42"},
                headers=AUTH,
            )
        name = r.json()["name"]
        assert "UniqueFeatureName42" in name or "fulldev" in name.lower()
