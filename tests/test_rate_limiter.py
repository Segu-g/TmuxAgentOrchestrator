"""Tests for token-bucket rate limiter on task submission.

Feature: Rate limiting / backpressure for task submission
- TokenBucketRateLimiter class with async token acquisition
- Orchestrator.submit_task honours rate limiter when configured
- REST endpoints: GET /rate-limit (status), PUT /rate-limit (reconfigure)
- RateLimitExceeded raised when rate limit is exceeded and wait=False

Design references:
- Tanenbaum, A.S. "Computer Networks" 5th ed. §5.3 — Token Bucket algorithm
- RFC 4115 "A Differentiated Service Two-Rate, Three-Color Marker with Efficient Handling of in-Profile Traffic" (2005)
- aiolimiter v1.2.1: Leaky Bucket for asyncio (2024)
- NGINX rate_limit_zone / limit_req (2025)
- DESIGN.md §11 (v0.20.0)
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tmux_orchestrator.agents.base import AgentStatus
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.rate_limiter import RateLimitExceeded, TokenBucketRateLimiter
from tmux_orchestrator.web.app import create_app


# ---------------------------------------------------------------------------
# Unit tests: TokenBucketRateLimiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_bucket_allows_burst() -> None:
    """A fresh bucket allows up to `burst` acquisitions immediately."""
    rl = TokenBucketRateLimiter(rate=1.0, burst=5)
    for _ in range(5):
        acquired = rl.try_acquire()
        assert acquired is True


@pytest.mark.asyncio
async def test_token_bucket_denies_when_empty() -> None:
    """try_acquire returns False once the bucket is empty."""
    rl = TokenBucketRateLimiter(rate=1.0, burst=3)
    for _ in range(3):
        rl.try_acquire()
    # Bucket empty now
    assert rl.try_acquire() is False


@pytest.mark.asyncio
async def test_token_bucket_refills_over_time() -> None:
    """Tokens refill at the configured rate per second."""
    rl = TokenBucketRateLimiter(rate=10.0, burst=1)
    # Drain
    assert rl.try_acquire() is True
    assert rl.try_acquire() is False  # empty
    # Advance internal clock by 0.2 s (2 tokens at 10/s, capped at burst=1)
    rl._last_refill -= 0.2
    rl._refill()
    assert rl.try_acquire() is True  # one token refilled


@pytest.mark.asyncio
async def test_token_bucket_acquire_waits() -> None:
    """acquire() waits until a token is available (async)."""
    rl = TokenBucketRateLimiter(rate=20.0, burst=1)  # fast refill
    rl.try_acquire()  # drain
    start = time.monotonic()
    await rl.acquire()  # should wait ~1/20s = 0.05s
    elapsed = time.monotonic() - start
    assert elapsed >= 0.01  # at least waited a bit


@pytest.mark.asyncio
async def test_token_bucket_acquire_raises_when_timeout() -> None:
    """acquire(timeout=...) raises RateLimitExceeded when wait exceeds timeout."""
    rl = TokenBucketRateLimiter(rate=0.1, burst=1)  # very slow refill (1 per 10s)
    rl.try_acquire()  # drain
    with pytest.raises(RateLimitExceeded):
        await rl.acquire(timeout=0.05)  # 50ms timeout, but needs 10s to refill


def test_token_bucket_status() -> None:
    """status() returns correct dict with rate, burst, available_tokens."""
    rl = TokenBucketRateLimiter(rate=5.0, burst=10)
    status = rl.status()
    assert status["rate"] == 5.0
    assert status["burst"] == 10
    assert "available_tokens" in status
    assert "enabled" in status
    assert status["enabled"] is True


def test_token_bucket_reconfigure() -> None:
    """reconfigure() updates rate and burst."""
    rl = TokenBucketRateLimiter(rate=1.0, burst=5)
    rl.reconfigure(rate=10.0, burst=20)
    s = rl.status()
    assert s["rate"] == 10.0
    assert s["burst"] == 20


def test_token_bucket_disabled() -> None:
    """rate=0 means unlimited — try_acquire always returns True."""
    rl = TokenBucketRateLimiter(rate=0.0, burst=0)
    for _ in range(1000):
        assert rl.try_acquire() is True


# ---------------------------------------------------------------------------
# Unit tests: Orchestrator.submit_task + rate limiter
# ---------------------------------------------------------------------------


def make_tmux_mock():
    tmux = MagicMock()
    tmux.new_window.return_value = MagicMock()
    tmux.new_subpane.return_value = MagicMock()
    return tmux


def make_config(**kwargs):
    from tmux_orchestrator.config import OrchestratorConfig
    return OrchestratorConfig(**kwargs)


@pytest.mark.asyncio
async def test_orchestrator_rate_limiter_allows_burst() -> None:
    """Tasks within burst limit are accepted."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    # Set a generous rate limiter
    orch.set_rate_limiter(TokenBucketRateLimiter(rate=100.0, burst=5))

    for i in range(5):
        task = await orch.submit_task(f"task {i}")
        assert task.id is not None


@pytest.mark.asyncio
async def test_orchestrator_rate_limiter_raises_when_exceeded() -> None:
    """submit_task raises RateLimitExceeded when bucket is empty and wait=False."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    # Tight rate limiter: burst=2, very slow refill
    orch.set_rate_limiter(TokenBucketRateLimiter(rate=0.01, burst=2))

    # First two should succeed
    await orch.submit_task("task 1")
    await orch.submit_task("task 2")
    # Third should raise
    with pytest.raises(RateLimitExceeded):
        await orch.submit_task("task 3", wait_for_token=False)


@pytest.mark.asyncio
async def test_orchestrator_no_rate_limiter_unlimited() -> None:
    """Without rate limiter, submit_task is unlimited (default behavior)."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    # No rate limiter set (default)
    for i in range(20):
        task = await orch.submit_task(f"task {i}")
        assert task.id is not None


@pytest.mark.asyncio
async def test_orchestrator_rate_limiter_publishes_status_event() -> None:
    """When rate limit is exceeded, a rate_limit_exceeded STATUS event is published."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    orch.set_rate_limiter(TokenBucketRateLimiter(rate=0.01, burst=1))

    from tmux_orchestrator.bus import MessageType
    events = []
    q = await bus.subscribe("test-rl-listener", broadcast=True)

    async def collect():
        while True:
            msg = await q.get()
            if msg.type == MessageType.STATUS:
                events.append(msg.payload)
            q.task_done()

    collector = asyncio.create_task(collect())

    await orch.submit_task("allowed task")
    try:
        await orch.submit_task("rejected task", wait_for_token=False)
    except RateLimitExceeded:
        pass

    await asyncio.sleep(0.05)
    collector.cancel()
    await asyncio.gather(collector, return_exceptions=True)
    await bus.unsubscribe("test-rl-listener")

    rl_events = [e for e in events if e.get("event") == "rate_limit_exceeded"]
    assert len(rl_events) == 1
    assert rl_events[0]["prompt"] == "rejected task"


# ---------------------------------------------------------------------------
# Web endpoint tests: GET /rate-limit, PUT /rate-limit
# ---------------------------------------------------------------------------


_API_KEY = "test-key-rl"


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def handle(self, ws) -> None:
        pass


class _MockOrchestratorRL:
    """Mock orchestrator that exposes rate limiter state."""

    _agents: dict = {}
    _director_pending: list = []
    _dispatch_task = None
    _paused: bool = False
    _task_started_at: dict = {}
    _completed_tasks: set = set()
    _rate_limiter: TokenBucketRateLimiter | None = None

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
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    async def update_task_priority(self, task_id: str, new_priority: int) -> bool:
        return False

    async def cancel_task(self, task_id: str) -> bool:
        return False

    def get_rate_limiter_status(self) -> dict:
        if self._rate_limiter is None:
            return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}
        return self._rate_limiter.status()

    def set_rate_limiter(self, rl: TokenBucketRateLimiter) -> None:
        self._rate_limiter = rl

    def reconfigure_rate_limiter(self, rate: float, burst: int) -> dict:
        if self._rate_limiter is None:
            self._rate_limiter = TokenBucketRateLimiter(rate=rate, burst=burst)
        else:
            self._rate_limiter.reconfigure(rate=rate, burst=burst)
        return self._rate_limiter.status()


@pytest.fixture
def app_rl():
    return create_app(_MockOrchestratorRL(), _MockHub(), api_key=_API_KEY)


@pytest.fixture
async def client_rl(app_rl):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_rl),
        base_url="http://localhost",
        headers={"X-API-Key": _API_KEY},
    ) as c:
        yield c


async def test_get_rate_limit_returns_status(client_rl) -> None:
    r = await client_rl.get("/rate-limit")
    assert r.status_code == 200
    data = r.json()
    assert "enabled" in data
    assert "rate" in data
    assert "burst" in data
    assert "available_tokens" in data


async def test_put_rate_limit_reconfigures(client_rl) -> None:
    r = await client_rl.put("/rate-limit", json={"rate": 5.0, "burst": 10})
    assert r.status_code == 200
    data = r.json()
    assert data["rate"] == 5.0
    assert data["burst"] == 10
    assert data["enabled"] is True


async def test_put_rate_limit_disable(client_rl) -> None:
    """Setting rate=0 disables the limiter."""
    r = await client_rl.put("/rate-limit", json={"rate": 0.0, "burst": 0})
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is False


async def test_get_rate_limit_requires_auth(app_rl) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_rl),
        base_url="http://localhost",
    ) as c:
        r = await c.get("/rate-limit")
        assert r.status_code == 401


async def test_put_rate_limit_requires_auth(app_rl) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_rl),
        base_url="http://localhost",
    ) as c:
        r = await c.put("/rate-limit", json={"rate": 5.0, "burst": 10})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Config integration tests
# ---------------------------------------------------------------------------


def test_config_rate_limit_defaults() -> None:
    """OrchestratorConfig defaults to no rate limiting (rate=0, burst=0)."""
    from tmux_orchestrator.config import OrchestratorConfig
    cfg = OrchestratorConfig()
    assert cfg.rate_limit_rps == 0.0
    assert cfg.rate_limit_burst == 0


def test_config_rate_limit_custom() -> None:
    """Custom rate_limit_rps and burst are preserved in config."""
    from tmux_orchestrator.config import OrchestratorConfig
    cfg = OrchestratorConfig(rate_limit_rps=5.0, rate_limit_burst=20)
    assert cfg.rate_limit_rps == 5.0
    assert cfg.rate_limit_burst == 20


@pytest.mark.asyncio
async def test_orchestrator_created_with_config_rate_limiter() -> None:
    """Orchestrator auto-creates rate limiter when config.rate_limit_rps > 0."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(rate_limit_rps=10.0, rate_limit_burst=5)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    status = orch.get_rate_limiter_status()
    assert status["enabled"] is True
    assert status["rate"] == 10.0
    assert status["burst"] == 5


@pytest.mark.asyncio
async def test_orchestrator_config_no_rate_limiter() -> None:
    """Orchestrator with rate_limit_rps=0 has no rate limiter (unlimited)."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(rate_limit_rps=0.0)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    status = orch.get_rate_limiter_status()
    assert status["enabled"] is False


@pytest.mark.asyncio
async def test_orchestrator_config_auto_burst() -> None:
    """When burst=0 in config with rps>0, burst defaults to 2×rps."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(rate_limit_rps=3.0, rate_limit_burst=0)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    status = orch.get_rate_limiter_status()
    assert status["enabled"] is True
    assert status["burst"] == 6  # 2 × 3.0


@pytest.mark.asyncio
async def test_reconfigure_rate_limiter_method() -> None:
    """reconfigure_rate_limiter updates rate/burst and returns status."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    status = orch.reconfigure_rate_limiter(rate=2.0, burst=4)
    assert status["enabled"] is True
    assert status["rate"] == 2.0
    assert status["burst"] == 4


@pytest.mark.asyncio
async def test_reconfigure_rate_limiter_disable() -> None:
    """reconfigure_rate_limiter(rate=0) disables limiting."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(rate_limit_rps=5.0, rate_limit_burst=10)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    status = orch.reconfigure_rate_limiter(rate=0.0, burst=0)
    assert status["enabled"] is False
