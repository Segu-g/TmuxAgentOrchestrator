"""Tests for ProcessPort integration into ClaudeCodeAgent and WebhookManager DI.

Verifies:
- ClaudeCodeAgent exposes a ``process: ProcessPort | None`` attribute
- After start(), ``process`` is set to a TmuxProcessAdapter
- WebhookManager can be injected into Orchestrator via constructor

Design references:
- DESIGN.md §10.34 (v1.0.34 — ProcessPort as canonical ClaudeCodeAgent interface)
- "Dependency Injection: a Python Way" Glukhov (2025)
- "How to Implement Dependency Injection in Python" OneUptime (2026)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.process_port import ProcessPort, TmuxProcessAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(**kwargs) -> OrchestratorConfig:
    defaults = dict(
        session_name="test",
        mailbox_dir="/tmp/test_mailbox",
        web_base_url="http://localhost:8000",
    )
    defaults.update(kwargs)
    return OrchestratorConfig(**defaults)


def make_tmux_mock():
    tmux = MagicMock()
    tmux.ensure_session = MagicMock()
    tmux.kill_session = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.new_pane = MagicMock(return_value=MagicMock())
    tmux.send_keys = MagicMock()
    tmux.capture_pane = MagicMock(return_value="")
    return tmux


# ---------------------------------------------------------------------------
# ProcessPort integration in ClaudeCodeAgent
# ---------------------------------------------------------------------------


def test_claude_code_agent_has_process_attribute():
    """ClaudeCodeAgent exposes a 'process' attribute initialized to None."""
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    bus = Bus()
    tmux = make_tmux_mock()
    agent = ClaudeCodeAgent("test-agent", bus, tmux)
    # Before start(), process is None
    assert agent.process is None


@pytest.mark.asyncio
async def test_claude_code_agent_process_set_after_start():
    """After start(), ClaudeCodeAgent.process is a ProcessPort instance."""
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    bus = Bus()
    tmux = make_tmux_mock()

    # Patch out the expensive parts of start()
    mock_pane = MagicMock()
    mock_pane.id = "%99"
    tmux.new_pane.return_value = mock_pane
    tmux.watch_pane = MagicMock()

    agent = ClaudeCodeAgent(
        "test-agent",
        bus,
        tmux,
        web_base_url="",  # disable startup hook wait
    )

    with (
        patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=None),
        patch.object(agent, "_wait_for_ready", new_callable=AsyncMock),
        patch.object(agent, "_start_message_loop", new_callable=AsyncMock),
        patch("asyncio.create_task", return_value=MagicMock()),
    ):
        await agent.start()

    assert agent.process is not None
    assert isinstance(agent.process, ProcessPort)
    assert isinstance(agent.process, TmuxProcessAdapter)
    # get_pane_id should return the pane's id
    assert agent.process.get_pane_id() == "%99"


def test_claude_code_agent_process_is_tmux_process_adapter():
    """ProcessPort imported from infrastructure path is the canonical class."""
    from tmux_orchestrator.infrastructure.process_port import TmuxProcessAdapter as CanonicalAdapter
    from tmux_orchestrator.process_port import TmuxProcessAdapter as ShimAdapter

    # Both paths reference the same class
    assert CanonicalAdapter is ShimAdapter


@pytest.mark.asyncio
async def test_claude_code_agent_process_port_used_for_dispatch():
    """_dispatch_task raises RuntimeError when process is None (no pane)."""
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent
    from tmux_orchestrator.agents.base import Task

    bus = Bus()
    tmux = make_tmux_mock()
    agent = ClaudeCodeAgent("test-agent", bus, tmux)
    # pane is None and process is None — should raise
    task = Task(id="t1", prompt="hello", priority=0)

    with pytest.raises(RuntimeError, match="has no pane"):
        await agent._dispatch_task(task)


# ---------------------------------------------------------------------------
# WebhookManager dependency injection
# ---------------------------------------------------------------------------


def test_orchestrator_accepts_injected_webhook_manager():
    """Orchestrator accepts an injected WebhookManager instead of creating one."""
    from tmux_orchestrator.webhook_manager import WebhookManager

    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()

    custom_wm = WebhookManager(timeout=99.0)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config, webhook_manager=custom_wm)

    assert orch._webhook_manager is custom_wm


def test_orchestrator_creates_default_webhook_manager_when_none_injected():
    """Orchestrator creates a default WebhookManager when webhook_manager=None."""
    from tmux_orchestrator.webhook_manager import WebhookManager

    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()

    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    assert orch._webhook_manager is not None
    assert isinstance(orch._webhook_manager, WebhookManager)


def test_orchestrator_webhook_manager_timeout_from_config():
    """Default WebhookManager uses timeout from OrchestratorConfig."""
    from tmux_orchestrator.webhook_manager import WebhookManager

    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(webhook_timeout=42.0)

    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    assert orch._webhook_manager._timeout == 42.0


def test_orchestrator_injected_webhook_manager_preserves_state():
    """An injected WebhookManager retains its registered webhooks."""
    from tmux_orchestrator.webhook_manager import WebhookManager

    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()

    custom_wm = WebhookManager()
    custom_wm.register(url="https://example.com/hook", events=["task.completed"])

    orch = Orchestrator(bus=bus, tmux=tmux, config=config, webhook_manager=custom_wm)

    # The injected manager (with its pre-registered hook) is used unchanged
    assert orch._webhook_manager is custom_wm
    hooks = orch._webhook_manager.list_all()
    assert any(h.url == "https://example.com/hook" for h in hooks)
