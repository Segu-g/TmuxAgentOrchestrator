"""Tests for v1.0.20 — worktree_path in GET /agents list and GET /agents/{id}/stats enrichment.

Feature: REST API 充実 + worktree_path フィールド追加 (DESIGN.md §10, v1.0.20)

Tested behaviours:
1. list_all() includes worktree_path field for agents with a worktree set.
2. list_all() includes worktree_path=None for agents without a worktree.
3. worktree_path is serialised as a string (not a Path object).
4. GET /agents returns worktree_path in each agent entry.
5. GET /agents/{id}/stats includes worktree_path, status, task_count, error_count.
6. GET /agents/{id}/stats returns enriched skeleton when context monitor not yet polled.
7. GET /agents/{id}/stats merges context monitor stats with enrichment fields.
8. GET /agents/{id}/stats returns 404 when agent is unknown.
9. task_count reflects completed tasks, error_count reflects failed tasks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import AgentRole
from tmux_orchestrator.registry import AgentRegistry

# ---------------------------------------------------------------------------
# Minimal stub agent
# ---------------------------------------------------------------------------


class StubAgent(Agent):
    def __init__(self, agent_id: str, bus: Bus, *, role: str = "worker") -> None:
        super().__init__(agent_id, bus)
        self.role = role

    async def start(self) -> None:
        self.status = AgentStatus.IDLE

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED

    async def _dispatch_task(self, task: Task) -> None:
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


def make_registry(**kwargs) -> AgentRegistry:
    defaults = dict(p2p_permissions=[], circuit_breaker_threshold=3, circuit_breaker_recovery=60.0)
    defaults.update(kwargs)
    return AgentRegistry(**defaults)


# ---------------------------------------------------------------------------
# 1–3: registry.list_all() worktree_path field
# ---------------------------------------------------------------------------


def test_list_all_includes_worktree_path_when_set():
    """list_all() serialises worktree_path as a string when the agent has one."""
    bus = Bus()
    reg = make_registry()
    agent = StubAgent("a1", bus)
    agent.worktree_path = Path("/tmp/worktrees/a1")
    reg.register(agent)
    result = reg.list_all()
    assert len(result) == 1
    assert result[0]["worktree_path"] == "/tmp/worktrees/a1"


def test_list_all_worktree_path_none_when_not_set():
    """list_all() returns worktree_path=None when agent has no worktree."""
    bus = Bus()
    reg = make_registry()
    agent = StubAgent("a1", bus)
    assert agent.worktree_path is None  # default
    reg.register(agent)
    result = reg.list_all()
    assert result[0]["worktree_path"] is None


def test_list_all_worktree_path_is_string_not_path():
    """worktree_path in list_all() is a str, not a Path, for JSON serialisability."""
    bus = Bus()
    reg = make_registry()
    agent = StubAgent("a1", bus)
    agent.worktree_path = Path("/tmp/worktrees/a1")
    reg.register(agent)
    result = reg.list_all()
    assert isinstance(result[0]["worktree_path"], str)


# ---------------------------------------------------------------------------
# Web API helper
# ---------------------------------------------------------------------------

_API_KEY = "test-worktree-path-key"


def _make_mock_orch(agent_id: str, *, worktree_path=None, context_stats=None, history=None):
    """Build a minimal orchestrator mock for web app tests."""
    import tmux_orchestrator.web.app as web_app_mod

    orch = MagicMock()
    orch.list_tasks.return_value = []
    orch.get_director.return_value = None
    orch.flush_director_pending.return_value = []
    orch.list_dlq.return_value = []
    orch.is_paused = False
    orch.bus = MagicMock()
    orch.bus.subscribe = AsyncMock(return_value=asyncio.Queue())
    orch.bus.unsubscribe = AsyncMock()

    # Agent mock
    mock_agent = MagicMock()
    mock_agent.status = AgentStatus.IDLE
    mock_agent.worktree_path = Path(worktree_path) if worktree_path else None

    def _get_agent(aid: str):
        return mock_agent if aid == agent_id else None

    orch.get_agent = MagicMock(side_effect=_get_agent)
    orch.list_agents.return_value = [
        {
            "id": agent_id,
            "status": "IDLE",
            "current_task": None,
            "role": AgentRole.WORKER,
            "parent_id": None,
            "tags": [],
            "bus_drops": 0,
            "circuit_breaker": "closed",
            "worktree_path": worktree_path,
        }
    ]

    orch.get_agent_context_stats = MagicMock(return_value=context_stats)
    orch.all_agent_context_stats = MagicMock(return_value=[])
    orch.get_agent_history = MagicMock(return_value=history or [])

    return orch


def _make_client(orch):
    import tmux_orchestrator.web.app as web_app_mod
    from tmux_orchestrator.web.app import create_app

    web_app_mod._credentials.clear()
    web_app_mod._sign_counts.clear()
    web_app_mod._sessions.clear()

    class _MockHub:
        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    app = create_app(orch, _MockHub(), api_key=_API_KEY)
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# 4: GET /agents returns worktree_path
# ---------------------------------------------------------------------------


def test_get_agents_includes_worktree_path():
    """GET /agents list response includes worktree_path for each agent."""
    orch = _make_mock_orch("worker-1", worktree_path="/tmp/wt/worker-1")
    client = _make_client(orch)
    resp = client.get("/agents", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["worktree_path"] == "/tmp/wt/worker-1"


# ---------------------------------------------------------------------------
# 5: GET /agents/{id}/stats includes enrichment fields
# ---------------------------------------------------------------------------


def test_stats_endpoint_includes_worktree_path():
    """GET /agents/{id}/stats includes worktree_path field."""
    orch = _make_mock_orch("worker-1", worktree_path="/tmp/wt/worker-1", context_stats=None)
    client = _make_client(orch)
    resp = client.get("/agents/worker-1/stats", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["worktree_path"] == "/tmp/wt/worker-1"


def test_stats_endpoint_includes_status():
    """GET /agents/{id}/stats includes agent status field."""
    orch = _make_mock_orch("worker-1", context_stats=None)
    client = _make_client(orch)
    resp = client.get("/agents/worker-1/stats", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "IDLE"


# ---------------------------------------------------------------------------
# 6: Skeleton response when context monitor not yet polled
# ---------------------------------------------------------------------------


def test_stats_endpoint_skeleton_when_no_context_stats():
    """GET /agents/{id}/stats returns enriched skeleton (200) even if monitor not polled."""
    orch = _make_mock_orch("worker-1", context_stats=None)
    client = _make_client(orch)
    resp = client.get("/agents/worker-1/stats", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_id"] == "worker-1"
    assert "task_count" in data
    assert "error_count" in data


# ---------------------------------------------------------------------------
# 7: Merged response when context stats exist
# ---------------------------------------------------------------------------


def test_stats_endpoint_merges_context_and_enrichment():
    """GET /agents/{id}/stats merges context monitor stats with enrichment fields."""
    ctx_stats = {
        "agent_id": "worker-1",
        "pane_chars": 5000,
        "estimated_tokens": 1250,
        "context_window_tokens": 200_000,
        "context_pct": 0.625,
        "warn_threshold_pct": 75.0,
        "notes_mtime": 0.0,
        "notes_updates": 0,
        "context_warnings": 0,
        "summarize_triggers": 0,
        "last_polled": 12345.0,
    }
    orch = _make_mock_orch("worker-1", worktree_path="/tmp/wt/w1", context_stats=ctx_stats)
    client = _make_client(orch)
    resp = client.get("/agents/worker-1/stats", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    # Context monitor fields preserved
    assert data["pane_chars"] == 5000
    assert data["estimated_tokens"] == 1250
    # Enrichment fields added
    assert data["worktree_path"] == "/tmp/wt/w1"
    assert data["status"] == "IDLE"
    assert "task_count" in data
    assert "error_count" in data


# ---------------------------------------------------------------------------
# 8: 404 for unknown agent
# ---------------------------------------------------------------------------


def test_stats_endpoint_404_for_unknown_agent():
    """GET /agents/{id}/stats returns 404 when agent is not registered."""
    orch = _make_mock_orch("worker-1", context_stats=None)
    client = _make_client(orch)
    resp = client.get("/agents/no-such-agent/stats", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 9: task_count and error_count
# ---------------------------------------------------------------------------


def test_stats_task_count_reflects_history():
    """task_count and error_count count history entries correctly."""
    history = [
        {"task_id": "t1", "status": "success"},
        {"task_id": "t2", "status": "error"},
        {"task_id": "t3", "status": "success"},
    ]
    orch = _make_mock_orch("worker-1", context_stats=None, history=history)
    client = _make_client(orch)
    resp = client.get("/agents/worker-1/stats", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_count"] == 3
    assert data["error_count"] == 1
