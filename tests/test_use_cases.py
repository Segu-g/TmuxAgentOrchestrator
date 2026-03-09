"""Tests for application-layer use case interactors.

Tests ``SubmitTaskUseCase`` and ``CancelTaskUseCase`` against a stub
``TaskService`` implementation, verifying that:
  - DTOs are correctly mapped to domain calls and back.
  - The use case orchestrates the service without containing business logic itself.
  - ``CancelTaskResult.was_running`` accurately reflects in-progress state.
  - ``SubmitTaskResult.to_dict()`` and ``CancelTaskResult.to_dict()`` produce
    JSON-serialisable plain dicts.

No real orchestrator or tmux session is started; tests use minimal stubs.

Reference: DESIGN.md §10.33 (v1.0.33 — UseCaseInteractor layer extraction)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from tmux_orchestrator.application.use_cases import (
    CancelTaskDTO,
    CancelTaskResult,
    CancelTaskUseCase,
    SubmitTaskDTO,
    SubmitTaskResult,
    SubmitTaskUseCase,
    TaskService,
)
from tmux_orchestrator.domain.task import Task


# ---------------------------------------------------------------------------
# Stub TaskService
# ---------------------------------------------------------------------------


@dataclass
class _StubAgent:
    """Minimal agent stub for was_running detection."""
    id: str
    _current_task: Task | None = None


class StubTaskService:
    """In-memory stub satisfying ``TaskService`` protocol for unit tests."""

    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._agents: list[_StubAgent] = []
        self._cancelled: set[str] = set()
        # Track calls for assertions
        self.submit_calls: list[dict] = []
        self.cancel_calls: list[str] = []

    def add_agent(self, agent: _StubAgent) -> None:
        self._agents.append(agent)

    async def submit_task(
        self,
        prompt: str,
        *,
        priority: int = 0,
        metadata: dict | None = None,
        depends_on: list[str] | None = None,
        idempotency_key: str | None = None,
        reply_to: str | None = None,
        target_agent: str | None = None,
        required_tags: list[str] | None = None,
        target_group: str | None = None,
        max_retries: int = 0,
        inherit_priority: bool = True,
        ttl: float | None = None,
        _task_id: str | None = None,
    ) -> Task:
        self.submit_calls.append({"prompt": prompt, "priority": priority})
        t = Task(
            id=_task_id or "task-001",
            prompt=prompt,
            priority=priority,
            metadata=metadata or {},
            depends_on=depends_on or [],
            reply_to=reply_to,
            target_agent=target_agent,
            required_tags=required_tags or [],
            target_group=target_group,
            max_retries=max_retries,
            inherit_priority=inherit_priority,
            ttl=ttl,
        )
        t.submitted_at = time.time()
        if ttl is not None:
            t.expires_at = t.submitted_at + ttl
        else:
            t.expires_at = None
        self._tasks[t.id] = t
        return t

    async def cancel_task(self, task_id: str) -> bool:
        self.cancel_calls.append(task_id)
        if task_id in self._tasks and task_id not in self._cancelled:
            self._cancelled.add(task_id)
            return True
        return False

    def list_agents(self) -> list[dict]:
        return [{"id": a.id} for a in self._agents]

    def get_agent(self, agent_id: str) -> _StubAgent | None:
        for a in self._agents:
            if a.id == agent_id:
                return a
        return None


# ---------------------------------------------------------------------------
# SubmitTaskUseCase tests
# ---------------------------------------------------------------------------


class TestSubmitTaskUseCase:
    @pytest.mark.asyncio
    async def test_execute_returns_submit_task_result(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        dto = SubmitTaskDTO(prompt="Hello world")
        result = await uc.execute(dto)
        assert isinstance(result, SubmitTaskResult)

    @pytest.mark.asyncio
    async def test_execute_passes_prompt(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        result = await uc.execute(SubmitTaskDTO(prompt="Test task"))
        assert result.prompt == "Test task"

    @pytest.mark.asyncio
    async def test_execute_passes_priority(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        result = await uc.execute(SubmitTaskDTO(prompt="p", priority=5))
        assert result.priority == 5

    @pytest.mark.asyncio
    async def test_execute_passes_reply_to(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        result = await uc.execute(SubmitTaskDTO(prompt="p", reply_to="agent-1"))
        assert result.reply_to == "agent-1"

    @pytest.mark.asyncio
    async def test_execute_passes_target_agent(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        result = await uc.execute(SubmitTaskDTO(prompt="p", target_agent="worker-1"))
        assert result.target_agent == "worker-1"

    @pytest.mark.asyncio
    async def test_execute_passes_required_tags(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        result = await uc.execute(SubmitTaskDTO(prompt="p", required_tags=["gpu", "fast"]))
        assert result.required_tags == ["gpu", "fast"]

    @pytest.mark.asyncio
    async def test_execute_passes_max_retries(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        result = await uc.execute(SubmitTaskDTO(prompt="p", max_retries=3))
        assert result.max_retries == 3

    @pytest.mark.asyncio
    async def test_execute_passes_ttl(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        result = await uc.execute(SubmitTaskDTO(prompt="p", ttl=60.0))
        assert result.ttl == 60.0

    @pytest.mark.asyncio
    async def test_execute_sets_expires_at_when_ttl_set(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        before = time.time()
        result = await uc.execute(SubmitTaskDTO(prompt="p", ttl=60.0))
        after = time.time()
        assert result.expires_at is not None
        assert before + 60.0 <= result.expires_at <= after + 60.0

    @pytest.mark.asyncio
    async def test_execute_no_ttl_expires_at_is_none(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        result = await uc.execute(SubmitTaskDTO(prompt="p"))
        assert result.expires_at is None

    @pytest.mark.asyncio
    async def test_execute_calls_service_submit(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        await uc.execute(SubmitTaskDTO(prompt="my task"))
        assert len(svc.submit_calls) == 1
        assert svc.submit_calls[0]["prompt"] == "my task"

    @pytest.mark.asyncio
    async def test_execute_task_id_in_result(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        result = await uc.execute(SubmitTaskDTO(prompt="x"))
        assert result.task_id == "task-001"

    @pytest.mark.asyncio
    async def test_execute_depends_on_empty_list_not_in_dict(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        result = await uc.execute(SubmitTaskDTO(prompt="x", depends_on=[]))
        d = result.to_dict()
        assert "depends_on" not in d

    @pytest.mark.asyncio
    async def test_execute_depends_on_non_empty_in_dict(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        result = await uc.execute(SubmitTaskDTO(prompt="x", depends_on=["task-000"]))
        d = result.to_dict()
        assert d["depends_on"] == ["task-000"]

    @pytest.mark.asyncio
    async def test_to_dict_always_has_required_keys(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        result = await uc.execute(SubmitTaskDTO(prompt="x"))
        d = result.to_dict()
        for key in ("task_id", "prompt", "priority", "max_retries", "retry_count",
                    "inherit_priority", "submitted_at", "ttl", "expires_at"):
            assert key in d, f"Missing key: {key}"

    @pytest.mark.asyncio
    async def test_optional_fields_absent_when_none(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        result = await uc.execute(SubmitTaskDTO(prompt="x"))
        d = result.to_dict()
        assert "reply_to" not in d
        assert "target_agent" not in d
        assert "target_group" not in d


# ---------------------------------------------------------------------------
# CancelTaskUseCase tests
# ---------------------------------------------------------------------------


class TestCancelTaskUseCase:
    @pytest.mark.asyncio
    async def test_execute_returns_cancel_task_result(self):
        svc = StubTaskService()
        uc = SubmitTaskUseCase(svc)
        submit_result = await uc.execute(SubmitTaskDTO(prompt="x"))
        cancel_uc = CancelTaskUseCase(svc)
        result = await cancel_uc.execute(CancelTaskDTO(task_id=submit_result.task_id))
        assert isinstance(result, CancelTaskResult)

    @pytest.mark.asyncio
    async def test_cancel_existing_task_returns_true(self):
        svc = StubTaskService()
        sub_uc = SubmitTaskUseCase(svc)
        sub_result = await sub_uc.execute(SubmitTaskDTO(prompt="x"))
        cancel_uc = CancelTaskUseCase(svc)
        result = await cancel_uc.execute(CancelTaskDTO(task_id=sub_result.task_id))
        assert result.cancelled is True

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_task_returns_false(self):
        svc = StubTaskService()
        cancel_uc = CancelTaskUseCase(svc)
        result = await cancel_uc.execute(CancelTaskDTO(task_id="no-such-task"))
        assert result.cancelled is False

    @pytest.mark.asyncio
    async def test_cancel_preserves_task_id(self):
        svc = StubTaskService()
        cancel_uc = CancelTaskUseCase(svc)
        result = await cancel_uc.execute(CancelTaskDTO(task_id="xyz"))
        assert result.task_id == "xyz"

    @pytest.mark.asyncio
    async def test_was_running_false_when_not_dispatched(self):
        """A queued but not-dispatched task: was_running=False."""
        svc = StubTaskService()
        sub_uc = SubmitTaskUseCase(svc)
        sub_result = await sub_uc.execute(SubmitTaskDTO(prompt="x"))
        cancel_uc = CancelTaskUseCase(svc)
        result = await cancel_uc.execute(CancelTaskDTO(task_id=sub_result.task_id))
        assert result.was_running is False

    @pytest.mark.asyncio
    async def test_was_running_true_when_task_is_dispatched(self):
        """Task currently assigned to an agent: was_running=True."""
        svc = StubTaskService()
        task = Task(id="running-task", prompt="active")
        task.submitted_at = time.time()
        task.expires_at = None
        svc._tasks["running-task"] = task
        # Simulate agent executing this task
        agent = _StubAgent(id="worker-1", _current_task=task)
        svc.add_agent(agent)

        cancel_uc = CancelTaskUseCase(svc)
        result = await cancel_uc.execute(CancelTaskDTO(task_id="running-task"))
        assert result.was_running is True

    @pytest.mark.asyncio
    async def test_was_running_false_when_different_task_running(self):
        """Agent is running a *different* task: was_running=False for our task."""
        svc = StubTaskService()
        other_task = Task(id="other-task", prompt="other")
        other_task.submitted_at = time.time()
        other_task.expires_at = None
        svc._tasks["other-task"] = other_task

        our_task = Task(id="our-task", prompt="ours")
        our_task.submitted_at = time.time()
        our_task.expires_at = None
        svc._tasks["our-task"] = our_task

        agent = _StubAgent(id="worker-1", _current_task=other_task)
        svc.add_agent(agent)

        cancel_uc = CancelTaskUseCase(svc)
        result = await cancel_uc.execute(CancelTaskDTO(task_id="our-task"))
        assert result.was_running is False

    @pytest.mark.asyncio
    async def test_cancel_calls_service(self):
        svc = StubTaskService()
        cancel_uc = CancelTaskUseCase(svc)
        await cancel_uc.execute(CancelTaskDTO(task_id="task-abc"))
        assert "task-abc" in svc.cancel_calls

    @pytest.mark.asyncio
    async def test_to_dict_contains_expected_keys(self):
        svc = StubTaskService()
        cancel_uc = CancelTaskUseCase(svc)
        result = await cancel_uc.execute(CancelTaskDTO(task_id="t"))
        d = result.to_dict()
        assert d["task_id"] == "t"
        assert "cancelled" in d
        assert "was_running" in d

    @pytest.mark.asyncio
    async def test_double_cancel_second_is_false(self):
        """Cancelling an already-cancelled task returns False."""
        svc = StubTaskService()
        task = Task(id="t", prompt="x")
        task.submitted_at = time.time()
        task.expires_at = None
        svc._tasks["t"] = task

        cancel_uc = CancelTaskUseCase(svc)
        r1 = await cancel_uc.execute(CancelTaskDTO(task_id="t"))
        r2 = await cancel_uc.execute(CancelTaskDTO(task_id="t"))
        assert r1.cancelled is True
        assert r2.cancelled is False


# ---------------------------------------------------------------------------
# TaskService protocol structural check
# ---------------------------------------------------------------------------


class TestTaskServiceProtocol:
    def test_stub_satisfies_protocol(self):
        """StubTaskService satisfies TaskService protocol (isinstance check)."""
        svc = StubTaskService()
        assert isinstance(svc, TaskService)

    def test_use_case_accepts_any_task_service(self):
        """SubmitTaskUseCase and CancelTaskUseCase accept any TaskService."""
        svc = StubTaskService()
        assert SubmitTaskUseCase(svc)._service is svc
        assert CancelTaskUseCase(svc)._service is svc
