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
from tmux_orchestrator.agents.completion import (
    ExplicitSignalStrategy,
    StopHookStrategy,
    make_completion_strategy,
)
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
# Tests for StopHookStrategy (unit-level — tests the strategy directly)
# ---------------------------------------------------------------------------


def test_stop_hook_strategy_creates_settings_file(tmp_path: Path) -> None:
    """StopHookStrategy.on_start must create .claude/settings.local.json."""
    strategy = StopHookStrategy("worker-1", "http://localhost:9000")
    strategy.on_start(tmp_path)
    assert (tmp_path / ".claude" / "settings.local.json").exists()


def test_stop_hook_strategy_correct_json_structure(tmp_path: Path) -> None:
    """settings.local.json must contain the correct Stop hook HTTP config."""
    strategy = StopHookStrategy("worker-1", "http://localhost:9000")
    strategy.on_start(tmp_path)

    data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    assert "hooks" in data
    assert "Stop" in data["hooks"]

    stop_hooks = data["hooks"]["Stop"]
    assert isinstance(stop_hooks, list) and len(stop_hooks) == 1

    hook_handlers = stop_hooks[0]["hooks"]
    assert isinstance(hook_handlers, list) and len(hook_handlers) == 1

    handler = hook_handlers[0]
    assert handler["type"] == "http"
    assert "worker-1" in handler["url"]
    assert "task-complete" in handler["url"]


def test_stop_hook_strategy_url_includes_agent_id(tmp_path: Path) -> None:
    """The hook URL must embed the correct agent_id and base URL port."""
    strategy = StopHookStrategy("my-special-agent", "http://localhost:8765")
    strategy.on_start(tmp_path)

    data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    handler = data["hooks"]["Stop"][0]["hooks"][0]
    assert handler["url"] == "http://localhost:8765/agents/my-special-agent/task-complete"


def test_stop_hook_strategy_has_timeout(tmp_path: Path) -> None:
    """The hook handler must include a positive integer timeout."""
    strategy = StopHookStrategy("worker-1", "http://localhost:8000")
    strategy.on_start(tmp_path)

    data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    handler = data["hooks"]["Stop"][0]["hooks"][0]
    assert "timeout" in handler
    assert isinstance(handler["timeout"], int) and handler["timeout"] > 0


def test_stop_hook_strategy_creates_claude_dir(tmp_path: Path) -> None:
    """on_start must create .claude/ directory if missing."""
    assert not (tmp_path / ".claude").exists()
    StopHookStrategy("worker-1", "http://localhost:8000").on_start(tmp_path)
    assert (tmp_path / ".claude").is_dir()


def test_stop_hook_strategy_skipped_when_no_web_base_url(tmp_path: Path) -> None:
    """When web_base_url is empty, no settings file must be written."""
    StopHookStrategy("worker-1", "").on_start(tmp_path)
    assert not (tmp_path / ".claude" / "settings.local.json").exists()


def test_stop_hook_strategy_has_api_key_header(tmp_path: Path) -> None:
    """HTTP hook must include X-Api-Key header with $TMUX_ORCHESTRATOR_API_KEY."""
    StopHookStrategy("worker-1", "http://localhost:8000").on_start(tmp_path)
    data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    handler = data["hooks"]["Stop"][0]["hooks"][0]
    assert handler["headers"].get("X-Api-Key") == "$TMUX_ORCHESTRATOR_API_KEY"


def test_stop_hook_strategy_has_allowed_env_vars(tmp_path: Path) -> None:
    """HTTP hook must declare allowedEnvVars so Claude Code expands $VAR in headers."""
    StopHookStrategy("worker-1", "http://localhost:8000").on_start(tmp_path)
    data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    handler = data["hooks"]["Stop"][0]["hooks"][0]
    assert "TMUX_ORCHESTRATOR_API_KEY" in handler.get("allowedEnvVars", [])


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
async def test_start_does_not_write_stop_hook_settings(tmp_path: Path) -> None:
    """ClaudeCodeAgent.start() must NOT write .claude/settings.local.json.

    All agents (Worker and Director) now use ExplicitSignalStrategy — they signal
    task completion via /task-complete, not via the Stop hook.  The settings file
    must not be created so that no stale hooks interfere with completion detection.
    """
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
    assert not settings_path.exists(), (
        "start() must NOT write .claude/settings.local.json — "
        "ExplicitSignalStrategy does not use the Stop hook"
    )

    await agent.stop()


@pytest.mark.asyncio
async def test_stop_hook_settings_absent_before_and_after_stop(tmp_path: Path) -> None:
    """No Stop hook settings file should exist before or after stop().

    Since ExplicitSignalStrategy never writes .claude/settings.local.json,
    neither start() nor stop() should create or remove it.
    """
    bus = make_bus()
    tmux = make_tmux_mock()

    wm = MagicMock()
    wm.setup = MagicMock(return_value=tmp_path)

    agent = ClaudeCodeAgent(
        agent_id="cleanup-agent",
        bus=bus,
        tmux=tmux,
        worktree_manager=wm,
        web_base_url="http://localhost:8000",
    )

    settings_path = tmp_path / ".claude" / "settings.local.json"

    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        await agent.start()

    assert not settings_path.exists(), "settings file must not exist after start()"

    await agent.stop()

    assert not settings_path.exists(), "settings file must not exist after stop()"


# ---------------------------------------------------------------------------
# stop_hook_active and last_assistant_message handling
# ---------------------------------------------------------------------------


async def test_task_complete_skips_when_stop_hook_active(
    client, mock_orchestrator
) -> None:
    """stop_hook_active=true means Claude is mid-continuation — must not mark done."""
    from tmux_orchestrator.agents.base import AgentStatus

    mock_agent = MagicMock()
    mock_agent.status = AgentStatus.BUSY
    mock_agent._current_task = MagicMock(id="task-x")
    mock_agent.id = "worker-active"
    mock_agent.bus = mock_orchestrator.bus
    mock_agent.handle_output = AsyncMock()
    mock_orchestrator._agents["worker-active"] = mock_agent

    resp = await client.post(
        "/agents/worker-active/task-complete",
        headers={"X-API-Key": _API_KEY},
        json={"stop_hook_active": True, "last_assistant_message": "still going..."},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "skipped"
    mock_agent.handle_output.assert_not_awaited()


async def test_task_complete_uses_last_assistant_message(
    client, mock_orchestrator
) -> None:
    """last_assistant_message must be preferred over 'output' field."""
    from tmux_orchestrator.agents.base import AgentStatus

    mock_agent = MagicMock()
    mock_agent.status = AgentStatus.BUSY
    mock_agent._current_task = MagicMock(id="task-y")
    mock_agent.id = "worker-msg"
    mock_agent.bus = mock_orchestrator.bus
    mock_agent.handle_output = AsyncMock()
    mock_orchestrator._agents["worker-msg"] = mock_agent

    resp = await client.post(
        "/agents/worker-msg/task-complete",
        headers={"X-API-Key": _API_KEY},
        json={
            "stop_hook_active": False,
            "last_assistant_message": "Here is the result.",
            "output": "should be ignored",
        },
    )
    assert resp.status_code == 200
    mock_agent.handle_output.assert_awaited_once_with("Here is the result.")


async def test_task_complete_falls_back_to_output_field(
    client, mock_orchestrator
) -> None:
    """When last_assistant_message is absent, 'output' field must be used."""
    from tmux_orchestrator.agents.base import AgentStatus

    mock_agent = MagicMock()
    mock_agent.status = AgentStatus.BUSY
    mock_agent._current_task = MagicMock(id="task-z")
    mock_agent.id = "worker-fallback"
    mock_agent.bus = mock_orchestrator.bus
    mock_agent.handle_output = AsyncMock()
    mock_orchestrator._agents["worker-fallback"] = mock_agent

    resp = await client.post(
        "/agents/worker-fallback/task-complete",
        headers={"X-API-Key": _API_KEY},
        json={"output": "fallback output"},
    )
    assert resp.status_code == 200
    mock_agent.handle_output.assert_awaited_once_with("fallback output")


async def test_task_complete_rejects_stale_task_id(client, mock_orchestrator) -> None:
    """Stop hook with a mismatched task_id must be rejected (stale hook from previous task)."""
    from tmux_orchestrator.agents.base import AgentStatus

    mock_agent = MagicMock()
    mock_agent.status = AgentStatus.BUSY
    mock_agent._current_task = MagicMock(id="current-task-id")
    mock_agent.id = "worker-stale"
    mock_agent.bus = mock_orchestrator.bus
    mock_agent.handle_output = AsyncMock()
    mock_orchestrator._agents["worker-stale"] = mock_agent

    resp = await client.post(
        "/agents/worker-stale/task-complete?task_id=old-task-id",
        headers={"X-API-Key": _API_KEY},
        json={"output": "done"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "skipped"
    assert data["reason"] == "task_id_mismatch"
    mock_agent.handle_output.assert_not_awaited()


async def test_task_complete_accepts_matching_task_id(client, mock_orchestrator) -> None:
    """Stop hook with the correct task_id must complete the task normally."""
    from tmux_orchestrator.agents.base import AgentStatus

    mock_agent = MagicMock()
    mock_agent.status = AgentStatus.BUSY
    mock_agent._current_task = MagicMock(id="task-xyz")
    mock_agent.id = "worker-match"
    mock_agent.bus = mock_orchestrator.bus
    mock_agent.handle_output = AsyncMock()
    mock_orchestrator._agents["worker-match"] = mock_agent

    resp = await client.post(
        "/agents/worker-match/task-complete?task_id=task-xyz",
        headers={"X-API-Key": _API_KEY},
        json={"output": "done"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    mock_agent.handle_output.assert_awaited_once()


# ---------------------------------------------------------------------------
# Director agents: stop hook skipped, pane polling skipped
# ---------------------------------------------------------------------------


def test_make_completion_strategy_returns_explicit_signal_for_all_roles(tmp_path: Path) -> None:
    """make_completion_strategy() returns ExplicitSignalStrategy for all roles.

    All agents — Workers and Directors alike — now signal task completion
    explicitly via /task-complete.  The Stop hook is no longer used as a
    completion trigger (it fires after every response turn, not on actual
    task completion).  No .claude/settings.local.json must be written.
    """
    from tmux_orchestrator.config import AgentRole

    for role in (AgentRole.DIRECTOR, AgentRole.WORKER):
        strategy = make_completion_strategy(role, "agent-x", "http://localhost:9000")
        assert isinstance(strategy, ExplicitSignalStrategy), (
            f"Expected ExplicitSignalStrategy for role={role}, got {type(strategy)}"
        )

        role_tmp = tmp_path / role.value
        role_tmp.mkdir()
        strategy.on_start(role_tmp)
        assert not (role_tmp / ".claude" / "settings.local.json").exists(), (
            f"ExplicitSignalStrategy must NOT create stop hook settings for role={role}"
        )


@pytest.mark.asyncio
async def test_wait_for_completion_director_skips_pane_polling() -> None:
    """ExplicitSignalStrategy must ignore settled pane output and never auto-complete.

    Only an explicit _current_task = None (via POST /task-complete) can end a
    director task.  A pane showing '❯' between director responses must not trigger
    completion.
    """
    strategy = ExplicitSignalStrategy()

    tmux = make_tmux_mock()
    tmux.capture_pane = MagicMock(return_value="❯ ")

    # Build a minimal agent-like object satisfying _AgentLike
    class FakeAgent:
        id = "agent-director"
        pane = MagicMock()
        _tmux = tmux
        _current_task: "Task | None" = None

        async def handle_output(self, text: str) -> None:
            pass

    from tmux_orchestrator.agents.base import Task

    agent = FakeAgent()
    task = Task(id="t-dir", prompt="orchestrate something")
    agent._current_task = task

    # After a short delay, simulate explicit completion
    async def explicit_complete():
        await asyncio.sleep(0.3)
        agent._current_task = None

    asyncio.create_task(explicit_complete())

    import time
    t0 = time.monotonic()
    await asyncio.wait_for(strategy.wait(agent, task), timeout=3.0)
    elapsed = time.monotonic() - t0

    # Must have waited for explicit signal (>= 0.3s), not short-circuited by pane poll
    assert elapsed >= 0.25, (
        f"Director completed too early ({elapsed:.2f}s) — pane polling must be disabled"
    )
    tmux.capture_pane.assert_not_called()


# ---------------------------------------------------------------------------
# _wait_for_completion: early return when Stop hook clears _current_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_completion_returns_if_task_cleared() -> None:
    """StopHookStrategy.wait must return immediately if Stop hook cleared _current_task."""
    strategy = StopHookStrategy("worker-wfc", "http://localhost:8000")

    tmux = make_tmux_mock()
    tmux.capture_pane = MagicMock(return_value="❯ ")

    class FakeAgent:
        id = "worker-wfc"
        pane = MagicMock()
        _tmux = tmux
        _current_task: "Task | None" = None
        handle_output = AsyncMock()

    from tmux_orchestrator.agents.base import Task

    agent = FakeAgent()
    task = Task(id="t-cleared", prompt="do something")
    agent._current_task = task

    # Simulate Stop hook firing: clear _current_task before polling detects done
    async def clear_task_after_delay():
        await asyncio.sleep(0.05)
        agent._current_task = None

    asyncio.create_task(clear_task_after_delay())

    # wait() should return quickly once _current_task is None
    await asyncio.wait_for(strategy.wait(agent, task), timeout=3.0)
    # handle_output must NOT have been called (Stop hook handled it externally)
    agent.handle_output.assert_not_awaited()
