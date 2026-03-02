"""Tests for web app authentication endpoints."""

from __future__ import annotations

import time

import httpx
import pytest

import tmux_orchestrator.web.app as web_app_mod
from tmux_orchestrator.web.app import create_app

_API_KEY = "test-key-xyz"


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def handle(self, ws) -> None:
        pass


class _MockOrchestrator:
    _agents: dict = {}
    _director_pending: list = []

    def list_agents(self) -> list:
        return []

    def list_tasks(self) -> list:
        return []

    def get_agent(self, agent_id: str):
        return None


@pytest.fixture(autouse=True)
def reset_state():
    """Reset module-level auth state before each test."""
    web_app_mod._credentials.clear()
    web_app_mod._sign_counts.clear()
    web_app_mod._sessions.clear()
    web_app_mod._pending_challenge = None
    yield


@pytest.fixture
def app():
    return create_app(_MockOrchestrator(), _MockHub(), api_key=_API_KEY)


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        yield c


async def test_auth_status_initial(client):
    r = await client.get("/auth/status")
    assert r.status_code == 200
    data = r.json()
    assert data["registered"] is False
    assert data["authenticated"] is False


async def test_get_root_always_returns_html(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def test_protected_without_auth_returns_401(client):
    r = await client.get("/agents")
    assert r.status_code == 401


async def test_api_key_auth_works(client):
    r = await client.get("/agents", headers={"X-API-Key": _API_KEY})
    assert r.status_code == 200


async def test_api_key_wrong_returns_401(client):
    r = await client.get("/agents", headers={"X-API-Key": "wrong-key"})
    assert r.status_code == 401


async def test_session_cookie_auth_works(client):
    token = "valid-session-token"
    web_app_mod._sessions[token] = time.time() + 3600
    r = await client.get("/agents", cookies={"session": token})
    assert r.status_code == 200


async def test_session_expired_returns_401(client):
    token = "expired-token"
    web_app_mod._sessions[token] = time.time() - 1  # already expired
    r = await client.get("/agents", cookies={"session": token})
    assert r.status_code == 401


async def test_logout_clears_session_cookie(client):
    token = "active-token"
    web_app_mod._sessions[token] = time.time() + 3600
    r = await client.post("/auth/logout", cookies={"session": token})
    assert r.status_code == 200
    # Cookie should be cleared (Max-Age=0 or blank value)
    set_cookie = r.headers.get("set-cookie", "")
    assert "session=" in set_cookie
    assert "max-age=0" in set_cookie.lower()


async def test_register_options_returns_challenge(client):
    r = await client.post("/auth/register-options")
    assert r.status_code == 200
    data = r.json()
    assert "challenge" in data
    assert "rp" in data
    assert data["rp"]["id"] == "localhost"


async def test_authenticate_options_no_creds_returns_empty(client):
    r = await client.post("/auth/authenticate-options")
    assert r.status_code == 200
    data = r.json()
    assert data.get("allowCredentials", []) == []
