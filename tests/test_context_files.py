"""Tests for context_files auto-copy to agent worktree (Issue #1).

The AgentConfig.context_files field lists paths (relative to the repo root)
that should be copied into the agent's worktree cwd before the agent starts.
This gives agents focused, pre-loaded context without polluting their entire
directory with unrelated files.

Design reference: DESIGN.md §5 (Context Engineering),
                  §10.5 (v0.11.0 candidates).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tmux_orchestrator.agents.base import AgentStatus
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


# ---------------------------------------------------------------------------
# Tests for _copy_context_files (unit-level, no tmux)
# ---------------------------------------------------------------------------


def test_copy_context_files_copies_files(tmp_path: Path) -> None:
    """_copy_context_files should copy listed paths into the target cwd."""
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    bus = make_bus()
    tmux = make_tmux_mock()

    # Create source files in a "repo root"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    docs_dir = repo_root / "docs"
    docs_dir.mkdir()
    (repo_root / "schema.md").write_text("# Schema\ncolumn: id")
    (docs_dir / "api.md").write_text("# API\nGET /agents")

    agent_cwd = tmp_path / "worktree"
    agent_cwd.mkdir()

    agent = ClaudeCodeAgent(
        agent_id="test-agent",
        bus=bus,
        tmux=tmux,
        context_files=["schema.md", "docs/api.md"],
        context_files_root=repo_root,
    )

    agent._copy_context_files(agent_cwd)

    assert (agent_cwd / "schema.md").read_text() == "# Schema\ncolumn: id"
    assert (agent_cwd / "docs" / "api.md").read_text() == "# API\nGET /agents"


def test_copy_context_files_missing_file_logs_warning(tmp_path: Path, caplog) -> None:
    """Missing context_files should log a warning rather than raise."""
    import logging

    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    bus = make_bus()
    tmux = make_tmux_mock()

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    agent_cwd = tmp_path / "worktree"
    agent_cwd.mkdir()

    agent = ClaudeCodeAgent(
        agent_id="test-agent",
        bus=bus,
        tmux=tmux,
        context_files=["nonexistent.md"],
        context_files_root=repo_root,
    )

    with caplog.at_level(logging.WARNING, logger="tmux_orchestrator.agents.claude_code"):
        agent._copy_context_files(agent_cwd)

    assert "nonexistent.md" in caplog.text


def test_copy_context_files_empty_list(tmp_path: Path) -> None:
    """With no context_files, _copy_context_files should be a no-op."""
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    bus = make_bus()
    tmux = make_tmux_mock()
    agent_cwd = tmp_path / "worktree"
    agent_cwd.mkdir()

    agent = ClaudeCodeAgent(
        agent_id="test-agent",
        bus=bus,
        tmux=tmux,
        context_files=[],
    )

    # Should not raise even without a context_files_root
    agent._copy_context_files(agent_cwd)
    # cwd should still be empty (no files copied)
    assert list(agent_cwd.iterdir()) == []


def test_copy_context_files_preserves_subdirectory_structure(tmp_path: Path) -> None:
    """Nested file paths must recreate the full directory structure in cwd."""
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    bus = make_bus()
    tmux = make_tmux_mock()

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    nested = repo_root / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "deep.txt").write_text("deep content")

    agent_cwd = tmp_path / "worktree"
    agent_cwd.mkdir()

    agent = ClaudeCodeAgent(
        agent_id="test-agent",
        bus=bus,
        tmux=tmux,
        context_files=["a/b/c/deep.txt"],
        context_files_root=repo_root,
    )

    agent._copy_context_files(agent_cwd)

    copied = agent_cwd / "a" / "b" / "c" / "deep.txt"
    assert copied.exists()
    assert copied.read_text() == "deep content"


def test_copy_context_files_no_root_logs_and_skips(tmp_path: Path, caplog) -> None:
    """When context_files_root is None and context_files is non-empty, warn and skip."""
    import logging

    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    bus = make_bus()
    tmux = make_tmux_mock()
    agent_cwd = tmp_path / "worktree"
    agent_cwd.mkdir()

    agent = ClaudeCodeAgent(
        agent_id="test-agent",
        bus=bus,
        tmux=tmux,
        context_files=["some.md"],
        context_files_root=None,
    )

    with caplog.at_level(logging.WARNING, logger="tmux_orchestrator.agents.claude_code"):
        agent._copy_context_files(agent_cwd)

    # Should warn that context_files_root is not set
    assert "context_files_root" in caplog.text or "context" in caplog.text.lower()
    # No files should have been copied
    assert list(agent_cwd.iterdir()) == []


# ---------------------------------------------------------------------------
# Integration: start() calls _copy_context_files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_copies_context_files(tmp_path: Path) -> None:
    """ClaudeCodeAgent.start() must copy context_files into the worktree."""
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    bus = make_bus()
    tmux = make_tmux_mock()

    # Worktree mock: just return tmp_path directly
    wm = MagicMock()
    wm.setup = MagicMock(return_value=tmp_path)

    # Source files
    repo_root = tmp_path.parent / "repo_root"
    repo_root.mkdir(exist_ok=True)
    (repo_root / "context.md").write_text("relevant context")

    agent = ClaudeCodeAgent(
        agent_id="ctx-agent",
        bus=bus,
        tmux=tmux,
        worktree_manager=wm,
        context_files=["context.md"],
        context_files_root=repo_root,
    )

    # Patch _wait_for_ready to skip actual tmux polling
    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        await agent.start()

    # The context file should have been copied
    assert (tmp_path / "context.md").exists()
    assert (tmp_path / "context.md").read_text() == "relevant context"

    # Clean up
    await agent.stop()
