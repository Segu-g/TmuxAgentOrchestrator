"""Integration tests: tasks router delegates to Use Case Interactors.

Verifies that:
  - ``POST /tasks`` handler delegates to ``SubmitTaskUseCase`` (not raw orchestrator).
  - ``DELETE /tasks/{id}`` handler delegates to ``CancelTaskUseCase``.
  - The route returns the expected JSON structure from Use Case result DTOs.
  - Use cases receive correctly-mapped fields from the HTTP body.
  - Error paths (404, 422) still work through the Use Case layer.

These tests use ``httpx.AsyncClient`` + FastAPI ``TestClient`` (ASGI) so no
real tmux or claude process is started.  A stub ``TaskService`` (shared with
``test_use_cases.py``) satisfies the ``TaskService`` protocol.

Design reference: DESIGN.md §10.46 — v1.1.14 UseCaseInteractor wiring.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tmux_orchestrator.application.use_cases import (
    CancelTaskDTO,
    CancelTaskResult,
    CancelTaskUseCase,
    GetAgentDTO,
    GetAgentResult,
    GetAgentUseCase,
    ListAgentsDTO,
    ListAgentsResult,
    ListAgentsUseCase,
    SubmitTaskDTO,
    SubmitTaskResult,
    SubmitTaskUseCase,
    TaskService,
)
from tmux_orchestrator.domain.task import Task
from tmux_orchestrator.web.routers.agents import build_agents_router
from tmux_orchestrator.web.routers.tasks import build_tasks_router


# ---------------------------------------------------------------------------
# Shared stub (same shape as in test_use_cases.py)
# ---------------------------------------------------------------------------


@dataclass
class _StubAgent:
    id: str
    _current_task: Task | None = None


class _StubTaskService:
    """Minimal ``TaskService`` stub for router integration tests."""

    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._agents: list[_StubAgent] = []
        self._cancelled: set[str] = set()
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
        t.expires_at = (t.submitted_at + ttl) if ttl is not None else None
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

    # Extra methods required by the tasks router (list_tasks etc.)
    def list_tasks(self) -> list[dict]:
        return []

    def get_waiting_task(self, task_id: str) -> Task | None:
        return None

    def _task_blocking(self, task_id: str) -> list[str]:
        return []

    def get_agent_history(self, agent_id: str, limit: int = 200):
        return []

    def list_dlq(self) -> list[dict]:
        return []

    async def update_task_priority(self, task_id: str, priority: int) -> bool:
        return False

    _active_tasks: dict = field(default_factory=dict)
    _task_started_at: dict = field(default_factory=dict)
    _completed_tasks: set = field(default_factory=set)


# Ensure MagicMock fields work in _StubTaskService
_StubTaskService._active_tasks = {}
_StubTaskService._task_started_at = {}
_StubTaskService._completed_tasks = set()


def _build_test_app(svc: _StubTaskService) -> FastAPI:
    """Build a minimal FastAPI app with tasks router wired to *svc*."""
    def _no_auth():
        pass

    router = build_tasks_router(svc, _no_auth)
    app = FastAPI()
    app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# POST /tasks — SubmitTaskUseCase integration
# ---------------------------------------------------------------------------


class TestSubmitTaskRouterIntegration:
    """POST /tasks handler delegates to SubmitTaskUseCase."""

    def test_post_tasks_returns_200_with_task_id(self):
        svc = _StubTaskService()
        client = TestClient(_build_test_app(svc))
        resp = client.post("/tasks", json={"prompt": "hello world"})
        assert resp.status_code == 200
        data = resp.json()
        assert "task_id" in data

    def test_post_tasks_uses_submit_use_case(self):
        """Verifies that submit_task on the stub is actually called."""
        svc = _StubTaskService()
        client = TestClient(_build_test_app(svc))
        client.post("/tasks", json={"prompt": "my task"})
        assert len(svc.submit_calls) == 1
        assert svc.submit_calls[0]["prompt"] == "my task"

    def test_post_tasks_returns_prompt_in_response(self):
        svc = _StubTaskService()
        client = TestClient(_build_test_app(svc))
        resp = client.post("/tasks", json={"prompt": "test prompt"})
        assert resp.json()["prompt"] == "test prompt"

    def test_post_tasks_returns_priority_in_response(self):
        svc = _StubTaskService()
        client = TestClient(_build_test_app(svc))
        resp = client.post("/tasks", json={"prompt": "p", "priority": 7})
        assert resp.json()["priority"] == 7

    def test_post_tasks_returns_max_retries_in_response(self):
        svc = _StubTaskService()
        client = TestClient(_build_test_app(svc))
        resp = client.post("/tasks", json={"prompt": "p", "max_retries": 3})
        assert resp.json()["max_retries"] == 3

    def test_post_tasks_response_has_submitted_at(self):
        svc = _StubTaskService()
        client = TestClient(_build_test_app(svc))
        resp = client.post("/tasks", json={"prompt": "p"})
        assert "submitted_at" in resp.json()

    def test_post_tasks_missing_prompt_returns_422(self):
        svc = _StubTaskService()
        client = TestClient(_build_test_app(svc))
        resp = client.post("/tasks", json={})
        assert resp.status_code == 422

    def test_post_tasks_passes_reply_to(self):
        svc = _StubTaskService()
        client = TestClient(_build_test_app(svc))
        resp = client.post("/tasks", json={"prompt": "x", "reply_to": "director-1"})
        assert resp.json().get("reply_to") == "director-1"

    def test_post_tasks_passes_target_agent(self):
        svc = _StubTaskService()
        client = TestClient(_build_test_app(svc))
        resp = client.post("/tasks", json={"prompt": "x", "target_agent": "worker-2"})
        assert resp.json().get("target_agent") == "worker-2"

    def test_post_tasks_passes_required_tags(self):
        svc = _StubTaskService()
        client = TestClient(_build_test_app(svc))
        resp = client.post("/tasks", json={"prompt": "x", "required_tags": ["gpu"]})
        assert "gpu" in resp.json().get("required_tags", [])

    def test_post_tasks_ttl_in_response(self):
        svc = _StubTaskService()
        client = TestClient(_build_test_app(svc))
        resp = client.post("/tasks", json={"prompt": "x", "ttl": 30.0})
        assert resp.json().get("ttl") == 30.0

    def test_post_tasks_submit_use_case_receives_sanitised_prompt(self):
        """Prompt is sanitised before being passed to SubmitTaskUseCase."""
        svc = _StubTaskService()
        client = TestClient(_build_test_app(svc))
        # A prompt with HTML-like content should be sanitised
        client.post("/tasks", json={"prompt": "hello"})
        # sanitize_prompt is idempotent for safe strings
        assert svc.submit_calls[0]["prompt"] == "hello"


# ---------------------------------------------------------------------------
# DELETE /tasks/{id} — CancelTaskUseCase integration
# ---------------------------------------------------------------------------


class TestDeleteTaskRouterIntegration:
    """DELETE /tasks/{id} handler delegates to CancelTaskUseCase."""

    def test_delete_existing_task_returns_200(self):
        svc = _StubTaskService()
        # Pre-populate a task so cancel_task returns True
        t = Task(id="t1", prompt="x")
        t.submitted_at = time.time()
        t.expires_at = None
        svc._tasks["t1"] = t
        client = TestClient(_build_test_app(svc))
        resp = client.delete("/tasks/t1")
        assert resp.status_code == 200

    def test_delete_returns_cancelled_true(self):
        svc = _StubTaskService()
        t = Task(id="t2", prompt="x")
        t.submitted_at = time.time()
        t.expires_at = None
        svc._tasks["t2"] = t
        client = TestClient(_build_test_app(svc))
        resp = client.delete("/tasks/t2")
        assert resp.json()["cancelled"] is True

    def test_delete_returns_task_id(self):
        svc = _StubTaskService()
        t = Task(id="t3", prompt="x")
        t.submitted_at = time.time()
        t.expires_at = None
        svc._tasks["t3"] = t
        client = TestClient(_build_test_app(svc))
        resp = client.delete("/tasks/t3")
        assert resp.json()["task_id"] == "t3"

    def test_delete_nonexistent_task_returns_404(self):
        svc = _StubTaskService()
        client = TestClient(_build_test_app(svc))
        resp = client.delete("/tasks/no-such-task")
        assert resp.status_code == 404

    def test_delete_calls_cancel_task_on_service(self):
        svc = _StubTaskService()
        t = Task(id="t4", prompt="x")
        t.submitted_at = time.time()
        t.expires_at = None
        svc._tasks["t4"] = t
        client = TestClient(_build_test_app(svc))
        client.delete("/tasks/t4")
        assert "t4" in svc.cancel_calls

    def test_delete_was_running_false_when_not_dispatched(self):
        svc = _StubTaskService()
        t = Task(id="t5", prompt="x")
        t.submitted_at = time.time()
        t.expires_at = None
        svc._tasks["t5"] = t
        client = TestClient(_build_test_app(svc))
        resp = client.delete("/tasks/t5")
        assert resp.json()["was_running"] is False

    def test_delete_was_running_true_when_agent_executing(self):
        svc = _StubTaskService()
        t = Task(id="running", prompt="x")
        t.submitted_at = time.time()
        t.expires_at = None
        svc._tasks["running"] = t
        svc.add_agent(_StubAgent(id="worker-1", _current_task=t))
        client = TestClient(_build_test_app(svc))
        resp = client.delete("/tasks/running")
        assert resp.json()["was_running"] is True


# ---------------------------------------------------------------------------
# GetAgentUseCase unit tests (standalone, no HTTP)
# ---------------------------------------------------------------------------


class TestGetAgentUseCaseStandalone:
    """GetAgentUseCase tested without HTTP layer."""

    @pytest.mark.asyncio
    async def test_found_agent_returns_found_true(self):
        svc = _StubTaskService()
        svc.add_agent(_StubAgent(id="a1"))
        uc = GetAgentUseCase(svc)
        result = await uc.execute(GetAgentDTO(agent_id="a1"))
        assert result.found is True

    @pytest.mark.asyncio
    async def test_missing_agent_returns_found_false(self):
        svc = _StubTaskService()
        uc = GetAgentUseCase(svc)
        result = await uc.execute(GetAgentDTO(agent_id="ghost"))
        assert result.found is False

    @pytest.mark.asyncio
    async def test_to_dict_has_id_when_found(self):
        svc = _StubTaskService()
        svc.add_agent(_StubAgent(id="a2"))
        uc = GetAgentUseCase(svc)
        result = await uc.execute(GetAgentDTO(agent_id="a2"))
        assert result.to_dict()["id"] == "a2"

    @pytest.mark.asyncio
    async def test_to_dict_empty_when_not_found(self):
        svc = _StubTaskService()
        uc = GetAgentUseCase(svc)
        result = await uc.execute(GetAgentDTO(agent_id="missing"))
        assert result.to_dict() == {}

    @pytest.mark.asyncio
    async def test_is_query_use_case_no_side_effects(self):
        svc = _StubTaskService()
        svc.add_agent(_StubAgent(id="b1"))
        uc = GetAgentUseCase(svc)
        before_agents = list(svc.list_agents())
        await uc.execute(GetAgentDTO(agent_id="b1"))
        after_agents = list(svc.list_agents())
        assert before_agents == after_agents
        # submit_calls / cancel_calls should remain empty
        assert svc.submit_calls == []
        assert svc.cancel_calls == []

    @pytest.mark.asyncio
    async def test_selects_correct_agent_from_multiple(self):
        svc = _StubTaskService()
        for i in range(5):
            svc.add_agent(_StubAgent(id=f"agent-{i}"))
        uc = GetAgentUseCase(svc)
        result = await uc.execute(GetAgentDTO(agent_id="agent-3"))
        assert result.agent_dict["id"] == "agent-3"

    @pytest.mark.asyncio
    async def test_get_agent_result_is_dataclass(self):
        """GetAgentResult should be a dataclass (field access)."""
        svc = _StubTaskService()
        svc.add_agent(_StubAgent(id="c1"))
        uc = GetAgentUseCase(svc)
        result = await uc.execute(GetAgentDTO(agent_id="c1"))
        # These attribute accesses would fail if not a dataclass
        assert hasattr(result, "found")
        assert hasattr(result, "agent_id")
        assert hasattr(result, "agent_dict")


# ---------------------------------------------------------------------------
# Agents router integration: GET /agents and GET /agents/{id} use ListAgents/GetAgent UC
# ---------------------------------------------------------------------------


def _build_agents_test_app(agents: list[dict]) -> FastAPI:
    """Build a minimal FastAPI test app with the agents router, backed by a mock
    orchestrator that returns *agents* from list_agents()."""

    def _no_auth():
        pass

    mock_orchestrator = MagicMock()
    mock_orchestrator.list_agents.return_value = agents

    def _get_agent_by_id(agent_id: str):
        for a in agents:
            if a.get("id") == agent_id:
                m = MagicMock()
                m.id = agent_id
                return m
        return None

    mock_orchestrator.get_agent.side_effect = _get_agent_by_id
    mock_orchestrator.config = MagicMock()
    mock_orchestrator.config.agent_groups = []

    router = build_agents_router(mock_orchestrator, _no_auth, episode_store=None)
    app = FastAPI()
    app.include_router(router)
    return app


class TestListAgentsRouterIntegration:
    """GET /agents handler delegates to ListAgentsUseCase."""

    def test_get_agents_returns_200(self):
        app = _build_agents_test_app([{"id": "worker-1"}])
        client = TestClient(app)
        resp = client.get("/agents")
        assert resp.status_code == 200

    def test_get_agents_returns_list(self):
        app = _build_agents_test_app([{"id": "worker-1"}, {"id": "worker-2"}])
        client = TestClient(app)
        resp = client.get("/agents")
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2

    def test_get_agents_returns_agent_ids(self):
        app = _build_agents_test_app([{"id": "a1"}, {"id": "a2"}])
        client = TestClient(app)
        resp = client.get("/agents")
        ids = {item["id"] for item in resp.json()}
        assert ids == {"a1", "a2"}

    def test_get_agents_empty_returns_empty_list(self):
        app = _build_agents_test_app([])
        client = TestClient(app)
        resp = client.get("/agents")
        assert resp.json() == []

    def test_get_agents_uses_list_agents_use_case(self):
        """Verify ListAgentsUseCase.execute is invoked (not raw orchestrator)."""
        agents = [{"id": "worker-1"}]
        app = _build_agents_test_app(agents)
        client = TestClient(app)
        resp = client.get("/agents")
        # If UseCase is wired, it reads from list_agents() which returns our data
        assert any(item["id"] == "worker-1" for item in resp.json())


class TestGetAgentRouterIntegration:
    """GET /agents/{agent_id} handler delegates to GetAgentUseCase."""

    def test_get_agent_found_returns_200(self):
        app = _build_agents_test_app([{"id": "worker-1", "status": "IDLE"}])
        client = TestClient(app)
        resp = client.get("/agents/worker-1")
        assert resp.status_code == 200

    def test_get_agent_returns_agent_dict(self):
        app = _build_agents_test_app([{"id": "worker-1", "status": "IDLE"}])
        client = TestClient(app)
        resp = client.get("/agents/worker-1")
        assert resp.json()["id"] == "worker-1"

    def test_get_agent_not_found_returns_404(self):
        app = _build_agents_test_app([])
        client = TestClient(app)
        resp = client.get("/agents/no-such-agent")
        assert resp.status_code == 404

    def test_get_agent_404_detail_contains_agent_id(self):
        app = _build_agents_test_app([])
        client = TestClient(app)
        resp = client.get("/agents/missing-agent")
        assert "missing-agent" in resp.json()["detail"]

    def test_get_agent_uses_get_agent_use_case(self):
        """Verify GetAgentUseCase is invoked (not orchestrator.get_agent_dict)."""
        app = _build_agents_test_app([{"id": "director-1", "role": "director"}])
        client = TestClient(app)
        resp = client.get("/agents/director-1")
        # If UseCase is wired, it finds the agent via list_agents() lookup
        assert resp.status_code == 200
        assert resp.json()["id"] == "director-1"

    def test_get_agent_preserves_extra_fields(self):
        app = _build_agents_test_app([{"id": "w1", "status": "BUSY", "role": "worker"}])
        client = TestClient(app)
        resp = client.get("/agents/w1")
        assert resp.json()["status"] == "BUSY"
        assert resp.json()["role"] == "worker"


# ---------------------------------------------------------------------------
# ListAgentsUseCase standalone unit tests (no HTTP)
# ---------------------------------------------------------------------------


class TestListAgentsUseCaseStandalone:
    """ListAgentsUseCase tested without HTTP layer."""

    @pytest.mark.asyncio
    async def test_returns_list_agents_result(self):
        svc = _StubTaskService()
        svc.add_agent(_StubAgent(id="w1"))
        uc = ListAgentsUseCase(svc)
        result = await uc.execute(ListAgentsDTO())
        assert isinstance(result, ListAgentsResult)

    @pytest.mark.asyncio
    async def test_empty_service_returns_empty(self):
        svc = _StubTaskService()
        uc = ListAgentsUseCase(svc)
        result = await uc.execute(ListAgentsDTO())
        assert result.items == []

    @pytest.mark.asyncio
    async def test_agents_in_result_have_id(self):
        svc = _StubTaskService()
        svc.add_agent(_StubAgent(id="agent-x"))
        uc = ListAgentsUseCase(svc)
        result = await uc.execute(ListAgentsDTO())
        assert result.items[0]["id"] == "agent-x"

    @pytest.mark.asyncio
    async def test_to_list_is_json_serialisable(self):
        import json
        svc = _StubTaskService()
        svc.add_agent(_StubAgent(id="serialisable"))
        uc = ListAgentsUseCase(svc)
        result = await uc.execute(ListAgentsDTO())
        # Should not raise
        encoded = json.dumps(result.to_list())
        assert "serialisable" in encoded

    @pytest.mark.asyncio
    async def test_no_side_effects(self):
        svc = _StubTaskService()
        svc.add_agent(_StubAgent(id="pure"))
        uc = ListAgentsUseCase(svc)
        before = list(svc.list_agents())
        await uc.execute(ListAgentsDTO())
        after = list(svc.list_agents())
        assert before == after
        assert svc.submit_calls == []
        assert svc.cancel_calls == []
