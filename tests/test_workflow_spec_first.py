"""Tests for POST /workflows/spec-first — Spec-First development workflow.

The spec-first workflow builds a strictly sequential 2-agent DAG:

  spec-writer ──→ implementer

- spec-writer:  reads the requirements, produces a formal SPEC.md with
  preconditions, postconditions, invariants, type signatures, and acceptance
  criteria, stores it in scratchpad ``{prefix}_spec``.
- implementer:  reads SPEC.md from the scratchpad, implements the feature
  satisfying every acceptance criterion, writes tests, and stores an
  implementation summary in ``{prefix}_impl``.

Design references:
- Vasilopoulos arXiv:2602.20478 "Codified Context" (2026): formal spec docs.
- Hou et al. "Trustworthy AI Requires Formal Methods" (2025).
- SYSMOBENCH arXiv:2509.23130 (2025): LLM TLA+ spec generation.
- DESIGN.md §10.44 (v1.1.8)
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
from tmux_orchestrator.web.schemas import SpecFirstWorkflowSubmit
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
# SpecFirstWorkflowSubmit — model validation
# ---------------------------------------------------------------------------


def test_spec_first_submit_empty_topic_rejected():
    """Empty topic should raise ValueError."""
    with pytest.raises(Exception):
        SpecFirstWorkflowSubmit(topic="", requirements="some requirements")


def test_spec_first_submit_whitespace_topic_rejected():
    """Whitespace-only topic should raise ValueError."""
    with pytest.raises(Exception):
        SpecFirstWorkflowSubmit(topic="   ", requirements="some requirements")


def test_spec_first_submit_empty_requirements_rejected():
    """Empty requirements should raise ValueError."""
    with pytest.raises(Exception):
        SpecFirstWorkflowSubmit(topic="binary search", requirements="")


def test_spec_first_submit_whitespace_requirements_rejected():
    """Whitespace-only requirements should raise ValueError."""
    with pytest.raises(Exception):
        SpecFirstWorkflowSubmit(topic="binary search", requirements="   ")


def test_spec_first_submit_valid_minimal():
    """Minimal valid request should construct with defaults."""
    obj = SpecFirstWorkflowSubmit(
        topic="binary search",
        requirements="Write a function that finds an element in a sorted list.",
    )
    assert obj.topic == "binary search"
    assert obj.requirements == "Write a function that finds an element in a sorted list."
    assert obj.spec_tags == []
    assert obj.impl_tags == []
    assert obj.reply_to is None


def test_spec_first_submit_with_tags():
    """Tags should be accepted on both roles."""
    obj = SpecFirstWorkflowSubmit(
        topic="stack data structure",
        requirements="Implement a LIFO stack with push, pop, and peek.",
        spec_tags=["spec-role"],
        impl_tags=["impl-role"],
    )
    assert obj.spec_tags == ["spec-role"]
    assert obj.impl_tags == ["impl-role"]


def test_spec_first_submit_with_reply_to():
    """reply_to field should be accepted."""
    obj = SpecFirstWorkflowSubmit(
        topic="CSV parser",
        requirements="Parse CSV files with quoted fields.",
        reply_to="director-1",
    )
    assert obj.reply_to == "director-1"


def test_spec_first_submit_multiple_tags():
    """Multiple tags should be accepted on each role."""
    obj = SpecFirstWorkflowSubmit(
        topic="LRU cache",
        requirements="Implement an LRU cache with O(1) get and put.",
        spec_tags=["spec", "senior"],
        impl_tags=["impl", "python"],
    )
    assert len(obj.spec_tags) == 2
    assert len(obj.impl_tags) == 2


# ---------------------------------------------------------------------------
# POST /workflows/spec-first — HTTP auth
# ---------------------------------------------------------------------------


def test_spec_first_workflow_requires_auth(client):
    """Endpoint should return 401 without API key."""
    resp = client.post(
        "/workflows/spec-first",
        json={
            "topic": "binary search",
            "requirements": "Find element in sorted list.",
        },
    )
    assert resp.status_code == 401


def test_spec_first_workflow_wrong_api_key(client):
    """Endpoint should return 401 with wrong API key."""
    resp = client.post(
        "/workflows/spec-first",
        json={
            "topic": "binary search",
            "requirements": "Find element in sorted list.",
        },
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_spec_first_workflow_empty_topic_returns_422(client):
    """Empty topic should return 422 Unprocessable Entity."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "", "requirements": "Some requirements."},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_spec_first_workflow_empty_requirements_returns_422(client):
    """Empty requirements should return 422 Unprocessable Entity."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "binary search", "requirements": ""},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_spec_first_workflow_missing_topic_returns_422(client):
    """Missing topic field should return 422."""
    resp = client.post(
        "/workflows/spec-first",
        json={"requirements": "Some requirements."},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_spec_first_workflow_missing_requirements_returns_422(client):
    """Missing requirements field should return 422."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "binary search"},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_spec_first_workflow_returns_200(client):
    """Valid request should return 200."""
    resp = client.post(
        "/workflows/spec-first",
        json={
            "topic": "binary search",
            "requirements": "Find element in sorted list using binary search.",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /workflows/spec-first — response structure
# ---------------------------------------------------------------------------


def test_spec_first_workflow_response_fields(client):
    """Response must contain workflow_id, name, task_ids, scratchpad_prefix."""
    resp = client.post(
        "/workflows/spec-first",
        json={
            "topic": "stack data structure",
            "requirements": "LIFO stack with push/pop/peek.",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "workflow_id" in data
    assert "name" in data
    assert "task_ids" in data
    assert "scratchpad_prefix" in data


def test_spec_first_workflow_name_starts_with_spec_first(client):
    """Workflow name should start with 'spec-first/'."""
    resp = client.post(
        "/workflows/spec-first",
        json={
            "topic": "binary search",
            "requirements": "Find element in sorted list.",
        },
        headers=auth_headers(),
    )
    data = resp.json()
    assert data["name"].startswith("spec-first/")


def test_spec_first_workflow_name_contains_topic(client):
    """Workflow name should contain the topic."""
    topic = "binary search function"
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": topic, "requirements": "Find element in sorted list."},
        headers=auth_headers(),
    )
    data = resp.json()
    assert "binary search function" in data["name"]


def test_spec_first_workflow_task_count(client):
    """Workflow must create exactly 2 tasks: spec_writer and implementer."""
    resp = client.post(
        "/workflows/spec-first",
        json={
            "topic": "queue data structure",
            "requirements": "FIFO queue with enqueue/dequeue.",
        },
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]
    assert len(task_ids) == 2
    assert "spec_writer" in task_ids
    assert "implementer" in task_ids


def test_spec_first_workflow_task_ids_are_strings(client):
    """All task IDs should be non-empty strings."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "hash map", "requirements": "Key-value store with O(1) ops."},
        headers=auth_headers(),
    )
    data = resp.json()
    for role, tid in data["task_ids"].items():
        assert isinstance(tid, str), f"task_id for {role} is not a string"
        assert len(tid) > 0, f"task_id for {role} is empty"


def test_spec_first_workflow_task_ids_distinct(client):
    """Both task IDs must be distinct."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "linked list", "requirements": "Doubly-linked list operations."},
        headers=auth_headers(),
    )
    data = resp.json()
    ids = list(data["task_ids"].values())
    assert len(ids) == len(set(ids)), "task IDs are not distinct"


def test_spec_first_workflow_id_is_valid_uuid(client):
    """workflow_id should be a valid UUID."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "binary heap", "requirements": "Min-heap with push/pop."},
        headers=auth_headers(),
    )
    data = resp.json()
    # Should not raise
    uuid.UUID(data["workflow_id"])


# ---------------------------------------------------------------------------
# Scratchpad key naming
# ---------------------------------------------------------------------------


def test_spec_first_scratchpad_prefix_format(client):
    """Scratchpad prefix should match 'specfirst_XXXXXXXX' pattern."""
    resp = client.post(
        "/workflows/spec-first",
        json={
            "topic": "sorting algorithm",
            "requirements": "Sort a list of integers in ascending order.",
        },
        headers=auth_headers(),
    )
    data = resp.json()
    assert re.match(r"^specfirst_[0-9a-f]{8}$", data["scratchpad_prefix"]), (
        f"unexpected prefix: {data['scratchpad_prefix']}"
    )


def test_spec_first_scratchpad_prefix_unique_across_runs(client):
    """Two workflow submissions should produce distinct scratchpad prefixes."""
    resp1 = client.post(
        "/workflows/spec-first",
        json={"topic": "topic A", "requirements": "Req A"},
        headers=auth_headers(),
    )
    resp2 = client.post(
        "/workflows/spec-first",
        json={"topic": "topic B", "requirements": "Req B"},
        headers=auth_headers(),
    )
    assert resp1.json()["scratchpad_prefix"] != resp2.json()["scratchpad_prefix"]


# ---------------------------------------------------------------------------
# Task dependency chain
# ---------------------------------------------------------------------------


def test_spec_first_spec_writer_has_no_dependencies(client):
    """spec_writer task should have no depends_on."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "binary heap", "requirements": "Min-heap with push/pop."},
        headers=auth_headers(),
    )
    data = resp.json()
    spec_writer_id = data["task_ids"]["spec_writer"]

    tasks = _get_tasks(client)
    assert spec_writer_id in tasks, "spec_writer task not found in /tasks"
    assert tasks[spec_writer_id].get("depends_on", []) == [], (
        "spec_writer should have no dependencies"
    )


def test_spec_first_implementer_depends_on_spec_writer(client):
    """implementer task should depend on the spec_writer task."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "priority queue", "requirements": "Min-heap with priorities."},
        headers=auth_headers(),
    )
    data = resp.json()
    spec_writer_id = data["task_ids"]["spec_writer"]
    implementer_id = data["task_ids"]["implementer"]

    tasks = _get_tasks(client)
    assert implementer_id in tasks, "implementer task not found in /tasks"
    assert spec_writer_id in tasks[implementer_id].get("depends_on", []), (
        f"implementer.depends_on={tasks[implementer_id].get('depends_on')} "
        f"does not contain spec_writer_id={spec_writer_id}"
    )


# ---------------------------------------------------------------------------
# reply_to propagation
# ---------------------------------------------------------------------------


def test_spec_first_workflow_reply_to_passed_to_implementer():
    """reply_to should be forwarded to the implementer task only."""
    app, orch = _make_app()
    reply_tos: list = []
    original_submit = orch.submit_task

    async def capture_submit(*args, **kwargs):
        reply_tos.append(kwargs.get("reply_to"))
        return await original_submit(*args, **kwargs)

    orch.submit_task = capture_submit  # type: ignore[method-assign]

    with TestClient(app) as c:
        c.post(
            "/workflows/spec-first",
            json={
                "topic": "trie data structure",
                "requirements": "Trie with insert and search.",
                "reply_to": "director-1",
            },
            headers=auth_headers(),
        )

    # 2 tasks: spec_writer, implementer
    assert len(reply_tos) == 2, f"Expected 2 submit_task calls, got {len(reply_tos)}"
    # Only implementer (last task) should have reply_to
    assert reply_tos[1] == "director-1", (
        f"reply_to not propagated to implementer: {reply_tos}"
    )
    assert reply_tos[0] is None, f"spec_writer should not have reply_to: {reply_tos[0]}"


def test_spec_first_workflow_reply_to_none_by_default(client):
    """reply_to=None should result in no reply_to on any task."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "set", "requirements": "Set with add/remove/contains."},
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]

    tasks = _get_tasks(client)
    for role, tid in task_ids.items():
        rt = tasks[tid].get("reply_to")
        assert rt is None, f"{role} task has unexpected reply_to={rt!r}"


# ---------------------------------------------------------------------------
# Tag routing
# ---------------------------------------------------------------------------


def test_spec_first_workflow_tags_forwarded(client):
    """spec_tags and impl_tags should appear in task required_tags."""
    resp = client.post(
        "/workflows/spec-first",
        json={
            "topic": "graph traversal",
            "requirements": "BFS and DFS traversal of an undirected graph.",
            "spec_tags": ["spec-role"],
            "impl_tags": ["impl-role"],
        },
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]

    tasks = _get_tasks(client)

    spec_tags = tasks[task_ids["spec_writer"]].get("required_tags", [])
    impl_tags = tasks[task_ids["implementer"]].get("required_tags", [])

    assert "spec-role" in spec_tags, f"spec_tags not forwarded: {spec_tags}"
    assert "impl-role" in impl_tags, f"impl_tags not forwarded: {impl_tags}"


def test_spec_first_workflow_empty_tags_result_in_no_constraint(client):
    """Empty tags lists should result in no required_tags constraint."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "set", "requirements": "Set with add/remove/contains."},
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]

    tasks = _get_tasks(client)
    for role, tid in task_ids.items():
        tags = tasks[tid].get("required_tags")
        assert not tags, f"{role} should have no required_tags, got: {tags}"


# ---------------------------------------------------------------------------
# Prompt content checks
# ---------------------------------------------------------------------------


def test_spec_first_spec_writer_prompt_mentions_topic(client):
    """Spec-writer task prompt should mention the topic."""
    topic = "red-black tree with insertion and deletion"
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": topic, "requirements": "Self-balancing BST."},
        headers=auth_headers(),
    )
    data = resp.json()
    spec_writer_id = data["task_ids"]["spec_writer"]

    tasks = _get_tasks(client)
    prompt = tasks[spec_writer_id].get("prompt", "")
    assert topic in prompt, f"topic not found in spec_writer prompt: {prompt[:200]}"


def test_spec_first_spec_writer_prompt_mentions_requirements(client):
    """Spec-writer task prompt should include the requirements."""
    requirements = "Implement a doubly-linked list with O(1) append and prepend."
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "linked list", "requirements": requirements},
        headers=auth_headers(),
    )
    data = resp.json()
    spec_writer_id = data["task_ids"]["spec_writer"]

    tasks = _get_tasks(client)
    prompt = tasks[spec_writer_id].get("prompt", "")
    assert requirements in prompt, (
        f"requirements not found in spec_writer prompt: {prompt[:300]}"
    )


def test_spec_first_spec_writer_prompt_mentions_role(client):
    """Spec-writer prompt should mention SPEC-WRITER role."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "graph", "requirements": "Adjacency list graph."},
        headers=auth_headers(),
    )
    data = resp.json()
    spec_writer_id = data["task_ids"]["spec_writer"]

    tasks = _get_tasks(client)
    prompt = tasks[spec_writer_id].get("prompt", "")
    assert "SPEC-WRITER" in prompt or "spec-writer" in prompt.lower() or "SPEC_WRITER" in prompt, (
        "spec_writer prompt should mention SPEC-WRITER role"
    )


def test_spec_first_implementer_prompt_mentions_topic(client):
    """Implementer task prompt should mention the topic."""
    topic = "priority queue with decrease-key"
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": topic, "requirements": "Fibonacci heap."},
        headers=auth_headers(),
    )
    data = resp.json()
    implementer_id = data["task_ids"]["implementer"]

    tasks = _get_tasks(client)
    prompt = tasks[implementer_id].get("prompt", "")
    assert topic in prompt, f"topic not found in implementer prompt: {prompt[:200]}"


def test_spec_first_implementer_prompt_mentions_role(client):
    """Implementer prompt should mention IMPLEMENTER role."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "trie", "requirements": "Trie with insert/search."},
        headers=auth_headers(),
    )
    data = resp.json()
    implementer_id = data["task_ids"]["implementer"]

    tasks = _get_tasks(client)
    prompt = tasks[implementer_id].get("prompt", "")
    assert "IMPLEMENTER" in prompt or "implementer" in prompt.lower(), (
        "implementer prompt should mention IMPLEMENTER role"
    )


def test_spec_first_spec_writer_prompt_contains_spec_md(client):
    """Spec-writer prompt should mention SPEC.md as the output artifact."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "binary search", "requirements": "Sorted list search."},
        headers=auth_headers(),
    )
    data = resp.json()
    spec_writer_id = data["task_ids"]["spec_writer"]

    tasks = _get_tasks(client)
    prompt = tasks[spec_writer_id].get("prompt", "")
    assert "SPEC.md" in prompt, f"SPEC.md not found in spec_writer prompt: {prompt[:200]}"


def test_spec_first_implementer_prompt_contains_spec_key(client):
    """Implementer prompt should reference the scratchpad spec key."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "binary search", "requirements": "Sorted list search."},
        headers=auth_headers(),
    )
    data = resp.json()
    prefix = data["scratchpad_prefix"]
    implementer_id = data["task_ids"]["implementer"]

    tasks = _get_tasks(client)
    prompt = tasks[implementer_id].get("prompt", "")
    expected_key = f"{prefix}_spec"
    assert expected_key in prompt, (
        f"scratchpad spec key '{expected_key}' not found in implementer prompt: {prompt[:300]}"
    )


def test_spec_first_spec_writer_prompt_contains_write_key(client):
    """Spec-writer prompt should contain a write command for the spec key."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "sorting", "requirements": "Sort integers ascending."},
        headers=auth_headers(),
    )
    data = resp.json()
    prefix = data["scratchpad_prefix"]
    spec_writer_id = data["task_ids"]["spec_writer"]

    tasks = _get_tasks(client)
    prompt = tasks[spec_writer_id].get("prompt", "")
    expected_key = f"{prefix}_spec"
    assert expected_key in prompt, (
        f"scratchpad spec key '{expected_key}' not found in spec_writer prompt: {prompt[:300]}"
    )


def test_spec_first_implementer_prompt_reads_scratchpad(client):
    """Implementer prompt should include a curl read command for the scratchpad."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "BST", "requirements": "Binary search tree operations."},
        headers=auth_headers(),
    )
    data = resp.json()
    implementer_id = data["task_ids"]["implementer"]

    tasks = _get_tasks(client)
    prompt = tasks[implementer_id].get("prompt", "")
    assert "curl" in prompt, "implementer prompt should contain curl commands for scratchpad"
    assert "scratchpad" in prompt.lower(), (
        "implementer prompt should reference the scratchpad"
    )


def test_spec_first_spec_writer_prompt_writes_scratchpad(client):
    """Spec-writer prompt should include a curl PUT command for the scratchpad."""
    resp = client.post(
        "/workflows/spec-first",
        json={"topic": "BST", "requirements": "Binary search tree operations."},
        headers=auth_headers(),
    )
    data = resp.json()
    spec_writer_id = data["task_ids"]["spec_writer"]

    tasks = _get_tasks(client)
    prompt = tasks[spec_writer_id].get("prompt", "")
    assert "curl" in prompt, "spec_writer prompt should contain curl commands for scratchpad"
    assert "PUT" in prompt or "put" in prompt.lower(), (
        "spec_writer prompt should PUT to the scratchpad"
    )


# ---------------------------------------------------------------------------
# Workflow uniqueness
# ---------------------------------------------------------------------------


def test_spec_first_multiple_submissions_have_distinct_workflow_ids(client):
    """Each submission should return a unique workflow_id."""
    resp1 = client.post(
        "/workflows/spec-first",
        json={"topic": "stack", "requirements": "LIFO stack."},
        headers=auth_headers(),
    )
    resp2 = client.post(
        "/workflows/spec-first",
        json={"topic": "queue", "requirements": "FIFO queue."},
        headers=auth_headers(),
    )
    assert resp1.json()["workflow_id"] != resp2.json()["workflow_id"]
