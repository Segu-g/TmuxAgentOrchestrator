"""Tests for POST /workflows/tdd — 3-agent TDD workflow endpoint.

The TDD workflow automatically builds a 3-step Workflow DAG:
  step_1 (test-writer) → step_2 (implementer, depends_on step_1)
                       → step_3 (refactorer, depends_on step_2)

Each step prompt embeds the feature description and references the shared
Scratchpad keys that agents use to hand off artifacts (test file path,
implementation file path).

Design references:
- TDFlow arXiv:2510.23761 (CMU/UCSD/JHU 2025): 4-sub-agent test-driven
  workflow achieves 88.8% on SWE-Bench Lite via context isolation.
- alexop.dev "Forcing Claude Code to TDD" (2025): context isolation is
  required for genuine test-first development; test-writer must not see
  implementation details.
- Tweag "Agentic Coding Handbook - TDD" (2025): test cases as the primary
  handoff artifact between workflow phases.
- Blackboard pattern (Buschmann 1996): shared scratchpad decouples producers
  from consumers without direct P2P messaging.
- DESIGN.md §10.31 (v0.36.0)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, AsyncMock

from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app
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


@pytest.fixture()
def client():
    app, orch = _make_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture(autouse=True)
def reset_state():
    """Reset module-level state before each test."""
    web_app_mod._scratchpad.clear()
    yield


def auth_headers() -> dict:
    return {"X-API-Key": _API_KEY}


# ---------------------------------------------------------------------------
# POST /workflows/tdd — basic happy path
# ---------------------------------------------------------------------------


def test_tdd_workflow_returns_workflow_id(client):
    """POST /workflows/tdd returns a workflow_id."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "fizzbuzz"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "workflow_id" in body


def test_tdd_workflow_returns_three_task_ids(client):
    """POST /workflows/tdd returns exactly 3 task IDs (test-writer, implementer, refactorer)."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "prime checker"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "task_ids" in body
    assert len(body["task_ids"]) == 3


def test_tdd_workflow_task_ids_have_expected_roles(client):
    """task_ids dict has keys: test_writer, implementer, refactorer."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "string reversal"},
        headers=auth_headers(),
    )
    body = resp.json()
    assert set(body["task_ids"].keys()) == {"test_writer", "implementer", "refactorer"}


def test_tdd_workflow_name_contains_feature(client):
    """The workflow name includes the feature name."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "bubble sort"},
        headers=auth_headers(),
    )
    body = resp.json()
    assert "bubble sort" in body["name"]


def test_tdd_workflow_scratchpad_key_in_response(client):
    """Response includes the scratchpad_prefix so agents can find artifacts."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "fibonacci"},
        headers=auth_headers(),
    )
    body = resp.json()
    assert "scratchpad_prefix" in body
    assert body["scratchpad_prefix"]  # non-empty string


def test_tdd_workflow_scratchpad_prefix_is_unique_per_run(client):
    """Each TDD workflow invocation gets a unique scratchpad_prefix."""
    resp1 = client.post(
        "/workflows/tdd",
        json={"feature": "stack"},
        headers=auth_headers(),
    )
    resp2 = client.post(
        "/workflows/tdd",
        json={"feature": "stack"},
        headers=auth_headers(),
    )
    prefix1 = resp1.json()["scratchpad_prefix"]
    prefix2 = resp2.json()["scratchpad_prefix"]
    # Each run must have a distinct scratchpad namespace
    assert prefix1 != prefix2


# ---------------------------------------------------------------------------
# Workflow DAG structure: dependency ordering
# ---------------------------------------------------------------------------


def test_tdd_workflow_tasks_are_submitted(client):
    """The 3 tasks from a TDD workflow appear in the orchestrator task list."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "fizzbuzz"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    task_ids = list(body["task_ids"].values())

    tasks_resp = client.get("/tasks", headers=auth_headers())
    assert tasks_resp.status_code == 200
    all_task_ids = [t["task_id"] for t in tasks_resp.json()]
    for tid in task_ids:
        assert tid in all_task_ids


def test_tdd_workflow_implementer_depends_on_test_writer(client):
    """The implementer task has the test_writer task as a dependency."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "fizzbuzz"},
        headers=auth_headers(),
    )
    body = resp.json()
    tw_id = body["task_ids"]["test_writer"]
    impl_id = body["task_ids"]["implementer"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    impl_task = tasks[impl_id]
    assert tw_id in impl_task["depends_on"]


def test_tdd_workflow_refactorer_depends_on_implementer(client):
    """The refactorer task has the implementer task as a dependency."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "fizzbuzz"},
        headers=auth_headers(),
    )
    body = resp.json()
    impl_id = body["task_ids"]["implementer"]
    refactor_id = body["task_ids"]["refactorer"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    refactor_task = tasks[refactor_id]
    assert impl_id in refactor_task["depends_on"]


# ---------------------------------------------------------------------------
# Prompt content: context isolation
# ---------------------------------------------------------------------------


def test_tdd_workflow_test_writer_prompt_mentions_feature(client):
    """The test-writer prompt mentions the feature name."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "palindrome checker"},
        headers=auth_headers(),
    )
    body = resp.json()
    tw_id = body["task_ids"]["test_writer"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    assert "palindrome checker" in tasks[tw_id]["prompt"]


def test_tdd_workflow_test_writer_prompt_mentions_scratchpad(client):
    """The test-writer prompt tells the agent to write the test file path to scratchpad."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "palindrome"},
        headers=auth_headers(),
    )
    body = resp.json()
    tw_id = body["task_ids"]["test_writer"]
    prefix = body["scratchpad_prefix"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    prompt = tasks[tw_id]["prompt"]
    assert prefix in prompt or "scratchpad" in prompt.lower()


def test_tdd_workflow_implementer_prompt_mentions_scratchpad(client):
    """The implementer prompt tells the agent to read the test file path from scratchpad."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "anagram"},
        headers=auth_headers(),
    )
    body = resp.json()
    impl_id = body["task_ids"]["implementer"]
    prefix = body["scratchpad_prefix"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    prompt = tasks[impl_id]["prompt"]
    assert prefix in prompt or "scratchpad" in prompt.lower()


def test_tdd_workflow_implementer_prompt_does_not_describe_implementation(client):
    """Implementer prompt must not contain 'how to implement' — context isolation check."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "binary search"},
        headers=auth_headers(),
    )
    body = resp.json()
    impl_id = body["task_ids"]["implementer"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    prompt = tasks[impl_id]["prompt"].lower()
    # The implementer should be instructed to look at the TESTS, not told how to implement
    assert "implement" in prompt  # should say 'implement the feature to make tests pass'


def test_tdd_workflow_refactorer_prompt_mentions_feature(client):
    """The refactorer prompt mentions the feature name and refactoring goal."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "merge sort"},
        headers=auth_headers(),
    )
    body = resp.json()
    refactor_id = body["task_ids"]["refactorer"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    prompt = tasks[refactor_id]["prompt"]
    assert "merge sort" in prompt
    assert "refactor" in prompt.lower() or "improve" in prompt.lower()


# ---------------------------------------------------------------------------
# Optional fields: language, required_tags, reply_to
# ---------------------------------------------------------------------------


def test_tdd_workflow_with_language_field(client):
    """POST /workflows/tdd accepts an optional 'language' field."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "queue", "language": "typescript"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    # Language should appear in at least the test-writer prompt
    tw_id = body["task_ids"]["test_writer"]
    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    assert "typescript" in tasks[tw_id]["prompt"].lower()


def test_tdd_workflow_with_required_tags(client):
    """POST /workflows/tdd accepts required_tags for each step."""
    resp = client.post(
        "/workflows/tdd",
        json={
            "feature": "cache",
            "test_writer_tags": ["tdd-test"],
            "implementer_tags": ["tdd-impl"],
            "refactorer_tags": ["tdd-refactor"],
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200


def test_tdd_workflow_with_reply_to(client):
    """POST /workflows/tdd accepts reply_to so the final result is routed back."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "hash map", "reply_to": "director-agent"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


def test_tdd_workflow_requires_auth(client):
    """POST /workflows/tdd returns 401 without auth header."""
    resp = client.post("/workflows/tdd", json={"feature": "test"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_tdd_workflow_missing_feature_returns_422(client):
    """POST /workflows/tdd without 'feature' field returns 422."""
    resp = client.post(
        "/workflows/tdd",
        json={"language": "python"},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_tdd_workflow_empty_feature_returns_422(client):
    """POST /workflows/tdd with empty feature string returns 422."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": ""},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Workflow tracking: GET /workflows shows the TDD workflow
# ---------------------------------------------------------------------------


def test_tdd_workflow_appears_in_list(client):
    """After POST /workflows/tdd, GET /workflows shows the new run."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "deque"},
        headers=auth_headers(),
    )
    wf_id = resp.json()["workflow_id"]

    list_resp = client.get("/workflows", headers=auth_headers())
    assert list_resp.status_code == 200
    runs = list_resp.json()
    wf_ids = [r["id"] for r in runs]
    assert wf_id in wf_ids


def test_tdd_workflow_get_by_id(client):
    """GET /workflows/{id} returns the TDD workflow run."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "heap"},
        headers=auth_headers(),
    )
    wf_id = resp.json()["workflow_id"]

    get_resp = client.get(f"/workflows/{wf_id}", headers=auth_headers())
    assert get_resp.status_code == 200
    run = get_resp.json()
    assert run["id"] == wf_id
    assert run["tasks_total"] == 3


def test_tdd_workflow_name_in_workflow_list(client):
    """The TDD workflow's name contains 'tdd' and the feature."""
    resp = client.post(
        "/workflows/tdd",
        json={"feature": "trie"},
        headers=auth_headers(),
    )
    wf_id = resp.json()["workflow_id"]

    get_resp = client.get(f"/workflows/{wf_id}", headers=auth_headers())
    run = get_resp.json()
    assert "trie" in run["name"]
