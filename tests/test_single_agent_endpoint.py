"""Tests for GET /agents/{id} single-agent endpoint.

Covers:
- 200 with correct agent dict for known agent ID
- 404 with detail message for unknown agent ID
- Response fields match the corresponding entry from GET /agents
- Endpoint requires authentication

Reference: DESIGN.md §10.40 — v1.1.4 GET /agents/{id}
Patterns: REST uniform resource interface; Microsoft Azure API Design best practices.
"""

from __future__ import annotations

import httpx
import pytest

from tmux_orchestrator.web.app import create_app


# ---------------------------------------------------------------------------
# Helpers / Mocks
# ---------------------------------------------------------------------------


_API_KEY = "test-key-xyz"

_AGENT_DICT = {
    "id": "worker-1",
    "status": "IDLE",
    "current_task": None,
    "role": "worker",
    "parent_id": None,
    "tags": ["compute"],
    "bus_drops": 0,
    "circuit_breaker": None,
    "worktree_path": None,
    "started_at": None,
    "uptime_s": 0.0,
}


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _MockOrchestrator:
    """Mock orchestrator that serves a fixed agent list."""

    def __init__(self, agents: list[dict] | None = None) -> None:
        self._agent_list: list[dict] = agents or [_AGENT_DICT]
        self._director_pending: list = []
        self._dispatch_task = None

    def list_agents(self) -> list[dict]:
        return self._agent_list

    def get_agent_dict(self, agent_id: str) -> dict | None:
        """Return a single agent dict by ID, or None if not found."""
        return next((a for a in self._agent_list if a["id"] == agent_id), None)

    def list_tasks(self) -> list:
        return []

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

    def get_rate_limiter_status(self) -> dict:
        return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}

    def reconfigure_rate_limiter(self, *, rate: float, burst: int) -> dict:
        return {"enabled": rate > 0, "rate": rate, "burst": burst,
                "available_tokens": float(burst)}

    def get_workflow_manager(self):
        from tmux_orchestrator.workflow_manager import WorkflowManager
        return WorkflowManager()

    @property
    def _webhook_manager(self):
        from tmux_orchestrator.webhook_manager import WebhookManager
        return WebhookManager()


@pytest.fixture()
def app():
    """Return a FastAPI app with a mock orchestrator containing one agent."""
    return create_app(_MockOrchestrator(), _MockHub(), api_key=_API_KEY)


@pytest.fixture()
def app_empty():
    """Return a FastAPI app with a mock orchestrator containing no agents."""
    return create_app(_MockOrchestrator(agents=[]), _MockHub(), api_key=_API_KEY)


# ---------------------------------------------------------------------------
# Tests: GET /agents/{agent_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_agent_known_id_returns_200(app) -> None:
    """GET /agents/{id} with a known ID returns 200 and agent dict."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/agents/worker-1",
            headers={"X-Api-Key": _API_KEY},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "worker-1"
    assert data["status"] in ("idle", "IDLE")


@pytest.mark.asyncio
async def test_get_agent_unknown_id_returns_404(app) -> None:
    """GET /agents/{id} with an unknown ID returns 404."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/agents/does-not-exist",
            headers={"X-Api-Key": _API_KEY},
        )
    assert resp.status_code == 404
    body = resp.json()
    assert "not found" in body["detail"].lower()


@pytest.mark.asyncio
async def test_get_agent_matches_list_entry(app) -> None:
    """GET /agents/{id} returns the same data as the entry in GET /agents."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        list_resp = await client.get("/agents", headers={"X-Api-Key": _API_KEY})
        single_resp = await client.get(
            "/agents/worker-1", headers={"X-Api-Key": _API_KEY}
        )
    assert list_resp.status_code == 200
    assert single_resp.status_code == 200

    agents_list = list_resp.json()
    single = single_resp.json()

    matched = next((a for a in agents_list if a["id"] == "worker-1"), None)
    assert matched is not None, "worker-1 should appear in GET /agents"
    assert single == matched


@pytest.mark.asyncio
async def test_get_agent_requires_auth(app) -> None:
    """GET /agents/{id} returns 401/403 when no API key is provided."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/agents/worker-1")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_agent_returns_all_standard_fields(app) -> None:
    """GET /agents/{id} response includes all standard agent dict fields."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/agents/worker-1",
            headers={"X-Api-Key": _API_KEY},
        )
    assert resp.status_code == 200
    data = resp.json()
    expected_fields = {
        "id", "status", "role", "tags", "bus_drops", "circuit_breaker",
        "worktree_path", "started_at", "uptime_s", "current_task", "parent_id",
    }
    missing = expected_fields - set(data.keys())
    assert not missing, f"Missing fields in response: {missing}"


@pytest.mark.asyncio
async def test_get_agent_detail_includes_id_in_404_message(app) -> None:
    """404 detail includes the requested agent ID."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/agents/nonexistent-agent",
            headers={"X-Api-Key": _API_KEY},
        )
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "nonexistent-agent" in detail
