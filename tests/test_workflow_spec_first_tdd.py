"""Tests for POST /workflows/spec-first-tdd — Spec-First TDD workflow.

The spec-first-tdd workflow builds a 3-agent sequential pipeline:

    spec-writer → implementer → tester

- spec-writer: reads requirements, produces formal SPEC.md, stores in scratchpad
- implementer: reads SPEC.md, implements the feature, stores in scratchpad
- tester: reads SPEC.md + implementation, writes pytest suite, runs tests

Design references:
- Vasilopoulos arXiv:2602.20478 "Codified Context" (2026): formal spec docs.
- Beck "TDD by Example" (2003): Red→Green→Refactor TDD cycle.
- AgentCoder: programmer → test_designer → test_executor pipeline.
- DESIGN.md §10.54 (v1.1.22)
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
from tmux_orchestrator.web.schemas import SpecFirstTddWorkflowSubmit
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

_SAMPLE_TOPIC = "Roman numeral converter"
_SAMPLE_REQUIREMENTS = (
    "Write a function that converts an integer (1-3999) to a Roman numeral string. "
    "Must handle all valid inputs and raise ValueError for invalid inputs."
)


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
# SpecFirstTddWorkflowSubmit — model validation
# ---------------------------------------------------------------------------


class TestSpecFirstTddSchema:
    def test_empty_topic_rejected(self):
        """Empty topic should raise ValueError."""
        with pytest.raises(Exception):
            SpecFirstTddWorkflowSubmit(topic="", requirements=_SAMPLE_REQUIREMENTS)

    def test_whitespace_topic_rejected(self):
        """Whitespace-only topic should raise ValueError."""
        with pytest.raises(Exception):
            SpecFirstTddWorkflowSubmit(topic="   ", requirements=_SAMPLE_REQUIREMENTS)

    def test_empty_requirements_rejected(self):
        """Empty requirements should raise ValueError."""
        with pytest.raises(Exception):
            SpecFirstTddWorkflowSubmit(topic=_SAMPLE_TOPIC, requirements="")

    def test_whitespace_requirements_rejected(self):
        """Whitespace-only requirements should raise ValueError."""
        with pytest.raises(Exception):
            SpecFirstTddWorkflowSubmit(topic=_SAMPLE_TOPIC, requirements="   ")

    def test_valid_minimal(self):
        """Minimal valid request should construct with defaults."""
        obj = SpecFirstTddWorkflowSubmit(
            topic=_SAMPLE_TOPIC, requirements=_SAMPLE_REQUIREMENTS
        )
        assert obj.topic == _SAMPLE_TOPIC
        assert obj.requirements == _SAMPLE_REQUIREMENTS
        assert obj.language == "Python"
        assert obj.spec_tags == []
        assert obj.impl_tags == []
        assert obj.tester_tags == []
        assert obj.reply_to is None

    def test_custom_language(self):
        """language field should be settable."""
        obj = SpecFirstTddWorkflowSubmit(
            topic=_SAMPLE_TOPIC, requirements=_SAMPLE_REQUIREMENTS, language="TypeScript"
        )
        assert obj.language == "TypeScript"

    def test_custom_tags(self):
        """Role-specific tags should be accepted."""
        obj = SpecFirstTddWorkflowSubmit(
            topic=_SAMPLE_TOPIC,
            requirements=_SAMPLE_REQUIREMENTS,
            spec_tags=["spec-role"],
            impl_tags=["impl-role"],
            tester_tags=["tester-role"],
        )
        assert obj.spec_tags == ["spec-role"]
        assert obj.impl_tags == ["impl-role"]
        assert obj.tester_tags == ["tester-role"]

    def test_reply_to_accepted(self):
        """reply_to should be accepted and preserved."""
        obj = SpecFirstTddWorkflowSubmit(
            topic=_SAMPLE_TOPIC,
            requirements=_SAMPLE_REQUIREMENTS,
            reply_to="director-1",
        )
        assert obj.reply_to == "director-1"

    def test_reply_to_default_is_none(self):
        """reply_to should default to None."""
        obj = SpecFirstTddWorkflowSubmit(
            topic=_SAMPLE_TOPIC, requirements=_SAMPLE_REQUIREMENTS
        )
        assert obj.reply_to is None


# ---------------------------------------------------------------------------
# HTTP endpoint — response structure
# ---------------------------------------------------------------------------


class TestSpecFirstTddEndpoint:
    def test_requires_auth(self, client):
        """Endpoint should reject requests without API key."""
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
        )
        assert resp.status_code == 401

    def test_returns_200_for_valid_request(self, client):
        """Valid request should return HTTP 200."""
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        assert resp.status_code == 200

    def test_returns_workflow_id(self, client):
        """Response must contain a valid UUID workflow_id."""
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        data = resp.json()
        assert "workflow_id" in data
        assert uuid.UUID(data["workflow_id"])  # valid UUID

    def test_returns_name_with_topic_prefix(self, client):
        """Response name must start with 'spec-first-tdd/'."""
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        data = resp.json()
        assert "name" in data
        assert data["name"].startswith("spec-first-tdd/")

    def test_returns_task_ids_with_all_three_roles(self, client):
        """Response must include task IDs for spec_writer, implementer, tester."""
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        data = resp.json()
        assert "task_ids" in data
        task_ids = data["task_ids"]
        assert "spec_writer" in task_ids
        assert "implementer" in task_ids
        assert "tester" in task_ids

    def test_task_ids_are_unique(self, client):
        """Each role must get a distinct task ID."""
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        data = resp.json()
        ids = list(data["task_ids"].values())
        assert len(ids) == len(set(ids)), "All task IDs must be unique"

    def test_returns_scratchpad_prefix(self, client):
        """Response must include a scratchpad_prefix."""
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        data = resp.json()
        assert "scratchpad_prefix" in data
        assert data["scratchpad_prefix"].startswith("sftdd_")

    def test_scratchpad_prefix_is_unique_across_calls(self, client):
        """Two workflow submissions must yield different scratchpad prefixes."""
        resp1 = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        resp2 = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        assert resp1.json()["scratchpad_prefix"] != resp2.json()["scratchpad_prefix"]

    def test_empty_topic_returns_422(self, client):
        """Empty topic should return 422 Unprocessable Entity."""
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": "", "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        assert resp.status_code == 422

    def test_empty_requirements_returns_422(self, client):
        """Empty requirements should return 422 Unprocessable Entity."""
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": ""},
            headers=auth_headers(),
        )
        assert resp.status_code == 422

    def test_missing_topic_returns_422(self, client):
        """Missing topic should return 422 Unprocessable Entity."""
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        assert resp.status_code == 422

    def test_missing_requirements_returns_422(self, client):
        """Missing requirements should return 422 Unprocessable Entity."""
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC},
            headers=auth_headers(),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Task queue — dependency chain verification
# ---------------------------------------------------------------------------


class TestSpecFirstTddTaskChain:
    def test_creates_three_tasks(self, client_and_orch):
        """Submitting the workflow should create exactly 3 tasks."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        assert resp.status_code == 200
        tasks = _get_tasks(client)
        data = resp.json()
        for role_key in ("spec_writer", "implementer", "tester"):
            assert data["task_ids"][role_key] in tasks

    def test_spec_writer_has_no_dependencies(self, client_and_orch):
        """spec-writer task must have no depends_on (fires immediately)."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        spec_writer_id = resp.json()["task_ids"]["spec_writer"]
        task = tasks[spec_writer_id]
        assert task.get("depends_on", []) == []

    def test_implementer_depends_on_spec_writer(self, client_and_orch):
        """implementer task must depend on spec-writer task."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        data = resp.json()
        spec_writer_id = data["task_ids"]["spec_writer"]
        impl_id = data["task_ids"]["implementer"]
        task = tasks[impl_id]
        assert spec_writer_id in task.get("depends_on", [])

    def test_tester_depends_on_implementer(self, client_and_orch):
        """tester task must depend on implementer task."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        data = resp.json()
        impl_id = data["task_ids"]["implementer"]
        tester_id = data["task_ids"]["tester"]
        task = tasks[tester_id]
        assert impl_id in task.get("depends_on", [])

    def test_tester_does_not_depend_on_spec_writer_directly(self, client_and_orch):
        """tester depends only on implementer, NOT directly on spec-writer."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        data = resp.json()
        spec_writer_id = data["task_ids"]["spec_writer"]
        tester_id = data["task_ids"]["tester"]
        task = tasks[tester_id]
        assert spec_writer_id not in task.get("depends_on", [])


# ---------------------------------------------------------------------------
# required_tags auto-generation
# ---------------------------------------------------------------------------


class TestSpecFirstTddRequiredTags:
    def test_default_spec_tags_assigned(self, client_and_orch):
        """spec-writer task must get auto-generated sftdd_spec tag by default."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        spec_id = resp.json()["task_ids"]["spec_writer"]
        task = tasks[spec_id]
        assert "sftdd_spec" in (task.get("required_tags") or [])

    def test_default_impl_tags_assigned(self, client_and_orch):
        """implementer task must get auto-generated sftdd_impl tag by default."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        impl_id = resp.json()["task_ids"]["implementer"]
        task = tasks[impl_id]
        assert "sftdd_impl" in (task.get("required_tags") or [])

    def test_default_tester_tags_assigned(self, client_and_orch):
        """tester task must get auto-generated sftdd_tester tag by default."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        tester_id = resp.json()["task_ids"]["tester"]
        task = tasks[tester_id]
        assert "sftdd_tester" in (task.get("required_tags") or [])

    def test_custom_spec_tags_override_default(self, client_and_orch):
        """Custom spec_tags should override the auto-generated default."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={
                "topic": _SAMPLE_TOPIC,
                "requirements": _SAMPLE_REQUIREMENTS,
                "spec_tags": ["custom-spec"],
            },
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        spec_id = resp.json()["task_ids"]["spec_writer"]
        task = tasks[spec_id]
        tags = task.get("required_tags") or []
        assert "custom-spec" in tags
        assert "sftdd_spec" not in tags

    def test_custom_impl_tags_override_default(self, client_and_orch):
        """Custom impl_tags should override the auto-generated default."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={
                "topic": _SAMPLE_TOPIC,
                "requirements": _SAMPLE_REQUIREMENTS,
                "impl_tags": ["custom-impl"],
            },
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        impl_id = resp.json()["task_ids"]["implementer"]
        task = tasks[impl_id]
        tags = task.get("required_tags") or []
        assert "custom-impl" in tags
        assert "sftdd_impl" not in tags

    def test_custom_tester_tags_override_default(self, client_and_orch):
        """Custom tester_tags should override the auto-generated default."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={
                "topic": _SAMPLE_TOPIC,
                "requirements": _SAMPLE_REQUIREMENTS,
                "tester_tags": ["custom-tester"],
            },
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        tester_id = resp.json()["task_ids"]["tester"]
        task = tasks[tester_id]
        tags = task.get("required_tags") or []
        assert "custom-tester" in tags
        assert "sftdd_tester" not in tags

    def test_roles_get_different_tags(self, client_and_orch):
        """spec-writer, implementer, and tester must each get different default tags."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        data = resp.json()
        spec_tags = set(tasks[data["task_ids"]["spec_writer"]].get("required_tags") or [])
        impl_tags = set(tasks[data["task_ids"]["implementer"]].get("required_tags") or [])
        tester_tags = set(tasks[data["task_ids"]["tester"]].get("required_tags") or [])
        # Each role gets a distinct tag set
        assert spec_tags != impl_tags
        assert spec_tags != tester_tags
        assert impl_tags != tester_tags


# ---------------------------------------------------------------------------
# Prompt content checks
# ---------------------------------------------------------------------------


class TestSpecFirstTddPromptContent:
    def test_spec_writer_prompt_contains_topic(self, client_and_orch):
        """spec-writer prompt must reference the topic."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        spec_id = resp.json()["task_ids"]["spec_writer"]
        prompt = tasks[spec_id]["prompt"]
        assert _SAMPLE_TOPIC in prompt

    def test_spec_writer_prompt_contains_requirements(self, client_and_orch):
        """spec-writer prompt must include the requirements text."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        spec_id = resp.json()["task_ids"]["spec_writer"]
        prompt = tasks[spec_id]["prompt"]
        assert _SAMPLE_REQUIREMENTS in prompt

    def test_implementer_prompt_references_spec_key(self, client_and_orch):
        """implementer prompt must reference the spec scratchpad key."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        data = resp.json()
        prefix = data["scratchpad_prefix"]
        impl_id = data["task_ids"]["implementer"]
        prompt = tasks[impl_id]["prompt"]
        assert f"{prefix}_spec" in prompt

    def test_tester_prompt_references_spec_and_impl_keys(self, client_and_orch):
        """tester prompt must reference both spec and impl scratchpad keys."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        data = resp.json()
        prefix = data["scratchpad_prefix"]
        tester_id = data["task_ids"]["tester"]
        prompt = tasks[tester_id]["prompt"]
        assert f"{prefix}_spec" in prompt
        assert f"{prefix}_impl" in prompt

    def test_tester_prompt_references_test_result_key(self, client_and_orch):
        """tester prompt must reference the test_result scratchpad key."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        data = resp.json()
        prefix = data["scratchpad_prefix"]
        tester_id = data["task_ids"]["tester"]
        prompt = tasks[tester_id]["prompt"]
        assert f"{prefix}_test_result" in prompt

    def test_spec_writer_prompt_mentions_acceptance_criteria(self, client_and_orch):
        """spec-writer prompt must ask for acceptance criteria in SPEC.md."""
        client, orch = client_and_orch
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        tasks = _get_tasks(client)
        spec_id = resp.json()["task_ids"]["spec_writer"]
        prompt = tasks[spec_id]["prompt"]
        assert "Acceptance Criteria" in prompt or "acceptance criteria" in prompt.lower()


# ---------------------------------------------------------------------------
# Workflow registration
# ---------------------------------------------------------------------------


class TestSpecFirstTddWorkflowRegistration:
    def test_workflow_appears_in_list(self, client):
        """After submission, the workflow must appear in GET /workflows."""
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        wf_id = resp.json()["workflow_id"]
        list_resp = client.get("/workflows", headers=auth_headers())
        assert list_resp.status_code == 200
        # GET /workflows returns objects with key "id" (not "workflow_id")
        ids = [w["id"] for w in list_resp.json()]
        assert wf_id in ids

    def test_workflow_retrievable_by_id(self, client):
        """Workflow must be retrievable via GET /workflows/{id}."""
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        wf_id = resp.json()["workflow_id"]
        get_resp = client.get(f"/workflows/{wf_id}", headers=auth_headers())
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["id"] == wf_id
        assert data["name"].startswith("spec-first-tdd/")

    def test_workflow_deletable(self, client):
        """Workflow must be deletable via DELETE /workflows/{id}."""
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": _SAMPLE_TOPIC, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        wf_id = resp.json()["workflow_id"]
        del_resp = client.delete(f"/workflows/{wf_id}", headers=auth_headers())
        assert del_resp.status_code == 200

    def test_workflow_name_includes_topic_slug(self, client):
        """Workflow name must include a slug derived from the topic."""
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": "My Feature", "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        data = resp.json()
        assert "My_Feature" in data["name"] or "my_feature" in data["name"].lower()

    def test_topic_truncated_at_40_chars_in_name(self, client):
        """Long topics must be truncated at 40 chars in the workflow name."""
        long_topic = "A" * 60
        resp = client.post(
            "/workflows/spec-first-tdd",
            json={"topic": long_topic, "requirements": _SAMPLE_REQUIREMENTS},
            headers=auth_headers(),
        )
        data = resp.json()
        slug = data["name"].split("/", 1)[1]
        assert len(slug) <= 40
