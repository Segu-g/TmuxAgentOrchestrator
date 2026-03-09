"""REST API tests for episodic memory endpoints.

Tests:
- GET /agents/{id}/memory — list episodes
- POST /agents/{id}/memory — add episode
- DELETE /agents/{id}/memory/{episode_id} — delete episode
- 404 for unknown agents
- Auth enforcement (401 without key)

Design reference: DESIGN.md §10.28 (v1.0.28); arXiv:2507.07957.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from tmux_orchestrator.agents.base import AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app

try:
    from tests.integration.test_orchestration import HeadlessAgent
except ImportError:
    from integration.test_orchestration import HeadlessAgent  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_API_KEY = "test-episode-key"


def make_config(**kwargs) -> OrchestratorConfig:
    defaults = dict(
        session_name="test-ep",
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


def _make_app(tmp_path=None):
    import tempfile
    from pathlib import Path

    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp())

    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(mailbox_dir=str(tmp_path))
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    app = create_app(orch, _StubHub(), api_key=_API_KEY)  # type: ignore[arg-type]
    return app, orch


def auth_headers() -> dict:
    return {"X-API-Key": _API_KEY}


@pytest.fixture()
def setup(tmp_path):
    app, orch = _make_app(tmp_path)
    agent = HeadlessAgent("worker-1", orch.bus)
    orch.register_agent(agent)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, orch


# ---------------------------------------------------------------------------
# GET /agents/{id}/memory — list
# ---------------------------------------------------------------------------


def test_list_episodes_empty(setup):
    client, orch = setup
    resp = client.get("/agents/worker-1/memory", headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_episodes_404_unknown_agent(setup):
    client, orch = setup
    resp = client.get("/agents/nobody/memory", headers=auth_headers())
    assert resp.status_code == 404


def test_list_episodes_requires_auth(setup):
    client, orch = setup
    resp = client.get("/agents/worker-1/memory")
    assert resp.status_code in (401, 403)


def test_list_episodes_after_post(setup):
    client, orch = setup
    client.post(
        "/agents/worker-1/memory",
        json={"summary": "did it", "outcome": "success"},
        headers=auth_headers(),
    )
    resp = client.get("/agents/worker-1/memory", headers=auth_headers())
    assert resp.status_code == 200
    episodes = resp.json()
    assert len(episodes) == 1
    assert episodes[0]["summary"] == "did it"
    assert episodes[0]["outcome"] == "success"
    assert episodes[0]["agent_id"] == "worker-1"
    assert "id" in episodes[0]
    assert "created_at" in episodes[0]


def test_list_episodes_newest_first(setup):
    client, orch = setup
    for i in range(3):
        client.post(
            "/agents/worker-1/memory",
            json={"summary": f"ep{i}", "outcome": "success"},
            headers=auth_headers(),
        )
    resp = client.get("/agents/worker-1/memory", headers=auth_headers())
    episodes = resp.json()
    assert len(episodes) == 3
    # Newest-first: last posted = ep2, first in list
    assert episodes[0]["summary"] == "ep2"
    assert episodes[-1]["summary"] == "ep0"


def test_list_episodes_limit_param(setup):
    client, orch = setup
    for i in range(5):
        client.post(
            "/agents/worker-1/memory",
            json={"summary": f"ep{i}", "outcome": "success"},
            headers=auth_headers(),
        )
    resp = client.get("/agents/worker-1/memory?limit=2", headers=auth_headers())
    assert resp.status_code == 200
    assert len(resp.json()) == 2


# ---------------------------------------------------------------------------
# POST /agents/{id}/memory — create
# ---------------------------------------------------------------------------


def test_post_episode_minimal(setup):
    client, orch = setup
    resp = client.post(
        "/agents/worker-1/memory",
        json={"summary": "wrote hello.py", "outcome": "success"},
        headers=auth_headers(),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["summary"] == "wrote hello.py"
    assert data["outcome"] == "success"
    assert data["lessons"] == ""
    assert data["task_id"] is None
    assert data["agent_id"] == "worker-1"
    assert "id" in data
    assert "created_at" in data


def test_post_episode_full_fields(setup):
    client, orch = setup
    resp = client.post(
        "/agents/worker-1/memory",
        json={
            "summary": "refactored cache",
            "outcome": "partial",
            "lessons": "Use LRU eviction next time",
            "task_id": "task-abc123",
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["outcome"] == "partial"
    assert data["lessons"] == "Use LRU eviction next time"
    assert data["task_id"] == "task-abc123"


def test_post_episode_invalid_outcome(setup):
    client, orch = setup
    resp = client.post(
        "/agents/worker-1/memory",
        json={"summary": "x", "outcome": "invalid-value"},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_post_episode_missing_summary(setup):
    client, orch = setup
    resp = client.post(
        "/agents/worker-1/memory",
        json={"outcome": "success"},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_post_episode_missing_outcome(setup):
    client, orch = setup
    resp = client.post(
        "/agents/worker-1/memory",
        json={"summary": "did something"},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_post_episode_404_unknown_agent(setup):
    client, orch = setup
    resp = client.post(
        "/agents/nobody/memory",
        json={"summary": "x", "outcome": "success"},
        headers=auth_headers(),
    )
    assert resp.status_code == 404


def test_post_episode_requires_auth(setup):
    client, orch = setup
    resp = client.post(
        "/agents/worker-1/memory",
        json={"summary": "x", "outcome": "success"},
    )
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# DELETE /agents/{id}/memory/{episode_id}
# ---------------------------------------------------------------------------


def test_delete_episode(setup):
    client, orch = setup
    create_resp = client.post(
        "/agents/worker-1/memory",
        json={"summary": "to be deleted", "outcome": "failure"},
        headers=auth_headers(),
    )
    episode_id = create_resp.json()["id"]
    del_resp = client.delete(
        f"/agents/worker-1/memory/{episode_id}",
        headers=auth_headers(),
    )
    assert del_resp.status_code == 200
    data = del_resp.json()
    assert data["deleted"] is True
    assert data["episode_id"] == episode_id

    # Verify it's gone
    list_resp = client.get("/agents/worker-1/memory", headers=auth_headers())
    assert all(ep["id"] != episode_id for ep in list_resp.json())


def test_delete_nonexistent_episode_404(setup):
    client, orch = setup
    resp = client.delete(
        "/agents/worker-1/memory/does-not-exist",
        headers=auth_headers(),
    )
    assert resp.status_code == 404


def test_delete_episode_unknown_agent_404(setup):
    client, orch = setup
    resp = client.delete(
        "/agents/nobody/memory/some-id",
        headers=auth_headers(),
    )
    assert resp.status_code == 404


def test_delete_episode_requires_auth(setup):
    client, orch = setup
    create_resp = client.post(
        "/agents/worker-1/memory",
        json={"summary": "x", "outcome": "success"},
        headers=auth_headers(),
    )
    episode_id = create_resp.json()["id"]
    resp = client.delete(f"/agents/worker-1/memory/{episode_id}")
    assert resp.status_code in (401, 403)


def test_delete_preserves_other_episodes(setup):
    client, orch = setup
    eps = []
    for i in range(3):
        r = client.post(
            "/agents/worker-1/memory",
            json={"summary": f"ep{i}", "outcome": "success"},
            headers=auth_headers(),
        )
        eps.append(r.json()["id"])

    client.delete(f"/agents/worker-1/memory/{eps[1]}", headers=auth_headers())
    list_resp = client.get("/agents/worker-1/memory", headers=auth_headers())
    remaining_ids = {ep["id"] for ep in list_resp.json()}
    assert eps[0] in remaining_ids
    assert eps[2] in remaining_ids
    assert eps[1] not in remaining_ids
