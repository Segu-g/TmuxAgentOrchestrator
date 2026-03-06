"""PhaseExecutor — translates PhaseSpec declarations into WorkflowTaskSpec dicts.

A *phase* is a higher-level abstraction over individual tasks.  Each phase has:
- A ``name`` (human-readable label)
- A ``pattern`` (how agents execute the phase): single | parallel | competitive | debate
- An ``agents`` selector (which agents to assign based on tags/group/count)
- Optional per-phase ``context`` override (overrides the global workflow context)

The ``expand_phases()`` function translates a list of ``PhaseSpec`` objects into a
flat list of task spec dicts (same schema as ``WorkflowTaskSpec``) with ``depends_on``
relationships automatically computed:

- Sequential phases: each phase's tasks depend on all tasks of the prior phase.
- parallel / competitive: N tasks at the same level, all independent.
- debate: advocate(s) + critic(s) per round, then judge at the end.

This module is purely functional — no async I/O, no orchestrator coupling.
The REST handler in ``web/app.py`` calls ``expand_phases()`` and feeds the
result to the same ``submit_task`` + ``WorkflowManager.submit()`` path used by
``POST /workflows`` (tasks= variant).

Design references:
- §12「ワークフロー設計の層構造」層1・2・3
- arXiv:2512.19769 (PayPal DSL, 2025): declarative pattern → task expansion,
  60% dev-time reduction, 74% fewer lines vs. imperative
- arXiv:2502.07056 (HTDAG, 2025): hierarchical task DAG, planner-executor
- LangGraph (2024): phase = node, transition = edge in StateGraph
- DESIGN.md §10.15 (v0.48.0)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass
class AgentSelector:
    """Describes how to select an agent (or agents) for a phase slot.

    Attributes
    ----------
    tags:
        ``required_tags`` constraint — only agents with ALL listed tags are eligible.
    count:
        Number of parallel agent slots to fill (used by ``parallel`` and
        ``competitive`` patterns, and for the ``advocate``/``critic`` role in ``debate``).
    target_agent:
        Force-dispatch to a specific agent ID (corresponds to ``Task.target_agent``).
    target_group:
        Restrict to agents in the named group (corresponds to ``Task.target_group``).
    """

    tags: list[str] = field(default_factory=list)
    count: int = 1
    target_agent: str | None = None
    target_group: str | None = None


# Allowed pattern values — validated by PhaseSpec at construction time.
_VALID_PATTERNS: frozenset[str] = frozenset({"single", "parallel", "competitive", "debate"})


@dataclass
class PhaseSpec:
    """Specification for a single phase in a declarative workflow.

    Attributes
    ----------
    name:
        Human-readable phase label (used to derive ``local_id`` values).
    pattern:
        Execution strategy: ``single`` | ``parallel`` | ``competitive`` | ``debate``.
    agents:
        Agent selector for the primary role (advocate in debate, worker in others).
    critic_agents:
        Agent selector for the ``critic`` role (debate pattern only).
    judge_agents:
        Agent selector for the ``judge`` role (debate pattern only).
    debate_rounds:
        Number of advocate/critic rounds before the judge phase (debate only).
    context:
        Optional per-phase context override.  When set, replaces the global
        ``context`` for this phase's task prompts.
    required_tags:
        Additional ``required_tags`` applied to all tasks in this phase
        (merged with ``agents.tags`` for the primary role).
    """

    name: str
    pattern: Literal["single", "parallel", "competitive", "debate"]
    agents: AgentSelector = field(default_factory=AgentSelector)
    critic_agents: AgentSelector = field(default_factory=AgentSelector)
    judge_agents: AgentSelector = field(default_factory=AgentSelector)
    debate_rounds: int = 1
    context: str | None = None
    required_tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.pattern not in _VALID_PATTERNS:
            raise ValueError(
                f"Invalid pattern {self.pattern!r}. "
                f"Must be one of: {sorted(_VALID_PATTERNS)}"
            )


# ---------------------------------------------------------------------------
# Phase status tracker (层2: Phase 一級市民)
# ---------------------------------------------------------------------------


@dataclass
class WorkflowPhaseStatus:
    """Run-time status tracker for a single workflow phase.

    Stored inside :class:`~tmux_orchestrator.workflow_manager.WorkflowRun`
    to expose phase-granular progress through ``GET /workflows/{id}``.

    Design reference: §12 層2「フェーズ管理」— explicit Phase concept with
    state tracking, separate from Task-level ``depends_on`` graph.
    """

    name: str
    pattern: str
    task_ids: list[str]
    status: str = "pending"   # pending | running | complete | failed
    started_at: float | None = None
    completed_at: float | None = None

    def mark_running(self) -> None:
        """Transition the phase to ``running``."""
        self.status = "running"
        if self.started_at is None:
            self.started_at = time.time()

    def mark_complete(self) -> None:
        """Transition the phase to ``complete``."""
        self.status = "complete"
        if self.completed_at is None:
            self.completed_at = time.time()

    def mark_failed(self) -> None:
        """Transition the phase to ``failed``."""
        self.status = "failed"
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
# Task spec builders
# ---------------------------------------------------------------------------


def _make_task_spec(
    local_id: str,
    prompt: str,
    depends_on: list[str],
    *,
    required_tags: list[str],
    target_agent: str | None = None,
    target_group: str | None = None,
) -> dict:
    """Build a task spec dict compatible with ``WorkflowTaskSpec``."""
    spec: dict = {
        "local_id": local_id,
        "prompt": prompt,
        "depends_on": list(depends_on),
        "required_tags": list(required_tags),
    }
    if target_agent is not None:
        spec["target_agent"] = target_agent
    if target_group is not None:
        spec["target_group"] = target_group
    return spec


def _phase_prompt(
    phase_name: str,
    pattern: str,
    context: str,
    *,
    role: str | None = None,
    agent_index: int | None = None,
    round_num: int | None = None,
    scratchpad_prefix: str = "",
) -> str:
    """Build a descriptive task prompt for a phase slot.

    The prompt embeds the phase name, pattern, role, and context so that agents
    understand their position in the larger workflow.
    """
    parts: list[str] = []

    # Header
    if role is not None:
        header_parts = [f"You are the {role.upper()} agent"]
        if round_num is not None:
            header_parts.append(f"in round {round_num}")
        header_parts.append(f"of the '{phase_name}' phase.")
        parts.append(" ".join(header_parts))
    else:
        slot_label = f"#{agent_index + 1}" if agent_index is not None else ""
        parts.append(f"You are agent{slot_label} in the '{phase_name}' phase ({pattern} pattern).")

    parts.append("")
    parts.append(f"## Task Context\n{context}")

    # Scratchpad guidance
    if scratchpad_prefix:
        key_base = f"{scratchpad_prefix}/{phase_name}"
        if role is not None:
            key = f"{key_base}/{role}" + (f"_r{round_num}" if round_num else "")
        elif agent_index is not None:
            key = f"{key_base}/{agent_index}"
        else:
            key = key_base

        parts.append(
            f"\n## Scratchpad\n"
            f"Write your final output to the scratchpad:\n"
            f"  key: `{key}`\n"
            f"Read prior phase outputs from: `{scratchpad_prefix}/`"
        )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Phase expansion logic
# ---------------------------------------------------------------------------


def _expand_single(
    phase: PhaseSpec,
    prior_ids: list[str],
    context: str,
    scratchpad_prefix: str,
) -> tuple[list[dict], list[WorkflowPhaseStatus]]:
    """Expand a ``single`` phase into one task spec."""
    local_id = f"phase_{phase.name}_0"
    prompt = _phase_prompt(
        phase.name,
        "single",
        context,
        scratchpad_prefix=scratchpad_prefix,
    )
    tags = list(phase.agents.tags) + list(phase.required_tags)
    task = _make_task_spec(
        local_id,
        prompt,
        depends_on=list(prior_ids),
        required_tags=tags,
        target_agent=phase.agents.target_agent,
        target_group=phase.agents.target_group,
    )
    ps = WorkflowPhaseStatus(name=phase.name, pattern="single", task_ids=[local_id])
    return [task], [ps]


def _expand_parallel(
    phase: PhaseSpec,
    prior_ids: list[str],
    context: str,
    scratchpad_prefix: str,
) -> tuple[list[dict], list[WorkflowPhaseStatus]]:
    """Expand a ``parallel`` phase into N independent tasks."""
    count = max(1, phase.agents.count)
    tasks: list[dict] = []
    local_ids: list[str] = []
    tags = list(phase.agents.tags) + list(phase.required_tags)

    for i in range(count):
        local_id = f"phase_{phase.name}_{i}"
        prompt = _phase_prompt(
            phase.name,
            "parallel",
            context,
            agent_index=i,
            scratchpad_prefix=scratchpad_prefix,
        )
        tasks.append(
            _make_task_spec(
                local_id,
                prompt,
                depends_on=list(prior_ids),
                required_tags=tags,
                target_agent=phase.agents.target_agent,
                target_group=phase.agents.target_group,
            )
        )
        local_ids.append(local_id)

    ps = WorkflowPhaseStatus(name=phase.name, pattern="parallel", task_ids=local_ids)
    return tasks, [ps]


def _expand_competitive(
    phase: PhaseSpec,
    prior_ids: list[str],
    context: str,
    scratchpad_prefix: str,
) -> tuple[list[dict], list[WorkflowPhaseStatus]]:
    """Expand a ``competitive`` phase into N independent tasks (same as parallel).

    The distinction from ``parallel`` is semantic: all agents solve the *same*
    problem and the caller (or a judge phase following this one) selects the
    best result.  Each task prompt notes the competitive context.
    """
    count = max(1, phase.agents.count)
    tasks: list[dict] = []
    local_ids: list[str] = []
    tags = list(phase.agents.tags) + list(phase.required_tags)

    for i in range(count):
        local_id = f"phase_{phase.name}_{i}"
        prompt = _phase_prompt(
            phase.name,
            "competitive",
            context,
            agent_index=i,
            scratchpad_prefix=scratchpad_prefix,
        )
        tasks.append(
            _make_task_spec(
                local_id,
                prompt,
                depends_on=list(prior_ids),
                required_tags=tags,
                target_agent=phase.agents.target_agent,
                target_group=phase.agents.target_group,
            )
        )
        local_ids.append(local_id)

    ps = WorkflowPhaseStatus(name=phase.name, pattern="competitive", task_ids=local_ids)
    return tasks, [ps]


def _expand_debate(
    phase: PhaseSpec,
    prior_ids: list[str],
    context: str,
    scratchpad_prefix: str,
) -> tuple[list[dict], list[WorkflowPhaseStatus]]:
    """Expand a ``debate`` phase into advocate/critic rounds + a judge task.

    Structure for ``debate_rounds=R``:
    - round 1: advocate_r1 (depends on prior_ids) + critic_r1 (depends on advocate_r1)
    - round 2: advocate_r2 (depends on critic_r1) + critic_r2 (depends on advocate_r2)
    - ...
    - judge: depends on critic_rR (and advocate_rR for completeness)

    Design references:
    - DESIGN.md §10.32 (v0.37.0 debate workflow)
    - DEBATE ACL 2024 (arXiv:2405.09935): Devil's Advocate reduces bias
    - CONSENSAGENT ACL 2025: sycophancy suppression via role isolation
    """
    rounds = max(1, phase.debate_rounds)
    advocate_tags = list(phase.agents.tags)
    critic_tags = list(phase.critic_agents.tags)
    judge_tags = list(phase.judge_agents.tags)

    tasks: list[dict] = []
    all_local_ids: list[str] = []
    current_deps = list(prior_ids)

    for r in range(1, rounds + 1):
        # Advocate
        adv_id = f"phase_{phase.name}_advocate_r{r}"
        adv_prompt = _phase_prompt(
            phase.name,
            "debate",
            context,
            role="advocate",
            round_num=r,
            scratchpad_prefix=scratchpad_prefix,
        )
        tasks.append(
            _make_task_spec(adv_id, adv_prompt, depends_on=current_deps, required_tags=advocate_tags)
        )
        all_local_ids.append(adv_id)

        # Critic (depends on advocate)
        crit_id = f"phase_{phase.name}_critic_r{r}"
        crit_prompt = _phase_prompt(
            phase.name,
            "debate",
            context,
            role="critic",
            round_num=r,
            scratchpad_prefix=scratchpad_prefix,
        )
        tasks.append(
            _make_task_spec(crit_id, crit_prompt, depends_on=[adv_id], required_tags=critic_tags)
        )
        all_local_ids.append(crit_id)

        # Next round depends on current critic
        current_deps = [crit_id]

    # Judge
    judge_id = f"phase_{phase.name}_judge"
    judge_prompt = _phase_prompt(
        phase.name,
        "debate",
        context,
        role="judge",
        scratchpad_prefix=scratchpad_prefix,
    )
    tasks.append(
        _make_task_spec(judge_id, judge_prompt, depends_on=current_deps, required_tags=judge_tags)
    )
    all_local_ids.append(judge_id)

    ps = WorkflowPhaseStatus(name=phase.name, pattern="debate", task_ids=all_local_ids)
    return tasks, [ps]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def expand_phases(
    phases: list[PhaseSpec],
    *,
    context: str,
    scratchpad_prefix: str = "",
) -> list[dict]:
    """Translate a list of PhaseSpec objects into task spec dicts.

    Tasks are emitted in dependency order:
    - Each phase's tasks depend on all *terminal* task IDs of the prior phase.
      (Terminal = the last task(s) in the prior phase's expanded list.)
    - Within a phase, tasks are independent (parallel/competitive) or
      sequentially linked (debate rounds).

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

        if phase.pattern == "single":
            new_tasks, _ = _expand_single(phase, prior_terminal_ids, effective_context, scratchpad_prefix)
        elif phase.pattern == "parallel":
            new_tasks, _ = _expand_parallel(phase, prior_terminal_ids, effective_context, scratchpad_prefix)
        elif phase.pattern == "competitive":
            new_tasks, _ = _expand_competitive(phase, prior_terminal_ids, effective_context, scratchpad_prefix)
        elif phase.pattern == "debate":
            new_tasks, _ = _expand_debate(phase, prior_terminal_ids, effective_context, scratchpad_prefix)
        else:
            raise ValueError(f"Unknown pattern: {phase.pattern!r}")

        all_tasks.extend(new_tasks)

        # Terminal IDs: for sequential chaining, the next phase depends on
        # ALL terminal tasks of the current phase.
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

        if phase.pattern == "single":
            new_tasks, new_statuses = _expand_single(phase, prior_terminal_ids, effective_context, scratchpad_prefix)
        elif phase.pattern == "parallel":
            new_tasks, new_statuses = _expand_parallel(phase, prior_terminal_ids, effective_context, scratchpad_prefix)
        elif phase.pattern == "competitive":
            new_tasks, new_statuses = _expand_competitive(phase, prior_terminal_ids, effective_context, scratchpad_prefix)
        elif phase.pattern == "debate":
            new_tasks, new_statuses = _expand_debate(phase, prior_terminal_ids, effective_context, scratchpad_prefix)
        else:
            raise ValueError(f"Unknown pattern: {phase.pattern!r}")

        all_tasks.extend(new_tasks)
        all_statuses.extend(new_statuses)
        prior_terminal_ids = _terminal_ids(phase, new_tasks)

    return all_tasks, all_statuses


def _terminal_ids(phase: PhaseSpec, tasks: list[dict]) -> list[str]:
    """Return the IDs of tasks that have no dependents within this phase.

    These are the tasks that the *next* phase must depend on.

    For single / parallel / competitive: all tasks are terminal.
    For debate: only the judge task is terminal (it has no dependents).
    """
    if phase.pattern == "debate":
        # Judge is the last task
        return [tasks[-1]["local_id"]]
    # All tasks are terminal (parallel/competitive: all at same level)
    return [t["local_id"] for t in tasks]
