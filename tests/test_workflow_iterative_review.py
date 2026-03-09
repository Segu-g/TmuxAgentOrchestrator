"""Tests for POST /workflows/iterative-review — Iterative Review workflow.

The iterative-review workflow builds a 3-agent sequential pipeline:

    implementer → reviewer → revisor

- implementer: writes initial code, saves to scratchpad
- reviewer: reads implementation, critiques it (Self-Refine FEEDBACK step),
  saves review to scratchpad
- revisor: reads implementation + review, produces improved code (Self-Refine REFINE step)

Design references:
- Self-Refine (Madaan et al. NeurIPS 2023, arXiv:2303.17651): FEEDBACK→REFINE loop.
- MAR: Multi-Agent Reflexion (arXiv:2512.20845, 2025): cross-agent feedback.
- RevAgent (arXiv:2511.00517, 2025): multi-stage code review pipeline.
- DESIGN.md §10.53 (v1.1.21)
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app
from tmux_orchestrator.web.schemas import IterativeReviewWorkflowSubmit
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

_SAMPLE_TASK = "Write a function that validates an email address"
_SAMPLE_TASK_SHORT = "Email validator"


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
# IterativeReviewWorkflowSubmit — model validation
# ---------------------------------------------------------------------------


class TestIterativeReviewSchema:
    def test_empty_task_rejected(self):
        """Empty task should raise ValueError."""
        with pytest.raises(Exception):
            IterativeReviewWorkflowSubmit(task="")

    def test_whitespace_task_rejected(self):
        """Whitespace-only task should raise ValueError."""
        with pytest.raises(Exception):
            IterativeReviewWorkflowSubmit(task="   ")

    def test_valid_minimal(self):
        """Minimal valid request should construct with defaults."""
        obj = IterativeReviewWorkflowSubmit(task=_SAMPLE_TASK)
        assert obj.task == _SAMPLE_TASK
        assert obj.language == "Python"
        assert obj.implementer_tags == []
        assert obj.reviewer_tags == []
        assert obj.revisor_tags == []
        assert obj.reply_to is None

    def test_custom_language(self):
        """language field should be settable."""
        obj = IterativeReviewWorkflowSubmit(task=_SAMPLE_TASK, language="TypeScript")
        assert obj.language == "TypeScript"

    def test_custom_tags(self):
        """Role-specific tags should be accepted."""
        obj = IterativeReviewWorkflowSubmit(
            task=_SAMPLE_TASK,
            implementer_tags=["impl-role"],
            reviewer_tags=["rev-role"],
            revisor_tags=["revisor-role"],
        )
        assert obj.implementer_tags == ["impl-role"]
        assert obj.reviewer_tags == ["rev-role"]
        assert obj.revisor_tags == ["revisor-role"]

    def test_reply_to_accepted(self):
        """reply_to should be accepted."""
        obj = IterativeReviewWorkflowSubmit(task=_SAMPLE_TASK, reply_to="director-1")
        assert obj.reply_to == "director-1"


# ---------------------------------------------------------------------------
# HTTP endpoint — response structure
# ---------------------------------------------------------------------------


class TestIterativeReviewEndpoint:
    def test_requires_auth(self, client):
        """Endpoint should reject requests without API key."""
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
        )
        assert resp.status_code == 401

    def test_returns_200_for_valid_request(self, client):
        """Valid request should return HTTP 200."""
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        assert resp.status_code == 200

    def test_returns_workflow_id(self, client):
        """Response must contain workflow_id."""
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        assert "workflow_id" in data
        assert uuid.UUID(data["workflow_id"])

    def test_returns_name(self, client):
        """Response must contain name starting with iterative-review/."""
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        assert "name" in data
        assert data["name"].startswith("iterative-review/")

    def test_returns_task_ids(self, client):
        """Response must contain task_ids dict."""
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        assert "task_ids" in data
        assert isinstance(data["task_ids"], dict)

    def test_returns_scratchpad_prefix(self, client):
        """Response must contain scratchpad_prefix starting with iterrev_."""
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        assert "scratchpad_prefix" in data
        assert data["scratchpad_prefix"].startswith("iterrev_")

    def test_task_ids_contain_three_keys(self, client):
        """task_ids should contain exactly 3 entries: implementer, reviewer, revisor."""
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        task_ids = data["task_ids"]
        assert len(task_ids) == 3
        assert "implementer" in task_ids
        assert "reviewer" in task_ids
        assert "revisor" in task_ids

    def test_all_task_ids_are_valid_uuids(self, client):
        """All task_ids values should be valid UUID strings."""
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        for key, tid in data["task_ids"].items():
            assert uuid.UUID(tid), f"task_ids[{key!r}] is not a valid UUID: {tid!r}"

    def test_workflow_name_contains_task_slug(self, client):
        """Workflow name should include the task slug."""
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": "validate email address"},
            headers=auth_headers(),
        )
        data = resp.json()
        assert "validate" in data["name"]

    def test_two_submissions_have_different_prefixes(self, client):
        """Each submission should get a unique scratchpad prefix."""
        r1 = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        r2 = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        assert r1.json()["scratchpad_prefix"] != r2.json()["scratchpad_prefix"]


# ---------------------------------------------------------------------------
# Task queue — dependency wiring
# ---------------------------------------------------------------------------


class TestIterativeReviewDependencies:
    def test_implementer_task_has_no_depends_on(self, client_and_orch):
        """Implementer task should have no dependencies (starts first)."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        impl_tid = data["task_ids"]["implementer"]
        assert impl_tid in tasks, "implementer task not found in queue"
        assert tasks[impl_tid].get("depends_on", []) == [], (
            "implementer task should have no dependencies"
        )

    def test_reviewer_depends_on_implementer(self, client_and_orch):
        """Reviewer task must depend on the implementer task."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        impl_tid = data["task_ids"]["implementer"]
        rev_tid = data["task_ids"]["reviewer"]

        assert rev_tid in tasks, "reviewer task not found in queue"
        rev_deps = tasks[rev_tid].get("depends_on", [])
        assert impl_tid in rev_deps, (
            f"reviewer should depend on implementer {impl_tid!r}, got {rev_deps!r}"
        )

    def test_revisor_depends_on_reviewer(self, client_and_orch):
        """Revisor task must depend on the reviewer task."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        rev_tid = data["task_ids"]["reviewer"]
        revisor_tid = data["task_ids"]["revisor"]

        assert revisor_tid in tasks, "revisor task not found in queue"
        revisor_deps = tasks[revisor_tid].get("depends_on", [])
        assert rev_tid in revisor_deps, (
            f"revisor should depend on reviewer {rev_tid!r}, got {revisor_deps!r}"
        )

    def test_dependency_chain_is_strictly_sequential(self, client_and_orch):
        """The pipeline must be strictly sequential: impl→rev→revisor."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        impl_tid = data["task_ids"]["implementer"]
        rev_tid = data["task_ids"]["reviewer"]
        revisor_tid = data["task_ids"]["revisor"]

        # implementer: no deps
        assert tasks[impl_tid].get("depends_on", []) == []
        # reviewer: only depends on implementer
        assert tasks[rev_tid].get("depends_on", []) == [impl_tid]
        # revisor: only depends on reviewer
        assert tasks[revisor_tid].get("depends_on", []) == [rev_tid]

    def test_all_tasks_registered_in_workflow(self, client_and_orch):
        """All 3 task IDs should be registered in the workflow run."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        wf_id = data["workflow_id"]

        wf_resp = client.get(f"/workflows/{wf_id}", headers=auth_headers())
        assert wf_resp.status_code == 200
        wf = wf_resp.json()

        registered_tids = set(wf["task_ids"])
        expected_tids = set(data["task_ids"].values())
        assert expected_tids == registered_tids


# ---------------------------------------------------------------------------
# Prompt content sanity checks
# ---------------------------------------------------------------------------


class TestIterativeReviewPromptContent:
    def test_implementer_prompt_contains_task(self, client_and_orch):
        """Implementer prompt should include the task description."""
        client, orch = client_and_orch
        unique_task = "write a function xyzabc123 that does stuff"
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": unique_task},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        impl_tid = data["task_ids"]["implementer"]
        prompt = tasks[impl_tid]["prompt"]
        assert unique_task in prompt, "implementer prompt should contain the task"

    def test_reviewer_prompt_contains_scratchpad_read(self, client_and_orch):
        """Reviewer prompt should reference the implementation scratchpad key."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        prefix = data["scratchpad_prefix"]
        tasks = _get_tasks(client)

        rev_tid = data["task_ids"]["reviewer"]
        prompt = tasks[rev_tid]["prompt"]
        impl_key = f"{prefix}_implementation"
        assert impl_key in prompt, (
            f"reviewer prompt should reference scratchpad key {impl_key!r}"
        )

    def test_revisor_prompt_contains_both_scratchpad_keys(self, client_and_orch):
        """Revisor prompt should reference both implementation and review scratchpad keys."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        prefix = data["scratchpad_prefix"]
        tasks = _get_tasks(client)

        revisor_tid = data["task_ids"]["revisor"]
        prompt = tasks[revisor_tid]["prompt"]
        impl_key = f"{prefix}_implementation"
        review_key = f"{prefix}_review"
        assert impl_key in prompt, (
            f"revisor prompt should reference {impl_key!r}"
        )
        assert review_key in prompt, (
            f"revisor prompt should reference {review_key!r}"
        )

    def test_implementer_prompt_mentions_implementation_file(self, client_and_orch):
        """Implementer prompt should reference implementation.py."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK, "language": "Python"},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        impl_tid = data["task_ids"]["implementer"]
        prompt = tasks[impl_tid]["prompt"]
        assert "implementation.python" in prompt or "implementation.py" in prompt, (
            "implementer prompt should reference implementation file"
        )

    def test_reviewer_prompt_mentions_review_md(self, client_and_orch):
        """Reviewer prompt should reference review.md output file."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        rev_tid = data["task_ids"]["reviewer"]
        prompt = tasks[rev_tid]["prompt"]
        assert "review.md" in prompt, "reviewer prompt should reference review.md"

    def test_revisor_prompt_mentions_revised_key(self, client_and_orch):
        """Revisor prompt should reference its revised scratchpad key."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        prefix = data["scratchpad_prefix"]
        tasks = _get_tasks(client)

        revisor_tid = data["task_ids"]["revisor"]
        prompt = tasks[revisor_tid]["prompt"]
        revised_key = f"{prefix}_revised"
        assert revised_key in prompt, (
            f"revisor prompt should reference {revised_key!r}"
        )

    def test_implementer_prompt_mentions_self_refine_pattern(self, client_and_orch):
        """Implementer prompt should mention that a reviewer will critique the code."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        impl_tid = data["task_ids"]["implementer"]
        prompt = tasks[impl_tid]["prompt"].lower()
        assert "reviewer" in prompt, "implementer prompt should mention reviewer"


# ---------------------------------------------------------------------------
# required_tags routing — auto-generation per role
# ---------------------------------------------------------------------------


class TestIterativeReviewRequiredTags:
    """Verify auto-generated required_tags for correct per-role agent routing."""

    def test_implementer_has_iterative_implementer_tag(self, client_and_orch):
        """Default: implementer task should get required_tags ['iterative_implementer']."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        impl_tid = data["task_ids"]["implementer"]
        impl_tags = tasks[impl_tid].get("required_tags", [])
        assert "iterative_implementer" in impl_tags, (
            f"implementer task should have 'iterative_implementer' tag, got {impl_tags!r}"
        )

    def test_reviewer_has_iterative_reviewer_tag(self, client_and_orch):
        """Default: reviewer task should get required_tags ['iterative_reviewer']."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        rev_tid = data["task_ids"]["reviewer"]
        rev_tags = tasks[rev_tid].get("required_tags", [])
        assert "iterative_reviewer" in rev_tags, (
            f"reviewer task should have 'iterative_reviewer' tag, got {rev_tags!r}"
        )

    def test_revisor_has_iterative_revisor_tag(self, client_and_orch):
        """Default: revisor task should get required_tags ['iterative_revisor']."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        revisor_tid = data["task_ids"]["revisor"]
        revisor_tags = tasks[revisor_tid].get("required_tags", [])
        assert "iterative_revisor" in revisor_tags, (
            f"revisor task should have 'iterative_revisor' tag, got {revisor_tags!r}"
        )

    def test_three_roles_get_distinct_tags(self, client_and_orch):
        """All three role tasks should have distinct required_tags."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        impl_tags = frozenset(tasks[data["task_ids"]["implementer"]].get("required_tags", []))
        rev_tags = frozenset(tasks[data["task_ids"]["reviewer"]].get("required_tags", []))
        revisor_tags = frozenset(tasks[data["task_ids"]["revisor"]].get("required_tags", []))

        all_tag_sets = {impl_tags, rev_tags, revisor_tags}
        assert len(all_tag_sets) == 3, (
            f"expected 3 distinct tag sets, got {all_tag_sets!r}"
        )

    def test_explicit_implementer_tags_override_default(self, client_and_orch):
        """Explicit implementer_tags should override the auto-generated default."""
        client, orch = client_and_orch
        custom_tags = ["my_implementer"]
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK, "implementer_tags": custom_tags},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        impl_tid = data["task_ids"]["implementer"]
        impl_tags = tasks[impl_tid].get("required_tags", [])
        assert impl_tags == custom_tags, (
            f"implementer should have custom tags {custom_tags!r}, got {impl_tags!r}"
        )

    def test_explicit_reviewer_tags_override_default(self, client_and_orch):
        """Explicit reviewer_tags should override the auto-generated default."""
        client, orch = client_and_orch
        custom_tags = ["custom_reviewer"]
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK, "reviewer_tags": custom_tags},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        rev_tid = data["task_ids"]["reviewer"]
        rev_tags = tasks[rev_tid].get("required_tags", [])
        assert rev_tags == custom_tags, (
            f"reviewer should have custom tags {custom_tags!r}, got {rev_tags!r}"
        )

    def test_explicit_revisor_tags_override_default(self, client_and_orch):
        """Explicit revisor_tags should override the auto-generated default."""
        client, orch = client_and_orch
        custom_tags = ["custom_revisor"]
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK, "revisor_tags": custom_tags},
            headers=auth_headers(),
        )
        data = resp.json()
        tasks = _get_tasks(client)

        revisor_tid = data["task_ids"]["revisor"]
        revisor_tags = tasks[revisor_tid].get("required_tags", [])
        assert revisor_tags == custom_tags, (
            f"revisor should have custom tags {custom_tags!r}, got {revisor_tags!r}"
        )


# ---------------------------------------------------------------------------
# Workflow listing / status
# ---------------------------------------------------------------------------


class TestIterativeReviewWorkflowStatus:
    def test_workflow_appears_in_list(self, client):
        """Submitted workflow should appear in GET /workflows."""
        resp = client.post(
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
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
            "/workflows/iterative-review",
            json={"task": _SAMPLE_TASK},
            headers=auth_headers(),
        )
        wf_id = resp.json()["workflow_id"]

        status_resp = client.get(f"/workflows/{wf_id}", headers=auth_headers())
        assert status_resp.status_code == 200
        wf = status_resp.json()
        assert wf["id"] == wf_id
        assert wf["name"].startswith("iterative-review/")
