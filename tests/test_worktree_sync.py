"""Tests for WorktreeManager.sync_to_branch() and POST /agents/{id}/sync.

Covers:
- WorktreeManager.sync_to_branch() — merge/cherry-pick/rebase strategies
- WorktreeManager.is_isolated() — returns True for owned agents, False otherwise
- sync_to_branch() raises ValueError for non-isolated agents
- sync_to_branch() raises RuntimeError on git conflict
- sync_to_branch() returns expected dict shape
- sync_to_branch() returns 0 commits_synced when already up-to-date
- POST /agents/{id}/sync — 200 success with expected response fields
- POST /agents/{id}/sync — 400 for isolate=false agents
- POST /agents/{id}/sync — 404 for unknown agent
- POST /agents/{id}/sync — 409 on merge conflict
- POST /agents/{id}/sync — 422 for unknown strategy
- strategy defaults to "merge"
- response schema validation

Design reference:
- DESIGN.md §10.71 (v1.1.39 — Worktree ↔ Branch Sync)
- git-merge(1): https://git-scm.com/docs/git-merge
- git-cherry-pick(1): https://git-scm.com/docs/git-cherry-pick
- Python cherry-picker: https://github.com/python/cherry-picker
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tmux_orchestrator.infrastructure.worktree import WorktreeManager
from tmux_orchestrator.web.app import create_app


# ---------------------------------------------------------------------------
# Git repo fixture helpers
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
    """Freshly initialised git repository with one commit on master."""
    _git("init", "-b", "master", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test User", cwd=tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("hello\n")
    _git("add", "README.md", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# WorktreeManager.is_isolated() tests
# ---------------------------------------------------------------------------


def test_is_isolated_returns_true_for_owned_agent(git_repo: Path) -> None:
    """is_isolated() returns True for agents set up with isolate=True."""
    wm = WorktreeManager(git_repo)
    wm.setup("agent-a", isolate=True)
    assert wm.is_isolated("agent-a") is True


def test_is_isolated_returns_false_for_shared_agent(git_repo: Path) -> None:
    """is_isolated() returns False for agents set up with isolate=False."""
    wm = WorktreeManager(git_repo)
    wm.setup("agent-b", isolate=False)
    assert wm.is_isolated("agent-b") is False


def test_is_isolated_returns_false_for_unregistered(git_repo: Path) -> None:
    """is_isolated() returns False for agents not registered at all."""
    wm = WorktreeManager(git_repo)
    assert wm.is_isolated("ghost-agent") is False


# ---------------------------------------------------------------------------
# WorktreeManager.sync_to_branch() — unit tests with mocked subprocess
# ---------------------------------------------------------------------------


def test_sync_to_branch_raises_for_non_isolated_agent(git_repo: Path) -> None:
    """sync_to_branch() raises ValueError when agent is not isolated."""
    wm = WorktreeManager(git_repo)
    wm.setup("shared-agent", isolate=False)
    with pytest.raises(ValueError, match="does not have an isolated worktree"):
        wm.sync_to_branch("shared-agent")


def test_sync_to_branch_raises_for_unregistered_agent(git_repo: Path) -> None:
    """sync_to_branch() raises ValueError for unknown agent_id."""
    wm = WorktreeManager(git_repo)
    with pytest.raises(ValueError, match="does not have an isolated worktree"):
        wm.sync_to_branch("ghost-999")


def test_sync_to_branch_raises_for_invalid_strategy(git_repo: Path) -> None:
    """sync_to_branch() raises ValueError for unknown strategy."""
    wm = WorktreeManager(git_repo)
    wm.setup("agent-s", isolate=True)
    with pytest.raises(ValueError, match="Unknown strategy"):
        wm.sync_to_branch("agent-s", strategy="squash")


def test_sync_to_branch_returns_zero_when_uptodate(git_repo: Path) -> None:
    """sync_to_branch() returns commits_synced=0 when already up-to-date."""
    wm = WorktreeManager(git_repo)
    wm.setup("agent-uptodate", isolate=True)
    # No commits on the worktree branch yet — log will be empty
    result = wm.sync_to_branch("agent-uptodate", strategy="merge", target_branch="master")
    assert result["commits_synced"] == 0
    assert result["agent_id"] == "agent-uptodate"
    assert result["source_branch"] == "worktree/agent-uptodate"
    assert result["target_branch"] == "master"
    assert result["strategy"] == "merge"


def test_sync_to_branch_merge_result_shape(git_repo: Path) -> None:
    """sync_to_branch(strategy='merge') returns all expected keys."""
    wm = WorktreeManager(git_repo)
    path = wm.setup("agent-merge", isolate=True)

    # Create a commit on the agent's worktree branch
    new_file = path / "work.py"
    new_file.write_text("x = 1\n")
    _git("add", "work.py", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test User", cwd=path)
    _git("commit", "-m", "agent work", cwd=path)

    result = wm.sync_to_branch("agent-merge", strategy="merge", target_branch="master")

    assert result["agent_id"] == "agent-merge"
    assert result["strategy"] == "merge"
    assert result["source_branch"] == "worktree/agent-merge"
    assert result["target_branch"] == "master"
    assert result["commits_synced"] == 1
    assert isinstance(result["merge_commit"], str)
    assert len(result["merge_commit"]) == 40  # full SHA

    # Verify file is on master
    _git("checkout", "master", cwd=git_repo)
    assert (git_repo / "work.py").exists()


def test_sync_to_branch_cherry_pick_result_shape(git_repo: Path) -> None:
    """sync_to_branch(strategy='cherry-pick') returns expected result."""
    wm = WorktreeManager(git_repo)
    path = wm.setup("agent-cp", isolate=True)

    new_file = path / "cherry.py"
    new_file.write_text("y = 2\n")
    _git("add", "cherry.py", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test User", cwd=path)
    _git("commit", "-m", "cherry commit", cwd=path)

    result = wm.sync_to_branch("agent-cp", strategy="cherry-pick", target_branch="master")

    assert result["commits_synced"] == 1
    assert result["strategy"] == "cherry-pick"
    assert isinstance(result["merge_commit"], str)

    _git("checkout", "master", cwd=git_repo)
    assert (git_repo / "cherry.py").exists()


def test_sync_to_branch_rebase_result_shape(git_repo: Path) -> None:
    """sync_to_branch(strategy='rebase') returns expected result."""
    wm = WorktreeManager(git_repo)
    path = wm.setup("agent-rb", isolate=True)

    new_file = path / "rebased.py"
    new_file.write_text("z = 3\n")
    _git("add", "rebased.py", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test User", cwd=path)
    _git("commit", "-m", "rebase commit", cwd=path)

    result = wm.sync_to_branch("agent-rb", strategy="rebase", target_branch="master")

    assert result["commits_synced"] == 1
    assert result["strategy"] == "rebase"
    assert isinstance(result["merge_commit"], str)

    _git("checkout", "master", cwd=git_repo)
    assert (git_repo / "rebased.py").exists()


def test_sync_to_branch_merge_raises_on_conflict(git_repo: Path) -> None:
    """sync_to_branch() raises RuntimeError when merge conflict occurs."""
    wm = WorktreeManager(git_repo)
    path = wm.setup("agent-conflict", isolate=True)

    # Create a conflicting commit on the worktree branch
    conflict_file = path / "README.md"
    conflict_file.write_text("conflicting change from agent\n")
    _git("add", "README.md", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test User", cwd=path)
    _git("commit", "-m", "conflict commit", cwd=path)

    # Now modify the same file on master to create a conflict
    readme = git_repo / "README.md"
    readme.write_text("conflicting change from master\n")
    _git("add", "README.md", cwd=git_repo)
    _git("commit", "-m", "master conflict commit", cwd=git_repo)

    with pytest.raises(RuntimeError, match="Merge conflict|conflict"):
        wm.sync_to_branch("agent-conflict", strategy="merge", target_branch="master")


def test_sync_to_branch_custom_message(git_repo: Path) -> None:
    """sync_to_branch() uses custom message for merge commit."""
    wm = WorktreeManager(git_repo)
    path = wm.setup("agent-msg", isolate=True)

    new_file = path / "msg_test.py"
    new_file.write_text("custom = True\n")
    _git("add", "msg_test.py", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test User", cwd=path)
    _git("commit", "-m", "message test commit", cwd=path)

    wm.sync_to_branch(
        "agent-msg",
        strategy="merge",
        target_branch="master",
        message="custom sync message",
    )

    # Verify the merge commit message on master
    log = subprocess.run(
        ["git", "log", "--format=%s", "-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert "custom sync message" in log.stdout


# ---------------------------------------------------------------------------
# REST endpoint tests: POST /agents/{id}/sync
# ---------------------------------------------------------------------------


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _MockOrchestrator:
    """Minimal orchestrator mock for REST endpoint tests."""

    def __init__(self, *, has_wm: bool = True, agent_is_isolated: bool = True):
        self._agents: dict = {}
        self._dispatch_task = None
        self._director_pending: list = []
        self.config = MagicMock()
        self.config.memory_auto_record = False

        # Mock WorktreeManager
        if has_wm:
            self._worktree_manager = MagicMock()
            self._worktree_manager.is_isolated.return_value = agent_is_isolated
            self._worktree_manager.sync_to_branch.return_value = {
                "agent_id": "worker-1",
                "strategy": "merge",
                "source_branch": "worktree/worker-1",
                "target_branch": "master",
                "commits_synced": 3,
                "merge_commit": "abc123def456abc123def456abc123def456abc1",
            }
        else:
            self._worktree_manager = None

    def list_agents(self) -> list:
        return []

    def list_tasks(self) -> list:
        return []

    def get_agent(self, agent_id: str):
        return self._agents.get(agent_id)

    def get_director(self):
        return None

    def flush_director_pending(self) -> list:
        return []

    def list_dlq(self) -> list:
        return []

    @property
    def is_paused(self) -> bool:
        return False


_API_KEY = "sync-test-key-abc"


@pytest.fixture
def mock_orch_with_isolated_agent():
    orch = _MockOrchestrator(has_wm=True, agent_is_isolated=True)
    orch._agents["worker-1"] = MagicMock()
    return orch


@pytest.fixture
def mock_orch_with_shared_agent():
    orch = _MockOrchestrator(has_wm=True, agent_is_isolated=False)
    orch._agents["worker-shared"] = MagicMock()
    return orch


@pytest.fixture
def mock_orch_no_wm():
    orch = _MockOrchestrator(has_wm=False)
    orch._agents["worker-1"] = MagicMock()
    return orch


@pytest.fixture
def app_sync(mock_orch_with_isolated_agent):
    return create_app(mock_orch_with_isolated_agent, _MockHub(), api_key=_API_KEY)


@pytest.fixture
async def client_sync(app_sync):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_sync),
        base_url="http://localhost",
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_sync_endpoint_returns_200_with_expected_fields(
    client_sync,
) -> None:
    """POST /agents/{id}/sync returns 200 with all expected response fields."""
    r = await client_sync.post(
        "/agents/worker-1/sync",
        json={"strategy": "merge", "target_branch": "master"},
        headers={"X-API-Key": _API_KEY},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["agent_id"] == "worker-1"
    assert data["strategy"] == "merge"
    assert data["source_branch"] == "worktree/worker-1"
    assert data["target_branch"] == "master"
    assert data["commits_synced"] == 3
    assert data["merge_commit"] == "abc123def456abc123def456abc123def456abc1"


@pytest.mark.asyncio
async def test_sync_endpoint_default_strategy_is_merge(
    client_sync,
) -> None:
    """POST /agents/{id}/sync defaults strategy to 'merge'."""
    r = await client_sync.post(
        "/agents/worker-1/sync",
        json={},  # no strategy specified
        headers={"X-API-Key": _API_KEY},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["strategy"] == "merge"


@pytest.mark.asyncio
async def test_sync_endpoint_default_target_is_master(
    client_sync,
) -> None:
    """POST /agents/{id}/sync defaults target_branch to 'master'."""
    r = await client_sync.post(
        "/agents/worker-1/sync",
        json={},
        headers={"X-API-Key": _API_KEY},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["target_branch"] == "master"


@pytest.mark.asyncio
async def test_sync_endpoint_404_for_unknown_agent(app_sync) -> None:
    """POST /agents/{id}/sync returns 404 for an unknown agent."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_sync),
        base_url="http://localhost",
    ) as c:
        r = await c.post(
            "/agents/ghost-999/sync",
            json={"strategy": "merge"},
            headers={"X-API-Key": _API_KEY},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_sync_endpoint_400_for_shared_agent() -> None:
    """POST /agents/{id}/sync returns 400 for isolate=false agents."""
    orch = _MockOrchestrator(has_wm=True, agent_is_isolated=False)
    orch._agents["worker-shared"] = MagicMock()
    app = create_app(orch, _MockHub(), api_key=_API_KEY)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        r = await c.post(
            "/agents/worker-shared/sync",
            json={"strategy": "merge"},
            headers={"X-API-Key": _API_KEY},
        )
    assert r.status_code == 400
    assert "isolate=false" in r.json()["detail"]


@pytest.mark.asyncio
async def test_sync_endpoint_400_when_no_worktree_manager() -> None:
    """POST /agents/{id}/sync returns 400 when no WorktreeManager is configured."""
    orch = _MockOrchestrator(has_wm=False)
    orch._agents["worker-1"] = MagicMock()
    app = create_app(orch, _MockHub(), api_key=_API_KEY)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        r = await c.post(
            "/agents/worker-1/sync",
            json={"strategy": "merge"},
            headers={"X-API-Key": _API_KEY},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_sync_endpoint_409_on_merge_conflict(mock_orch_with_isolated_agent) -> None:
    """POST /agents/{id}/sync returns 409 when WorktreeManager raises RuntimeError."""
    mock_orch_with_isolated_agent._worktree_manager.sync_to_branch.side_effect = (
        RuntimeError("Merge conflict in README.md")
    )
    app = create_app(mock_orch_with_isolated_agent, _MockHub(), api_key=_API_KEY)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        r = await c.post(
            "/agents/worker-1/sync",
            json={"strategy": "merge"},
            headers={"X-API-Key": _API_KEY},
        )
    assert r.status_code == 409
    assert "Merge conflict" in r.json()["detail"]


@pytest.mark.asyncio
async def test_sync_endpoint_422_for_unknown_strategy(
    client_sync,
) -> None:
    """POST /agents/{id}/sync returns 422 for an unknown strategy."""
    r = await client_sync.post(
        "/agents/worker-1/sync",
        json={"strategy": "squash"},
        headers={"X-API-Key": _API_KEY},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_sync_endpoint_requires_auth(app_sync) -> None:
    """POST /agents/{id}/sync requires API key authentication."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_sync),
        base_url="http://localhost",
    ) as c:
        r = await c.post(
            "/agents/worker-1/sync",
            json={"strategy": "merge"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_sync_endpoint_cherry_pick_strategy(
    mock_orch_with_isolated_agent,
) -> None:
    """POST /agents/{id}/sync accepts 'cherry-pick' strategy."""
    mock_orch_with_isolated_agent._worktree_manager.sync_to_branch.return_value = {
        "agent_id": "worker-1",
        "strategy": "cherry-pick",
        "source_branch": "worktree/worker-1",
        "target_branch": "master",
        "commits_synced": 2,
        "merge_commit": "def456" + "0" * 34,
    }
    app = create_app(mock_orch_with_isolated_agent, _MockHub(), api_key=_API_KEY)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        r = await c.post(
            "/agents/worker-1/sync",
            json={"strategy": "cherry-pick"},
            headers={"X-API-Key": _API_KEY},
        )
    assert r.status_code == 200
    assert r.json()["strategy"] == "cherry-pick"


@pytest.mark.asyncio
async def test_sync_endpoint_rebase_strategy(
    mock_orch_with_isolated_agent,
) -> None:
    """POST /agents/{id}/sync accepts 'rebase' strategy."""
    mock_orch_with_isolated_agent._worktree_manager.sync_to_branch.return_value = {
        "agent_id": "worker-1",
        "strategy": "rebase",
        "source_branch": "worktree/worker-1",
        "target_branch": "develop",
        "commits_synced": 1,
        "merge_commit": "aabbcc" + "0" * 34,
    }
    app = create_app(mock_orch_with_isolated_agent, _MockHub(), api_key=_API_KEY)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        r = await c.post(
            "/agents/worker-1/sync",
            json={"strategy": "rebase", "target_branch": "develop"},
            headers={"X-API-Key": _API_KEY},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["strategy"] == "rebase"
    assert data["target_branch"] == "develop"


@pytest.mark.asyncio
async def test_sync_endpoint_zero_commits_synced(
    mock_orch_with_isolated_agent,
) -> None:
    """POST /agents/{id}/sync returns 200 even when commits_synced=0 (already up-to-date)."""
    mock_orch_with_isolated_agent._worktree_manager.sync_to_branch.return_value = {
        "agent_id": "worker-1",
        "strategy": "merge",
        "source_branch": "worktree/worker-1",
        "target_branch": "master",
        "commits_synced": 0,
        "merge_commit": "a" * 40,
    }
    app = create_app(mock_orch_with_isolated_agent, _MockHub(), api_key=_API_KEY)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        r = await c.post(
            "/agents/worker-1/sync",
            json={},
            headers={"X-API-Key": _API_KEY},
        )
    assert r.status_code == 200
    assert r.json()["commits_synced"] == 0


@pytest.mark.asyncio
async def test_sync_endpoint_calls_worktree_manager(
    client_sync, mock_orch_with_isolated_agent
) -> None:
    """POST /agents/{id}/sync calls WorktreeManager.sync_to_branch() with correct args."""
    await client_sync.post(
        "/agents/worker-1/sync",
        json={"strategy": "cherry-pick", "target_branch": "develop", "message": "hi"},
        headers={"X-API-Key": _API_KEY},
    )
    wm = mock_orch_with_isolated_agent._worktree_manager
    wm.sync_to_branch.assert_called_once_with(
        "worker-1",
        strategy="cherry-pick",
        target_branch="develop",
        message="hi",
    )
