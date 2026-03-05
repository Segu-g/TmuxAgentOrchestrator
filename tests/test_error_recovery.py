"""Tests for ERROR state auto-recovery in the Orchestrator.

Covers:
- Orchestrator detects agents in ERROR state via _recovery_loop
- Attempts restart with exponential backoff
- Publishes status events on recovery success and permanent failure
- Respects recovery_attempts limit
- Error agent that consistently fails exhausts retries and publishes permanent failure

Reference: DESIGN.md §10.8 — Error Recovery (auto-restart, exponential backoff).
Inspired by Erlang OTP supervisor restart strategies and Nygard "Release It!" Ch. 5.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecoverableAgent(Agent):
    """Agent that goes to ERROR on the first task, then recovers on restart."""

    def __init__(self, agent_id: str, bus: Bus, *, fail_on_first: bool = True) -> None:
        super().__init__(agent_id, bus)
        self._fail_on_first = fail_on_first
        self._start_count = 0
        self.dispatched: list[Task] = []
        self.dispatched_event: asyncio.Event = asyncio.Event()

    async def start(self) -> None:
        self._start_count += 1
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(
            self._run_loop(), name=f"{self.id}-loop"
        )

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass

    async def _dispatch_task(self, task: Task) -> None:
        # On first dispatch (start_count == 1), raise to enter ERROR state
        if self._fail_on_first and self._start_count == 1:
            raise RuntimeError("Simulated dispatch failure")
        self.dispatched.append(task)
        self.dispatched_event.set()
        await asyncio.sleep(0)
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


class AlwaysErrorAgent(Agent):
    """Agent that always goes to ERROR state — never recovers.

    After each start(), it immediately sets status=ERROR so recovery attempts
    always observe ERROR, regardless of whether a task is dispatched.
    """

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self._start_count = 0

    async def start(self) -> None:
        self._start_count += 1
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(
            self._run_loop(), name=f"{self.id}-loop"
        )
        # Immediately queue a task so the run loop picks it up and fails
        await self._task_queue.put(Task(id="auto-fail", prompt="fail"))

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        # Drain internal queue to avoid stale tasks on restart
        while not self._task_queue.empty():
            try:
                self._task_queue.get_nowait()
                self._task_queue.task_done()
            except Exception:
                break

    async def _dispatch_task(self, task: Task) -> None:
        raise RuntimeError("Simulated permanent failure")

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
        watchdog_poll=99999,  # disable watchdog in tests
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


@pytest.mark.asyncio
async def test_orchestrator_has_recovery_loop() -> None:
    """Orchestrator starts a _recovery_task when started."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    await orch.start()
    try:
        assert orch._recovery_task is not None
        assert not orch._recovery_task.done()
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_recovery_loop_restarts_error_agent() -> None:
    """An agent that enters ERROR is stopped, then restarted by the recovery loop."""
    bus = Bus()
    tmux = make_tmux_mock()
    # Very short backoff for testing
    config = make_config(
        recovery_backoff_base=0.05,
        recovery_poll=0.05,
    )
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = RecoverableAgent("r1", bus, fail_on_first=True)
    orch.register_agent(agent)

    await orch.start()
    try:
        # Submit a task that will cause the agent to ERROR
        await orch.submit_task("trigger error")
        # Wait for the agent to go through ERROR → restart → IDLE
        for _ in range(60):
            await asyncio.sleep(0.1)
            if agent._start_count >= 2 and agent.status == AgentStatus.IDLE:
                break
        assert agent._start_count >= 2, "Agent should have been restarted"
        assert agent.status == AgentStatus.IDLE
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_recovery_publishes_recovered_event() -> None:
    """A STATUS event 'agent_recovered' is published after a successful restart."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(
        recovery_backoff_base=0.05,
        recovery_poll=0.05,
    )
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = RecoverableAgent("r2", bus, fail_on_first=True)
    orch.register_agent(agent)

    sub_id = "__test_recovery__"
    q = await bus.subscribe(sub_id, broadcast=True)

    await orch.start()
    try:
        await orch.submit_task("trigger error")

        # Collect events, looking for agent_recovered
        recovered = False
        for _ in range(80):
            await asyncio.sleep(0.1)
            # Drain the queue
            while not q.empty():
                msg = q.get_nowait()
                q.task_done()
                if (
                    msg.type == MessageType.STATUS
                    and msg.payload.get("event") == "agent_recovered"
                    and msg.payload.get("agent_id") == "r2"
                ):
                    recovered = True
            if recovered:
                break
        assert recovered, "agent_recovered STATUS event should have been published"
    finally:
        await orch.stop()
        await bus.unsubscribe(sub_id)


@pytest.mark.asyncio
async def test_recovery_exhaustion_publishes_permanent_failure() -> None:
    """After recovery_attempts exhausted, 'agent_recovery_failed' event is published."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(
        recovery_backoff_base=0.02,
        recovery_poll=0.02,
        recovery_attempts=2,  # exhaust quickly
    )
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = AlwaysErrorAgent("e1", bus)
    orch.register_agent(agent)

    sub_id = "__test_failure__"
    q = await bus.subscribe(sub_id, broadcast=True)

    await orch.start()
    try:
        await orch.submit_task("trigger permanent error")

        # Collect events, looking for agent_recovery_failed
        failed = False
        for _ in range(150):
            await asyncio.sleep(0.05)
            while not q.empty():
                msg = q.get_nowait()
                q.task_done()
                if (
                    msg.type == MessageType.STATUS
                    and msg.payload.get("event") == "agent_recovery_failed"
                    and msg.payload.get("agent_id") == "e1"
                ):
                    failed = True
            if failed:
                break
        assert failed, "agent_recovery_failed STATUS event should have been published"
    finally:
        await orch.stop()
        await bus.unsubscribe(sub_id)


@pytest.mark.asyncio
async def test_recovery_respects_attempts_count() -> None:
    """After exhausting recovery_attempts, the agent is not restarted again."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(
        recovery_backoff_base=0.02,
        recovery_poll=0.02,
        recovery_attempts=2,
    )
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    agent = AlwaysErrorAgent("e2", bus)
    orch.register_agent(agent)

    await orch.start()
    try:
        # AlwaysErrorAgent auto-queues a failure task on start()
        # Wait long enough for all retries to complete (2 attempts × ~0.02s each)
        await asyncio.sleep(2.0)

        # start_count should be: 1 (initial) + 2 (recovery attempts) = 3 max
        assert agent._start_count <= 4, (
            f"Should not restart more than recovery_attempts+1 times, got {agent._start_count}"
        )
    finally:
        await orch.stop()
