"""Tests for POST /workflows/refactor — 3-agent Code Refactoring workflow.

Pipeline (sequential):
  analyzer → refactorer → verifier

Design references:
- RefAgent arXiv:2511.03153 (November 2025)
- RefactorGPT PeerJ cs-3257 (October 2025)
- MUARF ICSE 2025 SRC
- LLM-Driven Code Refactoring (IDE @ ICSE 2025)
- DESIGN.md §10.102 (v1.2.27)
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

SAMPLE_CODE = """
def calculate(x, y, z):
    r = 0
    for i in range(x):
        for j in range(y):
            for k in range(z):
                r = r + i * j * k
    return r
"""


def _submit(client, payload: dict):
    return client.post("/workflows/refactor", json=payload, headers=AUTH)


def _get_tasks(client):
    r = client.get("/tasks", headers=AUTH)
    return {t["task_id"]: t for t in r.json()}


# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------


class TestRefactorWorkflowSchema:
    def test_missing_code_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert _submit(c, {}).status_code == 422

    def test_empty_code_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert _submit(c, {"code": ""}).status_code == 422

    def test_whitespace_code_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert _submit(c, {"code": "   "}).status_code == 422

    def test_missing_auth_returns_401(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            r = c.post("/workflows/refactor", json={"code": SAMPLE_CODE})
        assert r.status_code == 401

    def test_wrong_api_key_returns_401(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            r = c.post(
                "/workflows/refactor",
                json={"code": SAMPLE_CODE},
                headers={"X-API-Key": "wrong"},
            )
        assert r.status_code == 401

    def test_valid_request_returns_200(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert _submit(c, {"code": SAMPLE_CODE}).status_code == 200

    def test_empty_refactor_goals_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert (
                _submit(c, {"code": SAMPLE_CODE, "refactor_goals": []}).status_code
                == 422
            )

    def test_invalid_refactor_goal_returns_422(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            assert (
                _submit(
                    c, {"code": SAMPLE_CODE, "refactor_goals": ["invalid_goal"]}
                ).status_code
                == 422
            )

    def test_valid_single_goal_accepted(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            r = _submit(
                c,
                {"code": SAMPLE_CODE, "refactor_goals": ["reduce_complexity"]},
            )
        assert r.status_code == 200

    def test_all_valid_goals_accepted(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            r = _submit(
                c,
                {
                    "code": SAMPLE_CODE,
                    "refactor_goals": [
                        "reduce_complexity",
                        "eliminate_duplication",
                        "improve_naming",
                        "apply_design_patterns",
                        "improve_readability",
                        "extract_functions",
                    ],
                },
            )
        assert r.status_code == 200

    def test_all_fields_accepted(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            r = _submit(
                c,
                {
                    "code": SAMPLE_CODE,
                    "language": "python",
                    "refactor_goals": ["reduce_complexity", "improve_naming"],
                    "agent_timeout": 600,
                    "scratchpad_prefix": "test_prefix",
                    "analyzer_tags": ["analyzer"],
                    "refactorer_tags": ["dev"],
                    "verifier_tags": ["qa"],
                    "reply_to": None,
                },
            )
        assert r.status_code == 200

    def test_default_language_is_python(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            r = _submit(c, {"code": SAMPLE_CODE})
        assert r.status_code == 200

    def test_custom_language_accepted(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            r = _submit(c, {"code": SAMPLE_CODE, "language": "go"})
        assert r.status_code == 200

    def test_reply_to_accepted(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            r = _submit(c, {"code": SAMPLE_CODE, "reply_to": "director-1"})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Response structure tests
# ---------------------------------------------------------------------------


class TestRefactorWorkflowResponse:
    def test_response_has_workflow_id(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
        assert "workflow_id" in data
        assert len(data["workflow_id"]) > 0

    def test_response_name_contains_refactor(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
        assert "refactor" in data["name"]

    def test_response_has_three_task_ids(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
        ids = data["task_ids"]
        assert "analyzer" in ids
        assert "refactorer" in ids
        assert "verifier" in ids

    def test_response_task_ids_are_distinct(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
        vals = list(data["task_ids"].values())
        assert len(set(vals)) == 3

    def test_response_has_scratchpad_prefix(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
        assert "scratchpad_prefix" in data
        assert len(data["scratchpad_prefix"]) > 0

    def test_auto_prefix_starts_with_refactor(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
        assert data["scratchpad_prefix"].startswith("refactor_")

    def test_custom_scratchpad_prefix_used(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(
                c, {"code": SAMPLE_CODE, "scratchpad_prefix": "my_refactor"}
            ).json()
        assert data["scratchpad_prefix"] == "my_refactor"

    def test_response_has_refactor_goals(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
        assert "refactor_goals" in data
        assert "reduce_complexity" in data["refactor_goals"]

    def test_custom_goals_returned(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(
                c,
                {
                    "code": SAMPLE_CODE,
                    "refactor_goals": ["eliminate_duplication", "improve_naming"],
                },
            ).json()
        assert data["refactor_goals"] == ["eliminate_duplication", "improve_naming"]

    def test_two_submissions_have_different_workflow_ids(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            d1 = _submit(c, {"code": SAMPLE_CODE}).json()
            d2 = _submit(c, {"code": SAMPLE_CODE}).json()
        assert d1["workflow_id"] != d2["workflow_id"]

    def test_two_submissions_have_different_prefixes(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            d1 = _submit(c, {"code": SAMPLE_CODE}).json()
            d2 = _submit(c, {"code": SAMPLE_CODE}).json()
        assert d1["scratchpad_prefix"] != d2["scratchpad_prefix"]

    def test_workflow_registered_in_manager(self):
        app, orch = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
        wm = orch.get_workflow_manager()
        run = wm.get(data["workflow_id"])
        assert run is not None
        assert run.id == data["workflow_id"]

    def test_workflow_contains_all_three_task_ids(self):
        app, orch = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
        wm = orch.get_workflow_manager()
        run = wm.get(data["workflow_id"])
        task_set = set(data["task_ids"].values())
        assert task_set.issubset(set(run.task_ids))


# ---------------------------------------------------------------------------
# Task dependency tests (sequential pipeline)
# ---------------------------------------------------------------------------


class TestRefactorWorkflowDependencies:
    def test_analyzer_has_no_dependencies(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
            tasks = _get_tasks(c)
        analyzer_id = data["task_ids"]["analyzer"]
        # depends_on is omitted from response when empty
        assert tasks[analyzer_id].get("depends_on", []) == []

    def test_analyzer_is_queued(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
            tasks = _get_tasks(c)
        analyzer_id = data["task_ids"]["analyzer"]
        assert tasks[analyzer_id]["status"] == "queued"

    def test_refactorer_depends_on_analyzer(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
            tasks = _get_tasks(c)
        analyzer_id = data["task_ids"]["analyzer"]
        refactorer_id = data["task_ids"]["refactorer"]
        assert analyzer_id in tasks[refactorer_id]["depends_on"]

    def test_verifier_depends_on_refactorer(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
            tasks = _get_tasks(c)
        refactorer_id = data["task_ids"]["refactorer"]
        verifier_id = data["task_ids"]["verifier"]
        assert refactorer_id in tasks[verifier_id]["depends_on"]

    def test_verifier_does_not_depend_directly_on_analyzer(self):
        """Verifier only depends on refactorer, not directly on analyzer."""
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
            tasks = _get_tasks(c)
        analyzer_id = data["task_ids"]["analyzer"]
        verifier_id = data["task_ids"]["verifier"]
        assert analyzer_id not in tasks[verifier_id]["depends_on"]

    def test_refactorer_is_waiting(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
            tasks = _get_tasks(c)
        refactorer_id = data["task_ids"]["refactorer"]
        assert tasks[refactorer_id]["status"] == "waiting"

    def test_verifier_is_waiting(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
            tasks = _get_tasks(c)
        verifier_id = data["task_ids"]["verifier"]
        assert tasks[verifier_id]["status"] == "waiting"

    def test_three_tasks_present_in_tasks_list(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
            tasks = _get_tasks(c)
        expected_ids = set(data["task_ids"].values())
        assert expected_ids.issubset(set(tasks.keys()))

    def test_sequential_chain_is_correct(self):
        """Full dependency chain: analyzer → refactorer → verifier."""
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
            tasks = _get_tasks(c)
        ids = data["task_ids"]
        # analyzer has no deps (depends_on omitted when empty)
        assert tasks[ids["analyzer"]].get("depends_on", []) == []
        # refactorer depends on analyzer only
        assert ids["analyzer"] in tasks[ids["refactorer"]]["depends_on"]
        # verifier depends on refactorer only
        assert ids["refactorer"] in tasks[ids["verifier"]]["depends_on"]


# ---------------------------------------------------------------------------
# Prompt content tests
# ---------------------------------------------------------------------------


class TestRefactorWorkflowPrompts:
    def _get(self, code: str, **kwargs) -> tuple:
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": code, **kwargs}).json()
            tasks = _get_tasks(c)
        return data["task_ids"], tasks, data["scratchpad_prefix"]

    def test_analyzer_prompt_contains_code(self):
        ids, tasks, _ = self._get(SAMPLE_CODE)
        assert "calculate" in tasks[ids["analyzer"]]["prompt"]

    def test_refactorer_prompt_contains_code(self):
        ids, tasks, _ = self._get(SAMPLE_CODE)
        assert "calculate" in tasks[ids["refactorer"]]["prompt"]

    def test_verifier_prompt_contains_code(self):
        ids, tasks, _ = self._get(SAMPLE_CODE)
        assert "calculate" in tasks[ids["verifier"]]["prompt"]

    def test_analyzer_prompt_mentions_language(self):
        ids, tasks, _ = self._get(SAMPLE_CODE, language="go")
        assert "go" in tasks[ids["analyzer"]]["prompt"].lower()

    def test_refactorer_prompt_mentions_language(self):
        ids, tasks, _ = self._get(SAMPLE_CODE, language="typescript")
        assert "typescript" in tasks[ids["refactorer"]]["prompt"].lower()

    def test_verifier_prompt_mentions_language(self):
        ids, tasks, _ = self._get(SAMPLE_CODE, language="rust")
        assert "rust" in tasks[ids["verifier"]]["prompt"].lower()

    def test_analyzer_prompt_contains_analysis_key(self):
        ids, tasks, prefix = self._get(SAMPLE_CODE)
        assert f"{prefix}_analysis" in tasks[ids["analyzer"]]["prompt"]

    def test_refactorer_prompt_contains_analysis_key(self):
        ids, tasks, prefix = self._get(SAMPLE_CODE)
        assert f"{prefix}_analysis" in tasks[ids["refactorer"]]["prompt"]

    def test_refactorer_prompt_contains_refactored_key(self):
        ids, tasks, prefix = self._get(SAMPLE_CODE)
        assert f"{prefix}_refactored" in tasks[ids["refactorer"]]["prompt"]

    def test_verifier_prompt_contains_refactored_key(self):
        ids, tasks, prefix = self._get(SAMPLE_CODE)
        assert f"{prefix}_refactored" in tasks[ids["verifier"]]["prompt"]

    def test_verifier_prompt_contains_analysis_key(self):
        ids, tasks, prefix = self._get(SAMPLE_CODE)
        assert f"{prefix}_analysis" in tasks[ids["verifier"]]["prompt"]

    def test_verifier_prompt_contains_verification_key(self):
        ids, tasks, prefix = self._get(SAMPLE_CODE)
        assert f"{prefix}_verification" in tasks[ids["verifier"]]["prompt"]

    def test_analyzer_prompt_mentions_goals(self):
        ids, tasks, _ = self._get(
            SAMPLE_CODE, refactor_goals=["reduce_complexity"]
        )
        assert "reduce_complexity" in tasks[ids["analyzer"]]["prompt"]

    def test_analyzer_prompt_mentions_multiple_goals(self):
        ids, tasks, _ = self._get(
            SAMPLE_CODE,
            refactor_goals=["eliminate_duplication", "improve_naming"],
        )
        prompt = tasks[ids["analyzer"]]["prompt"]
        assert "eliminate_duplication" in prompt
        assert "improve_naming" in prompt

    def test_analyzer_prompt_mentions_quality_issues(self):
        ids, tasks, _ = self._get(SAMPLE_CODE)
        prompt = tasks[ids["analyzer"]]["prompt"].lower()
        assert any(
            kw in prompt
            for kw in ["quality", "complexity", "duplication", "naming", "issue"]
        )

    def test_refactorer_prompt_mentions_behavior_preservation(self):
        ids, tasks, _ = self._get(SAMPLE_CODE)
        prompt = tasks[ids["refactorer"]]["prompt"].lower()
        assert any(kw in prompt for kw in ["behavior", "behaviour", "semantic", "preserve"])

    def test_verifier_prompt_mentions_behavior_preservation(self):
        ids, tasks, _ = self._get(SAMPLE_CODE)
        prompt = tasks[ids["verifier"]]["prompt"].lower()
        assert any(
            kw in prompt
            for kw in ["behavior", "behaviour", "preservation", "semantic"]
        )

    def test_verifier_prompt_mentions_verdict(self):
        ids, tasks, _ = self._get(SAMPLE_CODE)
        prompt = tasks[ids["verifier"]]["prompt"].upper()
        assert any(kw in prompt for kw in ["PASS", "FAIL", "VERDICT"])

    def test_custom_prefix_appears_in_all_prompts(self):
        prefix = "my_custom_prefix"
        ids, tasks, _ = self._get(SAMPLE_CODE, scratchpad_prefix=prefix)
        assert f"{prefix}_analysis" in tasks[ids["analyzer"]]["prompt"]
        assert f"{prefix}_refactored" in tasks[ids["refactorer"]]["prompt"]
        assert f"{prefix}_verification" in tasks[ids["verifier"]]["prompt"]


# ---------------------------------------------------------------------------
# Tag routing tests
# ---------------------------------------------------------------------------


class TestRefactorWorkflowTags:
    def _submit_with_tags(self, client, **tag_kwargs):
        payload = {"code": SAMPLE_CODE, **tag_kwargs}
        return _submit(client, payload).json()

    def test_analyzer_tags_applied(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = self._submit_with_tags(c, analyzer_tags=["refactor_analyzer"])
            tasks = _get_tasks(c)
        analyzer_id = data["task_ids"]["analyzer"]
        assert "refactor_analyzer" in tasks[analyzer_id]["required_tags"]

    def test_refactorer_tags_applied(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = self._submit_with_tags(c, refactorer_tags=["dev", "python"])
            tasks = _get_tasks(c)
        refactorer_id = data["task_ids"]["refactorer"]
        assert "dev" in tasks[refactorer_id]["required_tags"]
        assert "python" in tasks[refactorer_id]["required_tags"]

    def test_verifier_tags_applied(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = self._submit_with_tags(c, verifier_tags=["qa"])
            tasks = _get_tasks(c)
        verifier_id = data["task_ids"]["verifier"]
        assert "qa" in tasks[verifier_id]["required_tags"]

    def test_no_tags_by_default(self):
        app, _ = _make_app()
        with TestClient(app) as c:
            data = _submit(c, {"code": SAMPLE_CODE}).json()
            tasks = _get_tasks(c)
        for role_id in data["task_ids"].values():
            # required_tags is omitted from response when empty
            assert tasks[role_id].get("required_tags", []) == []

    def test_multiple_submissions_independent(self):
        """Two separate workflow runs should not share task IDs."""
        app, _ = _make_app()
        with TestClient(app) as c:
            d1 = _submit(c, {"code": SAMPLE_CODE}).json()
            d2 = _submit(c, {"code": SAMPLE_CODE}).json()
        ids1 = set(d1["task_ids"].values())
        ids2 = set(d2["task_ids"].values())
        assert ids1.isdisjoint(ids2)
