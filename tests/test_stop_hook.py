"""Tests for completion strategies and agent startup/task-complete hooks.

Design (v1.0.10):
- ``NudgingStrategy`` (WORKER): writes Stop hook on each task dispatch so the
  web endpoint can send a nudge when Claude finishes a response without calling
  /task-complete.  ``on_start()`` is a no-op; task completion is explicit only.
- ``ExplicitSignalStrategy`` (DIRECTOR): no hooks at all; pure spin-wait.
- Startup detection (all roles): the agent plugin's ``hooks/session-start.sh``
  calls ``POST /agents/{id}/ready`` via the ``SessionStart`` hook.  The plugin
  is loaded via ``--plugin-dir`` passed to the claude launch command.  The
  endpoint sets ``_startup_ready`` so ``_wait_for_ready()`` can return instead
  of timing out.

References:
- Claude Code Hooks Reference https://code.claude.com/docs/en/hooks (2025)
- DESIGN.md §10.latest (v1.0.10)
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
    NudgingStrategy,
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
# NudgingStrategy unit tests
# ---------------------------------------------------------------------------


def test_nudging_strategy_on_start_is_noop(tmp_path: Path) -> None:
    """NudgingStrategy.on_start() must not write any files (startup is separate)."""
    strategy = NudgingStrategy("worker-1", "http://localhost:9000")
    strategy.on_start(tmp_path)
    assert not (tmp_path / ".claude" / "settings.local.json").exists()


def test_nudging_strategy_on_task_dispatch_writes_stop_hook(tmp_path: Path) -> None:
    """NudgingStrategy.on_task_dispatch() must write the Stop hook settings file."""
    strategy = NudgingStrategy("worker-1", "http://localhost:9000")
    strategy.on_task_dispatch(tmp_path, "task-abc")

    settings_path = tmp_path / ".claude" / "settings.local.json"
    assert settings_path.exists()
    data = json.loads(settings_path.read_text())
    assert "Stop" in data["hooks"]

    handler = data["hooks"]["Stop"][0]["hooks"][0]
    assert handler["type"] == "http"
    assert "worker-1" in handler["url"]
    assert "task-complete" in handler["url"]
    assert "task_id=task-abc" in handler["url"]


def test_nudging_strategy_stop_hook_includes_api_key_header(tmp_path: Path) -> None:
    """Stop hook HTTP handler must include X-Api-Key header expanding the env var."""
    NudgingStrategy("worker-1", "http://localhost:8000").on_task_dispatch(tmp_path, "t-1")
    data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    handler = data["hooks"]["Stop"][0]["hooks"][0]
    assert handler["headers"].get("X-Api-Key") == "$TMUX_ORCHESTRATOR_API_KEY"
    assert "TMUX_ORCHESTRATOR_API_KEY" in handler.get("allowedEnvVars", [])


def test_nudging_strategy_stop_hook_has_timeout(tmp_path: Path) -> None:
    """Stop hook handler must include a positive integer timeout."""
    NudgingStrategy("worker-1", "http://localhost:8000").on_task_dispatch(tmp_path, "t-1")
    data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    handler = data["hooks"]["Stop"][0]["hooks"][0]
    assert isinstance(handler.get("timeout"), int) and handler["timeout"] > 0


def test_nudging_strategy_skipped_when_no_web_base_url(tmp_path: Path) -> None:
    """When web_base_url is empty, on_task_dispatch must not write any file."""
    NudgingStrategy("worker-1", "").on_task_dispatch(tmp_path, "t-1")
    assert not (tmp_path / ".claude" / "settings.local.json").exists()


def test_nudging_strategy_on_stop_removes_settings_file(tmp_path: Path) -> None:
    """NudgingStrategy.on_stop() must remove settings.local.json."""
    strategy = NudgingStrategy("worker-1", "http://localhost:8000")
    strategy.on_task_dispatch(tmp_path, "t-1")
    assert (tmp_path / ".claude" / "settings.local.json").exists()
    strategy.on_stop(tmp_path)
    assert not (tmp_path / ".claude" / "settings.local.json").exists()


def test_explicit_signal_strategy_on_start_is_noop(tmp_path: Path) -> None:
    """ExplicitSignalStrategy.on_start() must not write any files."""
    ExplicitSignalStrategy().on_start(tmp_path)
    assert not (tmp_path / ".claude").exists()


def test_make_completion_strategy_returns_correct_strategy_per_role() -> None:
    """make_completion_strategy() returns NudgingStrategy for WORKER, ExplicitSignalStrategy for DIRECTOR."""
    from tmux_orchestrator.config import AgentRole

    worker_strategy = make_completion_strategy(AgentRole.WORKER, "agent-w", "http://localhost:9000")
    assert isinstance(worker_strategy, NudgingStrategy), (
        f"Expected NudgingStrategy for WORKER, got {type(worker_strategy)}"
    )

    director_strategy = make_completion_strategy(AgentRole.DIRECTOR, "agent-d", "http://localhost:9000")
    assert isinstance(director_strategy, ExplicitSignalStrategy), (
        f"Expected ExplicitSignalStrategy for DIRECTOR, got {type(director_strategy)}"
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

    mock_agent = MagicMock()
    mock_agent.status = AgentStatus.BUSY
    mock_task = MagicMock()
    mock_task.id = "task-123"
    mock_agent._current_task = mock_task
    mock_agent.id = "worker-1"
    mock_agent.bus = mock_orchestrator.bus
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
# POST /agents/{id}/ready endpoint
# ---------------------------------------------------------------------------


async def test_agent_ready_endpoint_sets_startup_event(client, mock_orchestrator) -> None:
    """POST /agents/{id}/ready must set _startup_ready event on the agent."""
    mock_agent = MagicMock()
    mock_agent.id = "agent-starting"
    ready_event = asyncio.Event()
    mock_agent._startup_ready = ready_event
    mock_orchestrator._agents["agent-starting"] = mock_agent

    assert not ready_event.is_set()
    resp = await client.post(
        "/agents/agent-starting/ready",
        headers={"X-API-Key": _API_KEY},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert ready_event.is_set()


async def test_agent_ready_endpoint_returns_404_for_unknown_agent(client) -> None:
    """POST /agents/{id}/ready must return 404 for unknown agent IDs."""
    resp = await client.post(
        "/agents/no-such-agent/ready",
        headers={"X-API-Key": _API_KEY},
    )
    assert resp.status_code == 404


async def test_agent_ready_endpoint_is_idempotent(client, mock_orchestrator) -> None:
    """POST /agents/{id}/ready called twice must not error (asyncio.Event.set is idempotent)."""
    mock_agent = MagicMock()
    mock_agent.id = "agent-idem"
    mock_agent._startup_ready = asyncio.Event()
    mock_orchestrator._agents["agent-idem"] = mock_agent

    resp1 = await client.post("/agents/agent-idem/ready", headers={"X-API-Key": _API_KEY})
    resp2 = await client.post("/agents/agent-idem/ready", headers={"X-API-Key": _API_KEY})
    assert resp1.status_code == 200
    assert resp2.status_code == 200


async def test_agent_ready_endpoint_no_auth_required(client, mock_orchestrator) -> None:
    """POST /agents/{id}/ready accepts unauthenticated requests.

    The endpoint has no auth so that the SessionStart hook can call it
    without needing to pass an API key (the hook fires from the same host).
    """
    mock_agent = MagicMock()
    mock_agent.id = "agent-noauth"
    mock_agent._startup_ready = asyncio.Event()
    mock_orchestrator._agents["agent-noauth"] = mock_agent

    resp = await client.post("/agents/agent-noauth/ready")
    assert resp.status_code == 200


async def test_agent_ready_endpoint_ok_when_no_startup_ready_attr(client, mock_orchestrator) -> None:
    """POST /agents/{id}/ready must not error if agent lacks _startup_ready (e.g. non-ClaudeCodeAgent)."""
    mock_agent = MagicMock(spec=[])  # no attributes at all
    mock_agent.id = "agent-no-event"
    mock_orchestrator._agents["agent-no-event"] = mock_agent

    resp = await client.post(
        "/agents/agent-no-event/ready",
        headers={"X-API-Key": _API_KEY},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Integration: ClaudeCodeAgent start() — plugin-dir and startup event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_start_sets_startup_ready_event(tmp_path: Path) -> None:
    """ClaudeCodeAgent.start() must set _startup_ready to an asyncio.Event when web_base_url is set.

    The SessionStart hook is provided by the agent plugin loaded via --plugin-dir.
    The plugin's session-start.sh calls POST /agents/{id}/ready, which sets this event.
    """
    from tmux_orchestrator.config import AgentRole

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
        role=AgentRole.WORKER,
    )

    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        await agent.start()

    assert isinstance(agent._startup_ready, asyncio.Event), (
        "start() must set _startup_ready to an asyncio.Event when web_base_url is set"
    )

    await agent.stop()


@pytest.mark.asyncio
async def test_director_start_sets_startup_ready_event(tmp_path: Path) -> None:
    """DIRECTOR agents also get _startup_ready set to an asyncio.Event when web_base_url is set."""
    from tmux_orchestrator.config import AgentRole

    bus = make_bus()
    tmux = make_tmux_mock()

    wm = MagicMock()
    wm.setup = MagicMock(return_value=tmp_path)

    agent = ClaudeCodeAgent(
        agent_id="director-agent",
        bus=bus,
        tmux=tmux,
        worktree_manager=wm,
        web_base_url="http://localhost:8000",
        role=AgentRole.DIRECTOR,
    )

    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        await agent.start()

    assert isinstance(agent._startup_ready, asyncio.Event), (
        "DIRECTOR must also get _startup_ready set to an asyncio.Event when web_base_url is set"
    )

    await agent.stop()


@pytest.mark.asyncio
async def test_start_includes_plugin_dir_in_launch_command(tmp_path: Path) -> None:
    """start() must pass --plugin-dir to the claude launch command when the plugin dir exists."""
    from tmux_orchestrator.config import AgentRole

    bus = make_bus()
    tmux = make_tmux_mock()

    wm = MagicMock()
    wm.setup = MagicMock(return_value=tmp_path)

    agent = ClaudeCodeAgent(
        agent_id="plugin-test-agent",
        bus=bus,
        tmux=tmux,
        worktree_manager=wm,
        web_base_url="http://localhost:8000",
        role=AgentRole.WORKER,
    )

    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        await agent.start()

    # Collect all send_keys calls and find the launch command
    send_keys_calls = tmux.send_keys.call_args_list
    launch_calls = [str(call) for call in send_keys_calls if "--plugin-dir" in str(call)]
    assert len(launch_calls) >= 1, (
        f"Expected --plugin-dir in launch command; send_keys calls: {send_keys_calls}"
    )
    assert "agent_plugin" in launch_calls[0], (
        f"--plugin-dir must point to agent_plugin directory; got: {launch_calls[0]}"
    )

    await agent.stop()


@pytest.mark.asyncio
async def test_worker_stop_calls_on_stop_strategy(tmp_path: Path) -> None:
    """WORKER stop() must call NudgingStrategy.on_stop() (which removes settings.local.json if written)."""
    from tmux_orchestrator.config import AgentRole

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
        role=AgentRole.WORKER,
    )

    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        await agent.start()

    # Write a fake settings.local.json as if on_task_dispatch was called
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("{}")

    await agent.stop()

    # NudgingStrategy.on_stop() should remove the settings file
    assert not settings_path.exists(), (
        "stop() must invoke NudgingStrategy.on_stop() which removes settings.local.json"
    )


@pytest.mark.asyncio
async def test_set_session_env_vars_sets_only_api_key(tmp_path: Path) -> None:
    """_set_session_env_vars() must set only the API key on the tmux session.

    Per-agent values (AGENT_ID, WEB_BASE_URL) are embedded directly in the
    launch command via ``env VAR=value`` so concurrent agent starts cannot
    race on session-level variables.
    """
    from tmux_orchestrator.config import AgentRole

    bus = make_bus()
    tmux = make_tmux_mock()
    mock_session = MagicMock()
    tmux.ensure_session = MagicMock(return_value=mock_session)

    agent = ClaudeCodeAgent(
        agent_id="env-test-agent",
        bus=bus,
        tmux=tmux,
        web_base_url="http://localhost:9999",
        api_key="my-secret-key",
        role=AgentRole.WORKER,
    )

    agent._set_session_env_vars()

    set_env_calls = {
        call.args[0]: call.args[1]
        for call in mock_session.set_environment.call_args_list
    }
    # Only API_KEY goes in the session environment (shared, same for all agents).
    assert set_env_calls.get("TMUX_ORCHESTRATOR_API_KEY") == "my-secret-key"
    # Per-agent vars must NOT be in the session env (race condition risk).
    assert "TMUX_ORCHESTRATOR_AGENT_ID" not in set_env_calls
    assert "TMUX_ORCHESTRATOR_WEB_BASE_URL" not in set_env_calls


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


async def test_task_complete_stop_hook_false_sends_nudge(
    client, mock_orchestrator
) -> None:
    """stop_hook_active=False means Claude finished a response turn — must send nudge, NOT complete."""
    from tmux_orchestrator.agents.base import AgentStatus

    mock_agent = MagicMock()
    mock_agent.status = AgentStatus.BUSY
    mock_task = MagicMock()
    mock_task.id = "task-y-1234abcd"
    mock_agent._current_task = mock_task
    mock_agent.id = "worker-msg"
    mock_agent.bus = mock_orchestrator.bus
    mock_agent.handle_output = AsyncMock()
    mock_agent.notify_stdin = AsyncMock()
    mock_orchestrator._agents["worker-msg"] = mock_agent

    resp = await client.post(
        "/agents/worker-msg/task-complete",
        headers={"X-API-Key": _API_KEY},
        json={
            "stop_hook_active": False,
            "last_assistant_message": "Here is the result.",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "nudged"
    # Must send nudge, NOT complete the task
    mock_agent.notify_stdin.assert_awaited_once()
    nudge_text = mock_agent.notify_stdin.call_args[0][0]
    assert "__ORCHESTRATOR__" in nudge_text
    assert "/task-complete" in nudge_text
    mock_agent.handle_output.assert_not_awaited()


async def test_task_complete_explicit_uses_output_field(
    client, mock_orchestrator
) -> None:
    """Explicit /task-complete (no stop_hook_active key) must use 'output' field."""
    from tmux_orchestrator.agents.base import AgentStatus

    mock_agent = MagicMock()
    mock_agent.status = AgentStatus.BUSY
    mock_agent._current_task = MagicMock(id="task-y")
    mock_agent.id = "worker-explicit"
    mock_agent.bus = mock_orchestrator.bus
    mock_agent.handle_output = AsyncMock()
    mock_orchestrator._agents["worker-explicit"] = mock_agent

    resp = await client.post(
        "/agents/worker-explicit/task-complete",
        headers={"X-API-Key": _API_KEY},
        json={"output": "Task finished successfully."},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    mock_agent.handle_output.assert_awaited_once_with("Task finished successfully.")


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
# Director agents: explicit signal required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_completion_director_requires_explicit_signal() -> None:
    """ExplicitSignalStrategy must never auto-complete — only explicit signal ends the task."""
    strategy = ExplicitSignalStrategy()

    tmux = make_tmux_mock()
    tmux.capture_pane = MagicMock(return_value="❯ ")

    class FakeAgent:
        id = "agent-director"
        pane = MagicMock()
        _tmux = tmux
        _current_task: "Task | None" = None

        async def handle_output(self, text: str) -> None:
            pass

        notify_stdin = AsyncMock()

    from tmux_orchestrator.agents.base import Task

    agent = FakeAgent()
    task = Task(id="t-dir", prompt="orchestrate something")
    agent._current_task = task

    async def explicit_complete():
        await asyncio.sleep(0.3)
        agent._current_task = None

    asyncio.create_task(explicit_complete())

    import time
    t0 = time.monotonic()
    await asyncio.wait_for(strategy.wait(agent, task), timeout=3.0)
    elapsed = time.monotonic() - t0

    assert elapsed >= 0.25, (
        f"Director completed too early ({elapsed:.2f}s) — pane polling must be disabled"
    )


@pytest.mark.asyncio
async def test_wait_for_completion_returns_when_task_cleared() -> None:
    """NudgingStrategy.wait must return when _current_task is cleared by explicit signal."""
    strategy = NudgingStrategy("worker-wfc", "http://localhost:8000")

    class FakeAgent:
        id = "worker-wfc"
        pane = MagicMock()
        _current_task: "Task | None" = None
        handle_output = AsyncMock()
        notify_stdin = AsyncMock()

    from tmux_orchestrator.agents.base import Task

    agent = FakeAgent()
    task = Task(id="t-cleared", prompt="do something")
    agent._current_task = task

    async def clear_task_after_delay():
        await asyncio.sleep(0.05)
        agent._current_task = None

    asyncio.create_task(clear_task_after_delay())

    await asyncio.wait_for(strategy.wait(agent, task), timeout=3.0)
    agent.handle_output.assert_not_awaited()
