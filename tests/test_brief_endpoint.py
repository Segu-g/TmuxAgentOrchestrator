"""Tests for POST /agents/{id}/brief — out-of-band context injection endpoint.

Covers:
- 200: brief_id returned, file written, notify_stdin called
- 200: brief_id auto-generated when not provided
- 200: isolate:false agent falls back to cwd
- 200: delivered=False when notify_stdin raises
- 404: agent not found
- 422: content empty
- 422: content exceeds 4096 characters
- 422: content whitespace-only
- Authentication required (401 without key)
- Deterministic brief_id when caller provides one
- Brief file content matches request body

Design reference: DESIGN.md §10.43 (v1.1.7)
References:
- OpenAI Agents SDK "Context Management" (2025):
  https://openai.github.io/openai-agents-python/context/
- LangChain "Context Engineering in Agents" (2025):
  https://docs.langchain.com/oss/python/langchain/context-engineering
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tmux_orchestrator.web.app import create_app

# ---------------------------------------------------------------------------
# Helpers / Mocks
# ---------------------------------------------------------------------------

_API_KEY = "brief-test-key"


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _MockAgent:
    """Minimal Agent mock with worktree_path and notify_stdin."""

    def __init__(self, agent_id: str, worktree_path: Path | None = None) -> None:
        self.id = agent_id
        self.worktree_path = worktree_path
        self._notifications: list[str] = []
        self._notify_error: Exception | None = None

    async def notify_stdin(self, notification: str) -> None:
        if self._notify_error is not None:
            raise self._notify_error
        self._notifications.append(notification)


class _MockOrchestrator:
    """Mock orchestrator that holds a dict of agent_id → _MockAgent."""

    def __init__(self, agents: dict[str, _MockAgent] | None = None) -> None:
        self._agents: dict[str, _MockAgent] = agents or {}
        self._director_pending: list = []

    def get_agent(self, agent_id: str) -> _MockAgent | None:
        return self._agents.get(agent_id)

    def get_agent_dict(self, agent_id: str) -> dict | None:
        a = self._agents.get(agent_id)
        if a is None:
            return None
        return {"id": a.id, "status": "IDLE"}

    def list_agents(self) -> list[dict]:
        return [{"id": a.id, "status": "IDLE"} for a in self._agents.values()]

    def list_tasks(self) -> list:
        return []

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

    def reconfigure_rate_limiter(self, *, rate: float, burst: int) -> dict:
        return {"enabled": rate > 0, "rate": rate, "burst": burst,
                "available_tokens": float(burst)}

    def get_workflow_manager(self):
        from tmux_orchestrator.workflow_manager import WorkflowManager
        return WorkflowManager()

    @property
    def _webhook_manager(self):
        from tmux_orchestrator.webhook_manager import WebhookManager
        return WebhookManager()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_worktree(tmp_path: Path) -> Path:
    """Return a temporary directory simulating an agent worktree."""
    wt = tmp_path / "worktrees" / "worker-1"
    wt.mkdir(parents=True)
    return wt


@pytest.fixture()
def agent_with_worktree(tmp_worktree: Path) -> _MockAgent:
    return _MockAgent("worker-1", worktree_path=tmp_worktree)


@pytest.fixture()
def agent_no_worktree() -> _MockAgent:
    """Agent with isolate:false (no worktree_path)."""
    return _MockAgent("worker-iso-false", worktree_path=None)


@pytest.fixture()
def app_with_agent(agent_with_worktree: _MockAgent):
    orch = _MockOrchestrator({"worker-1": agent_with_worktree})
    return create_app(orch, _MockHub(), api_key=_API_KEY)


@pytest.fixture()
def app_no_agent():
    orch = _MockOrchestrator({})
    return create_app(orch, _MockHub(), api_key=_API_KEY)


@pytest.fixture()
def app_iso_false(agent_no_worktree: _MockAgent):
    orch = _MockOrchestrator({"worker-iso-false": agent_no_worktree})
    return create_app(orch, _MockHub(), api_key=_API_KEY)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return {"X-Api-Key": _API_KEY}


# ---------------------------------------------------------------------------
# Tests: 200 success cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brief_returns_200_and_brief_id(
    app_with_agent, agent_with_worktree: _MockAgent
) -> None:
    """POST /agents/{id}/brief returns 200 with a brief_id."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_agent), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/agents/worker-1/brief",
            json={"content": "New requirement: add pagination."},
            headers=_headers(),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "brief_id" in data
    assert data["delivered"] is True
    # brief_id should be a valid UUID
    uuid.UUID(data["brief_id"])


@pytest.mark.asyncio
async def test_brief_file_written_to_worktree(
    app_with_agent, agent_with_worktree: _MockAgent, tmp_worktree: Path
) -> None:
    """POST /agents/{id}/brief writes __brief__/{id}.txt in the worktree."""
    content = "Priority has changed: focus on security first."
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_agent), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/agents/worker-1/brief",
            json={"content": content},
            headers=_headers(),
        )
    assert resp.status_code == 200
    brief_id = resp.json()["brief_id"]
    brief_file = tmp_worktree / "__brief__" / f"{brief_id}.txt"
    assert brief_file.exists(), f"Expected {brief_file} to exist"
    assert brief_file.read_text(encoding="utf-8") == content


@pytest.mark.asyncio
async def test_brief_notify_stdin_called(
    app_with_agent, agent_with_worktree: _MockAgent
) -> None:
    """POST /agents/{id}/brief calls notify_stdin with __BRIEF__:{brief_id}."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_agent), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/agents/worker-1/brief",
            json={"content": "Update: API changed to v2."},
            headers=_headers(),
        )
    assert resp.status_code == 200
    brief_id = resp.json()["brief_id"]
    assert len(agent_with_worktree._notifications) == 1
    assert agent_with_worktree._notifications[0] == f"__BRIEF__:{brief_id}"


@pytest.mark.asyncio
async def test_brief_caller_provided_brief_id(
    app_with_agent, agent_with_worktree: _MockAgent, tmp_worktree: Path
) -> None:
    """When caller provides brief_id, the same ID is used and returned."""
    my_id = "custom-brief-id-123"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_agent), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/agents/worker-1/brief",
            json={"content": "Use this custom ID.", "brief_id": my_id},
            headers=_headers(),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["brief_id"] == my_id
    brief_file = tmp_worktree / "__brief__" / f"{my_id}.txt"
    assert brief_file.exists()


@pytest.mark.asyncio
async def test_brief_worktree_path_in_response(
    app_with_agent, tmp_worktree: Path
) -> None:
    """Response includes worktree_path pointing to the agent's worktree."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_agent), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/agents/worker-1/brief",
            json={"content": "Check the new specs."},
            headers=_headers(),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "worktree_path" in data
    assert data["worktree_path"] == str(tmp_worktree)


@pytest.mark.asyncio
async def test_brief_notify_stdin_failure_delivered_false(
    app_iso_false, agent_no_worktree: _MockAgent, tmp_path: Path, monkeypatch
) -> None:
    """When notify_stdin raises, delivered=False but file is still written."""
    agent_no_worktree._notify_error = RuntimeError("pane closed")
    # Monkeypatch Path.cwd() to a known tmp_path
    monkeypatch.chdir(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_iso_false), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/agents/worker-iso-false/brief",
            json={"content": "Important: deadline moved to Friday."},
            headers=_headers(),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["delivered"] is False
    # File should still be written
    brief_file = tmp_path / "__brief__" / f"{data['brief_id']}.txt"
    assert brief_file.exists()


@pytest.mark.asyncio
async def test_brief_iso_false_fallback_to_cwd(
    app_iso_false, agent_no_worktree: _MockAgent, tmp_path: Path, monkeypatch
) -> None:
    """Agent with no worktree writes brief to cwd."""
    monkeypatch.chdir(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_iso_false), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/agents/worker-iso-false/brief",
            json={"content": "Fallback to cwd test."},
            headers=_headers(),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["worktree_path"] == str(tmp_path)
    brief_file = tmp_path / "__brief__" / f"{data['brief_id']}.txt"
    assert brief_file.exists()


@pytest.mark.asyncio
async def test_brief_multiple_briefs_accumulate(
    app_with_agent, agent_with_worktree: _MockAgent, tmp_worktree: Path
) -> None:
    """Multiple POST /brief requests create separate files."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_agent), base_url="http://test"
    ) as client:
        resp1 = await client.post(
            "/agents/worker-1/brief",
            json={"content": "First brief."},
            headers=_headers(),
        )
        resp2 = await client.post(
            "/agents/worker-1/brief",
            json={"content": "Second brief."},
            headers=_headers(),
        )
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    id1 = resp1.json()["brief_id"]
    id2 = resp2.json()["brief_id"]
    assert id1 != id2
    assert (tmp_worktree / "__brief__" / f"{id1}.txt").exists()
    assert (tmp_worktree / "__brief__" / f"{id2}.txt").exists()
    assert len(agent_with_worktree._notifications) == 2


# ---------------------------------------------------------------------------
# Tests: 404 not found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brief_unknown_agent_returns_404(app_no_agent) -> None:
    """POST /agents/{unknown_id}/brief returns 404."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_no_agent), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/agents/does-not-exist/brief",
            json={"content": "Should not arrive."},
            headers=_headers(),
        )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Tests: 422 validation errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brief_empty_content_returns_422(app_with_agent) -> None:
    """POST /agents/{id}/brief with empty content returns 422."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_agent), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/agents/worker-1/brief",
            json={"content": ""},
            headers=_headers(),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_brief_whitespace_content_returns_422(app_with_agent) -> None:
    """POST /agents/{id}/brief with whitespace-only content returns 422."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_agent), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/agents/worker-1/brief",
            json={"content": "   \n\t  "},
            headers=_headers(),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_brief_content_too_long_returns_422(app_with_agent) -> None:
    """POST /agents/{id}/brief with content > 4096 chars returns 422."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_agent), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/agents/worker-1/brief",
            json={"content": "x" * 4097},
            headers=_headers(),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_brief_content_exactly_4096_allowed(
    app_with_agent, agent_with_worktree: _MockAgent
) -> None:
    """POST /agents/{id}/brief with exactly 4096 chars succeeds."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_agent), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/agents/worker-1/brief",
            json={"content": "a" * 4096},
            headers=_headers(),
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_brief_missing_content_returns_422(app_with_agent) -> None:
    """POST /agents/{id}/brief without content field returns 422."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_agent), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/agents/worker-1/brief",
            json={},
            headers=_headers(),
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: authentication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brief_requires_auth(app_with_agent) -> None:
    """POST /agents/{id}/brief without API key returns 401 or 403."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_agent), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/agents/worker-1/brief",
            json={"content": "No auth."},
        )
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Tests: schema validation
# ---------------------------------------------------------------------------


def test_agent_brief_request_valid() -> None:
    """AgentBriefRequest accepts valid content."""
    from tmux_orchestrator.web.schemas import AgentBriefRequest

    req = AgentBriefRequest(content="hello world")
    assert req.content == "hello world"
    assert req.brief_id is None


def test_agent_brief_request_with_brief_id() -> None:
    """AgentBriefRequest stores caller-provided brief_id."""
    from tmux_orchestrator.web.schemas import AgentBriefRequest

    req = AgentBriefRequest(content="hello", brief_id="my-id")
    assert req.brief_id == "my-id"


def test_agent_brief_request_empty_content_raises() -> None:
    """AgentBriefRequest rejects empty content."""
    import pytest
    from pydantic import ValidationError

    from tmux_orchestrator.web.schemas import AgentBriefRequest

    with pytest.raises(ValidationError):
        AgentBriefRequest(content="")


def test_agent_brief_request_whitespace_content_raises() -> None:
    """AgentBriefRequest rejects whitespace-only content."""
    import pytest
    from pydantic import ValidationError

    from tmux_orchestrator.web.schemas import AgentBriefRequest

    with pytest.raises(ValidationError):
        AgentBriefRequest(content="   ")


def test_agent_brief_request_too_long_raises() -> None:
    """AgentBriefRequest rejects content > 4096 chars."""
    import pytest
    from pydantic import ValidationError

    from tmux_orchestrator.web.schemas import AgentBriefRequest

    with pytest.raises(ValidationError):
        AgentBriefRequest(content="x" * 4097)


def test_agent_brief_request_exactly_4096_ok() -> None:
    """AgentBriefRequest accepts content of exactly 4096 chars."""
    from tmux_orchestrator.web.schemas import AgentBriefRequest

    req = AgentBriefRequest(content="a" * 4096)
    assert len(req.content) == 4096
