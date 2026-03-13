"""Tests for v1.2.8 — workflow branch cleanup on completion + merge_to_main_on_complete.

Design reference: DESIGN.md §10.84 (v1.2.8)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from tmux_orchestrator.application.config import OrchestratorConfig
from tmux_orchestrator.application.workflow_manager import WorkflowManager
from tmux_orchestrator.domain.workflow import WorkflowRun


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(task_ids: list[str]) -> WorkflowRun:
    """Create a WorkflowRun with given task IDs."""
    return WorkflowRun(id="wf-test-1", name="test", task_ids=task_ids)


# ---------------------------------------------------------------------------
# WorktreeManager.delete_branch tests
# ---------------------------------------------------------------------------


class TestDeleteBranch:
    """Tests for WorktreeManager.delete_branch."""

    def test_delete_branch_calls_git_branch_minus_D(self, tmp_path):
        """delete_branch should invoke git branch -D with the branch name."""
        import subprocess

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=tmp_path,
            capture_output=True,
            env={**__import__("os").environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"},
        )
        subprocess.run(
            ["git", "branch", "worktree/test-agent-abc"],
            cwd=tmp_path,
            capture_output=True,
        )

        from tmux_orchestrator.infrastructure.worktree import WorktreeManager

        wm = WorktreeManager(tmp_path)
        # Branch should exist now
        result = subprocess.run(
            ["git", "branch", "--list", "worktree/test-agent-abc"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert "worktree/test-agent-abc" in result.stdout

        wm.delete_branch("worktree/test-agent-abc")

        # Branch should be gone
        result2 = subprocess.run(
            ["git", "branch", "--list", "worktree/test-agent-abc"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert "worktree/test-agent-abc" not in result2.stdout

    def test_delete_branch_no_raise_when_missing(self, tmp_path):
        """delete_branch on non-existent branch should not raise."""
        import subprocess

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=tmp_path,
            capture_output=True,
            env={**__import__("os").environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"},
        )

        from tmux_orchestrator.infrastructure.worktree import WorktreeManager

        wm = WorktreeManager(tmp_path)
        # Should not raise even if branch does not exist
        wm.delete_branch("worktree/nonexistent-branch")


# ---------------------------------------------------------------------------
# WorktreeManager.merge_branch_to_main tests
# ---------------------------------------------------------------------------


class TestMergeBranchToMain:
    """Tests for WorktreeManager.merge_branch_to_main."""

    def _setup_repo_with_main_and_feature(self, tmp_path):
        """Create a repo with main branch and a feature branch with a commit."""
        import subprocess

        env = {
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=tmp_path,
            capture_output=True,
            env=env,
        )
        # Create feature branch with a file
        subprocess.run(["git", "checkout", "-b", "worktree/phase1"], cwd=tmp_path, capture_output=True, env=env)
        (tmp_path / "step1.txt").write_text("Step 1 complete")
        subprocess.run(["git", "add", "step1.txt"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "feat: step 1"],
            cwd=tmp_path,
            capture_output=True,
            env=env,
        )
        subprocess.run(["git", "checkout", "main"], cwd=tmp_path, capture_output=True, env=env)
        return env

    def test_merge_branch_to_main_creates_merge_commit(self, tmp_path):
        """merge_branch_to_main should produce a merge commit on target branch."""
        import subprocess

        self._setup_repo_with_main_and_feature(tmp_path)

        from tmux_orchestrator.infrastructure.worktree import WorktreeManager

        wm = WorktreeManager(tmp_path)
        sha = wm.merge_branch_to_main("worktree/phase1", target="main")

        assert sha is not None
        assert len(sha) >= 7  # plausible SHA

        # The file should now be on main
        assert (tmp_path / "step1.txt").exists()

    def test_merge_branch_to_main_returns_none_on_nonexistent_target(self, tmp_path):
        """merge_branch_to_main should return None when target branch doesn't exist."""
        import subprocess

        self._setup_repo_with_main_and_feature(tmp_path)

        from tmux_orchestrator.infrastructure.worktree import WorktreeManager

        wm = WorktreeManager(tmp_path)
        result = wm.merge_branch_to_main("worktree/phase1", target="nonexistent")
        assert result is None

    def test_merge_branch_to_main_returns_none_when_no_new_commits(self, tmp_path):
        """merge_branch_to_main returns None when branch has no commits over target."""
        import subprocess

        env = {
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=tmp_path,
            capture_output=True,
            env=env,
        )
        # Create branch with no extra commits
        subprocess.run(["git", "branch", "worktree/empty-branch"], cwd=tmp_path, capture_output=True)

        from tmux_orchestrator.infrastructure.worktree import WorktreeManager

        wm = WorktreeManager(tmp_path)
        result = wm.merge_branch_to_main("worktree/empty-branch", target="main")
        assert result is None


# ---------------------------------------------------------------------------
# Orchestrator._workflow_branches tracking
# ---------------------------------------------------------------------------


class TestWorkflowBranchTracking:
    """Tests for Orchestrator._workflow_branches dict and spawn_ephemeral_agent."""

    def _make_mock_orchestrator(self):
        """Build a minimal Orchestrator-like object with required state."""
        from tmux_orchestrator.application.bus import Bus
        from tmux_orchestrator.application.config import AgentConfig, OrchestratorConfig

        bus = MagicMock()
        bus.publish = AsyncMock()
        tmux = MagicMock()
        config = OrchestratorConfig(
            session_name="test",
            agents=[
                AgentConfig(id="worker", type="claude_code", isolate=True),
            ],
        )

        from tmux_orchestrator.application.orchestrator import Orchestrator

        orch = Orchestrator(bus=bus, tmux=tmux, config=config)
        return orch

    @pytest.mark.asyncio
    async def test_workflow_branches_populated_on_spawn(self):
        """spawn_ephemeral_agent with workflow_id tracks branch."""
        orch = self._make_mock_orchestrator()

        # Mock agent
        mock_agent = MagicMock()
        mock_agent.status = MagicMock()
        mock_agent.start = AsyncMock()
        mock_agent.id = "worker-ephemeral-abc12345"

        with (
            patch(
                "tmux_orchestrator.agents.claude_code.ClaudeCodeAgent",
                return_value=mock_agent,
            ),
            patch.object(orch.registry, "register"),
        ):
            mock_agent._source_branch = None
            eph_id = await orch.spawn_ephemeral_agent("worker", workflow_id="wf-42")

        assert "wf-42" in orch._workflow_branches
        branches = orch._workflow_branches["wf-42"]
        assert any(eph_id in b for b in branches)

    @pytest.mark.asyncio
    async def test_workflow_branches_not_tracked_without_workflow_id(self):
        """spawn_ephemeral_agent without workflow_id should NOT track branches."""
        orch = self._make_mock_orchestrator()

        mock_agent = MagicMock()
        mock_agent.start = AsyncMock()
        mock_agent.id = "worker-ephemeral-xyz"

        with (
            patch(
                "tmux_orchestrator.agents.claude_code.ClaudeCodeAgent",
                return_value=mock_agent,
            ),
            patch.object(orch.registry, "register"),
        ):
            mock_agent._source_branch = None
            await orch.spawn_ephemeral_agent("worker")

        # No workflow tracking when workflow_id is None
        assert "wf-42" not in orch._workflow_branches

    @pytest.mark.asyncio
    async def test_multiple_spawns_same_workflow_append_branches(self):
        """Multiple spawns for same workflow_id accumulate branches in order."""
        orch = self._make_mock_orchestrator()

        spawned_ids = []

        async def fake_spawn(template_id, *, source_branch=None, workflow_id=None):
            eph_id = f"{template_id}-ephemeral-{len(spawned_ids):04x}"
            spawned_ids.append(eph_id)
            branch = f"worktree/{eph_id}"
            orch._ephemeral_agent_branches[eph_id] = branch
            orch._ephemeral_agents.add(eph_id)
            if workflow_id:
                orch._workflow_branches.setdefault(workflow_id, []).append(branch)
            return eph_id

        with patch.object(orch, "spawn_ephemeral_agent", side_effect=fake_spawn):
            await orch.spawn_ephemeral_agent("worker", workflow_id="wf-seq")
            await orch.spawn_ephemeral_agent("worker", workflow_id="wf-seq")
            await orch.spawn_ephemeral_agent("worker", workflow_id="wf-seq")

        assert len(orch._workflow_branches["wf-seq"]) == 3

    @pytest.mark.asyncio
    async def test_different_workflows_tracked_separately(self):
        """Branches from different workflows are tracked independently."""
        orch = self._make_mock_orchestrator()

        async def fake_spawn(template_id, *, source_branch=None, workflow_id=None):
            import uuid as _uuid
            eph_id = f"{template_id}-ephemeral-{_uuid.uuid4().hex[:8]}"
            branch = f"worktree/{eph_id}"
            orch._ephemeral_agent_branches[eph_id] = branch
            orch._ephemeral_agents.add(eph_id)
            if workflow_id:
                orch._workflow_branches.setdefault(workflow_id, []).append(branch)
            return eph_id

        with patch.object(orch, "spawn_ephemeral_agent", side_effect=fake_spawn):
            await orch.spawn_ephemeral_agent("worker", workflow_id="wf-A")
            await orch.spawn_ephemeral_agent("worker", workflow_id="wf-B")
            await orch.spawn_ephemeral_agent("worker", workflow_id="wf-A")

        assert len(orch._workflow_branches.get("wf-A", [])) == 2
        assert len(orch._workflow_branches.get("wf-B", [])) == 1


# ---------------------------------------------------------------------------
# Orchestrator.cleanup_workflow_branches
# ---------------------------------------------------------------------------


class TestCleanupWorkflowBranches:
    """Tests for Orchestrator.cleanup_workflow_branches."""

    def _make_orch_with_wm(self):
        """Build a minimal Orchestrator with a mock WorktreeManager."""
        from tmux_orchestrator.application.bus import Bus
        from tmux_orchestrator.application.config import AgentConfig, OrchestratorConfig
        from tmux_orchestrator.application.orchestrator import Orchestrator

        bus = MagicMock()
        bus.publish = AsyncMock()
        tmux = MagicMock()
        config = OrchestratorConfig(session_name="test")
        mock_wm = MagicMock()
        mock_wm.delete_branch = MagicMock()
        mock_wm.merge_branch_to_main = MagicMock(return_value="abc1234")

        orch = Orchestrator(bus=bus, tmux=tmux, config=config, worktree_manager=mock_wm)
        return orch, mock_wm

    @pytest.mark.asyncio
    async def test_cleanup_deletes_all_tracked_branches(self):
        """cleanup_workflow_branches deletes all branches for the workflow."""
        orch, mock_wm = self._make_orch_with_wm()
        orch._workflow_branches["wf-1"] = [
            "worktree/agent-a",
            "worktree/agent-b",
            "worktree/agent-c",
        ]

        deleted = await orch.cleanup_workflow_branches("wf-1")

        assert len(deleted) == 3
        assert mock_wm.delete_branch.call_count == 3
        assert call("worktree/agent-a") in mock_wm.delete_branch.call_args_list
        assert call("worktree/agent-b") in mock_wm.delete_branch.call_args_list
        assert call("worktree/agent-c") in mock_wm.delete_branch.call_args_list
        # Branches should be removed from tracking dict
        assert "wf-1" not in orch._workflow_branches

    @pytest.mark.asyncio
    async def test_cleanup_returns_empty_for_unknown_workflow(self):
        """cleanup_workflow_branches with unknown ID returns empty list."""
        orch, mock_wm = self._make_orch_with_wm()

        deleted = await orch.cleanup_workflow_branches("wf-unknown")

        assert deleted == []
        mock_wm.delete_branch.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_returns_empty_when_no_worktree_manager(self):
        """cleanup_workflow_branches returns empty list when no WM configured."""
        from tmux_orchestrator.application.bus import Bus
        from tmux_orchestrator.application.config import OrchestratorConfig
        from tmux_orchestrator.application.orchestrator import Orchestrator

        bus = MagicMock()
        bus.publish = AsyncMock()
        tmux = MagicMock()
        config = OrchestratorConfig(session_name="test")
        orch = Orchestrator(bus=bus, tmux=tmux, config=config, worktree_manager=None)
        orch._workflow_branches["wf-2"] = ["worktree/agent-x"]

        deleted = await orch.cleanup_workflow_branches("wf-2")

        assert deleted == []

    @pytest.mark.asyncio
    async def test_cleanup_with_merge_final_to_main_calls_merge_first(self):
        """cleanup with merge_final_to_main=True merges the last branch then deletes all."""
        orch, mock_wm = self._make_orch_with_wm()
        orch._workflow_branches["wf-3"] = [
            "worktree/phase1-eph",
            "worktree/phase2-eph",
        ]

        deleted = await orch.cleanup_workflow_branches("wf-3", merge_final_to_main=True)

        # merge should be called with the LAST branch
        mock_wm.merge_branch_to_main.assert_called_once_with("worktree/phase2-eph")
        assert len(deleted) == 2

    @pytest.mark.asyncio
    async def test_cleanup_continues_on_merge_failure(self):
        """cleanup should delete branches even when merge_branch_to_main raises."""
        orch, mock_wm = self._make_orch_with_wm()
        mock_wm.merge_branch_to_main.side_effect = RuntimeError("merge conflict")
        orch._workflow_branches["wf-4"] = ["worktree/agent-p", "worktree/agent-q"]

        # Should not raise
        deleted = await orch.cleanup_workflow_branches("wf-4", merge_final_to_main=True)

        # All branches should still be deleted
        assert len(deleted) == 2


# ---------------------------------------------------------------------------
# WorkflowManager.set_branch_cleanup_fn + _update_status trigger
# ---------------------------------------------------------------------------


class TestWorkflowManagerBranchCleanupTrigger:
    """Tests for WorkflowManager branch cleanup callback integration."""

    @pytest.mark.asyncio
    async def test_set_branch_cleanup_fn_is_called_on_complete(self):
        """set_branch_cleanup_fn callback is invoked when workflow completes."""
        wm = WorkflowManager()
        cleanup_calls = []

        async def mock_cleanup(wf_id: str) -> None:
            cleanup_calls.append(wf_id)

        wm.set_branch_cleanup_fn(mock_cleanup)

        run = wm.submit("test", ["task-1", "task-2"])
        wm.on_task_complete("task-1")
        wm.on_task_complete("task-2")

        # Allow scheduled futures to run
        await asyncio.sleep(0.01)
        assert run.id in cleanup_calls

    @pytest.mark.asyncio
    async def test_set_branch_cleanup_fn_is_called_on_failed(self):
        """set_branch_cleanup_fn callback is invoked when workflow fails."""
        wm = WorkflowManager()
        cleanup_calls = []

        async def mock_cleanup(wf_id: str) -> None:
            cleanup_calls.append(wf_id)

        wm.set_branch_cleanup_fn(mock_cleanup)

        run = wm.submit("test", ["task-a", "task-b"])
        wm.on_task_complete("task-a")
        wm.on_task_failed("task-b")

        await asyncio.sleep(0.01)
        assert run.id in cleanup_calls

    @pytest.mark.asyncio
    async def test_branch_cleanup_fn_called_once_not_twice(self):
        """branch_cleanup_fn should only fire on the FIRST terminal transition."""
        wm = WorkflowManager()
        cleanup_calls = []

        async def mock_cleanup(wf_id: str) -> None:
            cleanup_calls.append(wf_id)

        wm.set_branch_cleanup_fn(mock_cleanup)

        run = wm.submit("test", ["task-x"])
        wm.on_task_complete("task-x")
        # Calling on_task_complete again for an already-complete task should not re-trigger
        wm.on_task_complete("task-x")

        await asyncio.sleep(0.01)
        assert cleanup_calls.count(run.id) == 1

    def test_set_branch_cleanup_fn_stores_fn(self):
        """set_branch_cleanup_fn stores the callback correctly."""
        wm = WorkflowManager()

        async def my_fn(wf_id: str) -> None:
            pass

        assert wm._branch_cleanup_fn is None
        wm.set_branch_cleanup_fn(my_fn)
        assert wm._branch_cleanup_fn is my_fn


# ---------------------------------------------------------------------------
# OrchestratorConfig.workflow_branch_cleanup field
# ---------------------------------------------------------------------------


class TestOrchestratorConfigBranchCleanup:
    """Tests for OrchestratorConfig.workflow_branch_cleanup default."""

    def test_workflow_branch_cleanup_defaults_to_true(self):
        """OrchestratorConfig.workflow_branch_cleanup should default to True."""
        config = OrchestratorConfig()
        assert config.workflow_branch_cleanup is True

    def test_workflow_branch_cleanup_can_be_set_false(self):
        """OrchestratorConfig.workflow_branch_cleanup can be disabled."""
        config = OrchestratorConfig(workflow_branch_cleanup=False)
        assert config.workflow_branch_cleanup is False


# ---------------------------------------------------------------------------
# WorkflowSubmit.merge_to_main_on_complete field
# ---------------------------------------------------------------------------


class TestWorkflowSubmitMergeToMainField:
    """Tests for WorkflowSubmit.merge_to_main_on_complete schema field."""

    def test_merge_to_main_on_complete_field_exists(self):
        """WorkflowSubmit should have merge_to_main_on_complete field."""
        from tmux_orchestrator.web.schemas import WorkflowSubmit

        submit = WorkflowSubmit(
            name="test",
            tasks=[
                {
                    "local_id": "t1",
                    "prompt": "do something",
                    "depends_on": [],
                }
            ],
        )
        assert hasattr(submit, "merge_to_main_on_complete")
        assert submit.merge_to_main_on_complete is False

    def test_merge_to_main_on_complete_can_be_set_true(self):
        """WorkflowSubmit.merge_to_main_on_complete can be set to True."""
        from tmux_orchestrator.web.schemas import WorkflowSubmit

        submit = WorkflowSubmit(
            name="test",
            tasks=[
                {
                    "local_id": "t1",
                    "prompt": "do something",
                    "depends_on": [],
                }
            ],
            merge_to_main_on_complete=True,
        )
        assert submit.merge_to_main_on_complete is True
