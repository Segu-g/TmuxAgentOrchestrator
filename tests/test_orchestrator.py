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


class DummyDirectorAgent(DummyAgent):
    """DummyAgent with role=director for orchestrator director tests."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self.role = "director"


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
    """P2P message between unregistered agents with no explicit permission is dropped."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(p2p_permissions=[])
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    await orch.start()

    # "a" and "b" are NOT registered — hierarchy check requires registration
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


async def test_p2p_siblings_auto_permitted() -> None:
    """Root-level agents (no parent) are treated as siblings and may message each other."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(p2p_permissions=[])  # no explicit P2P config
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    a1 = DummyAgent("sib-1", bus)
    a2 = DummyAgent("sib-2", bus)
    orch.register_agent(a1)  # root-level, parent_id=None
    orch.register_agent(a2)  # root-level, parent_id=None

    await orch.start()
    q_a2 = await bus.subscribe("sib-2")
    try:
        msg = Message(
            type=MessageType.PEER_MSG,
            from_id="sib-1",
            to_id="sib-2",
            payload={"data": "hello sibling"},
        )
        await orch.route_message(msg)
        await asyncio.sleep(0.1)
        assert q_a2.qsize() == 1
        received = q_a2.get_nowait()
        assert received.payload["data"] == "hello sibling"
    finally:
        await orch.stop()


async def test_p2p_parent_child_auto_permitted() -> None:
    """Parent → child and child → parent are auto-permitted by hierarchy."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(p2p_permissions=[])
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    parent = DummyAgent("parent", bus)
    child = DummyAgent("child", bus)
    orch.register_agent(parent)
    orch.register_agent(child, parent_id="parent")

    await orch.start()
    q_parent = await bus.subscribe("parent")
    q_child = await bus.subscribe("child")
    try:
        # Parent → child
        await orch.route_message(Message(
            type=MessageType.PEER_MSG,
            from_id="parent", to_id="child",
            payload={"dir": "down"},
        ))
        # Child → parent
        await orch.route_message(Message(
            type=MessageType.PEER_MSG,
            from_id="child", to_id="parent",
            payload={"dir": "up"},
        ))
        await asyncio.sleep(0.1)
        assert q_child.qsize() == 1
        assert q_parent.qsize() == 1
    finally:
        await orch.stop()


async def test_p2p_cross_branch_blocked_without_explicit() -> None:
    """Agents in different branches of the hierarchy cannot communicate without explicit P2P."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(p2p_permissions=[])
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    root = DummyAgent("root", bus)
    branch_a = DummyAgent("branch-a", bus)
    branch_b = DummyAgent("branch-b", bus)
    orch.register_agent(root)
    orch.register_agent(branch_a, parent_id="root")
    orch.register_agent(branch_b, parent_id="root")

    await orch.start()
    q_b = await bus.subscribe("branch-b")
    try:
        # branch-a → branch-b: both children of root, so they ARE siblings → permitted
        await orch.route_message(Message(
            type=MessageType.PEER_MSG,
            from_id="branch-a", to_id="branch-b",
            payload={"test": "sibling via root"},
        ))
        await asyncio.sleep(0.1)
        # branch-a and branch-b share parent "root" → siblings → allowed
        assert q_b.qsize() == 1
    finally:
        await orch.stop()


async def test_p2p_cross_branch_deep_blocked() -> None:
    """Agents in different deep branches need explicit P2P permission."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(p2p_permissions=[])
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    root = DummyAgent("root", bus)
    branch_a = DummyAgent("branch-a", bus)
    branch_b = DummyAgent("branch-b", bus)
    leaf_a = DummyAgent("leaf-a", bus)   # child of branch-a
    leaf_b = DummyAgent("leaf-b", bus)   # child of branch-b
    orch.register_agent(root)
    orch.register_agent(branch_a, parent_id="root")
    orch.register_agent(branch_b, parent_id="root")
    orch.register_agent(leaf_a, parent_id="branch-a")
    orch.register_agent(leaf_b, parent_id="branch-b")

    await orch.start()
    q_leaf_b = await bus.subscribe("leaf-b")
    try:
        # leaf-a → leaf-b: different parents (branch-a vs branch-b) → cross-branch → blocked
        await orch.route_message(Message(
            type=MessageType.PEER_MSG,
            from_id="leaf-a", to_id="leaf-b",
            payload={"cross": "branch"},
        ))
        await asyncio.sleep(0.1)
        assert q_leaf_b.qsize() == 0
    finally:
        await orch.stop()


async def test_p2p_cross_branch_explicit_override() -> None:
    """Cross-branch communication is unlocked by explicit p2p_permissions config."""
    bus = Bus()
    tmux = make_tmux_mock()
    # Explicit lateral permission between leaf-a and leaf-b
    config = make_config(p2p_permissions=[("leaf-a", "leaf-b")])
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    root = DummyAgent("root", bus)
    branch_a = DummyAgent("branch-a", bus)
    branch_b = DummyAgent("branch-b", bus)
    leaf_a = DummyAgent("leaf-a", bus)
    leaf_b = DummyAgent("leaf-b", bus)
    orch.register_agent(root)
    orch.register_agent(branch_a, parent_id="root")
    orch.register_agent(branch_b, parent_id="root")
    orch.register_agent(leaf_a, parent_id="branch-a")
    orch.register_agent(leaf_b, parent_id="branch-b")

    await orch.start()
    q_leaf_b = await bus.subscribe("leaf-b")
    try:
        await orch.route_message(Message(
            type=MessageType.PEER_MSG,
            from_id="leaf-a", to_id="leaf-b",
            payload={"cross": "explicit"},
        ))
        await asyncio.sleep(0.1)
        assert q_leaf_b.qsize() == 1
        received = q_leaf_b.get_nowait()
        assert received.payload["cross"] == "explicit"
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


# ---------------------------------------------------------------------------
# Task timeout tests
# ---------------------------------------------------------------------------


class SlowDummyAgent(Agent):
    """Agent whose _dispatch_task never returns (simulates a hung task)."""

    def __init__(self, agent_id: str, bus: Bus, task_timeout: float | None = None) -> None:
        super().__init__(agent_id, bus, task_timeout=task_timeout)

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
        await asyncio.sleep(9999)  # never completes

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


async def test_task_timeout_publishes_result() -> None:
    """When a task times out, a RESULT message with error='timeout' is published."""
    bus = Bus()
    result_q = await bus.subscribe("__test__", broadcast=True)

    agent = SlowDummyAgent("slow-1", bus, task_timeout=0.1)
    await agent.start()

    task = Task(id="t-timeout", prompt="slow task")
    await agent.send_task(task)

    # Wait long enough for the timeout to fire
    await asyncio.sleep(0.5)

    results = []
    while not result_q.empty():
        msg = result_q.get_nowait()
        if msg.type == MessageType.RESULT:
            results.append(msg)

    assert any(
        r.payload.get("task_id") == "t-timeout" and r.payload.get("error") == "timeout"
        for r in results
    ), f"No timeout result found; got: {results}"

    await agent.stop()
    await bus.unsubscribe("__test__")


async def test_task_timeout_agent_returns_to_idle() -> None:
    """After a timeout the agent status returns to IDLE."""
    bus = Bus()
    agent = SlowDummyAgent("slow-2", bus, task_timeout=0.1)
    await agent.start()

    task = Task(id="t-idle", prompt="slow task")
    await agent.send_task(task)

    await asyncio.sleep(0.5)
    assert agent.status == AgentStatus.IDLE

    await agent.stop()


# ---------------------------------------------------------------------------
# Status bus event tests
# ---------------------------------------------------------------------------


async def test_agent_busy_event_published() -> None:
    """When a task starts, an agent_busy STATUS event is published."""
    bus = Bus()
    events_q = await bus.subscribe("__events__", broadcast=True)

    agent = DummyAgent("ev-1", bus)
    await agent.start()

    task = Task(id="t-busy", prompt="hello")
    await agent.send_task(task)
    await asyncio.sleep(0.3)

    events = []
    while not events_q.empty():
        msg = events_q.get_nowait()
        if msg.type == MessageType.STATUS:
            events.append(msg.payload)

    assert any(
        e.get("event") == "agent_busy" and e.get("agent_id") == "ev-1"
        for e in events
    ), f"No agent_busy event found; got: {events}"

    await agent.stop()
    await bus.unsubscribe("__events__")


async def test_agent_idle_event_published() -> None:
    """After a task completes, an agent_idle STATUS event is published."""
    bus = Bus()
    events_q = await bus.subscribe("__events2__", broadcast=True)

    agent = DummyAgent("ev-2", bus)
    await agent.start()

    task = Task(id="t-idle-ev", prompt="hello")
    await agent.send_task(task)
    await asyncio.sleep(0.3)

    events = []
    while not events_q.empty():
        msg = events_q.get_nowait()
        if msg.type == MessageType.STATUS:
            events.append(msg.payload)

    assert any(
        e.get("event") == "agent_idle" and e.get("agent_id") == "ev-2"
        for e in events
    ), f"No agent_idle event found; got: {events}"

    await agent.stop()
    await bus.unsubscribe("__events2__")


# ---------------------------------------------------------------------------
# AgentRole enum and config-level ubiquitous language
# ---------------------------------------------------------------------------


def test_agent_role_enum_values() -> None:
    from tmux_orchestrator.config import AgentRole
    assert AgentRole.WORKER.value == "worker"
    assert AgentRole.DIRECTOR.value == "director"


def test_agent_role_serialises_as_string() -> None:
    from tmux_orchestrator.config import AgentRole
    import json
    data = {"role": AgentRole.WORKER}
    assert json.dumps(data) == '{"role": "worker"}'


def test_agent_role_from_string() -> None:
    from tmux_orchestrator.config import AgentRole
    assert AgentRole("worker") == AgentRole.WORKER
    assert AgentRole("director") == AgentRole.DIRECTOR


# ---------------------------------------------------------------------------
# Task.trace_id
# ---------------------------------------------------------------------------


def test_task_trace_id_auto_generated() -> None:
    t = Task(id="t1", prompt="hello")
    assert t.trace_id
    assert len(t.trace_id) == 16  # 8 bytes hex


def test_task_trace_ids_are_unique() -> None:
    ids = {Task(id=f"t{i}", prompt="x").trace_id for i in range(20)}
    assert len(ids) == 20  # no collisions


# ---------------------------------------------------------------------------
# Orchestrator.get_director / flush_director_pending
# ---------------------------------------------------------------------------


async def test_get_director_returns_director_agent() -> None:
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    worker = DummyAgent("w1", bus)
    director = DummyDirectorAgent("d1", bus)
    orch.register_agent(worker)
    orch.register_agent(director)

    assert orch.get_director() is director


def test_get_director_returns_none_when_no_director() -> None:
    from unittest.mock import MagicMock
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    worker = DummyAgent("w1", bus)
    orch.register_agent(worker)

    assert orch.get_director() is None


def test_flush_director_pending_returns_and_clears() -> None:
    from unittest.mock import MagicMock
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    orch._director_pending = ["result-a", "result-b"]
    items = orch.flush_director_pending()
    assert items == ["result-a", "result-b"]
    assert orch._director_pending == []


# ---------------------------------------------------------------------------
# Circuit breaker in dispatch
# ---------------------------------------------------------------------------


async def test_circuit_breaker_blocks_errored_agent() -> None:
    """An agent whose circuit is OPEN should not receive dispatched tasks."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("cb-worker", bus)
    orch.register_agent(agent)

    # Manually trip the circuit breaker
    for _ in range(config.circuit_breaker_threshold):
        orch._breakers["cb-worker"].record_failure()

    assert not orch._breakers["cb-worker"].is_allowed()
    # _find_idle_agent should skip this agent
    agent.status = AgentStatus.IDLE
    found = orch._find_idle_agent()
    assert found is None


async def test_circuit_breaker_closes_after_success() -> None:
    """After a probe succeeds in HALF_OPEN, the circuit breaker returns to CLOSED."""
    import time
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(circuit_breaker_threshold=1, circuit_breaker_recovery=300.0)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("cb-w2", bus)
    orch.register_agent(agent)

    cb = orch._breakers["cb-w2"]
    cb.record_failure()  # OPEN (recovery=300s so still blocked)
    assert not cb.is_allowed()  # OPEN, timeout not elapsed

    # Back-date opened_at to simulate timeout elapsed
    cb._opened_at = time.monotonic() - 400.0
    assert cb.is_allowed()  # → HALF_OPEN (first call transitions)
    cb.record_success()  # → CLOSED
    assert cb.is_allowed()
    from tmux_orchestrator.circuit_breaker import BreakerState
    assert cb.state == BreakerState.CLOSED


# ---------------------------------------------------------------------------
# Bus drop count exposed in list_agents
# ---------------------------------------------------------------------------


async def test_list_agents_includes_bus_drops() -> None:
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("drop-agent", bus)
    orch.register_agent(agent)

    # Simulate a drop on the bus
    bus._drop_counts["drop-agent"] = 5

    agents = orch.list_agents()
    assert agents[0]["bus_drops"] == 5
