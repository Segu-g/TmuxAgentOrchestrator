"""Tests for POST /workflows/redblue — Red Team / Blue Team security review workflow.

The Red-Blue workflow builds a strictly sequential 3-agent DAG:

  implement (blue-team) ──→ attack (red-team) ──→ assess (arbiter)

- implement:  blue-team agent implements the feature_description in language
- attack:     red-team agent identifies vulnerabilities based on security_focus list
- assess:     arbiter produces a CVSS-style risk assessment report

Design references:
- arXiv:2601.19138, "AgenticSCR: Autonomous Agentic Secure Code Review" (2025):
  agentic multi-iteration secure code review with contextual awareness.
- "Red-Teaming LLM Multi-Agent Systems via Communication Attacks" ACL 2025
  (arXiv:2502.14847): structured adversarial evaluation improves robustness.
- OWASP Top 10 for LLMs 2025: security_focus maps to OWASP vulnerability categories.
- DESIGN.md §10.75 (v1.1.43)
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


def test_redblue_submit_empty_feature_description_rejected():
    """Empty feature_description should raise ValueError."""
    with pytest.raises(Exception):
        RedBlueWorkflowSubmit(feature_description="")


def test_redblue_submit_whitespace_feature_description_rejected():
    """Whitespace-only feature_description should raise ValueError."""
    with pytest.raises(Exception):
        RedBlueWorkflowSubmit(feature_description="   ")


def test_redblue_submit_valid_minimal():
    """Minimal valid request should construct with defaults."""
    obj = RedBlueWorkflowSubmit(
        feature_description="REST endpoint for user authentication with JWT tokens"
    )
    assert obj.feature_description == "REST endpoint for user authentication with JWT tokens"
    assert obj.language == "python"
    assert obj.security_focus == ["input_validation", "authentication", "injection"]
    assert obj.scratchpad_prefix == "redblue"
    assert obj.agent_timeout == 300
    assert obj.reply_to is None


def test_redblue_submit_default_language_is_python():
    """Default language should be 'python'."""
    obj = RedBlueWorkflowSubmit(feature_description="some feature")
    assert obj.language == "python"


def test_redblue_submit_custom_language():
    """Custom language should be accepted."""
    obj = RedBlueWorkflowSubmit(feature_description="some feature", language="go")
    assert obj.language == "go"


def test_redblue_submit_custom_security_focus():
    """Custom security_focus list should be accepted."""
    obj = RedBlueWorkflowSubmit(
        feature_description="feature",
        security_focus=["sql_injection", "xss", "csrf"],
    )
    assert obj.security_focus == ["sql_injection", "xss", "csrf"]


def test_redblue_submit_default_tags():
    """Default tags should be redblue_blue, redblue_red, redblue_arbiter."""
    obj = RedBlueWorkflowSubmit(feature_description="feature")
    assert obj.blue_tags == ["redblue_blue"]
    assert obj.red_tags == ["redblue_red"]
    assert obj.arbiter_tags == ["redblue_arbiter"]


def test_redblue_submit_custom_tags():
    """Custom tags should override defaults."""
    obj = RedBlueWorkflowSubmit(
        feature_description="feature",
        blue_tags=["custom_blue"],
        red_tags=["custom_red"],
        arbiter_tags=["custom_arbiter"],
    )
    assert obj.blue_tags == ["custom_blue"]
    assert obj.red_tags == ["custom_red"]
    assert obj.arbiter_tags == ["custom_arbiter"]


def test_redblue_submit_with_reply_to():
    """reply_to field should be accepted."""
    obj = RedBlueWorkflowSubmit(
        feature_description="API design", reply_to="director-1"
    )
    assert obj.reply_to == "director-1"


def test_redblue_submit_agent_timeout():
    """agent_timeout should be accepted."""
    obj = RedBlueWorkflowSubmit(
        feature_description="feature", agent_timeout=900
    )
    assert obj.agent_timeout == 900


# ---------------------------------------------------------------------------
# POST /workflows/redblue — HTTP responses
# ---------------------------------------------------------------------------


def test_redblue_workflow_requires_auth(client):
    """Endpoint should return 401 without API key."""
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": "JWT authentication endpoint"},
    )
    assert resp.status_code == 401


def test_redblue_workflow_wrong_api_key(client):
    """Endpoint should return 401 with wrong API key."""
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": "JWT authentication endpoint"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_redblue_workflow_empty_feature_description_returns_422(client):
    """Empty feature_description should return 422 Unprocessable Entity."""
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": ""},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_redblue_workflow_missing_feature_description_returns_422(client):
    """Missing feature_description field should return 422."""
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
        json={"feature_description": "REST endpoint for user authentication with JWT tokens"},
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
        json={"feature_description": "API rate limiting endpoint"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "workflow_id" in data
    assert "name" in data
    assert "task_ids" in data
    assert "scratchpad_prefix" in data


def test_redblue_workflow_phase_names(client):
    """Workflow must create exactly 3 phases: implement, attack, assess."""
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": "user authentication"},
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]
    assert len(task_ids) == 3
    assert "implement" in task_ids
    assert "attack" in task_ids
    assert "assess" in task_ids


def test_redblue_workflow_task_ids_are_strings(client):
    """All task IDs should be non-empty strings."""
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": "input validation middleware"},
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
        json={"feature_description": "caching strategy"},
        headers=auth_headers(),
    )
    data = resp.json()
    ids = list(data["task_ids"].values())
    assert len(ids) == len(set(ids)), "task IDs are not distinct"


# ---------------------------------------------------------------------------
# Scratchpad key naming
# ---------------------------------------------------------------------------


def test_redblue_scratchpad_prefix_contains_uuid_suffix(client):
    """Scratchpad prefix should be 'redblue_XXXXXXXX' pattern."""
    import re

    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": "API design"},
        headers=auth_headers(),
    )
    data = resp.json()
    prefix = data["scratchpad_prefix"]
    assert re.match(r"^redblue_[0-9a-f]{8}$", prefix), (
        f"scratchpad_prefix '{prefix}' does not match pattern redblue_XXXXXXXX"
    )


def test_redblue_scratchpad_prefix_uses_custom_prefix(client):
    """Custom scratchpad_prefix should appear in the prefix."""
    import re

    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": "API design", "scratchpad_prefix": "myreview"},
        headers=auth_headers(),
    )
    data = resp.json()
    prefix = data["scratchpad_prefix"]
    assert re.match(r"^myreview_[0-9a-f]{8}$", prefix), (
        f"scratchpad_prefix '{prefix}' does not use custom prefix 'myreview'"
    )


def test_redblue_scratchpad_prefix_unique(client):
    """Two submissions should produce different scratchpad prefixes."""
    app, orch = _make_app()
    with TestClient(app) as c:
        r1 = c.post(
            "/workflows/redblue",
            json={"feature_description": "API design"},
            headers=auth_headers(),
        )
        r2 = c.post(
            "/workflows/redblue",
            json={"feature_description": "API design"},
            headers=auth_headers(),
        )
    assert r1.json()["scratchpad_prefix"] != r2.json()["scratchpad_prefix"]


# ---------------------------------------------------------------------------
# DAG dependency chain verification
# ---------------------------------------------------------------------------


def test_redblue_dag_dependency_chain(client):
    """attack must depend on implement; assess must depend on attack."""
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": "JWT authentication endpoint"},
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]

    implement_id = task_ids["implement"]
    attack_id = task_ids["attack"]
    assess_id = task_ids["assess"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    assert tasks_resp.status_code == 200
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    assert implement_id in tasks, "implement task not found in /tasks"
    assert attack_id in tasks, "attack task not found in /tasks"
    assert assess_id in tasks, "assess task not found in /tasks"

    # implement: no dependencies
    assert tasks[implement_id].get("depends_on", []) == [], (
        "implement should have no dependencies"
    )
    # attack: depends on implement
    assert implement_id in tasks[attack_id].get("depends_on", []), (
        "attack should depend on implement"
    )
    # assess: depends on attack
    assert attack_id in tasks[assess_id].get("depends_on", []), (
        "assess should depend on attack"
    )


# ---------------------------------------------------------------------------
# Required tags routing
# ---------------------------------------------------------------------------


def test_redblue_default_tags_forwarded(client):
    """Default tags redblue_blue/red/arbiter should appear in task required_tags."""
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": "API design"},
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    blue_tags = tasks[task_ids["implement"]].get("required_tags", [])
    red_tags = tasks[task_ids["attack"]].get("required_tags", [])
    arbiter_tags = tasks[task_ids["assess"]].get("required_tags", [])

    assert "redblue_blue" in blue_tags, f"redblue_blue not in implement tags: {blue_tags}"
    assert "redblue_red" in red_tags, f"redblue_red not in attack tags: {red_tags}"
    assert "redblue_arbiter" in arbiter_tags, f"redblue_arbiter not in assess tags: {arbiter_tags}"


def test_redblue_custom_tags_forwarded(client):
    """Custom blue_tags, red_tags, arbiter_tags should appear in task required_tags."""
    resp = client.post(
        "/workflows/redblue",
        json={
            "feature_description": "API design",
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

    blue_tags = tasks[task_ids["implement"]].get("required_tags", [])
    red_tags = tasks[task_ids["attack"]].get("required_tags", [])
    arbiter_tags = tasks[task_ids["assess"]].get("required_tags", [])

    assert "architect" in blue_tags, f"blue_tags not forwarded: {blue_tags}"
    assert "security" in red_tags, f"red_tags not forwarded: {red_tags}"
    assert "senior" in arbiter_tags, f"arbiter_tags not forwarded: {arbiter_tags}"


# ---------------------------------------------------------------------------
# reply_to propagation
# ---------------------------------------------------------------------------


def test_redblue_reply_to_propagated_to_assess():
    """reply_to should be forwarded to the assess (arbiter) task."""
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
            json={"feature_description": "API design", "reply_to": "director-agent"},
            headers=auth_headers(),
        )

    # 3 tasks: implement, attack, assess
    assert len(reply_tos) == 3, f"Expected 3 submit_task calls, got {len(reply_tos)}"
    # Only assess (last task) should have reply_to
    assert reply_tos[2] == "director-agent", (
        f"reply_to not propagated to assess: {reply_tos}"
    )
    assert reply_tos[0] is None, f"implement should not have reply_to: {reply_tos[0]}"
    assert reply_tos[1] is None, f"attack should not have reply_to: {reply_tos[1]}"


def test_redblue_reply_to_none_by_default(client):
    """reply_to=None should result in no reply_to on any task."""
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": "API design"},
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
# Prompt content checks
# ---------------------------------------------------------------------------


def test_redblue_implement_prompt_mentions_feature_description(client):
    """Implement task prompt should mention the feature_description."""
    feature = "REST endpoint for user authentication with JWT tokens"
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": feature},
        headers=auth_headers(),
    )
    data = resp.json()
    implement_id = data["task_ids"]["implement"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    prompt = tasks[implement_id].get("prompt", "")
    assert feature in prompt, f"feature_description not found in implement prompt: {prompt[:200]}"


def test_redblue_attack_prompt_mentions_feature_description(client):
    """Attack task prompt should mention the feature_description."""
    feature = "input validation middleware"
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": feature},
        headers=auth_headers(),
    )
    data = resp.json()
    attack_id = data["task_ids"]["attack"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    prompt = tasks[attack_id].get("prompt", "")
    assert feature in prompt, f"feature_description not found in attack prompt: {prompt[:200]}"


def test_redblue_assess_prompt_mentions_feature_description(client):
    """Assess task prompt should mention the feature_description."""
    feature = "RBAC permission model"
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": feature},
        headers=auth_headers(),
    )
    data = resp.json()
    assess_id = data["task_ids"]["assess"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    prompt = tasks[assess_id].get("prompt", "")
    assert feature in prompt, f"feature_description not found in assess prompt: {prompt[:200]}"


def test_redblue_implement_prompt_mentions_language(client):
    """Implement prompt should mention the language."""
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": "some feature", "language": "go"},
        headers=auth_headers(),
    )
    data = resp.json()
    implement_id = data["task_ids"]["implement"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    prompt = tasks[implement_id].get("prompt", "")
    assert "go" in prompt.lower(), "language 'go' not found in implement prompt"


def test_redblue_attack_prompt_mentions_security_focus(client):
    """Attack prompt should include the security_focus items."""
    resp = client.post(
        "/workflows/redblue",
        json={
            "feature_description": "API design",
            "security_focus": ["sql_injection", "xss", "csrf"],
        },
        headers=auth_headers(),
    )
    data = resp.json()
    attack_id = data["task_ids"]["attack"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    prompt = tasks[attack_id].get("prompt", "")
    assert "sql_injection" in prompt, "sql_injection not found in attack prompt"
    assert "xss" in prompt, "xss not found in attack prompt"
    assert "csrf" in prompt, "csrf not found in attack prompt"


def test_redblue_implement_prompt_mentions_blue_team_role(client):
    """Implement prompt should mention BLUE-TEAM role."""
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": "API design"},
        headers=auth_headers(),
    )
    data = resp.json()
    implement_id = data["task_ids"]["implement"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    prompt = tasks[implement_id].get("prompt", "")
    assert "BLUE-TEAM" in prompt or "blue-team" in prompt.lower(), (
        "implement prompt should mention BLUE-TEAM role"
    )


def test_redblue_attack_prompt_mentions_red_team_role(client):
    """Attack prompt should mention RED-TEAM role."""
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": "API design"},
        headers=auth_headers(),
    )
    data = resp.json()
    attack_id = data["task_ids"]["attack"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    prompt = tasks[attack_id].get("prompt", "")
    assert "RED-TEAM" in prompt or "red-team" in prompt.lower(), (
        "attack prompt should mention RED-TEAM role"
    )


def test_redblue_assess_prompt_mentions_arbiter_role(client):
    """Assess prompt should mention ARBITER role."""
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": "API design"},
        headers=auth_headers(),
    )
    data = resp.json()
    assess_id = data["task_ids"]["assess"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    prompt = tasks[assess_id].get("prompt", "")
    assert "ARBITER" in prompt or "arbiter" in prompt.lower(), (
        "assess prompt should mention ARBITER role"
    )


def test_redblue_scratchpad_keys_in_prompts(client):
    """Scratchpad keys implementation/vulnerabilities/risk_report should be in prompts."""
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": "API design"},
        headers=auth_headers(),
    )
    data = resp.json()
    prefix = data["scratchpad_prefix"]
    task_ids = data["task_ids"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    implement_prompt = tasks[task_ids["implement"]].get("prompt", "")
    attack_prompt = tasks[task_ids["attack"]].get("prompt", "")
    assess_prompt = tasks[task_ids["assess"]].get("prompt", "")

    assert f"{prefix}_implementation" in implement_prompt, (
        "implementation key missing from implement prompt"
    )
    assert f"{prefix}_implementation" in attack_prompt, (
        "implementation key missing from attack prompt (needed for reading)"
    )
    assert f"{prefix}_vulnerabilities" in attack_prompt, (
        "vulnerabilities key missing from attack prompt"
    )
    assert f"{prefix}_implementation" in assess_prompt, (
        "implementation key missing from assess prompt (needed for reading)"
    )
    assert f"{prefix}_vulnerabilities" in assess_prompt, (
        "vulnerabilities key missing from assess prompt (needed for reading)"
    )
    assert f"{prefix}_risk_report" in assess_prompt, (
        "risk_report key missing from assess prompt"
    )


def test_redblue_assess_prompt_mentions_risk_levels(client):
    """Assess (arbiter) prompt should mention risk level values."""
    resp = client.post(
        "/workflows/redblue",
        json={"feature_description": "API design"},
        headers=auth_headers(),
    )
    data = resp.json()
    assess_id = data["task_ids"]["assess"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    prompt = tasks[assess_id].get("prompt", "")
    # The assess prompt must instruct the arbiter to produce a risk level
    assert any(level in prompt for level in ["LOW", "MEDIUM", "HIGH", "CRITICAL"]), (
        "assess prompt should mention risk levels LOW/MEDIUM/HIGH/CRITICAL"
    )
