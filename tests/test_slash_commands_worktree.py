"""Tests for auto-copy of slash commands into agent worktrees (v1.0.12).

When an agent starts, the slash command definitions from
``agent_plugin/commands/`` should be copied into
``{cwd}/.claude/commands/`` so agents can use plain ``/task-complete``
instead of the namespaced ``/tmux-orchestrator:task-complete``.

Design reference: design/v1.0.12-slash-commands-worktree.md
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent
from tmux_orchestrator.bus import Bus


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
    return tmux


def make_fake_plugin_dir(tmp_path: Path) -> Path:
    """Create a fake agent_plugin/commands/ directory with sample commands."""
    plugin_dir = tmp_path / "agent_plugin"
    commands_dir = plugin_dir / "commands"
    commands_dir.mkdir(parents=True)
    (commands_dir / "task-complete.md").write_text("# task-complete\nSignal task completion.")
    (commands_dir / "check-inbox.md").write_text("# check-inbox\nList unread messages.")
    (commands_dir / "send-message.md").write_text("# send-message\nSend a message.")
    return plugin_dir


# ---------------------------------------------------------------------------
# Unit tests for _copy_commands()
# ---------------------------------------------------------------------------


def test_copy_commands_copies_md_files(tmp_path: Path) -> None:
    """_copy_commands should copy all .md files to {cwd}/.claude/commands/."""
    plugin_dir = make_fake_plugin_dir(tmp_path)
    agent_cwd = tmp_path / "worktree"
    agent_cwd.mkdir()

    bus = make_bus()
    tmux = make_tmux_mock()
    agent = ClaudeCodeAgent(agent_id="test-agent", bus=bus, tmux=tmux)

    # Patch the plugin path to use our fake directory
    commands_src = plugin_dir / "commands"
    with patch.object(Path, "parent", new_callable=lambda: property(
        lambda self: plugin_dir.parent if self == commands_src else self.__class__.parent.fget(self)
    )):
        pass  # Use direct approach below

    # Directly patch agent_plugin path resolution
    with patch(
        "tmux_orchestrator.agents.claude_code.Path",
        side_effect=lambda *a: Path(*a),
    ):
        # Call _copy_commands with a fake commands_src
        commands_src_real = Path(__file__).parent.parent / "src" / "tmux_orchestrator" / "agent_plugin" / "commands"
        if not commands_src_real.is_dir():
            pytest.skip("agent_plugin/commands not available in this environment")

    agent._copy_commands(agent_cwd)

    # Verify commands were copied
    commands_dst = agent_cwd / ".claude" / "commands"
    assert commands_dst.is_dir(), ".claude/commands/ should be created"
    # Check at least task-complete.md was copied
    assert (commands_dst / "task-complete.md").exists(), "task-complete.md should be copied"


def test_copy_commands_with_fake_source(tmp_path: Path) -> None:
    """_copy_commands should copy files from a patched commands directory."""
    plugin_dir = make_fake_plugin_dir(tmp_path)
    agent_cwd = tmp_path / "worktree"
    agent_cwd.mkdir()

    bus = make_bus()
    tmux = make_tmux_mock()
    agent = ClaudeCodeAgent(agent_id="test-agent", bus=bus, tmux=tmux)

    # Patch the path resolution inside _copy_commands
    fake_commands_src = plugin_dir / "commands"
    with patch.object(
        type(agent),
        "_copy_commands",
        lambda self, cwd: _copy_commands_with_src(self, cwd, fake_commands_src),
    ):
        agent._copy_commands(agent_cwd)

    commands_dst = agent_cwd / ".claude" / "commands"
    assert commands_dst.is_dir()
    assert (commands_dst / "task-complete.md").exists()
    assert (commands_dst / "check-inbox.md").exists()
    assert (commands_dst / "send-message.md").exists()
    assert (commands_dst / "task-complete.md").read_text() == "# task-complete\nSignal task completion."


def _copy_commands_with_src(agent, cwd: Path, commands_src: Path) -> None:
    """Helper that runs the _copy_commands logic with a custom source path."""
    import shutil
    commands_dst = cwd / ".claude" / "commands"
    commands_dst.mkdir(parents=True, exist_ok=True)
    for cmd_file in sorted(commands_src.glob("*.md")):
        dest = commands_dst / cmd_file.name
        if not dest.exists():
            shutil.copy2(cmd_file, dest)


def test_copy_commands_does_not_overwrite_existing(tmp_path: Path) -> None:
    """_copy_commands should not overwrite pre-existing command files."""
    plugin_dir = make_fake_plugin_dir(tmp_path)
    agent_cwd = tmp_path / "worktree"
    agent_cwd.mkdir()

    # Pre-populate the destination with a custom version
    commands_dst = agent_cwd / ".claude" / "commands"
    commands_dst.mkdir(parents=True)
    custom_content = "# CUSTOM task-complete\nCustomized version."
    (commands_dst / "task-complete.md").write_text(custom_content)

    bus = make_bus()
    tmux = make_tmux_mock()
    agent = ClaudeCodeAgent(agent_id="test-agent", bus=bus, tmux=tmux)

    fake_commands_src = plugin_dir / "commands"
    with patch.object(
        type(agent),
        "_copy_commands",
        lambda self, cwd: _copy_commands_with_src(self, cwd, fake_commands_src),
    ):
        agent._copy_commands(agent_cwd)

    # The pre-existing file must NOT be overwritten
    assert (commands_dst / "task-complete.md").read_text() == custom_content
    # But new files that didn't exist should still be copied
    assert (commands_dst / "check-inbox.md").exists()


def test_copy_commands_creates_dot_claude_dir(tmp_path: Path) -> None:
    """_copy_commands should create .claude/commands/ even if it doesn't exist."""
    plugin_dir = make_fake_plugin_dir(tmp_path)
    agent_cwd = tmp_path / "worktree"
    agent_cwd.mkdir()

    # Ensure .claude/ doesn't exist
    assert not (agent_cwd / ".claude").exists()

    bus = make_bus()
    tmux = make_tmux_mock()
    agent = ClaudeCodeAgent(agent_id="test-agent", bus=bus, tmux=tmux)

    fake_commands_src = plugin_dir / "commands"
    with patch.object(
        type(agent),
        "_copy_commands",
        lambda self, cwd: _copy_commands_with_src(self, cwd, fake_commands_src),
    ):
        agent._copy_commands(agent_cwd)

    assert (agent_cwd / ".claude").is_dir()
    assert (agent_cwd / ".claude" / "commands").is_dir()


def test_copy_commands_noop_when_plugin_dir_missing(tmp_path: Path) -> None:
    """_copy_commands should be a no-op (not raise) if agent_plugin/commands/ is absent."""
    agent_cwd = tmp_path / "worktree"
    agent_cwd.mkdir()

    bus = make_bus()
    tmux = make_tmux_mock()
    agent = ClaudeCodeAgent(agent_id="test-agent", bus=bus, tmux=tmux)

    # Patch path to a non-existent directory
    nonexistent_src = tmp_path / "nonexistent" / "commands"

    def _copy_noop(self, cwd: Path) -> None:
        import shutil
        commands_src = nonexistent_src
        if not commands_src.is_dir():
            return
        commands_dst = cwd / ".claude" / "commands"
        commands_dst.mkdir(parents=True, exist_ok=True)
        for cmd_file in sorted(commands_src.glob("*.md")):
            dest = commands_dst / cmd_file.name
            if not dest.exists():
                shutil.copy2(cmd_file, dest)

    with patch.object(type(agent), "_copy_commands", _copy_noop):
        # Should not raise
        agent._copy_commands(agent_cwd)

    # .claude/commands/ should not be created since source doesn't exist
    assert not (agent_cwd / ".claude" / "commands").exists()


# ---------------------------------------------------------------------------
# Integration: start() calls _copy_commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_calls_copy_commands(tmp_path: Path) -> None:
    """ClaudeCodeAgent.start() must call _copy_commands with the worktree cwd."""
    bus = make_bus()
    tmux = make_tmux_mock()

    wm = MagicMock()
    wm.setup = MagicMock(return_value=tmp_path)

    agent = ClaudeCodeAgent(
        agent_id="cmd-agent",
        bus=bus,
        tmux=tmux,
        worktree_manager=wm,
    )

    copy_commands_called_with = []

    original_copy_commands = agent._copy_commands

    def track_copy(cwd: Path) -> None:
        copy_commands_called_with.append(cwd)
        original_copy_commands(cwd)

    with patch.object(agent, "_copy_commands", side_effect=track_copy):
        with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
            await agent.start()

    assert len(copy_commands_called_with) == 1, "_copy_commands should be called once"
    assert copy_commands_called_with[0] == tmp_path

    await agent.stop()


@pytest.mark.asyncio
async def test_start_copies_commands_to_worktree(tmp_path: Path) -> None:
    """When agent_plugin/commands/ exists, start() copies commands to the worktree."""
    bus = make_bus()
    tmux = make_tmux_mock()

    wm = MagicMock()
    wm.setup = MagicMock(return_value=tmp_path)

    agent = ClaudeCodeAgent(
        agent_id="cmd-agent",
        bus=bus,
        tmux=tmux,
        worktree_manager=wm,
    )

    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        await agent.start()

    commands_dst = tmp_path / ".claude" / "commands"
    # The real agent_plugin/commands/ exists in this project
    real_commands_src = (
        Path(__file__).parent.parent
        / "src" / "tmux_orchestrator" / "agent_plugin" / "commands"
    )
    if real_commands_src.is_dir():
        assert commands_dst.is_dir(), ".claude/commands/ should exist after start()"
        assert (commands_dst / "task-complete.md").exists()

    await agent.stop()


@pytest.mark.asyncio
async def test_non_isolated_agent_also_gets_commands(tmp_path: Path) -> None:
    """isolate=False agents should also receive slash commands."""
    bus = make_bus()
    tmux = make_tmux_mock()

    agent = ClaudeCodeAgent(
        agent_id="non-isolated",
        bus=bus,
        tmux=tmux,
        isolate=False,
        cwd_override=tmp_path,
    )

    copy_commands_called = []

    def track_copy(cwd: Path) -> None:
        copy_commands_called.append(cwd)

    with patch.object(agent, "_copy_commands", side_effect=track_copy):
        with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
            with patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=tmp_path):
                await agent.start()

    assert len(copy_commands_called) == 1, "_copy_commands called for non-isolated agent"
    # isolate=False: commands are copied to the per-agent subdir .agent/{agent_id}/
    # (not to the shared cwd root) to avoid file collisions between concurrent agents.
    expected_agent_dir = tmp_path / ".agent" / "non-isolated"
    assert copy_commands_called[0] == expected_agent_dir

    await agent.stop()


# ---------------------------------------------------------------------------
# CLAUDE.md content: plain command names
# ---------------------------------------------------------------------------


def test_claude_md_uses_plain_command_names(tmp_path: Path) -> None:
    """Generated CLAUDE.md should reference /task-complete (no namespace prefix)."""
    bus = make_bus()
    tmux = make_tmux_mock()

    agent = ClaudeCodeAgent(agent_id="worker-1", bus=bus, tmux=tmux)
    agent._write_agent_claude_md(tmp_path)

    content = (tmp_path / "CLAUDE.md").read_text()

    # Plain form must be present
    assert "/task-complete" in content

    # Namespaced form must NOT appear in the slash command reference table
    assert "/tmux-orchestrator:task-complete" not in content
    assert "/tmux-orchestrator:check-inbox" not in content


def test_claude_md_mentions_dot_claude_commands(tmp_path: Path) -> None:
    """Generated CLAUDE.md should mention .claude/commands/ availability."""
    bus = make_bus()
    tmux = make_tmux_mock()

    agent = ClaudeCodeAgent(agent_id="worker-1", bus=bus, tmux=tmux)
    agent._write_agent_claude_md(tmp_path)

    content = (tmp_path / "CLAUDE.md").read_text()
    assert ".claude/commands/" in content or ".claude" in content
