"""Tests for dynamic agent creation (Issue #5).

Orchestrator.create_agent() lets a Director or REST caller add agents at
runtime without any pre-configured YAML template.  The orchestrator handles
the matching CONTROL action ``create_agent`` as well.

References:
- DESIGN.md §11 (v0.22.0 candidates)
- GitHub Issue #5
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import AgentRole, OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(**kwargs) -> OrchestratorConfig:
    defaults = dict(session_name="test", task_timeout=30, watchdog_poll=999)
    defaults.update(kwargs)
    return OrchestratorConfig(**defaults)


def make_tmux_mock():
    tmux = MagicMock()
    tmux.new_pane = MagicMock(return_value=MagicMock(id="pane-1"))
    tmux.new_subpane = MagicMock(return_value=MagicMock(id="pane-2"))
    tmux.send_keys = MagicMock()
    tmux.watch_pane = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.capture_pane = MagicMock(return_value="❯ ")
    return tmux


def make_orch() -> tuple[Orchestrator, Bus]:
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())
    return orch, bus


# ---------------------------------------------------------------------------
# Unit: create_agent() basic behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_agent_returns_agent_with_correct_id() -> None:
    """create_agent() must start the agent and register it."""
    orch, _ = make_orch()

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        instance = AsyncMock()
        instance.id = "my-agent"
        instance.pane = None
        MockAgent.return_value = instance

        agent = await orch.create_agent(agent_id="my-agent")

    assert agent.id == "my-agent"
    instance.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_agent_auto_generates_id_when_omitted() -> None:
    """When agent_id is None, an ID is auto-generated."""
    orch, _ = make_orch()

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        instance = AsyncMock()
        instance.pane = None
        # ID is set based on constructor arg; simulate by intercepting the call
        captured: list[str] = []

        def _capture(*args, **kwargs):
            captured.append(kwargs.get("agent_id", ""))
            instance.id = kwargs.get("agent_id", "")
            return instance

        MockAgent.side_effect = _capture

        await orch.create_agent()

    assert len(captured) == 1
    assert captured[0].startswith("dyn-")


@pytest.mark.asyncio
async def test_create_agent_auto_generates_id_with_parent_prefix() -> None:
    """Auto-generated ID uses parent prefix when parent_id is given."""
    orch, _ = make_orch()

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        # Register a fake parent first
        parent = AsyncMock()
        parent.id = "director"
        parent.pane = None
        parent.status = AgentStatus.IDLE
        orch.registry.register(parent)

        captured: list[str] = []

        def _capture(*args, **kwargs):
            captured.append(kwargs.get("agent_id", ""))
            instance = AsyncMock()
            instance.id = kwargs.get("agent_id", "")
            instance.pane = None
            return instance

        MockAgent.side_effect = _capture

        await orch.create_agent(parent_id="director")

    assert captured[0].startswith("director-dyn-")


@pytest.mark.asyncio
async def test_create_agent_raises_on_duplicate_id() -> None:
    """create_agent() raises ValueError when agent_id is already registered."""
    orch, _ = make_orch()

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        instance = AsyncMock()
        instance.id = "dup"
        instance.pane = None

        def _capture(*args, **kwargs):
            instance.id = kwargs.get("agent_id", "dup")
            return instance

        MockAgent.side_effect = _capture

        # First creation succeeds
        await orch.create_agent(agent_id="dup")

        # Second with the same ID must raise
        with pytest.raises(ValueError, match="dup"):
            await orch.create_agent(agent_id="dup")


@pytest.mark.asyncio
async def test_create_agent_grants_p2p_with_parent() -> None:
    """When parent_id is given, P2P is auto-granted between parent and new agent."""
    orch, _ = make_orch()

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        parent = AsyncMock()
        parent.id = "dir"
        parent.pane = None
        parent.status = AgentStatus.IDLE
        orch.registry.register(parent)

        def _capture(*args, **kwargs):
            inst = AsyncMock()
            inst.id = kwargs.get("agent_id", "child")
            inst.pane = None
            return inst

        MockAgent.side_effect = _capture

        await orch.create_agent(agent_id="child", parent_id="dir")

    # P2P should be permitted in both directions
    assert orch.registry.is_p2p_permitted("dir", "child")
    assert orch.registry.is_p2p_permitted("child", "dir")


@pytest.mark.asyncio
async def test_create_agent_passes_tags_and_system_prompt() -> None:
    """create_agent() forwards tags and system_prompt to ClaudeCodeAgent."""
    orch, _ = make_orch()

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        captured_kwargs: list[dict] = []

        def _capture(*args, **kwargs):
            captured_kwargs.append(kwargs)
            inst = AsyncMock()
            inst.id = kwargs.get("agent_id", "w")
            inst.pane = None
            return inst

        MockAgent.side_effect = _capture

        await orch.create_agent(
            agent_id="w",
            tags=["python", "data"],
            system_prompt="You are a data scientist.",
        )

    assert captured_kwargs[0]["tags"] == ["python", "data"]
    assert captured_kwargs[0]["system_prompt"] == "You are a data scientist."


@pytest.mark.asyncio
async def test_create_agent_publishes_agent_created_event() -> None:
    """create_agent() publishes STATUS agent_created after the agent starts."""
    orch, bus = make_orch()

    events: list[Message] = []
    q = await bus.subscribe("__test__", broadcast=True)

    async def _collect():
        while True:
            msg = await q.get()
            if msg.type == MessageType.STATUS and msg.payload.get("event") == "agent_created":
                events.append(msg)

    collector = asyncio.create_task(_collect())

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        def _capture(*args, **kwargs):
            inst = AsyncMock()
            inst.id = kwargs.get("agent_id", "ev-agent")
            inst.pane = None
            return inst

        MockAgent.side_effect = _capture

        await orch.create_agent(agent_id="ev-agent")

    await asyncio.sleep(0)
    collector.cancel()
    await bus.unsubscribe("__test__")

    assert len(events) == 1
    assert events[0].payload["agent_id"] == "ev-agent"


# ---------------------------------------------------------------------------
# Unit: CONTROL message → create_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_create_agent_dispatches_to_create_agent() -> None:
    """CONTROL {action: create_agent} triggers create_agent() on the orchestrator."""
    orch, bus = make_orch()

    created_ids: list[str] = []
    orig = orch.create_agent

    async def _spy(**kwargs):
        agent = await orig(**kwargs)
        created_ids.append(agent.id)
        return agent

    orch.create_agent = _spy  # type: ignore[method-assign]

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        # Register fake parent
        parent = AsyncMock()
        parent.id = "dir2"
        parent.pane = None
        parent.status = AgentStatus.IDLE
        orch.registry.register(parent)

        def _capture(*args, **kwargs):
            inst = AsyncMock()
            inst.id = kwargs.get("agent_id", "ctrl-child")
            inst.pane = None
            return inst

        MockAgent.side_effect = _capture

        msg = Message(
            type=MessageType.CONTROL,
            from_id="dir2",
            to_id="__orchestrator__",
            payload={
                "action": "create_agent",
                "agent_id": "ctrl-child",
                "tags": ["ml"],
                "system_prompt": "You specialise in ML.",
            },
        )
        await orch._handle_control(msg)

    assert "ctrl-child" in created_ids


@pytest.mark.asyncio
async def test_control_create_agent_invalid_id_logs_error(caplog) -> None:
    """Duplicate ID in CONTROL create_agent logs an error without raising."""
    import logging

    orch, bus = make_orch()

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        existing = AsyncMock()
        existing.id = "taken"
        existing.pane = None
        existing.status = AgentStatus.IDLE
        orch.registry.register(existing)

        msg = Message(
            type=MessageType.CONTROL,
            from_id="dir3",
            to_id="__orchestrator__",
            payload={"action": "create_agent", "agent_id": "taken"},
        )

        with caplog.at_level(logging.ERROR, logger="tmux_orchestrator.orchestrator"):
            await orch._handle_control(msg)

    assert "taken" in caplog.text or "create_agent" in caplog.text


# ---------------------------------------------------------------------------
# REST: POST /agents/new
# ---------------------------------------------------------------------------


_API_KEY = "test-key"


@pytest.mark.asyncio
async def test_rest_post_agents_new_creates_agent() -> None:
    """POST /agents/new → 200 with status=created and agent_id."""
    from httpx import ASGITransport, AsyncClient

    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.web.ws import WebSocketHub

    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())
    hub = WebSocketHub(bus)
    app = create_app(orch, hub, api_key=_API_KEY)

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        def _capture(*args, **kwargs):
            inst = AsyncMock()
            inst.id = kwargs.get("agent_id") or "rest-dyn-abc"
            inst.pane = None
            return inst

        MockAgent.side_effect = _capture

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/agents/new",
                json={"agent_id": "rest-agent", "tags": ["foo"]},
                headers={"X-API-Key": _API_KEY},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "created"
    assert data["agent_id"] == "rest-agent"


@pytest.mark.asyncio
async def test_rest_post_agents_new_409_on_duplicate() -> None:
    """POST /agents/new returns 409 when agent_id already exists."""
    from httpx import ASGITransport, AsyncClient

    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.web.ws import WebSocketHub

    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())
    hub = WebSocketHub(bus)
    app = create_app(orch, hub, api_key=_API_KEY)

    # Pre-register an agent
    existing = AsyncMock()
    existing.id = "dup-rest"
    existing.pane = None
    existing.status = AgentStatus.IDLE
    orch.registry.register(existing)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/agents/new",
            json={"agent_id": "dup-rest"},
            headers={"X-API-Key": _API_KEY},
        )

    assert resp.status_code == 409
