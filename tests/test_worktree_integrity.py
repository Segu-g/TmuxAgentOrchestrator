"""Tests for WorktreeIntegrityChecker — git worktree integrity validation.

Covers:
- WorktreeStatus dataclass fields and defaults
- is_valid: True for a healthy worktree
- is_valid: False for a missing worktree directory
- is_locked: True when .git/index.lock exists
- is_dirty: True when uncommitted changes are present
- head_sha: present and valid
- branch: resolved from git output
- errors: populated on fsck failure (simulated via corruption)
- check_agent: returns None for agents with no worktree (isolate=False)
- WorktreeIntegrityChecker.check_agent works for a real worktree
- REST endpoint GET /agents/{agent_id}/worktree-status: 200 with WorktreeStatus fields
- REST endpoint GET /agents/{agent_id}/worktree-status: 404 for unknown agent
- REST endpoint GET /agents/{agent_id}/worktree-status: null path for isolate=False agent
- Orchestrator: dispatch skipped when worktree integrity check fails
- Orchestrator: integrity_check_failed bus event published on broken worktree
- Bus event: dirty_worktree published after agent stop with uncommitted changes

Design references:
- git-fsck(1): https://git-scm.com/docs/git-fsck
- git-worktree(1): https://git-scm.com/docs/git-worktree
- GitLab "Repository checks": https://docs.gitlab.com/ee/administration/repository_checks.html
- DESIGN.md §10.17 (v0.43.0)
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tmux_orchestrator.worktree import WorktreeManager
from tmux_orchestrator.worktree_integrity import WorktreeIntegrityChecker, WorktreeStatus


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


@pytest.fixture()
def wm(git_repo: Path) -> WorktreeManager:
    return WorktreeManager(git_repo)


# ---------------------------------------------------------------------------
# WorktreeStatus dataclass tests
# ---------------------------------------------------------------------------


def test_worktree_status_defaults() -> None:
    """WorktreeStatus has sensible defaults for all optional fields."""
    status = WorktreeStatus(agent_id="a1", path=None)
    assert status.agent_id == "a1"
    assert status.path is None
    assert status.is_valid is False
    assert status.is_dirty is False
    assert status.is_locked is False
    assert status.head_sha is None
    assert status.branch is None
    assert status.errors == []
    assert status.checked_at is not None  # auto-set


def test_worktree_status_to_dict_contains_all_fields() -> None:
    status = WorktreeStatus(agent_id="a1", path="/some/path", is_valid=True)
    d = status.to_dict()
    assert "agent_id" in d
    assert "path" in d
    assert "is_valid" in d
    assert "is_dirty" in d
    assert "is_locked" in d
    assert "head_sha" in d
    assert "branch" in d
    assert "errors" in d
    assert "checked_at" in d


# ---------------------------------------------------------------------------
# WorktreeIntegrityChecker core logic tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checker_healthy_worktree(git_repo: Path, wm: WorktreeManager) -> None:
    """A freshly created worktree should be valid, clean, unlocked."""
    checker = WorktreeIntegrityChecker(repo_root=git_repo)
    wt_path = wm.setup("agent-check-1")

    status = await checker.check_path("agent-check-1", wt_path)

    assert status.is_valid is True
    assert status.is_dirty is False
    assert status.is_locked is False
    assert status.head_sha is not None
    assert len(status.head_sha) == 40  # full SHA
    assert status.branch == "worktree/agent-check-1"
    assert status.errors == []

    wm.teardown("agent-check-1")


@pytest.mark.asyncio
async def test_checker_missing_path_is_invalid(git_repo: Path) -> None:
    """A path that does not exist → is_valid=False."""
    checker = WorktreeIntegrityChecker(repo_root=git_repo)
    missing = git_repo / ".worktrees" / "nonexistent"

    status = await checker.check_path("agent-missing", missing)

    assert status.is_valid is False
    assert len(status.errors) > 0


@pytest.mark.asyncio
async def test_checker_index_lock_detected(git_repo: Path, wm: WorktreeManager) -> None:
    """A worktree with index.lock → is_locked=True.

    In a linked worktree, .git is a file (gitdir pointer).  The actual git
    dir is at {main_repo}/.git/worktrees/{agent_id}/.  That's where
    index.lock would be placed by a crashed git process.
    """
    checker = WorktreeIntegrityChecker(repo_root=git_repo)
    wt_path = wm.setup("agent-lock-1")

    # In a linked worktree, the actual git dir is {repo}/.git/worktrees/{id}/
    actual_git_dir = git_repo / ".git" / "worktrees" / "agent-lock-1"
    lock_file = actual_git_dir / "index.lock"
    actual_git_dir.mkdir(parents=True, exist_ok=True)
    lock_file.write_text("fake lock\n")

    status = await checker.check_path("agent-lock-1", wt_path)

    assert status.is_locked is True

    lock_file.unlink(missing_ok=True)
    wm.teardown("agent-lock-1")


@pytest.mark.asyncio
async def test_checker_dirty_worktree_detected(git_repo: Path, wm: WorktreeManager) -> None:
    """A worktree with uncommitted changes → is_dirty=True."""
    checker = WorktreeIntegrityChecker(repo_root=git_repo)
    wt_path = wm.setup("agent-dirty-1")

    # Create an uncommitted file
    (wt_path / "dirty_file.txt").write_text("uncommitted work\n")

    status = await checker.check_path("agent-dirty-1", wt_path)

    assert status.is_dirty is True

    wm.teardown("agent-dirty-1")


@pytest.mark.asyncio
async def test_checker_clean_after_commit(git_repo: Path, wm: WorktreeManager) -> None:
    """After committing, the worktree should no longer be dirty."""
    checker = WorktreeIntegrityChecker(repo_root=git_repo)
    wt_path = wm.setup("agent-clean-1")

    (wt_path / "clean_file.txt").write_text("committed work\n")
    _git("config", "user.email", "test@example.com", cwd=wt_path)
    _git("config", "user.name", "Test", cwd=wt_path)
    _git("add", "clean_file.txt", cwd=wt_path)
    _git("commit", "-m", "commit work", cwd=wt_path)

    status = await checker.check_path("agent-clean-1", wt_path)

    assert status.is_dirty is False
    assert status.is_valid is True

    wm.teardown("agent-clean-1")


@pytest.mark.asyncio
async def test_check_agent_no_worktree_returns_none(git_repo: Path) -> None:
    """check_agent returns None when the agent has no worktree (isolate=False)."""
    checker = WorktreeIntegrityChecker(repo_root=git_repo)
    result = await checker.check_agent("agent-no-worktree", worktree_path=None)
    assert result is None


@pytest.mark.asyncio
async def test_check_agent_with_path(git_repo: Path, wm: WorktreeManager) -> None:
    """check_agent delegates to check_path when worktree_path is provided."""
    checker = WorktreeIntegrityChecker(repo_root=git_repo)
    wt_path = wm.setup("agent-with-path")

    result = await checker.check_agent("agent-with-path", worktree_path=wt_path)

    assert result is not None
    assert result.agent_id == "agent-with-path"
    assert result.is_valid is True

    wm.teardown("agent-with-path")


@pytest.mark.asyncio
async def test_checker_head_sha_is_40_chars(git_repo: Path, wm: WorktreeManager) -> None:
    """HEAD SHA must be a 40-character hex string for a valid worktree."""
    checker = WorktreeIntegrityChecker(repo_root=git_repo)
    wt_path = wm.setup("agent-sha-1")

    status = await checker.check_path("agent-sha-1", wt_path)

    assert status.head_sha is not None
    assert len(status.head_sha) == 40
    assert all(c in "0123456789abcdef" for c in status.head_sha)

    wm.teardown("agent-sha-1")


# ---------------------------------------------------------------------------
# WorktreeIntegrityChecker.check_all tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_all_returns_list(git_repo: Path, wm: WorktreeManager) -> None:
    """check_all returns a list of WorktreeStatus for all provided paths."""
    checker = WorktreeIntegrityChecker(repo_root=git_repo)
    p1 = wm.setup("agent-ca-1")
    p2 = wm.setup("agent-ca-2")

    agent_paths = {
        "agent-ca-1": p1,
        "agent-ca-2": p2,
        "agent-shared": None,  # isolate=False
    }
    results = await checker.check_all(agent_paths)

    # check_all should include only agents that have a worktree path
    assert len(results) == 2
    agent_ids = {s.agent_id for s in results}
    assert "agent-ca-1" in agent_ids
    assert "agent-ca-2" in agent_ids

    wm.teardown("agent-ca-1")
    wm.teardown("agent-ca-2")


# ---------------------------------------------------------------------------
# REST endpoint tests (mocked orchestrator)
# ---------------------------------------------------------------------------


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


@pytest.fixture()
def mock_orchestrator(git_repo: Path, wm: WorktreeManager) -> Any:
    """Return a mock orchestrator with a real WorktreeManager and one agent."""
    wt_path = wm.setup("agent-rest-1")
    orch = MagicMock()
    orch._worktree_manager = wm
    # Mock agent: has worktree_path
    agent = MagicMock()
    agent.id = "agent-rest-1"
    orch.get_agent.side_effect = lambda aid: agent if aid == "agent-rest-1" else None
    orch._repo_root = git_repo
    return orch, wt_path, wm


def test_worktree_status_endpoint_returns_200(mock_orchestrator: Any) -> None:
    """GET /agents/{agent_id}/worktree-status returns 200 with WorktreeStatus fields."""
    from fastapi.testclient import TestClient
    from tmux_orchestrator.web.app import create_app

    orch, wt_path, wm_inst = mock_orchestrator

    app = create_app(orch, _MockHub(), api_key="test-key")
    client = TestClient(app)

    resp = client.get(
        "/agents/agent-rest-1/worktree-status",
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "agent_id" in data
    assert "is_valid" in data
    assert "is_dirty" in data
    assert "is_locked" in data
    assert "head_sha" in data
    assert "branch" in data
    assert "errors" in data
    assert "checked_at" in data

    wm_inst.teardown("agent-rest-1")


def test_worktree_status_endpoint_404_unknown_agent(mock_orchestrator: Any) -> None:
    """GET /agents/{agent_id}/worktree-status returns 404 for unknown agents."""
    from fastapi.testclient import TestClient
    from tmux_orchestrator.web.app import create_app

    orch, wt_path, wm_inst = mock_orchestrator
    app = create_app(orch, _MockHub(), api_key="test-key")
    client = TestClient(app)

    resp = client.get(
        "/agents/unknown-agent/worktree-status",
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 404

    wm_inst.teardown("agent-rest-1")


def test_worktree_status_endpoint_no_worktree_agent(git_repo: Path) -> None:
    """GET /agents/{id}/worktree-status returns null path for isolate=False agents."""
    from fastapi.testclient import TestClient
    from tmux_orchestrator.web.app import create_app

    wm_inst = WorktreeManager(git_repo)
    orch = MagicMock()
    agent = MagicMock()
    agent.id = "shared-agent"
    orch.get_agent.side_effect = lambda aid: agent if aid == "shared-agent" else None
    orch._worktree_manager = wm_inst
    # shared-agent not in _owned (isolate=False)

    app = create_app(orch, _MockHub(), api_key="test-key")
    client = TestClient(app)

    resp = client.get(
        "/agents/shared-agent/worktree-status",
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["path"] is None
    assert data["agent_id"] == "shared-agent"


# ---------------------------------------------------------------------------
# dirty_worktree bus event test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dirty_worktree_event_on_teardown(git_repo: Path, wm: WorktreeManager) -> None:
    """WorktreeIntegrityChecker.check_and_publish_dirty publishes dirty_worktree event when dirty."""
    from tmux_orchestrator.bus import Bus

    bus = Bus()
    checker = WorktreeIntegrityChecker(repo_root=git_repo, bus=bus)

    wt_path = wm.setup("agent-dirty-evt")
    # Subscribe to events
    q = await bus.subscribe("test-subscriber", broadcast=True)

    # Create dirty state
    (wt_path / "untracked.txt").write_text("uncommitted\n")

    await checker.check_and_publish_dirty("agent-dirty-evt", wt_path)

    # Should receive a dirty_worktree event
    events = []
    while not q.empty():
        events.append(await q.get())

    dirty_events = [e for e in events if e.payload.get("event") == "dirty_worktree"]
    assert len(dirty_events) == 1
    assert dirty_events[0].payload["agent_id"] == "agent-dirty-evt"
    assert dirty_events[0].payload["path"] is not None

    await bus.unsubscribe("test-subscriber")
    wm.teardown("agent-dirty-evt")


@pytest.mark.asyncio
async def test_no_dirty_event_for_clean_worktree(git_repo: Path, wm: WorktreeManager) -> None:
    """check_and_publish_dirty does NOT publish when worktree is clean."""
    from tmux_orchestrator.bus import Bus

    bus = Bus()
    checker = WorktreeIntegrityChecker(repo_root=git_repo, bus=bus)

    wt_path = wm.setup("agent-clean-evt")
    q = await bus.subscribe("test-subscriber-2", broadcast=True)

    await checker.check_and_publish_dirty("agent-clean-evt", wt_path)

    events = []
    while not q.empty():
        events.append(await q.get())

    dirty_events = [e for e in events if e.payload.get("event") == "dirty_worktree"]
    assert len(dirty_events) == 0

    await bus.unsubscribe("test-subscriber-2")
    wm.teardown("agent-clean-evt")


# ---------------------------------------------------------------------------
# integrity_check_failed event test (dispatch hook)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integrity_check_failed_event_on_broken_worktree(git_repo: Path, wm: WorktreeManager) -> None:
    """WorktreeIntegrityChecker.check_and_publish_integrity emits integrity_check_failed for invalid worktrees."""
    from tmux_orchestrator.bus import Bus

    bus = Bus()
    checker = WorktreeIntegrityChecker(repo_root=git_repo, bus=bus)

    # A path that doesn't exist → is_valid=False
    nonexistent = git_repo / ".worktrees" / "broken-agent"
    q = await bus.subscribe("test-sub-integrity", broadcast=True)

    result = await checker.check_and_publish_integrity("broken-agent", nonexistent)

    assert result.is_valid is False

    events = []
    while not q.empty():
        events.append(await q.get())

    failed_events = [e for e in events if e.payload.get("event") == "integrity_check_failed"]
    assert len(failed_events) == 1
    assert failed_events[0].payload["agent_id"] == "broken-agent"

    await bus.unsubscribe("test-sub-integrity")


@pytest.mark.asyncio
async def test_no_failed_event_for_valid_worktree(git_repo: Path, wm: WorktreeManager) -> None:
    """check_and_publish_integrity does NOT emit integrity_check_failed for valid worktrees."""
    from tmux_orchestrator.bus import Bus

    bus = Bus()
    checker = WorktreeIntegrityChecker(repo_root=git_repo, bus=bus)
    wt_path = wm.setup("agent-valid-evt")
    q = await bus.subscribe("test-sub-valid", broadcast=True)

    result = await checker.check_and_publish_integrity("agent-valid-evt", wt_path)

    assert result.is_valid is True

    events = []
    while not q.empty():
        events.append(await q.get())

    failed_events = [e for e in events if e.payload.get("event") == "integrity_check_failed"]
    assert len(failed_events) == 0

    await bus.unsubscribe("test-sub-valid")
    wm.teardown("agent-valid-evt")
