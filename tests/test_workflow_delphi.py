"""Tests for POST /workflows/delphi — multi-round Delphi consensus workflow.

The Delphi workflow builds a DAG where, for each round, expert agents submit
opinions in parallel, followed by a moderator that synthesises them.
After all rounds, a consensus agent produces the final agreement document.

DAG structure (3 experts, 2 rounds):

  Round 1:  expert_security_r1  ─┐
            expert_perf_r1      ─┼─→ moderator_r1 ─┐
            expert_maint_r1     ─┘                  │
  Round 2:  expert_security_r2  ─┐  (depends_on mod_r1)
            expert_perf_r2      ─┼─→ moderator_r2 ─→ consensus
            expert_maint_r2     ─┘

Design references:
- DelphiAgent (ScienceDirect 2025): multiple LLM agents emulate Delphi method.
- RT-AID (ScienceDirect 2025): AI-assisted opinions accelerate convergence.
- Du et al. ICML 2024 (arXiv:2305.14325): multi-round debate converges to
  correct answer even when all agents are initially wrong.
- CONSENSAGENT ACL 2025: sycophancy-mitigation in multi-agent consensus.
- DESIGN.md §10.22 (v1.0.23)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app, DelphiWorkflowSubmit
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

_DEFAULT_EXPERTS = ["security", "performance", "maintainability"]


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
# DelphiWorkflowSubmit — model validation
# ---------------------------------------------------------------------------


def test_delphi_submit_empty_topic_rejected():
    """Empty topic should raise ValueError."""
    with pytest.raises(Exception):
        DelphiWorkflowSubmit(topic="")


def test_delphi_submit_whitespace_topic_rejected():
    """Whitespace-only topic should raise ValueError."""
    with pytest.raises(Exception):
        DelphiWorkflowSubmit(topic="   ")


def test_delphi_submit_too_few_experts_rejected():
    """Less than 2 experts should raise ValueError."""
    with pytest.raises(Exception):
        DelphiWorkflowSubmit(topic="API design", experts=["security"])


def test_delphi_submit_too_many_experts_rejected():
    """More than 5 experts should raise ValueError."""
    with pytest.raises(Exception):
        DelphiWorkflowSubmit(
            topic="API design",
            experts=["a", "b", "c", "d", "e", "f"],
        )


def test_delphi_submit_max_rounds_zero_rejected():
    """max_rounds=0 should raise ValueError."""
    with pytest.raises(Exception):
        DelphiWorkflowSubmit(topic="API design", max_rounds=0)


def test_delphi_submit_max_rounds_four_rejected():
    """max_rounds=4 should raise ValueError."""
    with pytest.raises(Exception):
        DelphiWorkflowSubmit(topic="API design", max_rounds=4)


def test_delphi_submit_empty_expert_name_rejected():
    """Empty string in experts list should raise ValueError."""
    with pytest.raises(Exception):
        DelphiWorkflowSubmit(topic="API design", experts=["security", ""])


def test_delphi_submit_valid_defaults():
    """Default DelphiWorkflowSubmit should be valid."""
    obj = DelphiWorkflowSubmit(topic="database choice")
    assert obj.topic == "database choice"
    assert len(obj.experts) == 3
    assert obj.max_rounds == 2


def test_delphi_submit_two_experts_valid():
    """2 experts (minimum) should be valid."""
    obj = DelphiWorkflowSubmit(topic="caching strategy", experts=["redis", "memcached"])
    assert len(obj.experts) == 2


def test_delphi_submit_five_experts_valid():
    """5 experts (maximum) should be valid."""
    obj = DelphiWorkflowSubmit(
        topic="cloud provider",
        experts=["security", "cost", "performance", "ux", "maintainability"],
    )
    assert len(obj.experts) == 5


def test_delphi_submit_duplicate_expert_names_rejected():
    """Duplicate expert names should raise ValueError."""
    with pytest.raises(Exception):
        DelphiWorkflowSubmit(topic="API design", experts=["security", "security", "performance"])


def test_delphi_submit_expert_with_slash_rejected():
    """Expert name with slash should raise ValueError (breaks scratchpad URL)."""
    with pytest.raises(Exception):
        DelphiWorkflowSubmit(topic="API design", experts=["security/compliance", "performance"])


def test_delphi_submit_expert_with_space_rejected():
    """Expert name with space should raise ValueError (only alnum/hyphen/underscore allowed)."""
    with pytest.raises(Exception):
        DelphiWorkflowSubmit(topic="API design", experts=["security expert", "performance"])


def test_delphi_submit_expert_with_leading_whitespace_rejected():
    """Expert name with leading whitespace should raise ValueError."""
    with pytest.raises(Exception):
        DelphiWorkflowSubmit(topic="API design", experts=[" security", "performance"])


def test_delphi_submit_expert_with_hyphen_and_underscore_valid():
    """Expert names with hyphens and underscores should be valid."""
    obj = DelphiWorkflowSubmit(
        topic="cloud provider",
        experts=["security-compliance", "performance_ops"],
    )
    assert len(obj.experts) == 2


def test_delphi_task_count_3_rounds_experts_r3_depends_on_mod_r2(client):
    """In 3-round Delphi: expert round 3 tasks depend on moderator round 2."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "API design", "max_rounds": 3, "experts": _DEFAULT_EXPERTS},
        headers=auth_headers(),
    )
    body = resp.json()
    mod_r2_id = body["task_ids"]["moderator_r2"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    for persona in _DEFAULT_EXPERTS:
        exp_r3_id = body["task_ids"][f"expert_{persona}_r3"]
        assert mod_r2_id in tasks[exp_r3_id]["depends_on"], (
            f"expert_{persona}_r3 should depend on moderator_r2"
        )


def test_delphi_workflow_expert_tags_propagated(client):
    """expert_tags should appear as required_tags on expert tasks."""
    resp = client.post(
        "/workflows/delphi",
        json={
            "topic": "storage",
            "max_rounds": 1,
            "experts": _DEFAULT_EXPERTS,
            "expert_tags": ["gpu", "expert"],
        },
        headers=auth_headers(),
    )
    body = resp.json()

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    for persona in _DEFAULT_EXPERTS:
        exp_id = body["task_ids"][f"expert_{persona}_r1"]
        task_tags = tasks[exp_id].get("required_tags", [])
        assert "gpu" in task_tags and "expert" in task_tags, (
            f"expert_{persona}_r1 should have required_tags=['gpu','expert'], got {task_tags}"
        )


# ---------------------------------------------------------------------------
# POST /workflows/delphi — basic happy path
# ---------------------------------------------------------------------------


def test_delphi_workflow_returns_200(client):
    """POST /workflows/delphi returns HTTP 200."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "REST vs GraphQL"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200


def test_delphi_workflow_returns_workflow_id(client):
    """POST /workflows/delphi returns a workflow_id."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "SQLite vs PostgreSQL"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "workflow_id" in body
    assert body["workflow_id"]


def test_delphi_workflow_returns_task_ids(client):
    """POST /workflows/delphi returns a non-empty task_ids dict."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "monolith vs microservices"},
        headers=auth_headers(),
    )
    body = resp.json()
    assert "task_ids" in body
    assert len(body["task_ids"]) > 0


def test_delphi_workflow_name_contains_topic(client):
    """Workflow name includes the delphi topic."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "event sourcing"},
        headers=auth_headers(),
    )
    body = resp.json()
    assert "event sourcing" in body["name"]


def test_delphi_workflow_scratchpad_prefix_in_response(client):
    """Response includes a non-empty scratchpad_prefix."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "CI/CD strategies"},
        headers=auth_headers(),
    )
    body = resp.json()
    assert "scratchpad_prefix" in body
    assert body["scratchpad_prefix"]


def test_delphi_workflow_scratchpad_prefix_unique_per_run(client):
    """Each delphi workflow invocation gets a unique scratchpad_prefix."""
    resp1 = client.post(
        "/workflows/delphi",
        json={"topic": "caching"},
        headers=auth_headers(),
    )
    resp2 = client.post(
        "/workflows/delphi",
        json={"topic": "caching"},
        headers=auth_headers(),
    )
    prefix1 = resp1.json()["scratchpad_prefix"]
    prefix2 = resp2.json()["scratchpad_prefix"]
    assert prefix1 != prefix2


def test_delphi_workflow_scratchpad_prefix_starts_with_delphi(client):
    """Scratchpad prefix should start with 'delphi_'."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "storage backends"},
        headers=auth_headers(),
    )
    body = resp.json()
    assert body["scratchpad_prefix"].startswith("delphi_")


# ---------------------------------------------------------------------------
# Task count: 3 experts, varying rounds
# ---------------------------------------------------------------------------


def test_delphi_task_count_1_round_3_experts(client):
    """1 round × 3 experts + 1 moderator + 1 consensus = 5 tasks."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "cloud storage", "max_rounds": 1, "experts": _DEFAULT_EXPERTS},
        headers=auth_headers(),
    )
    body = resp.json()
    # 3 experts × 1 round + 1 moderator × 1 round + 1 consensus = 5
    assert len(body["task_ids"]) == 5


def test_delphi_task_count_2_rounds_3_experts(client):
    """2 rounds × 3 experts + 2 moderators + 1 consensus = 9 tasks."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "database choice", "max_rounds": 2, "experts": _DEFAULT_EXPERTS},
        headers=auth_headers(),
    )
    body = resp.json()
    # 3 experts × 2 rounds + 2 moderators + 1 consensus = 9
    assert len(body["task_ids"]) == 9


def test_delphi_task_count_3_rounds_3_experts(client):
    """3 rounds × 3 experts + 3 moderators + 1 consensus = 13 tasks."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "API design", "max_rounds": 3, "experts": _DEFAULT_EXPERTS},
        headers=auth_headers(),
    )
    body = resp.json()
    # 3 experts × 3 rounds + 3 moderators + 1 consensus = 13
    assert len(body["task_ids"]) == 13


def test_delphi_task_count_1_round_2_experts(client):
    """1 round × 2 experts + 1 moderator + 1 consensus = 4 tasks."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "redis vs memcached", "max_rounds": 1, "experts": ["redis", "memcached"]},
        headers=auth_headers(),
    )
    body = resp.json()
    # 2 experts × 1 round + 1 moderator + 1 consensus = 4
    assert len(body["task_ids"]) == 4


def test_delphi_task_count_2_rounds_5_experts(client):
    """2 rounds × 5 experts + 2 moderators + 1 consensus = 13 tasks."""
    experts = ["security", "cost", "performance", "ux", "maintainability"]
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "cloud provider", "max_rounds": 2, "experts": experts},
        headers=auth_headers(),
    )
    body = resp.json()
    # 5 experts × 2 rounds + 2 moderators + 1 consensus = 13
    assert len(body["task_ids"]) == 13


# ---------------------------------------------------------------------------
# Task key naming
# ---------------------------------------------------------------------------


def test_delphi_task_ids_contain_expert_round_keys(client):
    """task_ids dict should have keys like expert_{persona}_r{n}."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "storage", "max_rounds": 1, "experts": _DEFAULT_EXPERTS},
        headers=auth_headers(),
    )
    body = resp.json()
    task_ids = body["task_ids"]
    for persona in _DEFAULT_EXPERTS:
        assert f"expert_{persona}_r1" in task_ids


def test_delphi_task_ids_contain_moderator_keys(client):
    """task_ids dict should have keys like moderator_r{n}."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "storage", "max_rounds": 2, "experts": _DEFAULT_EXPERTS},
        headers=auth_headers(),
    )
    body = resp.json()
    task_ids = body["task_ids"]
    assert "moderator_r1" in task_ids
    assert "moderator_r2" in task_ids


def test_delphi_task_ids_contain_consensus_key(client):
    """task_ids dict should always have a 'consensus' key."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "storage"},
        headers=auth_headers(),
    )
    body = resp.json()
    assert "consensus" in body["task_ids"]


# ---------------------------------------------------------------------------
# DAG dependency structure
# ---------------------------------------------------------------------------


def test_delphi_moderator_r1_depends_on_all_experts_r1(client):
    """Moderator round 1 must depend on ALL expert round 1 tasks."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "storage", "max_rounds": 1, "experts": _DEFAULT_EXPERTS},
        headers=auth_headers(),
    )
    body = resp.json()
    mod_r1_id = body["task_ids"]["moderator_r1"]
    expert_ids_r1 = [body["task_ids"][f"expert_{p}_r1"] for p in _DEFAULT_EXPERTS]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    mod_depends = tasks[mod_r1_id]["depends_on"]
    for eid in expert_ids_r1:
        assert eid in mod_depends, f"moderator_r1 should depend on expert task {eid}"


def test_delphi_experts_r2_depend_on_moderator_r1(client):
    """Expert round 2 tasks must each depend on moderator round 1."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "messaging", "max_rounds": 2, "experts": _DEFAULT_EXPERTS},
        headers=auth_headers(),
    )
    body = resp.json()
    mod_r1_id = body["task_ids"]["moderator_r1"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    for persona in _DEFAULT_EXPERTS:
        exp_r2_id = body["task_ids"][f"expert_{persona}_r2"]
        assert mod_r1_id in tasks[exp_r2_id]["depends_on"], (
            f"expert_{persona}_r2 should depend on moderator_r1"
        )


def test_delphi_consensus_depends_on_last_moderator(client):
    """Consensus task must depend on the last moderator task."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "infra design", "max_rounds": 2, "experts": _DEFAULT_EXPERTS},
        headers=auth_headers(),
    )
    body = resp.json()
    mod_r2_id = body["task_ids"]["moderator_r2"]
    consensus_id = body["task_ids"]["consensus"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    assert mod_r2_id in tasks[consensus_id]["depends_on"]


def test_delphi_experts_r1_have_no_depends_on(client):
    """Expert round 1 tasks should have no dependencies (start in parallel)."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "container runtime", "max_rounds": 1, "experts": _DEFAULT_EXPERTS},
        headers=auth_headers(),
    )
    body = resp.json()

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}

    for persona in _DEFAULT_EXPERTS:
        exp_r1_id = body["task_ids"][f"expert_{persona}_r1"]
        # depends_on is omitted from the response when it's an empty list
        assert tasks[exp_r1_id].get("depends_on", []) == [], (
            f"expert_{persona}_r1 should have no dependencies"
        )


# ---------------------------------------------------------------------------
# All tasks appear in /tasks endpoint
# ---------------------------------------------------------------------------


def test_delphi_all_tasks_submitted_to_orchestrator(client):
    """All Delphi workflow tasks appear in the /tasks list."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "Docker vs VMs", "max_rounds": 1, "experts": _DEFAULT_EXPERTS},
        headers=auth_headers(),
    )
    body = resp.json()
    task_ids = list(body["task_ids"].values())

    tasks_resp = client.get("/tasks", headers=auth_headers())
    assert tasks_resp.status_code == 200
    all_task_ids = [t["task_id"] for t in tasks_resp.json()]
    for tid in task_ids:
        assert tid in all_task_ids


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_delphi_workflow_requires_auth(client):
    """POST /workflows/delphi returns 401 without API key."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "security"},
    )
    assert resp.status_code == 401


def test_delphi_workflow_wrong_key_rejected(client):
    """POST /workflows/delphi returns 401 with wrong API key."""
    resp = client.post(
        "/workflows/delphi",
        json={"topic": "security"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# reply_to propagation
# ---------------------------------------------------------------------------


def test_delphi_reply_to_accepted_by_endpoint(client):
    """POST /workflows/delphi accepts reply_to to route the consensus result."""
    resp = client.post(
        "/workflows/delphi",
        json={
            "topic": "architecture",
            "max_rounds": 1,
            "experts": _DEFAULT_EXPERTS,
            "reply_to": "director-agent",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    # Ensure we still get a workflow_id and consensus task
    assert "workflow_id" in body
    assert "consensus" in body["task_ids"]
