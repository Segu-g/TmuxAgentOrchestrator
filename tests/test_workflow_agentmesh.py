"""Tests for POST /workflows/agentmesh — AgentMesh 4-role development pipeline.

4-agent sequential pipeline: planner -> coder -> debugger -> reviewer.
Each phase builds on the previous agent's output via the shared scratchpad
(Blackboard pattern).

Design references:
- Elias, "AgentMesh: A Cooperative Multi-Agent Generative AI Framework
  for Software Development Automation", arXiv:2507.19902 (2025)
- ACM TOSEM, "LLM-Based Multi-Agent Systems for Software Engineering" (2025),
  https://dl.acm.org/doi/10.1145/3712003
- DESIGN.md §10.73 (v1.1.41)
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

FEATURE_REQUEST = "Implement a binary search function that finds the position of a target value in a sorted list."


# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------


class TestAgentmeshWorkflowSchema:
    def test_missing_feature_request_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post("/workflows/agentmesh", json={}, headers=AUTH)
        assert r.status_code == 422

    def test_empty_feature_request_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/agentmesh",
                json={"feature_request": ""},
                headers=AUTH,
            )
        assert r.status_code == 422

    def test_whitespace_feature_request_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/agentmesh",
                json={"feature_request": "   "},
                headers=AUTH,
            )
        assert r.status_code == 422

    def test_missing_auth_returns_401(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/agentmesh",
                json={"feature_request": FEATURE_REQUEST},
            )
        assert r.status_code == 401

    def test_minimal_request_returns_200(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/agentmesh",
                json={"feature_request": FEATURE_REQUEST},
                headers=AUTH,
            )
        assert r.status_code == 200

    def test_default_language_is_python(self):
        """Default language should be 'python'."""
        from tmux_orchestrator.web.schemas import AgentmeshWorkflowSubmit
        body = AgentmeshWorkflowSubmit(feature_request=FEATURE_REQUEST)
        assert body.language == "python"

    def test_default_scratchpad_prefix_is_agentmesh(self):
        """Default scratchpad_prefix should be 'agentmesh'."""
        from tmux_orchestrator.web.schemas import AgentmeshWorkflowSubmit
        body = AgentmeshWorkflowSubmit(feature_request=FEATURE_REQUEST)
        assert body.scratchpad_prefix == "agentmesh"

    def test_default_agent_timeout_is_300(self):
        """Default agent_timeout should be 300."""
        from tmux_orchestrator.web.schemas import AgentmeshWorkflowSubmit
        body = AgentmeshWorkflowSubmit(feature_request=FEATURE_REQUEST)
        assert body.agent_timeout == 300

    def test_custom_language_accepted(self):
        """Custom language (e.g. 'typescript') should be accepted."""
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/agentmesh",
                json={"feature_request": FEATURE_REQUEST, "language": "typescript"},
                headers=AUTH,
            )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Response shape tests
# ---------------------------------------------------------------------------


class TestAgentmeshWorkflowResponse:
    def test_response_contains_workflow_id(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/agentmesh",
                json={"feature_request": FEATURE_REQUEST},
                headers=AUTH,
            )
        data = r.json()
        assert "workflow_id" in data
        assert data["workflow_id"]

    def test_response_contains_name(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/agentmesh",
                json={"feature_request": FEATURE_REQUEST},
                headers=AUTH,
            )
        data = r.json()
        assert "name" in data
        assert "agentmesh" in data["name"].lower()

    def test_response_contains_task_ids_with_four_roles(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/agentmesh",
                json={"feature_request": FEATURE_REQUEST},
                headers=AUTH,
            )
        data = r.json()
        assert "task_ids" in data
        task_ids = data["task_ids"]
        assert "planner" in task_ids
        assert "coder" in task_ids
        assert "debugger" in task_ids
        assert "reviewer" in task_ids

    def test_response_contains_scratchpad_prefix(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/agentmesh",
                json={"feature_request": FEATURE_REQUEST},
                headers=AUTH,
            )
        data = r.json()
        assert "scratchpad_prefix" in data
        assert data["scratchpad_prefix"]

    def test_all_four_task_ids_are_distinct(self):
        """All 4 task IDs must be unique."""
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/agentmesh",
                json={"feature_request": FEATURE_REQUEST},
                headers=AUTH,
            )
        task_ids = list(r.json()["task_ids"].values())
        assert len(set(task_ids)) == 4


# ---------------------------------------------------------------------------
# DAG structure tests (depends_on chain)
# ---------------------------------------------------------------------------


class TestAgentmeshWorkflowDAG:
    def _capture_submissions(self, feature_request: str = FEATURE_REQUEST):
        """Return list of (task, depends_on) tuples captured during submission."""
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
                "/workflows/agentmesh",
                json={"feature_request": feature_request},
                headers=AUTH,
            )

        return submitted

    def test_exactly_four_tasks_submitted(self):
        """Exactly 4 tasks should be submitted: planner, coder, debugger, reviewer."""
        submitted = self._capture_submissions()
        assert len(submitted) == 4

    def test_planner_has_no_dependencies(self):
        """Planner is the root task — no depends_on."""
        submitted = self._capture_submissions()
        planner_depends = submitted[0][1]
        assert not planner_depends

    def test_coder_depends_on_planner(self):
        """Coder must depend on planner."""
        submitted = self._capture_submissions()
        planner_id = submitted[0][0].id
        coder_depends = submitted[1][1]
        assert planner_id in coder_depends

    def test_debugger_depends_on_coder(self):
        """Debugger must depend on coder."""
        submitted = self._capture_submissions()
        coder_id = submitted[1][0].id
        debugger_depends = submitted[2][1]
        assert coder_id in debugger_depends

    def test_reviewer_depends_on_debugger(self):
        """Reviewer must depend on debugger."""
        submitted = self._capture_submissions()
        debugger_id = submitted[2][0].id
        reviewer_depends = submitted[3][1]
        assert debugger_id in reviewer_depends

    def test_sequential_chain_planner_coder_debugger_reviewer(self):
        """Full sequential chain: planner -> coder -> debugger -> reviewer."""
        submitted = self._capture_submissions()
        planner_id = submitted[0][0].id
        coder_id = submitted[1][0].id
        debugger_id = submitted[2][0].id

        # Verify each step depends on exactly the previous
        assert planner_id in submitted[1][1]
        assert coder_id in submitted[2][1]
        assert debugger_id in submitted[3][1]


# ---------------------------------------------------------------------------
# required_tags routing tests
# ---------------------------------------------------------------------------


class TestAgentmeshWorkflowTags:
    def _capture_tags(self, payload: dict | None = None) -> list:
        app, orch = _make_app()
        tags_received = []
        original_submit = orch.submit_task

        async def capture_submit(*args, **kwargs):
            tags_received.append(kwargs.get("required_tags"))
            return await original_submit(*args, **kwargs)

        orch.submit_task = capture_submit

        payload = payload or {"feature_request": FEATURE_REQUEST}
        with TestClient(app) as client:
            client.post("/workflows/agentmesh", json=payload, headers=AUTH)

        return tags_received

    def test_planner_has_agentmesh_planner_tag(self):
        tags = self._capture_tags()
        assert tags[0] == ["agentmesh_planner"]

    def test_coder_has_agentmesh_coder_tag(self):
        tags = self._capture_tags()
        assert tags[1] == ["agentmesh_coder"]

    def test_debugger_has_agentmesh_debugger_tag(self):
        tags = self._capture_tags()
        assert tags[2] == ["agentmesh_debugger"]

    def test_reviewer_has_agentmesh_reviewer_tag(self):
        tags = self._capture_tags()
        assert tags[3] == ["agentmesh_reviewer"]


# ---------------------------------------------------------------------------
# Prompt content tests
# ---------------------------------------------------------------------------


class TestAgentmeshWorkflowPrompts:
    def _get_prompts_and_data(self, payload: dict) -> tuple[list[str], dict]:
        app, orch = _make_app()
        prompts = []
        original_submit = orch.submit_task

        async def capture_submit(prompt, *args, **kwargs):
            prompts.append(prompt)
            return await original_submit(prompt, *args, **kwargs)

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            r = client.post("/workflows/agentmesh", json=payload, headers=AUTH)

        return prompts, r.json()

    def test_planner_prompt_contains_feature_request(self):
        prompts, _ = self._get_prompts_and_data({"feature_request": FEATURE_REQUEST})
        assert FEATURE_REQUEST in prompts[0]

    def test_planner_prompt_mentions_plan_role(self):
        prompts, _ = self._get_prompts_and_data({"feature_request": FEATURE_REQUEST})
        assert "planner" in prompts[0].lower() or "plan" in prompts[0].lower()

    def test_coder_prompt_reads_scratchpad(self):
        """Coder should read planner output from scratchpad."""
        prompts, data = self._get_prompts_and_data({"feature_request": FEATURE_REQUEST})
        prefix = data["scratchpad_prefix"]
        assert f"{prefix}_plan" in prompts[1] or "scratchpad" in prompts[1].lower()

    def test_debugger_prompt_reads_code_from_scratchpad(self):
        """Debugger should read coder output from scratchpad."""
        prompts, data = self._get_prompts_and_data({"feature_request": FEATURE_REQUEST})
        prefix = data["scratchpad_prefix"]
        assert f"{prefix}_code" in prompts[2] or "scratchpad" in prompts[2].lower()

    def test_reviewer_prompt_reads_all_artifacts(self):
        """Reviewer should read plan, code, and debugged from scratchpad."""
        prompts, data = self._get_prompts_and_data({"feature_request": FEATURE_REQUEST})
        prefix = data["scratchpad_prefix"]
        reviewer_prompt = prompts[3]
        assert f"{prefix}_plan" in reviewer_prompt or "plan" in reviewer_prompt.lower()
        assert f"{prefix}_code" in reviewer_prompt or "code" in reviewer_prompt.lower()
        assert f"{prefix}_debugged" in reviewer_prompt or "debugged" in reviewer_prompt.lower()

    def test_reviewer_prompt_mentions_star_rating(self):
        """Reviewer prompt should instruct the agent to provide a star rating."""
        prompts, _ = self._get_prompts_and_data({"feature_request": FEATURE_REQUEST})
        reviewer_prompt = prompts[3].lower()
        assert "star" in reviewer_prompt or "rating" in reviewer_prompt or "1-5" in reviewer_prompt

    def test_reviewer_writes_to_review_key(self):
        """Reviewer should write to the _review scratchpad key."""
        prompts, data = self._get_prompts_and_data({"feature_request": FEATURE_REQUEST})
        prefix = data["scratchpad_prefix"]
        assert f"{prefix}_review" in prompts[3]

    def test_planner_writes_to_plan_key(self):
        """Planner should write to the _plan scratchpad key."""
        prompts, data = self._get_prompts_and_data({"feature_request": FEATURE_REQUEST})
        prefix = data["scratchpad_prefix"]
        assert f"{prefix}_plan" in prompts[0]

    def test_coder_writes_to_code_key(self):
        """Coder should write to the _code scratchpad key."""
        prompts, data = self._get_prompts_and_data({"feature_request": FEATURE_REQUEST})
        prefix = data["scratchpad_prefix"]
        assert f"{prefix}_code" in prompts[1]

    def test_debugger_writes_to_debugged_key(self):
        """Debugger should write to the _debugged scratchpad key."""
        prompts, data = self._get_prompts_and_data({"feature_request": FEATURE_REQUEST})
        prefix = data["scratchpad_prefix"]
        assert f"{prefix}_debugged" in prompts[2]

    def test_language_appears_in_coder_prompt(self):
        """Language should appear in the coder prompt."""
        prompts, _ = self._get_prompts_and_data({
            "feature_request": FEATURE_REQUEST,
            "language": "typescript",
        })
        assert "typescript" in prompts[1].lower()

    def test_custom_scratchpad_prefix_used_in_prompts(self):
        """Custom scratchpad_prefix should appear in prompt key references."""
        prompts, data = self._get_prompts_and_data({
            "feature_request": FEATURE_REQUEST,
            "scratchpad_prefix": "myproject_agentmesh",
        })
        prefix = data["scratchpad_prefix"]
        assert prefix == "myproject_agentmesh"
        assert "myproject_agentmesh_plan" in prompts[0]

    def test_default_prefix_auto_generated_with_agentmesh_prefix(self):
        """Default scratchpad_prefix should auto-generate as agentmesh_<8hex>."""
        prompts, data = self._get_prompts_and_data({"feature_request": FEATURE_REQUEST})
        prefix = data["scratchpad_prefix"]
        assert prefix.startswith("agentmesh_")
        assert len(prefix) > len("agentmesh_")


# ---------------------------------------------------------------------------
# Timeout and reply_to tests
# ---------------------------------------------------------------------------


class TestAgentmeshWorkflowTimeoutAndReply:
    def _capture_kwargs(self, payload: dict) -> list[dict]:
        app, orch = _make_app()
        kwargs_list = []
        original_submit = orch.submit_task

        async def capture_submit(*args, **kwargs):
            kwargs_list.append(dict(kwargs))
            return await original_submit(*args, **kwargs)

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            client.post("/workflows/agentmesh", json=payload, headers=AUTH)

        return kwargs_list

    def test_agent_timeout_forwarded_to_all_tasks(self):
        """agent_timeout should be passed as timeout= to all 4 submitted tasks."""
        kwargs_list = self._capture_kwargs({
            "feature_request": FEATURE_REQUEST,
            "agent_timeout": 600,
        })
        assert all(kw.get("timeout") == 600 for kw in kwargs_list), \
            f"Expected all 600, got {[kw.get('timeout') for kw in kwargs_list]}"

    def test_default_agent_timeout_is_300(self):
        """Default agent_timeout (300) should be passed when not specified."""
        kwargs_list = self._capture_kwargs({"feature_request": FEATURE_REQUEST})
        assert all(kw.get("timeout") == 300 for kw in kwargs_list), \
            f"Expected all 300, got {[kw.get('timeout') for kw in kwargs_list]}"

    def test_reply_to_forwarded_to_reviewer_only(self):
        """reply_to should be forwarded to the reviewer (last task) only."""
        kwargs_list = self._capture_kwargs({
            "feature_request": FEATURE_REQUEST,
            "reply_to": "director-agent",
        })
        # reviewer (index 3) should have reply_to set
        assert kwargs_list[3].get("reply_to") == "director-agent"
        # Earlier tasks should not have reply_to
        assert kwargs_list[0].get("reply_to") is None
        assert kwargs_list[1].get("reply_to") is None
        assert kwargs_list[2].get("reply_to") is None


# ---------------------------------------------------------------------------
# Workflow registration tests
# ---------------------------------------------------------------------------


class TestAgentmeshWorkflowRegistration:
    def test_workflow_registered_with_manager(self):
        """The workflow should be registered with WorkflowManager."""
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/agentmesh",
                json={"feature_request": FEATURE_REQUEST},
                headers=AUTH,
            )

        workflow_id = r.json()["workflow_id"]
        wm = orch.get_workflow_manager()
        run = wm.get(workflow_id)
        assert run is not None
        assert run.id == workflow_id

    def test_workflow_contains_all_four_tasks(self):
        """The registered workflow should contain all 4 task IDs."""
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/agentmesh",
                json={"feature_request": FEATURE_REQUEST},
                headers=AUTH,
            )

        data = r.json()
        workflow_id = data["workflow_id"]
        task_ids = list(data["task_ids"].values())

        wm = orch.get_workflow_manager()
        run = wm.get(workflow_id)
        assert set(run.task_ids) == set(task_ids)

    def test_workflow_name_contains_agentmesh(self):
        """Workflow name should identify the agentmesh pattern."""
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/agentmesh",
                json={"feature_request": FEATURE_REQUEST},
                headers=AUTH,
            )

        name = r.json()["name"]
        assert "agentmesh" in name.lower()

    def test_agentmesh_endpoint_in_openapi(self):
        """POST /workflows/agentmesh must appear in the OpenAPI schema."""
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.get("/openapi.json")
        schema = r.json()
        paths = schema.get("paths", {})
        assert "/workflows/agentmesh" in paths
        assert "post" in paths["/workflows/agentmesh"]
