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
        patch("tmux_orchestrator.application.factory.TmuxInterface") as MockTmux,
        patch("tmux_orchestrator.application.factory.WorktreeManager", side_effect=RuntimeError("not git")),
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
        patch("tmux_orchestrator.application.factory.TmuxInterface") as MockTmux,
        patch("tmux_orchestrator.application.factory.WorktreeManager", side_effect=RuntimeError("not git")),
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
        patch("tmux_orchestrator.application.factory.TmuxInterface") as MockTmux,
        patch("tmux_orchestrator.application.factory.WorktreeManager", side_effect=RuntimeError("not git")),
    ):
        MockTmux.return_value = MagicMock()
        with pytest.raises(ValueError, match="Unknown agent type"):
            build_system(cfg)


def test_build_system_confirm_kill_forwarded(tmp_path):
    """confirm_kill callback is passed through to TmuxInterface."""
    cfg = _write_config(tmp_path, "session_name: s\nagents: []\n")
    callback = MagicMock(return_value=True)
    with (
        patch("tmux_orchestrator.application.factory.TmuxInterface") as MockTmux,
        patch("tmux_orchestrator.application.factory.WorktreeManager", side_effect=RuntimeError),
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


# ---------------------------------------------------------------------------
# repo_root config field (v1.0.0 worktree cwd bug fix)
# ---------------------------------------------------------------------------


def test_build_system_uses_config_repo_root(tmp_path):
    """When OrchestratorConfig.repo_root is set (via YAML), WorktreeManager
    is initialised with that path instead of Path.cwd().

    This prevents the 'cwd=PROJECT_ROOT' demo bug where worktrees were
    accidentally created inside the orchestrator's own repo when a demo script
    launched the server from the project root.
    """
    # Create a fake git repo under tmp_path to give WorktreeManager a valid root.
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()

    cfg_content = f"""\
session_name: test-repo-root
mailbox_dir: /tmp/orch-test-mailbox
repo_root: {repo}
agents: []
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(cfg_content)

    captured_roots: list = []

    class _CapturingWM:
        def __init__(self, root):
            captured_roots.append(Path(root).resolve())

        @staticmethod
        def find_repo_root(start):
            return repo.resolve()

        def _ensure_gitignore(self):
            pass

    with (
        patch("tmux_orchestrator.application.factory.TmuxInterface") as MockTmux,
        patch("tmux_orchestrator.application.factory.WorktreeManager", new=_CapturingWM),
    ):
        MockTmux.return_value = MagicMock()
        build_system(cfg_file)

    assert len(captured_roots) == 1, "WorktreeManager should be instantiated once"
    assert captured_roots[0] == repo.resolve(), (
        f"WorktreeManager should use config.repo_root={repo.resolve()!r}, "
        f"got {captured_roots[0]!r}"
    )


def test_build_system_config_path_parent_used_when_no_repo_root(tmp_path):
    """When repo_root is not set and the config file sits inside a git repo,
    config_path.parent is used as the WorktreeManager base — not Path.cwd().

    This is the 'good heuristic' path: if the YAML is committed inside the
    target repo the config's directory is always a valid git repo ancestor.
    """
    # Create git repo at tmp_path level
    (tmp_path / ".git").mkdir()

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("session_name: s\nagents: []\n")

    captured_roots: list = []

    class _CapturingWM:
        def __init__(self, root):
            captured_roots.append(Path(root).resolve())

        @staticmethod
        def find_repo_root(start):
            return tmp_path.resolve()

        def _ensure_gitignore(self):
            pass

    with (
        patch("tmux_orchestrator.application.factory.TmuxInterface") as MockTmux,
        patch("tmux_orchestrator.application.factory.WorktreeManager", new=_CapturingWM),
    ):
        MockTmux.return_value = MagicMock()
        build_system(cfg_file)

    assert len(captured_roots) == 1
    # Should use config_path.parent (tmp_path) rather than cwd
    assert captured_roots[0] == tmp_path.resolve()


def test_load_config_repo_root_parsed(tmp_path):
    """repo_root in YAML is parsed into an absolute Path."""
    from tmux_orchestrator.config import load_config

    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"repo_root: {tmp_path}\nagents: []\n")

    config = load_config(cfg)
    assert config.repo_root == tmp_path.resolve()


def test_load_config_repo_root_defaults_to_none(tmp_path):
    """When repo_root is absent from YAML, config.repo_root is None."""
    from tmux_orchestrator.config import load_config

    cfg = tmp_path / "config.yaml"
    cfg.write_text("agents: []\n")

    config = load_config(cfg)
    assert config.repo_root is None
