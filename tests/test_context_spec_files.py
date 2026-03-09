"""Tests for context_spec_files glob-pattern spec file auto-copy (v0.41.0).

Codified Context Infrastructure: agents receive on-demand specification documents
as cold-memory context. Globs are expanded at agent start time.

Design references:
- Vasilopoulos arXiv:2602.20478 "Codified Context" (2026-02): 3-tier memory,
  cold-memory spec docs as 3rd tier.
- Anthropic "Effective Context Engineering for AI Agents" (2025): spec docs
  enable persistent memory simulation.
- DESIGN.md §10.15 (v0.41.0)
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tmux_orchestrator.config import AgentConfig, load_config


# ---------------------------------------------------------------------------
# AgentConfig.context_spec_files field
# ---------------------------------------------------------------------------

class TestAgentConfigContextSpecFiles:
    def test_default_is_empty_list(self):
        cfg = AgentConfig(id="w1", type="claude_code")
        assert cfg.context_spec_files == []

    def test_can_set_context_spec_files(self):
        cfg = AgentConfig(
            id="w1",
            type="claude_code",
            context_spec_files=[".claude/specs/*.md", ".claude/specs/conventions.yaml"],
        )
        assert ".claude/specs/*.md" in cfg.context_spec_files

    def test_context_spec_files_is_list(self):
        cfg = AgentConfig(id="w1", type="claude_code", context_spec_files=["spec/*.md"])
        assert isinstance(cfg.context_spec_files, list)


# ---------------------------------------------------------------------------
# load_config: context_spec_files parsed from YAML
# ---------------------------------------------------------------------------

class TestLoadConfigContextSpecFiles:
    def test_context_spec_files_loaded_from_yaml(self, tmp_path):
        yaml_content = """\
session_name: test
agents:
  - id: worker-1
    type: claude_code
    context_spec_files:
      - .claude/specs/architecture.md
      - .claude/specs/decisions/*.md
"""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml_content)
        config = load_config(cfg_path)
        assert ".claude/specs/architecture.md" in config.agents[0].context_spec_files
        assert ".claude/specs/decisions/*.md" in config.agents[0].context_spec_files

    def test_context_spec_files_defaults_to_empty_when_absent(self, tmp_path):
        yaml_content = """\
session_name: test
agents:
  - id: worker-1
    type: claude_code
"""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml_content)
        config = load_config(cfg_path)
        assert config.agents[0].context_spec_files == []

    def test_context_spec_files_and_context_files_coexist(self, tmp_path):
        yaml_content = """\
session_name: test
agents:
  - id: worker-1
    type: claude_code
    context_files:
      - docs/README.md
    context_spec_files:
      - .claude/specs/*.md
"""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml_content)
        config = load_config(cfg_path)
        assert "docs/README.md" in config.agents[0].context_files
        assert ".claude/specs/*.md" in config.agents[0].context_spec_files


# ---------------------------------------------------------------------------
# ClaudeCodeAgent._copy_context_spec_files (unit tests)
# ---------------------------------------------------------------------------

def make_agent(tmp_path: Path, spec_files: list[str] = None, spec_root: Path = None):
    """Create a ClaudeCodeAgent with minimal mocking."""
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent
    from tmux_orchestrator.bus import Bus

    bus = Bus()
    tmux = MagicMock()
    tmux.new_pane = MagicMock(return_value=MagicMock(id="pane-1"))
    tmux.send_keys = MagicMock()
    tmux.capture_pane = MagicMock(return_value="❯ ")

    return ClaudeCodeAgent(
        agent_id="worker-1",
        bus=bus,
        tmux=tmux,
        mailbox=MagicMock(),
        worktree_manager=None,
        isolate=False,
        session_name="test",
        web_base_url="",
        api_key="",
        context_spec_files=spec_files or [],
        context_spec_files_root=spec_root,
    )


class TestCopyContextSpecFiles:
    def test_copies_single_spec_file(self, tmp_path):
        """A literal (non-glob) path in context_spec_files is copied."""
        spec_root = tmp_path / "repo"
        spec_root.mkdir()
        specs_dir = spec_root / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        arch_file = specs_dir / "architecture.md"
        arch_file.write_text("# Architecture\nMicroservices.")

        agent = make_agent(
            tmp_path,
            spec_files=[".claude/specs/architecture.md"],
            spec_root=spec_root,
        )

        dest_dir = tmp_path / "worktree"
        dest_dir.mkdir()
        agent._copy_context_spec_files(dest_dir)

        dest_file = dest_dir / ".claude" / "specs" / "architecture.md"
        assert dest_file.exists()
        assert dest_file.read_text() == "# Architecture\nMicroservices."

    def test_copies_files_matching_glob(self, tmp_path):
        """Glob patterns are expanded and all matching files are copied."""
        spec_root = tmp_path / "repo"
        spec_root.mkdir()
        decisions_dir = spec_root / ".claude" / "specs" / "decisions"
        decisions_dir.mkdir(parents=True)
        (decisions_dir / "adr-001.md").write_text("# ADR 001")
        (decisions_dir / "adr-002.md").write_text("# ADR 002")
        (decisions_dir / "adr-003.md").write_text("# ADR 003")

        agent = make_agent(
            tmp_path,
            spec_files=[".claude/specs/decisions/*.md"],
            spec_root=spec_root,
        )

        dest_dir = tmp_path / "worktree"
        dest_dir.mkdir()
        agent._copy_context_spec_files(dest_dir)

        copied = list((dest_dir / ".claude" / "specs" / "decisions").glob("*.md"))
        assert len(copied) == 3
        names = {f.name for f in copied}
        assert names == {"adr-001.md", "adr-002.md", "adr-003.md"}

    def test_skips_nonexistent_spec_file_with_warning(self, tmp_path):
        """A non-existent literal path emits a warning but does not raise."""
        spec_root = tmp_path / "repo"
        spec_root.mkdir()

        agent = make_agent(
            tmp_path,
            spec_files=[".claude/specs/does-not-exist.md"],
            spec_root=spec_root,
        )

        dest_dir = tmp_path / "worktree"
        dest_dir.mkdir()
        # Should not raise
        agent._copy_context_spec_files(dest_dir)
        # No files should be copied
        assert not list(dest_dir.rglob("*.md"))

    def test_skips_nonmatching_glob_silently(self, tmp_path):
        """A glob that matches no files does not raise."""
        spec_root = tmp_path / "repo"
        spec_root.mkdir()
        (spec_root / ".claude" / "specs").mkdir(parents=True)

        agent = make_agent(
            tmp_path,
            spec_files=[".claude/specs/*.md"],
            spec_root=spec_root,
        )

        dest_dir = tmp_path / "worktree"
        dest_dir.mkdir()
        agent._copy_context_spec_files(dest_dir)
        # No error, no files copied
        assert not list(dest_dir.rglob("*.md"))

    def test_noop_when_context_spec_files_empty(self, tmp_path):
        """Empty context_spec_files list results in no copies."""
        spec_root = tmp_path / "repo"
        spec_root.mkdir()

        agent = make_agent(tmp_path, spec_files=[], spec_root=spec_root)
        dest_dir = tmp_path / "worktree"
        dest_dir.mkdir()
        agent._copy_context_spec_files(dest_dir)
        assert not list(dest_dir.rglob("*"))

    def test_noop_when_spec_root_is_none_warns(self, tmp_path):
        """When spec_root is None but spec_files is non-empty, warns and skips."""
        agent = make_agent(
            tmp_path,
            spec_files=[".claude/specs/arch.md"],
            spec_root=None,
        )
        dest_dir = tmp_path / "worktree"
        dest_dir.mkdir()
        # Should not raise
        agent._copy_context_spec_files(dest_dir)
        assert not list(dest_dir.rglob("*"))

    def test_multiple_patterns_combined(self, tmp_path):
        """Multiple glob patterns all expand and copy correctly."""
        spec_root = tmp_path / "repo"
        (spec_root / ".claude" / "specs").mkdir(parents=True)
        (spec_root / ".claude" / "specs" / "arch.md").write_text("arch")
        (spec_root / ".claude" / "specs" / "conv.yaml").write_text("conventions:")

        agent = make_agent(
            tmp_path,
            spec_files=[
                ".claude/specs/*.md",
                ".claude/specs/*.yaml",
            ],
            spec_root=spec_root,
        )

        dest_dir = tmp_path / "worktree"
        dest_dir.mkdir()
        agent._copy_context_spec_files(dest_dir)

        specs = dest_dir / ".claude" / "specs"
        assert (specs / "arch.md").exists()
        assert (specs / "conv.yaml").exists()

    def test_spec_files_and_context_files_both_copied(self, tmp_path):
        """Both context_files and context_spec_files are copied independently."""
        spec_root = tmp_path / "repo"
        spec_root.mkdir()
        (spec_root / ".claude" / "specs").mkdir(parents=True)
        (spec_root / ".claude" / "specs" / "arch.md").write_text("arch")
        (spec_root / "docs").mkdir()
        (spec_root / "docs" / "README.md").write_text("readme")

        from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent
        from tmux_orchestrator.bus import Bus

        bus = Bus()
        tmux = MagicMock()
        tmux.new_pane = MagicMock(return_value=MagicMock(id="pane-1"))
        tmux.send_keys = MagicMock()
        tmux.capture_pane = MagicMock(return_value="❯ ")

        agent = ClaudeCodeAgent(
            agent_id="worker-1",
            bus=bus,
            tmux=tmux,
            mailbox=MagicMock(),
            worktree_manager=None,
            isolate=False,
            session_name="test",
            web_base_url="",
            api_key="",
            context_files=["docs/README.md"],
            context_files_root=spec_root,
            context_spec_files=[".claude/specs/*.md"],
            context_spec_files_root=spec_root,
        )

        dest_dir = tmp_path / "worktree"
        dest_dir.mkdir()
        agent._copy_context_files(dest_dir)
        agent._copy_context_spec_files(dest_dir)

        assert (dest_dir / "docs" / "README.md").exists()
        assert (dest_dir / ".claude" / "specs" / "arch.md").exists()


# ---------------------------------------------------------------------------
# factory.py: build_system wires context_spec_files_root
# ---------------------------------------------------------------------------

class TestBuildSystemContextSpecFiles:
    def test_context_spec_files_passed_to_agent(self, tmp_path):
        """build_system passes context_spec_files and root to ClaudeCodeAgent."""
        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)

        yaml_content = """\
session_name: test
mailbox_dir: /tmp/orch-test
agents:
  - id: worker-1
    type: claude_code
    context_spec_files:
      - .claude/specs/*.md
"""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml_content)

        from unittest.mock import patch, MagicMock

        with (
            patch("tmux_orchestrator.application.factory.TmuxInterface") as MockTmux,
            patch("tmux_orchestrator.application.factory.WorktreeManager", side_effect=RuntimeError),
            patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent,
        ):
            MockTmux.return_value = MagicMock()
            mock_agent = MagicMock()
            mock_agent.id = "worker-1"
            MockAgent.return_value = mock_agent

            from tmux_orchestrator.factory import build_system
            build_system(cfg_path)

            call_kwargs = MockAgent.call_args[1]
            assert call_kwargs.get("context_spec_files") == [".claude/specs/*.md"]
            assert call_kwargs.get("context_spec_files_root") is not None
