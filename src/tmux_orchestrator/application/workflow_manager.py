"""WorkflowManager: track multi-step DAG pipeline submissions.

Canonical location: tmux_orchestrator.application.workflow_manager

A *workflow* is a named collection of orchestrator tasks that were submitted
together as a directed acyclic graph (DAG).  The WorkflowManager records
which task IDs belong to each workflow run, and updates the run's status as
tasks complete.

Pure application-layer component — no tmux, no HTTP, no filesystem.
Depends only on domain/ types (WorkflowRun) and stdlib.

The root-level shim ``workflow_manager.py`` re-exports everything from here
for backward compat.

Design references:
- Apache Airflow DAG model — task dependencies as directed acyclic graph
  (https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dags.html)
- Prefect "Modern Data Stack" workflow orchestration
  (https://www.prefect.io/guide/blog/modern-data-stack)
- Tomasulo's algorithm / topological sort for dependency resolution
  (R. Tomasulo, IBM J. Res. Dev. 1967; Cormen et al. "Introduction to
  Algorithms" 4th ed. §22.4 — topological sort)
- AWS Step Functions — state machine for workflow orchestration
  (https://docs.aws.amazon.com/step-functions/latest/dg/concepts-amazon-states-language.html)
- Richardson "Microservices Patterns" 2018, Ch. 4 — Saga pattern
- DESIGN.md §10.20 (v0.25.0), §10.56 (v1.1.24 — Clean Architecture Phase 2).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable

# ---------------------------------------------------------------------------
# Strangler Fig: WorkflowRun is canonical in domain/workflow.py.
# Re-export it here so existing imports from this module continue to work.
# ---------------------------------------------------------------------------
from tmux_orchestrator.domain.workflow import WorkflowRun  # noqa: F401

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from tmux_orchestrator.domain.phase_strategy import LoopSpec


class WorkflowManager:
    """Tracks submitted workflow DAGs and their completion state.

    The manager is a pure observer — it does not submit tasks or control
    dispatch.  The orchestrator calls :meth:`on_task_complete` and
    :meth:`on_task_failed` when a RESULT arrives; the manager updates the
    workflow status accordingly.

    All methods are synchronous (no async I/O) so they can be called directly
    from the orchestrator's sync-safe callbacks.

    Phase completion tracking (v1.1.38):
    When a workflow run has phases (``run.phases`` is non-empty), the manager
    also tracks per-phase task completion.  When all tasks in a phase are done,
    the corresponding :class:`~tmux_orchestrator.domain.phase_strategy.WorkflowPhaseStatus`
    is transitioned to ``"complete"`` or ``"failed"``.  Skipped phases (with no
    task_ids) are treated as already resolved at submission time and are not
    tracked here.

    The internal ``_task_to_phase`` dict maps ``task_id → (workflow_id,
    phase_name)`` for O(1) lookup.  It is populated via :meth:`register_phases`
    after the caller attaches phases to the WorkflowRun.

    Design references:
    - DESIGN.md §10.20 (v0.25.0)
    - DESIGN.md §10.70 (v1.1.38 — phase completion tracking)
    - Netflix Maestro: per-step completion counters driving phase-level state
      (https://netflixtechblog.com/100x-faster-how-we-supercharged-netflix-maestros-workflow-engine-028e9637f041)
    - Argo Workflows DAG task completion tracking
      (https://argo-workflows.readthedocs.io/en/latest/walk-through/dag/)
    - Richardson "Microservices Patterns" Ch.4 — Saga Orchestration pattern
    """

    def __init__(self) -> None:
        self._runs: dict[str, WorkflowRun] = {}
        # task_id → workflow_id mapping for O(1) lookup on task completion
        self._task_to_workflow: dict[str, str] = {}
        # Phase completion tracking (v1.1.38):
        # task_id → (workflow_id, phase_name) for O(1) phase lookup
        self._task_to_phase: dict[str, tuple[str, str]] = {}
        # (workflow_id, phase_name) → set of completed task_ids
        self._phase_completed: dict[tuple[str, str], set[str]] = {}
        # (workflow_id, phase_name) → set of failed task_ids
        self._phase_failed: dict[tuple[str, str], set[str]] = {}
        # Loop until runtime evaluation (v1.2.7):
        # (workflow_id, loop_name) → list[list[str]] where inner list[i] is task_ids for iter i
        self._loop_iterations: dict[tuple[str, str], list[list[str]]] = {}
        # (workflow_id, loop_name) → LoopSpec (for until condition)
        self._loop_specs: dict[tuple[str, str], "LoopSpec"] = {}
        # (workflow_id, loop_name) → scratchpad prefix for the loop
        self._loop_scratchpad_prefix: dict[tuple[str, str], str] = {}
        # Tracks all completed task IDs (for loop until iteration completion checks)
        self._completed_tasks: set[str] = set()
        # Scratchpad store reference (injected via set_scratchpad)
        self._scratchpad: Any | None = None
        # Cancel function injected by the orchestrator (callable[[str], None])
        # Called synchronously; should schedule async cancellation internally.
        self._cancel_task_fn: Callable[[str], None] | None = None
        # Branch cleanup callback injected by the workflow router (v1.2.8).
        # Called asynchronously when a workflow reaches "complete" or "failed".
        # Signature: async (workflow_id: str) -> None
        # Design reference: DESIGN.md §10.84 (v1.2.8)
        self._branch_cleanup_fn: "Callable[[str], Awaitable[None]] | None" = None
        # Phase webhook callback injected by web/app.py (v1.2.9).
        # Called asynchronously when a phase transitions to complete/failed/skipped.
        # Signature: async (event_type: str, payload: dict) -> None
        # Design reference: DESIGN.md §10.85 (v1.2.9)
        self._fire_webhook_fn: "Callable[[str, dict], Awaitable[None]] | None" = None

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    def submit(self, name: str, task_ids: list[str]) -> WorkflowRun:
        """Register a new workflow run with the given task IDs.

        Parameters
        ----------
        name:
            Human-readable name for the workflow.
        task_ids:
            List of global orchestrator task IDs that belong to this run.
            Must be non-empty.

        Returns
        -------
        WorkflowRun
            The newly created workflow run record.
        """
        run_id = str(uuid.uuid4())
        run = WorkflowRun(
            id=run_id,
            name=name,
            task_ids=list(task_ids),
        )
        self._runs[run_id] = run
        for tid in task_ids:
            self._task_to_workflow[tid] = run_id
        return run

    def register_phases(self, workflow_id: str) -> None:
        """Register phase-task mappings for an existing workflow run.

        Must be called after :meth:`submit` and after the caller has attached
        ``WorkflowPhaseStatus`` objects to ``run.phases``.  Records the
        ``task_id → (workflow_id, phase_name)`` mapping for each non-skipped
        phase task, enabling O(1) phase-status lookups in
        :meth:`on_task_complete` and :meth:`on_task_failed`.

        Skipped phases (``status == "skipped"``, ``task_ids == []``) are not
        registered — they are already in their terminal state.

        This method is idempotent: calling it twice on the same workflow_id
        overwrites the existing mappings (safe for checkpoint-restore paths).

        Parameters
        ----------
        workflow_id:
            The :attr:`WorkflowRun.id` returned by :meth:`submit`.

        Design reference: DESIGN.md §10.70 (v1.1.38)
        """
        run = self._runs.get(workflow_id)
        if run is None:
            return
        for phase in run.phases:
            phase_key = (workflow_id, phase.name)
            if phase.status == "skipped" or not phase.task_ids:
                # Already terminal; no tracking needed.
                continue
            # Initialise tracking sets (preserve existing if idempotent call)
            if phase_key not in self._phase_completed:
                self._phase_completed[phase_key] = set()
            if phase_key not in self._phase_failed:
                self._phase_failed[phase_key] = set()
            for tid in phase.task_ids:
                self._task_to_phase[tid] = (workflow_id, phase.name)

    # ------------------------------------------------------------------
    # Loop until runtime evaluation (v1.2.7)
    # ------------------------------------------------------------------

    def set_scratchpad(self, scratchpad: Any) -> None:
        """Inject the shared scratchpad store for loop-until condition evaluation.

        Called by the web app after building the WorkflowManager so that
        ``_check_loop_until`` can evaluate ``SkipCondition`` against live
        scratchpad state.

        Design reference: DESIGN.md §10.83 (v1.2.7)
        """
        self._scratchpad = scratchpad

    def set_cancel_task_fn(self, fn: Callable[[str], None]) -> None:
        """Inject the cancel-task callback used when a loop until condition is met.

        The callback receives a task_id string and should schedule async
        cancellation (e.g. via ``asyncio.create_task(orchestrator.cancel_task(tid))``).
        It is called once per remaining-iteration task when the condition fires.

        Design reference: DESIGN.md §10.83 (v1.2.7)
        """
        self._cancel_task_fn = fn

    def set_branch_cleanup_fn(
        self, fn: "Callable[[str], Awaitable[None]]"
    ) -> None:
        """Inject the branch-cleanup callback called when a workflow reaches terminal state.

        The callback is called with the workflow_id string once when the workflow
        transitions to ``"complete"`` or ``"failed"``.  It should schedule
        async deletion of the worktree branches accumulated by ephemeral agents
        in that workflow.

        Example callback::

            async def cleanup(wf_id: str) -> None:
                await orchestrator.cleanup_workflow_branches(wf_id)

        Parameters
        ----------
        fn:
            Async callable: ``async (workflow_id: str) -> None``

        Design reference: DESIGN.md §10.84 (v1.2.8)
        """
        self._branch_cleanup_fn = fn

    def set_webhook_fn(
        self, fn: "Callable[[str, dict], Awaitable[None]]"
    ) -> None:
        """Inject the webhook-fire callback called when a phase transitions to a terminal state.

        The callback receives the event type string (``"phase_complete"``,
        ``"phase_failed"``, or ``"phase_skipped"``) and a payload dict, and
        should forward them to the :class:`~tmux_orchestrator.webhook_manager.WebhookManager`.

        Example callback::

            async def fire(event_type: str, payload: dict) -> None:
                await webhook_manager.deliver(event_type, payload)

        Parameters
        ----------
        fn:
            Async callable: ``async (event_type: str, payload: dict) -> None``

        Design reference: DESIGN.md §10.85 (v1.2.9)
        """
        self._fire_webhook_fn = fn

    def register_loop(
        self,
        workflow_id: str,
        loop_name: str,
        loop_spec: "LoopSpec",
        iterations: list[list[str]],
        scratchpad_prefix: str,
    ) -> None:
        """Register a LoopBlock's iteration task IDs and LoopSpec for runtime until evaluation.

        Parameters
        ----------
        workflow_id:
            The workflow run ID returned by :meth:`submit`.
        loop_name:
            The ``LoopBlock.name`` string (unique within the workflow).
        loop_spec:
            The :class:`~tmux_orchestrator.domain.phase_strategy.LoopSpec`
            carrying ``max`` and ``until`` condition.
        iterations:
            ``iterations[i]`` is the list of *global* orchestrator task IDs
            for iteration ``i`` (0-indexed).
        scratchpad_prefix:
            The scratchpad namespace prefix for the workflow run.

        Design reference: DESIGN.md §10.83 (v1.2.7)
        """
        key = (workflow_id, loop_name)
        self._loop_iterations[key] = iterations
        self._loop_specs[key] = loop_spec
        self._loop_scratchpad_prefix[key] = scratchpad_prefix

    # ------------------------------------------------------------------
    # Completion tracking
    # ------------------------------------------------------------------

    def on_task_complete(self, task_id: str) -> None:
        """Record a successful task completion.

        Marks the task as done and transitions the workflow to ``"complete"``
        if all tasks have now finished successfully.  Also updates the
        per-phase :class:`~tmux_orchestrator.domain.phase_strategy.WorkflowPhaseStatus`
        when all tasks in the phase are resolved.

        No-op when *task_id* is not associated with any tracked workflow, or
        when the workflow has already been cancelled.

        Design reference: DESIGN.md §10.70 (v1.1.38)
        """
        run = self._get_run_for_task(task_id)
        if run is None:
            return
        if run.status == "cancelled":
            return
        run._completed.add(task_id)
        run._failed.discard(task_id)  # idempotent: remove any prior failure
        self._completed_tasks.add(task_id)
        self._update_phase_status(task_id, run, completed=True)
        self._update_status(run)
        # Check loop-until conditions after each task completion (v1.2.7)
        self._check_loop_until(task_id)

    def on_task_failed(self, task_id: str) -> None:
        """Record a failed task.

        Marks the task as failed and immediately transitions the workflow to
        ``"failed"`` status.  Also updates the per-phase
        :class:`~tmux_orchestrator.domain.phase_strategy.WorkflowPhaseStatus`
        when all tasks in the phase are resolved (complete or failed).

        No-op when *task_id* is not associated with any tracked workflow, or
        when the workflow has already been cancelled.

        Design reference: DESIGN.md §10.70 (v1.1.38)
        """
        run = self._get_run_for_task(task_id)
        if run is None:
            return
        if run.status == "cancelled":
            return
        run._failed.add(task_id)
        run._completed.discard(task_id)
        self._update_phase_status(task_id, run, completed=False)
        self._update_status(run)

    def cancel(self, workflow_id: str) -> list[str]:
        """Mark a workflow as cancelled and return its task IDs.

        Sets the workflow status to ``"cancelled"`` and records the completion
        time.  Subsequent calls to :meth:`on_task_complete` and
        :meth:`on_task_failed` for tasks belonging to this workflow are no-ops.

        Returns the list of task IDs belonging to the workflow, or an empty
        list if *workflow_id* is unknown.

        Design references:
        - Apache Airflow ``dag_run.update_state("cancelled")``: bulk workflow cancel
        - AWS Step Functions ``StopExecution``: cancel a running state machine
        - DESIGN.md §10.22 (v0.27.0)
        """
        run = self._runs.get(workflow_id)
        if run is None:
            return []
        run.status = "cancelled"
        if run.completed_at is None:
            run.completed_at = time.time()
        return list(run.task_ids)

    def on_task_retrying(self, task_id: str) -> None:
        """Record that a task is being retried (intermediate failure).

        If *task_id* belongs to a tracked workflow that was transitioning to
        ``"failed"`` due to this task, reset the status to ``"running"`` so
        that the workflow is not prematurely marked as failed while retries are
        still outstanding.  Also reverts any phase-level failure marking for
        this task's phase.

        No-op when *task_id* is not associated with any tracked workflow.

        Design reference:
        - AWS SQS maxReceiveCount / Redrive policy — re-enqueue before DLQ
        - Netflix Hystrix retry — transient-failure tolerance
        - DESIGN.md §10.21 (v0.26.0)
        """
        run = self._get_run_for_task(task_id)
        if run is None:
            return
        # Remove from failed set — this task is still in-flight (retrying).
        run._failed.discard(task_id)
        run._completed.discard(task_id)
        # Revert phase-level failure tracking for this task.
        phase_key = self._task_to_phase.get(task_id)
        if phase_key is not None:
            self._phase_failed.get(phase_key, set()).discard(task_id)
            self._phase_completed.get(phase_key, set()).discard(task_id)
            # Revert phase to running if it had been marked failed.
            wf_id, phase_name = phase_key
            phase = self._get_phase(wf_id, phase_name)
            if phase is not None and phase.status == "failed":
                phase.mark_running()
        self._update_status(run)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, workflow_id: str) -> WorkflowRun | None:
        """Return the workflow run for *workflow_id*, or ``None`` if unknown."""
        return self._runs.get(workflow_id)

    def list_all(self) -> list[dict]:
        """Return a snapshot list of all workflow runs as dicts."""
        return [run.to_dict() for run in self._runs.values()]

    def status(self, workflow_id: str) -> dict | None:
        """Return the status dict for *workflow_id*, or ``None`` if unknown."""
        run = self._runs.get(workflow_id)
        if run is None:
            return None
        return run.to_dict()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def get_workflow_status_for_task(self, task_id: str) -> str | None:
        """Return the current status string of the workflow that contains *task_id*.

        Returns ``None`` when *task_id* is not part of any tracked workflow.
        Used by the orchestrator to detect workflow state transitions for webhook delivery.
        DESIGN.md §10.25 (v0.30.0).
        """
        run = self._get_run_for_task(task_id)
        if run is None:
            return None
        return run.status

    def get_workflow_id_for_task(self, task_id: str) -> str | None:
        """Return the workflow ID that contains *task_id*, or None.

        Used by the orchestrator to include workflow_id in webhook payloads.
        DESIGN.md §10.25 (v0.30.0).
        """
        return self._task_to_workflow.get(task_id)

    def _get_run_for_task(self, task_id: str) -> WorkflowRun | None:
        wf_id = self._task_to_workflow.get(task_id)
        if wf_id is None:
            return None
        return self._runs.get(wf_id)

    def _get_phase(self, workflow_id: str, phase_name: str) -> "Any | None":
        """Return the WorkflowPhaseStatus for the given workflow/phase pair, or None.

        Searches ``run.phases`` by name.  O(N_phases) but N_phases is small.

        Design reference: DESIGN.md §10.70 (v1.1.38)
        """
        run = self._runs.get(workflow_id)
        if run is None:
            return None
        for phase in run.phases:
            if phase.name == phase_name:
                return phase
        return None

    def _update_phase_status(self, task_id: str, run: WorkflowRun, *, completed: bool) -> None:
        """Update phase-level status when a task completes or fails.

        Looks up which phase *task_id* belongs to via ``_task_to_phase``.  If
        found, updates the per-phase tracking sets and — when all tasks in the
        phase are resolved — transitions the
        :class:`~tmux_orchestrator.domain.phase_strategy.WorkflowPhaseStatus`
        to ``"complete"`` or ``"failed"``.

        Also transitions the phase from ``"pending"`` to ``"running"`` on first
        task resolution (to reflect that work started).

        Parameters
        ----------
        task_id:
            The task that just completed or failed.
        run:
            The WorkflowRun that owns the phase.
        completed:
            ``True`` for a successful completion, ``False`` for a failure.

        Design reference: DESIGN.md §10.70 (v1.1.38)
        Research:
        - Netflix Maestro per-step completion counters
          (https://netflixtechblog.com/100x-faster-how-we-supercharged-netflix-maestros-workflow-engine-028e9637f041)
        - Richardson "Microservices Patterns" Ch.4 — Saga Orchestration
          (https://microservices.io/patterns/data/saga.html)
        """
        phase_key = self._task_to_phase.get(task_id)
        if phase_key is None:
            # Not a phases-mode workflow, or task not in any tracked phase.
            return
        wf_id, phase_name = phase_key
        phase = self._get_phase(wf_id, phase_name)
        if phase is None:
            return

        # Transition to running on first resolution (if still pending).
        if phase.status == "pending":
            phase.mark_running()

        # Update per-phase tracking sets.
        if completed:
            self._phase_completed.setdefault(phase_key, set()).add(task_id)
            self._phase_failed.setdefault(phase_key, set()).discard(task_id)
        else:
            self._phase_failed.setdefault(phase_key, set()).add(task_id)
            self._phase_completed.setdefault(phase_key, set()).discard(task_id)

        done = self._phase_completed.get(phase_key, set()) | self._phase_failed.get(phase_key, set())
        all_ids = set(phase.task_ids)

        # When all tasks are resolved, transition the phase to terminal state.
        if done >= all_ids:
            if self._phase_failed.get(phase_key):
                phase.mark_failed()
                self._fire_phase_webhook("phase_failed", run, phase_name, phase)
            else:
                phase.mark_complete()
                self._fire_phase_webhook("phase_complete", run, phase_name, phase)

    def _check_loop_until(self, completed_task_id: str) -> None:
        """After a task completes, check if any loop's until condition is now met.

        For each registered LoopBlock, when all tasks in one iteration have
        completed and the ``until`` condition evaluates to True against the
        current scratchpad, cancel all tasks from subsequent iterations
        (they are still in the orchestrator queue or waiting list) and clean up
        the registration.

        No-op when no scratchpad or cancel function has been injected.

        Design reference: DESIGN.md §10.83 (v1.2.7)
        """
        if not self._loop_iterations:
            return

        # Iterate over a snapshot of keys so we can delete during iteration.
        for (wf_id, loop_name) in list(self._loop_iterations.keys()):
            loop_spec = self._loop_specs.get((wf_id, loop_name))
            if loop_spec is None or loop_spec.until is None:
                continue

            iterations = self._loop_iterations.get((wf_id, loop_name))
            if not iterations:
                continue

            # Only check iterations that contain the just-completed task.
            for iter_idx, iter_task_ids in enumerate(iterations):
                if completed_task_id not in iter_task_ids:
                    continue

                # All tasks in this iteration must be complete before evaluating.
                if not all(tid in self._completed_tasks for tid in iter_task_ids):
                    break  # iteration not fully done yet; nothing to evaluate

                # Evaluate the until condition against the current scratchpad.
                scratchpad = self._scratchpad if self._scratchpad is not None else {}
                condition_met = loop_spec.until.is_met(scratchpad)
                if condition_met:
                    # Condition met — cancel all remaining iterations.
                    for future_iter in iterations[iter_idx + 1:]:
                        for tid in future_iter:
                            if self._cancel_task_fn is not None:
                                self._cancel_task_fn(tid)
                            # Mark the task+phase as resolved so the workflow
                            # can transition to "complete" once all tasks settle.
                            self._mark_task_skipped(tid, wf_id)

                    # Re-evaluate workflow status now that cancelled tasks are resolved.
                    run = self._runs.get(wf_id)
                    if run is not None:
                        self._update_status(run)

                    # Clean up registry so this loop is not evaluated again.
                    del self._loop_iterations[(wf_id, loop_name)]
                    self._loop_specs.pop((wf_id, loop_name), None)
                    self._loop_scratchpad_prefix.pop((wf_id, loop_name), None)
                break  # completed_task_id can only be in one iteration

    def _mark_task_skipped(self, task_id: str, workflow_id: str) -> None:
        """Mark a task as resolved-by-cancellation (loop early termination).

        When a loop until condition fires, the remaining iteration tasks are
        cancelled in the orchestrator queue.  This method records them as
        "done" in the WorkflowRun so that ``_update_status`` can transition
        the workflow to "complete" once the cancelled tasks are accounted for.

        Also marks the *phase* containing the task as "skipped" if that phase
        is not yet in a terminal state.

        Design reference: DESIGN.md §10.83 (v1.2.7)
        """
        run = self._runs.get(workflow_id)
        if run is None:
            return
        # Count cancelled tasks as completed so _update_status resolves correctly.
        run._completed.add(task_id)
        run._failed.discard(task_id)
        self._completed_tasks.add(task_id)
        # Mark phase as skipped.
        if run.phases:
            for phase_status in run.phases:
                if task_id in phase_status.task_ids:
                    if phase_status.status not in ("complete", "failed", "skipped"):
                        phase_status.mark_skipped()
                        self._fire_phase_webhook("phase_skipped", run, phase_status.name, phase_status)
                    break

    def _fire_phase_webhook(
        self,
        event_type: str,
        run: WorkflowRun,
        phase_name: str,
        phase: "Any",
    ) -> None:
        """Schedule a fire-and-forget webhook delivery for a phase transition.

        No-op when ``_fire_webhook_fn`` has not been injected or when there is
        no running asyncio event loop (e.g. synchronous test contexts).

        Parameters
        ----------
        event_type:
            One of ``"phase_complete"``, ``"phase_failed"``, ``"phase_skipped"``.
        run:
            The :class:`WorkflowRun` that owns the phase.
        phase_name:
            Human-readable phase name.
        phase:
            The phase status object (used to extract ``task_ids``).

        Design reference: DESIGN.md §10.85 (v1.2.9)
        Research: CloudEvents spec — combination of workflow_id + phase_name
        uniquely identifies the event; payload includes task_ids for tracing.
        """
        if self._fire_webhook_fn is None:
            return
        from datetime import datetime, timezone  # noqa: PLC0415

        payload = {
            "workflow_id": run.id,
            "workflow_name": run.name,
            "phase_name": phase_name,
            "task_ids": list(getattr(phase, "task_ids", [])),
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._fire_webhook_fn(event_type, payload))
        except RuntimeError:
            pass  # No running event loop — skip (sync test context)

    def _update_status(self, run: WorkflowRun) -> None:
        """Recompute and update the run's status field.

        When the workflow transitions to a terminal state (``"complete"`` or
        ``"failed"``) for the first time (``completed_at`` transitions from
        ``None``), the injected ``_branch_cleanup_fn`` (if any) is scheduled
        as a fire-and-forget asyncio task.

        Design reference: DESIGN.md §10.84 (v1.2.8)
        """
        all_ids = set(run.task_ids)
        done = run._completed | run._failed
        prev_completed_at = run.completed_at

        if run._failed:
            run.status = "failed"
            if done >= all_ids and run.completed_at is None:
                run.completed_at = time.time()
        elif done >= all_ids:
            run.status = "complete"
            if run.completed_at is None:
                run.completed_at = time.time()
        elif run._completed or run._failed:
            run.status = "running"
        else:
            run.status = "pending"

        # Trigger branch cleanup on first transition to terminal state.
        # prev_completed_at == None ensures we fire exactly once per workflow.
        if (
            prev_completed_at is None
            and run.completed_at is not None
            and run.status in ("complete", "failed")
            and self._branch_cleanup_fn is not None
        ):
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(self._branch_cleanup_fn(run.id))
            except RuntimeError:
                pass  # No running event loop — skip (sync test context)


# ---------------------------------------------------------------------------
# DAG validation helper (used by REST handler before submission)
# ---------------------------------------------------------------------------


def validate_dag(
    tasks: list[dict],
    *,
    local_id_key: str = "local_id",
    deps_key: str = "depends_on",
) -> list[dict]:
    """Validate and topologically sort a list of task spec dicts.

    Each dict must have a *local_id_key* field and optional *deps_key* list
    of local IDs that this task depends on.

    Returns the tasks in topological order (dependencies before dependents).

    Raises
    ------
    ValueError
        If any dependency references an unknown local_id, or if the graph
        contains a cycle.

    Design reference:
    - Kahn's algorithm (Kahn 1962) — topological sort in O(V + E)
    - Tomasulo's algorithm — dependency resolution by register renaming;
      the analogous operation here is "local_id → global task_id renaming"
    - Cormen et al. "Introduction to Algorithms" 4th ed. §22.4
    """
    local_ids = {t[local_id_key] for t in tasks}

    # Validate: all deps reference known local_ids
    for t in tasks:
        for dep in t.get(deps_key, []):
            if dep not in local_ids:
                raise ValueError(
                    f"Task {t[local_id_key]!r} depends on unknown local_id {dep!r}"
                )

    # Kahn's topological sort
    in_degree: dict[str, int] = {t[local_id_key]: 0 for t in tasks}
    dependents: dict[str, list[str]] = {t[local_id_key]: [] for t in tasks}

    for t in tasks:
        for dep in t.get(deps_key, []):
            in_degree[t[local_id_key]] += 1
            dependents[dep].append(t[local_id_key])

    task_by_id = {t[local_id_key]: t for t in tasks}
    queue = [lid for lid, deg in in_degree.items() if deg == 0]
    result: list[dict] = []

    while queue:
        lid = queue.pop(0)
        result.append(task_by_id[lid])
        for child_lid in dependents[lid]:
            in_degree[child_lid] -= 1
            if in_degree[child_lid] == 0:
                queue.append(child_lid)

    if len(result) != len(tasks):
        raise ValueError("Workflow dependency graph contains a cycle")

    return result


__all__ = ["WorkflowManager", "WorkflowRun", "validate_dag"]
