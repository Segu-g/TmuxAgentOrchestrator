"""Tests for the SystemFactory (factory.py)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tmux_orchestrator.factory import build_system, patch_web_url


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


BASIC_CONFIG = """\
session_name: test-session
mailbox_dir: /tmp/orch-test-mailbox
agents:
  - id: worker-1
    type: claude_code
    role: worker
"""


def _write_config(tmp_path: Path, content: str = BASIC_CONFIG) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(content)
    return cfg


# ---------------------------------------------------------------------------
# build_system
# ---------------------------------------------------------------------------


def test_build_system_returns_three_components(tmp_path):
    cfg = _write_config(tmp_path)
    with (
        patch("tmux_orchestrator.factory.TmuxInterface") as MockTmux,
        patch("tmux_orchestrator.factory.WorktreeManager", side_effect=RuntimeError("not git")),
        patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent,
    ):
        MockTmux.return_value = MagicMock()
        MockAgent.return_value = MagicMock(id="worker-1")

        from tmux_orchestrator.bus import Bus
        from tmux_orchestrator.orchestrator import Orchestrator

        orchestrator, bus, tmux = build_system(cfg)

        assert isinstance(orchestrator, Orchestrator)
        assert isinstance(bus, Bus)


def test_build_system_registers_agents(tmp_path):
    cfg = _write_config(tmp_path)
    with (
        patch("tmux_orchestrator.factory.TmuxInterface") as MockTmux,
        patch("tmux_orchestrator.factory.WorktreeManager", side_effect=RuntimeError("not git")),
        patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent,
    ):
        MockTmux.return_value = MagicMock()
        mock_agent = MagicMock()
        mock_agent.id = "worker-1"
        MockAgent.return_value = mock_agent

        orchestrator, bus, tmux = build_system(cfg)
        assert orchestrator.get_agent("worker-1") is mock_agent


def test_build_system_unknown_agent_type_raises(tmp_path):
    bad_config = """\
session_name: test-session
agents:
  - id: weird-agent
    type: unknown_type
    role: worker
"""
    cfg = _write_config(tmp_path, bad_config)
    with (
        patch("tmux_orchestrator.factory.TmuxInterface") as MockTmux,
        patch("tmux_orchestrator.factory.WorktreeManager", side_effect=RuntimeError("not git")),
    ):
        MockTmux.return_value = MagicMock()
        with pytest.raises(ValueError, match="Unknown agent type"):
            build_system(cfg)


def test_build_system_confirm_kill_forwarded(tmp_path):
    """confirm_kill callback is passed through to TmuxInterface."""
    cfg = _write_config(tmp_path, "session_name: s\nagents: []\n")
    callback = MagicMock(return_value=True)
    with (
        patch("tmux_orchestrator.factory.TmuxInterface") as MockTmux,
        patch("tmux_orchestrator.factory.WorktreeManager", side_effect=RuntimeError),
    ):
        MockTmux.return_value = MagicMock()
        build_system(cfg, confirm_kill=callback)
        MockTmux.assert_called_once()
        _, kwargs = MockTmux.call_args
        assert kwargs["confirm_kill"] is callback


# ---------------------------------------------------------------------------
# patch_web_url
# ---------------------------------------------------------------------------


def test_patch_web_url_updates_claude_agents(tmp_path):
    from unittest.mock import PropertyMock

    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent
    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.config import OrchestratorConfig
    from tmux_orchestrator.orchestrator import Orchestrator

    bus = Bus()
    config = OrchestratorConfig(session_name="s", agents=[], mailbox_dir=str(tmp_path))
    orch = Orchestrator(bus=bus, tmux=MagicMock(), config=config)

    agent = MagicMock(spec=ClaudeCodeAgent)
    agent.id = "a1"
    agent._web_base_url = "http://old"
    orch.registry._agents["a1"] = agent

    patch_web_url(orch, "0.0.0.0", 9000)
    assert agent._web_base_url == "http://localhost:9000"


def test_patch_web_url_skips_non_claude_agents(tmp_path):
    from tmux_orchestrator.agents.base import Agent
    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.config import OrchestratorConfig
    from tmux_orchestrator.orchestrator import Orchestrator

    bus = Bus()
    config = OrchestratorConfig(session_name="s", agents=[], mailbox_dir=str(tmp_path))
    orch = Orchestrator(bus=bus, tmux=MagicMock(), config=config)

    agent = MagicMock(spec=Agent)
    agent.id = "a1"
    orch.registry._agents["a1"] = agent

    # Should not raise or touch the mock's _web_base_url
    patch_web_url(orch, "localhost", 8000)
    assert not hasattr(agent, "_web_base_url") or agent._web_base_url != "http://localhost:8000"
