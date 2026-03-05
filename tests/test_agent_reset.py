"""Tests for POST /agents/{id}/reset endpoint and Orchestrator.reset_agent().

Covers:
- reset_agent() clears ERROR state, removes from permanently_failed, resets
  recovery_attempts, and restarts the agent
- REST endpoint 404 for unknown agent
- REST endpoint 200 returns {agent_id, reset: True} for known agent
- reset_agent() publishes agent_reset STATUS event
- Resetting a permanently-failed agent re-allows recovery

Reference: DESIGN.md §11 — ERROR エージェントの手動リセットエンドポイント
Patterns: Nordic APIs "Designing a True REST State Machine" (action sub-resource);
          Spring Statemachine REST guide (POST to transition endpoint).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class StubAgent(Agent):
    """Simple agent stub for testing reset."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self.start_count = 0
        self.stop_count = 0

    async def start(self) -> None:
        self.start_count += 1
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(
            self._run_loop(), name=f"{self.id}-loop"
        )

    async def stop(self) -> None:
        self.stop_count += 1
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass

    async def _dispatch_task(self, task: Task) -> None:
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


def make_config(**kwargs) -> OrchestratorConfig:
    defaults = dict(
        session_name="test",
        agents=[],
        p2p_permissions=[],
        task_timeout=10,
        watchdog_poll=99999,
        recovery_poll=99999,  # disable auto-recovery
    )
    defaults.update(kwargs)
    return OrchestratorConfig(**defaults)


def make_tmux_mock():
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    return tmux


# ---------------------------------------------------------------------------
# Unit tests: Orchestrator.reset_agent()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_agent_clears_error_state() -> None:
    """reset_agent() stops and restarts an ERROR agent, returning it to IDLE."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = StubAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()
    try:
        # Manually put agent in ERROR state
        agent.status = AgentStatus.ERROR
        prev_start = agent.start_count

        await orch.reset_agent("a1")

        assert agent.start_count == prev_start + 1
        assert agent.status == AgentStatus.IDLE
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_reset_agent_removes_from_permanently_failed() -> None:
    """reset_agent() removes the agent from _permanently_failed set."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = StubAgent("a2", bus)
    orch.register_agent(agent)
    await orch.start()
    try:
        # Mark agent as permanently failed
        orch._permanently_failed.add("a2")
        agent.status = AgentStatus.ERROR

        await orch.reset_agent("a2")

        assert "a2" not in orch._permanently_failed
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_reset_agent_clears_recovery_attempts() -> None:
    """reset_agent() resets the recovery attempt counter for the agent."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = StubAgent("a3", bus)
    orch.register_agent(agent)
    await orch.start()
    try:
        orch._recovery_attempts["a3"] = 5  # simulate exhausted retries
        agent.status = AgentStatus.ERROR

        await orch.reset_agent("a3")

        assert orch._recovery_attempts.get("a3", 0) == 0
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_reset_agent_publishes_status_event() -> None:
    """reset_agent() publishes an 'agent_reset' STATUS event on the bus."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = StubAgent("a4", bus)
    orch.register_agent(agent)

    sub_id = "__test_reset_event__"
    q = await bus.subscribe(sub_id, broadcast=True)

    await orch.start()
    try:
        agent.status = AgentStatus.ERROR
        await orch.reset_agent("a4")

        # Give the event loop a chance to deliver the message
        await asyncio.sleep(0.05)

        found = False
        while not q.empty():
            msg = q.get_nowait()
            q.task_done()
            if (
                msg.type == MessageType.STATUS
                and msg.payload.get("event") == "agent_reset"
                and msg.payload.get("agent_id") == "a4"
            ):
                found = True
        assert found, "agent_reset STATUS event should have been published"
    finally:
        await orch.stop()
        await bus.unsubscribe(sub_id)


@pytest.mark.asyncio
async def test_reset_agent_unknown_raises() -> None:
    """reset_agent() raises KeyError for an unknown agent_id."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    await orch.start()
    try:
        with pytest.raises(KeyError):
            await orch.reset_agent("nonexistent")
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# REST endpoint tests
# ---------------------------------------------------------------------------


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _MockOrchestrator:
    """Mock orchestrator that records reset calls."""

    def __init__(self):
        self._agents = {}
        self._dispatch_task = None
        self._director_pending = []
        self.reset_calls: list[str] = []

    def list_agents(self) -> list:
        return []

    def list_tasks(self) -> list:
        return []

    def get_agent(self, agent_id: str):
        return self._agents.get(agent_id)

    def get_director(self):
        return None

    def flush_director_pending(self) -> list:
        return []

    def list_dlq(self) -> list:
        return []

    @property
    def is_paused(self) -> bool:
        return False

    async def reset_agent(self, agent_id: str) -> None:
        if agent_id not in self._agents:
            raise KeyError(agent_id)
        self.reset_calls.append(agent_id)


_API_KEY = "test-key-xyz"


@pytest.fixture
def mock_orch_with_agent():
    orch = _MockOrchestrator()
    # Register a dummy agent entry (just needs to exist)
    orch._agents["worker-1"] = object()
    return orch


@pytest.fixture
def app_with_reset(mock_orch_with_agent):
    return create_app(mock_orch_with_agent, _MockHub(), api_key=_API_KEY)


@pytest.fixture
async def client_reset(app_with_reset):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_reset),
        base_url="http://localhost",
    ) as c:
        yield c


async def test_reset_endpoint_returns_200(client_reset) -> None:
    """POST /agents/{id}/reset returns 200 for a known agent."""
    r = await client_reset.post(
        "/agents/worker-1/reset",
        headers={"X-API-Key": _API_KEY},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["agent_id"] == "worker-1"
    assert data["reset"] is True


async def test_reset_endpoint_returns_404_for_unknown(client_reset) -> None:
    """POST /agents/{id}/reset returns 404 for an unknown agent_id."""
    r = await client_reset.post(
        "/agents/ghost-99/reset",
        headers={"X-API-Key": _API_KEY},
    )
    assert r.status_code == 404


async def test_reset_endpoint_requires_auth(app_with_reset) -> None:
    """POST /agents/{id}/reset requires authentication."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_reset),
        base_url="http://localhost",
    ) as c:
        r = await c.post("/agents/worker-1/reset")
    assert r.status_code == 401


async def test_reset_endpoint_calls_orchestrator(
    client_reset, mock_orch_with_agent
) -> None:
    """POST /agents/{id}/reset calls orchestrator.reset_agent()."""
    await client_reset.post(
        "/agents/worker-1/reset",
        headers={"X-API-Key": _API_KEY},
    )
    assert "worker-1" in mock_orch_with_agent.reset_calls
