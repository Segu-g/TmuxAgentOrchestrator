"""Tests for v1.2.13 task timeout escalation.

Feature: When a task times out (watchdog_timeout), it is re-queued at higher
priority with the timed-out agent added to excluded_agents, up to
max_task_escalations times.  After exhausting escalations, the task is
finally failed.

Design reference: DESIGN.md §10.89 (v1.2.13)
Research:
- GitGuardian "Celery Task Resilience" (2024) — escalating retry:
  https://blog.gitguardian.com/celery-tasks-retries-errors/
- Temporal WorkflowTaskTimeout reassignment (2024) — avoid stuck worker:
  https://community.temporal.io/t/handling-workflow-task-timeout-due-to-sticky-queue-task-timeout/16443
- Wikipedia "Aging (scheduling)" — priority bump on re-queue:
  https://en.wikipedia.org/wiki/Aging_(scheduling)
- AWS Builders Library "Timeouts, retries and backoff with jitter" (2022):
  https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tmux_orchestrator.application.bus import Bus, Message, MessageType
from tmux_orchestrator.application.config import AgentConfig, OrchestratorConfig
from tmux_orchestrator.application.orchestrator import Orchestrator
from tmux_orchestrator.application.registry import AgentRegistry
from tmux_orchestrator.domain.agent import AgentRole, AgentStatus
from tmux_orchestrator.domain.task import Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_tmux_mock():
    tmux = MagicMock()
    tmux.new_pane = MagicMock(return_value=MagicMock(id="pane-1"))
    tmux.new_subpane = MagicMock(return_value=MagicMock(id="pane-2"))
    tmux.send_keys = MagicMock()
    tmux.watch_pane = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.capture_pane = MagicMock(return_value="❯ ")
    tmux.unwatch_pane = MagicMock()
    return tmux


def make_agent_config(agent_id="worker", **kwargs) -> AgentConfig:
    defaults = dict(
        id=agent_id,
        type="claude_code",
        isolate=False,
        system_prompt="You are a worker.",
        tags=[],
    )
    defaults.update(kwargs)
    return AgentConfig(**defaults)


def make_orch_config(
    task_escalation_enabled=True,
    max_task_escalations=2,
    **kwargs,
) -> OrchestratorConfig:
    defaults = dict(
        session_name="test",
        task_timeout=30,
        watchdog_poll=999,
        task_escalation_enabled=task_escalation_enabled,
        max_task_escalations=max_task_escalations,
    )
    defaults.update(kwargs)
    return OrchestratorConfig(**defaults)


def make_orchestrator(config=None):
    bus = Bus()
    tmux = make_tmux_mock()
    if config is None:
        config = make_orch_config()
    return Orchestrator(bus=bus, tmux=tmux, config=config)


def make_agent_mock(agent_id: str, status=AgentStatus.IDLE):
    m = MagicMock()
    m.id = agent_id
    m.stop = AsyncMock()
    m.start = AsyncMock()
    m.status = status
    m.worktree_path = None
    m.started_at = None
    m.uptime_s = 0.0
    m.tags = []
    m.role = AgentRole.WORKER
    m._current_task = None
    return m


def make_task(task_id="t1", priority=5, escalation_count=0, excluded_agents=None) -> Task:
    return Task(
        id=task_id,
        prompt="do something",
        priority=priority,
        escalation_count=escalation_count,
        excluded_agents=excluded_agents or [],
    )


# ---------------------------------------------------------------------------
# 1. Task dataclass fields
# ---------------------------------------------------------------------------


class TestTaskFields:
    def test_escalation_count_defaults_to_zero(self):
        t = Task(id="t1", prompt="hello")
        assert t.escalation_count == 0

    def test_excluded_agents_defaults_to_empty_list(self):
        t = Task(id="t1", prompt="hello")
        assert t.excluded_agents == []

    def test_excluded_agents_is_independent_per_instance(self):
        t1 = Task(id="t1", prompt="a")
        t2 = Task(id="t2", prompt="b")
        t1.excluded_agents.append("worker-1")
        assert t2.excluded_agents == []

    def test_to_dict_includes_escalation_count(self):
        t = Task(id="t1", prompt="hello", escalation_count=2)
        d = t.to_dict()
        assert d["escalation_count"] == 2

    def test_to_dict_excludes_excluded_agents_when_empty(self):
        t = Task(id="t1", prompt="hello")
        d = t.to_dict()
        assert "excluded_agents" not in d

    def test_to_dict_includes_excluded_agents_when_non_empty(self):
        t = Task(id="t1", prompt="hello", excluded_agents=["worker-1"])
        d = t.to_dict()
        assert d["excluded_agents"] == ["worker-1"]


# ---------------------------------------------------------------------------
# 2. OrchestratorConfig fields
# ---------------------------------------------------------------------------


class TestOrchestratorConfigFields:
    def test_task_escalation_enabled_defaults_to_true(self):
        cfg = OrchestratorConfig(task_timeout=30, watchdog_poll=999)
        assert cfg.task_escalation_enabled is True

    def test_max_task_escalations_defaults_to_two(self):
        cfg = OrchestratorConfig(task_timeout=30, watchdog_poll=999)
        assert cfg.max_task_escalations == 2

    def test_task_escalation_enabled_can_be_set_false(self):
        cfg = make_orch_config(task_escalation_enabled=False)
        assert cfg.task_escalation_enabled is False

    def test_max_task_escalations_can_be_set(self):
        cfg = make_orch_config(max_task_escalations=5)
        assert cfg.max_task_escalations == 5


# ---------------------------------------------------------------------------
# 3. _handle_task_timeout unit tests
# ---------------------------------------------------------------------------


class TestHandleTaskTimeout:
    @pytest.mark.asyncio
    async def test_escalation_requeues_task(self):
        """On first timeout, task is re-queued, not failed."""
        orch = make_orchestrator(make_orch_config(max_task_escalations=2))
        task = make_task("t1", priority=5)
        orch._active_tasks["t1"] = task
        orch._task_priorities["t1"] = task.priority

        escalated = await orch._handle_task_timeout(task, "worker-a")

        assert escalated is True
        # Task should now be back in queue
        assert not orch._task_queue.empty()

    @pytest.mark.asyncio
    async def test_escalation_count_increments(self):
        """Re-queued task has escalation_count = 1."""
        orch = make_orchestrator(make_orch_config(max_task_escalations=2))
        task = make_task("t1", priority=5, escalation_count=0)
        orch._active_tasks["t1"] = task
        orch._task_priorities["t1"] = task.priority

        await orch._handle_task_timeout(task, "worker-a")

        # Retrieve from queue
        _, _, queued_task = await orch._task_queue.get()
        assert queued_task.escalation_count == 1

    @pytest.mark.asyncio
    async def test_timed_out_agent_added_to_excluded(self):
        """Timed-out agent is in escalated task's excluded_agents."""
        orch = make_orchestrator(make_orch_config(max_task_escalations=2))
        task = make_task("t1", priority=5)
        orch._active_tasks["t1"] = task
        orch._task_priorities["t1"] = task.priority

        await orch._handle_task_timeout(task, "worker-a")

        _, _, queued_task = await orch._task_queue.get()
        assert "worker-a" in queued_task.excluded_agents

    @pytest.mark.asyncio
    async def test_priority_is_bumped_lower_number(self):
        """Re-queued task has priority = max(0, original - 1)."""
        orch = make_orchestrator(make_orch_config(max_task_escalations=2))
        task = make_task("t1", priority=5)
        orch._active_tasks["t1"] = task
        orch._task_priorities["t1"] = task.priority

        await orch._handle_task_timeout(task, "worker-a")

        _, _, queued_task = await orch._task_queue.get()
        assert queued_task.priority == 4  # 5 - 1

    @pytest.mark.asyncio
    async def test_priority_does_not_go_below_zero(self):
        """Priority floor is 0."""
        orch = make_orchestrator(make_orch_config(max_task_escalations=2))
        task = make_task("t1", priority=0)
        orch._active_tasks["t1"] = task
        orch._task_priorities["t1"] = task.priority

        await orch._handle_task_timeout(task, "worker-a")

        _, _, queued_task = await orch._task_queue.get()
        assert queued_task.priority == 0

    @pytest.mark.asyncio
    async def test_after_max_escalations_returns_false(self):
        """After max_escalations the method returns False (final failure)."""
        orch = make_orchestrator(make_orch_config(max_task_escalations=1))
        # Task already escalated once
        task = make_task("t1", priority=5, escalation_count=1, excluded_agents=["worker-a"])
        orch._active_tasks["t1"] = task
        orch._task_priorities["t1"] = task.priority

        result = await orch._handle_task_timeout(task, "worker-b")

        assert result is False
        # Queue should be empty — task was NOT re-queued
        assert orch._task_queue.empty()

    @pytest.mark.asyncio
    async def test_escalation_disabled_returns_false(self):
        """When task_escalation_enabled=False, always returns False."""
        orch = make_orchestrator(make_orch_config(task_escalation_enabled=False))
        task = make_task("t1", priority=5)
        orch._active_tasks["t1"] = task
        orch._task_priorities["t1"] = task.priority

        result = await orch._handle_task_timeout(task, "worker-a")

        assert result is False
        assert orch._task_queue.empty()

    @pytest.mark.asyncio
    async def test_task_escalated_bus_event_published(self):
        """task_escalated STATUS event is published with correct payload."""
        orch = make_orchestrator(make_orch_config(max_task_escalations=2))
        task = make_task("t1", priority=5)
        orch._active_tasks["t1"] = task
        orch._task_priorities["t1"] = task.priority

        received: list[Message] = []

        async def listener(msg: Message) -> None:
            received.append(msg)

        # Subscribe to all messages (broadcast=True)
        sub_queue = await orch.bus.subscribe("test-listener", broadcast=True)

        await orch._handle_task_timeout(task, "worker-a")

        # Allow event to propagate
        await asyncio.sleep(0)

        # Drain the subscription queue
        events = []
        while not sub_queue.empty():
            m = sub_queue.get_nowait()
            sub_queue.task_done()
            events.append(m)

        task_escalated_events = [
            e for e in events
            if e.type == MessageType.STATUS
            and e.payload.get("event") == "task_escalated"
        ]
        assert len(task_escalated_events) == 1
        payload = task_escalated_events[0].payload
        assert payload["task_id"] == "t1"
        assert payload["escalation_count"] == 1
        assert "worker-a" in payload["excluded_agents"]
        assert payload["new_priority"] == 4
        assert payload["timed_out_agent_id"] == "worker-a"

    @pytest.mark.asyncio
    async def test_multiple_exclusions_accumulate(self):
        """Each escalation adds one more agent to excluded_agents."""
        orch = make_orchestrator(make_orch_config(max_task_escalations=3))
        task = make_task("t1", priority=5, escalation_count=1, excluded_agents=["worker-a"])
        orch._active_tasks["t1"] = task
        orch._task_priorities["t1"] = task.priority

        await orch._handle_task_timeout(task, "worker-b")

        _, _, queued_task = await orch._task_queue.get()
        assert "worker-a" in queued_task.excluded_agents
        assert "worker-b" in queued_task.excluded_agents
        assert queued_task.escalation_count == 2


# ---------------------------------------------------------------------------
# 4. find_idle_worker respects excluded_agent_ids
# ---------------------------------------------------------------------------


class TestFindIdleWorkerExcluded:
    def _make_registry(self) -> AgentRegistry:
        return AgentRegistry(p2p_permissions=[])

    def _make_agent(self, agent_id: str, status=AgentStatus.IDLE):
        m = MagicMock()
        m.id = agent_id
        m.status = status
        m.role = AgentRole.WORKER
        m.tags = []
        return m

    def test_excluded_agent_skipped(self):
        registry = AgentRegistry(p2p_permissions=[])
        worker_a = self._make_agent("worker-a")
        worker_b = self._make_agent("worker-b")
        registry.register(worker_a)
        registry.register(worker_b)

        result = registry.find_idle_worker(excluded_agent_ids={"worker-a"})

        assert result is not None
        assert result.id == "worker-b"

    def test_all_excluded_returns_none(self):
        registry = AgentRegistry(p2p_permissions=[])
        worker_a = self._make_agent("worker-a")
        registry.register(worker_a)

        result = registry.find_idle_worker(excluded_agent_ids={"worker-a"})

        assert result is None

    def test_empty_excluded_returns_first_idle(self):
        registry = AgentRegistry(p2p_permissions=[])
        worker_a = self._make_agent("worker-a")
        registry.register(worker_a)

        result = registry.find_idle_worker(excluded_agent_ids=set())

        assert result is not None
        assert result.id == "worker-a"

    def test_none_excluded_returns_first_idle(self):
        registry = AgentRegistry(p2p_permissions=[])
        worker_a = self._make_agent("worker-a")
        registry.register(worker_a)

        result = registry.find_idle_worker(excluded_agent_ids=None)

        assert result is not None
        assert result.id == "worker-a"
