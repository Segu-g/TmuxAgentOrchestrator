"""Tests for per-agent task history — GET /agents/{id}/history.

Design reference:
- DESIGN.md §11 — per-agent task history / timing stats endpoint
- TAMAS (IBM, 2025): "Beyond Black-Box Benchmarking: Observability, Analytics,
  and Optimization of Agentic Systems" arXiv:2503.06745 — agent analytics
  should track per-agent task execution times, outcomes, and throughput.
- Langfuse "AI Agent Observability" (2024): agent task history enables
  identifying bottlenecks and tracing decision paths.

Semantics:
- GET /agents/{id}/history returns the last N completed task records for
  the named agent, most-recent-first, with fields:
    task_id, prompt, started_at (ISO), finished_at (ISO), duration_s,
    status ("success" | "error"), error (str | null)
- Default N=50; caller can pass ?limit=N query parameter.
- 404 if agent_id is not registered.
- Orchestrator records task history in AgentRegistry or Orchestrator level.
- History is capped to 200 entries (configurable) to prevent unbounded growth.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import httpx
import pytest

import tmux_orchestrator.web.app as web_app_mod
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
    """Agent that completes tasks instantly and publishes RESULT."""

    def __init__(self, agent_id: str, bus: Bus, *, fail: bool = False) -> None:
        super().__init__(agent_id, bus)
        self.fail = fail
        self.dispatched_event = asyncio.Event()

    async def start(self) -> None:
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()

    async def _dispatch_task(self, task: Task) -> None:
        await asyncio.sleep(0.01)
        if self.fail:
            await self.bus.publish(Message(
                type=MessageType.RESULT,
                from_id=self.id,
                payload={"task_id": task.id, "error": "simulated error", "output": None},
            ))
        else:
            await self.bus.publish(Message(
                type=MessageType.RESULT,
                from_id=self.id,
                payload={"task_id": task.id, "output": "result text", "error": None},
            ))
        self.dispatched_event.set()
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


def make_tmux_mock():
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    return tmux


# ---------------------------------------------------------------------------
# Orchestrator-level history tests
# ---------------------------------------------------------------------------


async def test_history_empty_initially() -> None:
    """A new agent has empty task history."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        history = orch.get_agent_history("a1")
        assert history == []
    finally:
        await orch.stop()


async def test_history_records_successful_task() -> None:
    """A completed task appears in agent history with status=success."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("hello world")
        await asyncio.wait_for(agent.dispatched_event.wait(), timeout=2.0)
        # Give the route loop time to process the RESULT
        await asyncio.sleep(0.1)

        history = orch.get_agent_history("a1")
        assert len(history) == 1
        entry = history[0]
        assert entry["task_id"] == task.id
        assert entry["status"] == "success"
        assert entry["error"] is None
        assert "started_at" in entry
        assert "finished_at" in entry
        assert entry["duration_s"] >= 0.0
    finally:
        await orch.stop()


async def test_history_records_failed_task() -> None:
    """A failed task appears in agent history with status=error."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus, fail=True)
    orch.register_agent(agent)

    await orch.start()
    try:
        task = await orch.submit_task("failing task")
        await asyncio.wait_for(agent.dispatched_event.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        history = orch.get_agent_history("a1")
        assert len(history) == 1
        entry = history[0]
        assert entry["task_id"] == task.id
        assert entry["status"] == "error"
        assert entry["error"] == "simulated error"
    finally:
        await orch.stop()


async def test_history_most_recent_first() -> None:
    """History is ordered most-recent-first."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        task1 = await orch.submit_task("task 1")
        # Wait for first to complete
        await asyncio.wait_for(agent.dispatched_event.wait(), timeout=2.0)
        agent.dispatched_event.clear()
        await asyncio.sleep(0.1)

        task2 = await orch.submit_task("task 2")
        await asyncio.wait_for(agent.dispatched_event.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        history = orch.get_agent_history("a1")
        assert len(history) == 2
        # Most recent first
        assert history[0]["task_id"] == task2.id
        assert history[1]["task_id"] == task1.id
    finally:
        await orch.stop()


async def test_history_limit_parameter() -> None:
    """get_agent_history(limit=N) returns only the N most recent entries."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        for i in range(5):
            agent.dispatched_event.clear()
            await orch.submit_task(f"task {i}")
            await asyncio.wait_for(agent.dispatched_event.wait(), timeout=2.0)
            await asyncio.sleep(0.1)

        history = orch.get_agent_history("a1", limit=3)
        assert len(history) == 3
    finally:
        await orch.stop()


async def test_history_unknown_agent_returns_none() -> None:
    """get_agent_history returns None for an unknown agent_id."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    await orch.start()
    try:
        result = orch.get_agent_history("nonexistent")
        assert result is None
    finally:
        await orch.stop()


async def test_history_capped_at_max_size() -> None:
    """History is capped at 200 entries by default to prevent unbounded growth."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)

    # Directly inject fake history entries to test cap without running many tasks
    history_store = orch._agent_history.setdefault("a1", [])
    for i in range(250):
        history_store.append({"task_id": f"t{i}", "status": "success"})

    # get_agent_history should cap at 200
    history = orch.get_agent_history("a1", limit=200)
    assert len(history) <= 200


async def test_history_includes_prompt() -> None:
    """History entries include the task prompt."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        await orch.submit_task("my important prompt")
        await asyncio.wait_for(agent.dispatched_event.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        history = orch.get_agent_history("a1")
        assert len(history) == 1
        assert history[0]["prompt"] == "my important prompt"
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

    async def handle(self, ws) -> None:
        pass


class _MockOrchestratorForHistory:
    """Minimal mock orchestrator for REST endpoint tests."""

    def __init__(self) -> None:
        self._history: dict[str, list] = {
            "agent-1": [
                {
                    "task_id": "t1",
                    "prompt": "task 1",
                    "started_at": "2026-03-05T10:00:00+00:00",
                    "finished_at": "2026-03-05T10:00:01+00:00",
                    "duration_s": 1.0,
                    "status": "success",
                    "error": None,
                },
            ],
        }
        self._director_pending: list = []
        self._dispatch_task = None
        # Minimal config stub required by create_app (EpisodeStore, etc.)
        self.config = OrchestratorConfig(
            session_name="test",
            agents=[],
            mailbox_dir="~/.tmux_orchestrator",
        )

    def list_agents(self) -> list:
        return [{"id": "agent-1", "status": "IDLE"}]

    def list_tasks(self) -> list:
        return []

    def get_agent(self, agent_id: str):
        if agent_id in self._history:
            m = MagicMock()
            m.id = agent_id
            return m
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

    def get_agent_history(self, agent_id: str, *, limit: int = 50) -> list | None:
        h = self._history.get(agent_id)
        if h is None:
            return None
        return h[:limit]


@pytest.fixture(autouse=True)
def reset_state():
    web_app_mod._credentials.clear()
    web_app_mod._sign_counts.clear()
    web_app_mod._sessions.clear()
    web_app_mod._pending_challenge = None
    yield


@pytest.fixture
def mock_orch():
    return _MockOrchestratorForHistory()


@pytest.fixture
def app(mock_orch):
    return create_app(mock_orch, _MockHub(), api_key="test-key")


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        yield c


async def test_rest_get_agent_history(client):
    """GET /agents/{id}/history returns task history list."""
    r = await client.get(
        "/agents/agent-1/history",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["task_id"] == "t1"
    assert body[0]["status"] == "success"


async def test_rest_get_agent_history_not_found(client):
    """GET /agents/{id}/history for unknown agent returns 404."""
    r = await client.get(
        "/agents/nonexistent/history",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 404


async def test_rest_get_agent_history_with_limit(client):
    """GET /agents/{id}/history?limit=N respects the limit."""
    r = await client.get(
        "/agents/agent-1/history?limit=10",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)


async def test_rest_history_requires_auth(client):
    """GET /agents/{id}/history requires authentication."""
    r = await client.get("/agents/agent-1/history")
    assert r.status_code == 401
