"""Tests for POST /workflows/code-audit — 4-agent Code Audit workflow.

Pipeline (fan-out/fan-in):
  implementer → [security_auditor ∥ performance_auditor] → synthesizer

Design references:
- RepoAudit arXiv:2501.18160 ICML 2025
- iAudit ICSE 2025
- DESIGN.md §10.101 (v1.2.26)
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
    return client.post("/workflows/code-audit", json=payload, headers=AUTH)


def _get_tasks(client):
    r = client.get("/tasks", headers=AUTH)
    return {t["task_id"]: t for t in r.json()}


# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------


class TestCodeAuditWorkflowSchema:
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
            r = c.post("/workflows/code-audit", json={"feature": "a module"})
        assert r.status_code == 401

    def test_wrong_api_key_returns_401(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            r = c.post(
                "/workflows/code-audit",
                json={"feature": "a module"},
                headers={"X-API-Key": "wrong"},
            )
        assert r.status_code == 401

    def test_valid_request_returns_200(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert _submit(c, {"feature": "a stack"}).status_code == 200

    def test_empty_audit_focus_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert _submit(c, {"feature": "a q", "audit_focus": []}).status_code == 422

    def test_invalid_audit_focus_item_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert (
                _submit(c, {"feature": "a q", "audit_focus": ["style"]}).status_code
                == 422
            )

    def test_valid_audit_focus_security_only(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert (
                _submit(c, {"feature": "a q", "audit_focus": ["security"]}).status_code
                == 200
            )

    def test_valid_audit_focus_performance_only(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert (
                _submit(
                    c, {"feature": "a q", "audit_focus": ["performance"]}
                ).status_code
                == 200
            )

    def test_all_fields_accepted(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            r = _submit(
                c,
                {
                    "feature": "a LRU cache",
                    "language": "python",
                    "audit_focus": ["security", "performance"],
                    "agent_timeout": 600,
                    "scratchpad_prefix": "test_prefix",
                    "implementer_tags": ["dev"],
                    "security_auditor_tags": ["security"],
                    "performance_auditor_tags": ["perf"],
                    "synthesizer_tags": ["lead"],
                    "reply_to": None,
                },
            )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Response structure tests
# ---------------------------------------------------------------------------


class TestCodeAuditWorkflowResponse:
    def test_response_has_workflow_id(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a stack"}).json()
        assert "workflow_id" in data
        assert len(data["workflow_id"]) > 0

    def test_response_name_contains_code_audit(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a stack"}).json()
        assert "code-audit" in data["name"]
        assert "a stack" in data["name"]

    def test_response_has_four_task_ids(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a queue"}).json()
        ids = data["task_ids"]
        assert "implementer" in ids
        assert "security_auditor" in ids
        assert "performance_auditor" in ids
        assert "synthesizer" in ids

    def test_response_task_ids_are_distinct(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a deque"}).json()
        vals = list(data["task_ids"].values())
        assert len(set(vals)) == 4

    def test_response_has_scratchpad_prefix(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a set"}).json()
        assert "scratchpad_prefix" in data
        assert len(data["scratchpad_prefix"]) > 0

    def test_auto_prefix_starts_with_audit(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a heap"}).json()
        assert data["scratchpad_prefix"].startswith("audit_")

    def test_custom_scratchpad_prefix_used(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(
                c, {"feature": "a counter", "scratchpad_prefix": "my_prefix"}
            ).json()
        assert data["scratchpad_prefix"] == "my_prefix"

    def test_response_has_audit_focus(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a tree"}).json()
        assert "audit_focus" in data
        assert "security" in data["audit_focus"]
        assert "performance" in data["audit_focus"]

    def test_custom_audit_focus_returned(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(
                c, {"feature": "a graph", "audit_focus": ["security"]}
            ).json()
        assert data["audit_focus"] == ["security"]

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
# Task dependency tests (fan-out / fan-in DAG)
# ---------------------------------------------------------------------------


class TestCodeAuditWorkflowDependencies:
    def test_implementer_is_queued_no_deps(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a stack"}).json()
            tasks = _get_tasks(c)
        impl_id = data["task_ids"]["implementer"]
        assert tasks[impl_id]["status"] == "queued"

    def test_security_auditor_depends_on_implementer(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a min-heap"}).json()
            tasks = _get_tasks(c)
        impl_id = data["task_ids"]["implementer"]
        sec_id = data["task_ids"]["security_auditor"]
        assert impl_id in tasks[sec_id]["depends_on"]

    def test_performance_auditor_depends_on_implementer(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a B-tree"}).json()
            tasks = _get_tasks(c)
        impl_id = data["task_ids"]["implementer"]
        perf_id = data["task_ids"]["performance_auditor"]
        assert impl_id in tasks[perf_id]["depends_on"]

    def test_synthesizer_depends_on_security_auditor(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a hash table"}).json()
            tasks = _get_tasks(c)
        sec_id = data["task_ids"]["security_auditor"]
        synth_id = data["task_ids"]["synthesizer"]
        assert sec_id in tasks[synth_id]["depends_on"]

    def test_synthesizer_depends_on_performance_auditor(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a trie"}).json()
            tasks = _get_tasks(c)
        perf_id = data["task_ids"]["performance_auditor"]
        synth_id = data["task_ids"]["synthesizer"]
        assert perf_id in tasks[synth_id]["depends_on"]

    def test_security_auditor_is_waiting(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a list"}).json()
            tasks = _get_tasks(c)
        sec_id = data["task_ids"]["security_auditor"]
        assert tasks[sec_id]["status"] == "waiting"

    def test_performance_auditor_is_waiting(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a sorted set"}).json()
            tasks = _get_tasks(c)
        perf_id = data["task_ids"]["performance_auditor"]
        assert tasks[perf_id]["status"] == "waiting"

    def test_synthesizer_is_waiting(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a binary search tree"}).json()
            tasks = _get_tasks(c)
        synth_id = data["task_ids"]["synthesizer"]
        assert tasks[synth_id]["status"] == "waiting"

    def test_four_tasks_present_in_tasks_list(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a skip list"}).json()
            tasks = _get_tasks(c)
        expected_ids = set(data["task_ids"].values())
        assert expected_ids.issubset(set(tasks.keys()))

    def test_workflow_registered(self):
        app, orch = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a ring buffer"}).json()
        wm = orch.get_workflow_manager()
        run = wm.get(data["workflow_id"])
        assert run is not None
        assert run.id == data["workflow_id"]

    def test_security_and_performance_auditors_both_depend_on_implementer(self):
        """Both parallel auditors must wait for the implementer to finish."""
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a bloom filter"}).json()
            tasks = _get_tasks(c)
        impl_id = data["task_ids"]["implementer"]
        sec_id = data["task_ids"]["security_auditor"]
        perf_id = data["task_ids"]["performance_auditor"]
        assert impl_id in tasks[sec_id]["depends_on"]
        assert impl_id in tasks[perf_id]["depends_on"]

    def test_synthesizer_depends_on_both_auditors(self):
        """Synthesizer must wait for both auditors to finish."""
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": "a graph"}).json()
            tasks = _get_tasks(c)
        sec_id = data["task_ids"]["security_auditor"]
        perf_id = data["task_ids"]["performance_auditor"]
        synth_id = data["task_ids"]["synthesizer"]
        assert sec_id in tasks[synth_id]["depends_on"]
        assert perf_id in tasks[synth_id]["depends_on"]


# ---------------------------------------------------------------------------
# Prompt content tests
# ---------------------------------------------------------------------------


class TestCodeAuditWorkflowPrompts:
    def _get(self, feature: str, **kwargs) -> tuple:
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"feature": feature, **kwargs}).json()
            tasks = _get_tasks(c)
        return data["task_ids"], tasks, data["scratchpad_prefix"]

    def test_implementer_prompt_mentions_feature(self):
        ids, tasks, _ = self._get("a priority queue")
        assert "priority queue" in tasks[ids["implementer"]]["prompt"]

    def test_security_auditor_prompt_mentions_feature(self):
        ids, tasks, _ = self._get("a doubly linked list")
        assert "doubly linked list" in tasks[ids["security_auditor"]]["prompt"]

    def test_performance_auditor_prompt_mentions_feature(self):
        ids, tasks, _ = self._get("a circular buffer")
        assert "circular buffer" in tasks[ids["performance_auditor"]]["prompt"]

    def test_synthesizer_prompt_mentions_feature(self):
        ids, tasks, _ = self._get("a stack")
        assert "a stack" in tasks[ids["synthesizer"]]["prompt"]

    def test_implementer_prompt_mentions_language(self):
        ids, tasks, _ = self._get("a queue", language="go")
        assert "go" in tasks[ids["implementer"]]["prompt"].lower()

    def test_implementer_prompt_contains_impl_key(self):
        ids, tasks, prefix = self._get("a counter")
        assert f"{prefix}_impl" in tasks[ids["implementer"]]["prompt"]

    def test_security_auditor_prompt_contains_impl_key(self):
        ids, tasks, prefix = self._get("a set")
        assert f"{prefix}_impl" in tasks[ids["security_auditor"]]["prompt"]

    def test_security_auditor_prompt_contains_security_key(self):
        ids, tasks, prefix = self._get("a map")
        assert f"{prefix}_security_audit" in tasks[ids["security_auditor"]]["prompt"]

    def test_performance_auditor_prompt_contains_impl_key(self):
        ids, tasks, prefix = self._get("a ring buffer")
        assert f"{prefix}_impl" in tasks[ids["performance_auditor"]]["prompt"]

    def test_performance_auditor_prompt_contains_perf_key(self):
        ids, tasks, prefix = self._get("a bloom filter")
        assert f"{prefix}_performance_audit" in tasks[ids["performance_auditor"]]["prompt"]

    def test_synthesizer_prompt_contains_security_key(self):
        ids, tasks, prefix = self._get("a graph")
        assert f"{prefix}_security_audit" in tasks[ids["synthesizer"]]["prompt"]

    def test_synthesizer_prompt_contains_perf_key(self):
        ids, tasks, prefix = self._get("a tree")
        assert f"{prefix}_performance_audit" in tasks[ids["synthesizer"]]["prompt"]

    def test_synthesizer_prompt_contains_report_key(self):
        ids, tasks, prefix = self._get("a B-tree")
        assert f"{prefix}_audit_report" in tasks[ids["synthesizer"]]["prompt"]

    def test_security_auditor_prompt_mentions_owasp(self):
        ids, tasks, _ = self._get("a user auth module")
        prompt = tasks[ids["security_auditor"]]["prompt"].lower()
        assert "owasp" in prompt or "cwe" in prompt or "security" in prompt

    def test_performance_auditor_prompt_mentions_complexity(self):
        ids, tasks, _ = self._get("a hash map")
        prompt = tasks[ids["performance_auditor"]]["prompt"].lower()
        assert "complexity" in prompt or "performance" in prompt or "caching" in prompt

    def test_synthesizer_prompt_mentions_audit_report(self):
        ids, tasks, _ = self._get("a parser")
        prompt = tasks[ids["synthesizer"]]["prompt"].lower()
        assert "audit" in prompt or "report" in prompt or "findings" in prompt


# ---------------------------------------------------------------------------
# Tags tests
# ---------------------------------------------------------------------------


class TestCodeAuditWorkflowTags:
    def test_implementer_tags_applied(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(
                c, {"feature": "a stack", "implementer_tags": ["senior_dev"]}
            ).json()
            tasks = _get_tasks(c)
        assert "senior_dev" in tasks[data["task_ids"]["implementer"]]["required_tags"]

    def test_security_auditor_tags_applied(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(
                c,
                {"feature": "a queue", "security_auditor_tags": ["security_expert"]},
            ).json()
            tasks = _get_tasks(c)
        assert "security_expert" in tasks[data["task_ids"]["security_auditor"]]["required_tags"]

    def test_performance_auditor_tags_applied(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(
                c,
                {"feature": "a set", "performance_auditor_tags": ["perf_specialist"]},
            ).json()
            tasks = _get_tasks(c)
        assert "perf_specialist" in tasks[data["task_ids"]["performance_auditor"]]["required_tags"]

    def test_synthesizer_tags_applied(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(
                c, {"feature": "a map", "synthesizer_tags": ["lead_reviewer"]}
            ).json()
            tasks = _get_tasks(c)
        assert "lead_reviewer" in tasks[data["task_ids"]["synthesizer"]]["required_tags"]


# ---------------------------------------------------------------------------
# Pydantic model unit tests
# ---------------------------------------------------------------------------


class TestCodeAuditWorkflowSubmitModel:
    def test_defaults(self):
        from tmux_orchestrator.web.schemas import CodeAuditWorkflowSubmit

        m = CodeAuditWorkflowSubmit(feature="a stack")
        assert m.language == "python"
        assert m.audit_focus == ["security", "performance"]
        assert m.agent_timeout == 300
        assert m.implementer_tags == []
        assert m.security_auditor_tags == []
        assert m.performance_auditor_tags == []
        assert m.synthesizer_tags == []
        assert m.reply_to is None
        assert m.scratchpad_prefix == ""

    def test_empty_feature_raises(self):
        import pydantic
        from tmux_orchestrator.web.schemas import CodeAuditWorkflowSubmit

        with pytest.raises(pydantic.ValidationError):
            CodeAuditWorkflowSubmit(feature="")

    def test_empty_audit_focus_raises(self):
        import pydantic
        from tmux_orchestrator.web.schemas import CodeAuditWorkflowSubmit

        with pytest.raises(pydantic.ValidationError):
            CodeAuditWorkflowSubmit(feature="a q", audit_focus=[])

    def test_invalid_audit_focus_raises(self):
        import pydantic
        from tmux_orchestrator.web.schemas import CodeAuditWorkflowSubmit

        with pytest.raises(pydantic.ValidationError):
            CodeAuditWorkflowSubmit(feature="a q", audit_focus=["style"])

    def test_valid_security_only(self):
        from tmux_orchestrator.web.schemas import CodeAuditWorkflowSubmit

        m = CodeAuditWorkflowSubmit(feature="a q", audit_focus=["security"])
        assert m.audit_focus == ["security"]

    def test_valid_performance_only(self):
        from tmux_orchestrator.web.schemas import CodeAuditWorkflowSubmit

        m = CodeAuditWorkflowSubmit(feature="a q", audit_focus=["performance"])
        assert m.audit_focus == ["performance"]
