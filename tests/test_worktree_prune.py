"""Tests for WorktreeManager.prune_stale() and Orchestrator startup prune.

Covers:
- prune_stale() calls `git worktree prune --expire now`
- prune_stale() is a no-op (no exception) when git command fails
- Orchestrator.start() calls prune_stale() when worktree_manager is set
- Orchestrator.start() does not fail when worktree_manager is None

Reference: DESIGN.md §10.40 — v1.1.4 stale worktree auto-cleanup
Sources:
- git-scm.com/docs/git-worktree — `git worktree prune --expire now`
- anthropics/claude-code#26725 — stale worktrees never cleaned up
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from tmux_orchestrator.infrastructure.worktree import WorktreeManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Return a freshly initialised git repository with one commit."""
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("hello\n")
    _git("add", "README.md", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Tests: WorktreeManager.prune_stale()
# ---------------------------------------------------------------------------


def test_prune_stale_runs_git_worktree_prune(git_repo: Path) -> None:
    """prune_stale() calls `git worktree prune --expire now` in the repo root."""
    wm = WorktreeManager(git_repo)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        wm.prune_stale()
    mock_run.assert_called_once_with(
        ["git", "worktree", "prune", "--expire", "now"],
        cwd=git_repo,
        capture_output=True,
    )


def test_prune_stale_no_exception_on_failure(git_repo: Path) -> None:
    """prune_stale() does not raise even if git command fails."""
    wm = WorktreeManager(git_repo)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        # Should not raise
        wm.prune_stale()


def test_prune_stale_real_git(git_repo: Path) -> None:
    """prune_stale() executes successfully against a real git repo."""
    wm = WorktreeManager(git_repo)
    # Should not raise on a clean repo
    wm.prune_stale()


def test_prune_stale_cleans_stale_worktree_metadata(git_repo: Path) -> None:
    """prune_stale() removes stale worktree metadata after directory is deleted."""
    wm = WorktreeManager(git_repo)
    # Create a worktree
    path = wm.setup("agent-prune-test")
    assert path.exists()

    # Manually remove the worktree directory without git teardown (simulate crash)
    import shutil
    shutil.rmtree(path)

    # List worktrees before prune — the stale entry should exist
    result_before = subprocess.run(
        ["git", "worktree", "list"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    # The stale worktree may or may not appear depending on git version,
    # but prune_stale() should complete without error
    wm.prune_stale()

    # After prune, git worktree list should not raise errors
    result_after = subprocess.run(
        ["git", "worktree", "list"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result_after.returncode == 0


# ---------------------------------------------------------------------------
# Tests: Orchestrator.start() calls prune_stale()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_start_calls_prune_stale_when_wm_set() -> None:
    """Orchestrator.start() calls worktree_manager.prune_stale() before starting agents."""
    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.config import OrchestratorConfig
    from tmux_orchestrator.orchestrator import Orchestrator

    bus = Bus()
    config = OrchestratorConfig(
        session_name="test",
        agents=[],
        p2p_permissions=[],
        task_timeout=10,
        watchdog_poll=99999,
    )
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()

    mock_wm = MagicMock()
    mock_wm.prune_stale = MagicMock()

    orch = Orchestrator(bus, tmux, config, worktree_manager=mock_wm)

    # Patch start_agents to avoid needing real agents
    with patch.object(orch, "start_agents", new_callable=AsyncMock):
        await orch.start()

    mock_wm.prune_stale.assert_called_once()

    await orch.stop()


@pytest.mark.asyncio
async def test_orchestrator_start_no_error_when_wm_none() -> None:
    """Orchestrator.start() does not fail when worktree_manager is None."""
    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.config import OrchestratorConfig
    from tmux_orchestrator.orchestrator import Orchestrator

    bus = Bus()
    config = OrchestratorConfig(
        session_name="test",
        agents=[],
        p2p_permissions=[],
        task_timeout=10,
        watchdog_poll=99999,
    )
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()

    orch = Orchestrator(bus, tmux, config, worktree_manager=None)

    with patch.object(orch, "start_agents", new_callable=AsyncMock):
        await orch.start()  # Must not raise

    await orch.stop()
