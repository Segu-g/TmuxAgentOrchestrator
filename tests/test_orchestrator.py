"""Tests for the Orchestrator (task dispatch, P2P routing)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DummyAgent(Agent):
    """Minimal agent that records dispatched tasks."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self.dispatched: list[Task] = []

    async def start(self) -> None:
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(
            self._run_loop(), name=f"{self.id}-loop"
        )

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()

    async def _dispatch_task(self, task: Task) -> None:
        self.dispatched.append(task)
        await asyncio.sleep(0)  # yield
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
    )
    defaults.update(kwargs)
    return OrchestratorConfig(**defaults)


def make_tmux_mock():
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    return tmux


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_submit_and_dispatch() -> None:
    """A submitted task is dispatched to an idle agent."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("hello world")
        # Give the dispatch loop time to run
        await asyncio.sleep(0.3)
        assert any(t.id == task.id for t in agent.dispatched)
    finally:
        await orch.stop()


async def test_no_idle_agent_requeues() -> None:
    """If all agents are busy, the task stays in the queue."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    # Mark BUSY *after* start() (which would reset status to IDLE)
    agent.status = AgentStatus.BUSY
    try:
        await orch.submit_task("queued task")
        await asyncio.sleep(0.3)
        # Task should still be in queue (agent is busy)
        assert len(agent.dispatched) == 0
    finally:
        await orch.stop()


async def test_p2p_allowed() -> None:
    """P2P message between permitted agents is forwarded."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(p2p_permissions=[("a1", "a2")])
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    await orch.start()

    q_a2 = await bus.subscribe("a2")
    try:
        peer_msg = Message(
            type=MessageType.PEER_MSG,
            from_id="a1",
            to_id="a2",
            payload={"data": "ping"},
        )
        await orch.route_message(peer_msg)
        await asyncio.sleep(0.1)
        assert q_a2.qsize() == 1
        received = q_a2.get_nowait()
        assert received.payload["data"] == "ping"
    finally:
        await orch.stop()


async def test_p2p_blocked() -> None:
    """P2P message between non-permitted agents is dropped."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(p2p_permissions=[])
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    await orch.start()

    q_b = await bus.subscribe("b")
    try:
        blocked_msg = Message(
            type=MessageType.PEER_MSG,
            from_id="a",
            to_id="b",
            payload={"data": "should not arrive"},
        )
        await orch.route_message(blocked_msg)
        await asyncio.sleep(0.1)
        assert q_b.qsize() == 0
    finally:
        await orch.stop()


async def test_pause_and_resume() -> None:
    """Pausing stops dispatch; resuming re-enables it."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()

    try:
        orch.pause()
        assert orch.is_paused
        await orch.submit_task("paused task")
        await asyncio.sleep(0.3)
        assert len(agent.dispatched) == 0  # not dispatched while paused

        orch.resume()
        assert not orch.is_paused
        await asyncio.sleep(0.5)
        assert len(agent.dispatched) == 1  # dispatched after resume
    finally:
        await orch.stop()


async def test_list_agents() -> None:
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    a1 = DummyAgent("agent-1", bus)
    a2 = DummyAgent("agent-2", bus)
    orch.register_agent(a1)
    orch.register_agent(a2)
    await orch.start()

    try:
        agents = orch.list_agents()
        ids = {a["id"] for a in agents}
        assert {"agent-1", "agent-2"} == ids
    finally:
        await orch.stop()
