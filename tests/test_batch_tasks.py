"""Tests for POST /tasks/batch — submit multiple tasks in a single request.

Design reference:
- Batch Operations API design: adidas API Guidelines "Batch Operations"
  https://adidas.gitbook.io/api-guidelines/rest-api-guidelines/execution/batch-operations
- PayPal Batch API: "Batch: An API to bundle multiple REST operations"
  https://medium.com/paypal-tech/batch-an-api-to-bundle-multiple-paypal-rest-operations-6af6006e002
- Mscharhag: "Supporting bulk operations in REST APIs"
  https://www.mscharhag.com/api-design/bulk-and-batch-operations

Semantics:
- POST /tasks/batch accepts a JSON body: {"tasks": [ {TaskSubmit}, ... ]}
- Returns: {"tasks": [ {task_id, prompt, priority, ?reply_to}, ... ]}
- Tasks are submitted atomically (all or none) from the client's perspective,
  but each is queued independently in the priority queue.
- Partial success is not supported: if any task fails validation the entire
  batch returns 422 before any task is enqueued.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_batch_submit_returns_all_tasks():
    """POST /tasks/batch with N tasks returns N task records."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/tasks/batch",
            json={
                "tasks": [
                    {"prompt": "task one", "priority": 0},
                    {"prompt": "task two", "priority": 1},
                    {"prompt": "task three", "priority": 2},
                ]
            },
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "tasks" in data
    assert len(data["tasks"]) == 3
    prompts = {t["prompt"] for t in data["tasks"]}
    assert prompts == {"task one", "task two", "task three"}


def test_batch_submit_each_task_has_id():
    """Each submitted task gets a unique task_id."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/tasks/batch",
            json={"tasks": [{"prompt": "a"}, {"prompt": "b"}]},
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200
    data = resp.json()
    ids = [t["task_id"] for t in data["tasks"]]
    assert len(ids) == 2
    assert ids[0] != ids[1], "Each task must get a distinct ID"


def test_batch_submit_respects_priority():
    """Tasks submitted with different priorities are reflected in the response."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/tasks/batch",
            json={
                "tasks": [
                    {"prompt": "high-pri", "priority": 0},
                    {"prompt": "low-pri", "priority": 10},
                ]
            },
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200
    data = resp.json()
    priority_map = {t["prompt"]: t["priority"] for t in data["tasks"]}
    assert priority_map["high-pri"] == 0
    assert priority_map["low-pri"] == 10


def test_batch_submit_supports_reply_to():
    """Tasks in a batch can specify reply_to and it is preserved in the response."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/tasks/batch",
            json={
                "tasks": [
                    {"prompt": "task a", "reply_to": "director"},
                    {"prompt": "task b", "reply_to": "director"},
                ]
            },
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200
    data = resp.json()
    for t in data["tasks"]:
        assert t.get("reply_to") == "director"


def test_batch_submit_empty_returns_empty():
    """An empty tasks list is accepted and returns an empty list."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/tasks/batch",
            json={"tasks": []},
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tasks"] == []


def test_batch_submit_requires_auth():
    """POST /tasks/batch requires authentication."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/tasks/batch",
            json={"tasks": [{"prompt": "task"}]},
        )
    assert resp.status_code == 401


def test_batch_submit_invalid_body_returns_422():
    """A malformed body returns 422 Unprocessable Entity."""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/tasks/batch",
            json={"tasks": [{"not_a_prompt": "x"}]},  # missing 'prompt' field
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 422


def test_batch_submit_tasks_appear_in_queue():
    """After submitting a batch, all tasks appear in GET /tasks."""
    app, _ = _make_app()
    with TestClient(app) as client:
        client.post(
            "/tasks/batch",
            json={"tasks": [{"prompt": "x"}, {"prompt": "y"}, {"prompt": "z"}]},
            headers={"X-API-Key": "test-key"},
        )
        resp = client.get("/tasks", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    # There should be at least 3 tasks (possibly more if queue was non-empty)
    tasks = resp.json()
    assert len(tasks) >= 3
