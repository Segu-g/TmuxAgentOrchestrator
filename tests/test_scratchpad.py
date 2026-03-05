"""Tests for GET/PUT /scratchpad/{key} — shared key-value store for agents.

The shared scratchpad is a simple in-process key-value store exposed via REST.
It allows agents (and the orchestrator) to share data without requiring file I/O
or direct inter-agent messaging.

Design reference:
- Blackboard pattern: Buschmann et al., "Pattern-Oriented Software Architecture
  Vol 1: A System of Patterns" (1996) — shared working memory for multiple agents
- DESIGN.md §11 (architecture): shared scratchpad (low priority)

Semantics:
- PUT /scratchpad/{key}  — write a value; returns {"key": ..., "updated": true}
- GET /scratchpad/{key}  — read a value; returns {"key": ..., "value": ...}
                          or 404 if key not found
- GET /scratchpad/       — list all keys and their values
- DELETE /scratchpad/{key} — delete a key; returns {"key": ..., "deleted": true}
- Keys are strings; values are arbitrary JSON (via Pydantic Any field)
- Auth: same X-API-Key / session cookie as other endpoints
- State is in-process (no disk persistence); cleared on server restart
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, AsyncMock

import tmux_orchestrator.web.app as web_app_mod
from tmux_orchestrator.web.app import create_app


_API_KEY = "scratchpad-test-key"


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _MockOrchestrator:
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

    @property
    def bus(self):
        b = MagicMock()
        b.subscribe = AsyncMock(return_value=MagicMock())
        b.unsubscribe = AsyncMock()
        return b


@pytest.fixture(autouse=True)
def reset_state():
    """Reset module-level state before each test."""
    web_app_mod._credentials.clear()
    web_app_mod._sign_counts.clear()
    web_app_mod._sessions.clear()
    # Reset scratchpad
    web_app_mod._scratchpad.clear()
    yield


@pytest.fixture()
def client():
    orch = _MockOrchestrator()
    hub = _MockHub()
    app = create_app(orch, hub, api_key=_API_KEY)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def auth_headers() -> dict:
    return {"X-API-Key": _API_KEY}


# ---------------------------------------------------------------------------
# GET /scratchpad/ — list all entries
# ---------------------------------------------------------------------------


def test_scratchpad_list_empty(client):
    resp = client.get("/scratchpad/", headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json() == {}


def test_scratchpad_list_after_put(client):
    client.put("/scratchpad/foo", json={"value": "bar"}, headers=auth_headers())
    client.put("/scratchpad/baz", json={"value": 42}, headers=auth_headers())
    resp = client.get("/scratchpad/", headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["foo"] == "bar"
    assert data["baz"] == 42


# ---------------------------------------------------------------------------
# PUT /scratchpad/{key} — write a value
# ---------------------------------------------------------------------------


def test_scratchpad_put_string(client):
    resp = client.put("/scratchpad/mykey", json={"value": "hello"}, headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "mykey"
    assert body["updated"] is True


def test_scratchpad_put_dict_value(client):
    resp = client.put(
        "/scratchpad/config",
        json={"value": {"x": 1, "y": [1, 2, 3]}},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] is True


def test_scratchpad_put_overwrites(client):
    client.put("/scratchpad/k", json={"value": "v1"}, headers=auth_headers())
    client.put("/scratchpad/k", json={"value": "v2"}, headers=auth_headers())
    resp = client.get("/scratchpad/k", headers=auth_headers())
    assert resp.json()["value"] == "v2"


# ---------------------------------------------------------------------------
# GET /scratchpad/{key} — read a value
# ---------------------------------------------------------------------------


def test_scratchpad_get_existing(client):
    client.put("/scratchpad/answer", json={"value": 42}, headers=auth_headers())
    resp = client.get("/scratchpad/answer", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "answer"
    assert body["value"] == 42


def test_scratchpad_get_missing_returns_404(client):
    resp = client.get("/scratchpad/nonexistent", headers=auth_headers())
    assert resp.status_code == 404


def test_scratchpad_get_complex_value(client):
    payload = {"items": [1, 2, 3], "meta": {"done": True}}
    client.put("/scratchpad/data", json={"value": payload}, headers=auth_headers())
    resp = client.get("/scratchpad/data", headers=auth_headers())
    assert resp.json()["value"] == payload


# ---------------------------------------------------------------------------
# DELETE /scratchpad/{key} — remove a key
# ---------------------------------------------------------------------------


def test_scratchpad_delete_existing(client):
    client.put("/scratchpad/tmp", json={"value": "x"}, headers=auth_headers())
    resp = client.delete("/scratchpad/tmp", headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    # Should be gone now
    assert client.get("/scratchpad/tmp", headers=auth_headers()).status_code == 404


def test_scratchpad_delete_missing_returns_404(client):
    resp = client.delete("/scratchpad/ghost", headers=auth_headers())
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


def test_scratchpad_put_requires_auth(client):
    resp = client.put("/scratchpad/k", json={"value": "v"})
    assert resp.status_code == 401


def test_scratchpad_get_requires_auth(client):
    resp = client.get("/scratchpad/k")
    assert resp.status_code == 401


def test_scratchpad_list_requires_auth(client):
    resp = client.get("/scratchpad/")
    assert resp.status_code == 401


def test_scratchpad_delete_requires_auth(client):
    resp = client.delete("/scratchpad/k")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Key name edge cases
# ---------------------------------------------------------------------------


def test_scratchpad_key_with_hyphen(client):
    client.put("/scratchpad/my-key", json={"value": "val"}, headers=auth_headers())
    resp = client.get("/scratchpad/my-key", headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["value"] == "val"


def test_scratchpad_key_with_underscore(client):
    client.put("/scratchpad/my_key", json={"value": 99}, headers=auth_headers())
    assert client.get("/scratchpad/my_key", headers=auth_headers()).json()["value"] == 99


def test_scratchpad_multiple_keys_independent(client):
    client.put("/scratchpad/a", json={"value": 1}, headers=auth_headers())
    client.put("/scratchpad/b", json={"value": 2}, headers=auth_headers())
    assert client.get("/scratchpad/a", headers=auth_headers()).json()["value"] == 1
    assert client.get("/scratchpad/b", headers=auth_headers()).json()["value"] == 2
