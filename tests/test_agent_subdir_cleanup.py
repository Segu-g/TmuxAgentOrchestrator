"""Tests for .agent/{agent_id}/ subdir cleanup on agent stop (v1.1.37).

When an ``isolate: false`` agent stops, its ``.agent/{agent_id}/`` subdirectory
should be deleted automatically (``cleanup_subdir=True``, the default).

When ``cleanup_subdir=False`` the subdir is preserved for post-mortem inspection.

For ``isolate: true`` agents no ``.agent/`` directory is created, so cleanup
is a no-op and the worktree lifecycle is handled by ``WorktreeManager.teardown()``.

Design reference: DESIGN.md §10.69 (v1.1.37 — .agent/{id}/ cleanup on stop)
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
    tmux.unwatch_pane = MagicMock()
    return tmux


def make_non_isolated_agent(
    agent_id: str,
    cwd: Path,
    *,
    cleanup_subdir: bool = True,
    web_base_url: str = "",
) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(
        agent_id=agent_id,
        bus=make_bus(),
        tmux=make_tmux_mock(),
        isolate=False,
        cwd_override=cwd,
        web_base_url=web_base_url,
        cleanup_subdir=cleanup_subdir,
    )


# ---------------------------------------------------------------------------
# _cleanup_agent_subdir unit tests (no start() required)
# ---------------------------------------------------------------------------


def test_cleanup_subdir_deletes_agent_dir(tmp_path: Path) -> None:
    """cleanup_subdir=True (default): subdir should be deleted after stop."""
    agent = make_non_isolated_agent("worker-del", tmp_path)
    # Simulate what start() would set
    subdir = tmp_path / ".agent" / "worker-del"
    subdir.mkdir(parents=True)
    (subdir / "NOTES.md").write_text("notes")
    agent._cwd = subdir

    agent._cleanup_agent_subdir()

    assert not subdir.exists(), "Subdir must be deleted when cleanup_subdir=True"


def test_cleanup_subdir_false_preserves_agent_dir(tmp_path: Path) -> None:
    """cleanup_subdir=False: subdir must be preserved."""
    agent = make_non_isolated_agent("worker-keep", tmp_path, cleanup_subdir=False)
    subdir = tmp_path / ".agent" / "worker-keep"
    subdir.mkdir(parents=True)
    (subdir / "NOTES.md").write_text("notes")
    agent._cwd = subdir

    agent._cleanup_agent_subdir()

    assert subdir.exists(), "Subdir must be preserved when cleanup_subdir=False"


def test_cleanup_subdir_noop_when_cwd_is_none(tmp_path: Path) -> None:
    """No error when _cwd is None (agent never fully started)."""
    agent = make_non_isolated_agent("worker-none", tmp_path)
    agent._cwd = None  # simulates start() failure before cwd was set

    # Must not raise
    agent._cleanup_agent_subdir()


def test_cleanup_subdir_noop_for_isolated_agent(tmp_path: Path) -> None:
    """isolate=True agents: _cleanup_agent_subdir must be a no-op."""
    wm = MagicMock()
    wm.setup = MagicMock(return_value=tmp_path)
    wm.teardown = MagicMock()
    agent = ClaudeCodeAgent(
        agent_id="worker-iso",
        bus=make_bus(),
        tmux=make_tmux_mock(),
        isolate=True,
        worktree_manager=wm,
        cleanup_subdir=True,  # set True but should be ignored for isolated agents
    )
    # Simulate a cwd that happens to be inside .agent/ (should not happen in
    # practice but tests the guard)
    fake_subdir = tmp_path / ".agent" / "worker-iso"
    fake_subdir.mkdir(parents=True)
    agent._cwd = fake_subdir

    agent._cleanup_agent_subdir()

    # Must not have deleted the directory (isolate=True guard fires first)
    assert fake_subdir.exists(), "Isolated agent: cleanup_subdir must be no-op"


def test_cleanup_subdir_noop_when_dir_already_gone(tmp_path: Path) -> None:
    """No error when the subdir was already removed (idempotent)."""
    agent = make_non_isolated_agent("worker-gone", tmp_path)
    subdir = tmp_path / ".agent" / "worker-gone"
    # Do NOT create the directory — simulate it already being gone
    agent._cwd = subdir

    # Must not raise (shutil.rmtree(ignore_errors=True))
    agent._cleanup_agent_subdir()


def test_cleanup_subdir_noop_when_cwd_not_in_agent_dir(tmp_path: Path) -> None:
    """Safety guard: if _cwd.parent.name != '.agent', skip to avoid data loss."""
    agent = make_non_isolated_agent("worker-safe", tmp_path)
    # Simulate a misconfigured cwd that is NOT inside .agent/
    agent._cwd = tmp_path / "some-other-dir" / "worker-safe"
    (agent._cwd).mkdir(parents=True)

    agent._cleanup_agent_subdir()

    # Must NOT have deleted the dir (safety guard)
    assert agent._cwd.exists(), "Safety guard: must not delete when cwd is not in .agent/"


# ---------------------------------------------------------------------------
# Integration: stop() triggers cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_cleans_up_agent_subdir(tmp_path: Path) -> None:
    """After stop(), .agent/{id}/ must be deleted when cleanup_subdir=True."""
    agent = make_non_isolated_agent("worker-stop", tmp_path, cleanup_subdir=True)

    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        with patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=tmp_path):
            with patch("tmux_orchestrator.agents.claude_code.pre_trust_worktree"):
                await agent.start()

    subdir = tmp_path / ".agent" / "worker-stop"
    assert subdir.exists(), "Subdir must exist after start()"

    await agent.stop()

    assert not subdir.exists(), "Subdir must be deleted after stop() when cleanup_subdir=True"


@pytest.mark.asyncio
async def test_stop_preserves_agent_subdir_when_cleanup_disabled(tmp_path: Path) -> None:
    """After stop(), .agent/{id}/ must be preserved when cleanup_subdir=False."""
    agent = make_non_isolated_agent("worker-keep2", tmp_path, cleanup_subdir=False)

    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        with patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=tmp_path):
            with patch("tmux_orchestrator.agents.claude_code.pre_trust_worktree"):
                await agent.start()

    subdir = tmp_path / ".agent" / "worker-keep2"
    assert subdir.exists(), "Subdir must exist after start()"

    await agent.stop()

    assert subdir.exists(), "Subdir must be preserved after stop() when cleanup_subdir=False"


@pytest.mark.asyncio
async def test_stop_no_agent_subdir_for_isolated_agent(tmp_path: Path) -> None:
    """isolate=True agent stop() must not delete anything under .agent/."""
    worktree = tmp_path / "worktrees" / "worker-iso2"
    worktree.mkdir(parents=True)

    wm = MagicMock()
    wm.setup = MagicMock(return_value=worktree)
    wm.teardown = MagicMock()

    agent = ClaudeCodeAgent(
        agent_id="worker-iso2",
        bus=make_bus(),
        tmux=make_tmux_mock(),
        isolate=True,
        worktree_manager=wm,
        cleanup_subdir=True,
    )

    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        with patch("tmux_orchestrator.agents.claude_code.pre_trust_worktree"):
            await agent.start()

    # No .agent/ dir should be created by isolated agents
    assert not (worktree / ".agent").exists()
    assert not (tmp_path / ".agent").exists()

    await agent.stop()

    # Worktree itself should still be around (wm.teardown is mocked — it doesn't remove it)
    assert worktree.exists()
