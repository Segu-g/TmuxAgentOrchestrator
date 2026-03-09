"""Tests for WebhookManager retry with exponential backoff.

Covers:
- Success on first attempt: no retries
- Failure then success (fail 2, succeed on 3rd): 3 total HTTP calls
- All retries exhausted (all 500): failure recorded, N+1 total calls
- max_retries=0: fire-and-forget, exactly 1 call
- _backoff_sleep: result is within expected equal-jitter range
- retries field on WebhookDelivery records actual retry count

Strategy: tests call `_send()` directly (instead of going through `deliver()`) to
avoid background-task timing issues.  `deliver()` is integration-tested separately.

Reference: DESIGN.md §10.N (v1.0.22 — webhook retry backoff)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import httpx
import pytest

from tmux_orchestrator.webhook_manager import WebhookManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    return resp


# ---------------------------------------------------------------------------
# _backoff_sleep unit tests
# ---------------------------------------------------------------------------


def test_backoff_sleep_within_equal_jitter_range():
    """_backoff_sleep returns a value in [cap/2, cap] for attempt=0, base=1."""
    # attempt=0, base=1 → cap=min(60, 1*2^0)=1 → range [0.5, 1.0]
    for _ in range(100):
        val = WebhookManager._backoff_sleep(0, 1.0)
        assert 0.5 <= val <= 1.0, f"val={val} out of [0.5, 1.0]"


def test_backoff_sleep_capped_at_60s():
    """_backoff_sleep caps at 60 seconds regardless of attempt."""
    # attempt=20, base=1 → 2^20 >> 60 → cap=60 → range [30, 60]
    for _ in range(50):
        val = WebhookManager._backoff_sleep(20, 1.0)
        assert 30.0 <= val <= 60.0, f"val={val} out of [30, 60]"


def test_backoff_sleep_increases_with_attempt():
    """Higher attempt numbers produce higher average sleep."""
    import statistics
    samples_0 = [WebhookManager._backoff_sleep(0, 1.0) for _ in range(200)]
    samples_3 = [WebhookManager._backoff_sleep(3, 1.0) for _ in range(200)]
    assert statistics.mean(samples_3) > statistics.mean(samples_0)


# ---------------------------------------------------------------------------
# Integration tests: retry behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_on_first_attempt_no_retry():
    """If the first HTTP POST succeeds, no retries occur."""
    manager = WebhookManager(timeout=1.0, max_retries=3, retry_backoff_base=0.01)
    wh = manager.register(url="https://hook.test/ok", events=["task_complete"])

    mock_resp = _make_response(200)
    call_count = 0

    async def fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return mock_resp

    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, side_effect=fake_post):
        await manager.deliver("task_complete", {"x": 1})
        await asyncio.sleep(0.1)

    assert call_count == 1
    assert wh.delivery_count == 1
    assert wh.failure_count == 0
    deliveries = manager.last_deliveries(wh.id)
    assert deliveries[0].retries == 0
    assert deliveries[0].success is True


@pytest.mark.asyncio
async def test_fail_twice_then_succeed():
    """When the first 2 attempts fail and the 3rd succeeds, 3 HTTP calls are made."""
    manager = WebhookManager(timeout=1.0, max_retries=3, retry_backoff_base=0.001)
    wh = manager.register(url="https://hook.test/flaky", events=["task_complete"])

    responses = [_make_response(500), _make_response(500), _make_response(200)]
    call_count = 0

    async def fake_post(*args, **kwargs):
        nonlocal call_count
        resp = responses[min(call_count, len(responses) - 1)]
        call_count += 1
        return resp

    # Use _send directly to avoid background-task timing issues.
    body = b'{"event":"task_complete","data":{}}'
    with patch("tmux_orchestrator.webhook_manager.asyncio.sleep", new_callable=AsyncMock):
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, side_effect=fake_post):
            await manager._send(wh, "task_complete", body)

    assert call_count == 3
    assert wh.delivery_count == 1
    assert wh.failure_count == 0
    deliveries = manager.last_deliveries(wh.id)
    assert deliveries[0].retries == 2
    assert deliveries[0].success is True
    assert deliveries[0].status_code == 200


@pytest.mark.asyncio
async def test_all_retries_exhausted():
    """When all attempts fail, failure_count increments and delivery is recorded."""
    manager = WebhookManager(timeout=1.0, max_retries=2, retry_backoff_base=0.001)
    wh = manager.register(url="https://hook.test/down", events=["task_complete"])

    call_count = 0

    async def fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _make_response(503)

    body = b'{"event":"task_complete","data":{}}'
    with patch("tmux_orchestrator.webhook_manager.asyncio.sleep", new_callable=AsyncMock):
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, side_effect=fake_post):
            await manager._send(wh, "task_complete", body)

    # max_retries=2 → 1 initial + 2 retries = 3 total
    assert call_count == 3
    assert wh.delivery_count == 1
    assert wh.failure_count == 1
    deliveries = manager.last_deliveries(wh.id)
    assert deliveries[0].retries == 2
    assert deliveries[0].success is False
    assert deliveries[0].status_code == 503


@pytest.mark.asyncio
async def test_max_retries_zero_no_retry():
    """max_retries=0 means fire-and-forget: exactly 1 HTTP call even on failure."""
    manager = WebhookManager(timeout=1.0, max_retries=0, retry_backoff_base=1.0)
    wh = manager.register(url="https://hook.test/noretry", events=["task_complete"])

    call_count = 0

    async def fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _make_response(500)

    body = b'{"event":"task_complete","data":{}}'
    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, side_effect=fake_post):
        await manager._send(wh, "task_complete", body)

    assert call_count == 1
    assert wh.failure_count == 1
    deliveries = manager.last_deliveries(wh.id)
    assert deliveries[0].retries == 0


@pytest.mark.asyncio
async def test_connection_error_retried():
    """Connection errors are treated as transient and retried."""
    manager = WebhookManager(timeout=1.0, max_retries=2, retry_backoff_base=0.001)
    wh = manager.register(url="https://hook.test/connect-err", events=["task_complete"])

    call_count = 0

    async def fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.ConnectError("Connection refused")
        return _make_response(200)

    body = b'{"event":"task_complete","data":{}}'
    with patch("tmux_orchestrator.webhook_manager.asyncio.sleep", new_callable=AsyncMock):
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, side_effect=fake_post):
            await manager._send(wh, "task_complete", body)

    assert call_count == 3
    assert wh.delivery_count == 1
    assert wh.failure_count == 0
    deliveries = manager.last_deliveries(wh.id)
    assert deliveries[0].success is True
    assert deliveries[0].retries == 2


@pytest.mark.asyncio
async def test_per_webhook_retry_config():
    """Per-webhook max_retries overrides manager default."""
    manager = WebhookManager(timeout=1.0, max_retries=10, retry_backoff_base=0.001)
    # Register with override: only 1 retry
    wh = manager.register(
        url="https://hook.test/limited",
        events=["task_complete"],
        max_retries=1,
        retry_backoff_base=0.001,
    )
    assert wh.max_retries == 1

    call_count = 0

    async def fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _make_response(500)

    body = b'{"event":"task_complete","data":{}}'
    with patch("tmux_orchestrator.webhook_manager.asyncio.sleep", new_callable=AsyncMock):
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, side_effect=fake_post):
            await manager._send(wh, "task_complete", body)

    # max_retries=1 → 1 initial + 1 retry = 2 total
    assert call_count == 2
    assert wh.failure_count == 1


@pytest.mark.asyncio
async def test_sleep_called_between_retries():
    """asyncio.sleep is called once per retry (not before initial attempt)."""
    manager = WebhookManager(timeout=1.0, max_retries=2, retry_backoff_base=0.001)
    wh = manager.register(url="https://hook.test/sleep", events=["task_complete"])

    async def fake_post(*args, **kwargs):
        return _make_response(500)

    sleep_calls = []

    async def fake_sleep(duration):
        sleep_calls.append(duration)

    body = b'{"event":"task_complete","data":{}}'
    with patch("tmux_orchestrator.webhook_manager.asyncio.sleep", side_effect=fake_sleep):
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, side_effect=fake_post):
            await manager._send(wh, "task_complete", body)

    # 2 retries → 2 sleeps; the initial attempt has no preceding sleep
    assert len(sleep_calls) == 2


def test_to_dict_includes_retry_config():
    """Webhook.to_dict() exposes max_retries and retry_backoff_base."""
    manager = WebhookManager(max_retries=5, retry_backoff_base=2.0)
    wh = manager.register(url="https://x.com", events=["*"])
    d = wh.to_dict()
    assert d["max_retries"] == 5
    assert d["retry_backoff_base"] == 2.0
