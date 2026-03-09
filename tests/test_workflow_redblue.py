"""Tests for POST /workflows/redblue — Red Team / Blue Team adversarial evaluation.

The Red-Blue workflow builds a strictly sequential 3-agent DAG:

  blue_team ──→ red_team ──→ arbiter

- blue_team:  designs or implements a solution for *topic*
- red_team:   attacks the blue-team output, finding vulnerabilities and risks
- arbiter:    produces a balanced risk assessment and prioritised recommendations

Design references:
- Harrasse et al. "Debate, Deliberate, Decide (D3)" arXiv:2410.04663 (2026):
  adversarial multi-agent evaluation reduces positional/verbosity bias.
- "Red-Teaming LLM Multi-Agent Systems via Communication Attacks" ACL 2025
  (arXiv:2502.14847): structured adversarial evaluation improves robustness.
- Farzulla "Autonomous Red Team and Blue Team AI" DISSENSUS DAI-2513 (2025).
- DESIGN.md §10.23 (v1.0.24)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app, RedBlueWorkflowSubmit
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
# RedBlueWorkflowSubmit — model validation
# ---------------------------------------------------------------------------


def test_redblue_submit_empty_topic_rejected():
    """Empty topic should raise ValueError."""
    with pytest.raises(Exception):
        RedBlueWorkflowSubmit(topic="")


def test_redblue_submit_whitespace_topic_rejected():
    """Whitespace-only topic should raise ValueError."""
    with pytest.raises(Exception):
        RedBlueWorkflowSubmit(topic="   ")


def test_redblue_submit_valid_minimal():
    """Minimal valid request should construct with defaults."""
    obj = RedBlueWorkflowSubmit(topic="FastAPI endpoint authentication design")
    assert obj.topic == "FastAPI endpoint authentication design"
    assert obj.blue_tags == []
    assert obj.red_tags == []
    assert obj.arbiter_tags == []
    assert obj.reply_to is None


def test_redblue_submit_with_tags():
    """Tags should be accepted on all three roles."""
    obj = RedBlueWorkflowSubmit(
        topic="Database schema design",
        blue_tags=["architect"],
        red_tags=["security"],
        arbiter_tags=["senior"],
    )
    assert obj.blue_tags == ["architect"]
    assert obj.red_tags == ["security"]
    assert obj.arbiter_tags == ["senior"]


def test_redblue_submit_with_reply_to():
    """reply_to field should be accepted."""
    obj = RedBlueWorkflowSubmit(topic="API design", reply_to="director-1")
    assert obj.reply_to == "director-1"


# ---------------------------------------------------------------------------
# POST /workflows/redblue — HTTP responses
# ---------------------------------------------------------------------------


def test_redblue_workflow_requires_auth(client):
    """Endpoint should return 401 without API key."""
    resp = client.post(
        "/workflows/redblue",
        json={"topic": "FastAPI endpoint design"},
    )
    assert resp.status_code == 401


def test_redblue_workflow_wrong_api_key(client):
    """Endpoint should return 401 with wrong API key."""
    resp = client.post(
        "/workflows/redblue",
        json={"topic": "FastAPI endpoint design"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_redblue_workflow_empty_topic_returns_422(client):
    """Empty topic should return 422 Unprocessable Entity."""
    resp = client.post(
        "/workflows/redblue",
        json={"topic": ""},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_redblue_workflow_missing_topic_returns_422(client):
    """Missing topic field should return 422."""
    resp = client.post(
        "/workflows/redblue",
        json={},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_redblue_workflow_returns_200(client):
    """Valid request should return 200."""
    resp = client.post(
        "/workflows/redblue",
        json={"topic": "FastAPI endpoint authentication design"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /workflows/redblue — response structure
# ---------------------------------------------------------------------------


def test_redblue_workflow_response_fields(client):
    """Response must contain workflow_id, name, task_ids, scratchpad_prefix."""
    resp = client.post(
        "/workflows/redblue",
        json={"topic": "API rate limiting design"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "workflow_id" in data
    assert "name" in data
    assert "task_ids" in data
    assert "scratchpad_prefix" in data


def test_redblue_workflow_name_format(client):
    """Workflow name should be 'redblue/<topic>'."""
    resp = client.post(
        "/workflows/redblue",
        json={"topic": "API rate limiting design"},
        headers=auth_headers(),
    )
    data = resp.json()
    assert data["name"] == "redblue/API rate limiting design"


def test_redblue_workflow_task_count(client):
    """Workflow must create exactly 3 tasks: blue_team, red_team, arbiter."""
    resp = client.post(
        "/workflows/redblue",
        json={"topic": "user authentication"},
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]
    assert len(task_ids) == 3
    assert "blue_team" in task_ids
    assert "red_team" in task_ids
    assert "arbiter" in task_ids


def test_redblue_workflow_task_ids_are_strings(client):
    """All task IDs should be non-empty strings."""
    resp = client.post(
        "/workflows/redblue",
        json={"topic": "input validation design"},
        headers=auth_headers(),
    )
    data = resp.json()
    for role, tid in data["task_ids"].items():
        assert isinstance(tid, str), f"task_id for {role} is not a string"
        assert len(tid) > 0, f"task_id for {role} is empty"


def test_redblue_workflow_task_ids_distinct(client):
    """All 3 task IDs must be distinct."""
    resp = client.post(
        "/workflows/redblue",
        json={"topic": "caching strategy"},
        headers=auth_headers(),
    )
    data = resp.json()
    ids = list(data["task_ids"].values())
    assert len(ids) == len(set(ids)), "task IDs are not distinct"


# ---------------------------------------------------------------------------
# Scratchpad key naming
# ---------------------------------------------------------------------------


def test_redblue_scratchpad_prefix_format(client):
    """Scratchpad prefix should match 'redblue_XXXXXXXX' pattern."""
    import re

    resp = client.post(
        "/workflows/redblue",
        json={"topic": "API design"},
        headers=auth_headers(),
    )
    data = resp.json()
    prefix = data["scratchpad_prefix"]
    assert re.match(r"^redblue_[0-9a-f]{8}$", prefix), (
        f"scratchpad_prefix '{prefix}' does not match pattern redblue_XXXXXXXX"
    )


def test_redblue_scratchpad_prefix_unique(client):
    """Two submissions should produce different scratchpad prefixes."""
    app, orch = _make_app()
    with TestClient(app) as c:
        r1 = c.post(
            "/workflows/redblue",
            json={"topic": "API design"},
            headers=auth_headers(),
        )
        r2 = c.post(
            "/workflows/redblue",
            json={"topic": "API design"},
            headers=auth_headers(),
        )
    assert r1.json()["scratchpad_prefix"] != r2.json()["scratchpad_prefix"]


# ---------------------------------------------------------------------------
# DAG dependency chain verification
# ---------------------------------------------------------------------------


def test_redblue_dag_dependency_chain(client):
    """red_team must depend on blue_team; arbiter must depend on red_team."""
    resp = client.post(
        "/workflows/redblue",
        json={"topic": "JWT authentication design"},
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]

    blue_id = task_ids["blue_team"]
    red_id = task_ids["red_team"]
    arbiter_id = task_ids["arbiter"]

    # Retrieve submitted tasks from the orchestrator via /tasks
    tasks_resp = client.get("/tasks", headers=auth_headers())
    assert tasks_resp.status_code == 200
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    assert blue_id in tasks, "blue_team task not found in /tasks"
    assert red_id in tasks, "red_team task not found in /tasks"
    assert arbiter_id in tasks, "arbiter task not found in /tasks"

    # blue_team: no dependencies
    assert tasks[blue_id].get("depends_on", []) == [], (
        "blue_team should have no dependencies"
    )
    # red_team: depends on blue_team
    assert blue_id in tasks[red_id].get("depends_on", []), (
        "red_team should depend on blue_team"
    )
    # arbiter: depends on red_team
    assert red_id in tasks[arbiter_id].get("depends_on", []), (
        "arbiter should depend on red_team"
    )


# ---------------------------------------------------------------------------
# reply_to propagation
# ---------------------------------------------------------------------------


def test_redblue_reply_to_propagated_to_arbiter():
    """reply_to should be forwarded to the arbiter task (captured via mock)."""
    app, orch = _make_app()
    reply_tos: list = []
    original_submit = orch.submit_task

    async def capture_submit(*args, **kwargs):
        reply_tos.append(kwargs.get("reply_to"))
        return await original_submit(*args, **kwargs)

    orch.submit_task = capture_submit  # type: ignore[method-assign]

    with TestClient(app) as c:
        c.post(
            "/workflows/redblue",
            json={"topic": "API design", "reply_to": "director-agent"},
            headers=auth_headers(),
        )

    # 3 tasks: blue_team, red_team, arbiter
    assert len(reply_tos) == 3, f"Expected 3 submit_task calls, got {len(reply_tos)}"
    # Only arbiter (last task) should have reply_to
    assert reply_tos[2] == "director-agent", (
        f"reply_to not propagated to arbiter: {reply_tos}"
    )
    assert reply_tos[0] is None, f"blue_team should not have reply_to: {reply_tos[0]}"
    assert reply_tos[1] is None, f"red_team should not have reply_to: {reply_tos[1]}"


def test_redblue_reply_to_none_by_default(client):
    """reply_to=None should result in no reply_to on any task."""
    resp = client.post(
        "/workflows/redblue",
        json={"topic": "API design"},
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    for role, tid in task_ids.items():
        rt = tasks[tid].get("reply_to")
        assert rt is None, f"{role} task has unexpected reply_to={rt!r}"


# ---------------------------------------------------------------------------
# Tag routing
# ---------------------------------------------------------------------------


def test_redblue_tags_forwarded(client):
    """blue_tags, red_tags, arbiter_tags should appear in task required_tags."""
    resp = client.post(
        "/workflows/redblue",
        json={
            "topic": "API design",
            "blue_tags": ["architect"],
            "red_tags": ["security"],
            "arbiter_tags": ["senior"],
        },
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    blue_tags = tasks[task_ids["blue_team"]].get("required_tags", [])
    red_tags = tasks[task_ids["red_team"]].get("required_tags", [])
    arbiter_tags = tasks[task_ids["arbiter"]].get("required_tags", [])

    assert "architect" in blue_tags, f"blue_tags not forwarded: {blue_tags}"
    assert "security" in red_tags, f"red_tags not forwarded: {red_tags}"
    assert "senior" in arbiter_tags, f"arbiter_tags not forwarded: {arbiter_tags}"


# ---------------------------------------------------------------------------
# Prompt content checks
# ---------------------------------------------------------------------------


def test_redblue_blue_team_prompt_mentions_topic(client):
    """Blue-team task prompt should mention the topic."""
    topic = "OAuth2 token refresh endpoint design"
    resp = client.post(
        "/workflows/redblue",
        json={"topic": topic},
        headers=auth_headers(),
    )
    data = resp.json()
    blue_id = data["task_ids"]["blue_team"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    prompt = tasks[blue_id].get("prompt", "")
    assert topic in prompt, f"topic not found in blue_team prompt: {prompt[:200]}"


def test_redblue_red_team_prompt_mentions_topic(client):
    """Red-team task prompt should mention the topic."""
    topic = "input validation middleware design"
    resp = client.post(
        "/workflows/redblue",
        json={"topic": topic},
        headers=auth_headers(),
    )
    data = resp.json()
    red_id = data["task_ids"]["red_team"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    prompt = tasks[red_id].get("prompt", "")
    assert topic in prompt, f"topic not found in red_team prompt: {prompt[:200]}"


def test_redblue_arbiter_prompt_mentions_topic(client):
    """Arbiter task prompt should mention the topic."""
    topic = "RBAC permission model design"
    resp = client.post(
        "/workflows/redblue",
        json={"topic": topic},
        headers=auth_headers(),
    )
    data = resp.json()
    arbiter_id = data["task_ids"]["arbiter"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    prompt = tasks[arbiter_id].get("prompt", "")
    assert topic in prompt, f"topic not found in arbiter prompt: {prompt[:200]}"


def test_redblue_blue_team_prompt_mentions_role(client):
    """Blue-team prompt should mention BLUE-TEAM role."""
    resp = client.post(
        "/workflows/redblue",
        json={"topic": "API design"},
        headers=auth_headers(),
    )
    data = resp.json()
    blue_id = data["task_ids"]["blue_team"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    prompt = tasks[blue_id].get("prompt", "")
    assert "BLUE-TEAM" in prompt or "blue-team" in prompt.lower(), (
        "blue_team prompt should mention BLUE-TEAM role"
    )


def test_redblue_red_team_prompt_mentions_role(client):
    """Red-team prompt should mention RED-TEAM role."""
    resp = client.post(
        "/workflows/redblue",
        json={"topic": "API design"},
        headers=auth_headers(),
    )
    data = resp.json()
    red_id = data["task_ids"]["red_team"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    prompt = tasks[red_id].get("prompt", "")
    assert "RED-TEAM" in prompt or "red-team" in prompt.lower(), (
        "red_team prompt should mention RED-TEAM role"
    )


def test_redblue_arbiter_prompt_mentions_role(client):
    """Arbiter prompt should mention ARBITER role."""
    resp = client.post(
        "/workflows/redblue",
        json={"topic": "API design"},
        headers=auth_headers(),
    )
    data = resp.json()
    arbiter_id = data["task_ids"]["arbiter"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    prompt = tasks[arbiter_id].get("prompt", "")
    assert "ARBITER" in prompt or "arbiter" in prompt.lower(), (
        "arbiter prompt should mention ARBITER role"
    )


def test_redblue_scratchpad_keys_in_prompts(client):
    """Scratchpad keys should appear in the agent prompts."""
    resp = client.post(
        "/workflows/redblue",
        json={"topic": "API design"},
        headers=auth_headers(),
    )
    data = resp.json()
    prefix = data["scratchpad_prefix"]
    task_ids = data["task_ids"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    blue_prompt = tasks[task_ids["blue_team"]].get("prompt", "")
    red_prompt = tasks[task_ids["red_team"]].get("prompt", "")
    arbiter_prompt = tasks[task_ids["arbiter"]].get("prompt", "")

    assert f"{prefix}_blue_design" in blue_prompt, (
        "blue_design key missing from blue_team prompt"
    )
    assert f"{prefix}_blue_design" in red_prompt, (
        "blue_design key missing from red_team prompt (needed for reading)"
    )
    assert f"{prefix}_red_findings" in red_prompt, (
        "red_findings key missing from red_team prompt"
    )
    assert f"{prefix}_blue_design" in arbiter_prompt, (
        "blue_design key missing from arbiter prompt (needed for reading)"
    )
    assert f"{prefix}_red_findings" in arbiter_prompt, (
        "red_findings key missing from arbiter prompt (needed for reading)"
    )
    assert f"{prefix}_risk_report" in arbiter_prompt, (
        "risk_report key missing from arbiter prompt"
    )
