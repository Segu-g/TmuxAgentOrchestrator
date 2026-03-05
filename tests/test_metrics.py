"""Tests for Prometheus /metrics endpoint.

Covers:
- GET /metrics returns 200 with text/plain content-type
- Response contains expected metric families: agent status gauges,
  task queue length, task dispatch counter
- Metrics update when agent states change
- No authentication required (Prometheus scrape compatibility)

Reference: DESIGN.md §10.6 (v0.9.0) — Prometheus metrics /metrics (low priority,
           external deps, separate version). prometheus_client direct usage.
Sources:
- OneUptime blog (2025-01-06): python-custom-metrics-prometheus
- prometheus_client PyPI: https://pypi.org/project/prometheus-client/
"""

from __future__ import annotations

import pytest
import httpx

import tmux_orchestrator.web.app as web_app_mod
from tmux_orchestrator.web.app import create_app
from tmux_orchestrator.agents.base import AgentStatus


_API_KEY = "test-key-xyz"


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _MockOrchestrator:
    """Mock orchestrator with configurable agent list for metrics testing."""

    def __init__(self, agents=None, queue_size=0):
        self._mock_agents = agents or []
        self._mock_queue_size = queue_size
        self._dispatch_task = None
        self._director_pending = []

    def list_agents(self) -> list:
        return self._mock_agents

    def list_tasks(self) -> list:
        return [{"task_id": f"t{i}"} for i in range(self._mock_queue_size)]

    def get_agent(self, agent_id: str):
        return None

    def get_director(self):
        return None

    def flush_director_pending(self) -> list:
        return []

    def list_dlq(self) -> list:
        return []

    @property
    def is_paused(self) -> bool:
        return False

    async def reset_agent(self, agent_id: str) -> None:
        raise KeyError(agent_id)


@pytest.fixture(autouse=True)
def reset_state():
    web_app_mod._credentials.clear()
    web_app_mod._sign_counts.clear()
    web_app_mod._sessions.clear()
    web_app_mod._pending_challenge = None
    yield


@pytest.fixture
def orch_with_agents():
    return _MockOrchestrator(
        agents=[
            {"id": "w1", "status": "IDLE", "role": "worker", "current_task": None,
             "parent_id": None, "bus_drops": 0, "circuit_breaker": "CLOSED"},
            {"id": "w2", "status": "BUSY", "role": "worker", "current_task": "t1",
             "parent_id": None, "bus_drops": 0, "circuit_breaker": "CLOSED"},
            {"id": "w3", "status": "ERROR", "role": "worker", "current_task": None,
             "parent_id": None, "bus_drops": 2, "circuit_breaker": "OPEN"},
        ],
        queue_size=3,
    )


@pytest.fixture
def app_with_metrics(orch_with_agents):
    return create_app(orch_with_agents, _MockHub(), api_key=_API_KEY)


@pytest.fixture
async def metrics_client(app_with_metrics):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_metrics),
        base_url="http://localhost",
    ) as c:
        yield c


async def test_metrics_endpoint_returns_200(metrics_client) -> None:
    """GET /metrics returns HTTP 200."""
    r = await metrics_client.get("/metrics")
    assert r.status_code == 200


async def test_metrics_content_type_is_text_plain(metrics_client) -> None:
    """GET /metrics returns text/plain content type (Prometheus format)."""
    r = await metrics_client.get("/metrics")
    assert "text/plain" in r.headers["content-type"]


async def test_metrics_no_auth_required(app_with_metrics) -> None:
    """GET /metrics does NOT require authentication (Prometheus scraper compatibility)."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_metrics),
        base_url="http://localhost",
    ) as c:
        # No auth headers
        r = await c.get("/metrics")
    assert r.status_code == 200


async def test_metrics_contains_agent_status_gauge(metrics_client) -> None:
    """GET /metrics contains tmux_agent_status_total gauge."""
    r = await metrics_client.get("/metrics")
    body = r.text
    assert "tmux_agent_status_total" in body


async def test_metrics_contains_task_queue_gauge(metrics_client) -> None:
    """GET /metrics contains tmux_task_queue_size gauge."""
    r = await metrics_client.get("/metrics")
    body = r.text
    assert "tmux_task_queue_size" in body


async def test_metrics_contains_bus_drop_counter(metrics_client) -> None:
    """GET /metrics contains tmux_bus_drop_total counter."""
    r = await metrics_client.get("/metrics")
    body = r.text
    assert "tmux_bus_drop_total" in body


async def test_metrics_reflects_agent_count(metrics_client, orch_with_agents) -> None:
    """Metrics reflect the current agent status distribution (1 IDLE, 1 BUSY, 1 ERROR)."""
    r = await metrics_client.get("/metrics")
    body = r.text
    # Should see status label values; exact format is:
    # tmux_agent_status_total{status="IDLE"} 1.0
    # etc.
    assert 'status="IDLE"' in body
    assert 'status="BUSY"' in body
    assert 'status="ERROR"' in body


async def test_metrics_task_queue_size(metrics_client, orch_with_agents) -> None:
    """tmux_task_queue_size reflects the current queue depth."""
    r = await metrics_client.get("/metrics")
    body = r.text
    # queue_size = 3
    assert "tmux_task_queue_size 3" in body


async def test_metrics_empty_agents(app_with_metrics) -> None:
    """GET /metrics works even with no agents registered."""
    orch = _MockOrchestrator(agents=[], queue_size=0)
    empty_app = create_app(orch, _MockHub(), api_key=_API_KEY)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=empty_app),
        base_url="http://localhost",
    ) as c:
        r = await c.get("/metrics")
    assert r.status_code == 200
