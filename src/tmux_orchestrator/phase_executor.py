"""Backward-compatibility shim for phase_executor.py.

The canonical implementation has moved to:
    tmux_orchestrator.domain.phase_strategy

This module re-exports all public names so that existing imports continue
to work without modification.

Strangler Fig migration pattern (Fowler 2004):
  Old path  → tmux_orchestrator.phase_executor (this shim)
  New path  → tmux_orchestrator.domain.phase_strategy (canonical)

References:
- Fowler, "Strangler Fig Application", bliki, 2004
- DESIGN.md §10.55 (v1.1.23 — Clean Architecture Migration Phase 1)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Re-export value objects and status tracker from canonical domain location
# ---------------------------------------------------------------------------

from tmux_orchestrator.domain.phase_strategy import (  # noqa: F401
    AgentSelector,
    CompetitiveConfig,
    CompetitiveStrategy,
    DebateConfig,
    DebateStrategy,
    ParallelConfig,
    ParallelStrategy,
    PhaseSpec,
    PhaseStrategy,
    SingleConfig,
    SingleStrategy,
    StrategyConfig,
    WorkflowPhaseStatus,
    _VALID_PATTERNS,
    _make_task_spec,
    _phase_prompt,
    expand_phases_from_specs,
    get_strategy,
)

# ---------------------------------------------------------------------------
# Re-export the expand_phases public API
# (these functions remain in this shim for backward compat since they are
#  called by web/routers/workflows.py and tests directly)
# ---------------------------------------------------------------------------


def _terminal_ids(phase: PhaseSpec, tasks: list[dict]) -> list[str]:
    """Return the IDs of tasks that have no dependents within this phase.

    For single / parallel / competitive: all tasks are terminal.
    For debate: only the judge task is terminal.
    """
    if phase.pattern == "debate":
        return [tasks[-1]["local_id"]]
    return [t["local_id"] for t in tasks]


def expand_phases(
    phases: list[PhaseSpec],
    *,
    context: str,
    scratchpad_prefix: str = "",
) -> list[dict]:
    """Translate a list of PhaseSpec objects into task spec dicts.

    Uses the Strategy pattern: each phase's ``pattern`` selects the
    concrete :class:`~tmux_orchestrator.domain.phase_strategy.PhaseStrategy`
    implementation from the domain registry.

    Parameters
    ----------
    phases:
        Ordered list of phase specifications.
    context:
        Global workflow context string embedded in every task prompt.
        Overridden per-phase by ``PhaseSpec.context``.
    scratchpad_prefix:
        Prefix for scratchpad keys embedded in prompts.

    Returns
    -------
    list[dict]
        Flat list of task spec dicts ready for ``validate_dag()`` and
        ``orchestrator.submit_task()``.
    """
    all_tasks: list[dict] = []
    prior_terminal_ids: list[str] = []

    for phase in phases:
        effective_context = phase.context if phase.context is not None else context
        strategy = get_strategy(phase.pattern)
        new_tasks, _ = strategy.expand(phase, prior_terminal_ids, effective_context, scratchpad_prefix)
        all_tasks.extend(new_tasks)
        prior_terminal_ids = _terminal_ids(phase, new_tasks)

    return all_tasks


def expand_phases_with_status(
    phases: list[PhaseSpec],
    *,
    context: str,
    scratchpad_prefix: str = "",
) -> tuple[list[dict], list[WorkflowPhaseStatus]]:
    """Like ``expand_phases``, but also returns per-phase status trackers.

    Returns
    -------
    (task_specs, phase_statuses)
    """
    all_tasks: list[dict] = []
    all_statuses: list[WorkflowPhaseStatus] = []
    prior_terminal_ids: list[str] = []

    for phase in phases:
        effective_context = phase.context if phase.context is not None else context
        strategy = get_strategy(phase.pattern)
        new_tasks, new_statuses = strategy.expand(phase, prior_terminal_ids, effective_context, scratchpad_prefix)
        all_tasks.extend(new_tasks)
        all_statuses.extend(new_statuses)
        prior_terminal_ids = _terminal_ids(phase, new_tasks)

    return all_tasks, all_statuses


__all__ = [
    "AgentSelector",
    "CompetitiveConfig",
    "CompetitiveStrategy",
    "DebateConfig",
    "DebateStrategy",
    "ParallelConfig",
    "ParallelStrategy",
    "PhaseSpec",
    "PhaseStrategy",
    "SingleConfig",
    "SingleStrategy",
    "StrategyConfig",
    "WorkflowPhaseStatus",
    "_VALID_PATTERNS",
    "_make_task_spec",
    "_phase_prompt",
    "_terminal_ids",
    "expand_phases",
    "expand_phases_from_specs",
    "expand_phases_with_status",
    "get_strategy",
]
