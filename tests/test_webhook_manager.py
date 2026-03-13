"""Tests for WebhookManager — outbound event webhook delivery.

Tests cover:
- Register, list, get, delete webhooks
- deliver() fires for matching event, skips non-matching
- Wildcard '*' receives all events
- HMAC signature computed correctly
- No HMAC header when secret is None
- Delivery recorded (delivery_count, failure_count)
- HTTP 200 → success=True
- HTTP 500 → success=False, failure_count incremented
- Connection timeout → success=False, error recorded
- last_deliveries() returns last 20 (not more)
- Multiple webhooks: all matching ones fired
- REST POST /webhooks — success, invalid event 422
- REST GET /webhooks — list
- REST DELETE /webhooks/{id} — success, 404
- REST GET /webhooks/{id}/deliveries — delivery history

Design reference: DESIGN.md §10.25 (v0.30.0).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import tmux_orchestrator.web.app as web_app_mod
from tmux_orchestrator.webhook_manager import KNOWN_EVENTS, Webhook, WebhookDelivery, WebhookManager
from tmux_orchestrator.web.app import create_app


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def manager():
    return WebhookManager(timeout=5.0)


def _make_mock_response(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    return resp


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def handle(self, ws) -> None:
        pass


class _MockOrchestrator:
    _dispatch_task = None

    def __init__(self):
        self._webhook_manager = WebhookManager()

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

    def get_rate_limiter_status(self) -> dict:
        return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}

    def reconfigure_rate_limiter(self, *, rate: float, burst: int) -> dict:
        return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}

    def get_workflow_manager(self):
        from tmux_orchestrator.workflow_manager import WorkflowManager
        return WorkflowManager()


@pytest.fixture(autouse=True)
def reset_auth_state():
    web_app_mod._credentials.clear()
    web_app_mod._sign_counts.clear()
    web_app_mod._sessions.clear()
    web_app_mod._pending_challenge = None
    yield


@pytest.fixture
def orchestrator():
    return _MockOrchestrator()


@pytest.fixture
def app(orchestrator):
    return create_app(orchestrator, _MockHub(), api_key="test-key")


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "test-key"},
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Unit tests: WebhookManager CRUD
# ---------------------------------------------------------------------------


def test_register_webhook(manager):
    """Registering a webhook returns a Webhook with correct fields."""
    wh = manager.register(url="https://example.com/hook", events=["task_complete"])
    assert wh.id
    assert wh.url == "https://example.com/hook"
    assert wh.events == ["task_complete"]
    assert wh.secret is None
    assert wh.created_at > 0
    assert wh.delivery_count == 0
    assert wh.failure_count == 0


def test_register_webhook_with_secret(manager):
    """Registering a webhook with a secret stores it."""
    wh = manager.register(url="https://a.com", events=["task_failed"], secret="mysecret")
    assert wh.secret == "mysecret"


def test_list_webhooks(manager):
    """list_all() returns all registered webhooks."""
    manager.register(url="https://a.com", events=["task_complete"])
    manager.register(url="https://b.com", events=["task_failed"])
    all_wh = manager.list_all()
    assert len(all_wh) == 2
    urls = {w.url for w in all_wh}
    assert urls == {"https://a.com", "https://b.com"}


def test_get_webhook(manager):
    """get() returns the webhook by ID."""
    wh = manager.register(url="https://c.com", events=["*"])
    found = manager.get(wh.id)
    assert found is wh


def test_get_webhook_not_found(manager):
    """get() returns None for unknown ID."""
    assert manager.get("nonexistent-uuid") is None


def test_unregister_webhook(manager):
    """unregister() removes the webhook and returns True."""
    wh = manager.register(url="https://d.com", events=["task_complete"])
    removed = manager.unregister(wh.id)
    assert removed is True
    assert manager.get(wh.id) is None


def test_unregister_nonexistent(manager):
    """unregister() returns False when ID not found."""
    assert manager.unregister("does-not-exist") is False


# ---------------------------------------------------------------------------
# Unit tests: HMAC signing
# ---------------------------------------------------------------------------


def test_sign_produces_sha256_prefix(manager):
    """_sign() returns a string starting with 'sha256='."""
    sig = WebhookManager._sign(b"hello", "secret")
    assert sig.startswith("sha256=")


def test_sign_hmac_correctness():
    """_sign() computes the correct HMAC-SHA256 digest."""
    body = b'{"event":"test"}'
    secret = "mysecret"
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert WebhookManager._sign(body, secret) == expected


def test_sign_different_secrets_differ():
    """Different secrets produce different signatures."""
    body = b"same body"
    assert WebhookManager._sign(body, "secret1") != WebhookManager._sign(body, "secret2")


# ---------------------------------------------------------------------------
# Unit tests: deliver() — mock HTTP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_fires_for_matching_event(manager):
    """deliver() POSTs to webhooks subscribed to the event."""
    wh = manager.register(url="https://hook.example.com", events=["task_complete"])

    mock_resp = _make_mock_response(200)
    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
        await manager.deliver("task_complete", {"task_id": "t1"})
        # Allow background tasks to complete
        await asyncio.sleep(0.1)

    assert wh.delivery_count == 1
    assert wh.failure_count == 0


@pytest.mark.asyncio
async def test_deliver_skips_non_matching_event(manager):
    """deliver() does NOT POST to webhooks not subscribed to the event."""
    wh = manager.register(url="https://hook.example.com", events=["task_failed"])

    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
        await manager.deliver("task_complete", {"task_id": "t1"})
        await asyncio.sleep(0.05)
        mock_post.assert_not_called()

    assert wh.delivery_count == 0


@pytest.mark.asyncio
async def test_deliver_wildcard_receives_all_events(manager):
    """Wildcard '*' webhook receives every event."""
    wh = manager.register(url="https://all.example.com", events=["*"])

    mock_resp = _make_mock_response(200)
    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
        await manager.deliver("task_complete", {"task_id": "t1"})
        await manager.deliver("agent_status", {"agent_id": "w1"})
        await asyncio.sleep(0.1)

    assert wh.delivery_count == 2


@pytest.mark.asyncio
async def test_deliver_no_signature_without_secret(manager):
    """When secret is None, no X-Signature-SHA256 header is sent."""
    manager.register(url="https://hook.example.com", events=["task_complete"])

    mock_resp = _make_mock_response(200)
    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
        await manager.deliver("task_complete", {"task_id": "t1"})
        await asyncio.sleep(0.1)

    call_kwargs = mock_post.call_args[1]
    headers = call_kwargs.get("headers", {})
    assert "X-Signature-SHA256" not in headers


@pytest.mark.asyncio
async def test_deliver_includes_signature_with_secret(manager):
    """When secret is set, X-Signature-SHA256 header is included."""
    manager.register(url="https://hook.example.com", events=["task_complete"], secret="s3cret")

    mock_resp = _make_mock_response(200)
    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
        await manager.deliver("task_complete", {"task_id": "t1"})
        await asyncio.sleep(0.1)

    call_kwargs = mock_post.call_args[1]
    headers = call_kwargs.get("headers", {})
    assert "X-Signature-SHA256" in headers
    assert headers["X-Signature-SHA256"].startswith("sha256=")


@pytest.mark.asyncio
async def test_deliver_http_200_success(manager):
    """HTTP 200 response → success=True, failure_count stays 0."""
    wh = manager.register(url="https://hook.example.com", events=["task_complete"])
    mock_resp = _make_mock_response(200)

    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
        await manager.deliver("task_complete", {"task_id": "t1"})
        await asyncio.sleep(0.1)

    assert wh.delivery_count == 1
    assert wh.failure_count == 0
    deliveries = manager.last_deliveries(wh.id)
    assert len(deliveries) == 1
    assert deliveries[0].success is True
    assert deliveries[0].status_code == 200


@pytest.mark.asyncio
async def test_deliver_http_500_failure(manager):
    """HTTP 500 response → success=False, failure_count incremented."""
    # Register with max_retries=0 so the test completes quickly without retries.
    wh = manager.register(
        url="https://hook.example.com", events=["task_complete"], max_retries=0
    )
    mock_resp = _make_mock_response(500)

    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
        await manager.deliver("task_complete", {"task_id": "t1"})
        await asyncio.sleep(0.1)

    assert wh.delivery_count == 1
    assert wh.failure_count == 1
    deliveries = manager.last_deliveries(wh.id)
    assert deliveries[0].success is False
    assert deliveries[0].status_code == 500


@pytest.mark.asyncio
async def test_deliver_connection_error_recorded(manager):
    """Connection error → success=False, error field set."""
    # Register with max_retries=0 so the test completes quickly without retries.
    wh = manager.register(
        url="https://hook.example.com", events=["task_complete"], max_retries=0
    )

    with patch.object(
        httpx.AsyncClient,
        "post",
        new_callable=AsyncMock,
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        await manager.deliver("task_complete", {"task_id": "t1"})
        await asyncio.sleep(0.1)

    assert wh.delivery_count == 1
    assert wh.failure_count == 1
    deliveries = manager.last_deliveries(wh.id)
    assert deliveries[0].success is False
    assert deliveries[0].error is not None
    assert deliveries[0].status_code is None


@pytest.mark.asyncio
async def test_last_deliveries_returns_at_most_n(manager):
    """last_deliveries(n=20) returns at most 20 entries."""
    wh = manager.register(url="https://hook.example.com", events=["task_complete"])
    mock_resp = _make_mock_response(200)

    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
        for _ in range(30):
            await manager.deliver("task_complete", {"task_id": "t1"})
        await asyncio.sleep(0.3)

    deliveries = manager.last_deliveries(wh.id, n=20)
    assert len(deliveries) <= 20


@pytest.mark.asyncio
async def test_last_deliveries_circular_buffer(manager):
    """Circular buffer caps at 50 entries; oldest are evicted."""
    wh = manager.register(url="https://hook.example.com", events=["task_complete"])
    mock_resp = _make_mock_response(200)

    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
        for _ in range(60):
            await manager.deliver("task_complete", {"task_id": "t1"})
        await asyncio.sleep(0.6)

    # Buffer is capped at 50
    assert len(wh._deliveries) == 50
    # delivery_count reflects total (not just buffer size)
    assert wh.delivery_count == 60


@pytest.mark.asyncio
async def test_multiple_webhooks_all_matching_fired(manager):
    """Multiple webhooks subscribed to same event — all receive delivery."""
    wh1 = manager.register(url="https://a.example.com", events=["task_complete"])
    wh2 = manager.register(url="https://b.example.com", events=["task_complete"])
    mock_resp = _make_mock_response(200)

    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
        await manager.deliver("task_complete", {"task_id": "t1"})
        await asyncio.sleep(0.1)

    assert wh1.delivery_count == 1
    assert wh2.delivery_count == 1


# ---------------------------------------------------------------------------
# REST tests: /webhooks endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_webhooks_success(client, orchestrator):
    """POST /webhooks — successful registration returns webhook details."""
    resp = await client.post("/webhooks", json={
        "url": "https://example.com/hook",
        "events": ["task_complete", "task_failed"],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["url"] == "https://example.com/hook"
    assert "task_complete" in data["events"]
    assert "created_at" in data


@pytest.mark.asyncio
async def test_post_webhooks_invalid_event_422(client):
    """POST /webhooks with unknown event name → 422 Unprocessable Entity."""
    resp = await client.post("/webhooks", json={
        "url": "https://example.com/hook",
        "events": ["nonexistent_event"],
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_webhooks_list(client, orchestrator):
    """GET /webhooks returns the list of registered webhooks."""
    # Register one webhook
    await client.post("/webhooks", json={
        "url": "https://example.com/hook",
        "events": ["task_complete"],
    })
    resp = await client.get("/webhooks")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert any(w["url"] == "https://example.com/hook" for w in data)


@pytest.mark.asyncio
async def test_delete_webhook_success(client, orchestrator):
    """DELETE /webhooks/{id} removes the webhook."""
    create_resp = await client.post("/webhooks", json={
        "url": "https://del.example.com",
        "events": ["task_complete"],
    })
    wh_id = create_resp.json()["id"]

    del_resp = await client.delete(f"/webhooks/{wh_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True

    # Verify it's gone
    list_resp = await client.get("/webhooks")
    ids = [w["id"] for w in list_resp.json()]
    assert wh_id not in ids


@pytest.mark.asyncio
async def test_delete_webhook_not_found(client):
    """DELETE /webhooks/{id} with unknown ID → 404."""
    resp = await client.delete("/webhooks/nonexistent-uuid")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_webhook_deliveries(client, orchestrator):
    """GET /webhooks/{id}/deliveries returns delivery history."""
    create_resp = await client.post("/webhooks", json={
        "url": "https://log.example.com",
        "events": ["task_complete"],
    })
    wh_id = create_resp.json()["id"]

    # Manually inject a delivery record
    wh = orchestrator._webhook_manager.get(wh_id)
    from tmux_orchestrator.webhook_manager import WebhookDelivery
    import time
    wh._deliveries.append(WebhookDelivery(
        id=str(uuid.uuid4()),
        webhook_id=wh_id,
        event="task_complete",
        timestamp=time.time(),
        success=True,
        status_code=200,
        error=None,
        duration_ms=42.0,
    ))

    resp = await client.get(f"/webhooks/{wh_id}/deliveries")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["event"] == "task_complete"
    assert data[0]["success"] is True


@pytest.mark.asyncio
async def test_get_webhook_deliveries_not_found(client):
    """GET /webhooks/{id}/deliveries with unknown ID → 404."""
    resp = await client.get("/webhooks/nonexistent-uuid/deliveries")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


def test_to_dict_no_secret_field(manager):
    """to_dict() does not expose the secret field."""
    wh = manager.register(url="https://x.com", events=["task_complete"], secret="topsecret")
    d = wh.to_dict()
    assert "secret" not in d
    assert d["url"] == "https://x.com"


def test_known_events_contains_wildcard():
    """KNOWN_EVENTS includes the '*' wildcard."""
    assert "*" in KNOWN_EVENTS


def test_known_events_complete():
    """KNOWN_EVENTS contains all documented event names."""
    expected = {
        "task_complete", "task_failed", "task_retrying", "task_cancelled",
        "task_dependency_failed", "task_waiting", "agent_status",
        "workflow_complete", "workflow_failed", "workflow_cancelled",
        "phase_complete", "phase_failed", "phase_skipped",
        "*",
    }
    assert expected == KNOWN_EVENTS


@pytest.mark.asyncio
async def test_deliver_empty_webhooks_no_error(manager):
    """deliver() with no registered webhooks does not raise."""
    # Should return without error
    await manager.deliver("task_complete", {"task_id": "t1"})


@pytest.mark.asyncio
async def test_deliver_sends_correct_payload(manager):
    """deliver() sends JSON with event, timestamp, and data fields."""
    manager.register(url="https://payload-check.example.com", events=["task_complete"])
    mock_resp = _make_mock_response(200)

    sent_body: list[bytes] = []

    async def capture_post(url, *, content, headers, **kwargs):
        sent_body.append(content)
        return mock_resp

    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, side_effect=capture_post):
        await manager.deliver("task_complete", {"task_id": "abc123"})
        await asyncio.sleep(0.1)

    assert len(sent_body) == 1
    payload = json.loads(sent_body[0])
    assert payload["event"] == "task_complete"
    assert "timestamp" in payload
    assert payload["data"]["task_id"] == "abc123"
