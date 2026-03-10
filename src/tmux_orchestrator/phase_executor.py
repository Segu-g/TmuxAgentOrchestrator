"""Backward-compatibility shim for phase_executor.py.

The canonical implementation has moved to:
    tmux_orchestrator.domain.phase_strategy

This module re-exports all public names so that existing imports continue
to work without modification.

Loop support (v1.1.44):
``expand_phase_items_with_status`` and ``expand_loop_iter`` add support for
:class:`~tmux_orchestrator.domain.phase_strategy.LoopBlock` items in a
``phases`` list.  Each ``LoopBlock`` is expanded one iteration at a time via
``expand_loop_iter``; the orchestrator (or demo script) calls this when a
loop body completes and the ``until`` condition is not yet met.

Strangler Fig migration pattern (Fowler 2004):
  Old path  → tmux_orchestrator.phase_executor (this shim)
  New path  → tmux_orchestrator.domain.phase_strategy (canonical)

References:
- Fowler, "Strangler Fig Application", bliki, 2004
- DESIGN.md §10.55 (v1.1.23 — Clean Architecture Migration Phase 1)
- DESIGN.md §10.76 (v1.1.44 — LoopBlock + {iter} substitution)
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
    LoopBlock,
    LoopSpec,
    ParallelConfig,
    ParallelStrategy,
    PhaseSpec,
    PhaseStrategy,
    SingleConfig,
    SingleStrategy,
    SkipCondition,
    StrategyConfig,
    WorkflowPhaseStatus,
    _VALID_PATTERNS,
    _evaluate_skip,
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
    scratchpad: dict | None = None,
) -> list[dict]:
    """Translate a list of PhaseSpec objects into task spec dicts.

    Uses the Strategy pattern: each phase's ``pattern`` selects the
    concrete :class:`~tmux_orchestrator.domain.phase_strategy.PhaseStrategy`
    implementation from the domain registry.

    When a phase has a ``skip_condition`` that is met (checked against
    ``scratchpad``), no tasks are created for that phase and the prior terminal
    IDs are passed through unchanged so that downstream phases receive the
    correct dependency chain.

    Parameters
    ----------
    phases:
        Ordered list of phase specifications.
    context:
        Global workflow context string embedded in every task prompt.
        Overridden per-phase by ``PhaseSpec.context``.
    scratchpad_prefix:
        Prefix for scratchpad keys embedded in prompts.
    scratchpad:
        Optional scratchpad dict for evaluating ``skip_condition`` fields.
        When ``None``, skip conditions are never triggered.

    Returns
    -------
    list[dict]
        Flat list of task spec dicts ready for ``validate_dag()`` and
        ``orchestrator.submit_task()``.
    """
    all_tasks: list[dict] = []
    prior_terminal_ids: list[str] = []

    for phase in phases:
        if _evaluate_skip(phase, scratchpad):
            continue
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
    scratchpad: dict | None = None,
) -> tuple[list[dict], list[WorkflowPhaseStatus]]:
    """Like ``expand_phases``, but also returns per-phase status trackers.

    When a phase has a ``skip_condition`` that is met, no tasks are created
    and a :class:`WorkflowPhaseStatus` with ``status="skipped"`` is returned
    for that phase.  Downstream phases still run normally.

    Parameters
    ----------
    phases:
        Ordered list of phase specifications.
    context:
        Global workflow context string embedded in every task prompt.
    scratchpad_prefix:
        Prefix for scratchpad keys embedded in prompts.
    scratchpad:
        Optional scratchpad dict for evaluating ``skip_condition`` fields.

    Returns
    -------
    (task_specs, phase_statuses)
    """
    all_tasks: list[dict] = []
    all_statuses: list[WorkflowPhaseStatus] = []
    prior_terminal_ids: list[str] = []

    for phase in phases:
        if _evaluate_skip(phase, scratchpad):
            # Create a SKIPPED status tracker — no task IDs.
            skipped_ps = WorkflowPhaseStatus(
                name=phase.name,
                pattern=phase.pattern,
                task_ids=[],
            )
            skipped_ps.mark_skipped()
            all_statuses.append(skipped_ps)
            # prior_terminal_ids unchanged: downstream phases inherit the
            # same deps as if this phase completed normally.
            continue
        effective_context = phase.context if phase.context is not None else context
        strategy = get_strategy(phase.pattern)
        new_tasks, new_statuses = strategy.expand(phase, prior_terminal_ids, effective_context, scratchpad_prefix)
        all_tasks.extend(new_tasks)
        all_statuses.extend(new_statuses)
        prior_terminal_ids = _terminal_ids(phase, new_tasks)

    return all_tasks, all_statuses


# ---------------------------------------------------------------------------
# Loop support helpers (v1.1.44)
# ---------------------------------------------------------------------------


def _substitute_iter(text: str, iter_num: int) -> str:
    """Replace ``{iter}`` placeholder with the given iteration number (1-based).

    Parameters
    ----------
    text:
        Source string (e.g. a phase prompt or name).
    iter_num:
        Current iteration number (1-based).

    Returns
    -------
    str
        *text* with all ``{iter}`` occurrences replaced by *iter_num*.

    Examples
    --------
    >>> _substitute_iter("pdca_plan_iter{iter}", 2)
    'pdca_plan_iter2'
    """
    return text.replace("{iter}", str(iter_num))


def _substitute_iter_in_phase(phase: PhaseSpec, iter_num: int) -> PhaseSpec:
    """Return a copy of *phase* with ``{iter}`` replaced in its fields.

    Substitution is applied to:
    - ``name``
    - ``context``

    A shallow copy is returned (the original ``phase`` is not mutated).
    All other fields (agents, timeout, skip_condition, etc.) are shared with
    the original — they do not contain ``{iter}`` in normal usage.

    Parameters
    ----------
    phase:
        Source phase specification.
    iter_num:
        Current iteration number (1-based).

    Design reference: DESIGN.md §10.76 (v1.1.44)
    """
    from dataclasses import replace  # stdlib

    new_name = _substitute_iter(phase.name, iter_num)
    new_context = (
        _substitute_iter(phase.context, iter_num)
        if phase.context is not None
        else None
    )
    return replace(phase, name=new_name, context=new_context)


def _iter_prefix_header(iter_num: int, max_iter: int, prev_keys: list[str]) -> str:
    """Build the iteration context header injected into loop-body prompts.

    Format (injected at the front of the phase context)::

        [Iteration 2/4. Previous iteration scratchpad keys: pdca_plan_iter1, ...]

    Parameters
    ----------
    iter_num:
        Current iteration number (1-based).
    max_iter:
        Maximum number of iterations configured for the loop.
    prev_keys:
        Scratchpad keys written in the *previous* iteration (may be empty for
        iter_num==1).

    Returns
    -------
    str
        The header string (empty string when ``iter_num == 1`` and no prev_keys).

    Design reference: DESIGN.md §10.76 (v1.1.44)
    """
    if iter_num == 1 and not prev_keys:
        return ""
    if prev_keys:
        keys_list = ", ".join(prev_keys)
        return f"[Iteration {iter_num}/{max_iter}. Previous iteration scratchpad keys: {keys_list}]\n\n"
    return f"[Iteration {iter_num}/{max_iter}.]\n\n"


def _inject_header_into_phase(phase: PhaseSpec, header: str) -> PhaseSpec:
    """Return a copy of *phase* with *header* prepended to its context."""
    if not header:
        return phase
    from dataclasses import replace  # stdlib

    existing = phase.context or ""
    new_context = header + existing if existing else header.rstrip()
    return replace(phase, context=new_context)


def expand_loop_iter(
    loop_block: "LoopBlock",
    iter_num: int,
    *,
    context: str,
    scratchpad_prefix: str = "",
    prior_ids: list[str] | None = None,
    prev_scratchpad_keys: list[str] | None = None,
) -> tuple[list[dict], list[WorkflowPhaseStatus], list[str]]:
    """Expand one iteration of a LoopBlock into task specs and phase statuses.

    Each phase in ``loop_block.phases`` has ``{iter}`` substituted in its
    name and context before expansion.  An iteration-context header is
    prepended to each phase's context when ``prev_scratchpad_keys`` is
    provided.

    Parameters
    ----------
    loop_block:
        The :class:`LoopBlock` to expand.
    iter_num:
        Current iteration number (1-based).
    context:
        Global workflow context string.
    scratchpad_prefix:
        Scratchpad key prefix.
    prior_ids:
        Task IDs that the *first* phase of this iteration depends on.
        Typically the terminal IDs of the previous iteration's last phase
        (or the outer workflow's prior terminal IDs for iter_num == 1).
    prev_scratchpad_keys:
        Keys written to the scratchpad in the previous iteration.  Injected
        into the context header of each inner phase.

    Returns
    -------
    (task_specs, phase_statuses, terminal_ids)
    - ``task_specs`` — flat list of task spec dicts for this iteration.
    - ``phase_statuses`` — per-phase :class:`WorkflowPhaseStatus` trackers.
    - ``terminal_ids`` — local IDs of the terminal tasks of this iteration
      (used as ``prior_ids`` for the next iteration, or as dependencies for
      outer phases that ``depends_on`` this loop block).

    Design references:
    - Argo Workflows ``withSequence`` iteration variable substitution
    - AiiDA ``while_()`` per-iteration state isolation
    - DESIGN.md §10.76 (v1.1.44)
    """
    prior_ids = prior_ids or []
    prev_scratchpad_keys = prev_scratchpad_keys or []

    header = _iter_prefix_header(iter_num, loop_block.loop.max, prev_scratchpad_keys)

    all_tasks: list[dict] = []
    all_statuses: list[WorkflowPhaseStatus] = []
    current_prior = list(prior_ids)

    for item in loop_block.phases:
        if isinstance(item, LoopBlock):
            # Nested loop — recurse with same iter_num context
            inner_tasks, inner_statuses, inner_terminal = expand_loop_iter(
                item,
                iter_num,
                context=context,
                scratchpad_prefix=scratchpad_prefix,
                prior_ids=current_prior,
                prev_scratchpad_keys=prev_scratchpad_keys,
            )
            all_tasks.extend(inner_tasks)
            all_statuses.extend(inner_statuses)
            current_prior = inner_terminal
        else:
            # PhaseSpec — substitute {iter}, inject header, expand
            phase: PhaseSpec = item  # type: ignore[assignment]
            subst_phase = _substitute_iter_in_phase(phase, iter_num)
            subst_phase = _inject_header_into_phase(subst_phase, header)
            effective_context = subst_phase.context if subst_phase.context is not None else context
            strategy = get_strategy(subst_phase.pattern)
            new_tasks, new_statuses = strategy.expand(
                subst_phase, current_prior, effective_context, scratchpad_prefix
            )
            # Prefix local_ids with loop block name + iter number to avoid
            # collision when the same phase names appear across iterations
            # (e.g. "plan" in iter1 and iter2 would both produce "phase_plan_0"
            # without a namespace prefix, causing a DAG validation error).
            iter_prefix = f"{loop_block.name}_i{iter_num}_"
            rename_map: dict[str, str] = {
                t["local_id"]: f"{iter_prefix}{t['local_id']}"
                for t in new_tasks
            }
            renamed_tasks = []
            for t in new_tasks:
                renamed = dict(t)
                renamed["local_id"] = rename_map[t["local_id"]]
                # Fix intra-phase depends_on; leave external (prior_ids) as-is.
                renamed["depends_on"] = [rename_map.get(d, d) for d in t["depends_on"]]
                renamed_tasks.append(renamed)
            all_tasks.extend(renamed_tasks)
            # Update phase status task_ids to use renamed ids.
            for ps in new_statuses:
                ps.task_ids = [rename_map.get(tid, tid) for tid in ps.task_ids]
            all_statuses.extend(new_statuses)
            # Compute renamed terminal IDs for the NEXT phase's prior_ids.
            current_prior = [
                rename_map.get(lid, lid)
                for lid in _terminal_ids(subst_phase, new_tasks)
            ]

    return all_tasks, all_statuses, current_prior


def _expand_all_loop_iters(
    loop_block: "LoopBlock",
    *,
    context: str,
    scratchpad_prefix: str = "",
    prior_ids: list[str] | None = None,
) -> tuple[list[dict], list[WorkflowPhaseStatus], list[str]]:
    """Expand ALL iterations of a LoopBlock as a static DAG.

    Each iteration's last task becomes the ``prior_ids`` (dependencies) for
    the first task of the next iteration.  This pre-expands the entire loop
    body for ``loop.max`` iterations, providing a fully static task graph.

    The ``until`` condition is embedded in the agent prompts (agents are
    instructed to write the termination signal to the scratchpad); the
    framework does not short-circuit tasks when the condition is met at
    submit time.  Full dynamic termination support is a future enhancement
    (see DESIGN.md §10.76 Known Limitation).

    Parameters
    ----------
    loop_block:
        The :class:`LoopBlock` to fully expand.
    context:
        Global workflow context string.
    scratchpad_prefix:
        Scratchpad key prefix.
    prior_ids:
        Task IDs that the first iteration's first phase depends on.

    Returns
    -------
    (task_specs, phase_statuses, terminal_ids)
    - ``task_specs`` — flat list of all task specs across all iterations.
    - ``phase_statuses`` — all per-phase status trackers.
    - ``terminal_ids`` — local IDs of the final iteration's terminal tasks.

    Design reference: DESIGN.md §10.76 (v1.1.44)
    Known Limitation: ``until`` condition is not evaluated server-side at
    runtime; all iterations are pre-submitted.  Dynamic early-termination
    requires WorkflowManager hooks (future work).
    """
    prior_ids = prior_ids or []
    all_tasks: list[dict] = []
    all_statuses: list[WorkflowPhaseStatus] = []
    current_prior = list(prior_ids)
    prev_keys: list[str] = []

    for i in range(1, loop_block.loop.max + 1):
        iter_tasks, iter_statuses, iter_terminals = expand_loop_iter(
            loop_block,
            i,
            context=context,
            scratchpad_prefix=scratchpad_prefix,
            prior_ids=current_prior,
            prev_scratchpad_keys=prev_keys,
        )
        all_tasks.extend(iter_tasks)
        all_statuses.extend(iter_statuses)
        current_prior = iter_terminals
        # Track scratchpad keys from this iteration for the next iteration's header.
        # Keys are the local_ids of this iteration's tasks (as a proxy for their
        # associated scratchpad output keys).
        prev_keys = list(iter_terminals)

    return all_tasks, all_statuses, current_prior


def expand_phase_items_with_status(
    items: list,
    *,
    context: str,
    scratchpad_prefix: str = "",
    scratchpad: dict | None = None,
    iter_num: int = 1,
    prev_scratchpad_keys: list[str] | None = None,
) -> tuple[list[dict], list[WorkflowPhaseStatus], dict[str, list[str]]]:
    """Expand a mixed list of PhaseSpec and LoopBlock items.

    Processes each item in *items*:

    - ``PhaseSpec``: expanded using the usual strategy pattern (same as
      ``expand_phases_with_status``).
    - ``LoopBlock``: all ``loop.max`` iterations are pre-expanded into a
      static chain of tasks via ``_expand_all_loop_iters``.  The LoopBlock's
      name is recorded in the returned *loop_terminal_ids* dict so that outer
      phases that ``depends_on`` the loop block name can resolve their
      dependency.

    Parameters
    ----------
    items:
        Mixed list of :class:`PhaseSpec` and :class:`LoopBlock` objects.
    context:
        Global workflow context string.
    scratchpad_prefix:
        Scratchpad key prefix.
    scratchpad:
        Current scratchpad state for evaluating ``skip_condition`` fields
        (only applied to ``PhaseSpec`` items, not loop bodies).
    iter_num:
        Unused (kept for API compatibility); iteration numbering is internal.
    prev_scratchpad_keys:
        Unused (kept for API compatibility).

    Returns
    -------
    (task_specs, phase_statuses, loop_terminal_ids)
    - ``task_specs`` — flat list of task spec dicts.
    - ``phase_statuses`` — per-phase status trackers.
    - ``loop_terminal_ids`` — mapping from LoopBlock name to the local IDs
      of its terminal tasks (used to resolve ``depends_on: [loop_name]``
      in outer phases that follow the loop).

    Design reference: DESIGN.md §10.76 (v1.1.44)
    """
    all_tasks: list[dict] = []
    all_statuses: list[WorkflowPhaseStatus] = []
    loop_terminal_ids: dict[str, list[str]] = {}
    prior_terminal_ids: list[str] = []

    for item in items:
        if isinstance(item, LoopBlock):
            loop_tasks, loop_statuses, loop_terminals = _expand_all_loop_iters(
                item,
                context=context,
                scratchpad_prefix=scratchpad_prefix,
                prior_ids=prior_terminal_ids,
            )
            all_tasks.extend(loop_tasks)
            all_statuses.extend(loop_statuses)
            # Record terminal IDs for this loop block name so outer phases
            # that declare depends_on: [loop_block.name] can be wired up.
            loop_terminal_ids[item.name] = list(loop_terminals)
            prior_terminal_ids = list(loop_terminals)
        else:
            phase: PhaseSpec = item  # type: ignore[assignment]
            if _evaluate_skip(phase, scratchpad):
                skipped_ps = WorkflowPhaseStatus(
                    name=phase.name,
                    pattern=phase.pattern,
                    task_ids=[],
                )
                skipped_ps.mark_skipped()
                all_statuses.append(skipped_ps)
                continue
            effective_context = phase.context if phase.context is not None else context
            strategy = get_strategy(phase.pattern)
            new_tasks, new_statuses = strategy.expand(
                phase, prior_terminal_ids, effective_context, scratchpad_prefix
            )
            all_tasks.extend(new_tasks)
            all_statuses.extend(new_statuses)
            prior_terminal_ids = _terminal_ids(phase, new_tasks)

    return all_tasks, all_statuses, loop_terminal_ids


def is_until_condition_met(loop_block: "LoopBlock", scratchpad: dict) -> bool:
    """Return True if the loop's *until* condition is satisfied.

    When ``loop_block.loop.until`` is ``None``, always returns ``False``
    (no early termination; loop runs until *max*).

    Parameters
    ----------
    loop_block:
        The :class:`LoopBlock` whose ``until`` condition to evaluate.
    scratchpad:
        Current scratchpad state dict.

    Design reference: DESIGN.md §10.76 (v1.1.44)
    """
    if loop_block.loop.until is None:
        return False
    return loop_block.loop.until.is_met(scratchpad)


__all__ = [
    "AgentSelector",
    "CompetitiveConfig",
    "CompetitiveStrategy",
    "DebateConfig",
    "DebateStrategy",
    "LoopBlock",
    "LoopSpec",
    "ParallelConfig",
    "ParallelStrategy",
    "PhaseSpec",
    "PhaseStrategy",
    "SingleConfig",
    "SingleStrategy",
    "SkipCondition",
    "StrategyConfig",
    "WorkflowPhaseStatus",
    "_VALID_PATTERNS",
    "_evaluate_skip",
    "_expand_all_loop_iters",
    "_inject_header_into_phase",
    "_iter_prefix_header",
    "_make_task_spec",
    "_phase_prompt",
    "_substitute_iter",
    "_substitute_iter_in_phase",
    "_terminal_ids",
    "expand_loop_iter",
    "expand_phase_items_with_status",
    "expand_phases",
    "expand_phases_from_specs",
    "expand_phases_with_status",
    "get_strategy",
    "is_until_condition_met",
]
