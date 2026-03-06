"""Tests for Stop hook completion detection (v0.38.0).

The Stop hook writes `.claude/settings.local.json` into the agent worktree
with an HTTP hook pointing to ``POST /agents/{agent_id}/task-complete``.
When the hook fires, the web server publishes a RESULT on the bus and the
polling fallback (``_wait_for_completion``) is cancelled.

References:
- Claude Code Hooks Reference https://code.claude.com/docs/en/hooks (2025)
- DESIGN.md §10.12 (v0.38.0)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.web.app import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_bus() -> Bus:
    return Bus()


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


# ---------------------------------------------------------------------------
# Tests for _write_stop_hook_settings (unit-level, no tmux)
# ---------------------------------------------------------------------------


def test_write_stop_hook_settings_creates_settings_file(tmp_path: Path) -> None:
    """_write_stop_hook_settings must create .claude/settings.local.json."""
    bus = make_bus()
    tmux = make_tmux_mock()

    agent = ClaudeCodeAgent(
        agent_id="worker-1",
        bus=bus,
        tmux=tmux,
        web_base_url="http://localhost:9000",
    )

    agent._write_stop_hook_settings(tmp_path)

    settings_path = tmp_path / ".claude" / "settings.local.json"
    assert settings_path.exists(), ".claude/settings.local.json must be created"


def test_write_stop_hook_settings_correct_json_structure(tmp_path: Path) -> None:
    """settings.local.json must contain the correct Stop hook HTTP config."""
    bus = make_bus()
    tmux = make_tmux_mock()

    agent = ClaudeCodeAgent(
        agent_id="worker-1",
        bus=bus,
        tmux=tmux,
        web_base_url="http://localhost:9000",
    )

    agent._write_stop_hook_settings(tmp_path)

    settings_path = tmp_path / ".claude" / "settings.local.json"
    data = json.loads(settings_path.read_text())

    # Must have hooks.Stop
    assert "hooks" in data
    assert "Stop" in data["hooks"]

    stop_hooks = data["hooks"]["Stop"]
    assert isinstance(stop_hooks, list)
    assert len(stop_hooks) == 1

    matcher_group = stop_hooks[0]
    assert "hooks" in matcher_group

    hook_handlers = matcher_group["hooks"]
    assert isinstance(hook_handlers, list)
    assert len(hook_handlers) == 1

    handler = hook_handlers[0]
    assert handler["type"] == "http"
    assert "worker-1" in handler["url"]
    assert "task-complete" in handler["url"]


def test_write_stop_hook_settings_url_includes_agent_id(tmp_path: Path) -> None:
    """The hook URL must use the correct agent_id and base URL port."""
    bus = make_bus()
    tmux = make_tmux_mock()

    agent = ClaudeCodeAgent(
        agent_id="my-special-agent",
        bus=bus,
        tmux=tmux,
        web_base_url="http://localhost:8765",
    )

    agent._write_stop_hook_settings(tmp_path)

    settings_path = tmp_path / ".claude" / "settings.local.json"
    data = json.loads(settings_path.read_text())
    handler = data["hooks"]["Stop"][0]["hooks"][0]

    assert handler["url"] == "http://localhost:8765/agents/my-special-agent/task-complete"


def test_write_stop_hook_settings_has_timeout(tmp_path: Path) -> None:
    """The hook handler must include a timeout field."""
    bus = make_bus()
    tmux = make_tmux_mock()

    agent = ClaudeCodeAgent(
        agent_id="worker-1",
        bus=bus,
        tmux=tmux,
        web_base_url="http://localhost:8000",
    )

    agent._write_stop_hook_settings(tmp_path)

    settings_path = tmp_path / ".claude" / "settings.local.json"
    data = json.loads(settings_path.read_text())
    handler = data["hooks"]["Stop"][0]["hooks"][0]

    assert "timeout" in handler
    assert isinstance(handler["timeout"], int)
    assert handler["timeout"] > 0


def test_write_stop_hook_settings_creates_claude_dir(tmp_path: Path) -> None:
    """_write_stop_hook_settings must create .claude/ directory if missing."""
    bus = make_bus()
    tmux = make_tmux_mock()

    # Confirm .claude/ does not pre-exist
    assert not (tmp_path / ".claude").exists()

    agent = ClaudeCodeAgent(
        agent_id="worker-1",
        bus=bus,
        tmux=tmux,
        web_base_url="http://localhost:8000",
    )

    agent._write_stop_hook_settings(tmp_path)

    assert (tmp_path / ".claude").is_dir()


def test_write_stop_hook_settings_skipped_when_no_web_base_url(tmp_path: Path) -> None:
    """When web_base_url is empty, no settings file must be written."""
    bus = make_bus()
    tmux = make_tmux_mock()

    agent = ClaudeCodeAgent(
        agent_id="worker-1",
        bus=bus,
        tmux=tmux,
        web_base_url="",
    )

    agent._write_stop_hook_settings(tmp_path)

    settings_path = tmp_path / ".claude" / "settings.local.json"
    assert not settings_path.exists(), (
        "settings.local.json must NOT be created when web_base_url is empty"
    )


# ---------------------------------------------------------------------------
# Tests for POST /agents/{agent_id}/task-complete endpoint
# ---------------------------------------------------------------------------


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def handle(self, ws) -> None:
        pass


class _MockOrchestrator:
    def __init__(self):
        self._agents = {}
        self._director_pending = []
        self._dispatch_task = None
        self._published_messages = []
        # bus for the endpoint to publish to
        self.bus = Bus()

    def list_agents(self) -> list:
        return list(self._agents.values())

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

    def reconfigure_rate_limiter(self, *, rate: float, burst: int) -> dict:
        return {"enabled": rate > 0, "rate": rate, "burst": burst, "available_tokens": float(burst)}

    def get_workflow_manager(self):
        from tmux_orchestrator.workflow_manager import WorkflowManager
        return WorkflowManager()

    @property
    def _webhook_manager(self):
        from tmux_orchestrator.webhook_manager import WebhookManager
        return WebhookManager()


_API_KEY = "test-stop-hook-key"


@pytest.fixture
def mock_orchestrator():
    return _MockOrchestrator()


@pytest.fixture
def app(mock_orchestrator):
    return create_app(mock_orchestrator, _MockHub(), api_key=_API_KEY)


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        yield c


async def test_task_complete_endpoint_returns_ok(client, mock_orchestrator) -> None:
    """POST /agents/{id}/task-complete should return {status: ok} for a BUSY agent."""
    from tmux_orchestrator.agents.base import AgentStatus

    # Set up a mock agent in BUSY state with a current task
    mock_agent = MagicMock()
    mock_agent.status = AgentStatus.BUSY
    mock_task = MagicMock()
    mock_task.id = "task-123"
    mock_agent._current_task = mock_task
    mock_agent.id = "worker-1"

    # Wire bus to orchestrator
    mock_agent.bus = mock_orchestrator.bus

    # Add handle_output as an async method
    mock_agent.handle_output = AsyncMock()
    mock_orchestrator._agents["worker-1"] = mock_agent

    resp = await client.post(
        "/agents/worker-1/task-complete",
        headers={"X-API-Key": _API_KEY},
        json={"output": "Task done!", "exit_code": 0},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


async def test_task_complete_endpoint_calls_handle_output(client, mock_orchestrator) -> None:
    """POST /agents/{id}/task-complete must call agent.handle_output with provided output."""
    from tmux_orchestrator.agents.base import AgentStatus

    mock_agent = MagicMock()
    mock_agent.status = AgentStatus.BUSY
    mock_task = MagicMock()
    mock_task.id = "task-abc"
    mock_agent._current_task = mock_task
    mock_agent.id = "worker-2"
    mock_agent.bus = mock_orchestrator.bus
    mock_agent.handle_output = AsyncMock()
    mock_orchestrator._agents["worker-2"] = mock_agent

    resp = await client.post(
        "/agents/worker-2/task-complete",
        headers={"X-API-Key": _API_KEY},
        json={"output": "done", "exit_code": 0},
    )
    assert resp.status_code == 200
    mock_agent.handle_output.assert_awaited_once_with("done")


async def test_task_complete_endpoint_returns_404_for_unknown_agent(client) -> None:
    """POST /agents/{id}/task-complete must return 404 for unknown agents."""
    resp = await client.post(
        "/agents/unknown-agent/task-complete",
        headers={"X-API-Key": _API_KEY},
        json={},
    )
    assert resp.status_code == 404


async def test_task_complete_endpoint_returns_409_when_agent_not_busy(
    client, mock_orchestrator
) -> None:
    """POST /agents/{id}/task-complete must return 409 if agent is not BUSY."""
    from tmux_orchestrator.agents.base import AgentStatus

    mock_agent = MagicMock()
    mock_agent.status = AgentStatus.IDLE
    mock_agent._current_task = None
    mock_agent.id = "worker-idle"
    mock_orchestrator._agents["worker-idle"] = mock_agent

    resp = await client.post(
        "/agents/worker-idle/task-complete",
        headers={"X-API-Key": _API_KEY},
        json={},
    )
    assert resp.status_code == 409


async def test_task_complete_endpoint_requires_auth(client, mock_orchestrator) -> None:
    """POST /agents/{id}/task-complete must reject unauthenticated requests."""
    from tmux_orchestrator.agents.base import AgentStatus

    mock_agent = MagicMock()
    mock_agent.status = AgentStatus.BUSY
    mock_agent._current_task = MagicMock()
    mock_agent._current_task.id = "task-xyz"
    mock_agent.id = "worker-auth"
    mock_orchestrator._agents["worker-auth"] = mock_agent

    resp = await client.post(
        "/agents/worker-auth/task-complete",
        json={},
        # No auth header
    )
    assert resp.status_code == 401


async def test_task_complete_endpoint_body_optional(client, mock_orchestrator) -> None:
    """POST /agents/{id}/task-complete should work with empty body."""
    from tmux_orchestrator.agents.base import AgentStatus

    mock_agent = MagicMock()
    mock_agent.status = AgentStatus.BUSY
    mock_task = MagicMock()
    mock_task.id = "task-empty"
    mock_agent._current_task = mock_task
    mock_agent.id = "worker-empty"
    mock_agent.bus = mock_orchestrator.bus
    mock_agent.handle_output = AsyncMock()
    mock_orchestrator._agents["worker-empty"] = mock_agent

    resp = await client.post(
        "/agents/worker-empty/task-complete",
        headers={"X-API-Key": _API_KEY},
        json={},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Integration: start() calls _write_stop_hook_settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_writes_stop_hook_settings(tmp_path: Path) -> None:
    """ClaudeCodeAgent.start() must write .claude/settings.local.json."""
    bus = make_bus()
    tmux = make_tmux_mock()

    wm = MagicMock()
    wm.setup = MagicMock(return_value=tmp_path)

    agent = ClaudeCodeAgent(
        agent_id="hook-agent",
        bus=bus,
        tmux=tmux,
        worktree_manager=wm,
        web_base_url="http://localhost:8000",
    )

    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        await agent.start()

    settings_path = tmp_path / ".claude" / "settings.local.json"
    assert settings_path.exists(), "start() must write .claude/settings.local.json"

    data = json.loads(settings_path.read_text())
    handler = data["hooks"]["Stop"][0]["hooks"][0]
    assert "hook-agent" in handler["url"]

    await agent.stop()
