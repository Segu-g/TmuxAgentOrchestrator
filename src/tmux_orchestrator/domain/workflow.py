"""Pure domain types for workflow tracking.

Contains the core workflow entities used across the application.
These types have zero external dependencies — only Python stdlib.

Design:
- ``WorkflowStatus`` — enum-like string constants for a workflow run's state.
- ``WorkflowPhase`` — a single named phase within a workflow, with its
  execution pattern, task IDs, and lifecycle state.
- ``WorkflowRun`` — a complete workflow run record: name, phases, status.

Layer rule: this module must NOT import from infrastructure, web, or
application layers.  It may only import from stdlib and
``tmux_orchestrator.domain.*``.

Strangler Fig migration (Fowler 2004):
  Canonical location: ``tmux_orchestrator.domain.workflow`` (this file)
  Shim location:     ``tmux_orchestrator.workflow_manager.WorkflowRun``
                     ``tmux_orchestrator.phase_executor.WorkflowPhaseStatus``

References:
- Martin, "Clean Architecture" (2017) — Dependency Inversion Principle
- Fowler, "Strangler Fig Application", bliki, 2004
- Percival, Gregory, "Architecture Patterns with Python", O'Reilly, 2020
- DESIGN.md §10.55 (v1.1.23 — Clean Architecture Migration Phase 1)
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# WorkflowStatus — value constants for workflow and phase lifecycle
# ---------------------------------------------------------------------------


class WorkflowStatus(str, Enum):
    """Lifecycle states for a workflow run or workflow phase.

    Inherits from ``str`` so that values can be compared to plain strings
    (``run.status == "complete"``) without additional conversion, maintaining
    backward compatibility with code that stores status as a raw string.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# WorkflowPhase — domain entity for a single named phase
# ---------------------------------------------------------------------------


@dataclass
class WorkflowPhase:
    """Run-time status tracker for a single workflow phase.

    A *phase* is a group of tasks that share a common execution pattern
    (``single``, ``parallel``, ``competitive``, ``debate``).  Phases are
    sequential with respect to each other; within a phase, tasks may run
    in parallel.

    Attributes
    ----------
    name:
        Human-readable phase label.
    pattern:
        Execution strategy: ``single`` | ``parallel`` | ``competitive`` | ``debate``.
    task_ids:
        Ordered list of task IDs belonging to this phase.
    status:
        Current lifecycle state (pending → running → complete/failed).
    started_at:
        Unix timestamp when the first task in this phase was dispatched.
    completed_at:
        Unix timestamp when the phase reached a terminal state.
    """

    name: str
    pattern: str
    task_ids: list[str]
    status: str = WorkflowStatus.PENDING.value
    started_at: float | None = None
    completed_at: float | None = None

    def mark_running(self) -> None:
        """Transition the phase to ``running``."""
        self.status = WorkflowStatus.RUNNING.value
        if self.started_at is None:
            self.started_at = time.time()

    def mark_complete(self) -> None:
        """Transition the phase to ``complete``."""
        self.status = WorkflowStatus.COMPLETE.value
        if self.completed_at is None:
            self.completed_at = time.time()

    def mark_failed(self) -> None:
        """Transition the phase to ``failed``."""
        self.status = WorkflowStatus.FAILED.value
        if self.completed_at is None:
            self.completed_at = time.time()

    def to_dict(self) -> dict:
        """Return a JSON-serialisable snapshot."""
        return {
            "name": self.name,
            "pattern": self.pattern,
            "task_ids": list(self.task_ids),
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


# ---------------------------------------------------------------------------
# WorkflowRun — aggregate root for a submitted workflow DAG
# ---------------------------------------------------------------------------


@dataclass
class WorkflowRun:
    """An in-memory record of a submitted workflow DAG.

    This is the aggregate root for a workflow run.  It owns the phase list
    and is the source of truth for completion tracking.

    Attributes
    ----------
    id:
        Unique workflow run UUID.
    name:
        Human-readable name provided by the submitter.
    task_ids:
        Ordered list of global orchestrator task IDs belonging to this run.
    status:
        Current lifecycle state (pending → running → complete/failed/cancelled).
    phases:
        List of :class:`WorkflowPhase` objects when submitted via phases= mode.
        Empty when submitted via the legacy tasks= mode.
    created_at:
        Unix timestamp when the workflow was submitted.
    completed_at:
        Unix timestamp when all tasks finished (success or error), or ``None``
        while still in progress.

    Notes
    -----
    ``_completed`` and ``_failed`` are private tracking sets used by
    :class:`~tmux_orchestrator.workflow_manager.WorkflowManager` to determine
    when to transition the run to a terminal status.  They are excluded from
    ``repr`` and ``to_dict`` output.
    """

    id: str
    name: str
    task_ids: list[str]
    status: str = WorkflowStatus.PENDING.value
    phases: list[Any] = field(default_factory=list, repr=False)
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    # DAG edge list: list of (from_task_id, to_task_id) tuples.
    # Populated at submission time from depends_on relationships.
    # Used by GET /workflows/{id}/dag to return topology without re-derivation.
    # Design reference: DESIGN.md §10.90 (v1.2.14)
    dag_edges: list[tuple[str, str]] = field(default_factory=list, repr=False)
    _completed: set[str] = field(default_factory=set, repr=False)
    _failed: set[str] = field(default_factory=set, repr=False)

    @classmethod
    def create(cls, name: str, task_ids: list[str], *, run_id: str | None = None) -> "WorkflowRun":
        """Factory method: create a new WorkflowRun with a fresh UUID."""
        return cls(
            id=run_id or str(uuid.uuid4()),
            name=name,
            task_ids=list(task_ids),
        )

    def to_dict(self) -> dict:
        """Return a JSON-serialisable snapshot of this run."""
        d: dict = {
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
        if self.phases:
            d["phases"] = [p.to_dict() for p in self.phases]
        return d
