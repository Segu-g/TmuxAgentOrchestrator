"""Tests for branch artifact persistence — v1.2.6.

Feature B: keep_branch_on_stop flag — worktree filesystem removed but git branch
preserved so successor phases and post-mortem inspection can access committed artifacts.

Design reference: DESIGN.md §10.82 — v1.2.6 Branch Artifact Persistence
Research:
- git-worktree(1): https://git-scm.com/docs/git-worktree
  "git worktree remove" deletes the linked working tree but NOT the branch.
- "Using Git Worktrees for Multi-Feature Development with AI Agents"
  https://www.nrmitchi.com/2025/10/using-git-worktrees-for-multi-feature-development-with-ai-agents/
- "Mastering Git Worktree" — DataCamp (2025)
  https://www.datacamp.com/tutorial/git-worktree-tutorial
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from tmux_orchestrator.application.config import AgentConfig, AgentRole, OrchestratorConfig
from tmux_orchestrator.application.bus import Bus
from tmux_orchestrator.application.orchestrator import Orchestrator
from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def make_bus():
    return Bus()


def make_worktree_manager_mock():
    wm = MagicMock()
    wm.setup = MagicMock(return_value=Path("/fake/worktree"))
    wm.teardown = MagicMock()
    wm.keep_branch = MagicMock()
    wm.is_isolated = MagicMock(return_value=True)
    return wm


def make_agent_config(agent_id="worker", **kwargs) -> AgentConfig:
    defaults = dict(
        id=agent_id,
        type="claude_code",
        isolate=True,
        system_prompt="You are a worker.",
        tags=[],
    )
    defaults.update(kwargs)
    return AgentConfig(**defaults)


def make_orch_config(agents=None, **kwargs) -> OrchestratorConfig:
    defaults = dict(session_name="test", task_timeout=30, watchdog_poll=999)
    defaults.update(kwargs)
    if agents is not None:
        defaults["agents"] = agents
    return OrchestratorConfig(**defaults)


# ---------------------------------------------------------------------------
# Test 1: AgentConfig.keep_branch_on_stop defaults to False
# ---------------------------------------------------------------------------


def test_agent_config_keep_branch_on_stop_defaults_to_false():
    """AgentConfig.keep_branch_on_stop must default to False."""
    cfg = AgentConfig(id="w1", type="claude_code")
    assert cfg.keep_branch_on_stop is False


# ---------------------------------------------------------------------------
# Test 2: AgentConfig.keep_branch_on_stop can be set to True
# ---------------------------------------------------------------------------


def test_agent_config_keep_branch_on_stop_can_be_set():
    """AgentConfig.keep_branch_on_stop can be set to True."""
    cfg = AgentConfig(id="w1", type="claude_code", keep_branch_on_stop=True)
    assert cfg.keep_branch_on_stop is True


# ---------------------------------------------------------------------------
# Test 3: When keep_branch_on_stop=False, _teardown_worktree calls teardown()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teardown_worktree_calls_teardown_when_flag_false():
    """When _keep_branch_on_stop=False, _teardown_worktree calls WorktreeManager.teardown."""
    bus = make_bus()
    tmux = make_tmux_mock()
    wm = make_worktree_manager_mock()

    agent = ClaudeCodeAgent(
        agent_id="worker-test",
        bus=bus,
        tmux=tmux,
        worktree_manager=wm,
        isolate=True,
        keep_branch_on_stop=False,
    )
    agent.worktree_path = Path("/fake/worktree")

    await agent._teardown_worktree()

    wm.teardown.assert_called_once_with(
        "worker-test",
        merge_to_base=False,
        merge_target=None,
    )
    wm.keep_branch.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: When keep_branch_on_stop=True, _teardown_worktree calls keep_branch()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teardown_worktree_calls_keep_branch_when_flag_true():
    """When _keep_branch_on_stop=True, _teardown_worktree calls WorktreeManager.keep_branch."""
    bus = make_bus()
    tmux = make_tmux_mock()
    wm = make_worktree_manager_mock()

    agent = ClaudeCodeAgent(
        agent_id="worker-kb",
        bus=bus,
        tmux=tmux,
        worktree_manager=wm,
        isolate=True,
        keep_branch_on_stop=True,
    )
    agent.worktree_path = Path("/fake/worktree")

    await agent._teardown_worktree()

    wm.keep_branch.assert_called_once_with("worker-kb")
    wm.teardown.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: _teardown_worktree is a no-op when worktree_path is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teardown_worktree_noop_when_no_path():
    """_teardown_worktree is a no-op when worktree_path is None."""
    bus = make_bus()
    tmux = make_tmux_mock()
    wm = make_worktree_manager_mock()

    agent = ClaudeCodeAgent(
        agent_id="worker-noop",
        bus=bus,
        tmux=tmux,
        worktree_manager=wm,
        isolate=True,
        keep_branch_on_stop=True,
    )
    agent.worktree_path = None  # explicitly None

    await agent._teardown_worktree()

    wm.keep_branch.assert_not_called()
    wm.teardown.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: worktree_path is set to None after _teardown_worktree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teardown_worktree_clears_worktree_path():
    """After _teardown_worktree, worktree_path is set to None regardless of strategy."""
    bus = make_bus()
    tmux = make_tmux_mock()
    wm = make_worktree_manager_mock()

    agent = ClaudeCodeAgent(
        agent_id="worker-clear",
        bus=bus,
        tmux=tmux,
        worktree_manager=wm,
        isolate=True,
        keep_branch_on_stop=True,
    )
    agent.worktree_path = Path("/fake/worktree")

    await agent._teardown_worktree()

    assert agent.worktree_path is None


# ---------------------------------------------------------------------------
# Test 7: spawn_ephemeral_agent sets _keep_branch_on_stop=True for isolated agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_ephemeral_agent_sets_keep_branch_for_isolated():
    """spawn_ephemeral_agent auto-enables keep_branch_on_stop for isolated template configs."""
    bus = make_bus()
    tmux = make_tmux_mock()
    wm = make_worktree_manager_mock()

    template_cfg = make_agent_config("worker", isolate=True)
    orch_cfg = make_orch_config(agents=[template_cfg])
    orch = Orchestrator(bus=bus, tmux=tmux, config=orch_cfg, worktree_manager=wm)

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent.start", new_callable=AsyncMock):
        eph_id = await orch.spawn_ephemeral_agent("worker")

    eph_agent = orch.registry.get(eph_id)
    assert eph_agent is not None
    assert eph_agent._keep_branch_on_stop is True


# ---------------------------------------------------------------------------
# Test 8: spawn_ephemeral_agent non-isolated agents: keep_branch determined by config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_ephemeral_agent_non_isolated_uses_config():
    """Non-isolated ephemeral agents use template config's keep_branch_on_stop value."""
    bus = make_bus()
    tmux = make_tmux_mock()

    template_cfg = make_agent_config("worker", isolate=False, keep_branch_on_stop=False)
    orch_cfg = make_orch_config(agents=[template_cfg])
    orch = Orchestrator(bus=bus, tmux=tmux, config=orch_cfg, worktree_manager=None)

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent.start", new_callable=AsyncMock):
        eph_id = await orch.spawn_ephemeral_agent("worker")

    eph_agent = orch.registry.get(eph_id)
    assert eph_agent is not None
    # Non-isolated: isolate=False, so effective_keep_branch = False or False = False
    assert eph_agent._keep_branch_on_stop is False


# ---------------------------------------------------------------------------
# Test 9: keep_branch is called with the correct agent_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teardown_calls_keep_branch_with_correct_agent_id():
    """WorktreeManager.keep_branch is called with the agent's own ID."""
    bus = make_bus()
    tmux = make_tmux_mock()
    wm = make_worktree_manager_mock()

    agent = ClaudeCodeAgent(
        agent_id="specific-agent-id",
        bus=bus,
        tmux=tmux,
        worktree_manager=wm,
        isolate=True,
        keep_branch_on_stop=True,
    )
    agent.worktree_path = Path("/fake/worktree")

    await agent._teardown_worktree()

    wm.keep_branch.assert_called_once_with("specific-agent-id")


# ---------------------------------------------------------------------------
# Test 10: WorktreeManager.keep_branch removes worktree but keeps branch
# ---------------------------------------------------------------------------


def test_worktree_manager_keep_branch_removes_worktree_keeps_branch(tmp_path):
    """WorktreeManager.keep_branch removes the worktree directory but not the branch."""
    import subprocess
    from tmux_orchestrator.infrastructure.worktree import WorktreeManager

    # Create a git repo
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "file.txt").write_text("initial")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

    wm = WorktreeManager(tmp_path)
    wm.setup("agent-x", isolate=True)

    worktree_dir = tmp_path / ".worktrees" / "agent-x"
    assert worktree_dir.exists()

    wm.keep_branch("agent-x")

    # Worktree directory is removed
    assert not worktree_dir.exists()

    # Branch still exists
    result = subprocess.run(
        ["git", "branch", "--list", "worktree/agent-x"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert "worktree/agent-x" in result.stdout


# ---------------------------------------------------------------------------
# Test 11: _keep_branch_on_stop attribute exists on base Agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_base_has_keep_branch_on_stop_attribute():
    """Agent base class __init__ must set _keep_branch_on_stop to False by default."""
    bus = make_bus()
    tmux = make_tmux_mock()

    agent = ClaudeCodeAgent(
        agent_id="check-attr",
        bus=bus,
        tmux=tmux,
    )
    assert hasattr(agent, "_keep_branch_on_stop")
    assert agent._keep_branch_on_stop is False


# ---------------------------------------------------------------------------
# Test 12: ClaudeCodeAgent respects keep_branch_on_stop constructor param
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_code_agent_keep_branch_param():
    """ClaudeCodeAgent constructor sets _keep_branch_on_stop from parameter."""
    bus = make_bus()
    tmux = make_tmux_mock()

    agent_true = ClaudeCodeAgent(
        agent_id="kb-true",
        bus=bus,
        tmux=tmux,
        keep_branch_on_stop=True,
    )
    agent_false = ClaudeCodeAgent(
        agent_id="kb-false",
        bus=bus,
        tmux=tmux,
        keep_branch_on_stop=False,
    )
    assert agent_true._keep_branch_on_stop is True
    assert agent_false._keep_branch_on_stop is False
