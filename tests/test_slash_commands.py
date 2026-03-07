"""Tests for slash command auto-copy to agent worktree.

When a ClaudeCodeAgent starts in an isolated worktree, the orchestrator's
bundled slash commands (src/tmux_orchestrator/commands/*.md) must be copied
into {worktree}/.claude/commands/ so that agents can use /send-message,
/check-inbox, /read-message, /spawn-subagent, /list-agents, /progress,
/summarize, /delegate, /plan, /tdd, /change-strategy, /plan-workflow.

Root cause: Claude Code discovers project-scoped commands relative to the
directory it was launched in; worktrees have a different root than the main
TmuxAgentOrchestrator repo, so the top-level .claude/commands/ is invisible.

Design reference: DESIGN.md §10.19 (v1.0.1)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent
from tmux_orchestrator.bus import Bus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_agent(tmp_path: Path, **kwargs) -> ClaudeCodeAgent:
    bus = Bus()
    tmux = MagicMock()
    return ClaudeCodeAgent(
        agent_id="test-agent",
        bus=bus,
        tmux=tmux,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Unit tests for _copy_slash_commands
# ---------------------------------------------------------------------------


def test_copy_slash_commands_creates_dot_claude_commands(tmp_path: Path) -> None:
    """Slash commands should be copied to {cwd}/.claude/commands/."""
    agent = make_agent(tmp_path)
    agent._copy_slash_commands(tmp_path)

    commands_dir = tmp_path / ".claude" / "commands"
    assert commands_dir.is_dir(), ".claude/commands/ must be created"


def test_copy_slash_commands_copies_all_md_files(tmp_path: Path) -> None:
    """All bundled .md files must appear in the destination."""
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    # Discover what should be there
    commands_src = Path(ClaudeCodeAgent._copy_slash_commands.__module__.replace(".", "/"))
    # Use the package's commands directory directly
    pkg_commands = Path(__file__).parent.parent / "src" / "tmux_orchestrator" / "commands"

    agent = make_agent(tmp_path)
    agent._copy_slash_commands(tmp_path)

    commands_dir = tmp_path / ".claude" / "commands"
    copied = {f.name for f in commands_dir.glob("*.md")}
    assert len(copied) > 0, "At least one command file must be copied"

    # All expected core commands must be present
    expected_core = {
        "send-message.md",
        "check-inbox.md",
        "read-message.md",
        "spawn-subagent.md",
        "list-agents.md",
        "progress.md",
        "summarize.md",
        "plan.md",
    }
    missing = expected_core - copied
    assert not missing, f"Missing slash command files: {missing}"


def test_copy_slash_commands_content_is_intact(tmp_path: Path) -> None:
    """Copied files must have the same content as the source."""
    agent = make_agent(tmp_path)
    agent._copy_slash_commands(tmp_path)

    pkg_commands = Path(__file__).parent.parent / "src" / "tmux_orchestrator" / "commands"
    commands_dir = tmp_path / ".claude" / "commands"

    for src_file in pkg_commands.glob("*.md"):
        dst_file = commands_dir / src_file.name
        assert dst_file.exists(), f"{src_file.name} missing in destination"
        assert dst_file.read_text() == src_file.read_text(), (
            f"{src_file.name} content mismatch"
        )


def test_copy_slash_commands_idempotent(tmp_path: Path) -> None:
    """Calling _copy_slash_commands twice must not raise or corrupt files."""
    agent = make_agent(tmp_path)
    agent._copy_slash_commands(tmp_path)
    agent._copy_slash_commands(tmp_path)

    commands_dir = tmp_path / ".claude" / "commands"
    assert commands_dir.is_dir()
    copied = list(commands_dir.glob("*.md"))
    assert len(copied) > 0


def test_copy_slash_commands_missing_src_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If the bundled commands directory is absent, a warning is emitted and no crash."""
    import logging
    from unittest.mock import patch

    agent = make_agent(tmp_path)

    nonexistent = tmp_path / "does_not_exist"
    with patch.object(
        type(agent),
        "_copy_slash_commands",
        wraps=agent._copy_slash_commands,
    ):
        # Patch the Path resolution inside the method
        from tmux_orchestrator.agents import claude_code as mod

        orig_file = mod.__file__

        # Point __file__ to a fake location so commands_src won't exist
        with patch.object(mod, "__file__", str(tmp_path / "fake" / "agents" / "claude_code.py")):
            with caplog.at_level(logging.WARNING, logger="tmux_orchestrator.agents.claude_code"):
                agent._copy_slash_commands(tmp_path)

    # No .claude/commands/ should be created when src doesn't exist
    commands_dir = tmp_path / ".claude" / "commands"
    assert not commands_dir.exists() or len(list(commands_dir.glob("*.md"))) == 0

    # A warning should have been emitted
    assert any("bundled commands directory not found" in r.message for r in caplog.records)


def test_package_commands_dir_exists() -> None:
    """The bundled commands directory must exist in the package source tree."""
    pkg_commands = (
        Path(__file__).parent.parent / "src" / "tmux_orchestrator" / "commands"
    )
    assert pkg_commands.is_dir(), (
        f"Package commands directory missing: {pkg_commands}"
    )
    md_files = list(pkg_commands.glob("*.md"))
    assert len(md_files) >= 8, (
        f"Expected at least 8 command files, found {len(md_files)}: {[f.name for f in md_files]}"
    )


def test_send_message_command_present() -> None:
    """/send-message is the most critical command; it must be bundled."""
    pkg_commands = (
        Path(__file__).parent.parent / "src" / "tmux_orchestrator" / "commands"
    )
    assert (pkg_commands / "send-message.md").exists(), (
        "send-message.md must be in the bundled commands directory"
    )


def test_check_inbox_command_present() -> None:
    """/check-inbox must be bundled."""
    pkg_commands = (
        Path(__file__).parent.parent / "src" / "tmux_orchestrator" / "commands"
    )
    assert (pkg_commands / "check-inbox.md").exists()


def test_commands_not_empty() -> None:
    """Each bundled command file must have non-empty content."""
    pkg_commands = (
        Path(__file__).parent.parent / "src" / "tmux_orchestrator" / "commands"
    )
    for cmd_file in pkg_commands.glob("*.md"):
        content = cmd_file.read_text().strip()
        assert content, f"{cmd_file.name} is empty"
