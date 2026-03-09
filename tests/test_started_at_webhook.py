"""Tests for v1.0.21 — started_at/uptime_s fields + agent_status webhook delivery.

Feature 1: started_at / uptime_s
- Agent.started_at is None before start()
- Agent.started_at is a datetime after _record_start_time()
- Agent.uptime_s returns None before start
- Agent.uptime_s returns positive float after start
- registry.list_all() includes started_at (ISO8601 str) and uptime_s (float)
- started_at is None in list_all() before start
- GET /agents returns started_at and uptime_s per agent
- GET /agents/{id}/stats includes started_at and uptime_s in enrichment

Feature 2: Webhook agent_status delivery
- Orchestrator.start() registers static webhooks from config.webhooks
- agent_status webhook fires for agent_busy, agent_idle, agent_error events
- agent_status webhook does NOT fire for non-status events
- WebhookConfig dataclass exists with url, events, secret fields
- load_config() parses webhooks list from YAML

Design reference: DESIGN.md §10.N (v1.0.21)
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import tmux_orchestrator.web.app as web_app_mod
from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import AgentRole, OrchestratorConfig, WebhookConfig
from tmux_orchestrator.registry import AgentRegistry
from tmux_orchestrator.webhook_manager import WebhookManager


# ---------------------------------------------------------------------------
# Minimal stub agent
# ---------------------------------------------------------------------------


class StubAgent(Agent):
    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self.role = AgentRole.WORKER
        self.tags: list[str] = []

    async def start(self) -> None:
        self._record_start_time()
        self.status = AgentStatus.IDLE

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED

    async def _dispatch_task(self, task: Task) -> None:
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


def make_registry(**kwargs) -> AgentRegistry:
    defaults = dict(p2p_permissions=[], circuit_breaker_threshold=3, circuit_breaker_recovery=60.0)
    defaults.update(kwargs)
    return AgentRegistry(**defaults)


# ===========================================================================
# Part A: started_at / uptime_s unit tests
# ===========================================================================


def test_started_at_none_before_start():
    """Agent.started_at is None before start() is called."""
    bus = Bus()
    agent = StubAgent("a1", bus)
    assert agent.started_at is None


def test_uptime_s_none_before_start():
    """Agent.uptime_s is None before start() is called."""
    bus = Bus()
    agent = StubAgent("a1", bus)
    assert agent.uptime_s is None


@pytest.mark.asyncio
async def test_started_at_set_after_record_start_time():
    """Agent.started_at is a UTC datetime after _record_start_time()."""
    bus = Bus()
    agent = StubAgent("a1", bus)
    before = datetime.now(tz=timezone.utc)
    await agent.start()
    after = datetime.now(tz=timezone.utc)
    assert agent.started_at is not None
    assert agent.started_at.tzinfo is not None  # timezone-aware
    assert before <= agent.started_at <= after


@pytest.mark.asyncio
async def test_uptime_s_positive_after_start():
    """Agent.uptime_s returns a positive float after start()."""
    bus = Bus()
    agent = StubAgent("a1", bus)
    await agent.start()
    # Small sleep to ensure uptime > 0
    await asyncio.sleep(0.01)
    uptime = agent.uptime_s
    assert uptime is not None
    assert uptime >= 0.0


@pytest.mark.asyncio
async def test_uptime_s_increases_over_time():
    """Agent.uptime_s grows monotonically over time."""
    bus = Bus()
    agent = StubAgent("a1", bus)
    await agent.start()
    t1 = agent.uptime_s
    await asyncio.sleep(0.02)
    t2 = agent.uptime_s
    assert t2 is not None
    assert t1 is not None
    assert t2 > t1


def test_record_start_time_sets_utc_datetime():
    """_record_start_time() sets started_at to a UTC datetime."""
    bus = Bus()
    agent = StubAgent("a1", bus)
    agent._record_start_time()
    assert agent.started_at is not None
    assert agent.started_at.tzinfo == timezone.utc


def test_record_start_time_is_idempotent():
    """Calling _record_start_time() a second time overwrites the previous value."""
    bus = Bus()
    agent = StubAgent("a1", bus)
    agent._record_start_time()
    first = agent.started_at
    # small delay so timestamps differ
    time.sleep(0.005)
    agent._record_start_time()
    second = agent.started_at
    assert second is not None
    assert first is not None
    assert second >= first  # monotonically non-decreasing


# ---------------------------------------------------------------------------
# registry.list_all() — started_at / uptime_s
# ---------------------------------------------------------------------------


def test_list_all_started_at_none_before_start():
    """list_all() returns started_at=None for agents not yet started."""
    bus = Bus()
    reg = make_registry()
    agent = StubAgent("a1", bus)
    assert agent.started_at is None
    reg.register(agent)
    result = reg.list_all()
    assert result[0]["started_at"] is None


def test_list_all_uptime_s_none_before_start():
    """list_all() returns uptime_s=None for agents not yet started."""
    bus = Bus()
    reg = make_registry()
    agent = StubAgent("a1", bus)
    reg.register(agent)
    result = reg.list_all()
    assert result[0]["uptime_s"] is None


@pytest.mark.asyncio
async def test_list_all_started_at_iso8601_after_start():
    """list_all() returns started_at as an ISO 8601 string after start()."""
    bus = Bus()
    reg = make_registry()
    agent = StubAgent("a1", bus)
    await agent.start()
    reg.register(agent)
    result = reg.list_all()
    started_at = result[0]["started_at"]
    assert started_at is not None
    assert isinstance(started_at, str)
    # Must be parseable as ISO 8601
    dt = datetime.fromisoformat(started_at)
    assert dt.tzinfo is not None  # timezone-aware


@pytest.mark.asyncio
async def test_list_all_uptime_s_float_after_start():
    """list_all() returns uptime_s as a float after start()."""
    bus = Bus()
    reg = make_registry()
    agent = StubAgent("a1", bus)
    await agent.start()
    reg.register(agent)
    result = reg.list_all()
    uptime = result[0]["uptime_s"]
    assert uptime is not None
    assert isinstance(uptime, float)
    assert uptime >= 0.0


# ---------------------------------------------------------------------------
# Web API — GET /agents and GET /agents/{id}/stats
# ---------------------------------------------------------------------------

_API_KEY = "test-v1021-key"


def _make_mock_agent(agent_id: str, *, started_at_dt=None):
    """Build a minimal mock agent with started_at / uptime_s."""
    from datetime import timezone

    mock_agent = MagicMock()
    mock_agent.status = AgentStatus.IDLE
    mock_agent.worktree_path = None
    mock_agent.started_at = started_at_dt
    if started_at_dt is not None:
        mock_agent.uptime_s = (datetime.now(tz=timezone.utc) - started_at_dt).total_seconds()
    else:
        mock_agent.uptime_s = None
    return mock_agent


def _make_mock_orch(agent_id: str, *, started_at_dt=None, context_stats=None, history=None):
    orch = MagicMock()
    orch.list_tasks.return_value = []
    orch.get_director.return_value = None
    orch.flush_director_pending.return_value = []
    orch.list_dlq.return_value = []
    orch.is_paused = False
    orch.bus = MagicMock()
    orch.bus.subscribe = AsyncMock(return_value=asyncio.Queue())
    orch.bus.unsubscribe = AsyncMock()

    started_at_str = started_at_dt.isoformat() if started_at_dt else None
    uptime = (
        (datetime.now(tz=timezone.utc) - started_at_dt).total_seconds()
        if started_at_dt
        else None
    )

    mock_agent = _make_mock_agent(agent_id, started_at_dt=started_at_dt)

    def _get_agent(aid: str):
        return mock_agent if aid == agent_id else None

    orch.get_agent = MagicMock(side_effect=_get_agent)
    orch.list_agents.return_value = [
        {
            "id": agent_id,
            "status": "IDLE",
            "current_task": None,
            "role": AgentRole.WORKER,
            "parent_id": None,
            "tags": [],
            "bus_drops": 0,
            "circuit_breaker": "closed",
            "worktree_path": None,
            "started_at": started_at_str,
            "uptime_s": uptime,
        }
    ]
    orch.get_agent_context_stats = MagicMock(return_value=context_stats)
    orch.all_agent_context_stats = MagicMock(return_value=[])
    orch.get_agent_history = MagicMock(return_value=history or [])
    return orch


def _make_client(orch):
    from tmux_orchestrator.web.app import create_app

    web_app_mod._credentials.clear()
    web_app_mod._sign_counts.clear()
    web_app_mod._sessions.clear()

    class _MockHub:
        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    app = create_app(orch, _MockHub(), api_key=_API_KEY)
    return TestClient(app, raise_server_exceptions=True)


def test_get_agents_includes_started_at():
    """GET /agents response includes started_at for each agent."""
    dt = datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc)
    orch = _make_mock_orch("worker-1", started_at_dt=dt)
    client = _make_client(orch)
    resp = client.get("/agents", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["started_at"] == dt.isoformat()


def test_get_agents_started_at_none_when_not_started():
    """GET /agents returns started_at=None for agents not yet started."""
    orch = _make_mock_orch("worker-1", started_at_dt=None)
    client = _make_client(orch)
    resp = client.get("/agents", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["started_at"] is None
    assert data[0]["uptime_s"] is None


def test_get_agents_includes_uptime_s():
    """GET /agents response includes uptime_s for each started agent."""
    dt = datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc)
    orch = _make_mock_orch("worker-1", started_at_dt=dt)
    client = _make_client(orch)
    resp = client.get("/agents", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["uptime_s"] is not None
    assert isinstance(data[0]["uptime_s"], float)


def test_stats_endpoint_includes_started_at():
    """GET /agents/{id}/stats includes started_at field in enrichment."""
    dt = datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc)
    orch = _make_mock_orch("worker-1", started_at_dt=dt, context_stats=None)
    client = _make_client(orch)
    resp = client.get("/agents/worker-1/stats", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["started_at"] == dt.isoformat()


def test_stats_endpoint_includes_uptime_s():
    """GET /agents/{id}/stats includes uptime_s field in enrichment."""
    dt = datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc)
    orch = _make_mock_orch("worker-1", started_at_dt=dt, context_stats=None)
    client = _make_client(orch)
    resp = client.get("/agents/worker-1/stats", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["uptime_s"] is not None
    assert isinstance(data["uptime_s"], float)


def test_stats_endpoint_started_at_none_when_not_started():
    """GET /agents/{id}/stats returns started_at=None when agent not started."""
    orch = _make_mock_orch("worker-1", started_at_dt=None, context_stats=None)
    client = _make_client(orch)
    resp = client.get("/agents/worker-1/stats", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["started_at"] is None
    assert data["uptime_s"] is None


# ===========================================================================
# Part B: WebhookConfig dataclass + static webhook registration
# ===========================================================================


def test_webhook_config_fields():
    """WebhookConfig has url, events, and secret fields."""
    wh = WebhookConfig(
        url="http://example.com/hook",
        events=["agent_status", "task_complete"],
        secret="s3cr3t",
    )
    assert wh.url == "http://example.com/hook"
    assert wh.events == ["agent_status", "task_complete"]
    assert wh.secret == "s3cr3t"


def test_webhook_config_defaults():
    """WebhookConfig events defaults to [] and secret defaults to None."""
    wh = WebhookConfig(url="http://example.com/hook")
    assert wh.events == []
    assert wh.secret is None


def test_orchestrator_config_has_webhooks_field():
    """OrchestratorConfig has a webhooks field (list of WebhookConfig)."""
    cfg = OrchestratorConfig()
    assert hasattr(cfg, "webhooks")
    assert isinstance(cfg.webhooks, list)
    assert cfg.webhooks == []


def _make_orchestrator(cfg):
    """Create a minimal Orchestrator with a mock tmux for tests."""
    from unittest.mock import MagicMock
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.bus import Bus

    bus = Bus()
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    return Orchestrator(bus=bus, tmux=tmux, config=cfg), bus


def test_orchestrator_registers_static_webhooks():
    """Orchestrator.__init__ registers static webhooks from config.webhooks."""
    from tmux_orchestrator.config import OrchestratorConfig, WebhookConfig

    wh_cfg = WebhookConfig(
        url="http://localhost:9999/hook",
        events=["agent_status"],
        secret=None,
    )
    cfg = OrchestratorConfig(webhooks=[wh_cfg], watchdog_poll=9999.0, recovery_poll=9999.0)
    orch, _ = _make_orchestrator(cfg)

    webhooks = orch._webhook_manager.list_all()
    assert len(webhooks) == 1
    assert webhooks[0].url == "http://localhost:9999/hook"
    assert webhooks[0].events == ["agent_status"]


def test_orchestrator_registers_multiple_static_webhooks():
    """Orchestrator registers all webhooks from config.webhooks."""
    from tmux_orchestrator.config import OrchestratorConfig, WebhookConfig

    cfg = OrchestratorConfig(
        webhooks=[
            WebhookConfig(url="http://a.com/h1", events=["agent_status"]),
            WebhookConfig(url="http://b.com/h2", events=["task_complete", "*"]),
        ],
        watchdog_poll=9999.0,
        recovery_poll=9999.0,
    )
    orch, _ = _make_orchestrator(cfg)

    webhooks = orch._webhook_manager.list_all()
    urls = {w.url for w in webhooks}
    assert "http://a.com/h1" in urls
    assert "http://b.com/h2" in urls


# ===========================================================================
# Part B: agent_status webhook fires for IDLE/BUSY/ERROR events
# ===========================================================================


@pytest.mark.asyncio
async def test_agent_status_webhook_fires_for_agent_busy():
    """agent_status webhook fires when an agent_busy STATUS is published on the bus."""
    from unittest.mock import MagicMock
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.bus import Bus, Message, MessageType
    from tmux_orchestrator.config import OrchestratorConfig

    bus = Bus()
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    cfg = OrchestratorConfig(watchdog_poll=9999.0, recovery_poll=9999.0)
    orch = Orchestrator(bus=bus, tmux=tmux, config=cfg)

    delivered_events: list[dict] = []

    async def fake_deliver(event: str, data: dict) -> None:
        delivered_events.append({"event": event, "data": data})

    orch._webhook_manager.deliver = fake_deliver  # type: ignore[method-assign]

    # Manually start bus subscription (mimics orch.start())
    orch._bus_queue = await bus.subscribe("__orchestrator__", broadcast=True)

    # Publish an agent_busy STATUS message
    await bus.publish(Message(
        type=MessageType.STATUS,
        from_id="worker-1",
        payload={"event": "agent_busy", "agent_id": "worker-1", "status": "BUSY", "task_id": "t1"},
    ))

    # Run the route_loop once
    route_task = asyncio.create_task(orch._route_loop())
    await asyncio.sleep(0.05)
    route_task.cancel()
    try:
        await route_task
    except asyncio.CancelledError:
        pass

    assert any(e["event"] == "agent_status" for e in delivered_events), \
        f"Expected agent_status webhook. Got: {delivered_events}"
    matching = [e for e in delivered_events if e["event"] == "agent_status"]
    assert matching[0]["data"]["event"] == "agent_busy"
    assert matching[0]["data"]["agent_id"] == "worker-1"
    assert matching[0]["data"]["status"] == "BUSY"


@pytest.mark.asyncio
async def test_agent_status_webhook_fires_for_agent_idle():
    """agent_status webhook fires when an agent_idle STATUS is published."""
    from unittest.mock import MagicMock
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.bus import Bus, Message, MessageType
    from tmux_orchestrator.config import OrchestratorConfig

    bus = Bus()
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    cfg = OrchestratorConfig(watchdog_poll=9999.0, recovery_poll=9999.0)
    orch = Orchestrator(bus=bus, tmux=tmux, config=cfg)

    delivered_events: list[dict] = []

    async def fake_deliver(event: str, data: dict) -> None:
        delivered_events.append({"event": event, "data": data})

    orch._webhook_manager.deliver = fake_deliver  # type: ignore[method-assign]

    orch._bus_queue = await bus.subscribe("__orchestrator__", broadcast=True)

    await bus.publish(Message(
        type=MessageType.STATUS,
        from_id="worker-1",
        payload={"event": "agent_idle", "agent_id": "worker-1", "status": "IDLE"},
    ))

    route_task = asyncio.create_task(orch._route_loop())
    await asyncio.sleep(0.05)
    route_task.cancel()
    try:
        await route_task
    except asyncio.CancelledError:
        pass

    assert any(
        e["event"] == "agent_status" and e["data"]["event"] == "agent_idle"
        for e in delivered_events
    )


@pytest.mark.asyncio
async def test_agent_status_webhook_not_fired_for_other_events():
    """agent_status webhook does NOT fire for non-agent-status STATUS events."""
    from unittest.mock import MagicMock
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.bus import Bus, Message, MessageType
    from tmux_orchestrator.config import OrchestratorConfig

    bus = Bus()
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    cfg = OrchestratorConfig(watchdog_poll=9999.0, recovery_poll=9999.0)
    orch = Orchestrator(bus=bus, tmux=tmux, config=cfg)

    delivered_events: list[dict] = []

    async def fake_deliver(event: str, data: dict) -> None:
        delivered_events.append({"event": event, "data": data})

    orch._webhook_manager.deliver = fake_deliver  # type: ignore[method-assign]

    orch._bus_queue = await bus.subscribe("__orchestrator__", broadcast=True)

    # Publish a STATUS event that is NOT an agent lifecycle event
    await bus.publish(Message(
        type=MessageType.STATUS,
        from_id="__orchestrator__",
        payload={"event": "task_queued", "task_id": "t1"},
    ))

    route_task = asyncio.create_task(orch._route_loop())
    await asyncio.sleep(0.05)
    route_task.cancel()
    try:
        await route_task
    except asyncio.CancelledError:
        pass

    # No agent_status webhook should have been fired
    assert not any(e["event"] == "agent_status" for e in delivered_events), \
        f"Unexpected agent_status webhook. Got: {delivered_events}"


# ===========================================================================
# Part B: load_config() parses webhooks from YAML
# ===========================================================================


def test_load_config_parses_webhooks(tmp_path):
    """load_config() parses webhooks list from YAML and returns WebhookConfig objects."""
    import yaml
    from tmux_orchestrator.config import load_config

    yaml_data = {
        "session_name": "test",
        "agents": [],
        "webhooks": [
            {"url": "http://example.com/hook1", "events": ["agent_status", "task_complete"]},
            {"url": "http://example.com/hook2", "events": ["*"], "secret": "abc123"},
        ],
    }
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.dump(yaml_data))

    cfg = load_config(cfg_file)

    assert len(cfg.webhooks) == 2
    assert cfg.webhooks[0].url == "http://example.com/hook1"
    assert cfg.webhooks[0].events == ["agent_status", "task_complete"]
    assert cfg.webhooks[0].secret is None
    assert cfg.webhooks[1].url == "http://example.com/hook2"
    assert cfg.webhooks[1].events == ["*"]
    assert cfg.webhooks[1].secret == "abc123"


def test_load_config_webhooks_empty_when_not_specified(tmp_path):
    """load_config() returns empty webhooks list when YAML has no webhooks key."""
    import yaml
    from tmux_orchestrator.config import load_config

    yaml_data = {"session_name": "test", "agents": []}
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.dump(yaml_data))

    cfg = load_config(cfg_file)
    assert cfg.webhooks == []
