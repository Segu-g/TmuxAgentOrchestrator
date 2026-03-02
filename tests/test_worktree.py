"""Unit tests for WorktreeManager using a real (temporary) git repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tmux_orchestrator.worktree import WorktreeManager


# ---------------------------------------------------------------------------
# Helpers / fixtures
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
# Tests
# ---------------------------------------------------------------------------


def test_setup_creates_worktree(git_repo: Path) -> None:
    wm = WorktreeManager(git_repo)
    path = wm.setup("agent-1")

    assert path.exists(), "worktree directory should exist after setup"
    assert path == git_repo / ".worktrees" / "agent-1"


def test_setup_isolate_false_returns_repo_root(git_repo: Path) -> None:
    wm = WorktreeManager(git_repo)
    path = wm.setup("agent-2", isolate=False)

    assert path == git_repo
    # No .worktrees directory should be created for shared agents.
    assert not (git_repo / ".worktrees" / "agent-2").exists()


def test_teardown_removes_worktree_and_branch(git_repo: Path) -> None:
    wm = WorktreeManager(git_repo)
    path = wm.setup("agent-3")
    assert path.exists()

    wm.teardown("agent-3")

    assert not path.exists(), "worktree directory should be removed after teardown"
    # Branch should be deleted too.
    result = subprocess.run(
        ["git", "branch", "--list", "worktree/agent-3"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "", "worktree branch should be deleted"


def test_teardown_shared_is_noop(git_repo: Path) -> None:
    wm = WorktreeManager(git_repo)
    wm.setup("agent-4", isolate=False)

    # Should not raise and should not touch any git state.
    branches_before = subprocess.run(
        ["git", "branch", "--list"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    wm.teardown("agent-4")

    branches_after = subprocess.run(
        ["git", "branch", "--list"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert branches_before == branches_after, "shared teardown must not modify branches"


def test_gitignore_entry_added(git_repo: Path) -> None:
    assert not (git_repo / ".gitignore").exists()
    WorktreeManager(git_repo)
    gitignore = git_repo / ".gitignore"
    assert gitignore.exists()
    assert ".worktrees/" in gitignore.read_text().splitlines()


def test_gitignore_not_duplicated(git_repo: Path) -> None:
    WorktreeManager(git_repo)
    WorktreeManager(git_repo)  # second init on same repo
    gitignore = git_repo / ".gitignore"
    lines = gitignore.read_text().splitlines()
    assert lines.count(".worktrees/") == 1, "entry should not be duplicated"


def test_not_in_git_repo_raises(tmp_path: Path) -> None:
    # tmp_path has no .git directory.
    with pytest.raises(RuntimeError, match="Not inside a git repository"):
        WorktreeManager(tmp_path)


def test_worktree_path_before_setup_returns_none(git_repo: Path) -> None:
    wm = WorktreeManager(git_repo)
    assert wm.worktree_path("nonexistent-agent") is None


def test_duplicate_setup_cleaned_and_recreated(git_repo: Path) -> None:
    wm = WorktreeManager(git_repo)
    path1 = wm.setup("agent-5")
    assert path1.exists()

    # Second setup should clean up the first and create a fresh one.
    path2 = wm.setup("agent-5")
    assert path2 == path1
    assert path2.exists()

    wm.teardown("agent-5")
    assert not path2.exists()
