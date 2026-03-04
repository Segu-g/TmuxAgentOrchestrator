"""Workflow primitive: build and submit dependency-ordered task graphs.

A ``Workflow`` lets callers declare tasks with explicit ``after=[...]`` edges
and then submit them all in a single call.  The orchestrator's dispatch loop
enforces the dependency ordering via ``Task.depends_on``.

Design note: this is intentionally a thin client-side helper, not a persistent
workflow engine.  Recovery from partial failures (some tasks succeeded, the
orchestrator restarted) is out of scope; see DESIGN.md §10.5 for the full
discussion.

Pattern: Saga (Richardson "Microservices Patterns" 2018, Ch. 4) — linear
choreography without a saga coordinator; state is inferred from
``Orchestrator._completed_tasks``.

Reference: DESIGN.md §10.5 (2026-03-05).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tmux_orchestrator.agents.base import Task
    from tmux_orchestrator.orchestrator import Orchestrator


@dataclass
class WorkflowStep:
    """A pending task definition inside a Workflow (not yet submitted)."""

    prompt: str
    priority: int = 0
    metadata: dict = field(default_factory=dict)
    after: list["WorkflowStep"] = field(default_factory=list)
    # Filled in by Workflow.run() after the Task is created
    task: "Task | None" = None


class Workflow:
    """Builder for a group of tasks with dependency edges.

    Usage::

        wf = Workflow(orchestrator)
        fetch = wf.step("fetch dataset from S3")
        transform = wf.step("clean and normalise data", after=[fetch])
        report = wf.step("generate PDF report", after=[transform])
        tasks = await wf.run()

    Calling ``run()`` submits all steps in topological order so that each step
    is submitted before the steps that depend on it.  The orchestrator will not
    dispatch a step until all its ``depends_on`` task IDs appear in its
    ``_completed_tasks`` set.
    """

    def __init__(self, orchestrator: "Orchestrator") -> None:
        self._orchestrator = orchestrator
        self._steps: list[WorkflowStep] = []

    def step(
        self,
        prompt: str,
        *,
        after: list[WorkflowStep] | None = None,
        priority: int = 0,
        metadata: dict | None = None,
    ) -> WorkflowStep:
        """Declare a workflow step (does not submit yet)."""
        s = WorkflowStep(
            prompt=prompt,
            priority=priority,
            metadata=metadata or {},
            after=after or [],
        )
        self._steps.append(s)
        return s

    async def run(self) -> list["Task"]:
        """Submit all steps in topological order and return the created Tasks.

        Raises ``ValueError`` if the dependency graph contains a cycle.
        """
        ordered = _topological_sort(self._steps)
        tasks: list["Task"] = []
        for step in ordered:
            depends_on = [s.task.id for s in step.after if s.task is not None]
            task = await self._orchestrator.submit_task(
                step.prompt,
                priority=step.priority,
                metadata=step.metadata,
                depends_on=depends_on,
            )
            step.task = task
            tasks.append(task)
        return tasks


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _topological_sort(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    """Return *steps* in topological order (dependencies before dependents).

    Uses Kahn's algorithm.  Raises ``ValueError`` on cycles.
    """
    # Build in-degree map
    step_set = set(id(s) for s in steps)
    in_degree: dict[int, int] = {id(s): 0 for s in steps}
    dependents: dict[int, list[WorkflowStep]] = {id(s): [] for s in steps}

    for step in steps:
        for dep in step.after:
            if id(dep) not in step_set:
                raise ValueError(
                    f"Step {step.prompt!r} depends on a step not in this workflow"
                )
            in_degree[id(step)] += 1
            dependents[id(dep)].append(step)

    queue = [s for s in steps if in_degree[id(s)] == 0]
    result: list[WorkflowStep] = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        for dependent in dependents[id(node)]:
            in_degree[id(dependent)] -= 1
            if in_degree[id(dependent)] == 0:
                queue.append(dependent)

    if len(result) != len(steps):
        raise ValueError("Dependency graph contains a cycle")

    return result
