"""Tests for POST /workflows/adr — Architecture Decision Record auto-generation workflow.

3-agent pipeline: proposer → reviewer → synthesizer, producing a MADR-format DECISION.md.

Design references:
- AgenticAKM arXiv:2602.04445 (2026): multi-agent ADR generation improves quality
- Ochoa et al. arXiv:2507.05981 (2025): MAD enhances requirements engineering
- MADR 4.0.0 (2024-09): Markdown Architectural Decision Records format standard
- DESIGN.md §10.14 (v0.40.0)
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

class TestADRWorkflowSchema:
    def test_missing_topic_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post("/workflows/adr", json={}, headers=AUTH)
        assert r.status_code == 422

    def test_empty_topic_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post("/workflows/adr", json={"topic": ""}, headers=AUTH)
        assert r.status_code == 422

    def test_missing_auth_returns_401(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post("/workflows/adr", json={"topic": "SQLite vs PostgreSQL"})
        assert r.status_code == 401

    def test_minimal_request_returns_200(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/adr",
                json={"topic": "SQLite vs PostgreSQL"},
                headers=AUTH,
            )
        assert r.status_code == 200

    def test_response_contains_workflow_id(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/adr",
                json={"topic": "REST vs GraphQL"},
                headers=AUTH,
            )
        data = r.json()
        assert "workflow_id" in data
        assert data["workflow_id"]

    def test_response_contains_task_ids(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/adr",
                json={"topic": "Microservices vs Monolith"},
                headers=AUTH,
            )
        data = r.json()
        assert "task_ids" in data
        task_ids = data["task_ids"]
        assert "proposer" in task_ids
        assert "reviewer" in task_ids
        assert "synthesizer" in task_ids

    def test_response_contains_scratchpad_prefix(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/adr",
                json={"topic": "gRPC vs REST"},
                headers=AUTH,
            )
        data = r.json()
        assert "scratchpad_prefix" in data
        assert data["scratchpad_prefix"]

    def test_response_contains_name_with_adr_prefix(self):
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/adr",
                json={"topic": "PostgreSQL vs MySQL"},
                headers=AUTH,
            )
        data = r.json()
        assert "name" in data
        assert "adr" in data["name"].lower() or "postgresql" in data["name"].lower()


# ---------------------------------------------------------------------------
# Workflow DAG structure tests
# ---------------------------------------------------------------------------

class TestADRWorkflowDAG:
    def test_three_tasks_submitted(self):
        """Exactly 3 tasks should be submitted: proposer, reviewer, synthesizer."""
        app, orch = _make_app()
        submitted = []
        original_submit = orch.submit_task

        async def capture_submit(*args, **kwargs):
            task = await original_submit(*args, **kwargs)
            submitted.append(task)
            return task

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            client.post(
                "/workflows/adr",
                json={"topic": "SQLite vs PostgreSQL"},
                headers=AUTH,
            )

        assert len(submitted) == 3

    def test_reviewer_depends_on_proposer(self):
        """reviewer task must depend on proposer task."""
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
                "/workflows/adr",
                json={"topic": "SQLite vs PostgreSQL"},
                headers=AUTH,
            )

        assert len(submitted) == 3
        proposer_id = submitted[0][0].id
        reviewer_depends = submitted[1][1]
        assert proposer_id in reviewer_depends

    def test_synthesizer_depends_on_reviewer(self):
        """synthesizer task must depend on reviewer task."""
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
                "/workflows/adr",
                json={"topic": "SQLite vs PostgreSQL"},
                headers=AUTH,
            )

        reviewer_id = submitted[1][0].id
        synthesizer_depends = submitted[2][1]
        assert reviewer_id in synthesizer_depends

    def test_proposer_has_no_dependencies(self):
        """proposer is the first task and should have no dependencies."""
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
                "/workflows/adr",
                json={"topic": "SQLite vs PostgreSQL"},
                headers=AUTH,
            )

        proposer_depends = submitted[0][1]
        assert not proposer_depends


# ---------------------------------------------------------------------------
# Prompt content tests
# ---------------------------------------------------------------------------

class TestADRWorkflowPrompts:
    def _get_prompts(self, topic: str = "SQLite vs PostgreSQL") -> list[str]:
        app, orch = _make_app()
        prompts = []
        original_submit = orch.submit_task

        async def capture_submit(prompt, *args, **kwargs):
            prompts.append(prompt)
            return await original_submit(prompt, *args, **kwargs)

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            client.post("/workflows/adr", json={"topic": topic}, headers=AUTH)

        return prompts

    def test_proposer_prompt_contains_topic(self):
        prompts = self._get_prompts("SQLite vs PostgreSQL")
        assert "SQLite vs PostgreSQL" in prompts[0]

    def test_proposer_prompt_mentions_options(self):
        """Proposer should be asked to consider both options."""
        prompts = self._get_prompts("SQLite vs PostgreSQL")
        proposer_prompt = prompts[0].lower()
        # Should mention the topic in context of options
        assert "option" in proposer_prompt or "consider" in proposer_prompt or "sqlite" in proposer_prompt

    def test_reviewer_prompt_reads_scratchpad(self):
        """Reviewer should read proposer's output from scratchpad."""
        prompts = self._get_prompts()
        reviewer_prompt = prompts[1].lower()
        assert "scratchpad" in reviewer_prompt or "curl" in reviewer_prompt

    def test_synthesizer_prompt_writes_decision_md(self):
        """Synthesizer should write DECISION.md."""
        prompts = self._get_prompts()
        synth_prompt = prompts[2]
        assert "DECISION.md" in synth_prompt or "decision.md" in synth_prompt.lower()

    def test_synthesizer_prompt_mentions_madr(self):
        """Synthesizer should use MADR format."""
        prompts = self._get_prompts()
        synth_prompt = prompts[2].lower()
        assert "madr" in synth_prompt or "decision outcome" in synth_prompt or "considered options" in synth_prompt

    def test_synthesizer_prompt_reads_both_draft_and_review(self):
        """Synthesizer should read both ADR draft and reviewer artifacts from scratchpad."""
        prompts = self._get_prompts()
        synth_prompt = prompts[2].lower()
        # v1.1.40: proposer writes _draft key; synthesizer reads draft + review
        assert "draft" in synth_prompt or "review" in synth_prompt


# ---------------------------------------------------------------------------
# Optional fields tests
# ---------------------------------------------------------------------------

class TestADRWorkflowOptionalFields:
    def test_proposer_tags_applied(self):
        """proposer_tags routes proposer task to agents with those tags."""
        app, orch = _make_app()
        tags_received = []
        original_submit = orch.submit_task

        async def capture_submit(*args, **kwargs):
            tags_received.append(kwargs.get("required_tags"))
            return await original_submit(*args, **kwargs)

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            client.post(
                "/workflows/adr",
                json={
                    "topic": "ADR topic",
                    "proposer_tags": ["adr-proposer"],
                    "reviewer_tags": ["adr-reviewer"],
                    "synthesizer_tags": ["adr-synthesizer"],
                },
                headers=AUTH,
            )

        assert tags_received[0] == ["adr-proposer"]
        assert tags_received[1] == ["adr-reviewer"]
        assert tags_received[2] == ["adr-synthesizer"]

    def test_empty_tags_treated_as_none(self):
        """Empty tag lists should be treated as None (any agent)."""
        app, orch = _make_app()
        tags_received = []
        original_submit = orch.submit_task

        async def capture_submit(*args, **kwargs):
            tags_received.append(kwargs.get("required_tags"))
            return await original_submit(*args, **kwargs)

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            client.post(
                "/workflows/adr",
                json={"topic": "ADR topic"},
                headers=AUTH,
            )

        # All tags should be None (no required_tags filtering)
        for t in tags_received:
            assert t is None or t == []

    def test_reply_to_forwarded_to_synthesizer(self):
        """reply_to parameter should be forwarded to the synthesizer task."""
        app, orch = _make_app()
        reply_tos = []
        original_submit = orch.submit_task

        async def capture_submit(*args, **kwargs):
            reply_tos.append(kwargs.get("reply_to"))
            return await original_submit(*args, **kwargs)

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            client.post(
                "/workflows/adr",
                json={"topic": "ADR topic", "reply_to": "director-agent"},
                headers=AUTH,
            )

        # reply_to should be on the synthesizer (last task)
        assert reply_tos[2] == "director-agent"
        # Earlier tasks should not have reply_to
        assert reply_tos[0] is None
        assert reply_tos[1] is None


# ---------------------------------------------------------------------------
# Workflow registration tests
# ---------------------------------------------------------------------------

class TestADRWorkflowRegistration:
    def test_workflow_registered_with_manager(self):
        """The workflow should be registered with WorkflowManager."""
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/adr",
                json={"topic": "SQLite vs PostgreSQL"},
                headers=AUTH,
            )

        workflow_id = r.json()["workflow_id"]
        wm = orch.get_workflow_manager()
        run = wm.get(workflow_id)
        assert run is not None
        assert run.id == workflow_id

    def test_workflow_contains_all_three_tasks(self):
        """The registered workflow should contain all 3 task IDs."""
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/adr",
                json={"topic": "SQLite vs PostgreSQL"},
                headers=AUTH,
            )

        data = r.json()
        workflow_id = data["workflow_id"]
        task_ids = list(data["task_ids"].values())

        wm = orch.get_workflow_manager()
        run = wm.get(workflow_id)
        assert set(run.task_ids) == set(task_ids)

    def test_workflow_name_contains_topic(self):
        """Workflow name should identify the topic."""
        app, orch = _make_app()
        with TestClient(app) as client:
            r = client.post(
                "/workflows/adr",
                json={"topic": "SQLite vs PostgreSQL"},
                headers=AUTH,
            )

        name = r.json()["name"]
        assert "SQLite" in name or "sqlite" in name.lower() or "adr" in name.lower()

    def test_adr_endpoint_in_openapi(self):
        """POST /workflows/adr must appear in the OpenAPI schema."""
        app, _ = _make_app()
        with TestClient(app) as client:
            r = client.get("/openapi.json")
        schema = r.json()
        paths = schema.get("paths", {})
        assert "/workflows/adr" in paths
        assert "post" in paths["/workflows/adr"]


# ---------------------------------------------------------------------------
# v1.1.40: Enhanced fields — context, criteria, scratchpad_prefix, agent_timeout
# ---------------------------------------------------------------------------


class TestADRWorkflowEnhancedFields:
    def _get_prompts_and_data(self, payload: dict) -> tuple[list[str], dict]:
        app, orch = _make_app()
        prompts = []
        original_submit = orch.submit_task

        async def capture_submit(prompt, *args, **kwargs):
            prompts.append(prompt)
            return await original_submit(prompt, *args, **kwargs)

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            r = client.post("/workflows/adr", json=payload, headers=AUTH)

        return prompts, r.json()

    def test_context_included_in_proposer_prompt(self):
        """context field should appear in the proposer prompt."""
        ctx_text = "We have 10k sessions per day and need fast reads."
        prompts, _ = self._get_prompts_and_data({
            "topic": "SQLite vs PostgreSQL",
            "context": ctx_text,
        })
        assert ctx_text in prompts[0]

    def test_context_included_in_reviewer_prompt(self):
        """context field should appear in the reviewer prompt."""
        ctx_text = "Budget constraint: no managed cloud DB."
        prompts, _ = self._get_prompts_and_data({
            "topic": "SQLite vs PostgreSQL",
            "context": ctx_text,
        })
        assert ctx_text in prompts[1]

    def test_context_included_in_synthesizer_prompt(self):
        """context field should appear in the synthesizer prompt."""
        ctx_text = "Team has 2 engineers, no DBA."
        prompts, _ = self._get_prompts_and_data({
            "topic": "SQLite vs PostgreSQL",
            "context": ctx_text,
        })
        assert ctx_text in prompts[2]

    def test_criteria_included_in_proposer_prompt(self):
        """criteria list should appear in the proposer prompt."""
        prompts, _ = self._get_prompts_and_data({
            "topic": "SQLite vs PostgreSQL",
            "criteria": ["performance", "operability", "cost"],
        })
        assert "performance" in prompts[0]
        assert "operability" in prompts[0]
        assert "cost" in prompts[0]

    def test_empty_context_not_spuriously_added(self):
        """Empty context string should not add noisy empty-context line."""
        prompts, _ = self._get_prompts_and_data({
            "topic": "SQLite vs PostgreSQL",
            "context": "",
        })
        # An empty context= should not inject a "Context:" line with blank value
        assert "**Context:**  " not in prompts[0]

    def test_custom_scratchpad_prefix_used(self):
        """Custom scratchpad_prefix should appear in the scratchpad key references."""
        prompts, data = self._get_prompts_and_data({
            "topic": "SQLite vs PostgreSQL",
            "scratchpad_prefix": "myproject_adr",
        })
        # The response prefix should match our custom prefix
        assert data["scratchpad_prefix"] == "myproject_adr"
        # Proposer prompt should reference our prefix
        assert "myproject_adr_draft" in prompts[0]

    def test_default_scratchpad_prefix_auto_generated(self):
        """Default scratchpad_prefix (adr) should generate an auto-prefixed key."""
        prompts, data = self._get_prompts_and_data({
            "topic": "SQLite vs PostgreSQL",
        })
        prefix = data["scratchpad_prefix"]
        # Should be something like adr_<8hex>
        assert prefix.startswith("adr_")
        assert len(prefix) > 4

    def test_draft_key_in_proposer_prompt(self):
        """Proposer should write to the _draft scratchpad key (not _proposal)."""
        prompts, data = self._get_prompts_and_data({"topic": "SQLite vs PostgreSQL"})
        prefix = data["scratchpad_prefix"]
        assert f"{prefix}_draft" in prompts[0]

    def test_review_key_in_reviewer_prompt(self):
        """Reviewer should write to the _review scratchpad key."""
        prompts, data = self._get_prompts_and_data({"topic": "SQLite vs PostgreSQL"})
        prefix = data["scratchpad_prefix"]
        assert f"{prefix}_review" in prompts[1]

    def test_final_key_in_synthesizer_prompt(self):
        """Synthesizer should write to the _final scratchpad key (not _decision)."""
        prompts, data = self._get_prompts_and_data({"topic": "SQLite vs PostgreSQL"})
        prefix = data["scratchpad_prefix"]
        assert f"{prefix}_final" in prompts[2]

    def test_agent_timeout_forwarded_to_tasks(self):
        """agent_timeout should be passed as timeout= to all submitted tasks."""
        app, orch = _make_app()
        timeouts = []
        original_submit = orch.submit_task

        async def capture_submit(*args, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            return await original_submit(*args, **kwargs)

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            client.post(
                "/workflows/adr",
                json={"topic": "SQLite vs PostgreSQL", "agent_timeout": 600},
                headers=AUTH,
            )

        assert all(t == 600 for t in timeouts), f"Expected all 600, got {timeouts}"

    def test_default_agent_timeout_is_300(self):
        """Default agent_timeout (300) should be passed when not specified."""
        app, orch = _make_app()
        timeouts = []
        original_submit = orch.submit_task

        async def capture_submit(*args, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            return await original_submit(*args, **kwargs)

        orch.submit_task = capture_submit

        with TestClient(app) as client:
            client.post(
                "/workflows/adr",
                json={"topic": "SQLite vs PostgreSQL"},
                headers=AUTH,
            )

        assert all(t == 300 for t in timeouts), f"Expected all 300, got {timeouts}"
