"""WorkflowManager: track multi-step DAG pipeline submissions.

A *workflow* is a named collection of orchestrator tasks that were submitted
together as a directed acyclic graph (DAG).  The WorkflowManager records
which task IDs belong to each workflow run, and updates the run's status as
tasks complete.

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

Pattern: Saga (Richardson "Microservices Patterns" 2018, Ch. 4) — pipeline
orchestration without a long-lived coordinator.  This manager is a lightweight
observer: it only tracks completion, it does not drive execution.

DESIGN.md §10.20 (v0.25.0).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tmux_orchestrator.bus import Bus


@dataclass
class WorkflowRun:
    """An in-memory record of a submitted workflow DAG.

    Attributes
    ----------
    id:
        Unique workflow run UUID.
    name:
        Human-readable name provided by the submitter.
    task_ids:
        Ordered list of global orchestrator task IDs belonging to this run.
    status:
        ``"pending"`` → not yet started; ``"running"`` → at least one task
        dispatched; ``"complete"`` → all tasks succeeded; ``"failed"`` → at
        least one task errored.
    created_at:
        Unix timestamp when the workflow was submitted.
    completed_at:
        Unix timestamp when all tasks finished (success or error), or ``None``
        while still in progress.
    """

    id: str
    name: str
    task_ids: list[str]
    status: str = "pending"  # pending | running | complete | failed
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    _completed: set[str] = field(default_factory=set, repr=False)
    _failed: set[str] = field(default_factory=set, repr=False)

    def to_dict(self) -> dict:
        """Return a JSON-serialisable snapshot of this run."""
        return {
            "id": self.id,
            "name": self.name,
            "task_ids": self.task_ids,
            "status": self.status,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "tasks_total": len(self.task_ids),
            "tasks_done": len(self._completed) + len(self._failed),
            "tasks_failed": len(self._failed),
        }


class WorkflowManager:
    """Tracks submitted workflow DAGs and their completion state.

    The manager is a pure observer — it does not submit tasks or control
    dispatch.  The orchestrator calls :meth:`on_task_complete` and
    :meth:`on_task_failed` when a RESULT arrives; the manager updates the
    workflow status accordingly.

    All methods are synchronous (no async I/O) so they can be called directly
    from the orchestrator's sync-safe callbacks.

    Design reference: DESIGN.md §10.20 (v0.25.0).
    """

    def __init__(self) -> None:
        self._runs: dict[str, WorkflowRun] = {}
        # task_id → workflow_id mapping for O(1) lookup on task completion
        self._task_to_workflow: dict[str, str] = {}

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

    # ------------------------------------------------------------------
    # Completion tracking
    # ------------------------------------------------------------------

    def on_task_complete(self, task_id: str) -> None:
        """Record a successful task completion.

        Marks the task as done and transitions the workflow to ``"complete"``
        if all tasks have now finished successfully.

        No-op when *task_id* is not associated with any tracked workflow.
        """
        run = self._get_run_for_task(task_id)
        if run is None:
            return
        run._completed.add(task_id)
        run._failed.discard(task_id)  # idempotent: remove any prior failure
        self._update_status(run)

    def on_task_failed(self, task_id: str) -> None:
        """Record a failed task.

        Marks the task as failed and immediately transitions the workflow to
        ``"failed"`` status.

        No-op when *task_id* is not associated with any tracked workflow.
        """
        run = self._get_run_for_task(task_id)
        if run is None:
            return
        run._failed.add(task_id)
        run._completed.discard(task_id)
        self._update_status(run)

    def on_task_retrying(self, task_id: str) -> None:
        """Record that a task is being retried (intermediate failure).

        If *task_id* belongs to a tracked workflow that was transitioning to
        ``"failed"`` due to this task, reset the status to ``"running"`` so
        that the workflow is not prematurely marked as failed while retries are
        still outstanding.

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

    def _get_run_for_task(self, task_id: str) -> WorkflowRun | None:
        wf_id = self._task_to_workflow.get(task_id)
        if wf_id is None:
            return None
        return self._runs.get(wf_id)

    def _update_status(self, run: WorkflowRun) -> None:
        """Recompute and update the run's status field."""
        all_ids = set(run.task_ids)
        done = run._completed | run._failed

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
