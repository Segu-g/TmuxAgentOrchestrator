"""Tests for v1.2.11 agent stats dashboard enhancement.

New/enhanced endpoints:
- GET /agents/{id}/stats  — now includes tasks_completed, tasks_failed,
  error_rate, avg_task_duration_s, last_task_at (+ context_pct).
- GET /agents/summary     — cross-agent aggregate view.

Design references:
- Google SRE "Four Golden Signals" (latency, traffic, errors, saturation):
  https://sre.google/sre-book/monitoring-distributed-systems/
- Microsoft Azure Agent Factory observability best practices (2025):
  https://azure.microsoft.com/en-us/blog/agent-factory-top-5-agent-observability-best-practices-for-reliable-ai/
- TAMAS arXiv:2503.06745 (IBM, 2025) — per-agent task history.
- DESIGN.md §10.87 (v1.2.11).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

import tmux_orchestrator.web.app as web_app_mod
from tmux_orchestrator.application.config import OrchestratorConfig
from tmux_orchestrator.web.app import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def handle(self, ws) -> None:
        pass


def _make_agent_mock(agent_id: str, status: str = "IDLE") -> Any:
    m = MagicMock()
    m.id = agent_id
    m.status.value = status
    m.worktree_path = None
    m.started_at = None
    m.uptime_s = 0.0
    return m


class _MockOrchestrator:
    """Mock orchestrator for stats dashboard tests."""

    def __init__(self) -> None:
        self._agents: dict[str, Any] = {}
        self._histories: dict[str, list] = {}
        self._context_stats: dict[str, dict | None] = {}
        self._director_pending: list = []
        self._dispatch_task = None
        self.config = OrchestratorConfig(
            session_name="test",
            agents=[],
            mailbox_dir="~/.tmux_orchestrator",
        )

    def add_agent(
        self,
        agent_id: str,
        status: str = "IDLE",
        history: list | None = None,
        context_stats: dict | None = None,
    ) -> None:
        self._agents[agent_id] = _make_agent_mock(agent_id, status)
        self._histories[agent_id] = history or []
        self._context_stats[agent_id] = context_stats

    def list_agents(self) -> list:
        return [
            {"id": aid, "status": a.status.value}
            for aid, a in self._agents.items()
        ]

    def list_tasks(self) -> list:
        return []

    def get_agent(self, agent_id: str) -> Any | None:
        return self._agents.get(agent_id)

    def get_director(self) -> None:
        return None

    def flush_director_pending(self) -> list:
        return []

    def list_dlq(self) -> list:
        return []

    @property
    def is_paused(self) -> bool:
        return False

    def get_agent_history(self, agent_id: str, limit: int = 50) -> list | None:
        if agent_id not in self._agents and agent_id not in self._histories:
            return None
        return list(self._histories.get(agent_id, []))[:limit]

    def get_agent_context_stats(self, agent_id: str) -> dict | None:
        return self._context_stats.get(agent_id)

    def get_rate_limiter_status(self) -> dict:
        return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}

    def get_agent_drift_stats(self, agent_id: str) -> dict | None:
        return None

    def get_agent_drift_rebriefs(self, agent_id: str) -> list:
        return []


def _make_history(
    n_success: int = 0,
    n_error: int = 0,
    duration_s: float = 10.0,
) -> list:
    records = []
    for i in range(n_success):
        records.append({
            "task_id": f"ts{i}",
            "prompt": f"task {i}",
            "started_at": "2026-03-14T10:00:00+00:00",
            "finished_at": "2026-03-14T10:00:10+00:00",
            "duration_s": duration_s,
            "status": "success",
            "error": None,
        })
    for j in range(n_error):
        records.append({
            "task_id": f"te{j}",
            "prompt": f"failing task {j}",
            "started_at": "2026-03-14T10:00:00+00:00",
            "finished_at": "2026-03-14T10:00:10+00:00",
            "duration_s": duration_s,
            "status": "error",
            "error": "simulated error",
        })
    return records


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_web_state():
    web_app_mod._credentials.clear()
    web_app_mod._sign_counts.clear()
    web_app_mod._sessions.clear()
    web_app_mod._pending_challenge = None
    yield


@pytest.fixture
def orch() -> _MockOrchestrator:
    return _MockOrchestrator()


@pytest.fixture
def app(orch):
    return create_app(orch, _MockHub(), api_key="test-key")


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests: GET /agents/{id}/stats — enhanced fields
# ---------------------------------------------------------------------------


async def test_stats_includes_tasks_completed(orch, client):
    """GET /agents/{id}/stats includes tasks_completed field."""
    orch.add_agent("w1", history=_make_history(n_success=3, n_error=0))
    r = await client.get("/agents/w1/stats", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert "tasks_completed" in body
    assert body["tasks_completed"] == 3


async def test_stats_includes_error_rate_zero_when_no_failures(orch, client):
    """GET /agents/{id}/stats: error_rate is 0.0 when no failures."""
    orch.add_agent("w1", history=_make_history(n_success=5))
    r = await client.get("/agents/w1/stats", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert "error_rate" in body
    assert body["error_rate"] == 0.0


async def test_stats_error_rate_correct_with_failures(orch, client):
    """error_rate = tasks_failed / (tasks_completed + tasks_failed)."""
    # 3 success + 1 error → rate = 1/4 = 0.25
    orch.add_agent("w1", history=_make_history(n_success=3, n_error=1))
    r = await client.get("/agents/w1/stats", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert abs(body["error_rate"] - 0.25) < 1e-4


async def test_stats_avg_task_duration_correct(orch, client):
    """avg_task_duration_s is the mean of duration_s across all tasks."""
    history = [
        {
            "task_id": "t1",
            "prompt": "p1",
            "started_at": "2026-03-14T10:00:00+00:00",
            "finished_at": "2026-03-14T10:00:10+00:00",
            "duration_s": 10.0,
            "status": "success",
            "error": None,
        },
        {
            "task_id": "t2",
            "prompt": "p2",
            "started_at": "2026-03-14T10:00:10+00:00",
            "finished_at": "2026-03-14T10:00:30+00:00",
            "duration_s": 30.0,
            "status": "success",
            "error": None,
        },
    ]
    orch.add_agent("w1", history=history)
    r = await client.get("/agents/w1/stats", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert "avg_task_duration_s" in body
    assert abs(body["avg_task_duration_s"] - 20.0) < 0.01


async def test_stats_avg_duration_none_when_no_tasks(orch, client):
    """avg_task_duration_s is null when agent has no task history."""
    orch.add_agent("w1", history=[])
    r = await client.get("/agents/w1/stats", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("avg_task_duration_s") is None


async def test_stats_last_task_at_is_iso_timestamp(orch, client):
    """last_task_at is an ISO timestamp string when tasks exist."""
    orch.add_agent("w1", history=_make_history(n_success=2))
    r = await client.get("/agents/w1/stats", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert "last_task_at" in body
    assert body["last_task_at"] is not None
    # Should contain a date-like string
    assert "2026" in body["last_task_at"]


async def test_stats_last_task_at_none_when_no_history(orch, client):
    """last_task_at is null when agent has no history."""
    orch.add_agent("w1", history=[])
    r = await client.get("/agents/w1/stats", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("last_task_at") is None


async def test_stats_context_pct_present_when_monitor_data_available(orch, client):
    """context_pct is present (from context monitor) when available."""
    ctx = {
        "agent_id": "w1",
        "pane_chars": 1000,
        "estimated_tokens": 250,
        "context_window_tokens": 200_000,
        "context_pct": 0.1,
        "warn_threshold_pct": 75.0,
        "notes_mtime": 0.0,
        "notes_updates": 0,
        "context_warnings": 0,
        "summarize_triggers": 0,
        "compress_triggers": 0,
        "last_polled": 0.0,
    }
    orch.add_agent("w1", context_stats=ctx)
    r = await client.get("/agents/w1/stats", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert "context_pct" in body
    assert body["context_pct"] == 0.1


async def test_stats_context_pct_absent_when_no_monitor(orch, client):
    """context_pct is absent when context monitor has no data."""
    orch.add_agent("w1", context_stats=None)
    r = await client.get("/agents/w1/stats", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    # context_pct not in body when monitor hasn't polled
    assert "context_pct" not in body


async def test_stats_404_for_unknown_agent(orch, client):
    """GET /agents/{id}/stats returns 404 for unknown agents."""
    r = await client.get("/agents/ghost/stats", headers={"X-API-Key": "test-key"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Tests: GET /agents/summary
# ---------------------------------------------------------------------------


async def test_summary_returns_all_agents(orch, client):
    """GET /agents/summary returns a list containing all registered agents."""
    orch.add_agent("w1", history=_make_history(n_success=2))
    orch.add_agent("w2", history=_make_history(n_success=1, n_error=1))
    r = await client.get("/agents/summary", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert "agents" in body
    assert body["total_agents"] == 2
    agent_ids = {a["agent_id"] for a in body["agents"]}
    assert agent_ids == {"w1", "w2"}


async def test_summary_total_tasks_completed_sums_across_agents(orch, client):
    """summary.total_tasks_completed sums tasks_completed across all agents."""
    orch.add_agent("w1", history=_make_history(n_success=5))
    orch.add_agent("w2", history=_make_history(n_success=3))
    r = await client.get("/agents/summary", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert body["total_tasks_completed"] == 8
    assert body["total_tasks_failed"] == 0


async def test_summary_busiest_agent_is_most_completed(orch, client):
    """summary.busiest_agent is the agent with the most tasks_completed."""
    orch.add_agent("w1", history=_make_history(n_success=2))
    orch.add_agent("w2", history=_make_history(n_success=7))
    orch.add_agent("w3", history=_make_history(n_success=1))
    r = await client.get("/agents/summary", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert body["busiest_agent"] == "w2"


async def test_summary_busiest_agent_none_when_no_tasks(orch, client):
    """summary.busiest_agent is null when no tasks have been completed."""
    orch.add_agent("w1", history=[])
    orch.add_agent("w2", history=[])
    r = await client.get("/agents/summary", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert body["busiest_agent"] is None


async def test_summary_error_rate_per_agent(orch, client):
    """Each agent in summary has a correct error_rate field."""
    orch.add_agent("w1", history=_make_history(n_success=3, n_error=1))
    r = await client.get("/agents/summary", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    w1 = next(a for a in body["agents"] if a["agent_id"] == "w1")
    # 1 error out of 4 total = 0.25
    assert abs(w1["error_rate"] - 0.25) < 1e-4


async def test_summary_requires_auth(orch, client):
    """GET /agents/summary requires authentication."""
    r = await client.get("/agents/summary")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Tests: GET /agents/{id}/history
# ---------------------------------------------------------------------------


async def test_history_endpoint_returns_list_ordered_by_recency(orch, client):
    """GET /agents/{id}/history returns list ordered most-recent-first."""
    history = [
        {
            "task_id": "t2",
            "prompt": "latest",
            "started_at": "2026-03-14T11:00:00+00:00",
            "finished_at": "2026-03-14T11:00:05+00:00",
            "duration_s": 5.0,
            "status": "success",
            "error": None,
        },
        {
            "task_id": "t1",
            "prompt": "first",
            "started_at": "2026-03-14T10:00:00+00:00",
            "finished_at": "2026-03-14T10:00:03+00:00",
            "duration_s": 3.0,
            "status": "success",
            "error": None,
        },
    ]
    orch.add_agent("w1", history=history)
    r = await client.get("/agents/w1/history", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 2
    # First entry is most recent
    assert body[0]["task_id"] == "t2"


async def test_history_entries_have_duration_s(orch, client):
    """History entries include duration_s field."""
    orch.add_agent("w1", history=_make_history(n_success=1, duration_s=42.5))
    r = await client.get("/agents/w1/history", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert "duration_s" in body[0]
    assert body[0]["duration_s"] == 42.5


async def test_history_limit_parameter_respected(orch, client):
    """GET /agents/{id}/history?limit=N returns at most N entries."""
    orch.add_agent("w1", history=_make_history(n_success=10))
    r = await client.get(
        "/agents/w1/history?limit=3",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) <= 3


# ---------------------------------------------------------------------------
# Tests: TUI StatusBar widget
# ---------------------------------------------------------------------------


def test_statusbar_update_stats_sets_fields() -> None:
    """StatusBar.update_stats() sets tasks_completed, active_agents, high_error_agents."""
    from tmux_orchestrator.tui.widgets import StatusBar

    sb = StatusBar()
    sb.update_stats(
        tasks_completed=42,
        active_agents=3,
        high_error_agents=["w2"],
    )
    assert sb.tasks_completed == 42
    assert sb.active_agents == 3
    assert sb.high_error_agents == ["w2"]


def test_statusbar_render_includes_stats() -> None:
    """StatusBar.render() includes active agent count and tasks completed."""
    from tmux_orchestrator.tui.widgets import StatusBar

    sb = StatusBar()
    sb.update_stats(
        tasks_completed=10,
        active_agents=2,
        high_error_agents=[],
    )
    rendered = sb.render()
    assert "10" in rendered  # tasks_completed
    assert "2" in rendered   # active_agents


def test_statusbar_render_shows_high_error_warning() -> None:
    """StatusBar.render() shows ERR% warning when agents have high error rates."""
    from tmux_orchestrator.tui.widgets import StatusBar

    sb = StatusBar()
    sb.update_stats(
        tasks_completed=5,
        active_agents=2,
        high_error_agents=["worker-bad"],
    )
    rendered = sb.render()
    assert "worker-bad" in rendered
