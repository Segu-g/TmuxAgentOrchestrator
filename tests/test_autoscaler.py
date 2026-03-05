"""Tests for Queue-depth AutoScaler (v0.23.0).

Tests cover:
1. AutoScaler creates agent when queue depth exceeds threshold
2. AutoScaler does not create agent when at max
3. AutoScaler stops idle agents after cooldown when queue drains
4. AutoScaler respects min (never drops below autoscale_min)
5. AutoScaler does not scale during cooldown period
6. REST GET /orchestrator/autoscaler returns correct status
7. REST PUT /orchestrator/autoscaler reconfigures live

References:
- Kubernetes HPA https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/
- Thijssen "Autonomic Computing" (MIT Press, 2009) — MAPE-K loop
- AWS Auto Scaling cooldowns https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-cooldowns.html
- DESIGN.md §10.18 (v0.23.0)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tmux_orchestrator.agents.base import AgentStatus
from tmux_orchestrator.autoscaler import AutoScaler
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(**kwargs) -> OrchestratorConfig:
    defaults = dict(
        session_name="test",
        task_timeout=30,
        watchdog_poll=999,
        autoscale_max=5,
        autoscale_min=0,
        autoscale_threshold=2,
        autoscale_cooldown=30.0,
        autoscale_poll=0.05,
    )
    defaults.update(kwargs)
    return OrchestratorConfig(**defaults)


def make_tmux_mock() -> MagicMock:
    tmux = MagicMock()
    tmux.new_pane = MagicMock(return_value=MagicMock(id="pane-1"))
    tmux.new_subpane = MagicMock(return_value=MagicMock(id="pane-2"))
    tmux.send_keys = MagicMock()
    tmux.watch_pane = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.capture_pane = MagicMock(return_value="❯ ")
    return tmux


def make_orch(config: OrchestratorConfig | None = None) -> tuple[Orchestrator, Bus]:
    bus = Bus()
    tmux = make_tmux_mock()
    cfg = config or make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=cfg)
    return orch, bus


def make_idle_agent(agent_id: str) -> AsyncMock:
    agent = AsyncMock()
    agent.id = agent_id
    agent.pane = None
    agent.status = AgentStatus.IDLE
    return agent


def make_busy_agent(agent_id: str) -> AsyncMock:
    agent = AsyncMock()
    agent.id = agent_id
    agent.pane = None
    agent.status = AgentStatus.BUSY
    return agent


def make_autoscaler(orch: Orchestrator, **cfg_overrides) -> AutoScaler:
    """Create an AutoScaler with a fast poll interval for testing."""
    config = make_config(**cfg_overrides)
    return AutoScaler(orch, config)


# ---------------------------------------------------------------------------
# 1. Scale-up: creates agent when queue depth exceeds threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_up_when_queue_exceeds_threshold() -> None:
    """AutoScaler creates a new agent when queue_depth > threshold * idle_count."""
    orch, _ = make_orch()
    autoscaler = make_autoscaler(orch, autoscale_threshold=2)

    created: list[str] = []

    async def mock_create_agent(**kwargs):
        agent = AsyncMock()
        agent.id = f"auto-{len(created)}"
        agent.status = AgentStatus.IDLE
        created.append(agent.id)
        return agent

    orch.create_agent = mock_create_agent  # type: ignore[method-assign]

    # queue_depth=5, idle_count=0 → effective_threshold=max(1, 2*max(1,0))=2, 5>2
    orch.queue_depth = lambda: 5  # type: ignore[method-assign]

    await autoscaler._maybe_scale_up(queue_depth=5)

    assert len(created) == 1


@pytest.mark.asyncio
async def test_scale_up_not_triggered_below_threshold() -> None:
    """AutoScaler does not create agents when queue depth is at or below threshold."""
    orch, _ = make_orch()
    autoscaler = make_autoscaler(orch, autoscale_threshold=3)

    created: list[str] = []

    async def mock_create_agent(**kwargs):
        agent = AsyncMock()
        agent.id = f"auto-{len(created)}"
        created.append(agent.id)
        return agent

    orch.create_agent = mock_create_agent  # type: ignore[method-assign]

    # Register one idle agent → effective_threshold = max(1, 3*1) = 3
    idle = make_idle_agent("w-0")
    orch.registry.register(idle)

    # queue_depth=3 is NOT > 3, so no scale-up
    await autoscaler._maybe_scale_up(queue_depth=3)

    assert len(created) == 0


@pytest.mark.asyncio
async def test_scale_up_tracks_autoscaled_ids() -> None:
    """IDs of created agents are tracked in _autoscaled_ids."""
    orch, _ = make_orch()
    autoscaler = make_autoscaler(orch, autoscale_threshold=1)

    counter = [0]

    async def mock_create_agent(**kwargs):
        counter[0] += 1
        agent = AsyncMock()
        agent.id = f"auto-{counter[0]}"
        agent.status = AgentStatus.IDLE
        return agent

    orch.create_agent = mock_create_agent  # type: ignore[method-assign]
    orch.queue_depth = lambda: 10  # type: ignore[method-assign]

    await autoscaler._maybe_scale_up(queue_depth=10)

    assert len(autoscaler._autoscaled_ids) == 1
    assert autoscaler._last_scale_up is not None


# ---------------------------------------------------------------------------
# 2. AutoScaler does not create agent when at max
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_scale_up_when_at_max() -> None:
    """AutoScaler refuses to create more agents when autoscale_max is reached."""
    orch, _ = make_orch()
    autoscaler = make_autoscaler(orch, autoscale_max=2, autoscale_threshold=1)

    # Simulate 2 existing autoscaled agents (at max)
    for i in range(2):
        aid = f"auto-{i}"
        agent = make_idle_agent(aid)
        orch.registry.register(agent)
        autoscaler._autoscaled_ids.add(aid)

    created: list[str] = []

    async def mock_create_agent(**kwargs):
        agent = AsyncMock()
        agent.id = "extra"
        created.append(agent.id)
        return agent

    orch.create_agent = mock_create_agent  # type: ignore[method-assign]

    await autoscaler._maybe_scale_up(queue_depth=100)

    assert len(created) == 0, "Should not create agent when at max"


# ---------------------------------------------------------------------------
# 3. AutoScaler stops idle agents after cooldown when queue drains
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_down_after_cooldown() -> None:
    """AutoScaler stops an idle autoscaled agent after cooldown expires."""
    orch, _ = make_orch()
    autoscaler = make_autoscaler(orch, autoscale_min=0, autoscale_cooldown=0.01)

    aid = "auto-0"
    agent = make_idle_agent(aid)
    orch.registry.register(agent)
    autoscaler._autoscaled_ids.add(aid)

    # Simulate queue becoming empty a long time ago (cooldown expired)
    autoscaler._queue_empty_since = time.time() - 60.0

    await autoscaler._maybe_scale_down(queue_depth=0)

    agent.stop.assert_awaited_once()
    assert aid not in autoscaler._autoscaled_ids


@pytest.mark.asyncio
async def test_scale_down_unregisters_stopped_agent() -> None:
    """After scale-down, the agent is removed from the registry."""
    orch, _ = make_orch()
    autoscaler = make_autoscaler(orch, autoscale_min=0, autoscale_cooldown=0.01)

    aid = "auto-reg"
    agent = make_idle_agent(aid)
    orch.registry.register(agent)
    autoscaler._autoscaled_ids.add(aid)
    autoscaler._queue_empty_since = time.time() - 60.0

    await autoscaler._maybe_scale_down(queue_depth=0)

    assert orch.registry.get(aid) is None


# ---------------------------------------------------------------------------
# 4. AutoScaler respects min — never drops below autoscale_min
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_down_respects_min() -> None:
    """AutoScaler never scales below autoscale_min even after cooldown."""
    orch, _ = make_orch()
    autoscaler = make_autoscaler(orch, autoscale_min=2, autoscale_cooldown=0.01)

    # Register 2 autoscaled agents (equal to min)
    for i in range(2):
        aid = f"auto-{i}"
        agent = make_idle_agent(aid)
        orch.registry.register(agent)
        autoscaler._autoscaled_ids.add(aid)

    autoscaler._queue_empty_since = time.time() - 60.0

    # Stop should be a no-op because count == min
    for i in range(2):
        agent = orch.registry.get(f"auto-{i}")
        assert agent is not None

    await autoscaler._maybe_scale_down(queue_depth=0)

    # Neither agent should have been stopped
    for i in range(2):
        agent = orch.registry.get(f"auto-{i}")
        assert agent is not None, f"auto-{i} should still be registered"


@pytest.mark.asyncio
async def test_scale_down_stops_until_min() -> None:
    """When there are more agents than min, only excess ones are stopped."""
    orch, _ = make_orch()
    autoscaler = make_autoscaler(orch, autoscale_min=1, autoscale_cooldown=0.01)

    # 2 autoscaled agents, min=1 → only 1 should be stopped per cycle
    for i in range(2):
        aid = f"auto-{i}"
        agent = make_idle_agent(aid)
        orch.registry.register(agent)
        autoscaler._autoscaled_ids.add(aid)

    autoscaler._queue_empty_since = time.time() - 60.0

    # First scale-down cycle: stops one
    await autoscaler._maybe_scale_down(queue_depth=0)

    remaining = sum(
        1 for i in range(2)
        if orch.registry.get(f"auto-{i}") is not None
    )
    assert remaining == 1, "Should have stopped exactly one agent (down to min=1)"


# ---------------------------------------------------------------------------
# 5. AutoScaler does not scale during cooldown period
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_scale_down_during_cooldown() -> None:
    """AutoScaler does not stop agents while the cooldown period is still running."""
    orch, _ = make_orch()
    autoscaler = make_autoscaler(orch, autoscale_min=0, autoscale_cooldown=3600.0)

    aid = "auto-cooling"
    agent = make_idle_agent(aid)
    orch.registry.register(agent)
    autoscaler._autoscaled_ids.add(aid)

    # Set queue_empty_since to just now (cooldown not yet expired)
    autoscaler._queue_empty_since = time.time()

    await autoscaler._maybe_scale_down(queue_depth=0)

    # Agent must still be registered
    assert orch.registry.get(aid) is not None
    agent.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_scale_down_when_queue_not_empty() -> None:
    """AutoScaler resets the cooldown timer when queue is non-empty."""
    orch, _ = make_orch()
    autoscaler = make_autoscaler(orch, autoscale_min=0, autoscale_cooldown=0.01)

    aid = "auto-busy"
    agent = make_idle_agent(aid)
    orch.registry.register(agent)
    autoscaler._autoscaled_ids.add(aid)

    # Pre-set a stale timer to ensure it would have triggered
    autoscaler._queue_empty_since = time.time() - 9999.0

    # Non-empty queue resets the timer
    await autoscaler._maybe_scale_down(queue_depth=5)

    assert autoscaler._queue_empty_since is None
    agent.stop.assert_not_awaited()


# ---------------------------------------------------------------------------
# 6. REST GET /orchestrator/autoscaler returns correct status
# ---------------------------------------------------------------------------

_API_KEY = "test-key"


@pytest.mark.asyncio
async def test_rest_get_autoscaler_disabled() -> None:
    """GET /orchestrator/autoscaler returns enabled=false when autoscale_max=0."""
    from httpx import ASGITransport, AsyncClient

    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.web.ws import WebSocketHub

    # autoscale_max=0 → autoscaler disabled
    cfg = make_config(autoscale_max=0)
    orch, bus = make_orch(cfg)
    hub = WebSocketHub(bus)
    app = create_app(orch, hub, api_key=_API_KEY)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/orchestrator/autoscaler",
            headers={"X-API-Key": _API_KEY},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False
    assert data["agent_count"] == 0
    assert "queue_depth" in data
    assert "autoscaled_ids" in data


@pytest.mark.asyncio
async def test_rest_get_autoscaler_enabled() -> None:
    """GET /orchestrator/autoscaler returns enabled=true with live state when active."""
    from httpx import ASGITransport, AsyncClient

    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.web.ws import WebSocketHub

    cfg = make_config(autoscale_max=3, autoscale_min=0, autoscale_threshold=2)
    orch, bus = make_orch(cfg)
    hub = WebSocketHub(bus)
    app = create_app(orch, hub, api_key=_API_KEY)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/orchestrator/autoscaler",
            headers={"X-API-Key": _API_KEY},
        )

    assert resp.status_code == 200
    data = resp.json()
    # enabled reflects _enabled flag (False until start() is called)
    assert "enabled" in data
    assert data["max"] == 3
    assert data["min"] == 0
    assert data["threshold"] == 2
    assert isinstance(data["autoscaled_ids"], list)


# ---------------------------------------------------------------------------
# 7. REST PUT /orchestrator/autoscaler reconfigures live
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_put_autoscaler_reconfigures() -> None:
    """PUT /orchestrator/autoscaler updates live parameters and returns new values."""
    from httpx import ASGITransport, AsyncClient

    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.web.ws import WebSocketHub

    cfg = make_config(autoscale_max=5, autoscale_min=0, autoscale_threshold=3, autoscale_cooldown=30.0)
    orch, bus = make_orch(cfg)
    hub = WebSocketHub(bus)
    app = create_app(orch, hub, api_key=_API_KEY)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.put(
            "/orchestrator/autoscaler",
            json={"min": 1, "max": 8, "threshold": 5, "cooldown": 60.0},
            headers={"X-API-Key": _API_KEY},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["min"] == 1
    assert data["max"] == 8
    assert data["threshold"] == 5
    assert data["cooldown"] == 60.0

    # Verify the live state on the autoscaler
    assert orch._autoscaler._min == 1
    assert orch._autoscaler._max == 8
    assert orch._autoscaler._threshold == 5
    assert orch._autoscaler._cooldown == 60.0


@pytest.mark.asyncio
async def test_rest_put_autoscaler_409_when_disabled() -> None:
    """PUT /orchestrator/autoscaler returns 409 when autoscaling is not enabled."""
    from httpx import ASGITransport, AsyncClient

    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.web.ws import WebSocketHub

    cfg = make_config(autoscale_max=0)
    orch, bus = make_orch(cfg)
    hub = WebSocketHub(bus)
    app = create_app(orch, hub, api_key=_API_KEY)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.put(
            "/orchestrator/autoscaler",
            json={"min": 1},
            headers={"X-API-Key": _API_KEY},
        )

    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Additional: lifecycle (start/stop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autoscaler_start_creates_background_task() -> None:
    """AutoScaler.start() creates a background task."""
    orch, _ = make_orch()
    autoscaler = make_autoscaler(orch)

    # Override poll to make the loop yield immediately
    autoscaler._poll = 9999.0

    autoscaler.start()
    try:
        assert autoscaler._task is not None
        assert autoscaler._enabled is True
    finally:
        autoscaler.stop()
        if autoscaler._task:
            try:
                await autoscaler._task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_autoscaler_stop_cancels_task() -> None:
    """AutoScaler.stop() cancels the background task."""
    orch, _ = make_orch()
    autoscaler = make_autoscaler(orch)
    autoscaler._poll = 9999.0
    autoscaler.start()

    assert autoscaler._task is not None
    autoscaler.stop()

    assert autoscaler._task is None
    assert autoscaler._enabled is False


@pytest.mark.asyncio
async def test_autoscaler_status_returns_all_fields() -> None:
    """AutoScaler.status() returns a complete status dict."""
    orch, _ = make_orch()
    autoscaler = make_autoscaler(orch)

    status = await autoscaler.status()

    expected_keys = {
        "enabled", "agent_count", "queue_depth",
        "last_scale_up", "last_scale_down", "autoscaled_ids",
        "min", "max", "threshold", "cooldown",
    }
    assert expected_keys.issubset(status.keys())
    assert isinstance(status["autoscaled_ids"], list)


@pytest.mark.asyncio
async def test_orchestrator_get_autoscaler_status_disabled() -> None:
    """Orchestrator.get_autoscaler_status() returns disabled stub when autoscale_max=0."""
    cfg = make_config(autoscale_max=0)
    orch, _ = make_orch(cfg)

    status = await orch.get_autoscaler_status()

    assert status["enabled"] is False
    assert "queue_depth" in status
    assert status["autoscaled_ids"] == []


@pytest.mark.asyncio
async def test_orchestrator_get_autoscaler_status_enabled() -> None:
    """Orchestrator.get_autoscaler_status() delegates to autoscaler when enabled."""
    cfg = make_config(autoscale_max=3)
    orch, _ = make_orch(cfg)

    status = await orch.get_autoscaler_status()

    assert "enabled" in status
    assert status["max"] == 3


# ---------------------------------------------------------------------------
# queue_depth() method
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_depth_returns_zero_when_empty() -> None:
    """Orchestrator.queue_depth() returns 0 when no tasks are queued."""
    orch, _ = make_orch()
    assert orch.queue_depth() == 0


@pytest.mark.asyncio
async def test_queue_depth_reflects_pending_tasks() -> None:
    """Orchestrator.queue_depth() reflects the number of pending tasks."""
    from tmux_orchestrator.agents.base import Task

    orch, _ = make_orch()

    # Submit tasks without starting any agents (so they stay queued)
    for i in range(3):
        task = Task(id=f"t{i}", prompt=f"task {i}", priority=0, metadata={})
        await orch.submit_task(task)

    assert orch.queue_depth() == 3


# ---------------------------------------------------------------------------
# Reconfigure
# ---------------------------------------------------------------------------


def test_autoscaler_reconfigure_updates_params() -> None:
    """AutoScaler.reconfigure() updates all specified parameters."""
    orch, _ = make_orch()
    autoscaler = make_autoscaler(orch)

    result = autoscaler.reconfigure(min=2, max=10, threshold=4, cooldown=120.0)

    assert autoscaler._min == 2
    assert autoscaler._max == 10
    assert autoscaler._threshold == 4
    assert autoscaler._cooldown == 120.0
    assert result == {"min": 2, "max": 10, "threshold": 4, "cooldown": 120.0}


def test_autoscaler_reconfigure_partial_update() -> None:
    """AutoScaler.reconfigure() only updates specified fields."""
    orch, _ = make_orch()
    autoscaler = make_autoscaler(orch, autoscale_min=1, autoscale_max=5,
                                  autoscale_threshold=3, autoscale_cooldown=30.0)

    autoscaler.reconfigure(threshold=6)

    assert autoscaler._min == 1    # unchanged
    assert autoscaler._max == 5   # unchanged
    assert autoscaler._threshold == 6  # updated
    assert autoscaler._cooldown == 30.0  # unchanged
