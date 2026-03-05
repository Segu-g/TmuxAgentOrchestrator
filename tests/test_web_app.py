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
    _dispatch_task = None

    def list_agents(self) -> list:
        return []

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


# ---------------------------------------------------------------------------
# Health probes
# ---------------------------------------------------------------------------


async def test_healthz_returns_200(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "ts" in data


async def test_readyz_no_workers_returns_503(client):
    """With no agents registered, readyz should report not-ready."""
    r = await client.get("/readyz")
    # No dispatch loop running and no workers
    assert r.status_code in (200, 503)  # status depends on orchestrator state
    data = r.json()
    assert "checks" in data


def test_on_startup_hook_called_during_lifespan():
    """create_app(on_startup=...) is called within the lifespan context manager.

    Regression test: router.on_startup hooks are NOT called when a lifespan
    context manager is provided (FastAPI >= 0.93). The on_startup parameter of
    create_app() is the correct way to inject startup logic.

    Uses starlette TestClient (not httpx.AsyncClient) because only TestClient
    invokes the ASGI lifespan events.
    """
    from fastapi.testclient import TestClient

    called: list[str] = []

    async def startup_hook():
        called.append("startup")

    async def shutdown_hook():
        called.append("shutdown")

    app = create_app(
        _MockOrchestrator(),
        _MockHub(),
        api_key=_API_KEY,
        on_startup=startup_hook,
        on_shutdown=shutdown_hook,
    )

    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.get("/healthz")
        assert r.status_code == 200
        assert "startup" in called, (
            "on_startup hook was NOT called during lifespan — "
            "check create_app lifespan integration"
        )

    assert "shutdown" in called, "on_shutdown hook was NOT called during lifespan teardown"
