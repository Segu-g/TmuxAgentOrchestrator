"""Tests for POST /workflows/pair — PairCoder (Navigator + Driver) workflow.

The pair workflow builds a strictly sequential 2-agent DAG:

  navigator ──→ driver

- navigator: reads the task description, writes a structured PLAN.md
  (architecture, interfaces, acceptance criteria, step-by-step guide),
  stores it in scratchpad ``{prefix}_plan``.
- driver:    reads the navigator's plan, implements the code, writes tests,
  runs them, and writes ``driver_summary.md`` with pass/fail report.

Design references:
- Beck & Fowler "Extreme Programming Explained" (1999): Navigator/Driver roles.
- FlowHunt "TDD with AI Agents" (2025): PairCoder improves code quality vs
  single-agent baseline.
- Tweag "Agentic Coding Handbook — TDD" (2025): context-separated pair
  programming approach.
- DESIGN.md §10.27 (v1.0.27)
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
from tmux_orchestrator.web.app import create_app, PairWorkflowSubmit
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
# PairWorkflowSubmit — model validation
# ---------------------------------------------------------------------------


def test_pair_submit_empty_task_rejected():
    """Empty task should raise ValueError."""
    with pytest.raises(Exception):
        PairWorkflowSubmit(task="")


def test_pair_submit_whitespace_task_rejected():
    """Whitespace-only task should raise ValueError."""
    with pytest.raises(Exception):
        PairWorkflowSubmit(task="   ")


def test_pair_submit_valid_minimal():
    """Minimal valid request should construct with defaults."""
    obj = PairWorkflowSubmit(task="implement a binary search function")
    assert obj.task == "implement a binary search function"
    assert obj.navigator_tags == []
    assert obj.driver_tags == []
    assert obj.reply_to is None


def test_pair_submit_with_tags():
    """Tags should be accepted on both roles."""
    obj = PairWorkflowSubmit(
        task="implement a stack data structure",
        navigator_tags=["navigator"],
        driver_tags=["driver"],
    )
    assert obj.navigator_tags == ["navigator"]
    assert obj.driver_tags == ["driver"]


def test_pair_submit_with_reply_to():
    """reply_to field should be accepted."""
    obj = PairWorkflowSubmit(task="write a CSV parser", reply_to="director-1")
    assert obj.reply_to == "director-1"


def test_pair_submit_multiple_navigator_tags():
    """Multiple navigator tags should be accepted."""
    obj = PairWorkflowSubmit(
        task="implement LRU cache",
        navigator_tags=["navigator", "senior"],
    )
    assert len(obj.navigator_tags) == 2


# ---------------------------------------------------------------------------
# POST /workflows/pair — HTTP auth
# ---------------------------------------------------------------------------


def test_pair_workflow_requires_auth(client):
    """Endpoint should return 401 without API key."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a stack"},
    )
    assert resp.status_code == 401


def test_pair_workflow_wrong_api_key(client):
    """Endpoint should return 401 with wrong API key."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a stack"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_pair_workflow_empty_task_returns_422(client):
    """Empty task should return 422 Unprocessable Entity."""
    resp = client.post(
        "/workflows/pair",
        json={"task": ""},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_pair_workflow_missing_task_returns_422(client):
    """Missing task field should return 422."""
    resp = client.post(
        "/workflows/pair",
        json={},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_pair_workflow_returns_200(client):
    """Valid request should return 200."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a binary search function"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /workflows/pair — response structure
# ---------------------------------------------------------------------------


def test_pair_workflow_response_fields(client):
    """Response must contain workflow_id, name, task_ids, scratchpad_prefix."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a stack data structure"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "workflow_id" in data
    assert "name" in data
    assert "task_ids" in data
    assert "scratchpad_prefix" in data


def test_pair_workflow_name_starts_with_pair(client):
    """Workflow name should start with 'pair/'."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a stack"},
        headers=auth_headers(),
    )
    data = resp.json()
    assert data["name"].startswith("pair/")


def test_pair_workflow_name_contains_task_start(client):
    """Workflow name should contain the beginning of the task."""
    task = "implement a binary search function"
    resp = client.post(
        "/workflows/pair",
        json={"task": task},
        headers=auth_headers(),
    )
    data = resp.json()
    assert "implement a binary search function" in data["name"]


def test_pair_workflow_task_count(client):
    """Workflow must create exactly 2 tasks: navigator and driver."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a queue"},
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]
    assert len(task_ids) == 2
    assert "navigator" in task_ids
    assert "driver" in task_ids


def test_pair_workflow_task_ids_are_strings(client):
    """All task IDs should be non-empty strings."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a hash map"},
        headers=auth_headers(),
    )
    data = resp.json()
    for role, tid in data["task_ids"].items():
        assert isinstance(tid, str), f"task_id for {role} is not a string"
        assert len(tid) > 0, f"task_id for {role} is empty"


def test_pair_workflow_task_ids_distinct(client):
    """Both task IDs must be distinct."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a linked list"},
        headers=auth_headers(),
    )
    data = resp.json()
    ids = list(data["task_ids"].values())
    assert len(ids) == len(set(ids)), "task IDs are not distinct"


# ---------------------------------------------------------------------------
# Scratchpad key naming
# ---------------------------------------------------------------------------


def test_pair_scratchpad_prefix_format(client):
    """Scratchpad prefix should match 'pair_XXXXXXXX' pattern."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a sorting algorithm"},
        headers=auth_headers(),
    )
    data = resp.json()
    assert re.match(r"^pair_[0-9a-f]{8}$", data["scratchpad_prefix"]), (
        f"unexpected prefix: {data['scratchpad_prefix']}"
    )


def test_pair_scratchpad_prefix_unique_across_runs(client):
    """Two workflow submissions should produce distinct scratchpad prefixes."""
    resp1 = client.post(
        "/workflows/pair",
        json={"task": "implement task A"},
        headers=auth_headers(),
    )
    resp2 = client.post(
        "/workflows/pair",
        json={"task": "implement task B"},
        headers=auth_headers(),
    )
    assert resp1.json()["scratchpad_prefix"] != resp2.json()["scratchpad_prefix"]


# ---------------------------------------------------------------------------
# Task dependency chain
# ---------------------------------------------------------------------------


def test_pair_navigator_has_no_dependencies(client):
    """navigator task should have no depends_on."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a binary heap"},
        headers=auth_headers(),
    )
    data = resp.json()
    navigator_id = data["task_ids"]["navigator"]

    tasks = _get_tasks(client)
    assert navigator_id in tasks, "navigator task not found in /tasks"
    assert tasks[navigator_id].get("depends_on", []) == [], (
        "navigator should have no dependencies"
    )


def test_pair_driver_depends_on_navigator(client):
    """driver task should depend on the navigator task."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a priority queue"},
        headers=auth_headers(),
    )
    data = resp.json()
    navigator_id = data["task_ids"]["navigator"]
    driver_id = data["task_ids"]["driver"]

    tasks = _get_tasks(client)
    assert driver_id in tasks, "driver task not found in /tasks"
    assert navigator_id in tasks[driver_id].get("depends_on", []), (
        f"driver.depends_on={tasks[driver_id].get('depends_on')} "
        f"does not contain navigator_id={navigator_id}"
    )


# ---------------------------------------------------------------------------
# reply_to propagation
# ---------------------------------------------------------------------------


def test_pair_workflow_reply_to_passed_to_driver():
    """reply_to should be forwarded to the driver task only."""
    app, orch = _make_app()
    reply_tos: list = []
    original_submit = orch.submit_task

    async def capture_submit(*args, **kwargs):
        reply_tos.append(kwargs.get("reply_to"))
        return await original_submit(*args, **kwargs)

    orch.submit_task = capture_submit  # type: ignore[method-assign]

    with TestClient(app) as c:
        c.post(
            "/workflows/pair",
            json={"task": "implement a trie", "reply_to": "director-1"},
            headers=auth_headers(),
        )

    # 2 tasks: navigator, driver
    assert len(reply_tos) == 2, f"Expected 2 submit_task calls, got {len(reply_tos)}"
    # Only driver (last task) should have reply_to
    assert reply_tos[1] == "director-1", (
        f"reply_to not propagated to driver: {reply_tos}"
    )
    assert reply_tos[0] is None, f"navigator should not have reply_to: {reply_tos[0]}"


def test_pair_workflow_reply_to_none_by_default(client):
    """reply_to=None should result in no reply_to on any task."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a set"},
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


def test_pair_workflow_tags_forwarded(client):
    """navigator_tags and driver_tags should appear in task required_tags."""
    resp = client.post(
        "/workflows/pair",
        json={
            "task": "implement a graph traversal",
            "navigator_tags": ["nav-role"],
            "driver_tags": ["drv-role"],
        },
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]

    tasks = _get_tasks(client)

    nav_tags = tasks[task_ids["navigator"]].get("required_tags", [])
    drv_tags = tasks[task_ids["driver"]].get("required_tags", [])

    assert "nav-role" in nav_tags, f"navigator_tags not forwarded: {nav_tags}"
    assert "drv-role" in drv_tags, f"driver_tags not forwarded: {drv_tags}"


def test_pair_workflow_empty_tags_result_in_none(client):
    """Empty tags lists should result in no required_tags constraint."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a set"},
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


def test_pair_navigator_prompt_mentions_task(client):
    """Navigator task prompt should mention the task description."""
    task = "implement a red-black tree with insertion and deletion"
    resp = client.post(
        "/workflows/pair",
        json={"task": task},
        headers=auth_headers(),
    )
    data = resp.json()
    navigator_id = data["task_ids"]["navigator"]

    tasks = _get_tasks(client)
    prompt = tasks[navigator_id].get("prompt", "")
    assert task in prompt, f"task not found in navigator prompt: {prompt[:200]}"


def test_pair_driver_prompt_mentions_task(client):
    """Driver task prompt should mention the task description."""
    task = "implement a red-black tree with search"
    resp = client.post(
        "/workflows/pair",
        json={"task": task},
        headers=auth_headers(),
    )
    data = resp.json()
    driver_id = data["task_ids"]["driver"]

    tasks = _get_tasks(client)
    prompt = tasks[driver_id].get("prompt", "")
    assert task in prompt, f"task not found in driver prompt: {prompt[:200]}"


def test_pair_navigator_prompt_mentions_role(client):
    """Navigator prompt should mention NAVIGATOR role."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a graph"},
        headers=auth_headers(),
    )
    data = resp.json()
    navigator_id = data["task_ids"]["navigator"]

    tasks = _get_tasks(client)
    prompt = tasks[navigator_id].get("prompt", "")
    assert "NAVIGATOR" in prompt or "navigator" in prompt.lower(), (
        "navigator prompt should mention NAVIGATOR role"
    )


def test_pair_driver_prompt_mentions_role(client):
    """Driver prompt should mention DRIVER role."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a graph"},
        headers=auth_headers(),
    )
    data = resp.json()
    driver_id = data["task_ids"]["driver"]

    tasks = _get_tasks(client)
    prompt = tasks[driver_id].get("prompt", "")
    assert "DRIVER" in prompt or "driver" in prompt.lower(), (
        "driver prompt should mention DRIVER role"
    )


def test_pair_scratchpad_keys_in_prompts(client):
    """Scratchpad plan key should appear in both navigator and driver prompts."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a merge sort"},
        headers=auth_headers(),
    )
    data = resp.json()
    prefix = data["scratchpad_prefix"]
    task_ids = data["task_ids"]

    tasks = _get_tasks(client)
    nav_prompt = tasks[task_ids["navigator"]].get("prompt", "")
    drv_prompt = tasks[task_ids["driver"]].get("prompt", "")

    plan_key = f"{prefix}_plan"
    assert plan_key in nav_prompt, f"plan key missing from navigator prompt"
    assert plan_key in drv_prompt, f"plan key missing from driver prompt"


# ---------------------------------------------------------------------------
# Workflow ID
# ---------------------------------------------------------------------------


def test_pair_workflow_id_is_uuid(client):
    """workflow_id should be a valid UUID string."""
    resp = client.post(
        "/workflows/pair",
        json={"task": "implement a doubly linked list"},
        headers=auth_headers(),
    )
    data = resp.json()
    # Should not raise
    uuid.UUID(data["workflow_id"])


def test_pair_workflow_two_runs_different_workflow_ids(client):
    """Two workflow submissions should have distinct workflow IDs."""
    resp1 = client.post(
        "/workflows/pair",
        json={"task": "implement task X"},
        headers=auth_headers(),
    )
    resp2 = client.post(
        "/workflows/pair",
        json={"task": "implement task Y"},
        headers=auth_headers(),
    )
    assert resp1.json()["workflow_id"] != resp2.json()["workflow_id"]


# ---------------------------------------------------------------------------
# OpenAPI schema
# ---------------------------------------------------------------------------


def test_pair_workflow_registered_in_openapi(client):
    """The /workflows/pair endpoint should be listed in the OpenAPI schema."""
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    paths = schema.get("paths", {})
    assert "/workflows/pair" in paths, (
        f"/workflows/pair not found in OpenAPI paths: {list(paths.keys())}"
    )


def test_pair_workflow_openapi_has_post_method(client):
    """The /workflows/pair endpoint should support POST."""
    resp = client.get("/openapi.json")
    schema = resp.json()
    assert "post" in schema["paths"]["/workflows/pair"]


# ---------------------------------------------------------------------------
# Long task truncation in name
# ---------------------------------------------------------------------------


def test_pair_workflow_long_task_truncated_in_name(client):
    """Workflow name suffix should be at most 40 chars (from task[:40].strip())."""
    long_task = "implement a very complex data structure that has many features and more"
    resp = client.post(
        "/workflows/pair",
        json={"task": long_task},
        headers=auth_headers(),
    )
    data = resp.json()
    name_suffix = data["name"][len("pair/"):]
    assert len(name_suffix) <= 40, f"name suffix too long: {len(name_suffix)} chars"
