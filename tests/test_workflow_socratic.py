"""Tests for POST /workflows/socratic — Socratic dialogue workflow.

The Socratic workflow builds a strictly sequential 3-agent DAG:

  questioner ──→ responder ──→ synthesizer

- questioner:  probes assumptions using the Maieutic method (Phase 1: adversarial
               questions; Phase 2: integrative questions); stores Q&A log to
               scratchpad ``{prefix}_dialogue``.
- responder:   reads the questioner log and refines/defends the position; appends
               answers to the dialogue log.
- synthesizer: reads the complete dialogue and extracts a structured
               ``synthesis.md`` with main arguments, agreed points, unresolved
               questions, and recommendations.

Design references:
- Liang et al. "SocraSynth" arXiv:2402.06634 (2024): staged questioner →
  responder → synthesizer with sycophancy suppression.
- "KELE: Knowledge-Enhanced LLM for Socratic Teaching" arXiv:2409.05511
  EMNLP 2025: two-phase questioning (adversarial → constructive).
- "CONSENSAGENT" ACL 2025: dynamic prompt refinement reduces sycophancy.
- DESIGN.md §10.24 (v1.0.25)
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app, SocraticWorkflowSubmit
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
# SocraticWorkflowSubmit — model validation
# ---------------------------------------------------------------------------


def test_socratic_submit_empty_topic_rejected():
    """Empty topic should raise ValueError."""
    with pytest.raises(Exception):
        SocraticWorkflowSubmit(topic="")


def test_socratic_submit_whitespace_topic_rejected():
    """Whitespace-only topic should raise ValueError."""
    with pytest.raises(Exception):
        SocraticWorkflowSubmit(topic="   ")


def test_socratic_submit_valid_minimal():
    """Minimal valid request should construct with defaults."""
    obj = SocraticWorkflowSubmit(topic="REST vs GraphQL for mobile API")
    assert obj.topic == "REST vs GraphQL for mobile API"
    assert obj.questioner_tags == []
    assert obj.responder_tags == []
    assert obj.synthesizer_tags == []
    assert obj.reply_to is None


def test_socratic_submit_with_tags():
    """Tags should be accepted on all three roles."""
    obj = SocraticWorkflowSubmit(
        topic="monolith vs microservices",
        questioner_tags=["critic"],
        responder_tags=["architect"],
        synthesizer_tags=["senior"],
    )
    assert obj.questioner_tags == ["critic"]
    assert obj.responder_tags == ["architect"]
    assert obj.synthesizer_tags == ["senior"]


def test_socratic_submit_with_reply_to():
    """reply_to field should be accepted."""
    obj = SocraticWorkflowSubmit(topic="API design trade-offs", reply_to="director-1")
    assert obj.reply_to == "director-1"


# ---------------------------------------------------------------------------
# POST /workflows/socratic — HTTP responses
# ---------------------------------------------------------------------------


def test_socratic_workflow_requires_auth(client):
    """Endpoint should return 401 without API key."""
    resp = client.post(
        "/workflows/socratic",
        json={"topic": "REST vs GraphQL"},
    )
    assert resp.status_code == 401


def test_socratic_workflow_wrong_api_key(client):
    """Endpoint should return 401 with wrong API key."""
    resp = client.post(
        "/workflows/socratic",
        json={"topic": "REST vs GraphQL"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_socratic_workflow_empty_topic_returns_422(client):
    """Empty topic should return 422 Unprocessable Entity."""
    resp = client.post(
        "/workflows/socratic",
        json={"topic": ""},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_socratic_workflow_missing_topic_returns_422(client):
    """Missing topic field should return 422."""
    resp = client.post(
        "/workflows/socratic",
        json={},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_socratic_workflow_returns_200(client):
    """Valid request should return 200."""
    resp = client.post(
        "/workflows/socratic",
        json={"topic": "REST vs GraphQL for mobile API"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /workflows/socratic — response structure
# ---------------------------------------------------------------------------


def test_socratic_workflow_response_fields(client):
    """Response must contain workflow_id, name, task_ids, scratchpad_prefix."""
    resp = client.post(
        "/workflows/socratic",
        json={"topic": "synchronous vs asynchronous APIs"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "workflow_id" in data
    assert "name" in data
    assert "task_ids" in data
    assert "scratchpad_prefix" in data


def test_socratic_workflow_name_format(client):
    """Workflow name should be 'socratic/<topic>'."""
    resp = client.post(
        "/workflows/socratic",
        json={"topic": "REST vs GraphQL"},
        headers=auth_headers(),
    )
    data = resp.json()
    assert data["name"] == "socratic/REST vs GraphQL"


def test_socratic_workflow_task_count(client):
    """Workflow must create exactly 3 tasks: questioner, responder, synthesizer."""
    resp = client.post(
        "/workflows/socratic",
        json={"topic": "monolith vs microservices"},
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]
    assert len(task_ids) == 3
    assert "questioner" in task_ids
    assert "responder" in task_ids
    assert "synthesizer" in task_ids


def test_socratic_workflow_task_ids_are_strings(client):
    """All task IDs should be non-empty strings."""
    resp = client.post(
        "/workflows/socratic",
        json={"topic": "type inference trade-offs"},
        headers=auth_headers(),
    )
    data = resp.json()
    for role, tid in data["task_ids"].items():
        assert isinstance(tid, str), f"task_id for {role} is not a string"
        assert len(tid) > 0, f"task_id for {role} is empty"


def test_socratic_workflow_task_ids_distinct(client):
    """All 3 task IDs must be distinct."""
    resp = client.post(
        "/workflows/socratic",
        json={"topic": "caching strategy trade-offs"},
        headers=auth_headers(),
    )
    data = resp.json()
    ids = list(data["task_ids"].values())
    assert len(ids) == len(set(ids)), "task IDs are not distinct"


# ---------------------------------------------------------------------------
# Scratchpad key naming
# ---------------------------------------------------------------------------


def test_socratic_scratchpad_prefix_format(client):
    """Scratchpad prefix should match 'socratic_XXXXXXXX' pattern."""
    resp = client.post(
        "/workflows/socratic",
        json={"topic": "API design"},
        headers=auth_headers(),
    )
    data = resp.json()
    assert re.match(r"^socratic_[0-9a-f]{8}$", data["scratchpad_prefix"]), (
        f"unexpected prefix: {data['scratchpad_prefix']}"
    )


def test_socratic_scratchpad_prefix_unique_across_runs(client):
    """Two workflow submissions should produce distinct scratchpad prefixes."""
    resp1 = client.post(
        "/workflows/socratic",
        json={"topic": "topic A"},
        headers=auth_headers(),
    )
    resp2 = client.post(
        "/workflows/socratic",
        json={"topic": "topic B"},
        headers=auth_headers(),
    )
    assert resp1.json()["scratchpad_prefix"] != resp2.json()["scratchpad_prefix"]


# ---------------------------------------------------------------------------
# Task dependency chain
# ---------------------------------------------------------------------------


def test_socratic_questioner_has_no_dependencies(client):
    """questioner task should have no depends_on."""
    resp = client.post(
        "/workflows/socratic",
        json={"topic": "sync vs async"},
        headers=auth_headers(),
    )
    data = resp.json()
    questioner_id = data["task_ids"]["questioner"]

    # Fetch the task from the orchestrator via the REST API
    task_resp = client.get(f"/tasks/{questioner_id}", headers=auth_headers())
    if task_resp.status_code == 200:
        task_data = task_resp.json()
        assert task_data.get("depends_on", []) == []


def test_socratic_responder_depends_on_questioner(client):
    """responder task should depend on questioner task."""
    resp = client.post(
        "/workflows/socratic",
        json={"topic": "sync vs async"},
        headers=auth_headers(),
    )
    data = resp.json()
    questioner_id = data["task_ids"]["questioner"]
    responder_id = data["task_ids"]["responder"]

    task_resp = client.get(f"/tasks/{responder_id}", headers=auth_headers())
    if task_resp.status_code == 200:
        task_data = task_resp.json()
        assert questioner_id in task_data.get("depends_on", [])


def test_socratic_synthesizer_depends_on_responder(client):
    """synthesizer task should depend on responder task."""
    resp = client.post(
        "/workflows/socratic",
        json={"topic": "sync vs async"},
        headers=auth_headers(),
    )
    data = resp.json()
    responder_id = data["task_ids"]["responder"]
    synthesizer_id = data["task_ids"]["synthesizer"]

    task_resp = client.get(f"/tasks/{synthesizer_id}", headers=auth_headers())
    if task_resp.status_code == 200:
        task_data = task_resp.json()
        assert responder_id in task_data.get("depends_on", [])


# ---------------------------------------------------------------------------
# reply_to propagation
# ---------------------------------------------------------------------------


def test_socratic_reply_to_propagates_to_synthesizer(client):
    """reply_to should be forwarded to the synthesizer task (not questioner/responder)."""
    with patch.object(
        Orchestrator,
        "submit_task",
        new_callable=AsyncMock,
    ) as mock_submit:
        # Set up mock return values for three successive calls
        task_q = MagicMock()
        task_q.id = "task-q-id"
        task_r = MagicMock()
        task_r.id = "task-r-id"
        task_s = MagicMock()
        task_s.id = "task-s-id"
        mock_submit.side_effect = [task_q, task_r, task_s]

        app, orch = _make_app()
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.post(
                "/workflows/socratic",
                json={"topic": "REST vs GraphQL", "reply_to": "director-agent"},
                headers=auth_headers(),
            )

        assert resp.status_code == 200

        # Inspect call args for the synthesizer (3rd call)
        calls = mock_submit.call_args_list
        assert len(calls) == 3
        # synthesizer call has reply_to="director-agent"
        synthesizer_kwargs = calls[2].kwargs
        assert synthesizer_kwargs.get("reply_to") == "director-agent"
        # questioner call has no reply_to
        questioner_kwargs = calls[0].kwargs
        assert questioner_kwargs.get("reply_to") is None
        # responder call has no reply_to
        responder_kwargs = calls[1].kwargs
        assert responder_kwargs.get("reply_to") is None


# ---------------------------------------------------------------------------
# Tag routing
# ---------------------------------------------------------------------------


def test_socratic_tags_routed_to_correct_roles(client):
    """Each role's tags should be passed only to that role's task."""
    with patch.object(
        Orchestrator,
        "submit_task",
        new_callable=AsyncMock,
    ) as mock_submit:
        task_q = MagicMock()
        task_q.id = "task-q"
        task_r = MagicMock()
        task_r.id = "task-r"
        task_s = MagicMock()
        task_s.id = "task-s"
        mock_submit.side_effect = [task_q, task_r, task_s]

        app, orch = _make_app()
        with TestClient(app, raise_server_exceptions=True) as c:
            c.post(
                "/workflows/socratic",
                json={
                    "topic": "API design",
                    "questioner_tags": ["critic"],
                    "responder_tags": ["architect"],
                    "synthesizer_tags": ["senior"],
                },
                headers=auth_headers(),
            )

        calls = mock_submit.call_args_list
        assert calls[0].kwargs.get("required_tags") == ["critic"]
        assert calls[1].kwargs.get("required_tags") == ["architect"]
        assert calls[2].kwargs.get("required_tags") == ["senior"]


# ---------------------------------------------------------------------------
# Prompt content checks
# ---------------------------------------------------------------------------


def test_socratic_questioner_prompt_contains_topic(client):
    """questioner prompt must mention the topic."""
    topic = "REST vs GraphQL trade-offs"
    with patch.object(
        Orchestrator,
        "submit_task",
        new_callable=AsyncMock,
    ) as mock_submit:
        task_q = MagicMock()
        task_q.id = "q"
        task_r = MagicMock()
        task_r.id = "r"
        task_s = MagicMock()
        task_s.id = "s"
        mock_submit.side_effect = [task_q, task_r, task_s]

        app, _ = _make_app()
        with TestClient(app, raise_server_exceptions=True) as c:
            c.post(
                "/workflows/socratic",
                json={"topic": topic},
                headers=auth_headers(),
            )

        questioner_prompt = mock_submit.call_args_list[0].args[0]
        assert topic in questioner_prompt


def test_socratic_questioner_prompt_contains_dialogue_key(client):
    """questioner prompt must reference the dialogue scratchpad key."""
    with patch.object(
        Orchestrator,
        "submit_task",
        new_callable=AsyncMock,
    ) as mock_submit:
        task_q = MagicMock()
        task_q.id = "q"
        task_r = MagicMock()
        task_r.id = "r"
        task_s = MagicMock()
        task_s.id = "s"
        mock_submit.side_effect = [task_q, task_r, task_s]

        app, _ = _make_app()
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.post(
                "/workflows/socratic",
                json={"topic": "API design"},
                headers=auth_headers(),
            )

        prefix = resp.json()["scratchpad_prefix"]
        questioner_prompt = mock_submit.call_args_list[0].args[0]
        assert f"{prefix}_dialogue" in questioner_prompt


def test_socratic_responder_prompt_contains_dialogue_key(client):
    """responder prompt must reference the dialogue scratchpad key."""
    with patch.object(
        Orchestrator,
        "submit_task",
        new_callable=AsyncMock,
    ) as mock_submit:
        task_q = MagicMock()
        task_q.id = "q"
        task_r = MagicMock()
        task_r.id = "r"
        task_s = MagicMock()
        task_s.id = "s"
        mock_submit.side_effect = [task_q, task_r, task_s]

        app, _ = _make_app()
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.post(
                "/workflows/socratic",
                json={"topic": "API design"},
                headers=auth_headers(),
            )

        prefix = resp.json()["scratchpad_prefix"]
        responder_prompt = mock_submit.call_args_list[1].args[0]
        assert f"{prefix}_dialogue" in responder_prompt


def test_socratic_synthesizer_prompt_contains_synthesis_key(client):
    """synthesizer prompt must reference the synthesis scratchpad key."""
    with patch.object(
        Orchestrator,
        "submit_task",
        new_callable=AsyncMock,
    ) as mock_submit:
        task_q = MagicMock()
        task_q.id = "q"
        task_r = MagicMock()
        task_r.id = "r"
        task_s = MagicMock()
        task_s.id = "s"
        mock_submit.side_effect = [task_q, task_r, task_s]

        app, _ = _make_app()
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.post(
                "/workflows/socratic",
                json={"topic": "API design"},
                headers=auth_headers(),
            )

        prefix = resp.json()["scratchpad_prefix"]
        synthesizer_prompt = mock_submit.call_args_list[2].args[0]
        assert f"{prefix}_synthesis" in synthesizer_prompt


def test_socratic_synthesizer_prompt_contains_dialogue_key(client):
    """synthesizer prompt must reference the dialogue scratchpad key (to read it)."""
    with patch.object(
        Orchestrator,
        "submit_task",
        new_callable=AsyncMock,
    ) as mock_submit:
        task_q = MagicMock()
        task_q.id = "q"
        task_r = MagicMock()
        task_r.id = "r"
        task_s = MagicMock()
        task_s.id = "s"
        mock_submit.side_effect = [task_q, task_r, task_s]

        app, _ = _make_app()
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.post(
                "/workflows/socratic",
                json={"topic": "API design"},
                headers=auth_headers(),
            )

        prefix = resp.json()["scratchpad_prefix"]
        synthesizer_prompt = mock_submit.call_args_list[2].args[0]
        assert f"{prefix}_dialogue" in synthesizer_prompt


def test_socratic_questioner_prompt_mentions_maieutic_method(client):
    """questioner prompt must mention the Maieutic or Socratic method."""
    with patch.object(
        Orchestrator,
        "submit_task",
        new_callable=AsyncMock,
    ) as mock_submit:
        task_q = MagicMock()
        task_q.id = "q"
        task_r = MagicMock()
        task_r.id = "r"
        task_s = MagicMock()
        task_s.id = "s"
        mock_submit.side_effect = [task_q, task_r, task_s]

        app, _ = _make_app()
        with TestClient(app, raise_server_exceptions=True) as c:
            c.post(
                "/workflows/socratic",
                json={"topic": "API design"},
                headers=auth_headers(),
            )

        questioner_prompt = mock_submit.call_args_list[0].args[0]
        assert any(
            word in questioner_prompt.lower()
            for word in ("maieutic", "socratic", "questioner")
        )


def test_socratic_synthesizer_prompt_mentions_synthesis_sections(client):
    """synthesizer prompt must list the required synthesis.md sections."""
    with patch.object(
        Orchestrator,
        "submit_task",
        new_callable=AsyncMock,
    ) as mock_submit:
        task_q = MagicMock()
        task_q.id = "q"
        task_r = MagicMock()
        task_r.id = "r"
        task_s = MagicMock()
        task_s.id = "s"
        mock_submit.side_effect = [task_q, task_r, task_s]

        app, _ = _make_app()
        with TestClient(app, raise_server_exceptions=True) as c:
            c.post(
                "/workflows/socratic",
                json={"topic": "API design"},
                headers=auth_headers(),
            )

        synthesizer_prompt = mock_submit.call_args_list[2].args[0]
        # Check that the synthesis.md template structure is mentioned
        assert "synthesis.md" in synthesizer_prompt
        assert "Recommendations" in synthesizer_prompt
