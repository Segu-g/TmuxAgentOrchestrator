"""Tests for agent drain / graceful shutdown (v0.28.0).

Design references:
- Kubernetes Pod terminationGracePeriodSeconds
- HAProxy graceful restart
- UNIX SO_LINGER graceful socket close
- AWS ECS stopTimeout
- DESIGN.md §10.23 (v0.28.0)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

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


def make_config(**kwargs) -> OrchestratorConfig:
    defaults = dict(
        session_name="test",
        agents=[],
        p2p_permissions=[],
        task_timeout=10,
        watchdog_poll=9999.0,
        recovery_poll=9999.0,
    )
    defaults.update(kwargs)
    return OrchestratorConfig(**defaults)


def make_tmux_mock():
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    return tmux


class DummyAgent(Agent):
    """Minimal agent that records dispatched tasks and sets itself IDLE immediately."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self.dispatched: list[Task] = []
        self.dispatched_event: asyncio.Event = asyncio.Event()
        self.stopped = False

    async def start(self) -> None:
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(
            self._run_loop(), name=f"{self.id}-loop"
        )

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        self.stopped = True
        if self._run_task:
            self._run_task.cancel()

    async def _dispatch_task(self, task: Task) -> None:
        self.dispatched.append(task)
        self.dispatched_event.set()
        await asyncio.sleep(0)
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


class SlowDummyAgent(Agent):
    """Agent that holds BUSY for a long time, publishing RESULT when released."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self.dispatched: list[Task] = []
        self.hold = asyncio.Event()
        self.started_event = asyncio.Event()
        self.stopped = False

    async def start(self) -> None:
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(
            self._run_loop(), name=f"{self.id}-loop"
        )

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        self.stopped = True
        if self._run_task:
            self._run_task.cancel()

    async def _dispatch_task(self, task: Task) -> None:
        self.dispatched.append(task)
        self.started_event.set()
        # Wait until released
        await self.hold.wait()
        # Publish RESULT so the orchestrator _route_loop sees it
        await self.bus.publish(Message(
            type=MessageType.RESULT,
            from_id=self.id,
            payload={"task_id": task.id, "output": "done"},
        ))
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def handle(self, ws) -> None:
        pass


# ---------------------------------------------------------------------------
# Unit tests — AgentStatus enum
# ---------------------------------------------------------------------------


def test_agent_status_draining_exists():
    """AgentStatus.DRAINING is defined in the enum."""
    assert AgentStatus.DRAINING == "DRAINING"
    assert AgentStatus.DRAINING in list(AgentStatus)


def test_agent_status_draining_is_string():
    """AgentStatus.DRAINING behaves as a string (str, Enum)."""
    assert AgentStatus.DRAINING.value == "DRAINING"
    # AgentStatus extends str so the value itself compares equal to "DRAINING"
    assert AgentStatus.DRAINING == "DRAINING"


# ---------------------------------------------------------------------------
# Orchestrator unit tests — drain_agent()
# ---------------------------------------------------------------------------


async def test_drain_idle_agent_stops_immediately() -> None:
    """drain_agent on an IDLE agent stops and removes it immediately."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()
    try:
        result = await orch.drain_agent("a1")
        assert result["status"] == "stopped_immediately"
        assert agent.stopped is True
        # Agent should be removed from registry
        assert orch.get_agent("a1") is None
    finally:
        await orch.stop()


async def test_drain_idle_agent_publishes_agent_drained() -> None:
    """drain_agent on an IDLE agent publishes agent_drained STATUS event."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)

    q = await bus.subscribe("drain-observer", broadcast=True)
    await orch.start()
    try:
        await orch.drain_agent("a1")
        await asyncio.sleep(0.05)
        events = []
        while not q.empty():
            msg = q.get_nowait()
            if msg.type == MessageType.STATUS:
                events.append(msg.payload.get("event"))
        assert "agent_drained" in events
    finally:
        await orch.stop()


async def test_drain_busy_agent_sets_draining_status() -> None:
    """drain_agent on a BUSY agent sets status to DRAINING, does not stop it."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    agent = SlowDummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()
    try:
        await orch.submit_task("slow task")
        # Wait for agent to start the task
        await asyncio.wait_for(agent.started_event.wait(), timeout=2.0)

        result = await orch.drain_agent("a1")
        assert result["status"] == "draining"
        assert agent.status == AgentStatus.DRAINING
        assert agent.stopped is False
        assert "a1" in orch._draining_agents
    finally:
        agent.hold.set()  # unblock
        await orch.stop()


async def test_drain_busy_agent_publishes_agent_draining() -> None:
    """drain_agent on BUSY agent publishes agent_draining STATUS event."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    agent = SlowDummyAgent("a1", bus)
    orch.register_agent(agent)

    q = await bus.subscribe("drain-observer", broadcast=True)
    await orch.start()
    try:
        await orch.submit_task("slow task")
        await asyncio.wait_for(agent.started_event.wait(), timeout=2.0)

        await orch.drain_agent("a1")
        await asyncio.sleep(0.05)
        events = []
        while not q.empty():
            msg = q.get_nowait()
            if msg.type == MessageType.STATUS:
                events.append(msg.payload.get("event"))
        assert "agent_draining" in events
    finally:
        agent.hold.set()
        await orch.stop()


async def test_drain_already_draining_returns_already_draining() -> None:
    """drain_agent on a DRAINING agent returns already_draining."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    agent = SlowDummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()
    try:
        await orch.submit_task("slow task")
        await asyncio.wait_for(agent.started_event.wait(), timeout=2.0)
        await orch.drain_agent("a1")

        result2 = await orch.drain_agent("a1")
        assert result2["status"] == "already_draining"
    finally:
        agent.hold.set()
        await orch.stop()


async def test_drain_stopped_agent_returns_already_stopped() -> None:
    """drain_agent on a STOPPED agent returns already_stopped."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()
    try:
        await agent.stop()  # manually stop
        result = await orch.drain_agent("a1")
        assert result["status"] == "already_stopped"
    finally:
        await orch.stop()


async def test_drain_error_agent_returns_already_stopped() -> None:
    """drain_agent on an ERROR agent returns already_stopped."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()
    try:
        agent.status = AgentStatus.ERROR
        result = await orch.drain_agent("a1")
        assert result["status"] == "already_stopped"
    finally:
        await orch.stop()


async def test_drain_unknown_agent_raises_key_error() -> None:
    """drain_agent on an unknown agent_id raises KeyError."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())
    await orch.start()
    try:
        with pytest.raises(KeyError):
            await orch.drain_agent("nonexistent")
    finally:
        await orch.stop()


async def test_draining_agent_not_dispatched_new_tasks() -> None:
    """A DRAINING agent does not receive new tasks from the dispatch loop."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    slow = SlowDummyAgent("slow", bus)
    fast = DummyAgent("fast", bus)
    orch.register_agent(slow)
    orch.register_agent(fast)

    await orch.start()
    try:
        # Put slow agent to work
        await orch.submit_task("task 1")
        await asyncio.wait_for(slow.started_event.wait(), timeout=2.0)

        # Drain the slow agent
        await orch.drain_agent("slow")
        assert slow.status == AgentStatus.DRAINING

        # Submit a new task — should go to fast agent, not to slow (draining)
        await orch.submit_task("task 2")
        await asyncio.wait_for(fast.dispatched_event.wait(), timeout=2.0)
        assert len(fast.dispatched) >= 1
        # Slow agent should not have received a second task
        assert len(slow.dispatched) == 1
    finally:
        slow.hold.set()
        await orch.stop()


async def test_draining_agent_auto_stopped_after_task_completes() -> None:
    """After a DRAINING agent's task completes, it is auto-stopped and removed."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    agent = SlowDummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()
    try:
        await orch.submit_task("slow task")
        await asyncio.wait_for(agent.started_event.wait(), timeout=2.0)

        await orch.drain_agent("a1")
        assert agent.status == AgentStatus.DRAINING

        # Release the task
        agent.hold.set()

        # Wait for drain to complete (agent_drained event)
        q = await bus.subscribe("drain-check", broadcast=True)
        for _ in range(20):
            await asyncio.sleep(0.1)
            while not q.empty():
                msg = q.get_nowait()
                if (msg.type == MessageType.STATUS and
                        msg.payload.get("event") == "agent_drained"):
                    return  # test passes
        pytest.fail("agent_drained event not published within 2s")
    finally:
        agent.hold.set()
        await orch.stop()


async def test_drain_all_idle_orchestrator() -> None:
    """drain_all stops all IDLE agents immediately."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    a1 = DummyAgent("a1", bus)
    a2 = DummyAgent("a2", bus)
    orch.register_agent(a1)
    orch.register_agent(a2)

    await orch.start()
    try:
        result = await orch.drain_all()
        assert set(result["stopped_immediately"]) == {"a1", "a2"}
        assert result["draining"] == []
        assert a1.stopped is True
        assert a2.stopped is True
        assert orch.get_agent("a1") is None
        assert orch.get_agent("a2") is None
    finally:
        await orch.stop()


async def test_drain_all_returns_correct_summary() -> None:
    """drain_all returns draining + stopped_immediately + already_stopped buckets."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    slow = SlowDummyAgent("slow", bus)
    idle = DummyAgent("idle", bus)
    orch.register_agent(slow)
    orch.register_agent(idle)

    await orch.start()
    try:
        await orch.submit_task("busy task")
        await asyncio.wait_for(slow.started_event.wait(), timeout=2.0)

        result = await orch.drain_all()
        assert "slow" in result["draining"]
        assert "idle" in result["stopped_immediately"]
        assert result["already_stopped"] == []
    finally:
        slow.hold.set()
        await orch.stop()


async def test_drained_agent_no_longer_in_list_agents() -> None:
    """After drain completes, agent no longer appears in list_agents()."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()
    try:
        await orch.drain_agent("a1")
        ids = [a["id"] for a in orch.list_agents()]
        assert "a1" not in ids
    finally:
        await orch.stop()


async def test_draining_agent_removed_from_registry_after_task() -> None:
    """Draining agent is removed from registry once its task produces a RESULT."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    agent = SlowDummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()
    try:
        await orch.submit_task("slow task")
        await asyncio.wait_for(agent.started_event.wait(), timeout=2.0)
        await orch.drain_agent("a1")

        agent.hold.set()
        # Poll for removal from registry
        for _ in range(20):
            await asyncio.sleep(0.1)
            if orch.get_agent("a1") is None:
                break
        assert orch.get_agent("a1") is None, "Agent should be removed from registry after drain"
    finally:
        agent.hold.set()
        await orch.stop()


# ---------------------------------------------------------------------------
# REST API tests
# ---------------------------------------------------------------------------


class _FullMockOrchestrator:
    """Mock orchestrator with just enough API surface for drain endpoint tests."""

    def __init__(self):
        self._agents: dict = {}
        self._director_pending: list = []
        self._dispatch_task = None
        self._drain_results: dict[str, dict] = {}

    def list_agents(self) -> list:
        return [
            {"id": a.id, "status": a.status.value, "role": "worker",
             "current_task": None, "parent_id": None, "tags": [],
             "bus_drops": 0, "circuit_breaker": None}
            for a in self._agents.values()
        ]

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

    def get_rate_limiter_status(self) -> dict:
        return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}

    def get_workflow_manager(self):
        from tmux_orchestrator.workflow_manager import WorkflowManager
        return WorkflowManager()

    async def drain_agent(self, agent_id: str) -> dict:
        if agent_id not in self._agents:
            raise KeyError(agent_id)
        return self._drain_results.get(agent_id, {"agent_id": agent_id, "status": "draining"})

    async def drain_all(self) -> dict:
        return {
            "draining": ["a1"],
            "stopped_immediately": ["a2"],
            "already_stopped": [],
        }


def _make_web_app_with_mock(mock_orch):
    import tmux_orchestrator.web.app as m
    m._credentials.clear()
    m._sign_counts.clear()
    m._sessions.clear()
    m._pending_challenge = None
    token = m._new_session()
    app = create_app(mock_orch, _MockHub(), api_key="test-key")
    return app, token


async def test_post_drain_agent_200_idle() -> None:
    """POST /agents/{id}/drain returns 200 with stopped_immediately for IDLE agent."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())
    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()

    import tmux_orchestrator.web.app as m
    m._credentials.clear(); m._sessions.clear()
    token = m._new_session()
    app = create_app(orch, _MockHub(), api_key="test-key")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={"session": token},
    ) as client:
        resp = await client.post("/agents/a1/drain")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "stopped_immediately"
    await orch.stop()


async def test_post_drain_agent_404_not_found() -> None:
    """POST /agents/{id}/drain returns 404 for unknown agent."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())
    await orch.start()

    import tmux_orchestrator.web.app as m
    m._credentials.clear(); m._sessions.clear()
    token = m._new_session()
    app = create_app(orch, _MockHub(), api_key="test-key")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={"session": token},
    ) as client:
        resp = await client.post("/agents/nonexistent/drain")
    assert resp.status_code == 404
    await orch.stop()


async def test_post_drain_agent_409_already_draining() -> None:
    """POST /agents/{id}/drain returns 409 when agent is already DRAINING."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    agent = SlowDummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()

    import tmux_orchestrator.web.app as m
    m._credentials.clear(); m._sessions.clear()
    token = m._new_session()
    app = create_app(orch, _MockHub(), api_key="test-key")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={"session": token},
    ) as client:
        await orch.submit_task("slow task")
        await asyncio.wait_for(agent.started_event.wait(), timeout=2.0)
        await orch.drain_agent("a1")  # mark draining via orch directly

        resp = await client.post("/agents/a1/drain")
    assert resp.status_code == 409
    agent.hold.set()
    await orch.stop()


async def test_get_drain_status_200() -> None:
    """GET /agents/{id}/drain returns drain status fields."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()

    import tmux_orchestrator.web.app as m
    m._credentials.clear(); m._sessions.clear()
    token = m._new_session()
    app = create_app(orch, _MockHub(), api_key="test-key")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={"session": token},
    ) as client:
        resp = await client.get("/agents/a1/drain")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "a1"
        assert "draining" in data
        assert "status" in data
        assert data["draining"] is False
        assert data["status"] == "IDLE"
    await orch.stop()


async def test_get_drain_status_404_not_found() -> None:
    """GET /agents/{id}/drain returns 404 for unknown agent."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())
    await orch.start()

    import tmux_orchestrator.web.app as m
    m._credentials.clear(); m._sessions.clear()
    token = m._new_session()
    app = create_app(orch, _MockHub(), api_key="test-key")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={"session": token},
    ) as client:
        resp = await client.get("/agents/nobody/drain")
    assert resp.status_code == 404
    await orch.stop()


async def test_get_drain_status_shows_draining_true() -> None:
    """GET /agents/{id}/drain shows draining=true after drain call on BUSY agent."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    agent = SlowDummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()

    import tmux_orchestrator.web.app as m
    m._credentials.clear(); m._sessions.clear()
    token = m._new_session()
    app = create_app(orch, _MockHub(), api_key="test-key")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={"session": token},
    ) as client:
        await orch.submit_task("slow task")
        await asyncio.wait_for(agent.started_event.wait(), timeout=2.0)
        await orch.drain_agent("a1")

        resp = await client.get("/agents/a1/drain")
        assert resp.status_code == 200
        data = resp.json()
        assert data["draining"] is True
        assert data["status"] == "DRAINING"

    agent.hold.set()
    await orch.stop()


async def test_post_orchestrator_drain_returns_summary() -> None:
    """POST /orchestrator/drain drains all agents and returns summary."""
    bus = Bus()
    tmux = make_tmux_mock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=make_config())

    a1 = DummyAgent("a1", bus)
    a2 = DummyAgent("a2", bus)
    orch.register_agent(a1)
    orch.register_agent(a2)
    await orch.start()

    import tmux_orchestrator.web.app as m
    m._credentials.clear(); m._sessions.clear()
    token = m._new_session()
    app = create_app(orch, _MockHub(), api_key="test-key")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={"session": token},
    ) as client:
        resp = await client.post("/orchestrator/drain")
    assert resp.status_code == 200
    data = resp.json()
    assert "draining" in data
    assert "stopped_immediately" in data
    assert "already_stopped" in data
    # Both agents were IDLE, so both should be stopped_immediately
    assert set(data["stopped_immediately"]) == {"a1", "a2"}
    await orch.stop()
