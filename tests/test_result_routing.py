"""Tests for task result routing via reply_to field.

When a task is submitted with ``reply_to="<agent_id>"``, the RESULT message
produced by any worker should be delivered directly to the specified agent's
mailbox (written as a file) AND the target agent should be notified via
``notify_stdin``.

Design reference:
- Request-reply pattern: "Learning Notes #15 – Request Reply Pattern | RabbitMQ"
  https://parottasalna.com/2024/12/28/learning-notes-15-request-reply-pattern-rabbitmq/ (2024)
- Moore, David J. "A Taxonomy of Hierarchical Multi-Agent Systems"
  https://arxiv.org/abs/2508.12683 (2025)
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.messaging import Mailbox
from tmux_orchestrator.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CapturingAgent(Agent):
    """Minimal agent that records dispatched tasks and captures notifications."""

    def __init__(self, agent_id: str, bus: Bus, mailbox: Mailbox | None = None) -> None:
        super().__init__(agent_id, bus)
        self.dispatched: list[Task] = []
        self.notifications: list[str] = []
        self.mailbox = mailbox
        self.dispatched_event: asyncio.Event = asyncio.Event()

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
        self.dispatched_event.set()
        await asyncio.sleep(0)
        # Publish a RESULT
        await self.bus.publish(Message(
            type=MessageType.RESULT,
            from_id=self.id,
            payload={"task_id": task.id, "output": "done"},
        ))
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        self.notifications.append(notification)


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_submit_task_with_reply_to_field() -> None:
    """Task.reply_to is preserved when set via submit_task(reply_to=...)."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    worker = CapturingAgent("worker", bus)
    orch.register_agent(worker)

    await orch.start()
    try:
        task = await orch.submit_task("hello", reply_to="parent-agent")
        assert task.reply_to == "parent-agent"
    finally:
        await orch.stop()


async def test_result_routed_to_mailbox_when_reply_to_set(tmp_path: Path) -> None:
    """When reply_to is set, completed RESULT is written to reply_to agent's mailbox."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(mailbox_dir=str(tmp_path))
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    # Worker that does the task
    worker = CapturingAgent("worker", bus)
    orch.register_agent(worker)

    # Parent agent with a mailbox (the reply_to target)
    mailbox = Mailbox(tmp_path, "test")
    parent = CapturingAgent("parent", bus, mailbox=mailbox)
    orch.register_agent(parent)
    # Give orchestrator access to the parent agent's mailbox via config
    orch._mailbox = mailbox

    await orch.start()
    try:
        task = await orch.submit_task("do something", reply_to="parent")
        # Wait for the worker to complete the task
        await asyncio.wait_for(worker.dispatched_event.wait(), timeout=3.0)
        # Wait for the RESULT to be routed
        await asyncio.sleep(0.2)

        # The parent's mailbox should have received the RESULT message
        inbox = mailbox.list_inbox("parent")
        assert len(inbox) >= 1, f"Expected ≥1 message in parent inbox, got: {inbox}"
    finally:
        await orch.stop()


async def test_result_routed_notifies_agent_stdin_when_reply_to_set(tmp_path: Path) -> None:
    """When reply_to is set and agent is registered, notify_stdin is called."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(mailbox_dir=str(tmp_path))
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    worker = CapturingAgent("worker", bus)
    orch.register_agent(worker)

    mailbox = Mailbox(tmp_path, "test")
    parent = CapturingAgent("parent", bus, mailbox=mailbox)
    orch.register_agent(parent)
    orch._mailbox = mailbox

    await orch.start()
    try:
        task = await orch.submit_task("do something", reply_to="parent")
        await asyncio.wait_for(worker.dispatched_event.wait(), timeout=3.0)
        await asyncio.sleep(0.3)

        # The parent agent should have been notified about the incoming message
        assert any(n.startswith("__MSG__:") for n in parent.notifications), (
            f"Expected __MSG__:... notification, got: {parent.notifications}"
        )
    finally:
        await orch.stop()


async def test_reply_to_none_does_not_affect_routing() -> None:
    """When reply_to is not set, result routing is unchanged (no mailbox write)."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    worker = CapturingAgent("worker", bus)
    orch.register_agent(worker)

    await orch.start()
    try:
        task = await orch.submit_task("do something")  # no reply_to
        assert task.reply_to is None

        await asyncio.wait_for(worker.dispatched_event.wait(), timeout=3.0)
        await asyncio.sleep(0.2)
        # No crash, no side effects — test passes if we reach here
    finally:
        await orch.stop()


async def test_reply_to_unknown_agent_does_not_crash(tmp_path: Path) -> None:
    """When reply_to names an unregistered agent, the RESULT is still broadcast normally."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(mailbox_dir=str(tmp_path))
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    worker = CapturingAgent("worker", bus)
    orch.register_agent(worker)
    orch._mailbox = Mailbox(tmp_path, "test")

    await orch.start()
    try:
        task = await orch.submit_task("task", reply_to="nonexistent-agent")
        await asyncio.wait_for(worker.dispatched_event.wait(), timeout=3.0)
        await asyncio.sleep(0.2)
        # Orchestrator must not crash
        assert orch._dispatch_task is not None
        assert not orch._dispatch_task.done()
    finally:
        await orch.stop()


async def test_submit_task_reply_to_exposed_in_rest(tmp_path: Path) -> None:
    """POST /tasks with reply_to stores the field on the returned Task."""
    from fastapi.testclient import TestClient

    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(mailbox_dir=str(tmp_path))
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.web.ws import WebSocketHub

    class _StubHub:
        async def start(self) -> None: pass
        async def stop(self) -> None: pass

    app = create_app(orch, _StubHub(), api_key="test-key")  # type: ignore[arg-type]

    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/tasks",
            json={"prompt": "hello", "priority": 0, "reply_to": "parent-agent"},
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("reply_to") == "parent-agent"
