"""Tests for WorkflowPhaseStatus completion tracking (v1.1.38).

Verifies that WorkflowManager correctly updates WorkflowPhaseStatus objects
when tasks complete or fail, and that the workflow's overall status advances
to "complete" when all phases finish.

Design reference: DESIGN.md §10.70 (v1.1.38)
"""

from __future__ import annotations

import time

import pytest

from tmux_orchestrator.application.workflow_manager import WorkflowManager
from tmux_orchestrator.domain.phase_strategy import WorkflowPhaseStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager() -> WorkflowManager:
    """Return a fresh WorkflowManager for each test."""
    return WorkflowManager()


def _make_phase(name: str, task_ids: list[str], pattern: str = "single") -> WorkflowPhaseStatus:
    """Build a WorkflowPhaseStatus with the given task_ids."""
    return WorkflowPhaseStatus(name=name, pattern=pattern, task_ids=list(task_ids))


def _submit_with_phases(
    wm: WorkflowManager,
    name: str,
    phases: list[WorkflowPhaseStatus],
) -> str:
    """Submit a workflow, attach phases, call register_phases, return workflow_id."""
    all_task_ids = [tid for ps in phases for tid in ps.task_ids]
    run = wm.submit(name=name, task_ids=all_task_ids)
    run.phases = phases
    wm.register_phases(run.id)
    return run.id


# ---------------------------------------------------------------------------
# register_phases
# ---------------------------------------------------------------------------


class TestRegisterPhases:
    def test_unknown_workflow_id_is_noop(self) -> None:
        wm = _make_manager()
        # Should not raise
        wm.register_phases("nonexistent-id")

    def test_skipped_phase_not_registered(self) -> None:
        wm = _make_manager()
        skipped = _make_phase("A", task_ids=[])
        skipped.mark_skipped()
        run = wm.submit("wf", task_ids=[])
        run.phases = [skipped]
        wm.register_phases(run.id)
        # No mapping should have been created
        assert len(wm._task_to_phase) == 0

    def test_tasks_registered_correctly(self) -> None:
        wm = _make_manager()
        phase_a = _make_phase("A", ["task-1", "task-2"])
        phase_b = _make_phase("B", ["task-3"])
        wf_id = _submit_with_phases(wm, "wf", [phase_a, phase_b])

        assert wm._task_to_phase["task-1"] == (wf_id, "A")
        assert wm._task_to_phase["task-2"] == (wf_id, "A")
        assert wm._task_to_phase["task-3"] == (wf_id, "B")

    def test_idempotent_double_call(self) -> None:
        wm = _make_manager()
        phase_a = _make_phase("A", ["task-1"])
        wf_id = _submit_with_phases(wm, "wf", [phase_a])
        # Call again — should not raise or corrupt state
        wm.register_phases(wf_id)
        assert wm._task_to_phase["task-1"] == (wf_id, "A")


# ---------------------------------------------------------------------------
# Phase status transitions on task complete
# ---------------------------------------------------------------------------


class TestPhaseStatusOnTaskComplete:
    def test_single_task_phase_marks_complete(self) -> None:
        wm = _make_manager()
        phase = _make_phase("A", ["t1"])
        wf_id = _submit_with_phases(wm, "wf", [phase])

        wm.on_task_complete("t1")

        assert phase.status == "complete"
        assert phase.completed_at is not None

    def test_phase_with_two_tasks_only_complete_when_both_done(self) -> None:
        wm = _make_manager()
        phase = _make_phase("A", ["t1", "t2"], pattern="parallel")
        wf_id = _submit_with_phases(wm, "wf", [phase])

        # First task completes — phase NOT complete yet
        wm.on_task_complete("t1")
        assert phase.status == "running"

        # Second task completes — phase IS complete now
        wm.on_task_complete("t2")
        assert phase.status == "complete"

    def test_phase_transitions_to_running_on_first_completion(self) -> None:
        wm = _make_manager()
        phase = _make_phase("A", ["t1", "t2"])
        wf_id = _submit_with_phases(wm, "wf", [phase])

        assert phase.status == "pending"
        wm.on_task_complete("t1")
        assert phase.status == "running"  # Not complete yet, but running

    def test_phase_complete_sets_completed_at_timestamp(self) -> None:
        wm = _make_manager()
        phase = _make_phase("A", ["t1"])
        wf_id = _submit_with_phases(wm, "wf", [phase])

        before = time.time()
        wm.on_task_complete("t1")
        after = time.time()

        assert phase.completed_at is not None
        assert before <= phase.completed_at <= after

    def test_multiple_phases_tracked_independently(self) -> None:
        wm = _make_manager()
        phase_a = _make_phase("A", ["t1"])
        phase_b = _make_phase("B", ["t2"])
        wf_id = _submit_with_phases(wm, "wf", [phase_a, phase_b])

        # Complete phase A only
        wm.on_task_complete("t1")
        assert phase_a.status == "complete"
        assert phase_b.status == "pending"

        # Complete phase B
        wm.on_task_complete("t2")
        assert phase_b.status == "complete"

    def test_task_not_in_any_phase_does_not_crash(self) -> None:
        wm = _make_manager()
        # No phases registered — should be a no-op
        wm._task_to_workflow["orphan-task"] = "no-such-run"
        # Should not raise
        wm.on_task_complete("orphan-task")

    def test_non_phase_workflow_unaffected(self) -> None:
        """Workflows without phases still track completion at run level."""
        wm = _make_manager()
        run = wm.submit("wf", task_ids=["t1", "t2"])
        # No phases attached — phases list is empty

        wm.on_task_complete("t1")
        assert run.status == "running"

        wm.on_task_complete("t2")
        assert run.status == "complete"


# ---------------------------------------------------------------------------
# Phase status transitions on task failed
# ---------------------------------------------------------------------------


class TestPhaseStatusOnTaskFailed:
    def test_single_task_phase_marks_failed(self) -> None:
        wm = _make_manager()
        phase = _make_phase("A", ["t1"])
        wf_id = _submit_with_phases(wm, "wf", [phase])

        wm.on_task_failed("t1")

        assert phase.status == "failed"
        assert phase.completed_at is not None

    def test_partial_failure_makes_phase_failed(self) -> None:
        """If any task in a phase fails and all tasks are resolved, phase is failed."""
        wm = _make_manager()
        phase = _make_phase("A", ["t1", "t2"], pattern="parallel")
        wf_id = _submit_with_phases(wm, "wf", [phase])

        wm.on_task_complete("t1")
        assert phase.status == "running"

        wm.on_task_failed("t2")
        assert phase.status == "failed"

    def test_partial_failure_still_pending_until_all_resolved(self) -> None:
        """Phase is not marked failed until ALL tasks are resolved."""
        wm = _make_manager()
        phase = _make_phase("A", ["t1", "t2", "t3"], pattern="parallel")
        wf_id = _submit_with_phases(wm, "wf", [phase])

        wm.on_task_failed("t1")
        # Only 1 of 3 resolved — phase not terminal yet
        assert phase.status == "running"

        wm.on_task_complete("t2")
        # 2 of 3 resolved — still not terminal
        assert phase.status == "running"

        wm.on_task_complete("t3")
        # All resolved; one failed → phase = failed
        assert phase.status == "failed"


# ---------------------------------------------------------------------------
# Workflow-level status driven by phase completion
# ---------------------------------------------------------------------------


class TestWorkflowStatusFromPhases:
    def test_workflow_complete_when_all_phases_done(self) -> None:
        wm = _make_manager()
        phase_a = _make_phase("A", ["t1"])
        phase_b = _make_phase("B", ["t2"])
        wf_id = _submit_with_phases(wm, "wf", [phase_a, phase_b])

        wm.on_task_complete("t1")
        run = wm.get(wf_id)
        assert run is not None
        assert run.status == "running"  # Not complete yet

        wm.on_task_complete("t2")
        assert run.status == "complete"

    def test_workflow_failed_when_phase_task_fails(self) -> None:
        wm = _make_manager()
        phase = _make_phase("A", ["t1"])
        wf_id = _submit_with_phases(wm, "wf", [phase])

        wm.on_task_failed("t1")

        run = wm.get(wf_id)
        assert run is not None
        assert run.status == "failed"
        assert phase.status == "failed"


# ---------------------------------------------------------------------------
# Skipped phase handling
# ---------------------------------------------------------------------------


class TestSkippedPhases:
    def test_skipped_phase_already_resolved_at_submission(self) -> None:
        wm = _make_manager()
        skipped = _make_phase("A", task_ids=[])
        skipped.mark_skipped()
        normal = _make_phase("B", ["t1"])
        wf_id = _submit_with_phases(wm, "wf", [skipped, normal])

        # Skipped phase starts as skipped
        assert skipped.status == "skipped"

        # Completing the normal phase completes the workflow
        wm.on_task_complete("t1")
        run = wm.get(wf_id)
        assert run is not None
        assert normal.status == "complete"
        assert run.status == "complete"

    def test_all_skipped_workflow_with_no_tasks_stays_pending(self) -> None:
        """Edge case: workflow with only skipped phases and no tasks.

        The WorkflowManager does not automatically advance a workflow with zero
        tasks to "complete" at submission time.  Status remains "pending" until
        _update_status is triggered by a task completion/failure.  This is a
        pre-existing behaviour for zero-task workflows; v1.1.38 does not change it.
        """
        wm = _make_manager()
        skipped = _make_phase("A", task_ids=[])
        skipped.mark_skipped()
        # No tasks at all
        run = wm.submit("wf", task_ids=[])
        run.phases = [skipped]
        wm.register_phases(run.id)

        # No tasks to complete — status stays "pending" (pre-existing behaviour).
        # The skipped phase is already in its terminal state.
        assert run.status == "pending"
        assert skipped.status == "skipped"


# ---------------------------------------------------------------------------
# Retry interaction
# ---------------------------------------------------------------------------


class TestRetryInteraction:
    def test_retrying_task_reverts_phase_failure(self) -> None:
        wm = _make_manager()
        phase = _make_phase("A", ["t1"])
        wf_id = _submit_with_phases(wm, "wf", [phase])

        # Task fails (first attempt)
        wm.on_task_failed("t1")
        assert phase.status == "failed"

        # Task is being retried — phase should revert to running
        wm.on_task_retrying("t1")
        assert phase.status == "running"

    def test_retrying_then_success_marks_phase_complete(self) -> None:
        wm = _make_manager()
        phase = _make_phase("A", ["t1"])
        wf_id = _submit_with_phases(wm, "wf", [phase])

        wm.on_task_failed("t1")
        wm.on_task_retrying("t1")
        wm.on_task_complete("t1")

        assert phase.status == "complete"

    def test_retrying_task_not_in_phase_is_noop(self) -> None:
        """on_task_retrying for a task not in any phase should not raise."""
        wm = _make_manager()
        run = wm.submit("wf", task_ids=["t1"])
        # No phases attached
        wm.on_task_retrying("t1")  # Should not raise


# ---------------------------------------------------------------------------
# get_phase helper
# ---------------------------------------------------------------------------


class TestGetPhaseHelper:
    def test_get_phase_returns_correct_phase(self) -> None:
        wm = _make_manager()
        phase_a = _make_phase("A", ["t1"])
        phase_b = _make_phase("B", ["t2"])
        wf_id = _submit_with_phases(wm, "wf", [phase_a, phase_b])

        result = wm._get_phase(wf_id, "B")
        assert result is phase_b

    def test_get_phase_unknown_workflow_returns_none(self) -> None:
        wm = _make_manager()
        assert wm._get_phase("nonexistent", "A") is None

    def test_get_phase_unknown_phase_name_returns_none(self) -> None:
        wm = _make_manager()
        phase_a = _make_phase("A", ["t1"])
        wf_id = _submit_with_phases(wm, "wf", [phase_a])
        assert wm._get_phase(wf_id, "Z") is None
