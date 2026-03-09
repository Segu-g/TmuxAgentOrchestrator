"""Strategy pattern for workflow phase execution.

A *phase strategy* encapsulates the algorithm that translates a phase
declaration into a list of concrete task specs.  Each strategy class
represents one execution mode:

- ``SingleStrategy``      — one task for the phase
- ``ParallelStrategy``    — N independent tasks at the same DAG level
- ``CompetitiveStrategy`` — N independent tasks solving the same problem
- ``DebateStrategy``      — advocate/critic rounds + judge

Domain value objects ``AgentSelector`` and ``PhaseSpec`` live here because
they describe *what* a phase should do — that is pure domain knowledge with
no infrastructure dependency.

Layer rule: this module must NOT import from infrastructure, web, or
application layers.  Only stdlib and ``tmux_orchestrator.domain.*`` are
allowed.

Design:
- ``PhaseStrategy`` is a ``typing.Protocol`` (PEP 544 structural subtyping).
  Concrete strategies need not inherit from it — duck typing is sufficient.
- Strategies are ORTHOGONAL to ``WorkflowRun``.  A strategy answers
  "how do I execute this phase?" while ``WorkflowRun`` answers
  "what is the current state of this workflow?".

Strangler Fig migration (Fowler 2004):
  Canonical location: ``tmux_orchestrator.domain.phase_strategy`` (this file)
  Shim location:     ``tmux_orchestrator.phase_executor``
                     (re-exports ``PhaseSpec``, ``AgentSelector``,
                     ``WorkflowPhaseStatus``, ``expand_phases``, etc.)

References:
- Gamma et al., "Design Patterns" (GoF, 1994) — Strategy pattern, p. 315
- Python PEP 544 — Protocols: Structural subtyping (static duck typing)
- Percival & Gregory, "Architecture Patterns with Python", O'Reilly, 2020
- DESIGN.md §10.55 (v1.1.23 — Clean Architecture Migration Phase 1)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Value objects — describe WHAT a phase does
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
        ``competitive`` patterns, and for the ``advocate``/``critic`` role
        in ``debate``).
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
# WorkflowPhaseStatus — runtime status tracker (phase-level, not run-level)
# ---------------------------------------------------------------------------


@dataclass
class WorkflowPhaseStatus:
    """Run-time status tracker for a single workflow phase.

    Stored inside a WorkflowRun to expose phase-granular progress through
    ``GET /workflows/{id}``.

    This dataclass duplicates the interface of
    :class:`~tmux_orchestrator.domain.workflow.WorkflowPhase` but is kept
    separate for backward compatibility with the ``phase_executor`` shim.
    Both are valid; callers should prefer ``WorkflowPhase`` for new code.

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
# PhaseStrategy Protocol — HOW a phase is executed
# ---------------------------------------------------------------------------


@runtime_checkable
class PhaseStrategy(Protocol):
    """Protocol for phase execution strategies.

    Each concrete strategy class encapsulates the algorithm for expanding a
    :class:`PhaseSpec` into a list of task spec dicts (``WorkflowTaskSpec``
    compatible dicts).

    Implementations must be pure functions of their arguments — no I/O,
    no network, no filesystem access.

    Method signature
    ----------------
    ``expand(phase, prior_ids, context, scratchpad_prefix) -> (task_specs, phase_statuses)``

    Parameters
    ----------
    phase:
        The phase declaration to expand.
    prior_ids:
        Task IDs of the terminal tasks of the immediately preceding phase.
        The new phase's root tasks must ``depends_on`` these IDs.
    context:
        Effective context string for this phase (already resolved: per-phase
        override or global workflow context).
    scratchpad_prefix:
        Scratchpad key prefix to embed in task prompts for agent coordination.

    Returns
    -------
    tuple[list[dict], list[WorkflowPhaseStatus]]
        - task_specs: flat list of task spec dicts ready for ``validate_dag()``
        - phase_statuses: list of :class:`WorkflowPhaseStatus` trackers
    """

    def expand(
        self,
        phase: PhaseSpec,
        prior_ids: list[str],
        context: str,
        scratchpad_prefix: str,
    ) -> tuple[list[dict], list[WorkflowPhaseStatus]]: ...


# ---------------------------------------------------------------------------
# Shared prompt / task-spec helpers (pure functions)
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
    """Build a descriptive task prompt for a phase slot."""
    parts: list[str] = []

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
# Concrete strategies
# ---------------------------------------------------------------------------


class SingleStrategy:
    """Expand a ``single`` phase into one task spec."""

    def expand(
        self,
        phase: PhaseSpec,
        prior_ids: list[str],
        context: str,
        scratchpad_prefix: str,
    ) -> tuple[list[dict], list[WorkflowPhaseStatus]]:
        local_id = f"phase_{phase.name}_0"
        prompt = _phase_prompt(phase.name, "single", context, scratchpad_prefix=scratchpad_prefix)
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


class ParallelStrategy:
    """Expand a ``parallel`` phase into N independent tasks."""

    def expand(
        self,
        phase: PhaseSpec,
        prior_ids: list[str],
        context: str,
        scratchpad_prefix: str,
    ) -> tuple[list[dict], list[WorkflowPhaseStatus]]:
        count = max(1, phase.agents.count)
        tasks: list[dict] = []
        local_ids: list[str] = []
        tags = list(phase.agents.tags) + list(phase.required_tags)

        for i in range(count):
            local_id = f"phase_{phase.name}_{i}"
            prompt = _phase_prompt(
                phase.name, "parallel", context, agent_index=i, scratchpad_prefix=scratchpad_prefix
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


class CompetitiveStrategy:
    """Expand a ``competitive`` phase into N independent tasks.

    The distinction from ``parallel`` is semantic: all agents solve the *same*
    problem and a subsequent judge phase selects the best result.
    """

    def expand(
        self,
        phase: PhaseSpec,
        prior_ids: list[str],
        context: str,
        scratchpad_prefix: str,
    ) -> tuple[list[dict], list[WorkflowPhaseStatus]]:
        count = max(1, phase.agents.count)
        tasks: list[dict] = []
        local_ids: list[str] = []
        tags = list(phase.agents.tags) + list(phase.required_tags)

        for i in range(count):
            local_id = f"phase_{phase.name}_{i}"
            prompt = _phase_prompt(
                phase.name, "competitive", context, agent_index=i, scratchpad_prefix=scratchpad_prefix
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


class DebateStrategy:
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

    def expand(
        self,
        phase: PhaseSpec,
        prior_ids: list[str],
        context: str,
        scratchpad_prefix: str,
    ) -> tuple[list[dict], list[WorkflowPhaseStatus]]:
        rounds = max(1, phase.debate_rounds)
        advocate_tags = list(phase.agents.tags)
        critic_tags = list(phase.critic_agents.tags)
        judge_tags = list(phase.judge_agents.tags)

        tasks: list[dict] = []
        all_local_ids: list[str] = []
        current_deps = list(prior_ids)

        for r in range(1, rounds + 1):
            adv_id = f"phase_{phase.name}_advocate_r{r}"
            adv_prompt = _phase_prompt(
                phase.name, "debate", context, role="advocate", round_num=r,
                scratchpad_prefix=scratchpad_prefix,
            )
            tasks.append(
                _make_task_spec(adv_id, adv_prompt, depends_on=current_deps, required_tags=advocate_tags)
            )
            all_local_ids.append(adv_id)

            crit_id = f"phase_{phase.name}_critic_r{r}"
            crit_prompt = _phase_prompt(
                phase.name, "debate", context, role="critic", round_num=r,
                scratchpad_prefix=scratchpad_prefix,
            )
            tasks.append(
                _make_task_spec(crit_id, crit_prompt, depends_on=[adv_id], required_tags=critic_tags)
            )
            all_local_ids.append(crit_id)

            current_deps = [crit_id]

        judge_id = f"phase_{phase.name}_judge"
        judge_prompt = _phase_prompt(
            phase.name, "debate", context, role="judge", scratchpad_prefix=scratchpad_prefix,
        )
        tasks.append(
            _make_task_spec(judge_id, judge_prompt, depends_on=current_deps, required_tags=judge_tags)
        )
        all_local_ids.append(judge_id)

        ps = WorkflowPhaseStatus(name=phase.name, pattern="debate", task_ids=all_local_ids)
        return tasks, [ps]


# ---------------------------------------------------------------------------
# Strategy registry — maps pattern name to concrete strategy instance
# ---------------------------------------------------------------------------

_STRATEGY_REGISTRY: dict[str, PhaseStrategy] = {
    "single": SingleStrategy(),
    "parallel": ParallelStrategy(),
    "competitive": CompetitiveStrategy(),
    "debate": DebateStrategy(),
}


def get_strategy(pattern: str) -> PhaseStrategy:
    """Return the concrete strategy for the given pattern name.

    Raises ``ValueError`` for unknown patterns.
    """
    try:
        return _STRATEGY_REGISTRY[pattern]
    except KeyError:
        raise ValueError(
            f"Unknown phase pattern {pattern!r}. "
            f"Must be one of: {sorted(_STRATEGY_REGISTRY)}"
        )
