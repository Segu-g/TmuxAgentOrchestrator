"""Tests for MetricsCollector time-series ring buffer.

Design reference: DESIGN.md §10.92 (v1.2.16)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, AsyncMock

import pytest

from tmux_orchestrator.infrastructure.metrics_collector import (
    MetricsCollector,
    MetricsSnapshot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_collector(
    queue_depth: int = 0,
    agent_statuses: dict[str, str] | None = None,
    cumulative: dict[str, Any] | None = None,
    max_snapshots: int = 10,
) -> MetricsCollector:
    if agent_statuses is None:
        agent_statuses = {}
    if cumulative is None:
        cumulative = {"tasks_completed_total": 0, "tasks_failed_total": 0, "per_agent": {}}
    return MetricsCollector(
        get_queue_depth=lambda: queue_depth,
        get_agent_statuses=lambda: agent_statuses,
        get_cumulative_stats=lambda: cumulative,
        max_snapshots=max_snapshots,
    )


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestMetricsCollectorInit:
    def test_initializes_with_empty_deque(self):
        collector = _make_collector()
        assert collector.get_snapshots() == []

    def test_get_latest_returns_none_when_empty(self):
        collector = _make_collector()
        assert collector.get_latest() is None

    def test_max_snapshots_respected(self):
        """Ring buffer should not exceed max_snapshots entries."""
        collector = _make_collector(max_snapshots=3)
        for _ in range(5):
            collector._snapshots.append(collector._collect())
        assert len(collector._snapshots) == 3

    def test_interval_s_defaults_to_ten(self):
        collector = _make_collector()
        assert collector.interval_s == 10.0


class TestMetricsCollectorCollect:
    def test_collect_returns_snapshot_structure(self):
        collector = _make_collector(
            queue_depth=4,
            agent_statuses={"w1": "BUSY", "w2": "IDLE"},
            cumulative={"tasks_completed_total": 7, "tasks_failed_total": 1, "per_agent": {}},
        )
        snap = collector._collect()
        assert isinstance(snap, MetricsSnapshot)
        assert snap.queue_depth == 4
        assert snap.active_agents == 1
        assert snap.idle_agents == 1
        assert snap.tasks_completed_total == 7
        assert snap.tasks_failed_total == 1
        assert snap.timestamp  # non-empty ISO string

    def test_queue_depth_reflects_actual_value(self):
        collector = _make_collector(queue_depth=10)
        snap = collector._collect()
        assert snap.queue_depth == 10

    def test_active_agents_count_matches_busy_agents(self):
        statuses = {"w1": "BUSY", "w2": "BUSY", "w3": "IDLE"}
        collector = _make_collector(agent_statuses=statuses)
        snap = collector._collect()
        assert snap.active_agents == 2

    def test_idle_agents_count_matches_idle_agents(self):
        statuses = {"w1": "IDLE", "w2": "IDLE", "w3": "BUSY"}
        collector = _make_collector(agent_statuses=statuses)
        snap = collector._collect()
        assert snap.idle_agents == 2

    def test_per_agent_status_populated(self):
        statuses = {"w1": "IDLE"}
        collector = _make_collector(agent_statuses=statuses)
        snap = collector._collect()
        assert "w1" in snap.per_agent
        assert snap.per_agent["w1"]["status"] == "IDLE"

    def test_per_agent_stats_merged_from_cumulative(self):
        statuses = {"w1": "BUSY"}
        cumulative = {
            "tasks_completed_total": 3,
            "tasks_failed_total": 1,
            "per_agent": {
                "w1": {"tasks_completed": 3, "tasks_failed": 1, "error_rate": 0.25}
            },
        }
        collector = _make_collector(agent_statuses=statuses, cumulative=cumulative)
        snap = collector._collect()
        assert snap.per_agent["w1"]["tasks_completed"] == 3
        assert snap.per_agent["w1"]["error_rate"] == 0.25

    def test_collect_with_no_agents(self):
        collector = _make_collector(queue_depth=0, agent_statuses={})
        snap = collector._collect()
        assert snap.active_agents == 0
        assert snap.idle_agents == 0
        assert snap.per_agent == {}


class TestGetSnapshots:
    def test_get_snapshots_returns_all(self):
        collector = _make_collector(max_snapshots=10)
        for _ in range(3):
            collector._snapshots.append(collector._collect())
        assert len(collector.get_snapshots()) == 3

    def test_get_snapshots_last_n_limits_results(self):
        collector = _make_collector(max_snapshots=10)
        for _ in range(8):
            collector._snapshots.append(collector._collect())
        result = collector.get_snapshots(last_n=5)
        assert len(result) == 5

    def test_get_snapshots_last_n_zero_returns_all(self):
        collector = _make_collector(max_snapshots=10)
        for _ in range(4):
            collector._snapshots.append(collector._collect())
        # last_n=0 or None should return all
        assert len(collector.get_snapshots(last_n=None)) == 4

    def test_get_latest_returns_most_recent(self):
        collector = _make_collector(max_snapshots=10)
        for i in range(3):
            snap = collector._collect()
            collector._snapshots.append(snap)
        latest = collector.get_latest()
        assert latest is not None
        assert latest is collector.get_snapshots()[-1]


class TestMetricsCollectorAsync:
    @pytest.mark.asyncio
    async def test_start_creates_background_task(self):
        collector = _make_collector()
        await collector.start(interval_s=100.0)
        assert collector._task is not None
        assert not collector._task.done()
        await collector.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        collector = _make_collector()
        await collector.start(interval_s=100.0)
        await collector.stop()
        assert collector._task.done()

    @pytest.mark.asyncio
    async def test_collect_loop_appends_snapshots(self):
        """Integration: loop appends at least one snapshot within a short interval."""
        collector = _make_collector(queue_depth=2)
        await collector.start(interval_s=0.05)
        await asyncio.sleep(0.15)  # enough for 2-3 ticks
        await collector.stop()
        assert len(collector.get_snapshots()) >= 2

    @pytest.mark.asyncio
    async def test_stop_is_idempotent_when_not_started(self):
        collector = _make_collector()
        # Should not raise
        await collector.stop()


class TestMetricsEndpoints:
    """Integration tests for GET /metrics/time-series and GET /metrics/agents/{id}."""

    def _make_mock_orchestrator(self) -> Any:
        orch = MagicMock()
        orch.queue_size = lambda: 3
        orch.get_all_agent_statuses = lambda: {"w1": "IDLE", "w2": "BUSY"}
        orch.get_cumulative_stats = lambda: {
            "tasks_completed_total": 5,
            "tasks_failed_total": 1,
            "per_agent": {
                "w1": {"tasks_completed": 3, "tasks_failed": 0, "error_rate": 0.0},
                "w2": {"tasks_completed": 2, "tasks_failed": 1, "error_rate": 0.333},
            },
        }
        return orch

    def test_get_metrics_time_series_returns_correct_structure(self):
        """GET /metrics/time-series returns the correct response shape."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from tmux_orchestrator.web.routers.system import build_system_router
        from tmux_orchestrator.infrastructure.metrics_collector import MetricsCollector

        orch = self._make_mock_orchestrator()
        collector = MetricsCollector(
            get_queue_depth=orch.queue_size,
            get_agent_statuses=orch.get_all_agent_statuses,
            get_cumulative_stats=orch.get_cumulative_stats,
            max_snapshots=10,
        )
        # Add a snapshot manually
        collector._snapshots.append(collector._collect())

        app = FastAPI()
        app.include_router(build_system_router(orch, lambda: None, metrics_collector=collector))
        client = TestClient(app)
        resp = client.get("/metrics/time-series")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert "interval_s" in data
        assert "count" in data
        assert "snapshots" in data
        assert "latest" in data
        assert len(data["snapshots"]) == 1
        snap = data["snapshots"][0]
        assert "timestamp" in snap
        assert snap["queue_depth"] == 3
        assert snap["active_agents"] == 1
        assert snap["idle_agents"] == 1

    def test_get_agent_metrics_series_returns_per_agent_series(self):
        """GET /metrics/agents/{id} returns per-agent time series."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from tmux_orchestrator.web.routers.system import build_system_router
        from tmux_orchestrator.infrastructure.metrics_collector import MetricsCollector

        orch = self._make_mock_orchestrator()
        collector = MetricsCollector(
            get_queue_depth=orch.queue_size,
            get_agent_statuses=orch.get_all_agent_statuses,
            get_cumulative_stats=orch.get_cumulative_stats,
            max_snapshots=10,
        )
        # Add a snapshot manually
        collector._snapshots.append(collector._collect())

        app = FastAPI()
        app.include_router(build_system_router(orch, lambda: None, metrics_collector=collector))
        client = TestClient(app)
        resp = client.get("/metrics/agents/w1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "w1"
        assert "series" in data
        assert len(data["series"]) == 1
        entry = data["series"][0]
        assert entry["status"] == "IDLE"

    def test_get_metrics_disabled_when_collector_none(self):
        """When metrics_collector is None, endpoint returns enabled=False."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from tmux_orchestrator.web.routers.system import build_system_router

        orch = self._make_mock_orchestrator()
        app = FastAPI()
        app.include_router(build_system_router(orch, lambda: None, metrics_collector=None))
        client = TestClient(app)
        resp = client.get("/metrics/time-series")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["snapshots"] == []
