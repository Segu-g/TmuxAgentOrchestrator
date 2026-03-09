"""Tests for POST /workflows/mob-review — Mob Code Review workflow.

The mob-review workflow builds an (N+1)-agent DAG:

  reviewer_security       ──┐
  reviewer_performance    ──┼─→ synthesizer
  reviewer_maintainability──┤
  reviewer_testing        ──┘

- reviewer_{aspect}: examines the code from one quality dimension (e.g. security),
  writes findings to review_{aspect}.md, stores in scratchpad.
- synthesizer: reads all aspect reviews from the scratchpad, produces
  MOB_REVIEW.md with a unified severity table and recommendations.

Design references:
- ChatEval (arXiv:2308.07201, ICLR 2024): unique reviewer personas are essential.
- Agent-as-a-Judge (arXiv:2508.02994, 2025): aggregating independent judgements
  reduces variance akin to a voting committee.
- Code in Harmony (OpenReview 2025): parallel multi-agent code evaluation.
- DESIGN.md §10.52 (v1.1.20)
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app
from tmux_orchestrator.web.schemas import MobReviewWorkflowSubmit
import tmux_orchestrator.web.app as web_app_mod


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


_API_KEY = "test-key"

_SAMPLE_CODE = "def get_user(user_id, db):\n    return db.execute('SELECT * FROM users WHERE id = ' + user_id).fetchone()"
_TWO_ASPECTS = ["security", "performance"]
_DEFAULT_ASPECTS = ["security", "performance", "maintainability", "testing"]


@pytest.fixture()
def client():
    app, orch = _make_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def client_and_orch():
    app, orch = _make_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, orch


@pytest.fixture(autouse=True)
def reset_state():
    """Reset module-level state before each test."""
    web_app_mod._scratchpad.clear()
    yield


def auth_headers() -> dict:
    return {"X-API-Key": _API_KEY}


def _get_tasks(client) -> dict:
    """Fetch all queued tasks from /tasks and return as {task_id: task_dict}."""
    tasks_resp = client.get("/tasks", headers=auth_headers())
    assert tasks_resp.status_code == 200
    return {t["task_id"]: t for t in tasks_resp.json()}


# ---------------------------------------------------------------------------
# MobReviewWorkflowSubmit — model validation
# ---------------------------------------------------------------------------


class TestMobReviewSchema:
    def test_empty_code_rejected(self):
        """Empty code should raise ValueError."""
        with pytest.raises(Exception):
            MobReviewWorkflowSubmit(code="", aspects=_TWO_ASPECTS)

    def test_whitespace_code_rejected(self):
        """Whitespace-only code should raise ValueError."""
        with pytest.raises(Exception):
            MobReviewWorkflowSubmit(code="   ", aspects=_TWO_ASPECTS)

    def test_one_aspect_rejected(self):
        """Less than 2 aspects should raise ValueError."""
        with pytest.raises(Exception):
            MobReviewWorkflowSubmit(code=_SAMPLE_CODE, aspects=["security"])

    def test_nine_aspects_rejected(self):
        """More than 8 aspects should raise ValueError."""
        with pytest.raises(Exception):
            MobReviewWorkflowSubmit(
                code=_SAMPLE_CODE,
                aspects=[f"aspect_{i}" for i in range(9)],
            )

    def test_blank_aspect_name_rejected(self):
        """Blank aspect names should raise ValueError."""
        with pytest.raises(Exception):
            MobReviewWorkflowSubmit(code=_SAMPLE_CODE, aspects=["security", ""])

    def test_valid_minimal(self):
        """Minimal valid request should construct with defaults."""
        obj = MobReviewWorkflowSubmit(code=_SAMPLE_CODE, aspects=_TWO_ASPECTS)
        assert obj.code == _SAMPLE_CODE
        assert obj.aspects == _TWO_ASPECTS
        assert obj.language == "Python"
        assert obj.reviewer_tags == []
        assert obj.synthesizer_tags == []
        assert obj.reply_to is None

    def test_valid_with_custom_language(self):
        """Custom language should be accepted."""
        obj = MobReviewWorkflowSubmit(
            code="const f = () => {}", aspects=_TWO_ASPECTS, language="TypeScript"
        )
        assert obj.language == "TypeScript"

    def test_valid_with_tags(self):
        """reviewer_tags and synthesizer_tags should be accepted."""
        obj = MobReviewWorkflowSubmit(
            code=_SAMPLE_CODE,
            aspects=_TWO_ASPECTS,
            reviewer_tags=["reviewer-role"],
            synthesizer_tags=["synthesizer-role"],
        )
        assert obj.reviewer_tags == ["reviewer-role"]
        assert obj.synthesizer_tags == ["synthesizer-role"]

    def test_valid_with_reply_to(self):
        """reply_to field should be accepted."""
        obj = MobReviewWorkflowSubmit(
            code=_SAMPLE_CODE,
            aspects=_TWO_ASPECTS,
            reply_to="director-1",
        )
        assert obj.reply_to == "director-1"

    def test_eight_aspects_accepted(self):
        """Exactly 8 aspects should be accepted (boundary)."""
        obj = MobReviewWorkflowSubmit(
            code=_SAMPLE_CODE,
            aspects=[f"aspect_{i}" for i in range(8)],
        )
        assert len(obj.aspects) == 8

    def test_two_aspects_accepted(self):
        """Exactly 2 aspects should be accepted (boundary)."""
        obj = MobReviewWorkflowSubmit(
            code=_SAMPLE_CODE,
            aspects=["security", "performance"],
        )
        assert len(obj.aspects) == 2

    def test_default_aspects(self):
        """Omitting aspects should use the 4 standard dimensions."""
        obj = MobReviewWorkflowSubmit(code=_SAMPLE_CODE)
        assert len(obj.aspects) == 4
        assert "security" in obj.aspects
        assert "performance" in obj.aspects
        assert "maintainability" in obj.aspects
        assert "testing" in obj.aspects


# ---------------------------------------------------------------------------
# POST /workflows/mob-review — HTTP auth
# ---------------------------------------------------------------------------


class TestMobReviewAuth:
    def test_requires_auth(self, client):
        """Endpoint should return 401 without API key."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
        )
        assert resp.status_code == 401

    def test_wrong_api_key(self, client):
        """Endpoint should return 401 with wrong API key."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers={"X-API-Key": "wrong-key"},
        )
        assert resp.status_code == 401

    def test_empty_code_returns_422(self, client):
        """Empty code should return 422 Unprocessable Entity."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": "", "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        assert resp.status_code == 422

    def test_missing_code_returns_422(self, client):
        """Missing code field should return 422."""
        resp = client.post(
            "/workflows/mob-review",
            json={"aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        assert resp.status_code == 422

    def test_one_aspect_returns_422(self, client):
        """Single aspect should return 422."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": ["security"]},
            headers=auth_headers(),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /workflows/mob-review — response structure
# ---------------------------------------------------------------------------


class TestMobReviewResponseStructure:
    def test_returns_200(self, client):
        """Valid request with 2 aspects should return 200."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        assert resp.status_code == 200

    def test_returns_workflow_id(self, client):
        """Response must contain workflow_id as a UUID string."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        assert "workflow_id" in data
        assert uuid.UUID(data["workflow_id"])

    def test_returns_name(self, client):
        """Response must contain human-readable name."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS, "language": "Python"},
            headers=auth_headers(),
        )
        data = resp.json()
        assert "name" in data
        assert data["name"].startswith("mob-review/")

    def test_returns_task_ids(self, client):
        """Response must contain task_ids dict."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        assert "task_ids" in data
        assert isinstance(data["task_ids"], dict)

    def test_returns_scratchpad_prefix(self, client):
        """Response must contain scratchpad_prefix."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        assert "scratchpad_prefix" in data
        assert data["scratchpad_prefix"].startswith("mobreview_")

    def test_two_aspects_yield_three_tasks(self, client):
        """2 aspects should produce 2 reviewer tasks + 1 synthesizer = 3 tasks."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        task_ids = data["task_ids"]
        assert len(task_ids) == 3  # 2 reviewers + 1 synthesizer

    def test_four_aspects_yield_five_tasks(self, client):
        """4 aspects should produce 4 reviewer tasks + 1 synthesizer = 5 tasks."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _DEFAULT_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        task_ids = data["task_ids"]
        assert len(task_ids) == 5  # 4 reviewers + 1 synthesizer

    def test_task_ids_contain_reviewer_keys(self, client):
        """task_ids must contain reviewer_{aspect} keys for each aspect."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        task_ids = data["task_ids"]
        assert "reviewer_security" in task_ids
        assert "reviewer_performance" in task_ids

    def test_task_ids_contain_synthesizer_key(self, client):
        """task_ids must contain 'synthesizer' key."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        assert "synthesizer" in data["task_ids"]

    def test_all_task_ids_are_valid_uuids(self, client):
        """All task_ids values must be valid UUID strings."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        for key, tid in data["task_ids"].items():
            assert uuid.UUID(tid), f"task_ids[{key!r}] is not a valid UUID: {tid!r}"

    def test_workflow_name_contains_language(self, client):
        """Workflow name should include the language slug."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS, "language": "TypeScript React"},
            headers=auth_headers(),
        )
        data = resp.json()
        # Language slug replaces spaces with underscores
        assert "TypeScript" in data["name"]


# ---------------------------------------------------------------------------
# Task queue — dependency wiring
# ---------------------------------------------------------------------------


class TestMobReviewDependencies:
    def test_reviewer_tasks_have_no_depends_on(self, client_and_orch):
        """Reviewer tasks should depend on nothing (start in parallel)."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        reviewer_keys = [k for k in data["task_ids"] if k.startswith("reviewer_")]
        for key in reviewer_keys:
            tid = data["task_ids"][key]
            assert tid in tasks, f"task {key!r} not found in queue"
            assert tasks[tid].get("depends_on", []) == [], (
                f"reviewer task {key!r} should have no dependencies"
            )

    def test_synthesizer_depends_on_all_reviewers(self, client_and_orch):
        """Synthesizer must depend on all reviewer task IDs."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        synth_tid = data["task_ids"]["synthesizer"]
        assert synth_tid in tasks, "synthesizer task not found in queue"

        reviewer_tids = {
            data["task_ids"][k]
            for k in data["task_ids"]
            if k.startswith("reviewer_")
        }
        synth_deps = set(tasks[synth_tid].get("depends_on", []))
        assert reviewer_tids == synth_deps, (
            f"synthesizer depends_on {synth_deps!r} != reviewer task IDs {reviewer_tids!r}"
        )

    def test_tasks_registered_in_workflow(self, client_and_orch):
        """All task IDs should be registered in a workflow run."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        wf_id = data["workflow_id"]

        wf_resp = client.get(f"/workflows/{wf_id}", headers=auth_headers())
        assert wf_resp.status_code == 200
        wf = wf_resp.json()

        registered_tids = set(wf["task_ids"])
        expected_tids = set(data["task_ids"].values())
        assert expected_tids == registered_tids, (
            f"workflow task_ids {registered_tids!r} != expected {expected_tids!r}"
        )


# ---------------------------------------------------------------------------
# Prompt content sanity checks
# ---------------------------------------------------------------------------


class TestMobReviewPromptContent:
    def test_reviewer_prompts_contain_code(self, client_and_orch):
        """Reviewer prompts should include the supplied code."""
        client, orch = client_and_orch
        code_snippet = "def very_unique_func_xyzabc(x): return x * 42"
        resp = client.post(
            "/workflows/mob-review",
            json={"code": code_snippet, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        reviewer_keys = [k for k in data["task_ids"] if k.startswith("reviewer_")]
        for key in reviewer_keys:
            tid = data["task_ids"][key]
            prompt = tasks[tid]["prompt"]
            assert code_snippet in prompt, (
                f"reviewer {key!r} prompt does not contain the supplied code"
            )

    def test_reviewer_prompts_mention_aspect(self, client_and_orch):
        """Each reviewer's prompt should mention its assigned aspect."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": ["security", "performance"]},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        for aspect in ["security", "performance"]:
            tid = data["task_ids"][f"reviewer_{aspect}"]
            prompt = tasks[tid]["prompt"].lower()
            assert aspect in prompt, (
                f"reviewer_security prompt should mention 'security'"
            )

    def test_synthesizer_prompt_contains_code(self, client_and_orch):
        """Synthesizer prompt should include the code for reference."""
        client, orch = client_and_orch
        code_snippet = "def another_unique_func_zyxwvu(n): return n ** 2"
        resp = client.post(
            "/workflows/mob-review",
            json={"code": code_snippet, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        synth_tid = data["task_ids"]["synthesizer"]
        prompt = tasks[synth_tid]["prompt"]
        assert code_snippet in prompt, "synthesizer prompt does not contain the code"

    def test_synthesizer_prompt_mentions_mob_review_md(self, client_and_orch):
        """Synthesizer prompt should reference MOB_REVIEW.md output file."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        synth_tid = data["task_ids"]["synthesizer"]
        prompt = tasks[synth_tid]["prompt"]
        assert "MOB_REVIEW.md" in prompt, (
            "synthesizer prompt should reference MOB_REVIEW.md"
        )

    def test_reviewer_prompts_mention_scratchpad_key(self, client_and_orch):
        """Each reviewer prompt should contain its scratchpad key."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        prefix = data["scratchpad_prefix"]
        tasks = _get_tasks(client)

        for aspect in ["security", "performance"]:
            tid = data["task_ids"][f"reviewer_{aspect}"]
            prompt = tasks[tid]["prompt"]
            expected_key = f"{prefix}_review_{aspect}"
            assert expected_key in prompt, (
                f"reviewer_{aspect} prompt should contain scratchpad key {expected_key!r}"
            )

    def test_synthesizer_prompt_mentions_all_review_keys(self, client_and_orch):
        """Synthesizer prompt should reference all reviewer scratchpad keys."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        prefix = data["scratchpad_prefix"]
        tasks = _get_tasks(client)

        synth_tid = data["task_ids"]["synthesizer"]
        prompt = tasks[synth_tid]["prompt"]

        for aspect in ["security", "performance"]:
            review_key = f"{prefix}_review_{aspect}"
            assert review_key in prompt, (
                f"synthesizer prompt should reference scratchpad key {review_key!r}"
            )

    def test_synthesizer_prompt_mentions_synthesis_key(self, client_and_orch):
        """Synthesizer prompt should reference its own synthesis scratchpad key."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        data = resp.json()
        prefix = data["scratchpad_prefix"]
        tasks = _get_tasks(client)

        synth_tid = data["task_ids"]["synthesizer"]
        prompt = tasks[synth_tid]["prompt"]
        synthesis_key = f"{prefix}_synthesis"
        assert synthesis_key in prompt, (
            f"synthesizer prompt should reference its synthesis key {synthesis_key!r}"
        )


# ---------------------------------------------------------------------------
# Workflow listing / status
# ---------------------------------------------------------------------------


class TestMobReviewWorkflowStatus:
    def test_workflow_appears_in_list(self, client):
        """Submitted workflow should appear in GET /workflows."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        wf_id = resp.json()["workflow_id"]

        list_resp = client.get("/workflows", headers=auth_headers())
        assert list_resp.status_code == 200
        wf_ids = [wf["id"] for wf in list_resp.json()]
        assert wf_id in wf_ids

    def test_workflow_status_endpoint(self, client):
        """GET /workflows/{id} should return workflow details."""
        resp = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        wf_id = resp.json()["workflow_id"]

        status_resp = client.get(f"/workflows/{wf_id}", headers=auth_headers())
        assert status_resp.status_code == 200
        wf = status_resp.json()
        assert wf["id"] == wf_id
        assert wf["name"].startswith("mob-review/")

    def test_two_separate_workflows_have_different_prefixes(self, client):
        """Each workflow submission should get a unique scratchpad prefix."""
        r1 = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        r2 = client.post(
            "/workflows/mob-review",
            json={"code": _SAMPLE_CODE, "aspects": _TWO_ASPECTS},
            headers=auth_headers(),
        )
        assert r1.json()["scratchpad_prefix"] != r2.json()["scratchpad_prefix"]
