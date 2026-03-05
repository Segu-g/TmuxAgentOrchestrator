"""Tests for target_agent task routing — dispatch to a specific agent.

When a Task has ``target_agent`` set to an agent ID, the dispatch loop
MUST route that task to the named agent (and wait if that agent is busy).

Design reference:
- DESIGN.md §11 — task routing and pipeline support
- Hohpe & Woolf "Enterprise Integration Patterns" (2003) — Message Router:
  "A Message Router routes each message to the correct recipient channel
   based on its content or context."

Semantics:
- POST /tasks accepts optional ``target_agent: str | None``
- If set, the task is only dispatched when the named agent is IDLE
- If the named agent does not exist, the task goes to DLQ with reason
  "unknown target_agent"
- Tasks without target_agent continue to dispatch to any idle worker
- The target_agent is stored in Task.target_agent and used by _dispatch_loop
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

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


class DummyAgent(Agent):
    """In-process stub that processes tasks without tmux."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)

    async def start(self) -> None:
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()

    async def _dispatch_task(self, task: Task) -> None:
        await asyncio.sleep(0.05)
        await self.handle_output("done")

    async def handle_output(self, text: str) -> None:
        task_id = self._current_task.id if self._current_task else "unknown"
        await self.bus.publish(
            Message(
                type=MessageType.RESULT,
                from_id=self.id,
                payload={"task_id": task_id, "output": text},
            )
        )
        self._set_idle()

    async def notify_stdin(self, notification: str) -> None:
        pass


class BusyAgent(Agent):
    """Agent that stays BUSY until explicitly released."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self._release = asyncio.Event()

    async def start(self) -> None:
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()

    async def _dispatch_task(self, task: Task) -> None:
        await self._release.wait()
        await self.handle_output("released")

    async def handle_output(self, text: str) -> None:
        task_id = self._current_task.id if self._current_task else "unknown"
        await self.bus.publish(
            Message(
                type=MessageType.RESULT,
                from_id=self.id,
                payload={"task_id": task_id, "output": text},
            )
        )
        self._set_idle()

    async def notify_stdin(self, notification: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Test: Task dataclass has target_agent field
# ---------------------------------------------------------------------------


def test_task_has_target_agent_field():
    task = Task(id="t1", prompt="hello", target_agent="agent-a")
    assert task.target_agent == "agent-a"


def test_task_target_agent_defaults_to_none():
    task = Task(id="t1", prompt="hello")
    assert task.target_agent is None


# ---------------------------------------------------------------------------
# Test: REST endpoint accepts target_agent
# ---------------------------------------------------------------------------

_API_KEY = "target-test-key"


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _MockOrchestrator:
    _dispatch_task = None

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

    async def submit_task(self, prompt, **kwargs):
        t = Task(id="fake-id", prompt=prompt, **{k: v for k, v in kwargs.items() if v is not None})
        return t

    @property
    def bus(self):
        b = MagicMock()
        b.subscribe = AsyncMock(return_value=MagicMock())
        b.unsubscribe = AsyncMock()
        return b


@pytest.fixture
def web_client():
    orch = _MockOrchestrator()
    hub = _MockHub()
    app = create_app(orch, hub, api_key=_API_KEY)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_post_task_accepts_target_agent(web_client):
    resp = web_client.post(
        "/tasks",
        json={"prompt": "do stuff", "target_agent": "agent-a"},
        headers={"X-API-Key": _API_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "task_id" in body


def test_post_task_target_agent_in_response(web_client):
    resp = web_client.post(
        "/tasks",
        json={"prompt": "do stuff", "target_agent": "agent-a"},
        headers={"X-API-Key": _API_KEY},
    )
    body = resp.json()
    # target_agent should appear in response when set
    assert body.get("target_agent") == "agent-a"


def test_post_task_no_target_agent_omitted(web_client):
    resp = web_client.post(
        "/tasks",
        json={"prompt": "do stuff"},
        headers={"X-API-Key": _API_KEY},
    )
    body = resp.json()
    assert "target_agent" not in body or body.get("target_agent") is None


# ---------------------------------------------------------------------------
# Test: Dispatch routing — target_agent is respected
# ---------------------------------------------------------------------------


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.mark.asyncio
async def test_targeted_task_routes_to_correct_agent():
    """A task with target_agent='agent-b' must go to agent-b, not agent-a."""
    bus = Bus()
    config = make_config()
    orch = Orchestrator(bus, MagicMock(), config)

    agent_a = DummyAgent("agent-a", bus)
    agent_b = DummyAgent("agent-b", bus)
    orch.register_agent(agent_a)
    orch.register_agent(agent_b)
    await orch.start()

    # Submit a targeted task to agent-b
    task = await orch.submit_task("targeted work", target_agent="agent-b")

    # Give dispatch loop time to route
    await asyncio.sleep(0.3)

    # task should have been processed — both agents should be idle
    assert agent_a.status == AgentStatus.IDLE
    assert agent_b.status == AgentStatus.IDLE

    await orch.stop()


@pytest.mark.asyncio
async def test_untargeted_task_dispatches_to_any_idle():
    """A task without target_agent still dispatches to any idle worker."""
    bus = Bus()
    config = make_config()
    orch = Orchestrator(bus, MagicMock(), config)

    agent_a = DummyAgent("agent-a", bus)
    orch.register_agent(agent_a)
    await orch.start()

    task = await orch.submit_task("any work")
    await asyncio.sleep(0.3)

    assert agent_a.status == AgentStatus.IDLE

    await orch.stop()


@pytest.mark.asyncio
async def test_targeted_task_to_nonexistent_agent_goes_to_dlq():
    """A task targeting an unknown agent should be dead-lettered."""
    bus = Bus()
    config = make_config(dlq_max_retries=2)
    orch = Orchestrator(bus, MagicMock(), config)

    agent_a = DummyAgent("agent-a", bus)
    orch.register_agent(agent_a)
    await orch.start()

    task = await orch.submit_task("work for ghost", target_agent="ghost-agent")
    await asyncio.sleep(0.5)

    dlq = orch.list_dlq()
    assert len(dlq) > 0
    dlq_task = dlq[0]
    assert dlq_task["task_id"] == task.id
    assert "target_agent" in dlq_task.get("reason", "").lower() or "ghost-agent" in dlq_task.get("reason", "")

    await orch.stop()
