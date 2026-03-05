"""Tests for POST /workflows/debate — 3-agent Advocate/Critic/Judge debate workflow.

The Debate workflow automatically builds a Workflow DAG for multi-round structured
argumentation:

  Round 1:  step_1 (advocate) → step_2 (critic, depends_on step_1)
  Round 2:  step_3 (advocate_r2, depends_on step_2) → step_4 (critic_r2, depends_on step_3)
  Final:    step_N (judge, depends_on last_critic)

Key design principles (Du et al. ICML 2024, DEBATE ACL 2024, ChatEval ICLR 2024):
- Role diversity: advocate/critic/judge have distinct personas and instructions.
- Context isolation: each agent sees only what it needs (own role + prior round).
- Scratchpad as Blackboard: artifacts stored at debate/{run_id}/round_{n}/{role}.
- Termination: judge synthesizes after max_rounds; max_rounds defaults to 2.

Design references:
- Du et al. "Improving Factuality and Reasoning in Language Models through
  Multiagent Debate" ICML 2024 (arXiv:2305.14325)
- DEBATE: Devil's Advocate-Based Assessment and Text Evaluation, ACL 2024
  (arXiv:2405.09935): Commander + Scorer + Critic structure
- ChatEval: Towards Better LLM-based Evaluators through Multi-Agent Debate,
  ICLR 2024 (arXiv:2308.07201): role diversity is the most critical factor
- DESIGN.md §10.32 (v0.37.0)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

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
# POST /workflows/debate — basic happy path
# ---------------------------------------------------------------------------


def test_debate_workflow_returns_workflow_id(client):
    """POST /workflows/debate returns a workflow_id."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "SQLite vs PostgreSQL"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "workflow_id" in body


def test_debate_workflow_returns_task_ids(client):
    """POST /workflows/debate returns a non-empty task_ids dict."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "REST vs GraphQL"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "task_ids" in body
    assert len(body["task_ids"]) > 0


def test_debate_workflow_default_max_rounds_is_2(client):
    """Default max_rounds=2 produces 5 tasks: advocate×2 + critic×2 + judge×1."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "monolith vs microservices"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    # 2 rounds × 2 agents (advocate + critic) + 1 judge = 5 tasks
    assert len(body["task_ids"]) == 5


def test_debate_workflow_max_rounds_1(client):
    """max_rounds=1 produces 3 tasks: advocate + critic + judge."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "tabs vs spaces", "max_rounds": 1},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    # 1 round × 2 agents + 1 judge = 3 tasks
    assert len(body["task_ids"]) == 3


def test_debate_workflow_max_rounds_3(client):
    """max_rounds=3 produces 7 tasks: advocate×3 + critic×3 + judge×1."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "OOP vs functional programming", "max_rounds": 3},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    # 3 rounds × 2 + 1 judge = 7 tasks
    assert len(body["task_ids"]) == 7


def test_debate_workflow_name_contains_topic(client):
    """The workflow name includes the debate topic."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "event sourcing"},
        headers=auth_headers(),
    )
    body = resp.json()
    assert "event sourcing" in body["name"]


def test_debate_workflow_scratchpad_prefix_in_response(client):
    """Response includes scratchpad_prefix so agents can find artifacts."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "CI/CD strategies"},
        headers=auth_headers(),
    )
    body = resp.json()
    assert "scratchpad_prefix" in body
    assert body["scratchpad_prefix"]  # non-empty


def test_debate_workflow_scratchpad_prefix_unique_per_run(client):
    """Each debate workflow invocation gets a unique scratchpad_prefix."""
    resp1 = client.post(
        "/workflows/debate",
        json={"topic": "caching"},
        headers=auth_headers(),
    )
    resp2 = client.post(
        "/workflows/debate",
        json={"topic": "caching"},
        headers=auth_headers(),
    )
    prefix1 = resp1.json()["scratchpad_prefix"]
    prefix2 = resp2.json()["scratchpad_prefix"]
    assert prefix1 != prefix2


# ---------------------------------------------------------------------------
# Workflow DAG structure: dependency ordering
# ---------------------------------------------------------------------------


def test_debate_workflow_tasks_are_submitted(client):
    """All debate tasks appear in the orchestrator task list."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "Docker vs VMs"},
        headers=auth_headers(),
    )
    body = resp.json()
    task_ids = list(body["task_ids"].values())

    tasks_resp = client.get("/tasks", headers=auth_headers())
    assert tasks_resp.status_code == 200
    all_task_ids = [t["task_id"] for t in tasks_resp.json()]
    for tid in task_ids:
        assert tid in all_task_ids


def test_debate_workflow_critic_round1_depends_on_advocate_round1(client):
    """Critic (round 1) depends on advocate (round 1)."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "serverless vs containers"},
        headers=auth_headers(),
    )
    body = resp.json()
    advocate_1_id = body["task_ids"]["advocate_r1"]
    critic_1_id = body["task_ids"]["critic_r1"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    assert advocate_1_id in tasks[critic_1_id]["depends_on"]


def test_debate_workflow_advocate_round2_depends_on_critic_round1(client):
    """Advocate (round 2) depends on critic (round 1) — advocate reads critic's rebuttal."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "NoSQL vs SQL"},
        headers=auth_headers(),
    )
    body = resp.json()
    critic_1_id = body["task_ids"]["critic_r1"]
    advocate_2_id = body["task_ids"]["advocate_r2"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    assert critic_1_id in tasks[advocate_2_id]["depends_on"]


def test_debate_workflow_judge_depends_on_last_critic(client):
    """Judge task depends on the last critic round."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "async vs sync"},
        headers=auth_headers(),
    )
    body = resp.json()
    # Default max_rounds=2 → last critic is critic_r2
    critic_2_id = body["task_ids"]["critic_r2"]
    judge_id = body["task_ids"]["judge"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    assert critic_2_id in tasks[judge_id]["depends_on"]


def test_debate_workflow_max_rounds_1_judge_depends_on_critic_r1(client):
    """With max_rounds=1, judge depends on critic_r1."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "Python vs Go", "max_rounds": 1},
        headers=auth_headers(),
    )
    body = resp.json()
    critic_1_id = body["task_ids"]["critic_r1"]
    judge_id = body["task_ids"]["judge"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    assert critic_1_id in tasks[judge_id]["depends_on"]


# ---------------------------------------------------------------------------
# Prompt content
# ---------------------------------------------------------------------------


def test_debate_workflow_advocate_prompt_mentions_topic(client):
    """Advocate round-1 prompt mentions the debate topic."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "event-driven architecture"},
        headers=auth_headers(),
    )
    body = resp.json()
    advocate_id = body["task_ids"]["advocate_r1"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    prompt = tasks[advocate_id]["prompt"]
    assert "event-driven architecture" in prompt


def test_debate_workflow_critic_prompt_mentions_scratchpad(client):
    """Critic prompt references scratchpad so it can read advocate's argument."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "DDD vs CRUD"},
        headers=auth_headers(),
    )
    body = resp.json()
    critic_id = body["task_ids"]["critic_r1"]
    prefix = body["scratchpad_prefix"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    prompt = tasks[critic_id]["prompt"]
    assert prefix in prompt or "scratchpad" in prompt.lower()


def test_debate_workflow_judge_prompt_mentions_decision(client):
    """Judge prompt instructs the agent to write a decision."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "REST vs gRPC"},
        headers=auth_headers(),
    )
    body = resp.json()
    judge_id = body["task_ids"]["judge"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    prompt = tasks[judge_id]["prompt"]
    assert "decision" in prompt.lower() or "DECISION" in prompt


def test_debate_workflow_advocate_role_name_in_prompt(client):
    """Advocate prompt identifies the agent as ADVOCATE."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "cloud-native vs on-prem"},
        headers=auth_headers(),
    )
    body = resp.json()
    advocate_id = body["task_ids"]["advocate_r1"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    prompt = tasks[advocate_id]["prompt"].upper()
    assert "ADVOCATE" in prompt


def test_debate_workflow_critic_role_name_in_prompt(client):
    """Critic prompt identifies the agent as CRITIC or DEVIL'S ADVOCATE."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "CQRS vs simple CRUD"},
        headers=auth_headers(),
    )
    body = resp.json()
    critic_id = body["task_ids"]["critic_r1"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    prompt = tasks[critic_id]["prompt"].upper()
    assert "CRITIC" in prompt or "DEVIL" in prompt


def test_debate_workflow_judge_role_name_in_prompt(client):
    """Judge prompt identifies the agent as JUDGE."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "stream processing vs batch"},
        headers=auth_headers(),
    )
    body = resp.json()
    judge_id = body["task_ids"]["judge"]

    tasks_resp = client.get("/tasks", headers=auth_headers())
    tasks = {t["task_id"]: t for t in tasks_resp.json()}
    prompt = tasks[judge_id]["prompt"].upper()
    assert "JUDGE" in prompt


# ---------------------------------------------------------------------------
# Optional fields: required_tags, reply_to
# ---------------------------------------------------------------------------


def test_debate_workflow_with_required_tags(client):
    """POST /workflows/debate accepts required_tags for each role."""
    resp = client.post(
        "/workflows/debate",
        json={
            "topic": "type safety",
            "advocate_tags": ["debate-advocate"],
            "critic_tags": ["debate-critic"],
            "judge_tags": ["debate-judge"],
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200


def test_debate_workflow_with_reply_to(client):
    """POST /workflows/debate accepts reply_to to route the judge's result."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "agile vs waterfall", "reply_to": "director-agent"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


def test_debate_workflow_requires_auth(client):
    """POST /workflows/debate returns 401 without auth header."""
    resp = client.post("/workflows/debate", json={"topic": "test"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_debate_workflow_missing_topic_returns_422(client):
    """POST /workflows/debate without 'topic' field returns 422."""
    resp = client.post(
        "/workflows/debate",
        json={"max_rounds": 2},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_debate_workflow_empty_topic_returns_422(client):
    """POST /workflows/debate with empty topic string returns 422."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": ""},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_debate_workflow_max_rounds_too_low_returns_422(client):
    """POST /workflows/debate with max_rounds=0 returns 422."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "networking", "max_rounds": 0},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_debate_workflow_max_rounds_too_high_returns_422(client):
    """POST /workflows/debate with max_rounds=4 returns 422 (max is 3)."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "networking", "max_rounds": 4},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Workflow tracking: GET /workflows shows the debate workflow
# ---------------------------------------------------------------------------


def test_debate_workflow_appears_in_list(client):
    """After POST /workflows/debate, GET /workflows shows the new run."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "authentication strategies"},
        headers=auth_headers(),
    )
    wf_id = resp.json()["workflow_id"]

    list_resp = client.get("/workflows", headers=auth_headers())
    assert list_resp.status_code == 200
    runs = list_resp.json()
    wf_ids = [r["id"] for r in runs]
    assert wf_id in wf_ids


def test_debate_workflow_get_by_id_default_rounds(client):
    """GET /workflows/{id} returns the debate workflow run with 5 tasks (2 rounds)."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "containerization"},
        headers=auth_headers(),
    )
    wf_id = resp.json()["workflow_id"]

    get_resp = client.get(f"/workflows/{wf_id}", headers=auth_headers())
    assert get_resp.status_code == 200
    run = get_resp.json()
    assert run["id"] == wf_id
    assert run["tasks_total"] == 5  # 2 rounds × 2 + 1 judge


def test_debate_workflow_name_in_workflow_list(client):
    """The debate workflow's name contains 'debate' and the topic."""
    resp = client.post(
        "/workflows/debate",
        json={"topic": "observability"},
        headers=auth_headers(),
    )
    wf_id = resp.json()["workflow_id"]

    get_resp = client.get(f"/workflows/{wf_id}", headers=auth_headers())
    run = get_resp.json()
    assert "observability" in run["name"]
