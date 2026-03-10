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
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal, Protocol, Union, runtime_checkable


# ---------------------------------------------------------------------------
# StrategyConfig value objects — typed parameters per strategy (stdlib only)
#
# Design: stdlib dataclasses with a ``type`` discriminator field for each
# strategy pattern.  The domain layer is kept free of third-party dependencies
# (no Pydantic) — Pydantic wrapper models live in the web/schemas layer.
#
# References:
# - Gamma et al., "Design Patterns" (GoF 1994) — Strategy pattern
# - ezyang, "Idiomatic ADTs in Python with dataclasses and Union" (2020)
# - DESIGN.md §10.63 (v1.1.31)
# ---------------------------------------------------------------------------


@dataclass
class SingleConfig:
    """Typed configuration for the ``single`` strategy.

    Currently has no extra parameters beyond the discriminator, but provides
    a typed placeholder for future extension (e.g. retry policy).
    """

    type: Literal["single"] = "single"


@dataclass
class ParallelConfig:
    """Typed configuration for the ``parallel`` strategy.

    Attributes
    ----------
    merge_strategy:
        How to aggregate outputs from parallel agents.
        ``"collect"`` (default) — return all outputs as a list.
        ``"first_wins"`` — return the first completed output.
    """

    type: Literal["parallel"] = "parallel"
    merge_strategy: str = "collect"

    def __post_init__(self) -> None:
        valid = {"collect", "first_wins"}
        if self.merge_strategy not in valid:
            raise ValueError(f"merge_strategy must be one of {sorted(valid)!r}")


@dataclass
class CompetitiveConfig:
    """Typed configuration for the ``competitive`` strategy.

    Attributes
    ----------
    scorer:
        Scoring function identifier.  ``"llm_judge"`` (default) delegates
        evaluation to a subsequent judge agent.
    top_k:
        Number of top-scored solutions to preserve.  Must be >= 1.
    timeout_per_agent:
        Per-agent task timeout override (seconds).  When set, each
        competitive task gets this timeout instead of the phase-level
        or global ``task_timeout``.
    judge_prompt_template:
        Optional template string for the judge task prompt.  Supports
        ``{criteria}``, ``{solutions}``, and ``{context}`` placeholders
        substituted at expand time.  When empty (default), the built-in
        judge prompt is generated from the phase name and context.

        Design reference: DESIGN.md §10.64 (v1.1.32)
        Research: Monte Carlo Data "LLM-As-Judge: 7 Best Practices";
        arXiv 2504.17087 (meta-judge rubric pipeline).
    """

    type: Literal["competitive"] = "competitive"
    scorer: str = "llm_judge"
    top_k: int = 1
    timeout_per_agent: int | None = None
    judge_prompt_template: str = ""

    def __post_init__(self) -> None:
        if self.top_k < 1:
            raise ValueError("top_k must be >= 1")


@dataclass
class DebateConfig:
    """Typed configuration for the ``debate`` strategy.

    Attributes
    ----------
    rounds:
        Number of advocate/critic rounds before the judge phase.
        Must be >= 1.
    require_consensus:
        When ``True``, the judge must include "CONSENSUS_REACHED" in its
        output to mark the debate as successful.  (Future: early-stop signal.)
    judge_criteria:
        Free-text criteria injected into the judge task prompt.  Allows
        custom evaluation dimensions (e.g. "correctness, brevity, clarity").
    early_stop_signal:
        Keyword string that, when written by the judge agent to the
        scratchpad, signals early termination of remaining rounds.
        When non-empty, the judge prompt instructs the agent to emit
        this signal if consensus is detected before all rounds complete.
        When empty (default), early-stop behaviour is disabled.

        Design reference: DESIGN.md §10.64 (v1.1.32)
        Research: arXiv 2510.12697 (Adaptive Stability Detection);
        ICLR 2025 MAD blog (convergence-based stopping).
    """

    type: Literal["debate"] = "debate"
    rounds: int = 1
    require_consensus: bool = False
    judge_criteria: str = ""
    early_stop_signal: str = ""

    def __post_init__(self) -> None:
        if self.rounds < 1:
            raise ValueError("rounds must be >= 1")


# Type alias for the discriminated union of all strategy configs.
StrategyConfig = Union[SingleConfig, ParallelConfig, CompetitiveConfig, DebateConfig]


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
    timeout: int | None = None
    strategy_config: StrategyConfig | None = None  # type: ignore[type-arg]

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
    timeout: int | None = None,
) -> dict:
    """Build a task spec dict compatible with ``WorkflowTaskSpec``."""
    spec: dict = {
        "local_id": local_id,
        "prompt": prompt,
        "depends_on": list(depends_on),
        "required_tags": list(required_tags),
        "timeout": timeout,
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
# Judge prompt helpers (pure functions)
# ---------------------------------------------------------------------------


def _render_competitive_judge_prompt(
    *,
    template: str,
    context: str,
    scratchpad_prefix: str,
    phase_name: str,
    scorer: str,
) -> str:
    """Render a competitive judge prompt from a user-supplied template.

    Placeholders supported:
    - ``{context}``    — the effective phase context string.
    - ``{solutions}``  — hint pointing to scratchpad prefix where solver
                         outputs are stored.
    - ``{criteria}``   — the ``scorer`` field value (e.g. ``"llm_judge"``).

    Unknown placeholders are left as-is by using a ``defaultdict`` fallback
    in ``str.format_map`` — no ``KeyError`` is raised for missing keys.

    Design reference: DESIGN.md §10.64 (v1.1.32)
    """
    solutions_hint = (
        f"Read solver outputs from scratchpad prefix '{scratchpad_prefix}' "
        f"(keys: '{scratchpad_prefix}/{phase_name}/0', "
        f"'{scratchpad_prefix}/{phase_name}/1', …)"
    )

    # Use defaultdict so that unknown {placeholders} degrade to empty string
    # rather than raising KeyError (safe against partial templates).
    substitutions: dict[str, str] = defaultdict(str, {
        "context": context,
        "solutions": solutions_hint,
        "criteria": scorer,
    })
    return template.format_map(substitutions)


def _build_debate_judge_early_stop_instruction(early_stop_signal: str) -> str:
    """Return the early-stop instruction paragraph for a debate judge prompt.

    When ``early_stop_signal`` is non-empty, the judge is instructed to
    write the signal keyword to the scratchpad if consensus is detected,
    enabling the orchestrator to skip remaining rounds.

    Design reference: DESIGN.md §10.64 (v1.1.32)
    Research: arXiv 2510.12697 (Adaptive Stability Detection in MAD);
    ICLR 2025 MAD blog (convergence-based stopping).
    """
    return (
        f"\n## Early-Stop Protocol\n"
        f"If you detect that the debate has reached consensus and further rounds\n"
        f"would not improve the outcome, write the following signal keyword\n"
        f"verbatim as the **last line** of your scratchpad output:\n\n"
        f"    {early_stop_signal}\n\n"
        f"The orchestrator monitors the scratchpad for this signal and will\n"
        f"skip any remaining rounds when it is detected."
    )


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
            timeout=phase.timeout,
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
                    timeout=phase.timeout,
                )
            )
            local_ids.append(local_id)

        ps = WorkflowPhaseStatus(name=phase.name, pattern="parallel", task_ids=local_ids)
        return tasks, [ps]


class CompetitiveStrategy:
    """Expand a ``competitive`` phase into N independent tasks.

    The distinction from ``parallel`` is semantic: all agents solve the *same*
    problem and a subsequent judge phase selects the best result.

    When ``PhaseSpec.strategy_config`` is a :class:`CompetitiveConfig` with a
    non-empty ``judge_prompt_template``, an additional judge task is appended
    that depends on all solver tasks.  The template is rendered with
    ``str.format_map`` using the following placeholders:

    - ``{context}`` — the phase context string.
    - ``{solutions}`` — a hint pointing to the scratchpad prefix.
    - ``{criteria}`` — the ``scorer`` field value from :class:`CompetitiveConfig`.

    Unknown placeholders are left as-is (defaultdict fallback) and do not
    raise ``KeyError``.

    Design reference: DESIGN.md §10.64 (v1.1.32)
    Research: Monte Carlo Data "LLM-As-Judge: 7 Best Practices";
    arXiv 2504.17087 (meta-judge rubric pipeline).
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
                    timeout=phase.timeout,
                )
            )
            local_ids.append(local_id)

        # When strategy_config carries a judge_prompt_template, append a judge task.
        if (
            phase.strategy_config is not None
            and isinstance(phase.strategy_config, CompetitiveConfig)
            and phase.strategy_config.judge_prompt_template
        ):
            judge_id = f"phase_{phase.name}_judge"
            judge_prompt = _render_competitive_judge_prompt(
                template=phase.strategy_config.judge_prompt_template,
                context=context,
                scratchpad_prefix=scratchpad_prefix,
                phase_name=phase.name,
                scorer=phase.strategy_config.scorer,
            )
            tasks.append(
                _make_task_spec(
                    judge_id,
                    judge_prompt,
                    depends_on=list(local_ids),
                    required_tags=tags,
                    timeout=phase.timeout,
                )
            )
            local_ids.append(judge_id)

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
                _make_task_spec(
                    adv_id, adv_prompt, depends_on=current_deps,
                    required_tags=advocate_tags, timeout=phase.timeout,
                )
            )
            all_local_ids.append(adv_id)

            crit_id = f"phase_{phase.name}_critic_r{r}"
            crit_prompt = _phase_prompt(
                phase.name, "debate", context, role="critic", round_num=r,
                scratchpad_prefix=scratchpad_prefix,
            )
            tasks.append(
                _make_task_spec(
                    crit_id, crit_prompt, depends_on=[adv_id],
                    required_tags=critic_tags, timeout=phase.timeout,
                )
            )
            all_local_ids.append(crit_id)

            current_deps = [crit_id]

        judge_id = f"phase_{phase.name}_judge"
        judge_prompt = _phase_prompt(
            phase.name, "debate", context, role="judge", scratchpad_prefix=scratchpad_prefix,
        )

        # Append early-stop instruction when DebateConfig.early_stop_signal is set.
        if (
            phase.strategy_config is not None
            and isinstance(phase.strategy_config, DebateConfig)
            and phase.strategy_config.early_stop_signal
        ):
            judge_prompt += _build_debate_judge_early_stop_instruction(
                phase.strategy_config.early_stop_signal
            )

        tasks.append(
            _make_task_spec(
                judge_id, judge_prompt, depends_on=current_deps,
                required_tags=judge_tags, timeout=phase.timeout,
            )
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


# ---------------------------------------------------------------------------
# Canonical expand_phases helpers (domain layer)
# ---------------------------------------------------------------------------


def _terminal_ids_domain(phase: PhaseSpec, tasks: list[dict]) -> list[str]:
    """Return the IDs of tasks that have no dependents within this phase.

    For single / parallel / competitive: all tasks are terminal.
    For debate: only the judge task is terminal.
    """
    if phase.pattern == "debate":
        return [tasks[-1]["local_id"]]
    return [t["local_id"] for t in tasks]


def expand_phases_from_specs(
    phases: list[PhaseSpec],
    *,
    context: str,
    scratchpad_prefix: str = "",
) -> list[dict]:
    """Canonical domain-layer phase expansion.

    Translates a list of :class:`PhaseSpec` objects into task spec dicts.
    Each phase's ``timeout`` is propagated to every generated task spec.

    This function mirrors :func:`~tmux_orchestrator.phase_executor.expand_phases`
    but lives in the domain layer (no infrastructure imports).

    Parameters
    ----------
    phases:
        Ordered list of phase specifications.
    context:
        Global workflow context string embedded in every task prompt.
        Overridden per-phase by ``PhaseSpec.context``.
    scratchpad_prefix:
        Prefix for scratchpad keys embedded in prompts.
    """
    all_tasks: list[dict] = []
    prior_terminal_ids: list[str] = []

    for phase in phases:
        effective_context = phase.context if phase.context is not None else context
        strategy = get_strategy(phase.pattern)
        new_tasks, _ = strategy.expand(phase, prior_terminal_ids, effective_context, scratchpad_prefix)
        all_tasks.extend(new_tasks)
        prior_terminal_ids = _terminal_ids_domain(phase, new_tasks)

    return all_tasks
