"""Integration tests: real Bus + Orchestrator + Mailbox, no tmux.

These tests exercise the full orchestration lifecycle without launching
any tmux processes.  The ``HeadlessAgent`` stands in for ClaudeCodeAgent,
implementing the same Agent contract but running entirely in-process.

Each test is marked ``integration`` and can be run with::

    pytest -m integration tests/integration/
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import AgentConfig, OrchestratorConfig
from tmux_orchestrator.messaging import Mailbox
from tmux_orchestrator.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# HeadlessAgent — in-process agent stub
# ---------------------------------------------------------------------------


class HeadlessAgent(Agent):
    """Agent that records interactions without touching tmux or the filesystem.

    On receiving a task it immediately publishes a RESULT and returns to IDLE,
    making it suitable for testing dispatch and routing logic.
    """

    def __init__(
        self,
        agent_id: str,
        bus: Bus,
        *,
        role: str = "worker",
        mailbox: Mailbox | None = None,
        task_timeout: float | None = None,
        result_delay: float = 0.0,
    ) -> None:
        super().__init__(agent_id, bus, task_timeout=task_timeout)
        self.role = role
        self.mailbox = mailbox
        self.dispatched: list[Task] = []
        self.stdin_notifications: list[str] = []
        self._result_delay = result_delay

    async def start(self) -> None:
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(
            self._run_loop(), name=f"{self.id}-loop"
        )
        await self._start_message_loop()

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()
        if self._msg_task:
            self._msg_task.cancel()

    async def _dispatch_task(self, task: Task) -> None:
        self.dispatched.append(task)
        if self._result_delay > 0:
            await asyncio.sleep(self._result_delay)
        await self.bus.publish(
            Message(
                type=MessageType.RESULT,
                from_id=self.id,
                payload={
                    "task_id": task.id,
                    "output": f"done: {task.prompt}",
                    "error": None,
                },
            )
        )
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        self.stdin_notifications.append(notification)


# ---------------------------------------------------------------------------
# HeadlessOrchestrator — spawns HeadlessAgent instead of ClaudeCodeAgent
# ---------------------------------------------------------------------------


class HeadlessOrchestrator(Orchestrator):
    """Orchestrator subclass that replaces _spawn_subagent with in-process version."""

    def __init__(self, bus: Bus, config: OrchestratorConfig) -> None:
        tmux_mock = MagicMock()
        super().__init__(bus, tmux_mock, config)
        # Track spawned sub-agents for assertions
        self.spawned_sub_agents: list[Agent] = []

    async def _spawn_subagent(
        self,
        parent_id: str,
        template_cfg: AgentConfig,
        *,
        share_parent: bool = False,
    ) -> Agent:
        sub_id = f"{parent_id}-sub-{uuid.uuid4().hex[:6]}"
        mailbox = Mailbox(self.config.mailbox_dir, self.config.session_name)
        agent = HeadlessAgent(
            sub_id,
            self.bus,
            role=template_cfg.role,
            mailbox=mailbox,
        )
        self.register_agent(agent, parent_id=parent_id)
        self._p2p.add(frozenset({parent_id, sub_id}))
        await agent.start()
        self.spawned_sub_agents.append(agent)

        await self.bus.publish(
            Message(
                type=MessageType.STATUS,
                from_id="__orchestrator__",
                to_id=parent_id,
                payload={
                    "event": "subagent_spawned",
                    "sub_agent_id": sub_id,
                    "parent_id": parent_id,
                },
            )
        )
        return agent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_config(
    tmp_path: Path,
    agents: list[AgentConfig] | None = None,
    p2p_permissions: list[tuple[str, str]] | None = None,
    **kwargs,
) -> OrchestratorConfig:
    return OrchestratorConfig(
        session_name="integration-test",
        agents=agents or [],
        p2p_permissions=p2p_permissions or [],
        task_timeout=5,
        mailbox_dir=str(tmp_path / "mailbox"),
        **kwargs,
    )


async def collect_messages(
    bus: Bus,
    *,
    predicate,
    timeout: float = 2.0,
    count: int = 1,
) -> list[Message]:
    """Subscribe to all bus messages and collect up to *count* matching ones."""
    listener_id = f"test-listener-{uuid.uuid4().hex[:6]}"
    q = await bus.subscribe(listener_id, broadcast=True)
    results: list[Message] = []
    deadline = asyncio.get_event_loop().time() + timeout
    try:
        while len(results) < count and asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                msg = await asyncio.wait_for(q.get(), timeout=max(remaining, 0.01))
                q.task_done()
                if predicate(msg):
                    results.append(msg)
            except asyncio.TimeoutError:
                break
    finally:
        await bus.unsubscribe(listener_id)
    return results


# ---------------------------------------------------------------------------
# Test: Full dispatch round-trip
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_full_dispatch_round_trip(tmp_path):
    """Submit a task → dispatched to idle HeadlessAgent → RESULT published."""
    bus = Bus()
    cfg = make_config(tmp_path)
    orch = HeadlessOrchestrator(bus, cfg)

    agent = HeadlessAgent("worker-1", bus)
    orch.register_agent(agent)
    await orch.start()

    try:
        task = await orch.submit_task("write tests")

        results = await collect_messages(
            bus,
            predicate=lambda m: (
                m.type == MessageType.RESULT
                and m.payload.get("task_id") == task.id
            ),
        )

        assert len(results) == 1
        assert results[0].payload["output"] == "done: write tests"
        assert len(agent.dispatched) == 1
        assert agent.dispatched[0].id == task.id
        assert agent.status == AgentStatus.IDLE
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# Test: Parallel multi-agent dispatch
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_parallel_multi_agent_dispatch(tmp_path):
    """N tasks dispatched concurrently to N idle agents."""
    N = 3
    bus = Bus()
    cfg = make_config(tmp_path)
    orch = HeadlessOrchestrator(bus, cfg)

    agents = [HeadlessAgent(f"worker-{i}", bus, result_delay=0.05) for i in range(N)]
    for a in agents:
        orch.register_agent(a)
    await orch.start()

    try:
        tasks = [await orch.submit_task(f"task-{i}") for i in range(N)]
        task_ids = {t.id for t in tasks}

        results = await collect_messages(
            bus,
            predicate=lambda m: m.type == MessageType.RESULT and m.payload.get("task_id") in task_ids,
            timeout=3.0,
            count=N,
        )

        assert len(results) == N
        received_task_ids = {r.payload["task_id"] for r in results}
        assert received_task_ids == task_ids

        # All agents should have processed exactly one task
        total_dispatched = sum(len(a.dispatched) for a in agents)
        assert total_dispatched == N
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# Test: P2P with mailbox and stdin notification
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_p2p_with_mailbox_and_stdin_notification(tmp_path):
    """Agent A sends PEER_MSG to B → routed → B's mailbox updated + stdin notified."""
    bus = Bus()
    mailbox = Mailbox(str(tmp_path / "mailbox"), "integration-test")
    cfg = make_config(tmp_path)
    orch = HeadlessOrchestrator(bus, cfg)

    agent_a = HeadlessAgent("agent-a", bus, mailbox=mailbox)
    agent_b = HeadlessAgent("agent-b", bus, mailbox=mailbox)
    orch.register_agent(agent_a)
    orch.register_agent(agent_b)
    await orch.start()

    try:
        # Agents are root-level siblings → hierarchy-permitted
        peer_msg = Message(
            type=MessageType.PEER_MSG,
            from_id="agent-a",
            to_id="agent-b",
            payload={"text": "hello from A"},
        )
        await bus.publish(peer_msg)

        # Wait for the forwarded message to reach agent-b
        forwarded = await collect_messages(
            bus,
            predicate=lambda m: (
                m.type == MessageType.PEER_MSG
                and m.payload.get("_forwarded")
                and m.to_id == "agent-b"
            ),
        )
        assert len(forwarded) == 1
        assert forwarded[0].payload["text"] == "hello from A"

        # agent-b's _message_loop writes to mailbox and calls notify_stdin
        await asyncio.sleep(0.1)  # let message loop process
        inbox = mailbox.list_inbox("agent-b")
        assert len(inbox) == 1

        assert len(agent_b.stdin_notifications) == 1
        assert agent_b.stdin_notifications[0].startswith("__MSG__:")
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# Test: Director result buffering
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_director_result_buffering(tmp_path):
    """Worker RESULT is buffered in orchestrator._director_pending when a director exists."""
    bus = Bus()
    cfg = make_config(tmp_path)
    orch = HeadlessOrchestrator(bus, cfg)

    director = HeadlessAgent("director-1", bus, role="director")
    worker = HeadlessAgent("worker-1", bus, role="worker")
    orch.register_agent(director)
    orch.register_agent(worker)
    await orch.start()

    try:
        task = await orch.submit_task("compute something")

        # Wait for the RESULT to be published
        await collect_messages(
            bus,
            predicate=lambda m: m.type == MessageType.RESULT and m.payload.get("task_id") == task.id,
        )
        await asyncio.sleep(0.05)  # let route loop buffer

        assert len(orch._director_pending) == 1
        assert "worker-1" in orch._director_pending[0]
        assert task.id in orch._director_pending[0]
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# Test: Sub-agent spawning via CONTROL message
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_subagent_spawning_via_control(tmp_path):
    """Parent sends CONTROL spawn_subagent → sub-agent created → STATUS response."""
    bus = Bus()
    worker_template = AgentConfig(id="worker-template", type="claude_code", role="worker")
    cfg = make_config(tmp_path, agents=[worker_template])
    orch = HeadlessOrchestrator(bus, cfg)

    parent = HeadlessAgent("parent-agent", bus)
    orch.register_agent(parent)
    await orch.start()

    try:
        # Send CONTROL message requesting sub-agent spawn
        control_msg = Message(
            type=MessageType.CONTROL,
            from_id="parent-agent",
            to_id="__orchestrator__",
            payload={"action": "spawn_subagent", "template_id": "worker-template"},
        )
        await bus.publish(control_msg)

        # Wait for STATUS subagent_spawned
        status_msgs = await collect_messages(
            bus,
            predicate=lambda m: (
                m.type == MessageType.STATUS
                and m.payload.get("event") == "subagent_spawned"
                and m.payload.get("parent_id") == "parent-agent"
            ),
            timeout=2.0,
        )

        assert len(status_msgs) == 1
        sub_id = status_msgs[0].payload["sub_agent_id"]
        assert sub_id.startswith("parent-agent-sub-")

        # Sub-agent should be registered
        assert orch.get_agent(sub_id) is not None
        assert orch._agent_parents.get(sub_id) == "parent-agent"

        # P2P between parent and sub-agent should be granted
        assert len(orch.spawned_sub_agents) == 1
    finally:
        await orch.stop()
