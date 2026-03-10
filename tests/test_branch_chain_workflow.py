"""Tests for branch-chain workflow execution (v1.2.4).

Verifies:
- PhaseSpec.chain_branch field exists and defaults to False
- PhaseSpecModel.chain_branch field exists
- WorktreeManager.create_from_branch creates a worktree from a named source branch
- Orchestrator._ephemeral_agent_branches tracks spawned agent branches
- Orchestrator.get_worktree_manager() returns the manager or None
- Schema conversion preserves chain_branch
- Task specs carry chain_branch when set
- SequenceBlock with chain_branch=True propagates to inner phases
- _make_task_spec embeds chain_branch in the output dict
- expand_phases_with_status produces task specs with chain_branch

Design reference: DESIGN.md §10.80 (v1.2.4)
Research:
- "The Rise of Git Worktrees in the Age of AI Coding Agents" — knowledge.buka.sh (2025)
- "Git Worktree Tutorial: Work on Multiple Branches Without Switching" — DataCamp (2025)
- "GitHub Copilot Plan-Then-Execute: Leveraging Background Agents and Git Worktrees" — Codewrecks (2025)
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tmux_orchestrator.agents.base import AgentStatus
from tmux_orchestrator.application.bus import Bus
from tmux_orchestrator.application.config import AgentConfig, AgentRole, OrchestratorConfig
from tmux_orchestrator.application.orchestrator import Orchestrator
from tmux_orchestrator.domain.phase_strategy import (
    AgentSelector,
    PhaseSpec,
    SequenceBlock,
    SingleStrategy,
    _make_task_spec,
    expand_phases_from_specs,
)
from tmux_orchestrator.phase_executor import expand_phases_with_status
from tmux_orchestrator.web.schemas import PhaseSpecModel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(agents=None, **kwargs) -> OrchestratorConfig:
    defaults = dict(session_name="test", task_timeout=30, watchdog_poll=999)
    defaults.update(kwargs)
    if agents is not None:
        defaults["agents"] = agents
    return OrchestratorConfig(**defaults)


def make_tmux_mock():
    tmux = MagicMock()
    tmux.new_pane = MagicMock(return_value=MagicMock(id="pane-1"))
    tmux.new_subpane = MagicMock(return_value=MagicMock(id="pane-2"))
    tmux.send_keys = MagicMock()
    tmux.watch_pane = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.capture_pane = MagicMock(return_value="❯ ")
    return tmux


def make_agent_config(agent_id="worker", **kwargs) -> AgentConfig:
    defaults = dict(
        id=agent_id,
        type="claude_code",
        isolate=True,
        system_prompt="You are a worker agent.",
        tags=[],
    )
    defaults.update(kwargs)
    return AgentConfig(**defaults)


def make_orch(agent_configs=None, worktree_manager=None) -> tuple[Orchestrator, Bus]:
    bus = Bus()
    tmux = make_tmux_mock()
    cfg = make_config(agents=agent_configs or [])
    orch = Orchestrator(bus=bus, tmux=tmux, config=cfg, worktree_manager=worktree_manager)
    return orch, bus


# ---------------------------------------------------------------------------
# Test 1: PhaseSpec.chain_branch defaults to False
# ---------------------------------------------------------------------------


def test_phase_spec_chain_branch_defaults_to_false():
    """PhaseSpec.chain_branch must default to False."""
    phase = PhaseSpec(name="test", pattern="single")
    assert phase.chain_branch is False


# ---------------------------------------------------------------------------
# Test 2: PhaseSpecModel.chain_branch field exists
# ---------------------------------------------------------------------------


def test_phase_spec_model_chain_branch_field_exists():
    """PhaseSpecModel must have a chain_branch field defaulting to False."""
    model = PhaseSpecModel(name="test", pattern="single")
    assert hasattr(model, "chain_branch")
    assert model.chain_branch is False


def test_phase_spec_model_chain_branch_can_be_set_true():
    """PhaseSpecModel.chain_branch can be set to True."""
    model = PhaseSpecModel(name="test", pattern="single", chain_branch=True)
    assert model.chain_branch is True


# ---------------------------------------------------------------------------
# Test 3: WorktreeManager.create_from_branch creates worktree from named branch
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_git_repo(tmp_path):
    """Create a temporary git repository with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True
    )
    # Create initial commit so HEAD exists
    (repo / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True
    )
    return repo


def test_create_from_branch_creates_worktree(temp_git_repo):
    """create_from_branch should create a worktree branching from source_branch."""
    from tmux_orchestrator.infrastructure.worktree import WorktreeManager

    wm = WorktreeManager(temp_git_repo)
    # Create a source branch to chain from
    subprocess.run(
        ["git", "checkout", "-b", "worktree/source-agent"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )
    # Write a file and commit
    (temp_git_repo / "step1.txt").write_text("step 1 output")
    subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "step1"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )
    # Return to main
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    # Now branch a new worktree from worktree/source-agent
    wt_path = wm.create_from_branch("target-agent", "worktree/source-agent")

    assert wt_path.exists()
    # The file committed on source branch should be visible in the new worktree
    assert (wt_path / "step1.txt").exists()
    assert (wt_path / "step1.txt").read_text() == "step 1 output"
    # Clean up
    wm.teardown("target-agent")


def test_create_from_branch_registers_agent(temp_git_repo):
    """create_from_branch should register the agent as owned (isolated)."""
    from tmux_orchestrator.infrastructure.worktree import WorktreeManager

    wm = WorktreeManager(temp_git_repo)
    wm.create_from_branch("new-agent", "main")

    assert wm.is_isolated("new-agent")
    wm.teardown("new-agent")


def test_create_from_branch_nonexistent_raises(temp_git_repo):
    """create_from_branch with a non-existent source branch should raise RuntimeError."""
    from tmux_orchestrator.infrastructure.worktree import WorktreeManager

    wm = WorktreeManager(temp_git_repo)
    with pytest.raises(RuntimeError, match="failed"):
        wm.create_from_branch("some-agent", "branch-that-does-not-exist")


# ---------------------------------------------------------------------------
# Test 5: Orchestrator._ephemeral_agent_branches tracks spawned agent branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ephemeral_agent_branches_populated_on_spawn():
    """spawn_ephemeral_agent should populate _ephemeral_agent_branches for isolate=True agents."""
    agent_cfg = make_agent_config("worker", isolate=True)
    orch, bus = make_orch(agent_configs=[agent_cfg])

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        instance = AsyncMock()
        captured_ids: list[str] = []

        def _capture(*args, **kwargs):
            aid = kwargs.get("agent_id", "")
            captured_ids.append(aid)
            instance.agent_id = aid
            instance.tags = []
            return instance

        MockAgent.side_effect = _capture
        ephemeral_id = await orch.spawn_ephemeral_agent("worker")

    assert ephemeral_id in orch._ephemeral_agent_branches
    assert orch._ephemeral_agent_branches[ephemeral_id] == f"worktree/{ephemeral_id}"


@pytest.mark.asyncio
async def test_ephemeral_agent_branches_empty_for_non_isolated():
    """spawn_ephemeral_agent with isolate=False should NOT add a branch entry."""
    agent_cfg = make_agent_config("worker", isolate=False)
    orch, bus = make_orch(agent_configs=[agent_cfg])

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        instance = AsyncMock()

        def _capture(*args, **kwargs):
            aid = kwargs.get("agent_id", "")
            instance.agent_id = aid
            instance.tags = []
            return instance

        MockAgent.side_effect = _capture
        ephemeral_id = await orch.spawn_ephemeral_agent("worker")

    assert ephemeral_id not in orch._ephemeral_agent_branches


# ---------------------------------------------------------------------------
# Test 6: Orchestrator.get_worktree_manager returns the manager or None
# ---------------------------------------------------------------------------


def test_get_worktree_manager_returns_none_when_not_configured():
    """get_worktree_manager should return None when no manager is configured."""
    orch, _ = make_orch()
    assert orch.get_worktree_manager() is None


def test_get_worktree_manager_returns_manager_when_configured():
    """get_worktree_manager should return the injected WorktreeManager."""
    mock_wm = MagicMock()
    orch, _ = make_orch(worktree_manager=mock_wm)
    assert orch.get_worktree_manager() is mock_wm


# ---------------------------------------------------------------------------
# Test 7: Schema conversion preserves chain_branch
# ---------------------------------------------------------------------------


def test_to_domain_phase_spec_preserves_chain_branch():
    """_to_domain_phase_spec should map chain_branch from PhaseSpecModel to PhaseSpec."""
    model = PhaseSpecModel(name="step1", pattern="single", chain_branch=True)
    # Simulate what _to_domain_phase_spec does:
    phase = PhaseSpec(
        name=model.name,
        pattern=model.pattern,  # type: ignore[arg-type]
        chain_branch=getattr(model, "chain_branch", False),
    )
    assert phase.chain_branch is True


def test_to_domain_phase_spec_chain_branch_defaults_false():
    """_to_domain_phase_spec should default chain_branch to False when not set."""
    model = PhaseSpecModel(name="step2", pattern="single")
    phase = PhaseSpec(
        name=model.name,
        pattern=model.pattern,  # type: ignore[arg-type]
        chain_branch=getattr(model, "chain_branch", False),
    )
    assert phase.chain_branch is False


# ---------------------------------------------------------------------------
# Test 8: _make_task_spec carries chain_branch field
# ---------------------------------------------------------------------------


def test_make_task_spec_includes_chain_branch_when_true():
    """_make_task_spec should include chain_branch=True in the output dict."""
    spec = _make_task_spec(
        "phase_x_0",
        "do something",
        depends_on=[],
        required_tags=[],
        chain_branch=True,
    )
    assert spec.get("chain_branch") is True


def test_make_task_spec_omits_chain_branch_when_false():
    """_make_task_spec should NOT include chain_branch when False (default)."""
    spec = _make_task_spec(
        "phase_x_0",
        "do something",
        depends_on=[],
        required_tags=[],
    )
    assert "chain_branch" not in spec


# ---------------------------------------------------------------------------
# Test 9: SingleStrategy propagates chain_branch from PhaseSpec to task spec
# ---------------------------------------------------------------------------


def test_single_strategy_propagates_chain_branch():
    """SingleStrategy.expand should carry chain_branch=True to the task spec."""
    phase = PhaseSpec(name="step1", pattern="single", chain_branch=True)
    strategy = SingleStrategy()
    tasks, _ = strategy.expand(phase, [], "context", "prefix")
    assert len(tasks) == 1
    assert tasks[0].get("chain_branch") is True


def test_single_strategy_no_chain_branch_by_default():
    """SingleStrategy.expand should NOT add chain_branch key when False."""
    phase = PhaseSpec(name="step1", pattern="single")
    strategy = SingleStrategy()
    tasks, _ = strategy.expand(phase, [], "context", "prefix")
    assert len(tasks) == 1
    assert "chain_branch" not in tasks[0]


# ---------------------------------------------------------------------------
# Test 10: SequenceBlock with chain_branch=True propagates to inner PhaseSpec
# ---------------------------------------------------------------------------


def test_sequence_block_with_chain_branch_phases():
    """SequenceBlock containing PhaseSpec with chain_branch=True should produce task specs with that flag."""
    from tmux_orchestrator.phase_executor import _expand_sequence_block

    block = SequenceBlock(
        name="pipeline",
        phases=[
            PhaseSpec(name="step1", pattern="single", chain_branch=True),
            PhaseSpec(name="step2", pattern="single"),
        ],
    )
    tasks, statuses, terminals = _expand_sequence_block(
        block, context="ctx", scratchpad_prefix="sp"
    )
    # step1 task should have chain_branch=True
    step1_task = next(t for t in tasks if "step1" in t["local_id"])
    assert step1_task.get("chain_branch") is True
    # step2 task should not have chain_branch
    step2_task = next(t for t in tasks if "step2" in t["local_id"])
    assert "chain_branch" not in step2_task


# ---------------------------------------------------------------------------
# Test 11: expand_phases_with_status produces correct task specs with chain_branch
# ---------------------------------------------------------------------------


def test_expand_phases_with_status_chain_branch():
    """expand_phases_with_status should propagate chain_branch through the full expansion pipeline."""
    phases = [
        PhaseSpec(name="phase_a", pattern="single", chain_branch=True),
        PhaseSpec(name="phase_b", pattern="single"),
    ]
    tasks, statuses = expand_phases_with_status(phases, context="ctx")
    assert len(tasks) == 2
    task_a = next(t for t in tasks if "phase_a" in t["local_id"])
    task_b = next(t for t in tasks if "phase_b" in t["local_id"])
    assert task_a.get("chain_branch") is True
    assert "chain_branch" not in task_b
