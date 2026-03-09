"""FastAPI web application — REST endpoints + WebSocket hub."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from tmux_orchestrator.webhook_manager import WebhookManager

import webauthn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel
from webauthn.helpers.structs import (
    AuthenticationCredential,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    RegistrationCredential,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from tmux_orchestrator.agents.base import Task
from tmux_orchestrator.bus import Message, MessageType
from tmux_orchestrator.config import AgentRole
from tmux_orchestrator.episode_store import EpisodeNotFoundError, EpisodeStore
from tmux_orchestrator.schemas import Episode, EpisodeCreate
from tmux_orchestrator.web.ws import WebSocketHub

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class TaskSubmit(BaseModel):
    prompt: str
    priority: int = 0
    metadata: dict[str, Any] = {}
    reply_to: str | None = None  # agent_id that receives the RESULT in its mailbox
    target_agent: str | None = None  # when set, task is only dispatched to this agent
    # Capability tags: ALL tags must be present in the target agent's tags list.
    # Reference: FIPA Directory Facilitator (2002); Kubernetes nodeSelector.
    required_tags: list[str] = []
    # Named agent group: when set, task is only dispatched to agents in that group.
    # Acts as AND-filter with required_tags.
    # Reference: Kubernetes Node Pools; AWS Auto Scaling Groups; DESIGN.md §10.26 (v0.31.0)
    target_group: str | None = None
    # Per-task retry count: how many times to re-enqueue on failure before DLQ.
    # Reference: AWS SQS maxReceiveCount; Netflix Hystrix; DESIGN.md §10.21 (v0.26.0)
    max_retries: int = 0
    # Task-level dependency list: global task IDs that must complete before this
    # task is dispatched.  Tasks with unmet deps are held in _waiting_tasks.
    # Reference: GNU Make prerequisites; Dask task graph; DESIGN.md §10.24 (v0.29.0)
    depends_on: list[str] = []
    # Priority inheritance: when True (default), child task priority = min(own, parent).
    # Prevents high-priority dependent tasks from being delayed by lower-priority work.
    # Reference: Liu & Layland JACM (1973); DESIGN.md §10.27 (v0.32.0)
    inherit_priority: bool = True
    # TTL (Time-to-Live) in seconds.  None = never expires (default).
    # When set, the task is automatically expired *ttl* seconds after submission
    # if it has not yet been dispatched to an agent (queued) or if it is still
    # waiting for dependency resolution (waiting).
    # Reference: RabbitMQ TTL; Azure Service Bus; DESIGN.md §10.28 (v0.33.0)
    ttl: float | None = None


class TaskBatchItem(BaseModel):
    """A single item in a POST /tasks/batch request.

    Extends :class:`TaskSubmit` with an optional ``local_id`` so that tasks
    within the same batch can declare dependencies on each other by local name.
    ``local_id`` references are resolved to global task IDs before the tasks
    are submitted to the orchestrator.

    Design reference:
    - Apache Airflow: DAG nodes referenced by ``task_id`` within a DAG
    - AWS Step Functions: states referenced by name within a state machine
    - Tomasulo's algorithm: register renaming == local_id → global_task_id
    - DESIGN.md §10.24 (v0.29.0)
    """

    local_id: str | None = None  # optional caller-defined name for intra-batch deps
    prompt: str
    priority: int = 0
    metadata: dict[str, Any] = {}
    reply_to: str | None = None
    target_agent: str | None = None
    required_tags: list[str] = []
    target_group: str | None = None
    max_retries: int = 0
    # depends_on may reference: global task IDs OR sibling local_ids in this batch.
    # Sibling local_ids are resolved to global IDs at submission time.
    depends_on: list[str] = []
    # TTL in seconds; None = use orchestrator default_task_ttl (or never expires).
    ttl: float | None = None


class TaskBatchSubmit(BaseModel):
    """Request body for POST /tasks/batch."""

    tasks: list[TaskBatchItem]


class AgentKillResponse(BaseModel):
    agent_id: str
    stopped: bool


class SendMessage(BaseModel):
    type: str = "PEER_MSG"
    payload: dict[str, Any] = {}


class SpawnAgent(BaseModel):
    parent_id: str
    template_id: str


class DynamicAgentCreate(BaseModel):
    """Request body for POST /agents/new — template-free dynamic agent creation.

    Allows a Director (or operator) to add a new agent at runtime without
    any pre-configured YAML entry.  All fields are optional; sensible defaults
    are applied by the orchestrator.
    """

    agent_id: str | None = None
    tags: list[str] = []
    system_prompt: str | None = None
    isolate: bool = True
    merge_on_stop: bool = False
    merge_target: str | None = None
    command: str | None = None
    role: str = "worker"
    task_timeout: int | None = None
    parent_id: str | None = None


class DirectorChat(BaseModel):
    message: str


class ScratchpadWrite(BaseModel):
    """Request body for PUT /scratchpad/{key}."""

    value: Any


class TaskPriorityUpdate(BaseModel):
    """Request body for PATCH /tasks/{task_id}."""

    priority: int


class RateLimitUpdate(BaseModel):
    """Request body for PUT /rate-limit.

    Set ``rate=0`` to disable rate limiting (unlimited throughput).
    """

    rate: float
    burst: int = 0


class WebhookCreate(BaseModel):
    """Request body for POST /webhooks.

    Reference: GitHub Webhooks; Stripe Webhooks; DESIGN.md §10.25 (v0.30.0).
    """

    url: str
    events: list[str]
    secret: str | None = None


class TaskCompleteBody(BaseModel):
    """Optional request body for POST /agents/{agent_id}/task-complete.

    Sent by the Claude Code Stop hook when the agent finishes a turn.

    Design reference:
    - Claude Code Hooks Reference https://code.claude.com/docs/en/hooks (2025)
    - DESIGN.md §10.12 (v0.38.0)
    """

    output: str = ""
    exit_code: int = 0


class ChangeStrategyRequest(BaseModel):
    """Request body for POST /agents/{agent_id}/change-strategy.

    Allows an agent to autonomously request a change in execution strategy
    for its current (or next) task phase.  The orchestrator fulfills the
    request by spawning parallel sub-tasks and routing results back to the
    requesting agent.

    Attributes
    ----------
    pattern:
        Execution strategy to switch to.  Only ``single``, ``parallel``, and
        ``competitive`` are supported in v0.49.0.  ``debate`` may be added in
        a future iteration.
    count:
        Number of parallel workers to spawn (``parallel`` / ``competitive``
        patterns only).  Must be between 1 and 10 inclusive.
    tags:
        Optional ``required_tags`` list for dispatching the spawned tasks.
    context:
        Prompt context for the spawned tasks.  When provided, the orchestrator
        immediately submits ``count`` tasks with this context.  When omitted,
        only the strategy preference is recorded.
    reply_to:
        Agent ID that collects the results of spawned tasks.  Typically set to
        the requesting agent's own ID so it can aggregate outcomes.

    Design references:
    - §12「ワークフロー設計の層構造」層3 実行方式の自律切り替え
    - arXiv:2505.19591 (Evolving Orchestration 2025): dynamic strategy adaptation
    - ALAS arXiv:2505.12501 (2025): orchestrator escalation pattern
    - DESIGN.md §10.16 (v0.49.0)
    """

    pattern: str
    count: int = 2
    tags: list[str] = []
    context: str | None = None
    reply_to: str | None = None

    from pydantic import field_validator

    @field_validator("pattern")
    @classmethod
    def pattern_must_be_valid(cls, v: str) -> str:
        valid = {"single", "parallel", "competitive"}
        if v not in valid:
            raise ValueError(
                f"pattern must be one of {sorted(valid)!r}. "
                "'debate' strategy is planned for a future release."
            )
        return v

    @field_validator("count")
    @classmethod
    def count_must_be_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("count must be >= 1")
        if v > 10:
            raise ValueError("count must be <= 10 (safety limit)")
        return v


class AutoScalerUpdate(BaseModel):
    """Request body for PUT /orchestrator/autoscaler.

    All fields are optional — only supplied fields are updated.
    """

    min: int | None = None
    max: int | None = None
    threshold: int | None = None
    cooldown: float | None = None


class GroupCreate(BaseModel):
    """Request body for POST /groups.

    Creates a named agent group (logical pool).  Tasks may declare
    ``target_group`` to restrict dispatch to group members.

    Design reference: Kubernetes Node Pools; AWS Auto Scaling Groups;
    Apache Mesos Roles; DESIGN.md §10.26 (v0.31.0).
    """

    name: str
    agent_ids: list[str] = []


class GroupAddAgent(BaseModel):
    """Request body for POST /groups/{name}/agents."""

    agent_id: str


class WorkflowTaskSpec(BaseModel):
    """A single task node in a workflow DAG submission.

    ``local_id`` is a caller-defined name used to express dependencies
    within this submission.  It is translated to a global orchestrator
    task ID by ``POST /workflows`` before the tasks are enqueued.

    Design reference:
    - Apache Airflow: DAG nodes identified by ``task_id`` strings
    - AWS Step Functions: states referenced by name within a state machine
    - Tomasulo's algorithm: register renaming == local_id → global_task_id
    - DESIGN.md §10.20 (v0.25.0)
    """

    local_id: str
    prompt: str
    depends_on: list[str] = []
    target_agent: str | None = None
    required_tags: list[str] = []
    target_group: str | None = None
    priority: int = 0
    # Per-task retry count: how many times to re-enqueue on failure before DLQ.
    # Reference: AWS SQS maxReceiveCount; Netflix Hystrix; DESIGN.md §10.21 (v0.26.0)
    max_retries: int = 0
    # Priority inheritance: when True (default), child task priority = min(own, parent).
    # Reference: Liu & Layland JACM (1973); DESIGN.md §10.27 (v0.32.0)
    inherit_priority: bool = True
    # TTL in seconds; None = use orchestrator default_task_ttl (or never expires).
    ttl: float | None = None


class AgentSelectorModel(BaseModel):
    """Agent selector for a workflow phase.

    Attributes
    ----------
    tags:
        ``required_tags`` constraint applied to task dispatch.
    count:
        Number of parallel agent slots (used by ``parallel`` and ``competitive``
        patterns, and for the advocate/critic role in ``debate``).
    target_agent:
        Force-dispatch to a specific agent ID.
    target_group:
        Restrict to agents in the named group.

    Design reference: DESIGN.md §10.15 (v0.48.0)
    """

    tags: list[str] = []
    count: int = 1
    target_agent: str | None = None
    target_group: str | None = None


class PhaseSpecModel(BaseModel):
    """A single phase in a declarative workflow submission.

    Attributes
    ----------
    name:
        Human-readable phase label.
    pattern:
        Execution strategy: ``single`` | ``parallel`` | ``competitive`` | ``debate``.
    agents:
        Agent selector for the primary role.
    critic_agents:
        Agent selector for the critic role (debate only).
    judge_agents:
        Agent selector for the judge role (debate only).
    debate_rounds:
        Number of advocate/critic rounds (debate only, default 1).
    context:
        Optional per-phase context override.

    Design references:
    - arXiv:2512.19769 (PayPal DSL 2025): declarative phase → task expansion
    - §12「ワークフロー設計の層構造」層1・2・3
    - DESIGN.md §10.15 (v0.48.0)
    """

    name: str
    pattern: str
    agents: AgentSelectorModel = AgentSelectorModel()
    critic_agents: AgentSelectorModel = AgentSelectorModel()
    judge_agents: AgentSelectorModel = AgentSelectorModel()
    debate_rounds: int = 1
    context: str | None = None
    required_tags: list[str] = []

    from pydantic import field_validator

    @field_validator("pattern")
    @classmethod
    def pattern_must_be_valid(cls, v: str) -> str:
        valid = {"single", "parallel", "competitive", "debate"}
        if v not in valid:
            raise ValueError(f"pattern must be one of {sorted(valid)!r}")
        return v


class WorkflowSubmit(BaseModel):
    """Request body for POST /workflows.

    Supports two mutually exclusive submission modes:

    1. **tasks= (legacy)**: Submit a raw DAG of :class:`WorkflowTaskSpec` nodes.
       Backward-compatible with the original ``POST /workflows`` API.

    2. **phases= (new)**: Submit a declarative list of :class:`PhaseSpecModel`
       objects.  The server expands each phase into task specs and builds a
       DAG automatically.

    Exactly one of ``tasks`` or ``phases`` must be provided.  Providing neither
    raises HTTP 422.

    Design references:
    - arXiv:2512.19769 (PayPal DSL 2025): declarative pattern reduces dev time 60%
    - §12「ワークフロー設計の層構造」層1 宣言的モード
    - DESIGN.md §10.15 (v0.48.0)
    """

    name: str = "workflow"
    tasks: list[WorkflowTaskSpec] | None = None
    phases: list[PhaseSpecModel] | None = None
    context: str = ""
    task_timeout: int | None = None

    from pydantic import model_validator

    @model_validator(mode="after")
    def tasks_or_phases_required(self) -> "WorkflowSubmit":
        if not self.tasks and not self.phases:
            raise ValueError("Either 'tasks' or 'phases' must be provided")
        if self.tasks and self.phases:
            raise ValueError("Provide either 'tasks' or 'phases', not both")
        return self


class TddWorkflowSubmit(BaseModel):
    """Request body for POST /workflows/tdd — 3-agent TDD workflow.

    Submits a Red→Green→Refactor Workflow DAG with three context-isolated
    sub-agents:
      1. ``test-writer``: writes failing tests for *feature* (RED phase)
      2. ``implementer``: reads tests from the scratchpad and writes the
         minimal implementation that makes them pass (GREEN phase)
      3. ``refactorer``: reviews the implementation and improves code quality
         while keeping tests green (REFACTOR phase)

    Artifacts are passed via the shared scratchpad (Blackboard pattern).  The
    ``test-writer`` writes the test file path to
    ``{scratchpad_prefix}/tests_path``; the ``implementer`` reads it and
    writes the implementation path to ``{scratchpad_prefix}/impl_path``; the
    ``refactorer`` reads both.

    Design references:
    - TDFlow arXiv:2510.23761 (2025): context-isolated sub-agents achieve
      88.8% on SWE-Bench Lite.
    - alexop.dev "Forcing Claude Code to TDD" (2025): context isolation is
      mandatory for genuine test-first development.
    - Blackboard pattern (Buschmann 1996): shared scratchpad decouples
      producers from consumers.
    - DESIGN.md §10.31 (v0.36.0)
    """

    feature: str
    language: str = "python"
    # Optional per-phase required_tags for agent capability routing
    test_writer_tags: list[str] = []
    implementer_tags: list[str] = []
    refactorer_tags: list[str] = []
    # When set, the refactorer RESULT is routed to this agent's mailbox
    reply_to: str | None = None

    from pydantic import field_validator

    @field_validator("feature")
    @classmethod
    def feature_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("feature must not be empty")
        return v


class DebateWorkflowSubmit(BaseModel):
    """Request body for POST /workflows/debate — 3-role multi-round debate workflow.

    Submits an Advocate/Critic/Judge Workflow DAG with structured argumentation:

    For each round 1..max_rounds:
      - ``advocate_r{n}``: builds or refines the affirmative argument
      - ``critic_r{n}``: challenges the argument (Devil's Advocate)
    Final:
      - ``judge``: synthesizes all rounds and writes ``DECISION.md`` to scratchpad

    Artifacts are passed via the shared scratchpad (Blackboard pattern):
      ``{scratchpad_prefix}_r{n}_advocate`` — advocate's argument for round n
      ``{scratchpad_prefix}_r{n}_critic``   — critic's rebuttal for round n
      ``{scratchpad_prefix}_decision``      — judge's final decision

    Design references:
    - Du et al. "Improving Factuality and Reasoning in Language Models through
      Multiagent Debate" ICML 2024 (arXiv:2305.14325): multi-agent debate
      significantly improves factuality and reasoning.
    - DEBATE: Devil's Advocate-Based Assessment ACL 2024 (arXiv:2405.09935):
      Commander + Scorer + Critic structure; terminates when critic outputs
      "NO ISSUE" or max iterations reached.
    - ChatEval ICLR 2024 (arXiv:2308.07201): role diversity (different
      role_descriptions) is the most critical factor in debate quality.
    - DESIGN.md §10.32 (v0.37.0)
    """

    topic: str
    max_rounds: int = 2
    # Optional per-role required_tags for agent capability routing
    advocate_tags: list[str] = []
    critic_tags: list[str] = []
    judge_tags: list[str] = []
    # When set, the judge RESULT is routed to this agent's mailbox
    reply_to: str | None = None

    from pydantic import field_validator

    @field_validator("topic")
    @classmethod
    def topic_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("topic must not be empty")
        return v

    @field_validator("max_rounds")
    @classmethod
    def max_rounds_must_be_valid(cls, v: int) -> int:
        if v < 1 or v > 3:
            raise ValueError("max_rounds must be between 1 and 3")
        return v


class AdrWorkflowSubmit(BaseModel):
    """Request body for POST /workflows/adr — Architecture Decision Record auto-generation.

    Submits a Proposer → Reviewer → Synthesizer Workflow DAG that produces a
    MADR-format DECISION.md via the shared scratchpad (Blackboard pattern):

      - ``{scratchpad_prefix}_proposal``: proposer's analysis of options
      - ``{scratchpad_prefix}_review``:   reviewer's technical critique
      - ``{scratchpad_prefix}_decision``: synthesizer's final MADR DECISION.md

    Design references:
    - AgenticAKM arXiv:2602.04445 (2026): Extractor/Retriever/Generator/Validator
      multi-agent decomposition improves ADR quality over single-LLM calls.
    - Ochoa et al. arXiv:2507.05981 "MAD for Requirements Engineering" (RE 2025):
      multi-agent debate enhances requirements classification accuracy.
    - MADR 4.0.0 (2024-09-17): Markdown Architectural Decision Records standard format.
    - DESIGN.md §10.14 (v0.40.0)
    """

    topic: str
    # Optional per-role required_tags for agent capability routing
    proposer_tags: list[str] = []
    reviewer_tags: list[str] = []
    synthesizer_tags: list[str] = []
    # When set, the synthesizer RESULT is routed to this agent's mailbox
    reply_to: str | None = None

    from pydantic import field_validator

    @field_validator("topic")
    @classmethod
    def topic_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("topic must not be empty")
        return v


class DelphiWorkflowSubmit(BaseModel):
    """Request body for POST /workflows/delphi — multi-round Delphi consensus workflow.

    Submits a Delphi-style multi-round expert consensus DAG.  For each round,
    *N* expert agents (3–5 personas such as security / performance / maintainability)
    independently submit opinions to the scratchpad in **parallel**, then a
    **moderator** agent reads all opinions, synthesises them, and writes feedback.
    After ``max_rounds``, a final **consensus** agent reads all moderator summaries
    and writes ``consensus.md``.

    Scratchpad keys (Blackboard pattern):

    - ``{scratchpad_prefix}_r{n}_{expert}`` — expert *expert*'s opinion in round *n*
    - ``{scratchpad_prefix}_r{n}_moderator`` — moderator's synthesis for round *n*
    - ``{scratchpad_prefix}_consensus``      — final consensus document

    Artifacts produced by agents:

    - ``expert_{persona}_r{n}.md`` — per-expert opinion file (in expert worktree)
    - ``delphi_round_{n}.md``      — moderator's round summary (in moderator worktree)
    - ``consensus.md``             — final consensus (in consensus agent worktree)

    Design references:

    - DelphiAgent (ScienceDirect 2025): multiple LLM agents emulate the Delphi
      method, reaching consensus through iterative feedback and synthesis.
      https://www.sciencedirect.com/science/article/abs/pii/S0306457325001827
    - RT-AID (ScienceDirect 2025): Real-Time AI Delphi — AI-assisted opinions
      accelerate convergence in the Delphi process.
      https://www.sciencedirect.com/science/article/pii/S0016328725001661
    - Du et al. "Improving Factuality and Reasoning in Language Models through
      Multiagent Debate" ICML 2024 (arXiv:2305.14325): even if all agents are
      wrong in round 1, debate across rounds converges to correct answer.
    - CONSENSAGENT ACL 2025: sycophancy-mitigation prompts improve consensus
      quality while maintaining efficiency.
      https://aclanthology.org/2025.findings-acl.1141/
    - DESIGN.md §10.22 (v1.0.23)
    """

    topic: str
    experts: list[str] = ["security", "performance", "maintainability"]
    max_rounds: int = 2
    # Optional per-role required_tags for agent capability routing
    expert_tags: list[str] = []
    moderator_tags: list[str] = []
    # When set, the consensus RESULT is routed to this agent's mailbox
    reply_to: str | None = None

    from pydantic import field_validator

    @field_validator("topic")
    @classmethod
    def topic_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("topic must not be empty")
        return v

    @field_validator("experts")
    @classmethod
    def experts_must_be_valid(cls, v: list[str]) -> list[str]:
        import re as _re
        if len(v) < 2:
            raise ValueError("experts must contain at least 2 personas")
        if len(v) > 5:
            raise ValueError("experts must contain at most 5 personas")
        for expert in v:
            if not expert.strip():
                raise ValueError("expert persona names must not be empty")
            if expert != expert.strip():
                raise ValueError(
                    f"expert persona name {expert!r} must not have leading/trailing whitespace"
                )
            if not _re.match(r"^[a-zA-Z0-9_-]+$", expert):
                raise ValueError(
                    f"expert persona name {expert!r} must contain only "
                    "alphanumeric characters, hyphens, and underscores"
                )
        if len(v) != len(set(v)):
            raise ValueError("expert persona names must be unique")
        return v

    @field_validator("max_rounds")
    @classmethod
    def max_rounds_must_be_valid(cls, v: int) -> int:
        if v < 1 or v > 3:
            raise ValueError("max_rounds must be between 1 and 3")
        return v


class RedBlueWorkflowSubmit(BaseModel):
    """Request body for POST /workflows/redblue — Red Team / Blue Team adversarial evaluation.

    Submits a 3-agent adversarial evaluation DAG:

      1. ``blue_team``: constructs a design, implementation plan, or proposal for
         *topic* and stores it in ``{scratchpad_prefix}_blue_design``.
      2. ``red_team``: reads the blue-team output and attacks it from an adversarial
         perspective — identifying vulnerabilities, flaws, and risks.  Stores
         findings in ``{scratchpad_prefix}_red_findings``.
      3. ``arbiter``: reads both artifacts and produces a balanced risk assessment
         report with prioritised recommendations.  Stores result in
         ``{scratchpad_prefix}_risk_report``.

    Design references:
    - Harrasse et al. "Debate, Deliberate, Decide (D3)" arXiv:2410.04663 (2026):
      adversarial multi-agent evaluation reduces positional/verbosity bias.
    - "Red-Teaming LLM Multi-Agent Systems via Communication Attacks" ACL 2025
      (arXiv:2502.14847): structured adversarial evaluation improves system robustness.
    - Farzulla, "Autonomous Red Team and Blue Team AI" DISSENSUS DAI-2513 (2025):
      pairing adversarial + defensive agents produces realistic security assessments.
    - DESIGN.md §10.23 (v1.0.24)
    """

    topic: str
    # Optional per-role required_tags for agent capability routing
    blue_tags: list[str] = []
    red_tags: list[str] = []
    arbiter_tags: list[str] = []
    # When set, the arbiter RESULT is routed to this agent's mailbox
    reply_to: str | None = None

    from pydantic import field_validator

    @field_validator("topic")
    @classmethod
    def topic_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("topic must not be empty")
        return v


class SocraticWorkflowSubmit(BaseModel):
    """Request body for POST /workflows/socratic — Socratic dialogue workflow.

    Submits a 3-agent Socratic dialogue DAG that probes assumptions, refines
    definitions, and extracts a structured conclusion via the shared scratchpad
    (Blackboard pattern):

      - ``{scratchpad_prefix}_dialogue``:  questioner/responder exchange log
      - ``{scratchpad_prefix}_synthesis``: synthesizer's structured conclusion

    Pipeline (strictly sequential):

      1. **questioner**: applies the Maieutic method — challenges assumptions,
         demands precise definitions, and probes the logical basis of the
         topic.  Starts with adversarial questions and shifts toward integrative
         ones.  Stores the full Q&A log in the scratchpad.
      2. **responder**: reads the questioner's output and elaborates, defends,
         or revises the position in response to each question.  Appends
         answers to the dialogue log.
      3. **synthesizer**: reads the complete dialogue and produces a structured
         ``synthesis.md`` with main arguments, agreed points, unresolved
         questions, and recommendations.

    Design references:
    - Liang et al. "SocraSynth" arXiv:2402.06634 (2024): staged
      questioner → responder → synthesizer with sycophancy suppression.
    - "KELE: Knowledge-Enhanced LLM for Socratic Teaching" arXiv:2409.05511
      EMNLP 2025: two-phase questioning (adversarial → constructive).
    - "CONSENSAGENT" ACL 2025: dynamic prompt refinement reduces sycophancy.
    - DESIGN.md §10.24 (v1.0.25)
    """

    topic: str
    # Optional per-role required_tags for agent capability routing
    questioner_tags: list[str] = []
    responder_tags: list[str] = []
    synthesizer_tags: list[str] = []
    # When set, the synthesizer RESULT is routed to this agent's mailbox
    reply_to: str | None = None

    from pydantic import field_validator

    @field_validator("topic")
    @classmethod
    def topic_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("topic must not be empty")
        return v


class PairWorkflowSubmit(BaseModel):
    """Request body for POST /workflows/pair — PairCoder (Navigator + Driver) workflow.

    Submits a 2-agent Pair Programming Workflow DAG modelled on the Navigator /
    Driver pattern from Extreme Programming (Beck & Fowler 1999):

      - **navigator**: reads the task description, produces a structured
        ``PLAN.md`` (architecture, interfaces, acceptance criteria, step-by-step
        implementation guide) and stores it in the shared scratchpad.
      - **driver**: reads the navigator's PLAN.md, implements the code, writes
        tests, runs them, and stores the implementation summary in the scratchpad.

    Scratchpad keys (Blackboard pattern):

    - ``{scratchpad_prefix}_plan``   : navigator's PLAN.md content
    - ``{scratchpad_prefix}_result`` : driver's implementation summary

    Artifacts produced by agents:

    - ``PLAN.md``           — navigator's structured plan (in navigator worktree)
    - ``<impl_file>.py``    — driver's implementation (in driver worktree)
    - ``test_<impl>.py``    — driver's tests (in driver worktree)
    - ``driver_summary.md`` — driver's completion summary (in driver worktree)

    Design references:
    - Beck & Fowler "Extreme Programming Explained" (1999): Navigator/Driver roles.
    - FlowHunt "TDD with AI Agents" (2025): PairCoder improves code quality vs
      single-agent baseline.
    - Tweag "Agentic Coding Handbook — TDD" (2025): context-separated pair
      programming approach.
    - DESIGN.md §10.27 (v1.0.27)
    """

    task: str
    # Optional per-role required_tags for agent capability routing
    navigator_tags: list[str] = []
    driver_tags: list[str] = []
    # When set, the driver's RESULT is routed to this agent's mailbox
    reply_to: str | None = None

    from pydantic import field_validator

    @field_validator("task")
    @classmethod
    def task_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("task must not be empty")
        return v


class FulldevWorkflowSubmit(BaseModel):
    """Request body for POST /workflows/fulldev — Full Software Development Lifecycle.

    Submits a 5-agent sequential pipeline DAG:

      1. ``spec-writer``: writes feature requirements specification (SPEC.md) and
         stores it in ``{scratchpad_prefix}_spec``.
      2. ``architect``: reads spec, writes ADR/design document (DESIGN.md) and
         stores it in ``{scratchpad_prefix}_design``.
      3. ``tdd-test-writer``: reads spec + design, writes failing pytest tests and
         stores them in ``{scratchpad_prefix}_tests``.
      4. ``tdd-implementer``: reads spec + tests, writes implementation that makes
         tests pass, stores it in ``{scratchpad_prefix}_impl``.
      5. ``reviewer``: reads all artifacts, writes code review to
         ``{scratchpad_prefix}_review``.

    All handoffs use the shared scratchpad (Blackboard pattern). Each task
    ``depends_on`` the previous task, forming a linear pipeline.

    Design references:
    - MetaGPT arXiv:2308.00352 (2023/2024): PM → Architect → Engineer SOP pipeline.
    - AgentMesh arXiv:2507.19902 (2025): Planner → Coder → Debugger → Reviewer.
    - arXiv:2508.00083 "Survey on Code Generation with LLM-based Agents" (2025):
      Pipeline-based labor division + Blackboard model for inter-agent handoff.
    - arXiv:2505.16339 "Rethinking Code Review Workflows" (2025): LLM code review
      integrated into automated pipelines.
    - DESIGN.md §10.16 (v0.42.0)
    """

    feature: str
    language: str = "python"
    # Optional per-role required_tags for agent capability routing
    spec_writer_tags: list[str] = []
    architect_tags: list[str] = []
    test_writer_tags: list[str] = []
    implementer_tags: list[str] = []
    reviewer_tags: list[str] = []
    # When set, the reviewer RESULT is routed to this agent's mailbox
    reply_to: str | None = None

    from pydantic import field_validator

    @field_validator("feature")
    @classmethod
    def feature_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("feature must not be empty")
        return v


class CleanArchWorkflowSubmit(BaseModel):
    """Request body for POST /workflows/clean-arch — 4-layer Clean Architecture pipeline.

    Submits a 4-agent sequential pipeline DAG based on Robert C. Martin's Clean
    Architecture (2017) concentric-ring model:

      1. ``domain-designer``: defines domain Entities, Value Objects, Aggregates,
         and Domain Events without any framework dependency; stores result in
         ``{scratchpad_prefix}_domain``.
      2. ``usecase-designer``: reads domain layer; defines Use Cases (Interactors),
         Input/Output DTOs, and Port interfaces; stores in ``{scratchpad_prefix}_usecases``.
      3. ``adapter-designer``: reads domain + use-cases; defines concrete Interface
         Adapters (Repository impls, Presenters, Controllers); stores in
         ``{scratchpad_prefix}_adapters``.
      4. ``framework-designer``: reads all previous layers; writes final
         ``ARCHITECTURE.md`` synthesising the full design plus executable Python
         skeleton showing framework wiring; stores in ``{scratchpad_prefix}_arch``.

    All handoffs use the shared scratchpad (Blackboard pattern). Each task
    ``depends_on`` the previous task, forming a linear pipeline.

    Scratchpad keys use underscores (not slashes) as namespace separator.

    Design references:
    - Robert C. Martin, "Clean Architecture" (2017): Domain → Use Cases →
      Interface Adapters → Frameworks & Drivers concentric-ring model.
    - AgentMesh arXiv:2507.19902 (2025): Planner→Coder→Debugger→Reviewer 4-role
      artifact-centric pipeline for software development automation.
    - Muthu (2025-11) "The Architecture is the Prompt": hexagonal architecture
      boundaries map directly to AI agent context constraints.
    - Marta Fernández García "Applying Hexagonal Architecture in AI Agent
      Development" (Medium, 2025).
    - DESIGN.md §10.30 (v1.0.30)
    """

    feature: str
    language: str = "python"
    # Optional per-role required_tags for agent capability routing
    domain_designer_tags: list[str] = []
    usecase_designer_tags: list[str] = []
    adapter_designer_tags: list[str] = []
    framework_designer_tags: list[str] = []
    # When set, the framework-designer RESULT is routed to this agent's mailbox
    reply_to: str | None = None

    from pydantic import field_validator

    @field_validator("feature")
    @classmethod
    def feature_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("feature must not be empty")
        return v


class DDDWorkflowSubmit(BaseModel):
    """Request body for POST /workflows/ddd — DDD Bounded Context decomposition.

    Submits a 3-phase workflow DAG using Domain-Driven Design patterns:

      Phase 1 (sequential):
        ``context-mapper``: performs EventStorming analysis; identifies Bounded
        Contexts and their Ubiquitous Language; writes ``EVENTSTORMING.md`` and
        ``BOUNDED_CONTEXTS.md``; stores context list in scratchpad.

      Phase 2 (parallel, one agent per Bounded Context):
        ``domain-expert-{context}``: reads ``BOUNDED_CONTEXTS.md``; designs domain
        model (Entities, Aggregates, Value Objects, Domain Services) for its
        assigned context; writes ``DOMAIN_{CONTEXT}.md`` and stores in scratchpad.

      Phase 3 (sequential, depends on ALL domain-expert tasks):
        ``integration-designer``: reads all domain models; produces
        ``CONTEXT_MAP.md`` with explicit context-mapping patterns (Shared Kernel,
        Customer–Supplier, Anti-Corruption Layer) between every pair of contexts.

    All handoffs use the shared scratchpad (Blackboard pattern).
    Scratchpad keys use underscores (not slashes) as namespace separator.

    The ``contexts`` field is optional. When omitted the context-mapper agent
    discovers and names the Bounded Contexts autonomously from the feature
    description. When provided (e.g. ``["Orders", "Inventory", "Shipping"]``),
    those names are used directly.

    Design references:
    - Evans, "Domain-Driven Design" (2003): Bounded Context + Ubiquitous Language
      as the core strategic-design patterns.
    - Brandolini, "Introducing EventStorming" (2021): discovery workshop technique.
    - IJCSE V12I3P102, "Designing Scalable Multi-Agent AI Systems using EventStorming
      and DDD" (2025): EventStorming maps directly to agent communication protocols.
    - Russ Miles, "Domain-Driven Agent Design", Engineering Agents Substack, 2025:
      DICE framework — Bounded Context as LLM agent context constraint.
    - Bakthavachalu, "Applying DDD for Agentic Applications", Medium, 2025:
      Risk / Regulatory / Validation bounded-context decomposition case study.
    - DESIGN.md §10.31 (v1.0.31)
    """

    topic: str
    contexts: list[str] = []
    language: str = "python"
    # Optional per-role required_tags for agent capability routing
    context_mapper_tags: list[str] = []
    domain_expert_tags: list[str] = []
    integration_designer_tags: list[str] = []
    # When set, the integration-designer RESULT is routed to this agent's mailbox
    reply_to: str | None = None

    from pydantic import field_validator

    @field_validator("topic")
    @classmethod
    def topic_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("topic must not be empty")
        return v

    @field_validator("contexts")
    @classmethod
    def contexts_names_must_not_be_blank(cls, v: list) -> list:
        for name in v:
            if not str(name).strip():
                raise ValueError("context names must not be blank")
        return v


class CompetitionWorkflowSubmit(BaseModel):
    """Request body for POST /workflows/competition — Best-of-N competitive solver.

    Submits a (N+1)-agent workflow DAG where N solver agents tackle the same
    problem independently using different strategies, and a single judge agent
    selects the winner:

      **Phase 1 — Parallel Solvers** (``depends_on=[]``, all start simultaneously):
        ``solver_{strategy}`` for each strategy in ``strategies``:
        Each solver receives the same ``problem`` description plus a strategy
        hint.  It writes its solution to ``solver_{strategy}_result.md``,
        extracts a numeric score on a ``SCORE: <number>`` line, and stores the
        result in the shared scratchpad.

      **Phase 2 — Judge** (``depends_on=all solver task IDs``):
        ``judge``: reads all solver results from the scratchpad, compares them
        against ``scoring_criterion``, selects the winner, and writes
        ``COMPETITION_RESULT.md`` containing:
        - ``WINNER: <strategy>``
        - A score table (strategy → score)
        - Rationale for the selection
        The judge stores the result in the shared scratchpad.

    Scratchpad keys (Blackboard pattern):
    - ``{prefix}_solver_{strategy}``  : solver result + score for each strategy
    - ``{prefix}_judge``              : judge's ``COMPETITION_RESULT.md`` content

    Artifacts produced by agents:
    - ``solver_{strategy}_result.md`` — each solver's solution + SCORE line
    - ``COMPETITION_RESULT.md``       — judge's winner declaration

    Design references:
    - "Making, not Taking, the Best of N" (FusioN), arXiv:2510.00931, 2025:
      BoN selection vs. synthesis comparison; list-wise judge evaluation.
    - M-A-P "Multi-Agent-based Parallel Test-Time Scaling", arXiv:2506.12928,
      2025: parallel BoN with list-wise verdict outperforms point-wise.
    - "When AIs Judge AIs: Agent-as-a-Judge", arXiv:2508.02994, 2025:
      agent judge observes intermediate steps and produces structured scores.
    - MultiAgentBench, arXiv:2503.01935, 2025: collaboration + competition
      benchmark demonstrating milestone-based KPIs for competitive agents.
    - DESIGN.md §10.36 (v1.1.0)
    """

    problem: str
    strategies: list[str]
    scoring_criterion: str = "correctness and efficiency"
    # Optional per-role required_tags for agent capability routing
    solver_tags: list[str] = []
    judge_tags: list[str] = []
    # When set, the judge RESULT is routed to this agent's mailbox
    reply_to: str | None = None

    from pydantic import field_validator

    @field_validator("problem")
    @classmethod
    def problem_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("problem must not be empty")
        return v

    @field_validator("strategies")
    @classmethod
    def strategies_must_have_two_to_ten(cls, v: list) -> list:
        if len(v) < 2:
            raise ValueError("strategies must have at least 2 entries")
        if len(v) > 10:
            raise ValueError("strategies must have at most 10 entries")
        for s in v:
            if not str(s).strip():
                raise ValueError("strategy names must not be blank")
        return v


# ---------------------------------------------------------------------------
# Module-level auth state
# ---------------------------------------------------------------------------

_credentials: dict[str, bytes] = {}    # b64url(cred_id) → public_key bytes
_sign_counts: dict[str, int] = {}      # b64url(cred_id) → sign_count
_sessions: dict[str, float] = {}       # session_token  → expiry (unix ts)
_pending_challenge: bytes | None = None
_SESSION_TTL = 86_400  # 24 h

# ---------------------------------------------------------------------------
# Shared scratchpad — in-process key/value store (cleared on restart)
# ---------------------------------------------------------------------------

_scratchpad: dict[str, Any] = {}  # key → arbitrary JSON-serialisable value


def _new_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + _SESSION_TTL
    return token


def _valid_session(token: str | None) -> bool:
    if not token:
        return False
    expiry = _sessions.get(token)
    return expiry is not None and expiry > time.time()


def _request_origin(request: Request) -> str:
    """Derive the WebAuthn expected_origin, respecting X-Forwarded-Proto from proxies."""
    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    return f"{scheme}://{request.url.netloc}"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------


def _make_session_auth():
    async def _check(request: Request) -> None:
        if not _valid_session(request.cookies.get("session")):
            raise HTTPException(401, "Authentication required")
    return _check


def _make_combined_auth(api_key: str):
    """Session cookie OR X-API-Key/query param; both accepted."""
    async def _check(request: Request) -> None:
        if _valid_session(request.cookies.get("session")):
            return
        if api_key:
            provided = (
                request.headers.get("X-API-Key", "")
                or request.query_params.get("key", "")
            )
            if provided == api_key:
                return
        raise HTTPException(401, "Authentication required")
    return _check


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _build_agent_tree(agents: list[dict]) -> list[dict]:
    """Convert a flat list of agent dicts into a nested tree.

    Each dict must have an ``id`` and optional ``parent_id`` key.  Returns a
    list of root nodes (``parent_id is None``), each with a ``children`` key
    that recursively holds child nodes.

    The resulting structure is compatible with d3-hierarchy's ``d3.hierarchy()``
    function (the library expects a tree rooted at a single node, but we expose
    multiple roots as a list for the REST caller to use freely).
    """
    by_id: dict[str, dict] = {}
    for a in agents:
        node = {**a, "children": []}
        by_id[a["id"]] = node

    roots: list[dict] = []
    for node in by_id.values():
        parent_id = node.get("parent_id")
        if parent_id and parent_id in by_id:
            by_id[parent_id]["children"].append(node)
        else:
            roots.append(node)

    return roots


def create_app(
    orchestrator: Any,
    hub: WebSocketHub,
    *,
    api_key: str = "",
    on_startup: Callable[[], Any] | None = None,
    on_shutdown: Callable[[], Any] | None = None,
    cors_origins: list[str] | None = None,
    rate_limit: str = "60/minute",
) -> FastAPI:
    """Create and wire up the FastAPI application.

    Parameters
    ----------
    orchestrator:
        The :class:`~tmux_orchestrator.orchestrator.Orchestrator` instance.
    hub:
        A :class:`WebSocketHub` already connected to the bus.
    on_startup:
        Optional async callable invoked during lifespan startup (after hub).
        Use this to start the orchestrator when using the web server.
        ``router.on_startup`` hooks are NOT called when a ``lifespan`` context
        manager is provided (FastAPI ≥ 0.93 behaviour), so callers must use
        this parameter instead.
    on_shutdown:
        Optional async callable invoked during lifespan shutdown (before hub).
    cors_origins:
        List of allowed CORS origins.  When ``None`` (default), defaults to
        loopback-only: ``["http://localhost", "http://localhost:8000",
        "http://127.0.0.1", "http://127.0.0.1:8000"]``.
        Reference: OWASP CORS cheat sheet; DESIGN.md §10.18 (v0.44.0).
    rate_limit:
        Global rate limit string for SlowAPI (default ``"60/minute"``).
        Applied to all ``POST /tasks`` submissions.
        Reference: SlowAPI docs; DESIGN.md §10.18 (v0.44.0).
    """
    from fastapi.middleware.cors import CORSMiddleware
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    from tmux_orchestrator.security import AuditLogMiddleware

    auth = _make_combined_auth(api_key)

    # Rate limiter (SlowAPI / token bucket)
    # Reference: SlowAPI docs https://slowapi.readthedocs.io/ (2025)
    _limiter = Limiter(key_func=get_remote_address)

    # Effective CORS origins — loopback-only by default
    _cors_origins: list[str] = cors_origins if cors_origins is not None else [
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:8000",
    ]

    @asynccontextmanager
    async def _lifespan(application: FastAPI):  # noqa: ARG001
        await hub.start()
        logger.info("WebSocket hub started")
        # Signal the orchestrator to defer agent process startup so that
        # start_agents() can be called AFTER the server begins accepting
        # requests.  This prevents the SessionStart hook deadlock: agents call
        # POST /agents/{id}/ready via curl, which requires the HTTP server to
        # be up first.
        if hasattr(orchestrator, "_defer_agent_start"):
            orchestrator._defer_agent_start = True
        if on_startup is not None:
            await on_startup()
        # Server is now accepting requests.  Start agent processes in the
        # background so their SessionStart hooks can reach this server.
        _agents_task: asyncio.Task | None = None
        if hasattr(orchestrator, "start_agents"):
            _agents_task = asyncio.create_task(
                orchestrator.start_agents(),
                name="orchestrator-agent-startup",
            )
        yield
        # Cancel in-flight agent startups on shutdown.
        if _agents_task and not _agents_task.done():
            _agents_task.cancel()
            await asyncio.gather(_agents_task, return_exceptions=True)
        if on_shutdown is not None:
            await on_shutdown()
        await hub.stop()
        logger.info("WebSocket hub stopped")

    app = FastAPI(
        title="TmuxAgentOrchestrator",
        description="REST + WebSocket API for the tmux agent orchestrator",
        version="0.1.0",
        lifespan=_lifespan,
    )

    # ------------------------------------------------------------------
    # Security middleware (CORS + Audit log)
    # Reference: DESIGN.md §10.18 (v0.44.0)
    # ------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(AuditLogMiddleware)

    # Rate limiter state + exception handler
    app.state.limiter = _limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Auth endpoints (no auth dependency — public)
    # ------------------------------------------------------------------

    @app.get("/auth/status", include_in_schema=False)
    async def auth_status(request: Request) -> dict:
        return {
            "registered": bool(_credentials),
            "authenticated": _valid_session(request.cookies.get("session")),
        }

    @app.post("/auth/register-options", include_in_schema=False)
    async def auth_register_options(request: Request) -> JSONResponse:
        global _pending_challenge
        rp_id = request.url.hostname
        options = webauthn.generate_registration_options(
            rp_id=rp_id,
            rp_name="TmuxAgentOrchestrator",
            user_id=b"admin",
            user_name="admin",
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        _pending_challenge = options.challenge
        return JSONResponse(json.loads(webauthn.options_to_json(options)))

    @app.post("/auth/register", include_in_schema=False)
    async def auth_register(request: Request) -> JSONResponse:
        global _pending_challenge
        if _pending_challenge is None:
            raise HTTPException(400, "No pending challenge")
        rp_id = request.url.hostname
        origin = _request_origin(request)
        try:
            body = await request.body()
            credential = RegistrationCredential.parse_raw(body)
            verification = webauthn.verify_registration_response(
                credential=credential,
                expected_challenge=_pending_challenge,
                expected_rp_id=rp_id,
                expected_origin=origin,
            )
        except Exception as exc:
            logger.warning("Registration failed: %s", exc)
            raise HTTPException(400, f"Registration failed: {exc}")
        cred_key = _b64url_encode(verification.credential_id)
        _credentials[cred_key] = verification.credential_public_key
        _sign_counts[cred_key] = verification.sign_count
        _pending_challenge = None
        token = _new_session()
        resp = JSONResponse({"status": "ok"})
        resp.set_cookie("session", token, httponly=True, samesite="lax", path="/")
        return resp

    @app.post("/auth/authenticate-options", include_in_schema=False)
    async def auth_authenticate_options(request: Request) -> JSONResponse:
        global _pending_challenge
        rp_id = request.url.hostname
        allow_credentials = [
            PublicKeyCredentialDescriptor(id=_b64url_decode(k))
            for k in _credentials
        ]
        options = webauthn.generate_authentication_options(
            rp_id=rp_id,
            allow_credentials=allow_credentials,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        _pending_challenge = options.challenge
        return JSONResponse(json.loads(webauthn.options_to_json(options)))

    @app.post("/auth/authenticate", include_in_schema=False)
    async def auth_authenticate(request: Request) -> JSONResponse:
        global _pending_challenge
        if _pending_challenge is None:
            raise HTTPException(400, "No pending challenge")
        rp_id = request.url.hostname
        origin = _request_origin(request)
        try:
            body = await request.body()
            credential = AuthenticationCredential.parse_raw(body)
            cred_key = _b64url_encode(credential.raw_id)
            if cred_key not in _credentials:
                raise HTTPException(401, "Unknown credential")
            verification = webauthn.verify_authentication_response(
                credential=credential,
                expected_challenge=_pending_challenge,
                expected_rp_id=rp_id,
                expected_origin=origin,
                credential_public_key=_credentials[cred_key],
                credential_current_sign_count=_sign_counts[cred_key],
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Authentication failed: %s", exc)
            raise HTTPException(400, f"Authentication failed: {exc}")
        _sign_counts[cred_key] = verification.new_sign_count
        _pending_challenge = None
        token = _new_session()
        resp = JSONResponse({"status": "ok"})
        resp.set_cookie("session", token, httponly=True, samesite="lax", path="/")
        return resp

    @app.post("/auth/logout", include_in_schema=False)
    async def auth_logout(request: Request) -> JSONResponse:
        token = request.cookies.get("session")
        if token:
            _sessions.pop(token, None)
        resp = JSONResponse({"status": "ok"})
        resp.delete_cookie("session", path="/")
        return resp

    # ------------------------------------------------------------------
    # REST endpoints
    # ------------------------------------------------------------------

    @app.post("/tasks", summary="Submit a new task", dependencies=[Depends(auth)])
    @_limiter.limit(rate_limit)
    async def submit_task(request: Request, body: TaskSubmit) -> dict:  # noqa: ARG001 (request used by SlowAPI)
        from tmux_orchestrator.security import sanitize_prompt
        task = await orchestrator.submit_task(
            sanitize_prompt(body.prompt),
            priority=body.priority,
            metadata=body.metadata,
            depends_on=body.depends_on or None,
            reply_to=body.reply_to,
            target_agent=body.target_agent,
            required_tags=body.required_tags or None,
            target_group=body.target_group,
            max_retries=body.max_retries,
            inherit_priority=body.inherit_priority,
            ttl=body.ttl,
        )
        result: dict = {
            "task_id": task.id,
            "prompt": task.prompt,
            "priority": task.priority,
            "max_retries": task.max_retries,
            "retry_count": task.retry_count,
            "inherit_priority": task.inherit_priority,
            "submitted_at": task.submitted_at,
            "ttl": task.ttl,
            "expires_at": task.expires_at,
        }
        if task.depends_on:
            result["depends_on"] = task.depends_on
        if task.reply_to is not None:
            result["reply_to"] = task.reply_to
        if task.target_agent is not None:
            result["target_agent"] = task.target_agent
        if task.required_tags:
            result["required_tags"] = task.required_tags
        if task.target_group is not None:
            result["target_group"] = task.target_group
        return result

    @app.post("/tasks/batch", summary="Submit multiple tasks in one request", dependencies=[Depends(auth)])
    async def submit_tasks_batch(body: TaskBatchSubmit) -> dict:
        """Submit a list of tasks atomically.

        All tasks in the batch are validated before any are enqueued.  If the
        request body is malformed, FastAPI returns 422 before this handler runs.

        Design reference:
        - adidas API Guidelines "Batch Operations"
          https://adidas.gitbook.io/api-guidelines/rest-api-guidelines/execution/batch-operations
        - PayPal Batch API (Medium, PayPal Tech Blog)
          https://medium.com/paypal-tech/batch-an-api-to-bundle-multiple-paypal-rest-operations-6af6006e002
        """
        results: list[dict] = []
        # Build local_id → global task_id map for intra-batch dependency resolution.
        # Two-pass approach: first allocate UUIDs, then submit with resolved deps.
        # Reference: Tomasulo's algorithm register renaming; DESIGN.md §10.24 (v0.29.0)
        import uuid as _uuid  # noqa: PLC0415
        local_to_global: dict[str, str] = {}
        # Pre-allocate task IDs for all items that have a local_id
        for item in body.tasks:
            if item.local_id:
                local_to_global[item.local_id] = str(_uuid.uuid4())

        # Now submit each task, resolving local_ids in depends_on to global IDs
        for item in body.tasks:
            # Resolve depends_on: replace local_id refs with global task IDs
            resolved_deps: list[str] = []
            for dep in item.depends_on:
                if dep in local_to_global:
                    resolved_deps.append(local_to_global[dep])
                else:
                    # Assume it is already a global task ID
                    resolved_deps.append(dep)

            # Use the pre-allocated ID if this item has a local_id
            # We submit via a thin wrapper that lets us pass a pre-allocated ID
            task = await orchestrator.submit_task(
                item.prompt,
                priority=item.priority,
                metadata=item.metadata,
                depends_on=resolved_deps or None,
                reply_to=item.reply_to,
                target_agent=item.target_agent,
                required_tags=item.required_tags or None,
                target_group=item.target_group,
                max_retries=item.max_retries,
                ttl=item.ttl,
                _task_id=local_to_global.get(item.local_id) if item.local_id else None,
            )
            record: dict = {
                "task_id": task.id,
                "prompt": task.prompt,
                "priority": task.priority,
                "max_retries": task.max_retries,
                "retry_count": task.retry_count,
                "submitted_at": task.submitted_at,
                "ttl": task.ttl,
                "expires_at": task.expires_at,
            }
            if item.local_id:
                record["local_id"] = item.local_id
            if task.depends_on:
                record["depends_on"] = task.depends_on
            if task.reply_to is not None:
                record["reply_to"] = task.reply_to
            if task.target_agent is not None:
                record["target_agent"] = task.target_agent
            if task.required_tags:
                record["required_tags"] = task.required_tags
            if task.target_group is not None:
                record["target_group"] = task.target_group
            results.append(record)
        return {"tasks": results}

    @app.get("/tasks", summary="List all tasks (active + completed)", dependencies=[Depends(auth)])
    async def list_tasks(skip: int = 0, limit: int = 100) -> list[dict]:
        """Return all tasks: currently queued, in-progress, and completed/failed.

        Combines the pending queue, currently dispatched (in-progress) tasks,
        and per-agent history into a single flat list.  Use ``skip`` and
        ``limit`` query params for pagination.

        Each task record contains at minimum:
        - ``task_id``: unique task identifier
        - ``status``: one of ``"queued"``, ``"in_progress"``, ``"success"``, ``"error"``
        - ``prompt``: task prompt text
        - ``priority``: dispatch priority (lower = higher priority)
        - ``max_retries``: maximum allowed retries
        - ``retry_count``: current retry attempt count

        Design reference:
        - AWS SQS message visibility / dead-letter queue listing
        - DESIGN.md §10.21 (v0.26.0)
        """
        all_tasks: list[dict] = []

        # 1. Pending (queued) and waiting tasks
        # list_tasks() returns both queued and waiting items, each with a "status" field.
        for item in orchestrator.list_tasks():
            task_status = item.get("status", "queued")  # "queued" or "waiting"
            record: dict = {
                "task_id": item["task_id"],
                "prompt": item["prompt"],
                "priority": item["priority"],
                "status": task_status,
                "max_retries": 0,
                "retry_count": 0,
                "submitted_at": item.get("submitted_at"),
                "ttl": item.get("ttl"),
                "expires_at": item.get("expires_at"),
            }
            if item.get("depends_on"):
                record["depends_on"] = item["depends_on"]
            if item.get("required_tags"):
                record["required_tags"] = item["required_tags"]
            if item.get("target_agent"):
                record["target_agent"] = item["target_agent"]
            all_tasks.append(record)

        # Enrich queued tasks with retry fields from _active_tasks if tracked
        queued_ids = {t["task_id"] for t in all_tasks}

        # 2. In-progress tasks (currently being worked on by agents)
        for agent in orchestrator.list_agents():
            agent_obj = orchestrator.get_agent(agent["id"])
            if agent_obj is not None and agent_obj._current_task is not None:
                ct = agent_obj._current_task
                if ct.id not in queued_ids:
                    all_tasks.append({
                        "task_id": ct.id,
                        "prompt": ct.prompt,
                        "priority": ct.priority,
                        "status": "in_progress",
                        "agent_id": agent["id"],
                        "max_retries": ct.max_retries,
                        "retry_count": ct.retry_count,
                        **({"required_tags": ct.required_tags} if ct.required_tags else {}),
                        **({"target_agent": ct.target_agent} if ct.target_agent else {}),
                    })

        # 3. Completed / failed tasks from per-agent history
        seen_task_ids = {t["task_id"] for t in all_tasks}
        for agent in orchestrator.list_agents():
            history = orchestrator.get_agent_history(agent["id"], limit=200) or []
            for record in history:
                tid = record.get("task_id")
                if tid and tid not in seen_task_ids:
                    seen_task_ids.add(tid)
                    # Retrieve retry fields from _active_tasks if still present,
                    # otherwise default to 0 (already cleaned up on success/final failure)
                    active_task = orchestrator._active_tasks.get(tid)
                    all_tasks.append({
                        "task_id": tid,
                        "prompt": record.get("prompt", ""),
                        "priority": 0,
                        "status": record.get("status", "unknown"),
                        "started_at": record.get("started_at"),
                        "finished_at": record.get("finished_at"),
                        "duration_s": record.get("duration_s"),
                        "error": record.get("error"),
                        "agent_id": agent["id"],
                        "max_retries": active_task.max_retries if active_task else 0,
                        "retry_count": active_task.retry_count if active_task else 0,
                    })

        # Apply pagination
        return all_tasks[skip : skip + limit]

    @app.get("/agents", summary="List agents and their status", dependencies=[Depends(auth)])
    async def list_agents() -> list[dict]:
        return orchestrator.list_agents()

    @app.get("/agents/tree", summary="Agent hierarchy as nested tree", dependencies=[Depends(auth)])
    async def agents_tree() -> list[dict]:
        """Return the agent list as a nested JSON tree (d3-hierarchy compatible).

        Each node has: ``id``, ``status``, ``role``, ``parent_id``,
        ``current_task``, ``bus_drops``, ``circuit_breaker``, ``children``.

        The top level of the returned list contains root-level agents
        (``parent_id == None``); each node's ``children`` list recursively
        contains its sub-agents.
        """
        agents = orchestrator.list_agents()
        return _build_agent_tree(agents)

    @app.delete("/agents/{agent_id}", summary="Stop an agent", dependencies=[Depends(auth)])
    async def stop_agent(agent_id: str) -> AgentKillResponse:
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        await agent.stop()
        return AgentKillResponse(agent_id=agent_id, stopped=True)

    @app.post("/agents/{agent_id}/reset", summary="Manually reset an agent from ERROR state", dependencies=[Depends(auth)])
    async def reset_agent(agent_id: str) -> dict:
        """Stop and restart *agent_id*, clearing ERROR and permanently-failed state.

        Use this endpoint when an agent exhausted automatic recovery attempts
        and needs a manual restart.  Returns 404 if the agent is not registered.

        Design note: ``POST /agents/{id}/reset`` follows the action sub-resource
        pattern — an imperative verb endpoint rather than a PUT state replacement.
        Reference: DESIGN.md §11; Nordic APIs "Designing a True REST State Machine".
        """
        try:
            await orchestrator.reset_agent(agent_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        return {"agent_id": agent_id, "reset": True}

    @app.post(
        "/agents/{agent_id}/change-strategy",
        summary="Request an autonomous strategy change for the agent's current phase",
        dependencies=[Depends(auth)],
    )
    async def change_agent_strategy(agent_id: str, body: ChangeStrategyRequest) -> dict:
        """Allow an agent to autonomously change its execution strategy.

        This endpoint implements §12 層3 「実行方式の自律切り替え」: when an agent
        determines that the current ``single`` execution strategy is insufficient
        for its task, it calls this endpoint to escalate to a ``parallel`` or
        ``competitive`` pattern.

        Behaviour by ``pattern``:

        - **``single``**: No-op; acknowledges the strategy (default, no spawning).
        - **``parallel``**: When ``context`` is provided, submits ``count`` identical
          tasks that will be dispatched to different agents simultaneously.  Each
          spawned task has ``reply_to`` set to the requesting agent so that results
          are delivered back to it.  When ``context`` is omitted, only the strategy
          preference is recorded (no immediate spawning).
        - **``competitive``**: Same as ``parallel`` but task prompts indicate
          competition semantics (agents solve the same problem independently; the
          best result wins).

        Returns
        -------
        dict
            ``{"status": "accepted", "agent_id": ..., "pattern": ..., "count": ...,
              "tags": ..., "spawned_task_ids": [...]}``

            ``spawned_task_ids`` is present (and non-empty) only when ``context``
            was provided and tasks were actually submitted.

        HTTP error codes:
        - 404: agent not found
        - 422: schema validation failure (invalid pattern or count)

        Design references:
        - §12「ワークフロー設計の層構造」層3 実行方式の自律切り替え
        - arXiv:2505.19591 (Evolving Orchestration 2025): dynamic orchestration
        - ALAS arXiv:2505.12501 (2025): three-layer adaptive execution framework
        - DESIGN.md §10.16 (v0.49.0)
        """
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")

        spawned_task_ids: list[str] = []

        # When context is provided, immediately spawn the parallel/competitive tasks.
        if body.context is not None and body.pattern in ("parallel", "competitive"):
            count = body.count
            for i in range(count):
                if body.pattern == "competitive":
                    slot_prompt = (
                        f"You are solver #{i + 1} of {count} in a COMPETITIVE phase.\n"
                        f"Solve the following problem independently.  Write your solution "
                        f"to the scratchpad and include a numeric score or quality metric.\n\n"
                        f"## Task\n{body.context}"
                    )
                else:
                    slot_prompt = (
                        f"You are worker #{i + 1} of {count} in a PARALLEL phase.\n"
                        f"Complete the following task.  "
                        f"The requesting agent ({agent_id}) will aggregate all results.\n\n"
                        f"## Task\n{body.context}"
                    )

                task = await orchestrator.submit_task(
                    slot_prompt,
                    required_tags=body.tags if body.tags else None,
                    reply_to=body.reply_to,
                )
                spawned_task_ids.append(task.id)

            logger.info(
                "change-strategy: agent=%s pattern=%s count=%d spawned=%s",
                agent_id, body.pattern, count, spawned_task_ids,
            )

        response: dict = {
            "status": "accepted",
            "agent_id": agent_id,
            "pattern": body.pattern,
            "count": body.count,
            "tags": body.tags,
        }
        if spawned_task_ids:
            response["spawned_task_ids"] = spawned_task_ids

        return response

    @app.post(
        "/agents/{agent_id}/task-complete",
        summary="Signal task completion (explicit) or nudge agent (Stop hook)",
        dependencies=[Depends(auth)],
    )
    async def agent_task_complete(agent_id: str, request: Request, task_id: str | None = None) -> dict:
        """Handle task-complete signal from agent or Stop hook nudge from Claude Code.

        Two call sources are distinguished by the request body:

        **Explicit** ``/task-complete`` slash command (body has no ``stop_hook_active`` key):
        - Completes the current task via ``handle_output()``.
        - Returns ``{"status": "ok"}``.
        - Body: ``{"output": "<one-line summary>"}``

        **Claude Code Stop hook** (body contains ``stop_hook_active`` key):
        - ``stop_hook_active=True``: Claude is mid-tool-call continuation — skip entirely.
          Returns ``{"status": "skipped", "reason": "stop_hook_active"}``.
        - ``stop_hook_active=False``: Claude finished a response turn but the agent has
          not called ``/task-complete`` → send a nudge via ``notify_stdin``.
          Returns ``{"status": "nudged"}``.
          The task remains open; only an explicit call can complete it.

        HTTP error codes:
        - 404: agent not found
        - 409: agent is not in BUSY state (no active task to complete)

        Design references:
        - Claude Code Hooks Reference https://code.claude.com/docs/en/hooks (2025)
        - DESIGN.md §10.latest (v1.0.x Stop hook / NudgingStrategy)
        """
        from tmux_orchestrator.agents.base import AgentStatus

        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        if agent.status != AgentStatus.BUSY:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Agent {agent_id!r} is not BUSY (status={agent.status.value!r}); "
                    "cannot complete a task that is not in progress"
                ),
            )

        # If the stop hook URL includes ?task_id=<id>, validate it against the
        # current task.  _update_stop_hook_for_task() writes the task_id into the
        # URL before each dispatch, so a mismatch means the hook is stale (fired
        # from a previous task).  Calls without a task_id (e.g. direct API use
        # or old-style stop hooks) are accepted as before.
        if task_id and agent._current_task and agent._current_task.id != task_id:
            logger.debug(
                "Agent %s task-complete skipped: task_id mismatch (hook=%r, current=%r)",
                agent_id, task_id, agent._current_task.id,
            )
            return {"status": "skipped", "reason": "task_id_mismatch"}

        # Parse optional body.
        # Claude Code Stop hook sends: {"stop_hook_active": bool, "last_assistant_message": str, ...}
        # Explicit /task-complete slash command sends: {"output": "<summary>"}
        #
        # The presence of the "stop_hook_active" key distinguishes the two sources:
        #   - Key present  → came from Stop hook → nudge the agent (never complete the task).
        #   - Key absent   → explicit /task-complete call → complete the task.
        #
        # This ensures the Stop hook is purely a nudge trigger, never a task-completion trigger.
        # Reference: DESIGN.md §10.latest (v1.0.x Stop hook / NudgingStrategy)
        nudge_requested = False
        output = ""
        try:
            body = await request.json()
            if "stop_hook_active" in body:
                # Came from Stop hook.
                if body.get("stop_hook_active"):
                    # stop_hook_active=True → Claude is mid-tool-call continuation → skip entirely.
                    return {"status": "skipped", "reason": "stop_hook_active"}
                # stop_hook_active=False → Claude finished a response turn but task still open.
                nudge_requested = True
            else:
                # Explicit /task-complete call.
                output = (
                    body.get("last_assistant_message")
                    or body.get("output")
                    or ""
                )
        except Exception:  # noqa: BLE001
            pass  # body is optional; treat as explicit call with empty output

        if nudge_requested:
            task_id_prefix = agent._current_task.id[:8] if agent._current_task else "?"
            nudge = (
                f"__ORCHESTRATOR__: Your task is still open (task_id={task_id_prefix}). "
                "If all work is complete and artefacts are committed, call:\n"
                "    /task-complete <one-line summary>\n"
                "If you still have work to do, please continue."
            )
            await agent.notify_stdin(nudge)
            logger.info(
                "Agent %s: Stop hook fired — nudge sent (task still open)",
                agent_id,
            )
            return {"status": "nudged"}

        # Capture task_id before handle_output() clears _current_task.
        completed_task_id = agent._current_task.id if agent._current_task else None
        await agent.handle_output(output)
        logger.info(
            "Agent %s task-complete received via explicit signal (task_id=%s)",
            agent_id,
            completed_task_id or "unknown",
        )
        # --- Episode auto-record (v1.0.29) ---
        # When memory_auto_record is enabled, automatically append an episode to
        # the agent's JSONL store.  The output string becomes the episode summary.
        # Reference: Wang & Chen "MIRIX" arXiv:2507.07957 (2025);
        # DESIGN.md §10.29 (v1.0.29).
        _auto_record = getattr(_orch_config, "memory_auto_record", True)
        if _auto_record and output:
            try:
                _episode_store.append(
                    agent_id,
                    summary=output[:500],  # cap at 500 chars to keep episodes compact
                    outcome="success",
                    lessons="",
                    task_id=completed_task_id,
                )
                logger.debug(
                    "Episode auto-recorded for agent %s task %s",
                    agent_id, completed_task_id,
                )
            except Exception as _ep_err:  # noqa: BLE001
                logger.warning(
                    "Episode auto-record failed for agent %s: %s", agent_id, _ep_err
                )
        return {"status": "ok"}

    @app.post(
        "/agents/{agent_id}/ready",
        summary="Signal agent startup readiness (called by SessionStart hook)",
        # No auth: hook fires from claude's process on the same host.
        # The endpoint only sets an asyncio.Event — no sensitive data is exposed.
    )
    async def agent_ready(agent_id: str) -> dict:
        """Set the startup-ready event for *agent_id*.

        Called by the ``SessionStart`` hook (via ``curl``) when Claude Code
        starts a new session.  Sets ``agent._startup_ready`` so that
        ``ClaudeCodeAgent._wait_for_ready()`` can return instead of timing out.

        - 404 if agent is not found.
        - 200 ``{"status": "ok"}`` on success (even if ``_startup_ready`` is
          already set or absent — idempotent by design).
        """
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        ready_event = getattr(agent, "_startup_ready", None)
        if ready_event is not None:
            ready_event.set()
        return {"status": "ok"}

    @app.post(
        "/agents/{agent_id}/drain",
        summary="Drain an agent — stop it after its current task completes",
        dependencies=[Depends(auth)],
    )
    async def drain_agent(agent_id: str) -> dict:
        """Put *agent_id* into graceful drain mode.

        - **IDLE**: immediately stops the agent and removes it from the registry.
          Returns ``{status: "stopped_immediately"}``.
        - **BUSY**: marks the agent as ``DRAINING``; it will be auto-stopped and
          removed from the registry once its current task finishes.
          Returns ``{status: "draining"}``.
        - **DRAINING / STOPPED / ERROR**: returns 409 Conflict.

        A STATUS event ``agent_draining`` (or ``agent_drained`` for immediate stops)
        is published to the bus.

        Design references:
        - Kubernetes Pod ``terminationGracePeriodSeconds``
        - HAProxy graceful restart
        - UNIX ``SO_LINGER`` graceful socket close
        - AWS ECS ``stopTimeout``
        - DESIGN.md §10.23 (v0.28.0)
        """
        try:
            result = await orchestrator.drain_agent(agent_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        status = result.get("status")
        if status in ("already_draining", "already_stopped"):
            raise HTTPException(
                status_code=409,
                detail=f"Agent {agent_id!r} cannot be drained (current status: {status})",
            )
        return result

    @app.get(
        "/agents/{agent_id}/drain",
        summary="Check drain status of an agent",
        dependencies=[Depends(auth)],
    )
    async def get_agent_drain_status(agent_id: str) -> dict:
        """Return the drain status of *agent_id*.

        Response fields:
        - ``agent_id``: the agent's ID
        - ``draining``: ``true`` if the agent is currently in DRAINING state
        - ``status``: the agent's current status value

        Returns 404 if the agent is not registered.

        Design reference: DESIGN.md §10.23 (v0.28.0).
        """
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        from tmux_orchestrator.agents.base import AgentStatus  # noqa: PLC0415
        return {
            "agent_id": agent_id,
            "draining": agent.status == AgentStatus.DRAINING,
            "status": agent.status.value,
        }

    @app.post(
        "/orchestrator/drain",
        summary="Drain all agents — graceful orchestrator shutdown",
        dependencies=[Depends(auth)],
    )
    async def drain_orchestrator() -> dict:
        """Drain all registered agents.

        Iterates over every registered agent and calls ``drain_agent()``:
        - IDLE agents are stopped immediately.
        - BUSY agents are marked DRAINING and auto-stopped after their current task.

        Response fields:
        - ``draining``: agent IDs that are now draining (were BUSY)
        - ``stopped_immediately``: agent IDs stopped immediately (were IDLE)
        - ``already_stopped``: agent IDs skipped (already STOPPED, ERROR, or DRAINING)

        Design reference: DESIGN.md §10.23 (v0.28.0).
        """
        return await orchestrator.drain_all()

    @app.get(
        "/agents/{agent_id}/stats",
        summary="Per-agent context usage stats",
        dependencies=[Depends(auth)],
    )
    async def agent_context_stats(agent_id: str) -> dict:
        """Return context window usage statistics for *agent_id*.

        Fields:
        - ``pane_chars``: character count of the last captured pane output.
        - ``estimated_tokens``: estimated token count (pane_chars / 4).
        - ``context_window_tokens``: configured total context window size.
        - ``context_pct``: percentage of context window used (0-100+).
        - ``warn_threshold_pct``: threshold at which context_warning is emitted.
        - ``notes_mtime``: mtime of NOTES.md at last check (Unix timestamp).
        - ``notes_updates``: number of NOTES.md changes detected.
        - ``context_warnings``: number of context_warning events emitted.
        - ``summarize_triggers``: number of /summarize auto-injections.
        - ``last_polled``: monotonic timestamp of the last poll cycle.
        - ``worktree_path``: filesystem path to the agent's worktree (str | null).
        - ``status``: current agent status (IDLE/BUSY/STOPPED/ERROR/DRAINING).
        - ``task_count``: number of completed tasks (success + error).
        - ``error_count``: number of tasks that completed with an error.

        Returns 404 if the agent is not registered.

        Design reference: Liu et al. "Lost in the Middle" TACL 2024
        (https://arxiv.org/abs/2307.03172) — context saturation degrades recall;
        monitoring context size enables proactive compression. DESIGN.md §11 (v0.21.0).
        Design reference (enrichment): Zalando RESTful API Guidelines §compatibility —
        adding optional fields is a backward-compatible change. DESIGN.md §10 (v1.0.20).
        """
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")

        # Build enrichment fields from registry/history regardless of context monitor.
        history = orchestrator.get_agent_history(agent_id, limit=200) or []
        task_count = len(history)
        error_count = sum(1 for r in history if r.get("status") == "error")

        enrichment: dict = {
            "worktree_path": (
                str(agent.worktree_path) if agent.worktree_path is not None else None
            ),
            "status": agent.status.value,
            "task_count": task_count,
            "error_count": error_count,
            "started_at": (
                agent.started_at.isoformat() if agent.started_at is not None else None
            ),
            "uptime_s": agent.uptime_s,
        }

        stats = orchestrator.get_agent_context_stats(agent_id)
        if stats is None:
            # Agent registered but context monitor has not polled yet;
            # return skeleton with enrichment fields.
            return {"agent_id": agent_id, **enrichment}

        return {**stats, **enrichment}

    @app.get(
        "/context-stats",
        summary="Context usage stats for all agents",
        dependencies=[Depends(auth)],
    )
    async def all_context_stats() -> list:
        """Return context window usage statistics for all tracked agents.

        See ``GET /agents/{id}/stats`` for field descriptions.

        Design reference: DESIGN.md §11 (v0.21.0) — エージェントのコンテキスト使用量モニタリング.
        """
        return orchestrator.all_agent_context_stats()

    @app.get(
        "/agents/{agent_id}/drift",
        summary="Per-agent behavioral drift stats",
        dependencies=[Depends(auth)],
    )
    async def agent_drift_stats(agent_id: str) -> dict:
        """Return behavioral drift statistics for *agent_id*.

        Fields:
        - ``drift_score``: composite drift score (0–1; lower = more drifted).
        - ``role_score``: keyword overlap between system_prompt and pane output.
        - ``idle_score``: 1 when pane is active; 0 when idle past ``drift_idle_threshold``.
        - ``length_score``: output line-count stability score.
        - ``warned``: whether the agent is currently in a drift-warned state.
        - ``drift_warnings``: cumulative count of agent_drift_warning events emitted.
        - ``drift_threshold``: the configured composite score threshold.
        - ``last_polled``: monotonic timestamp of the most recent poll.

        Returns 404 if the agent is unknown or not yet tracked by the drift monitor.

        Design reference: Rath arXiv:2601.04170 "Agent Drift" (2026) — ASI framework;
        DESIGN.md §10.20 (v1.0.9).
        """
        stats = orchestrator.get_agent_drift_stats(agent_id)
        if stats is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} drift stats not yet available")
        return stats

    @app.get(
        "/drift",
        summary="Behavioral drift stats for all agents",
        dependencies=[Depends(auth)],
    )
    async def all_drift_stats() -> list:
        """Return behavioral drift statistics for all tracked agents.

        See ``GET /agents/{id}/drift`` for field descriptions.

        Design reference: Rath arXiv:2601.04170 "Agent Drift" (2026);
        DESIGN.md §10.20 (v1.0.9).
        """
        return orchestrator.all_agent_drift_stats()

    @app.get(
        "/agents/{agent_id}/history",
        summary="Per-agent task history",
        dependencies=[Depends(auth)],
    )
    async def agent_history(agent_id: str, limit: int = 50) -> list:
        """Return the last *limit* completed task records for *agent_id*.

        Each record contains:
        - ``task_id``: unique task identifier
        - ``prompt``: the task prompt text
        - ``started_at``: ISO timestamp when the task was dispatched
        - ``finished_at``: ISO timestamp when the RESULT arrived
        - ``duration_s``: wall-clock seconds from dispatch to RESULT
        - ``status``: ``"success"`` or ``"error"``
        - ``error``: error message string, or null on success

        Results are ordered most-recent-first.  Pass ``?limit=N`` to control
        how many records are returned (default 50, capped at 200).

        Design reference: TAMAS (IBM, 2025) "Beyond Black-Box Benchmarking:
        Observability, Analytics, and Optimization of Agentic Systems"
        arXiv:2503.06745 — per-agent task history enables bottleneck analysis.
        Langfuse "AI Agent Observability" (2024): tracing decision paths.
        """
        history = orchestrator.get_agent_history(agent_id, limit=min(limit, 200))
        if history is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        return history

    # ------------------------------------------------------------------
    # Worktree integrity (v0.43.0)
    # ------------------------------------------------------------------

    @app.get(
        "/agents/{agent_id}/worktree-status",
        summary="Worktree integrity status for an agent",
        dependencies=[Depends(auth)],
    )
    async def agent_worktree_status(agent_id: str) -> dict:
        """Return the git worktree integrity status for *agent_id*.

        Checks performed:
        - Path existence (worktree directory must exist on disk)
        - ``index.lock`` presence (indicates a crashed git process)
        - HEAD resolution (``git rev-parse HEAD`` must succeed)
        - Branch name (expected ``worktree/{agent_id}``)
        - Dirty state (uncommitted changes via ``git status --porcelain``)
        - Object-store integrity (``git fsck --no-dangling``)

        Fields returned:
        - ``agent_id``: the agent identifier
        - ``path``: absolute path to the worktree, or null for shared agents
        - ``is_valid``: True iff the worktree is structurally sound
        - ``is_dirty``: True iff uncommitted changes are present
        - ``is_locked``: True iff a stale ``index.lock`` is present
        - ``head_sha``: 40-character SHA of HEAD, or null
        - ``branch``: current branch name, or null
        - ``errors``: list of diagnostic messages from fsck / git commands
        - ``checked_at``: ISO 8601 timestamp of when the check ran

        Returns 404 if the agent is not registered with the orchestrator.

        Design references:
        - git-fsck(1): https://git-scm.com/docs/git-fsck
        - GitLab "Repository checks": https://docs.gitlab.com/ee/administration/repository_checks.html
        - DESIGN.md §10.17 (v0.43.0)
        """
        from tmux_orchestrator.infrastructure.worktree_integrity import WorktreeIntegrityChecker

        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")

        # Resolve the worktree path for this agent.
        wm = getattr(orchestrator, "_worktree_manager", None)
        worktree_path = None
        if wm is not None:
            worktree_path = wm.worktree_path(agent_id)

        # Determine repo root for git commands
        repo_root = getattr(orchestrator, "_repo_root", None)
        if repo_root is None and wm is not None:
            repo_root = getattr(wm, "_repo_root", None)

        if worktree_path is None:
            # Agent uses isolate=False (shared repo) — return a stub status.
            from tmux_orchestrator.infrastructure.worktree_integrity import WorktreeStatus
            status = WorktreeStatus(agent_id=agent_id, path=None)
            return status.to_dict()

        if repo_root is None:
            repo_root = worktree_path  # fallback: use worktree itself as cwd

        checker = WorktreeIntegrityChecker(repo_root=repo_root)
        status = await checker.check_path(agent_id, worktree_path)
        return status.to_dict()

    # ------------------------------------------------------------------
    # Task result persistence (Event Sourcing / CQRS read side)
    # ------------------------------------------------------------------

    @app.get(
        "/results",
        summary="Query persisted task results",
        dependencies=[Depends(auth)],
    )
    async def query_results(
        agent_id: str | None = None,
        task_id: str | None = None,
        date: str | None = None,
        limit: int = 50,
    ) -> list:
        """Return persisted task results from the append-only JSONL store.

        Query parameters are AND-combined:
        - ``agent_id``: filter by the agent that completed the task.
        - ``task_id``: filter to a specific task.
        - ``date``: ``YYYY-MM-DD`` — scan only that day's file.
        - ``limit``: maximum records returned (default 50).

        Returns an empty list when ``result_store_enabled=False`` or no
        results have been persisted yet.

        Design reference:
        - Martin Fowler "Event Sourcing" (2005): append-only log of facts.
        - Greg Young "CQRS Documents" (2010): separate write/read paths.
        - Rich Hickey "The Value of Values" (Datomic, 2012): immutable facts.
        - DESIGN.md §10.19 (v0.24.0).
        """
        result_store = getattr(orchestrator, "_result_store", None)
        if result_store is None:
            return []
        return result_store.query(
            agent_id=agent_id,
            task_id=task_id,
            date=date,
            limit=limit,
        )

    @app.get(
        "/results/dates",
        summary="List dates with persisted result data",
        dependencies=[Depends(auth)],
    )
    async def results_dates() -> list:
        """Return a sorted list of ``YYYY-MM-DD`` date strings for which
        result data exists in the JSONL store.

        Returns an empty list when ``result_store_enabled=False`` or no
        results have been persisted yet.

        Design reference: DESIGN.md §10.19 (v0.24.0).
        """
        result_store = getattr(orchestrator, "_result_store", None)
        if result_store is None:
            return []
        return result_store.all_dates()

    # ------------------------------------------------------------------
    # Workflow DAG API
    # ------------------------------------------------------------------

    @app.post(
        "/workflows",
        summary="Submit a multi-step workflow DAG",
        dependencies=[Depends(auth)],
    )
    async def submit_workflow(body: WorkflowSubmit) -> dict:
        """Submit a named workflow as a directed acyclic graph of tasks.

        Supports two submission modes:

        **tasks= (legacy DAG mode)**:
        Each task in ``tasks`` may reference other tasks in the same submission
        via ``depends_on`` (a list of ``local_id`` strings).

        **phases= (declarative phase mode)**:
        A list of :class:`PhaseSpecModel` objects where each phase has a
        ``pattern`` (single | parallel | competitive | debate) and an
        ``agents`` selector.  The server expands phases into a task DAG
        automatically.  Sequential phases are chained via ``depends_on``;
        parallel/competitive phases fan out; debate phases build an
        advocate/critic/judge chain.

        In both modes the handler:
        1. Validates the DAG for unknown ``local_id`` references and cycles.
        2. Assigns a global orchestrator task ID to each local node.
        3. Submits tasks to the orchestrator in topological order, translating
           ``depends_on`` local IDs to global task IDs.
        4. Registers all task IDs with the ``WorkflowManager`` for status
           tracking.
        5. Returns the workflow ID and a ``local_id → global_task_id`` mapping.

        Returns 400 on invalid DAG (unknown dependency or cycle).
        Returns 422 on schema validation failure (neither tasks nor phases provided).

        Design references:
        - Apache Airflow DAG model — directed acyclic graph of tasks
        - AWS Step Functions — state machine workflow definition
        - Tomasulo's algorithm — register renaming == local_id → task_id mapping
        - Prefect "Modern Data Stack" — submit pipeline as a unit
        - arXiv:2512.19769 (PayPal DSL 2025): declarative pattern → 60% dev-time reduction
        - §12「ワークフロー設計の層構造」層1 宣言的モード
        - DESIGN.md §10.20 (v0.25.0), §10.15 (v0.48.0)
        """
        from tmux_orchestrator.workflow_manager import validate_dag  # noqa: PLC0415

        # ------------------------------------------------------------------
        # Phase expansion path (new declarative mode)
        # ------------------------------------------------------------------
        phase_statuses = None
        if body.phases is not None:
            from tmux_orchestrator.phase_executor import (  # noqa: PLC0415
                AgentSelector,
                PhaseSpec,
                expand_phases_with_status,
            )

            run_id_prefix = uuid.uuid4().hex[:8]
            phase_specs: list[PhaseSpec] = []
            for p in body.phases:
                phase_specs.append(
                    PhaseSpec(
                        name=p.name,
                        pattern=p.pattern,  # type: ignore[arg-type]
                        agents=AgentSelector(
                            tags=p.agents.tags,
                            count=p.agents.count,
                            target_agent=p.agents.target_agent,
                            target_group=p.agents.target_group,
                        ),
                        critic_agents=AgentSelector(
                            tags=p.critic_agents.tags,
                            count=p.critic_agents.count,
                            target_agent=p.critic_agents.target_agent,
                            target_group=p.critic_agents.target_group,
                        ),
                        judge_agents=AgentSelector(
                            tags=p.judge_agents.tags,
                            count=p.judge_agents.count,
                            target_agent=p.judge_agents.target_agent,
                            target_group=p.judge_agents.target_group,
                        ),
                        debate_rounds=p.debate_rounds,
                        context=p.context,
                        required_tags=p.required_tags,
                    )
                )

            task_specs, phase_statuses = expand_phases_with_status(
                phase_specs,
                context=body.context,
                scratchpad_prefix=f"wf/{run_id_prefix}",
            )
        else:
            # Legacy tasks= path
            task_specs = [t.model_dump() for t in body.tasks]  # type: ignore[union-attr]

        # ------------------------------------------------------------------
        # DAG validation + submission (shared by both paths)
        # ------------------------------------------------------------------
        try:
            ordered = validate_dag(task_specs, local_id_key="local_id", deps_key="depends_on")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # Assign global IDs and submit in dependency order
        local_to_global: dict[str, str] = {}
        global_task_ids: list[str] = []

        for spec in ordered:
            global_deps = [local_to_global[lid] for lid in spec.get("depends_on", [])]
            task = await orchestrator.submit_task(
                spec["prompt"],
                priority=spec.get("priority", 0),
                depends_on=global_deps or None,
                target_agent=spec.get("target_agent"),
                required_tags=spec.get("required_tags") or None,
                target_group=spec.get("target_group"),
                max_retries=spec.get("max_retries", 0),
                inherit_priority=spec.get("inherit_priority", True),
                ttl=spec.get("ttl"),
            )
            local_to_global[spec["local_id"]] = task.id
            global_task_ids.append(task.id)

        # Register with WorkflowManager for status tracking
        wm = orchestrator.get_workflow_manager()
        run = wm.submit(name=body.name, task_ids=global_task_ids)

        # Attach phase status trackers to the workflow run (if phases were used)
        if phase_statuses is not None:
            # Remap local_id → global_task_id for each phase's task_ids
            for ps in phase_statuses:
                ps.task_ids = [local_to_global[lid] for lid in ps.task_ids]
            run.phases = phase_statuses

        response: dict = {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": local_to_global,
        }
        if phase_statuses is not None:
            response["phases"] = [ps.to_dict() for ps in phase_statuses]
        return response

    @app.post(
        "/workflows/tdd",
        summary="Submit a 3-agent TDD workflow (Red→Green→Refactor)",
        dependencies=[Depends(auth)],
    )
    async def submit_tdd_workflow(body: TddWorkflowSubmit) -> dict:
        """Submit a context-isolated 3-agent TDD workflow DAG.

        Automatically builds and submits a 3-step Workflow DAG:

        1. **test-writer** (RED): writes failing pytest tests for *feature*
           and stores the test file path in the scratchpad.
        2. **implementer** (GREEN): reads the test file path from scratchpad,
           writes the minimal implementation that makes tests pass, and stores
           the implementation file path.
        3. **refactorer** (REFACTOR): reads both paths from scratchpad, verifies
           tests still pass, and improves code quality.

        The scratchpad acts as a Blackboard — agents communicate via shared
        state without direct P2P messaging.  This enforces context isolation:
        each agent starts with exactly the information it needs.

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``tdd/{feature}``)
        - ``task_ids``: dict with keys ``test_writer``, ``implementer``,
          ``refactorer`` mapping to global task IDs
        - ``scratchpad_prefix``: scratchpad namespace for this run
          (e.g. ``tdd/{workflow_id[:8]}``)

        Design references:
        - TDFlow arXiv:2510.23761 (2025): context isolation via sub-agents
          achieves 88.8% on SWE-Bench Lite.
        - alexop.dev "Forcing Claude Code to TDD" (2025): genuine TDD
          requires separate context windows for each phase.
        - Blackboard pattern (Buschmann 1996): shared working memory.
        - DESIGN.md §10.31 (v0.36.0)
        """
        import uuid as _uuid  # noqa: PLC0415

        lang = body.language
        feature_slug = body.feature.replace(" ", "_")

        # Register workflow first to get a stable run ID for the scratchpad prefix.
        # We submit tasks in a second pass with the prefix baked into prompts.
        wm = orchestrator.get_workflow_manager()
        wf_name = f"tdd/{body.feature}"

        # Pre-generate a workflow run UUID so we can derive the scratchpad prefix
        # before submitting tasks.  We call wm.submit() after task creation with
        # the actual task IDs.
        # NOTE: scratchpad keys must NOT contain '/' since the REST route uses
        # a plain {key} path parameter, not {key:path}.  Underscores are used
        # as the namespace separator instead.
        pre_run_id = str(_uuid.uuid4())
        scratchpad_prefix = f"tdd_{pre_run_id[:8]}"
        tests_path_key = f"{scratchpad_prefix}_tests_path"
        impl_path_key = f"{scratchpad_prefix}_impl_path"

        # --- Scratchpad helper snippet (shared across all 3 prompts) ---
        # Agents read web_base_url + api_key from __orchestrator_context__.json
        # and __orchestrator_api_key__ (see CLAUDE.md "API Key for Authenticated Requests").
        # Scratchpad keys use underscores (NOT slashes) because the REST route
        # /scratchpad/{key} treats '/' as a URL path separator.
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        # --- Prompt templates (context isolation) ---
        # test-writer gets: feature name + language + where to store artifacts
        # It must NOT know how the feature will be implemented.
        test_writer_prompt = (
            f"You are the TEST-WRITER agent in a Red→Green→Refactor TDD workflow.\n"
            f"\n"
            f"**Feature to test:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task (RED phase):\n"
            f"1. Write failing {lang} pytest tests for '{body.feature}'.\n"
            f"   - Focus on behaviour, not implementation.\n"
            f"   - Tests must fail when run against an empty/stub implementation.\n"
            f"   - Use descriptive test function names (they are the specification).\n"
            f"2. Save the test file as `test_{feature_slug}.py` in your working directory.\n"
            f"3. Verify the tests fail: run `python -m pytest test_{feature_slug}.py -v` and confirm failures.\n"
            f"4. Write the ABSOLUTE PATH to the test file to the shared scratchpad.\n"
            f"   Scratchpad key: `{tests_path_key}`\n"
            f"   Get your web_base_url and api_key from `__orchestrator_context__.json` and `__orchestrator_api_key__`.\n"
            f"   Then run:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
            f"     \"$WEB_BASE_URL/scratchpad/{tests_path_key}\" \\\n"
            f"     -H 'Content-Type: application/json' \\\n"
            f"     -d '{{\"value\": \"'$(pwd)/test_{feature_slug}.py'\"}}'\n"
            f"   ```\n"
            f"\n"
            f"Do NOT write any implementation code. Focus only on tests."
        )

        # implementer gets: feature name + language + where to READ test path
        # It must NOT see the test-writer's rationale — only the test file.
        implementer_prompt = (
            f"You are the IMPLEMENTER agent in a Red→Green→Refactor TDD workflow.\n"
            f"\n"
            f"**Feature to implement:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task (GREEN phase):\n"
            f"1. Get the test file path from the shared scratchpad.\n"
            f"   Scratchpad key: `{tests_path_key}`\n"
            f"   Get your web_base_url and api_key from `__orchestrator_context__.json` and `__orchestrator_api_key__`.\n"
            f"   Then run:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   TEST_FILE=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
            f"     \"$WEB_BASE_URL/scratchpad/{tests_path_key}\" \\\n"
            f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            f"   echo \"Test file: $TEST_FILE\"\n"
            f"   ```\n"
            f"2. Read the test file to understand what needs to be implemented.\n"
            f"3. Write the MINIMAL {lang} implementation of '{body.feature}' that makes all tests pass.\n"
            f"   - Write only enough code to pass the tests — no over-engineering.\n"
            f"4. Verify: run `python -m pytest $TEST_FILE -v` and confirm all tests pass.\n"
            f"5. Write the ABSOLUTE PATH to your implementation file to the scratchpad.\n"
            f"   Scratchpad key: `{impl_path_key}`\n"
            f"   ```bash\n"
            f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
            f"     \"$WEB_BASE_URL/scratchpad/{impl_path_key}\" \\\n"
            f"     -H 'Content-Type: application/json' \\\n"
            f"     -d '{{\"value\": \"<absolute_path_to_impl_file>\"}}'\n"
            f"   ```\n"
            f"\n"
            f"Implement '{body.feature}' to make the tests pass."
        )

        # refactorer gets: feature name + where to READ both paths
        refactorer_prompt = (
            f"You are the REFACTORER agent in a Red→Green→Refactor TDD workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task (REFACTOR phase):\n"
            f"1. Get both artifact paths from the shared scratchpad.\n"
            f"   Get your web_base_url and api_key from `__orchestrator_context__.json` and `__orchestrator_api_key__`.\n"
            f"   Then run:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   TEST_FILE=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
            f"     \"$WEB_BASE_URL/scratchpad/{tests_path_key}\" \\\n"
            f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            f"   IMPL_FILE=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
            f"     \"$WEB_BASE_URL/scratchpad/{impl_path_key}\" \\\n"
            f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            f"   echo \"Test: $TEST_FILE  Impl: $IMPL_FILE\"\n"
            f"   ```\n"
            f"2. Verify tests still pass: `python -m pytest $TEST_FILE -v`\n"
            f"3. Refactor and improve the implementation:\n"
            f"   - Remove duplication, improve naming, add docstrings.\n"
            f"   - Do NOT change behaviour — all tests must remain green.\n"
            f"4. Run tests again to confirm: `python -m pytest $TEST_FILE -v`\n"
            f"5. Write a brief summary of improvements to stdout.\n"
            f"\n"
            f"Refactor '{body.feature}' to improve code quality while keeping tests green."
        )

        # --- Submit the 3-step DAG ---
        tw_task = await orchestrator.submit_task(
            test_writer_prompt,
            required_tags=body.test_writer_tags or None,
        )
        impl_task = await orchestrator.submit_task(
            implementer_prompt,
            required_tags=body.implementer_tags or None,
            depends_on=[tw_task.id],
        )
        refactorer_task = await orchestrator.submit_task(
            refactorer_prompt,
            required_tags=body.refactorer_tags or None,
            depends_on=[impl_task.id],
            reply_to=body.reply_to,
        )

        # Register with WorkflowManager
        run = wm.submit(
            name=wf_name,
            task_ids=[tw_task.id, impl_task.id, refactorer_task.id],
        )

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": {
                "test_writer": tw_task.id,
                "implementer": impl_task.id,
                "refactorer": refactorer_task.id,
            },
            "scratchpad_prefix": scratchpad_prefix,
        }

    @app.post(
        "/workflows/debate",
        summary="Submit a multi-round Advocate/Critic/Judge debate workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_debate_workflow(body: DebateWorkflowSubmit) -> dict:
        """Submit a structured multi-agent debate Workflow DAG.

        Builds and submits a ``max_rounds``-round debate DAG:

        For each round n in 1..max_rounds:
          1. **advocate_r{n}**: presents or refines the affirmative argument for
             *topic*, reading the previous critic's rebuttal from scratchpad
             (round > 1).  Stores argument to scratchpad.
          2. **critic_r{n}**: challenges the advocate's argument using a
             Devil's Advocate persona, reading from scratchpad.  Stores rebuttal.

        Final step:
          - **judge**: reads all rounds from scratchpad, synthesizes the debate,
            and writes ``DECISION.md`` content to ``{scratchpad_prefix}_decision``.

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``debate/{topic}``)
        - ``task_ids``: dict mapping role keys to global task IDs.
          Keys: ``advocate_r1``, ``critic_r1``, ..., ``advocate_rN``,
          ``critic_rN``, ``judge``
        - ``scratchpad_prefix``: scratchpad namespace for this run

        Design references:
        - Du et al. ICML 2024 (arXiv:2305.14325): 2-3 rounds is optimal.
        - DEBATE ACL 2024 (arXiv:2405.09935): Devil's Advocate prevents bias.
        - ChatEval ICLR 2024 (arXiv:2308.07201): role diversity is critical.
        - DESIGN.md §10.32 (v0.37.0)
        """
        import uuid as _uuid  # noqa: PLC0415

        wm = orchestrator.get_workflow_manager()
        wf_name = f"debate/{body.topic}"

        pre_run_id = str(_uuid.uuid4())
        scratchpad_prefix = f"debate_{pre_run_id[:8]}"

        # Scratchpad helper snippet (shared across all prompts)
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _scratchpad_key(suffix: str) -> str:
            """Derive a flat scratchpad key (no slashes)."""
            return f"{scratchpad_prefix}_{suffix}"

        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            """Bash snippet that writes $var to the scratchpad key."""
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )

        def _read_snippet(key: str, varname: str) -> str:
            """Bash snippet that reads scratchpad key into $varname."""
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        all_tasks: list = []  # list of (role_key, task_coroutine_args)
        prev_task_id: str | None = None
        task_ids_map: dict[str, str] = {}

        # Build tasks for each round
        for rn in range(1, body.max_rounds + 1):
            adv_key = _scratchpad_key(f"r{rn}_advocate")
            crit_key = _scratchpad_key(f"r{rn}_critic")

            # --- Advocate prompt ---
            if rn == 1:
                advocate_preamble = (
                    f"You are the ADVOCATE agent in a structured debate workflow.\n"
                    f"\n"
                    f"**Debate topic:** {body.topic}\n"
                    f"**Round:** {rn} of {body.max_rounds}\n"
                    f"\n"
                    f"Your task:\n"
                    f"1. Present a clear, well-structured argument IN FAVOUR of one "
                    f"position on '{body.topic}'.\n"
                    f"   - Choose the stronger/more practical position.\n"
                    f"   - Support your argument with concrete reasons, examples, "
                    f"and trade-offs.\n"
                    f"   - Be specific and technical where appropriate.\n"
                )
            else:
                prev_crit_key = _scratchpad_key(f"r{rn - 1}_critic")
                advocate_preamble = (
                    f"You are the ADVOCATE agent in a structured debate workflow.\n"
                    f"\n"
                    f"**Debate topic:** {body.topic}\n"
                    f"**Round:** {rn} of {body.max_rounds} (rebuttal round)\n"
                    f"\n"
                    f"Your task:\n"
                    f"1. Read the critic's previous rebuttal from the scratchpad:\n"
                    f"   ```bash\n"
                    f"   {_ctx_snippet}\n"
                    + _read_snippet(prev_crit_key, "CRITIC_ARG")
                    + f"   echo \"Critic said: $CRITIC_ARG\"\n"
                    f"   ```\n"
                    f"2. Respond to the critic's points — defend your original position,\n"
                    f"   concede weak points if warranted, and strengthen your argument.\n"
                    f"   Be specific and address each critique directly.\n"
                )
            advocate_prompt = (
                advocate_preamble
                + f"\n"
                f"3. Write your argument to a file `advocate_r{rn}.md` in your "
                f"working directory.\n"
                f"4. Store your argument in the shared scratchpad:\n"
                f"   ```bash\n"
                f"   {_ctx_snippet}\n"
                f"   CONTENT=$(cat advocate_r{rn}.md)\n"
                + _write_snippet(adv_key)
                + f"\n"
                f"   ```\n"
                f"\n"
                f"Write a structured, technical argument. Be concise (max 400 words)."
            )

            # --- Critic prompt ---
            critic_prompt = (
                f"You are the CRITIC agent (Devil's Advocate) in a structured "
                f"debate workflow.\n"
                f"\n"
                f"**Debate topic:** {body.topic}\n"
                f"**Round:** {rn} of {body.max_rounds}\n"
                f"\n"
                f"Your task:\n"
                f"1. Read the advocate's argument from the shared scratchpad:\n"
                f"   ```bash\n"
                f"   {_ctx_snippet}\n"
                + _read_snippet(adv_key, "ADVOCATE_ARG")
                + f"   echo \"Advocate said: $ADVOCATE_ARG\"\n"
                f"   ```\n"
                f"2. Challenge the advocate's argument rigorously.\n"
                f"   - Identify logical flaws, missing trade-offs, and counterexamples.\n"
                f"   - Present the strongest possible counter-argument.\n"
                f"   - Do NOT agree unless truly warranted — your role is to stress-test "
                f"the argument.\n"
                f"   - Be specific and technical.\n"
                f"3. Write your rebuttal to a file `critic_r{rn}.md` in your "
                f"working directory.\n"
                f"4. Store your rebuttal in the shared scratchpad:\n"
                f"   ```bash\n"
                f"   CONTENT=$(cat critic_r{rn}.md)\n"
                + _write_snippet(crit_key)
                + f"\n"
                f"   ```\n"
                f"\n"
                f"Write a rigorous critique. Be concise (max 400 words)."
            )

            # Determine dependencies
            adv_depends = [prev_task_id] if prev_task_id else []
            adv_task = await orchestrator.submit_task(
                advocate_prompt,
                required_tags=body.advocate_tags or None,
                depends_on=adv_depends,
            )
            prev_task_id = adv_task.id
            task_ids_map[f"advocate_r{rn}"] = adv_task.id

            crit_task = await orchestrator.submit_task(
                critic_prompt,
                required_tags=body.critic_tags or None,
                depends_on=[adv_task.id],
            )
            prev_task_id = crit_task.id
            task_ids_map[f"critic_r{rn}"] = crit_task.id

        # --- Judge prompt ---
        # Collect all scratchpad keys for judge to read
        round_keys_desc = "\n".join(
            f"   - Round {rn} Advocate: key `{_scratchpad_key(f'r{rn}_advocate')}`\n"
            f"   - Round {rn} Critic:   key `{_scratchpad_key(f'r{rn}_critic')}`"
            for rn in range(1, body.max_rounds + 1)
        )
        decision_key = _scratchpad_key("decision")

        judge_prompt = (
            f"You are the JUDGE agent in a structured debate workflow.\n"
            f"\n"
            f"**Debate topic:** {body.topic}\n"
            f"**Rounds completed:** {body.max_rounds}\n"
            f"\n"
            f"Your task:\n"
            f"1. Read all debate rounds from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   ```\n"
            f"   Keys to read:\n"
            f"{round_keys_desc}\n"
            f"   Use curl to read each key: "
            f"`curl -s -H \"X-API-Key: $API_KEY\" \"$WEB_BASE_URL/scratchpad/<key>\"`\n"
            f"\n"
            f"2. Write `DECISION.md` in your working directory containing:\n"
            f"   - **Topic**: {body.topic}\n"
            f"   - **Summary of advocate's position** (2-3 sentences)\n"
            f"   - **Summary of critic's challenges** (2-3 sentences)\n"
            f"   - **Decision**: which position is stronger and why\n"
            f"   - **Rationale**: key factors that determined the decision\n"
            f"   - **Caveats**: important trade-offs or conditions to consider\n"
            f"\n"
            f"3. Store the DECISION.md content in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat DECISION.md)\n"
            + _write_snippet(decision_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Write a balanced, well-reasoned DECISION.md. Be objective and cite "
            f"specific arguments from the debate."
        )

        judge_task = await orchestrator.submit_task(
            judge_prompt,
            required_tags=body.judge_tags or None,
            depends_on=[prev_task_id] if prev_task_id else [],
            reply_to=body.reply_to,
        )
        task_ids_map["judge"] = judge_task.id

        # Register all tasks with WorkflowManager
        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }

    @app.post(
        "/workflows/adr",
        summary="Submit a Proposer/Reviewer/Synthesizer ADR generation workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_adr_workflow(body: AdrWorkflowSubmit) -> dict:
        """Submit a 3-agent Architecture Decision Record (ADR) Workflow DAG.

        Pipeline:
          1. **proposer**: analyses the topic, lists candidate options with
             technical pros/cons, and stores the analysis in the scratchpad.
          2. **reviewer**: reads the proposal and produces a technical critique
             — identifies gaps, missing trade-offs, and biases.
          3. **synthesizer**: reads both proposal and review, then produces a
             final MADR-format ``DECISION.md`` and writes it to the scratchpad.

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``adr/<topic>``)
        - ``task_ids``: dict with keys ``proposer``, ``reviewer``, ``synthesizer``
        - ``scratchpad_prefix``: scratchpad namespace for this run

        Design references:
        - AgenticAKM arXiv:2602.04445 (2026): multi-agent decomposition.
        - Ochoa et al. arXiv:2507.05981 "MAD for RE" (2025).
        - MADR 4.0.0 (2024-09-17): Markdown ADR standard.
        - DESIGN.md §10.14 (v0.40.0)
        """
        import uuid as _uuid  # noqa: PLC0415

        wm = orchestrator.get_workflow_manager()
        wf_name = f"adr/{body.topic}"

        pre_run_id = str(_uuid.uuid4())
        scratchpad_prefix = f"adr_{pre_run_id[:8]}"

        # Shared bash snippets for reading context and API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _scratchpad_key(suffix: str) -> str:
            return f"{scratchpad_prefix}_{suffix}"

        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )

        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        proposal_key = _scratchpad_key("proposal")
        review_key = _scratchpad_key("review")
        decision_key = _scratchpad_key("decision")

        # --- Proposer prompt ---
        proposer_prompt = (
            f"You are the PROPOSER agent in an Architecture Decision Record (ADR) workflow.\n"
            f"\n"
            f"**ADR Topic:** {body.topic}\n"
            f"\n"
            f"Your task is to analyse the architectural decision and identify candidate options.\n"
            f"\n"
            f"Steps:\n"
            f"1. Identify 2-3 concrete options for addressing '{body.topic}'.\n"
            f"2. For each option, document:\n"
            f"   - Brief description\n"
            f"   - Pros (technical advantages, performance, maintainability, cost)\n"
            f"   - Cons (drawbacks, risks, operational complexity)\n"
            f"   - When this option is most appropriate\n"
            f"3. Write your analysis to `proposal.md` in your working directory.\n"
            f"4. Store it in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat proposal.md)\n"
            + _write_snippet(proposal_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be technical, specific, and objective. Do not recommend a winner yet — "
            f"that is the synthesizer's job. Max 500 words."
        )

        # --- Reviewer prompt ---
        reviewer_prompt = (
            f"You are the REVIEWER agent in an Architecture Decision Record (ADR) workflow.\n"
            f"\n"
            f"**ADR Topic:** {body.topic}\n"
            f"\n"
            f"Your task is to critically review the proposer's analysis.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the proposer's analysis from the scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(proposal_key, "PROPOSAL")
            + f"   echo \"Proposal: $PROPOSAL\"\n"
            f"   ```\n"
            f"2. Critically evaluate the proposal:\n"
            f"   - Are any options missing or underrepresented?\n"
            f"   - Are the pros/cons accurate and complete?\n"
            f"   - Are there hidden risks or biases in the analysis?\n"
            f"   - What additional decision drivers should be considered?\n"
            f"     (e.g. team expertise, operational burden, vendor lock-in, scalability)\n"
            f"3. Write your critique to `review.md` in your working directory.\n"
            f"4. Store it in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat review.md)\n"
            + _write_snippet(review_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be rigorous and independent. Do not simply agree with the proposer — "
            f"your role is to stress-test the analysis. Max 400 words."
        )

        # --- Synthesizer prompt ---
        synthesizer_prompt = (
            f"You are the SYNTHESIZER agent in an Architecture Decision Record (ADR) workflow.\n"
            f"\n"
            f"**ADR Topic:** {body.topic}\n"
            f"\n"
            f"Your task is to read the proposal and review, then produce a final "
            f"MADR-format DECISION.md.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read both artifacts from the scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(proposal_key, "PROPOSAL")
            + _read_snippet(review_key, "REVIEW")
            + f"   ```\n"
            f"2. Write `DECISION.md` in MADR format with these sections:\n"
            f"   ```markdown\n"
            f"   # ADR: {body.topic}\n"
            f"   Status: Accepted\n"
            f"   Date: $(date +%Y-%m-%d)\n"
            f"   ## Context and Problem Statement\n"
            f"   ## Decision Drivers\n"
            f"   ## Considered Options\n"
            f"   ## Decision Outcome\n"
            f"   ### Consequences\n"
            f"   ## Pros and Cons of the Options\n"
            f"   ```\n"
            f"3. Store DECISION.md in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat DECISION.md)\n"
            + _write_snippet(decision_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Synthesize both the proposal and the reviewer's critique. "
            f"Choose the best option with clear rationale. "
            f"Acknowledge the reviewer's concerns in the Consequences section."
        )

        # Submit tasks in pipeline order
        proposer_task = await orchestrator.submit_task(
            proposer_prompt,
            required_tags=body.proposer_tags or None,
            depends_on=[],
        )

        reviewer_task = await orchestrator.submit_task(
            reviewer_prompt,
            required_tags=body.reviewer_tags or None,
            depends_on=[proposer_task.id],
        )

        synthesizer_task = await orchestrator.submit_task(
            synthesizer_prompt,
            required_tags=body.synthesizer_tags or None,
            depends_on=[reviewer_task.id],
            reply_to=body.reply_to,
        )

        task_ids_map = {
            "proposer": proposer_task.id,
            "reviewer": reviewer_task.id,
            "synthesizer": synthesizer_task.id,
        }

        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }

    @app.post(
        "/workflows/delphi",
        summary="Submit a multi-round Delphi expert-consensus workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_delphi_workflow(body: DelphiWorkflowSubmit) -> dict:
        """Submit a Delphi-style multi-round expert consensus Workflow DAG.

        For each round *n* in 1..max_rounds:

        1. **expert_{persona}_r{n}** (parallel, one per persona): Each expert
           agent reads the previous moderator's feedback (if round > 1) from the
           scratchpad, then independently produces an opinion on ``topic`` from
           their persona's perspective.  Writes ``expert_{persona}_r{n}.md`` and
           stores it to the scratchpad.
        2. **moderator_r{n}**: Reads all expert opinions for round *n*, synthesises
           them into a structured summary with convergence/divergence analysis, and
           writes ``delphi_round_{n}.md``.  Stores the summary to scratchpad.

        Final step:

        - **consensus**: Reads all moderator summaries, identifies points of
          consensus and remaining disagreements, and writes ``consensus.md``
          containing a structured final agreement.

        Returns:

        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``delphi/<topic>``)
        - ``task_ids``: dict mapping role keys to global task IDs.
          Keys: ``expert_{e}_r{n}`` for each expert/round, ``moderator_r{n}``,
          ``consensus``
        - ``scratchpad_prefix``: scratchpad namespace for this run

        Design references:

        - DelphiAgent (ScienceDirect 2025): multiple LLM agents emulate the
          Delphi method with iterative feedback and synthesis.
        - RT-AID (ScienceDirect 2025): AI-assisted opinions accelerate Delphi
          convergence even with limited expert samples.
        - Du et al. ICML 2024 (arXiv:2305.14325): multi-round debate converges
          to correct answer even when all agents are initially wrong.
        - CONSENSAGENT ACL 2025: sycophancy-mitigation in multi-agent consensus.
        - DESIGN.md §10.22 (v1.0.23)
        """
        import uuid as _uuid  # noqa: PLC0415

        wm = orchestrator.get_workflow_manager()
        wf_name = f"delphi/{body.topic}"

        pre_run_id = str(_uuid.uuid4())
        scratchpad_prefix = f"delphi_{pre_run_id[:8]}"

        # Shared bash snippets for reading context and API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _scratchpad_key(suffix: str) -> str:
            """Derive a flat scratchpad key (no slashes)."""
            return f"{scratchpad_prefix}_{suffix}"

        def _write_snippet(key: str, filename: str) -> str:
            """Python3-based snippet to safely write a file's content to scratchpad.

            Uses python3 json.dumps for correct escaping of quotes, newlines, and
            special characters — avoiding the shell-quoting fragility of:
                -d '{"value": "'$CONTENT'"}'
            which breaks when file content contains quotes or newlines.

            The ``_ctx_snippet`` must be run in the same shell session beforehand
            to set $API_KEY and $WEB_BASE_URL.
            """
            return (
                f"   python3 -c \"\n"
                f"import json, urllib.request, os, sys\n"
                f"content = open('{filename}', 'r', errors='replace').read()\n"
                f"payload = json.dumps({{'value': content}}).encode()\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"if not api_key:\n"
                f"    try: api_key = open('__orchestrator_api_key__').read().strip()\n"
                f"    except: pass\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"base_url = ctx['web_base_url']\n"
                f"req = urllib.request.Request(base_url + '/scratchpad/{key}', data=payload, method='PUT')\n"
                f"req.add_header('Content-Type', 'application/json')\n"
                f"req.add_header('X-API-Key', api_key)\n"
                f"urllib.request.urlopen(req, timeout=15)\n"
                f"print('Stored to scratchpad: {key}')\n"
                f"\"  "
            )

        def _read_snippet(key: str, varname: str) -> str:
            """Bash snippet that reads scratchpad key into $varname."""
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        task_ids_map: dict[str, str] = {}
        all_task_ids: list[str] = []

        # prev_moderator_task_id: the previous round's moderator task (or None for round 1)
        prev_moderator_task_id: str | None = None

        for rn in range(1, body.max_rounds + 1):
            expert_task_ids: list[str] = []

            for persona in body.experts:
                expert_key = _scratchpad_key(f"r{rn}_{persona}")

                if rn == 1:
                    feedback_section = (
                        f"This is Round 1 — there is no prior feedback. "
                        f"Present your initial expert opinion on the topic below.\n"
                    )
                else:
                    prev_mod_key = _scratchpad_key(f"r{rn - 1}_moderator")
                    feedback_section = (
                        f"This is Round {rn}. Read the moderator's Round {rn - 1} "
                        f"feedback from the scratchpad:\n"
                        f"   ```bash\n"
                        f"   {_ctx_snippet}\n"
                        + _read_snippet(prev_mod_key, "MODERATOR_FEEDBACK")
                        + f"   echo \"Moderator feedback: $MODERATOR_FEEDBACK\"\n"
                        f"   ```\n"
                        f"Revise your opinion taking the moderator's synthesis into account.\n"
                        f"Maintain your expert perspective — do NOT simply agree with everyone.\n"
                    )

                expert_prompt = (
                    f"You are an expert agent representing the **{persona.upper()}** "
                    f"perspective in a Delphi consensus workflow.\n"
                    f"\n"
                    f"**Topic:** {body.topic}\n"
                    f"**Round:** {rn} of {body.max_rounds}\n"
                    f"**Your persona:** {persona} specialist\n"
                    f"\n"
                    f"{feedback_section}\n"
                    f"Your tasks:\n"
                    f"1. Analyse '{body.topic}' strictly from the **{persona}** "
                    f"specialist perspective.\n"
                    f"   - Identify risks, trade-offs, requirements, and recommendations "
                    f"specific to your domain.\n"
                    f"   - Be concrete and technical. Avoid generic statements.\n"
                    f"   - Keep your opinion to 200-350 words.\n"
                    f"2. Write your opinion to `expert_{persona}_r{rn}.md`.\n"
                    f"3. Store your opinion in the shared scratchpad using this Python script:\n"
                    f"   ```python\n"
                    + _write_snippet(expert_key, f"expert_{persona}_r{rn}.md")
                    + f"\n"
                    f"   ```\n"
                )

                expert_task = await orchestrator.submit_task(
                    expert_prompt,
                    required_tags=body.expert_tags or None,
                    depends_on=[prev_moderator_task_id] if prev_moderator_task_id else [],
                )
                role_key = f"expert_{persona}_r{rn}"
                task_ids_map[role_key] = expert_task.id
                all_task_ids.append(expert_task.id)
                expert_task_ids.append(expert_task.id)

            # --- Moderator prompt ---
            mod_key = _scratchpad_key(f"r{rn}_moderator")
            round_expert_keys_desc = "\n".join(
                f"   - {persona} opinion: key `{_scratchpad_key(f'r{rn}_{persona}')}`"
                for persona in body.experts
            )

            moderator_prompt = (
                f"You are the MODERATOR agent in a Delphi consensus workflow.\n"
                f"\n"
                f"**Topic:** {body.topic}\n"
                f"**Round:** {rn} of {body.max_rounds}\n"
                f"**Expert personas:** {', '.join(body.experts)}\n"
                f"\n"
                f"Your tasks:\n"
                f"1. Read all expert opinions for Round {rn} from the scratchpad:\n"
                f"   ```bash\n"
                f"   {_ctx_snippet}\n"
                f"   ```\n"
                f"   Keys to read:\n"
                f"{round_expert_keys_desc}\n"
                f"   Use curl to read each key:\n"
                f"   `curl -s -H \"X-API-Key: $API_KEY\" \"$WEB_BASE_URL/scratchpad/<key>\"`\n"
                f"\n"
                f"2. Write `delphi_round_{rn}.md` containing:\n"
                f"   - **Round {rn} Summary**: 2-3 sentence overview of expert opinions\n"
                f"   - **Points of Convergence**: where experts agree (bullet list)\n"
                f"   - **Points of Divergence**: where experts disagree (bullet list)\n"
                f"   - **Key Insights per Persona**: one paragraph per expert persona\n"
            )
            if rn < body.max_rounds:
                moderator_prompt += (
                    f"   - **Feedback for Round {rn + 1}**: questions/areas for experts "
                    f"to refine in the next round (3-5 bullet points)\n"
                )
            moderator_prompt += (
                f"\n"
                f"3. Store the round summary in the shared scratchpad using this Python script:\n"
                f"   ```python\n"
                + _write_snippet(mod_key, f"delphi_round_{rn}.md")
                + f"\n"
                f"   ```\n"
                f"\n"
                f"Be objective. Accurately represent each expert's position. "
                f"Do not favour any single perspective."
            )

            moderator_task = await orchestrator.submit_task(
                moderator_prompt,
                required_tags=body.moderator_tags or None,
                depends_on=expert_task_ids,
            )
            mod_role_key = f"moderator_r{rn}"
            task_ids_map[mod_role_key] = moderator_task.id
            all_task_ids.append(moderator_task.id)
            prev_moderator_task_id = moderator_task.id

        # --- Consensus prompt ---
        consensus_key = _scratchpad_key("consensus")
        all_mod_keys_desc = "\n".join(
            f"   - Round {rn} moderator summary: key `{_scratchpad_key(f'r{rn}_moderator')}`"
            for rn in range(1, body.max_rounds + 1)
        )

        consensus_prompt = (
            f"You are the CONSENSUS agent in a Delphi consensus workflow.\n"
            f"\n"
            f"**Topic:** {body.topic}\n"
            f"**Rounds completed:** {body.max_rounds}\n"
            f"**Expert personas:** {', '.join(body.experts)}\n"
            f"\n"
            f"Your tasks:\n"
            f"1. Read all moderator round summaries from the scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   ```\n"
            f"   Keys to read:\n"
            f"{all_mod_keys_desc}\n"
            f"   Use curl to read each: "
            f"`curl -s -H \"X-API-Key: $API_KEY\" \"$WEB_BASE_URL/scratchpad/<key>\"`\n"
            f"\n"
            f"2. Write `consensus.md` containing:\n"
            f"   - **Topic**: {body.topic}\n"
            f"   - **Expert Perspectives**: brief summary of each persona's view\n"
            f"   - **Consensus Points**: areas of strong agreement across all experts\n"
            f"   - **Remaining Disagreements**: unresolved tensions and why\n"
            f"   - **Recommended Decision**: the most defensible position given all "
            f"expert input\n"
            f"   - **Key Trade-offs**: top 3-5 trade-offs decision-makers should know\n"
            f"   - **Caveats**: conditions under which the recommendation changes\n"
            f"\n"
            f"3. Store the consensus document in the shared scratchpad using this Python script:\n"
            f"   ```python\n"
            + _write_snippet(consensus_key, "consensus.md")
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Write a balanced, well-structured consensus.md. "
            f"Represent all expert perspectives fairly."
        )

        consensus_task = await orchestrator.submit_task(
            consensus_prompt,
            required_tags=body.moderator_tags or None,
            depends_on=[prev_moderator_task_id] if prev_moderator_task_id else [],
            reply_to=body.reply_to,
        )
        task_ids_map["consensus"] = consensus_task.id
        all_task_ids.append(consensus_task.id)

        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }

    @app.post(
        "/workflows/redblue",
        summary="Submit a Red Team / Blue Team adversarial evaluation workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_redblue_workflow(body: RedBlueWorkflowSubmit) -> dict:
        """Submit a 3-agent adversarial evaluation Workflow DAG.

        Pipeline (strictly sequential):

          1. **blue_team**: designs or implements a solution for *topic*, stores
             result in ``{scratchpad_prefix}_blue_design``.
          2. **red_team**: reads blue_team output and attacks it — lists
             vulnerabilities, flaws, and risks; stores result in
             ``{scratchpad_prefix}_red_findings``.
          3. **arbiter**: reads both artifacts, produces a balanced risk
             assessment with prioritised recommendations; stores result in
             ``{scratchpad_prefix}_risk_report``.

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``redblue/<topic>``)
        - ``task_ids``: dict with keys ``blue_team``, ``red_team``, ``arbiter``
        - ``scratchpad_prefix``: scratchpad namespace for this run

        Design references:
        - Harrasse et al. "D3" arXiv:2410.04663 (2026): adversarial multi-agent
          evaluation reduces bias and improves agreement with human judgments.
        - "Red-Teaming LLM MAS via Communication Attacks" ACL 2025 arXiv:2502.14847.
        - Farzulla "Autonomous Red Team and Blue Team AI" DAI-2513 (2025).
        - DESIGN.md §10.23 (v1.0.24)
        """
        import uuid as _uuid  # noqa: PLC0415

        wm = orchestrator.get_workflow_manager()
        wf_name = f"redblue/{body.topic}"

        pre_run_id = str(_uuid.uuid4())
        scratchpad_prefix = f"redblue_{pre_run_id[:8]}"

        # Shared bash snippets for reading context and API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _scratchpad_key(suffix: str) -> str:
            return f"{scratchpad_prefix}_{suffix}"

        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )

        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        blue_design_key = _scratchpad_key("blue_design")
        red_findings_key = _scratchpad_key("red_findings")
        risk_report_key = _scratchpad_key("risk_report")

        # --- Blue-team prompt ---
        blue_prompt = (
            f"You are the BLUE-TEAM agent in a Red/Blue adversarial evaluation workflow.\n"
            f"\n"
            f"**Evaluation topic:** {body.topic}\n"
            f"\n"
            f"Your task is to produce a concrete design or implementation plan for the topic above.\n"
            f"\n"
            f"Steps:\n"
            f"1. Analyse the topic and design a concrete solution:\n"
            f"   - Describe the approach, architecture, or implementation plan.\n"
            f"   - Include key technical decisions with rationale.\n"
            f"   - Be specific: name technologies, patterns, or APIs you would use.\n"
            f"   - Address security, performance, and maintainability.\n"
            f"2. Write your design to `blue_design.md` in your working directory.\n"
            f"3. Store it in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat blue_design.md)\n"
            + _write_snippet(blue_design_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be thorough and honest — the red-team will scrutinise your design. Max 500 words."
        )

        # --- Red-team prompt ---
        red_prompt = (
            f"You are the RED-TEAM agent in a Red/Blue adversarial evaluation workflow.\n"
            f"\n"
            f"**Evaluation topic:** {body.topic}\n"
            f"\n"
            f"Your role is to act as an adversary and find weaknesses in the blue-team design.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the blue-team design from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(blue_design_key, "BLUE_DESIGN")
            + f"   echo \"Blue design: $BLUE_DESIGN\"\n"
            f"   ```\n"
            f"2. Attack the design rigorously from an adversarial perspective:\n"
            f"   - Identify security vulnerabilities (authentication, authorisation, injection, etc.).\n"
            f"   - Find scalability / reliability risks.\n"
            f"   - Spot missing error handling or edge cases.\n"
            f"   - Highlight hidden assumptions that may not hold in production.\n"
            f"   - Be specific: name exact weaknesses, not vague concerns.\n"
            f"   - Do NOT be constructive — your job is to list problems, not solutions.\n"
            f"3. Write your findings to `red_findings.md` in your working directory.\n"
            f"4. Store findings in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat red_findings.md)\n"
            + _write_snippet(red_findings_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be adversarial and thorough. Max 400 words."
        )

        # --- Arbiter prompt ---
        arbiter_prompt = (
            f"You are the ARBITER agent in a Red/Blue adversarial evaluation workflow.\n"
            f"\n"
            f"**Evaluation topic:** {body.topic}\n"
            f"\n"
            f"Your task is to read both the blue-team design and red-team findings,\n"
            f"then produce a balanced risk assessment report.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read both artifacts from the scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(blue_design_key, "BLUE_DESIGN")
            + _read_snippet(red_findings_key, "RED_FINDINGS")
            + f"   ```\n"
            f"2. Write `risk_report.md` in your working directory with these sections:\n"
            f"   ```markdown\n"
            f"   # Risk Assessment: {body.topic}\n"
            f"   ## Blue-Team Design Summary\n"
            f"   ## Red-Team Findings Summary\n"
            f"   ## Risk Matrix\n"
            f"   | Risk | Severity (High/Med/Low) | Likelihood | Mitigation |\n"
            f"   |------|------------------------|------------|------------|\n"
            f"   ## Overall Risk Level\n"
            f"   ## Prioritised Recommendations\n"
            f"   ## Verdict\n"
            f"   ```\n"
            f"3. Store the report in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat risk_report.md)\n"
            + _write_snippet(risk_report_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be balanced and objective. Acknowledge both the blue-team's strengths and "
            f"the red-team's valid concerns. Prioritise recommendations by severity."
        )

        # Submit tasks in pipeline order
        blue_task = await orchestrator.submit_task(
            blue_prompt,
            required_tags=body.blue_tags or None,
            depends_on=[],
        )

        red_task = await orchestrator.submit_task(
            red_prompt,
            required_tags=body.red_tags or None,
            depends_on=[blue_task.id],
        )

        arbiter_task = await orchestrator.submit_task(
            arbiter_prompt,
            required_tags=body.arbiter_tags or None,
            depends_on=[red_task.id],
            reply_to=body.reply_to,
        )

        task_ids_map = {
            "blue_team": blue_task.id,
            "red_team": red_task.id,
            "arbiter": arbiter_task.id,
        }

        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }

    @app.post(
        "/workflows/socratic",
        summary="Submit a Socratic dialogue (questioner/responder/synthesizer) workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_socratic_workflow(body: SocraticWorkflowSubmit) -> dict:
        """Submit a 3-agent Socratic dialogue Workflow DAG.

        Pipeline (strictly sequential):

          1. **questioner**: applies Maieutic method — probes assumptions,
             demands definitions, challenges logical basis.  Phase 1 uses
             adversarial questions; Phase 2 shifts to integrative questions.
             Stores Q&A log in ``{scratchpad_prefix}_dialogue``.
          2. **responder**: reads questioner output and refines, defends, or
             revises the position.  Appends answers to the dialogue log.
          3. **synthesizer**: reads the complete dialogue and extracts a
             structured ``synthesis.md`` with main arguments, agreed points,
             unresolved questions, and recommendations.  Stores result in
             ``{scratchpad_prefix}_synthesis``.

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``socratic/<topic>``)
        - ``task_ids``: dict with keys ``questioner``, ``responder``,
          ``synthesizer``
        - ``scratchpad_prefix``: scratchpad namespace for this run

        Design references:
        - Liang et al. "SocraSynth" arXiv:2402.06634 (2024): staged
          questioner → responder → synthesizer with sycophancy suppression.
        - "KELE" arXiv:2409.05511 EMNLP 2025: two-phase questioning.
        - "CONSENSAGENT" ACL 2025: dynamic prompt refinement reduces sycophancy.
        - DESIGN.md §10.24 (v1.0.25)
        """
        import uuid as _uuid  # noqa: PLC0415

        wm = orchestrator.get_workflow_manager()
        wf_name = f"socratic/{body.topic}"

        pre_run_id = str(_uuid.uuid4())
        scratchpad_prefix = f"socratic_{pre_run_id[:8]}"

        # Shared bash snippets for reading context and API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _scratchpad_key(suffix: str) -> str:
            return f"{scratchpad_prefix}_{suffix}"

        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )

        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        dialogue_key = _scratchpad_key("dialogue")
        synthesis_key = _scratchpad_key("synthesis")

        # --- Questioner prompt ---
        questioner_prompt = (
            f"You are the QUESTIONER agent in a Socratic dialogue workflow.\n"
            f"\n"
            f"**Dialogue topic:** {body.topic}\n"
            f"\n"
            f"Your role is to probe this topic using the Maieutic (midwifery) method.\n"
            f"Do NOT take a position yourself — your job is to draw out and sharpen the\n"
            f"thinking of the responder through precise, incisive questions.\n"
            f"\n"
            f"Steps:\n"
            f"1. Write 4–6 Socratic questions targeting the topic above.\n"
            f"   **Phase 1 questions (questions 1–3)** — adversarial / challenging:\n"
            f"   - 'What exactly do you mean by X?'\n"
            f"   - 'What is your evidence for that assumption?'\n"
            f"   - 'Give a concrete counter-example where that fails.'\n"
            f"   **Phase 2 questions (questions 4–6)** — integrative / constructive:\n"
            f"   - 'Under what conditions would that be correct?'\n"
            f"   - 'What would you need to be true for this to work?'\n"
            f"   - 'Where do you see the strongest case for the alternative?'\n"
            f"2. Write your questions to `questioner_output.md` in your working directory.\n"
            f"3. Store the Q&A log (your questions only for now) in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat questioner_output.md)\n"
            + _write_snippet(dialogue_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be sharp and neutral. Do not answer your own questions. Max 300 words."
        )

        # --- Responder prompt ---
        responder_prompt = (
            f"You are the RESPONDER agent in a Socratic dialogue workflow.\n"
            f"\n"
            f"**Dialogue topic:** {body.topic}\n"
            f"\n"
            f"Your role is to answer the Socratic questions posed by the questioner,\n"
            f"refining and defending the best position on this topic.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the questioner's questions from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(dialogue_key, "QUESTIONS")
            + f"   echo \"Questions: $QUESTIONS\"\n"
            f"   ```\n"
            f"2. Write a `responder_output.md` that:\n"
            f"   - Addresses each question in turn (quote each question before answering).\n"
            f"   - Provides concrete, specific answers with evidence or examples.\n"
            f"   - Refines or revises the position where the questioning reveals a weakness.\n"
            f"   - Acknowledges genuine uncertainty rather than bluffing.\n"
            f"3. Append your answers to the dialogue log in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   FULL_DIALOGUE=\"$QUESTIONS\n\n--- RESPONDER ANSWERS ---\n$(cat responder_output.md)\"\n"
            f"   CONTENT=$FULL_DIALOGUE\n"
            + _write_snippet(dialogue_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be honest and precise. Acknowledge where the questioning reveals genuine weaknesses."
            f" Max 400 words."
        )

        # --- Synthesizer prompt ---
        synthesizer_prompt = (
            f"You are the SYNTHESIZER agent in a Socratic dialogue workflow.\n"
            f"\n"
            f"**Dialogue topic:** {body.topic}\n"
            f"\n"
            f"Your task is to read the complete Socratic dialogue and extract a structured\n"
            f"conclusion that will be useful to the design team.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the full dialogue from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(dialogue_key, "DIALOGUE")
            + f"   echo \"Dialogue: $DIALOGUE\"\n"
            f"   ```\n"
            f"2. Write `synthesis.md` in your working directory with these sections:\n"
            f"   ```markdown\n"
            f"   # Socratic Synthesis: {body.topic}\n"
            f"   ## Main Arguments Surfaced\n"
            f"   ## Points of Agreement\n"
            f"   ## Unresolved Questions\n"
            f"   ## Recommendations\n"
            f"   ## Key Assumptions and Preconditions\n"
            f"   ```\n"
            f"3. Store the synthesis in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat synthesis.md)\n"
            + _write_snippet(synthesis_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be precise and actionable. The synthesis should help the reader make a\n"
            f"well-informed decision about the topic. Max 400 words."
        )

        # Submit tasks in pipeline order
        questioner_task = await orchestrator.submit_task(
            questioner_prompt,
            required_tags=body.questioner_tags or None,
            depends_on=[],
        )

        responder_task = await orchestrator.submit_task(
            responder_prompt,
            required_tags=body.responder_tags or None,
            depends_on=[questioner_task.id],
        )

        synthesizer_task = await orchestrator.submit_task(
            synthesizer_prompt,
            required_tags=body.synthesizer_tags or None,
            depends_on=[responder_task.id],
            reply_to=body.reply_to,
        )

        task_ids_map = {
            "questioner": questioner_task.id,
            "responder": responder_task.id,
            "synthesizer": synthesizer_task.id,
        }

        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }

    @app.post(
        "/workflows/pair",
        summary="Submit a 2-agent PairCoder (Navigator + Driver) workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_pair_workflow(body: PairWorkflowSubmit) -> dict:
        """Submit a 2-agent PairCoder Workflow DAG.

        Pipeline (strictly sequential):

          1. **navigator**: analyses the task, writes a structured ``PLAN.md``
             (architecture decisions, interfaces, step-by-step guide, acceptance
             criteria).  Stores the plan in the shared scratchpad.
          2. **driver**: reads the navigator's plan from the scratchpad,
             implements the code, writes tests, runs them, and writes
             ``driver_summary.md`` with a pass/fail report.

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``pair/<task[:40]>``)
        - ``task_ids``: dict with keys ``navigator``, ``driver``
        - ``scratchpad_prefix``: scratchpad namespace for this run

        Design references:
        - Beck & Fowler "Extreme Programming Explained" (1999).
        - FlowHunt "TDD with AI Agents" (2025): PairCoder quality gains.
        - DESIGN.md §10.27 (v1.0.27)
        """
        import uuid as _uuid  # noqa: PLC0415

        wm = orchestrator.get_workflow_manager()
        # Use first 40 chars of task as name suffix
        task_slug = body.task[:40].strip().replace("\n", " ")
        wf_name = f"pair/{task_slug}"

        pre_run_id = str(_uuid.uuid4())
        scratchpad_prefix = f"pair_{pre_run_id[:8]}"

        # Shared bash snippets for reading context and API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _scratchpad_key(suffix: str) -> str:
            return f"{scratchpad_prefix}_{suffix}"

        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )

        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        plan_key = _scratchpad_key("plan")
        result_key = _scratchpad_key("result")

        # --- Navigator prompt ---
        navigator_prompt = (
            f"You are the NAVIGATOR agent in a PairCoder workflow.\n"
            f"\n"
            f"**Task:** {body.task}\n"
            f"\n"
            f"Your role is to produce a thorough, structured PLAN.md that the Driver\n"
            f"agent will use to implement the task.  Do NOT write any implementation\n"
            f"code yourself — your job is to plan, not to code.\n"
            f"\n"
            f"Steps:\n"
            f"1. Write `PLAN.md` in your working directory with these sections:\n"
            f"   ```markdown\n"
            f"   # Plan: <task title>\n"
            f"   ## Goal\n"
            f"   ## Architecture & Design Decisions\n"
            f"   ## Module / File Layout\n"
            f"   ## Public Interfaces\n"
            f"   ## Step-by-step Implementation Guide\n"
            f"   ## Acceptance Criteria\n"
            f"   ## Edge Cases & Gotchas\n"
            f"   ```\n"
            f"2. Store PLAN.md in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat PLAN.md)\n"
            + _write_snippet(plan_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be precise and concrete — the Driver must be able to implement from your\n"
            f"plan without additional context.  Max 500 words."
        )

        # --- Driver prompt ---
        driver_prompt = (
            f"You are the DRIVER agent in a PairCoder workflow.\n"
            f"\n"
            f"**Task:** {body.task}\n"
            f"\n"
            f"Your role is to implement the task strictly following the Navigator's\n"
            f"PLAN.md.  Write the code, tests, run the tests, and report results.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the Navigator's plan from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(plan_key, "PLAN")
            + f"   echo \"$PLAN\" > PLAN.md\n"
            f"   cat PLAN.md\n"
            f"   ```\n"
            f"2. Implement the code exactly as described in PLAN.md.\n"
            f"   - Create the implementation file(s) in your working directory.\n"
            f"   - Create test file(s) following the acceptance criteria in PLAN.md.\n"
            f"3. Run the tests and capture the result:\n"
            f"   ```bash\n"
            f"   python -m pytest test_*.py -v 2>&1 | tee test_output.txt || true\n"
            f"   ```\n"
            f"4. Write `driver_summary.md` with:\n"
            f"   ```markdown\n"
            f"   # Driver Summary\n"
            f"   ## Implementation\n"
            f"   ## Test Results\n"
            f"   ## Deviations from Plan\n"
            f"   ## Status: PASS | FAIL\n"
            f"   ```\n"
            f"5. Store the summary in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat driver_summary.md)\n"
            + _write_snippet(result_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Follow the plan faithfully.  If a step is unclear, make the simplest\n"
            f"reasonable interpretation and document it in Deviations from Plan."
        )

        # Submit tasks in pipeline order
        navigator_task = await orchestrator.submit_task(
            navigator_prompt,
            required_tags=body.navigator_tags or None,
            depends_on=[],
        )

        driver_task = await orchestrator.submit_task(
            driver_prompt,
            required_tags=body.driver_tags or None,
            depends_on=[navigator_task.id],
            reply_to=body.reply_to,
        )

        task_ids_map = {
            "navigator": navigator_task.id,
            "driver": driver_task.id,
        }

        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }

    @app.post(
        "/workflows/fulldev",
        summary="Submit a 5-agent Full Software Development Lifecycle workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_fulldev_workflow(body: FulldevWorkflowSubmit) -> dict:
        """Submit a 5-agent Full Software Development Lifecycle Workflow DAG.

        Pipeline (each step depends_on the previous):

        1. **spec-writer**: writes a precise feature specification (SPEC.md) with
           functional requirements and acceptance criteria; stores it to scratchpad.
        2. **architect**: reads the spec, writes an architecture/design document
           (DESIGN.md or ADR) with component breakdown and interface definitions;
           stores it to scratchpad.
        3. **tdd-test-writer** (RED): reads spec + design, writes failing pytest
           tests that codify acceptance criteria; stores test file path to scratchpad.
        4. **tdd-implementer** (GREEN): reads spec + test file path, writes the
           minimal implementation that makes all tests pass; stores impl path.
        5. **reviewer**: reads spec, tests, and implementation; writes a structured
           code review (REVIEW.md) categorising blocking/non-blocking issues.

        All artifacts are passed via the shared scratchpad (Blackboard pattern).
        Scratchpad keys use underscores (not slashes) as namespace separator.

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``fulldev/<feature>``)
        - ``task_ids``: dict with keys ``spec_writer``, ``architect``,
          ``test_writer``, ``implementer``, ``reviewer`` mapping to global task IDs
        - ``scratchpad_prefix``: scratchpad namespace for this run
          (e.g. ``fulldev_{run_id[:8]}``)

        Design references:
        - MetaGPT arXiv:2308.00352 (2023/2024): PM → Architect → Engineer SOP pipeline.
        - AgentMesh arXiv:2507.19902 (2025): Planner → Coder → Debugger → Reviewer.
        - arXiv:2508.00083 "Survey on Code Generation with LLM-based Agents" (2025).
        - arXiv:2505.16339 "Rethinking Code Review Workflows" (2025).
        - DESIGN.md §10.16 (v0.42.0)
        """
        import uuid as _uuid  # noqa: PLC0415

        lang = body.language
        feature_slug = body.feature.replace(" ", "_")

        wm = orchestrator.get_workflow_manager()
        wf_name = f"fulldev/{body.feature}"

        pre_run_id = str(_uuid.uuid4())
        scratchpad_prefix = f"fulldev_{pre_run_id[:8]}"

        # Scratchpad keys (underscores only — no slashes)
        spec_key = f"{scratchpad_prefix}_spec"
        design_key = f"{scratchpad_prefix}_design"
        tests_key = f"{scratchpad_prefix}_tests"
        impl_key = f"{scratchpad_prefix}_impl"
        review_key = f"{scratchpad_prefix}_review"

        # Shared bash snippet for reading context + API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )

        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        # ---------------------------------------------------------------
        # 1. SPEC-WRITER prompt
        # ---------------------------------------------------------------
        spec_writer_prompt = (
            f"You are the SPEC-WRITER agent in a Full Software Development Lifecycle workflow.\n"
            f"\n"
            f"**Feature to specify:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task:\n"
            f"1. Write a precise, unambiguous specification for '{body.feature}'.\n"
            f"   Use this format:\n"
            f"   ```markdown\n"
            f"   # Specification: {body.feature}\n"
            f"   ## Context\n"
            f"   ## Functional Requirements\n"
            f"   1. <FR-1> ...\n"
            f"   ## Acceptance Criteria\n"
            f"   - AC-1: Given ... when ... then ...\n"
            f"   ## Out of Scope\n"
            f"   ## Glossary\n"
            f"   ```\n"
            f"2. Save the specification to `SPEC.md` in your working directory.\n"
            f"3. Store the SPEC.md contents in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat SPEC.md)\n"
            + _write_snippet(spec_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Focus on WHAT the system must do, not HOW to implement it.\n"
            f"Be precise: every requirement must be verifiable. Max 400 words."
        )

        # ---------------------------------------------------------------
        # 2. ARCHITECT prompt
        # ---------------------------------------------------------------
        architect_prompt = (
            f"You are the ARCHITECT agent in a Full Software Development Lifecycle workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task:\n"
            f"1. Read the feature specification from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(spec_key, "SPEC")
            + f"   echo \"Spec loaded: $(echo $SPEC | wc -c) chars\"\n"
            f"   ```\n"
            f"2. Design the {lang} implementation architecture:\n"
            f"   - Identify the main components/classes/modules.\n"
            f"   - Define the public API (function signatures, class interfaces).\n"
            f"   - List key design decisions (data structures, error handling strategy).\n"
            f"   - Note any constraints or non-functional requirements.\n"
            f"3. Write the design to `DESIGN.md`:\n"
            f"   ```markdown\n"
            f"   # Design: {body.feature}\n"
            f"   ## Components\n"
            f"   ## Public API\n"
            f"   ## Key Design Decisions\n"
            f"   ## Implementation Notes\n"
            f"   ```\n"
            f"4. Store DESIGN.md in the scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat DESIGN.md)\n"
            + _write_snippet(design_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Focus on the architecture, not the implementation. Max 400 words."
        )

        # ---------------------------------------------------------------
        # 3. TDD-TEST-WRITER prompt
        # ---------------------------------------------------------------
        test_writer_prompt = (
            f"You are the TDD-TEST-WRITER agent in a Full Software Development Lifecycle workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task (RED phase — write failing tests first):\n"
            f"1. Read the specification and design from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(spec_key, "SPEC")
            + _read_snippet(design_key, "DESIGN")
            + f"   echo \"Spec + Design loaded\"\n"
            f"   ```\n"
            f"2. Write {lang} pytest tests for '{body.feature}':\n"
            f"   - Each test must correspond to an Acceptance Criterion in the spec.\n"
            f"   - Tests must FAIL against an empty/stub implementation.\n"
            f"   - Use descriptive test names that act as a specification.\n"
            f"   - Import the module that the implementer will create.\n"
            f"3. Save tests to `test_{feature_slug}.py` in your working directory.\n"
            f"4. Verify tests fail: `python -m pytest test_{feature_slug}.py -v 2>&1 | tail -20`\n"
            f"   (Expect ImportError or AssertionError — that's correct for RED phase)\n"
            f"5. Store the ABSOLUTE PATH to the test file in the scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(pwd)/test_{feature_slug}.py\n"
            + _write_snippet(tests_key, "CONTENT")
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Do NOT write any implementation code. Focus only on tests."
        )

        # ---------------------------------------------------------------
        # 4. TDD-IMPLEMENTER prompt
        # ---------------------------------------------------------------
        implementer_prompt = (
            f"You are the TDD-IMPLEMENTER agent in a Full Software Development Lifecycle workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task (GREEN phase — make tests pass):\n"
            f"1. Read the specification and test file path from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(spec_key, "SPEC")
            + _read_snippet(tests_key, "TEST_FILE")
            + f"   echo \"Spec loaded, test file: $TEST_FILE\"\n"
            f"   ```\n"
            f"2. Read the test file: `cat $TEST_FILE`\n"
            f"3. Write the MINIMAL {lang} implementation of '{body.feature}' that makes\n"
            f"   all tests pass:\n"
            f"   - Implement only what the tests require.\n"
            f"   - Follow the design from the spec (the architect's DESIGN.md).\n"
            f"   - Write the module in the same directory as the tests\n"
            f"     (so the test imports work).\n"
            f"4. Verify: `python -m pytest $TEST_FILE -v`\n"
            f"   All tests must pass before proceeding.\n"
            f"5. Store the ABSOLUTE PATH to the implementation file in the scratchpad:\n"
            f"   ```bash\n"
            f"   # Replace 'your_impl_file.py' with the actual filename\n"
            f"   CONTENT=$(pwd)/your_impl_file.py\n"
            + _write_snippet(impl_key, "CONTENT")
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Implement '{body.feature}' to make all tests pass."
        )

        # ---------------------------------------------------------------
        # 5. REVIEWER prompt
        # ---------------------------------------------------------------
        reviewer_prompt = (
            f"You are the REVIEWER agent in a Full Software Development Lifecycle workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task:\n"
            f"1. Read all artifacts from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(spec_key, "SPEC")
            + _read_snippet(tests_key, "TEST_FILE")
            + _read_snippet(impl_key, "IMPL_FILE")
            + f"   echo \"All artifacts loaded\"\n"
            f"   echo \"Test file: $TEST_FILE\"\n"
            f"   echo \"Impl file: $IMPL_FILE\"\n"
            f"   ```\n"
            f"2. Read the actual test and implementation files:\n"
            f"   `cat $TEST_FILE && cat $IMPL_FILE`\n"
            f"3. Run the tests to verify they pass:\n"
            f"   `python -m pytest $TEST_FILE -v`\n"
            f"4. Write a structured code review to `REVIEW.md`:\n"
            f"   ```markdown\n"
            f"   # Code Review: {body.feature}\n"
            f"   ## Summary\n"
            f"   ## Spec Compliance\n"
            f"   (Does the implementation satisfy all acceptance criteria?)\n"
            f"   ## Blocking Issues\n"
            f"   ## Non-Blocking Issues\n"
            f"   ## Suggestions\n"
            f"   ## Test Coverage Assessment\n"
            f"   ## Conclusion: APPROVED / APPROVED WITH CHANGES / REJECTED\n"
            f"   ```\n"
            f"5. Store your review in the scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat REVIEW.md)\n"
            + _write_snippet(review_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be rigorous. Do not approve code that fails tests or violates the spec."
        )

        # ---------------------------------------------------------------
        # Submit the 5-step DAG (linear pipeline)
        # ---------------------------------------------------------------
        sw_task = await orchestrator.submit_task(
            spec_writer_prompt,
            required_tags=body.spec_writer_tags or None,
        )
        arch_task = await orchestrator.submit_task(
            architect_prompt,
            required_tags=body.architect_tags or None,
            depends_on=[sw_task.id],
        )
        tw_task = await orchestrator.submit_task(
            test_writer_prompt,
            required_tags=body.test_writer_tags or None,
            depends_on=[arch_task.id],
        )
        impl_task = await orchestrator.submit_task(
            implementer_prompt,
            required_tags=body.implementer_tags or None,
            depends_on=[tw_task.id],
        )
        rev_task = await orchestrator.submit_task(
            reviewer_prompt,
            required_tags=body.reviewer_tags or None,
            depends_on=[impl_task.id],
            reply_to=body.reply_to,
        )

        task_ids_map = {
            "spec_writer": sw_task.id,
            "architect": arch_task.id,
            "test_writer": tw_task.id,
            "implementer": impl_task.id,
            "reviewer": rev_task.id,
        }

        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }

    @app.post(
        "/workflows/clean-arch",
        summary="Submit a 4-agent Clean Architecture pipeline workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_clean_arch_workflow(body: CleanArchWorkflowSubmit) -> dict:
        """Submit a 4-agent Clean Architecture Workflow DAG.

        Pipeline (each step depends_on the previous):

        1. **domain-designer**: defines domain Entities, Value Objects, Aggregates,
           and Domain Events without any framework dependency; writes ``DOMAIN.md``
           and stores it in the shared scratchpad.
        2. **usecase-designer**: reads the domain layer; defines Use Cases
           (Application Interactors), Input/Output DTOs, and Port interfaces (abstract
           boundaries); writes ``USECASES.md`` and stores it.
        3. **adapter-designer**: reads domain + use-cases; defines concrete Interface
           Adapters (Repository implementations, Presenters, Controllers); writes
           ``ADAPTERS.md`` and stores it.
        4. **framework-designer**: reads all previous layers; synthesises the complete
           architecture into ``ARCHITECTURE.md`` plus writes executable Python skeleton
           files demonstrating framework wiring (FastAPI/SQLite/CLI); stores the result.

        All artifacts are passed via the shared scratchpad (Blackboard pattern).
        Scratchpad keys use underscores (not slashes) as namespace separator.

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``clean-arch/<feature>``)
        - ``task_ids``: dict with keys ``domain_designer``, ``usecase_designer``,
          ``adapter_designer``, ``framework_designer`` mapping to global task IDs
        - ``scratchpad_prefix``: scratchpad namespace for this run
          (e.g. ``cleanarch_{run_id[:8]}``)

        Design references:
        - Robert C. Martin, "Clean Architecture" (2017): Domain → Use Cases →
          Interface Adapters → Frameworks & Drivers.
        - AgentMesh arXiv:2507.19902 (2025): 4-role artifact-centric pipeline.
        - Muthu (2025-11) "The Architecture is the Prompt".
        - DESIGN.md §10.30 (v1.0.30)
        """
        import uuid as _uuid  # noqa: PLC0415

        lang = body.language
        wm = orchestrator.get_workflow_manager()
        wf_name = f"clean-arch/{body.feature}"

        pre_run_id = str(_uuid.uuid4())
        scratchpad_prefix = f"cleanarch_{pre_run_id[:8]}"

        # Scratchpad keys (underscores only — no slashes)
        domain_key = f"{scratchpad_prefix}_domain"
        usecases_key = f"{scratchpad_prefix}_usecases"
        adapters_key = f"{scratchpad_prefix}_adapters"
        arch_key = f"{scratchpad_prefix}_arch"

        # Shared bash snippet for reading context + API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )

        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        # ---------------------------------------------------------------
        # 1. DOMAIN-DESIGNER prompt
        # ---------------------------------------------------------------
        domain_designer_prompt = (
            f"You are the DOMAIN-DESIGNER agent in a Clean Architecture workflow.\n"
            f"\n"
            f"**Feature to design:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your role is to define the innermost layer of Clean Architecture:\n"
            f"the **Domain layer** (Entities, Value Objects, Aggregates, Domain Events).\n"
            f"This layer must have ZERO framework or infrastructure dependencies.\n"
            f"\n"
            f"Steps:\n"
            f"1. Write `DOMAIN.md` in your working directory with this structure:\n"
            f"   ```markdown\n"
            f"   # Domain Layer: {body.feature}\n"
            f"   ## Entities\n"
            f"   (Core business objects with identity — list each with fields and invariants)\n"
            f"   ## Value Objects\n"
            f"   (Immutable descriptors without identity)\n"
            f"   ## Aggregates\n"
            f"   (Consistency boundaries — specify aggregate root)\n"
            f"   ## Domain Events\n"
            f"   (State changes that domain experts care about)\n"
            f"   ## Domain Rules / Invariants\n"
            f"   (Business rules that must always hold)\n"
            f"   ## Ubiquitous Language\n"
            f"   (Key terms with precise definitions)\n"
            f"   ```\n"
            f"2. Write stub {lang} classes for each Entity and Value Object in\n"
            f"   `domain.py` (pure {lang} — no external imports except stdlib).\n"
            f"3. Store DOMAIN.md in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat DOMAIN.md)\n"
            + _write_snippet(domain_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Focus ONLY on the domain model. Do NOT define use cases, repositories,\n"
            f"or any infrastructure. Keep it under 400 words."
        )

        # ---------------------------------------------------------------
        # 2. USECASE-DESIGNER prompt
        # ---------------------------------------------------------------
        usecase_designer_prompt = (
            f"You are the USECASE-DESIGNER agent in a Clean Architecture workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your role is to define the **Application / Use-Case layer** of Clean\n"
            f"Architecture: Use Cases (Interactors), Input/Output DTOs, and Port\n"
            f"interfaces (abstract boundaries the domain defines for infrastructure).\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the Domain layer from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(domain_key, "DOMAIN")
            + f"   echo \"Domain loaded: $(echo $DOMAIN | wc -c) chars\"\n"
            f"   ```\n"
            f"2. Write `USECASES.md` with this structure:\n"
            f"   ```markdown\n"
            f"   # Use-Case Layer: {body.feature}\n"
            f"   ## Use Cases (Interactors)\n"
            f"   (List each use case: name, input DTO fields, output DTO fields,\n"
            f"    steps, domain rules invoked)\n"
            f"   ## Port Interfaces\n"
            f"   (Abstract interfaces the use cases need — e.g. IRepository, INotifier)\n"
            f"   ## Input / Output DTOs\n"
            f"   (Plain data structures crossing the use-case boundary)\n"
            f"   ```\n"
            f"3. Write stub {lang} abstract classes / protocols for each Port in\n"
            f"   `ports.py` (stdlib `abc.ABC` only, no framework imports).\n"
            f"4. Store USECASES.md in the scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat USECASES.md)\n"
            + _write_snippet(usecases_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Derive use cases strictly from the Domain layer above.\n"
            f"Do NOT define adapters or framework wiring. Max 400 words."
        )

        # ---------------------------------------------------------------
        # 3. ADAPTER-DESIGNER prompt
        # ---------------------------------------------------------------
        adapter_designer_prompt = (
            f"You are the ADAPTER-DESIGNER agent in a Clean Architecture workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your role is to define the **Interface Adapters layer** of Clean\n"
            f"Architecture: concrete implementations of Port interfaces (Repositories,\n"
            f"Presenters, Controllers / Request Handlers).\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the Domain and Use-Case layers from the scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(domain_key, "DOMAIN")
            + _read_snippet(usecases_key, "USECASES")
            + f"   echo \"Domain + Use-Cases loaded\"\n"
            f"   ```\n"
            f"2. Write `ADAPTERS.md` with this structure:\n"
            f"   ```markdown\n"
            f"   # Interface Adapters Layer: {body.feature}\n"
            f"   ## Repository Adapters\n"
            f"   (Concrete implementations of IRepository ports — e.g. SQLite, in-memory)\n"
            f"   ## Presenter Adapters\n"
            f"   (Transforms use-case output DTOs to view models / JSON responses)\n"
            f"   ## Controller / Request Handler Adapters\n"
            f"   (Translates HTTP/CLI input to use-case input DTOs)\n"
            f"   ## Adapter Dependency Map\n"
            f"   (Which adapter implements which port)\n"
            f"   ```\n"
            f"3. Write stub {lang} classes for each adapter in `adapters.py`.\n"
            f"   - Repositories may import sqlite3 or similar stdlib only.\n"
            f"   - Implement the Port abstract interfaces from ports.py.\n"
            f"4. Store ADAPTERS.md in the scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat ADAPTERS.md)\n"
            + _write_snippet(adapters_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Adapters implement Ports — they must NOT import domain entities directly\n"
            f"except through the Port interfaces. Max 400 words."
        )

        # ---------------------------------------------------------------
        # 4. FRAMEWORK-DESIGNER prompt
        # ---------------------------------------------------------------
        framework_designer_prompt = (
            f"You are the FRAMEWORK-DESIGNER agent in a Clean Architecture workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your role is to synthesise ALL previous layers and define the outermost\n"
            f"**Frameworks & Drivers layer** of Clean Architecture: the wiring that\n"
            f"connects adapters to use cases via dependency injection.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read all previous layers from the scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(domain_key, "DOMAIN")
            + _read_snippet(usecases_key, "USECASES")
            + _read_snippet(adapters_key, "ADAPTERS")
            + f"   echo \"All layers loaded\"\n"
            f"   ```\n"
            f"2. Write `ARCHITECTURE.md` synthesising the complete design:\n"
            f"   ```markdown\n"
            f"   # Clean Architecture: {body.feature}\n"
            f"   ## Layer Overview\n"
            f"   (Concentric rings: Domain → Use Cases → Interface Adapters → Frameworks)\n"
            f"   ## Dependency Rule\n"
            f"   (Verify: each layer only imports inward)\n"
            f"   ## Framework Wiring\n"
            f"   (How FastAPI / SQLite / CLI entry points connect to adapters and use cases)\n"
            f"   ## Module Structure\n"
            f"   (directory tree with file → layer mapping)\n"
            f"   ## Dependency Injection Strategy\n"
            f"   (How adapters are injected into interactors at startup)\n"
            f"   ## Key Design Decisions\n"
            f"   ```\n"
            f"3. Write `main.py` as the composition root that wires everything together:\n"
            f"   - Instantiates repository adapters\n"
            f"   - Injects them into use-case interactors\n"
            f"   - Exposes a simple CLI or HTTP entry point\n"
            f"   Keep it under 60 lines.\n"
            f"4. Store ARCHITECTURE.md in the scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat ARCHITECTURE.md)\n"
            + _write_snippet(arch_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"The composition root (main.py) is the ONLY place where all layers touch.\n"
            f"Verify the Dependency Rule: inner layers must NOT import outer layers."
        )

        # ---------------------------------------------------------------
        # Submit the 4-step DAG (linear pipeline)
        # ---------------------------------------------------------------
        domain_task = await orchestrator.submit_task(
            domain_designer_prompt,
            required_tags=body.domain_designer_tags or None,
        )
        usecase_task = await orchestrator.submit_task(
            usecase_designer_prompt,
            required_tags=body.usecase_designer_tags or None,
            depends_on=[domain_task.id],
        )
        adapter_task = await orchestrator.submit_task(
            adapter_designer_prompt,
            required_tags=body.adapter_designer_tags or None,
            depends_on=[usecase_task.id],
        )
        framework_task = await orchestrator.submit_task(
            framework_designer_prompt,
            required_tags=body.framework_designer_tags or None,
            depends_on=[adapter_task.id],
            reply_to=body.reply_to,
        )

        task_ids_map = {
            "domain_designer": domain_task.id,
            "usecase_designer": usecase_task.id,
            "adapter_designer": adapter_task.id,
            "framework_designer": framework_task.id,
        }

        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }

    @app.post(
        "/workflows/ddd",
        summary="Submit a DDD Bounded Context decomposition workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_ddd_workflow(body: DDDWorkflowSubmit) -> dict:
        """Submit a DDD Bounded Context Decomposition Workflow DAG.

        Three-phase pipeline:

        1. **context-mapper** (Phase 1 — sequential): performs EventStorming
           analysis on the topic; identifies Bounded Contexts and their Ubiquitous
           Language; writes ``EVENTSTORMING.md`` and ``BOUNDED_CONTEXTS.md``;
           stores context list in scratchpad under ``{prefix}_contexts``.

        2. **domain-expert-{context}** (Phase 2 — parallel, one per context):
           reads ``BOUNDED_CONTEXTS.md``; designs the domain model for its
           assigned context (Entities, Aggregates, Value Objects, Domain Services);
           writes ``DOMAIN_{CONTEXT}.md`` and stores in ``{prefix}_domain_{context}``.

        3. **integration-designer** (Phase 3 — sequential, depends on all domain
           experts): reads all domain models; produces ``CONTEXT_MAP.md`` with
           context-mapping patterns (Shared Kernel / Customer–Supplier / ACL)
           between every pair of Bounded Contexts.

        All artifacts are passed via the shared scratchpad (Blackboard pattern).
        Scratchpad keys use underscores (not slashes) as namespace separator.

        If ``contexts`` is supplied in the request body, those names are used
        directly (skipping autonomous discovery). If omitted, the context-mapper
        discovers them from the topic description.

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``ddd/<topic>``)
        - ``task_ids``: dict with keys ``context_mapper``,
          ``domain_expert_{context}`` (one per context), and
          ``integration_designer``, mapping to global task IDs
        - ``scratchpad_prefix``: scratchpad namespace for this run
          (e.g. ``ddd_{run_id[:8]}``)

        Design references:
        - Evans, "Domain-Driven Design" (2003): Bounded Context + Ubiquitous Language.
        - IJCSE V12I3P102 (2025): EventStorming → agent communication protocols.
        - Russ Miles, "Domain-Driven Agent Design", 2025 (DICE framework).
        - DESIGN.md §10.31 (v1.0.31)
        """
        import uuid as _uuid  # noqa: PLC0415

        lang = body.language
        topic = body.topic
        wm = orchestrator.get_workflow_manager()
        wf_name = f"ddd/{topic}"

        pre_run_id = str(_uuid.uuid4())
        scratchpad_prefix = f"ddd_{pre_run_id[:8]}"

        # Scratchpad keys
        contexts_key = f"{scratchpad_prefix}_contexts"
        bounded_contexts_key = f"{scratchpad_prefix}_bounded_contexts"

        # Shared bash snippet for reading context + API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )

        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        # Determine contexts list: use provided or defer to agent discovery
        contexts: list[str] = [c.strip() for c in body.contexts if c.strip()]
        contexts_hint = (
            f"Use these Bounded Context names exactly: {', '.join(contexts)}"
            if contexts
            else "Identify 2–4 Bounded Contexts autonomously from the topic description."
        )

        # ---------------------------------------------------------------
        # Phase 1: CONTEXT-MAPPER prompt
        # ---------------------------------------------------------------
        context_mapper_prompt = (
            f"You are the CONTEXT-MAPPER agent in a DDD (Domain-Driven Design) workflow.\n"
            f"\n"
            f"**Topic:** {topic}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your role is to perform an **EventStorming** analysis and identify the\n"
            f"**Bounded Contexts** that structure the domain.\n"
            f"\n"
            f"{contexts_hint}\n"
            f"\n"
            f"Steps:\n"
            f"1. Write `EVENTSTORMING.md` in your working directory:\n"
            f"   ```markdown\n"
            f"   # EventStorming: {topic}\n"
            f"   ## Domain Events\n"
            f"   (Orange stickies — past-tense facts: 'OrderPlaced', 'PaymentFailed')\n"
            f"   ## Commands\n"
            f"   (Blue stickies — actions that trigger events: 'PlaceOrder', 'ProcessPayment')\n"
            f"   ## Aggregates\n"
            f"   (Yellow stickies — clusters of related events and commands)\n"
            f"   ## Bounded Contexts Identified\n"
            f"   (List each context name with a 1-sentence purpose)\n"
            f"   ```\n"
            f"2. Write `BOUNDED_CONTEXTS.md` in your working directory:\n"
            f"   ```markdown\n"
            f"   # Bounded Contexts: {topic}\n"
            f"   (For each context, include:)\n"
            f"   ## <Context Name>\n"
            f"   **Purpose:** <one sentence>\n"
            f"   **Ubiquitous Language:** <key domain terms specific to this context>\n"
            f"   **Core Aggregates:** <names>\n"
            f"   **Inbound Events:** <events this context reacts to>\n"
            f"   **Outbound Events:** <events this context produces>\n"
            f"   ```\n"
            f"3. Store BOUNDED_CONTEXTS.md in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat BOUNDED_CONTEXTS.md)\n"
            + _write_snippet(bounded_contexts_key)
            + f"\n"
            f"   ```\n"
            f"4. Store the comma-separated list of context names (no spaces around commas):\n"
            f"   ```bash\n"
            f"   # Example: CONTEXT_NAMES='Orders,Inventory,Shipping'\n"
            f"   CONTEXT_NAMES='<comma-separated list>'\n"
            + _write_snippet(contexts_key, "CONTEXT_NAMES")
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Keep EVENTSTORMING.md and BOUNDED_CONTEXTS.md under 600 words combined.\n"
            f"Focus on strategic design — do NOT write implementation code."
        )

        # ---------------------------------------------------------------
        # Phase 2: DOMAIN-EXPERT prompts (one per context)
        # ---------------------------------------------------------------
        # Build per-context scratchpad keys and prompts
        def _domain_key(ctx_name: str) -> str:
            safe = ctx_name.lower().replace(" ", "_").replace("-", "_")
            return f"{scratchpad_prefix}_domain_{safe}"

        def _domain_expert_prompt(ctx_name: str) -> str:
            dk = _domain_key(ctx_name)
            safe = ctx_name.lower().replace(" ", "_").replace("-", "_")
            return (
                f"You are the DOMAIN-EXPERT agent for the **{ctx_name}** Bounded Context\n"
                f"in a DDD workflow.\n"
                f"\n"
                f"**Overall Topic:** {topic}\n"
                f"**Your Bounded Context:** {ctx_name}\n"
                f"**Language:** {lang}\n"
                f"\n"
                f"Steps:\n"
                f"1. Read the full Bounded Contexts overview from the shared scratchpad:\n"
                f"   ```bash\n"
                f"   {_ctx_snippet}\n"
                + _read_snippet(bounded_contexts_key, "BOUNDED_CONTEXTS")
                + f"   echo \"Bounded contexts loaded: $(echo $BOUNDED_CONTEXTS | wc -c) chars\"\n"
                f"   ```\n"
                f"2. Write `DOMAIN_{safe.upper()}.md` in your working directory:\n"
                f"   ```markdown\n"
                f"   # Domain Model: {ctx_name}\n"
                f"   ## Entities\n"
                f"   (Objects with identity — list each with fields and invariants)\n"
                f"   ## Value Objects\n"
                f"   (Immutable descriptors without identity)\n"
                f"   ## Aggregates\n"
                f"   (Consistency boundaries — specify aggregate root and invariants)\n"
                f"   ## Domain Services\n"
                f"   (Stateless operations that don't belong to a single aggregate)\n"
                f"   ## Domain Events\n"
                f"   (Past-tense facts this context produces)\n"
                f"   ## Ubiquitous Language\n"
                f"   (Term → definition, unique to this context)\n"
                f"   ```\n"
                f"3. Write stub {lang} classes for entities/value objects in\n"
                f"   `domain_{safe}.py` (pure {lang} — stdlib only, no frameworks).\n"
                f"4. Store the domain model in the shared scratchpad:\n"
                f"   ```bash\n"
                f"   CONTENT=$(cat DOMAIN_{safe.upper()}.md)\n"
                + _write_snippet(dk)
                + f"\n"
                f"   ```\n"
                f"\n"
                f"Focus ONLY on the {ctx_name} context. Do NOT design other contexts.\n"
                f"Keep it under 400 words."
            )

        # ---------------------------------------------------------------
        # Phase 3: INTEGRATION-DESIGNER prompt
        # ---------------------------------------------------------------
        def _integration_designer_prompt(ctx_names: list[str]) -> str:
            read_all = ""
            for ctx in ctx_names:
                dk = _domain_key(ctx)
                safe_var = ctx.upper().replace(" ", "_").replace("-", "_")
                read_all += _read_snippet(dk, f"DOMAIN_{safe_var}")
            return (
                f"You are the INTEGRATION-DESIGNER agent in a DDD workflow.\n"
                f"\n"
                f"**Topic:** {topic}\n"
                f"**Language:** {lang}\n"
                f"\n"
                f"Your role is to produce a **Context Map** — the strategic diagram\n"
                f"that shows how the Bounded Contexts relate and integrate.\n"
                f"\n"
                f"Steps:\n"
                f"1. Read all domain models from the shared scratchpad:\n"
                f"   ```bash\n"
                f"   {_ctx_snippet}\n"
                + _read_snippet(bounded_contexts_key, "BOUNDED_CONTEXTS")
                + read_all
                + f"   echo \"All domain models loaded\"\n"
                f"   ```\n"
                f"2. Write `CONTEXT_MAP.md` in your working directory:\n"
                f"   ```markdown\n"
                f"   # Context Map: {topic}\n"
                f"   ## Summary\n"
                f"   (1–2 sentences: what the full domain does)\n"
                f"   ## Bounded Contexts\n"
                f"   (Recap: one line per context with its core responsibility)\n"
                f"   ## Context Relationships\n"
                f"   (For each pair of contexts that interact, specify the pattern:)\n"
                f"   ### <Context A> ↔ <Context B>\n"
                f"   **Pattern:** Shared Kernel | Customer–Supplier | Conformist | ACL | Published Language\n"
                f"   **Direction:** <upstream> → <downstream>\n"
                f"   **Shared Concepts:** <terms or events shared across the boundary>\n"
                f"   **Integration Mechanism:** <domain events / REST API / message queue / direct call>\n"
                f"   ## Anti-Corruption Layers\n"
                f"   (List any ACLs required and what they translate)\n"
                f"   ## Shared Kernel\n"
                f"   (List any truly shared types, if applicable; otherwise 'None')\n"
                f"   ## Key Design Decisions\n"
                f"   (Rationale for the chosen integration patterns)\n"
                f"   ```\n"
                f"3. Store CONTEXT_MAP.md in the shared scratchpad:\n"
                f"   ```bash\n"
                f"   CONTENT=$(cat CONTEXT_MAP.md)\n"
                + _write_snippet(f"{scratchpad_prefix}_context_map")
                + f"\n"
                f"   ```\n"
                f"\n"
                f"Use standard DDD context-mapping patterns (Evans 2003, Vernon 2013).\n"
                f"Keep CONTEXT_MAP.md under 600 words."
            )

        # ---------------------------------------------------------------
        # Submit the DAG
        # Phase 1: context-mapper (no dependencies)
        # Phase 2: domain-expert-N (each depends on context-mapper)
        # Phase 3: integration-designer (depends on ALL domain-experts)
        # ---------------------------------------------------------------
        context_mapper_task = await orchestrator.submit_task(
            context_mapper_prompt,
            required_tags=body.context_mapper_tags or None,
        )

        # For provided contexts, submit phase 2 immediately (with dependency on context-mapper).
        # If contexts not provided, we still need to submit the domain-expert tasks; we use the
        # provided-contexts path. When contexts=[], we fall back to a single placeholder context
        # that instructs the agent to read the context list from scratchpad.
        effective_contexts = contexts if contexts else ["(auto-discovered)"]

        domain_expert_tasks = []
        task_ids_map: dict[str, str] = {"context_mapper": context_mapper_task.id}

        if contexts:
            # Contexts are known — submit domain-expert tasks now
            for ctx_name in contexts:
                prompt = _domain_expert_prompt(ctx_name)
                t = await orchestrator.submit_task(
                    prompt,
                    required_tags=body.domain_expert_tags or None,
                    depends_on=[context_mapper_task.id],
                )
                safe = ctx_name.lower().replace(" ", "_").replace("-", "_")
                task_ids_map[f"domain_expert_{safe}"] = t.id
                domain_expert_tasks.append(t)
        else:
            # Contexts not provided — submit a single domain-expert task that reads
            # the context list from scratchpad and designs all contexts in one pass.
            auto_prompt = (
                f"You are the DOMAIN-EXPERT agent in a DDD workflow.\n"
                f"\n"
                f"**Topic:** {topic}\n"
                f"**Language:** {lang}\n"
                f"\n"
                f"The context-mapper agent has already identified the Bounded Contexts.\n"
                f"Read the context list and design ALL domain models in a single pass.\n"
                f"\n"
                f"Steps:\n"
                f"1. Read the Bounded Contexts overview from the shared scratchpad:\n"
                f"   ```bash\n"
                f"   {_ctx_snippet}\n"
                + _read_snippet(bounded_contexts_key, "BOUNDED_CONTEXTS")
                + _read_snippet(contexts_key, "CONTEXT_NAMES")
                + f"   echo \"Contexts: $CONTEXT_NAMES\"\n"
                f"   ```\n"
                f"2. For EACH context in CONTEXT_NAMES, write a `DOMAIN_<CONTEXT>.md` file\n"
                f"   with Entities, Value Objects, Aggregates, Domain Services, Domain Events,\n"
                f"   and Ubiquitous Language sections.\n"
                f"3. Store each domain model in the scratchpad:\n"
                f"   ```bash\n"
                f"   for CTX in $(echo $CONTEXT_NAMES | tr ',' ' '); do\n"
                f"     SAFE=$(echo $CTX | tr '[:upper:]' '[:lower:]' | tr ' -' '_')\n"
                f"     CONTENT=$(cat DOMAIN_${{CTX}}.md 2>/dev/null || echo \"(empty)\")\n"
                + _write_snippet(f"{scratchpad_prefix}_domain_$SAFE")
                + f"\n"
                f"   done\n"
                f"   ```\n"
                f"\n"
                f"Keep each domain model under 300 words. Focus on strategic design."
            )
            t = await orchestrator.submit_task(
                auto_prompt,
                required_tags=body.domain_expert_tags or None,
                depends_on=[context_mapper_task.id],
            )
            task_ids_map["domain_expert_auto"] = t.id
            domain_expert_tasks.append(t)

        # Phase 3: integration-designer depends on ALL domain-expert tasks
        integration_prompt = _integration_designer_prompt(
            contexts if contexts else ["(all contexts from scratchpad)"]
        )
        integration_task = await orchestrator.submit_task(
            integration_prompt,
            required_tags=body.integration_designer_tags or None,
            depends_on=[t.id for t in domain_expert_tasks],
            reply_to=body.reply_to,
        )
        task_ids_map["integration_designer"] = integration_task.id

        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }

    @app.post(
        "/workflows/competition",
        summary="Submit a Best-of-N competitive solver workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_competition_workflow(body: CompetitionWorkflowSubmit) -> dict:
        """Submit a Best-of-N competitive solver Workflow DAG.

        Spawns N solver agents in **parallel** (all ``depends_on=[]``), each
        applying a different strategy to the same problem.  After all solvers
        complete, a **judge** agent reads every solver's result from the shared
        scratchpad, compares them against the scoring criterion, and writes
        ``COMPETITION_RESULT.md`` declaring the winner.

        Workflow topology::

            solver_strategy_0 ──┐
            solver_strategy_1 ──┼─→ judge
            solver_strategy_2 ──┘

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``competition/<problem[:40]>``)
        - ``task_ids``: dict with keys ``solver_{strategy}`` (one per strategy)
          and ``judge``, mapping to global task IDs
        - ``scratchpad_prefix``: scratchpad namespace for this run
          (e.g. ``competition_{run_id[:8]}``)

        Design references:
        - "Making, not Taking, the Best of N" (FusioN), arXiv:2510.00931, 2025.
        - M-A-P "Multi-Agent Parallel Test-Time Scaling", arXiv:2506.12928, 2025.
        - "When AIs Judge AIs: Agent-as-a-Judge", arXiv:2508.02994, 2025.
        - MultiAgentBench, arXiv:2503.01935, 2025.
        - DESIGN.md §10.36 (v1.1.0)
        """
        import uuid as _uuid  # noqa: PLC0415

        wm = orchestrator.get_workflow_manager()
        problem_slug = body.problem[:40].strip().replace("\n", " ")
        wf_name = f"competition/{problem_slug}"

        pre_run_id = str(_uuid.uuid4())
        scratchpad_prefix = f"competition_{pre_run_id[:8]}"

        # Shared Python snippet for reading orchestrator context + API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _write_snippet(key: str, filename: str) -> str:
            """Python3-based scratchpad write snippet (handles quotes/newlines)."""
            return (
                f"   python3 -c \"\n"
                f"import json, urllib.request, os\n"
                f"content = open('{filename}', 'r', errors='replace').read()\n"
                f"payload = json.dumps({{'value': content}}).encode()\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"if not api_key:\n"
                f"    try: api_key = open('__orchestrator_api_key__').read().strip()\n"
                f"    except: pass\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"base_url = ctx['web_base_url']\n"
                f"req = urllib.request.Request(base_url + '/scratchpad/{key}', data=payload, method='PUT')\n"
                f"req.add_header('Content-Type', 'application/json')\n"
                f"req.add_header('X-API-Key', api_key)\n"
                f"urllib.request.urlopen(req, timeout=15)\n"
                f"print('Stored to scratchpad: {key}')\n"
                f"\"  "
            )

        task_ids_map: dict[str, str] = {}
        solver_task_ids: list[str] = []

        # Phase 1: one solver per strategy, all in parallel (depends_on=[])
        for strategy in body.strategies:
            # Sanitise strategy name for use in file/key names
            safe_strategy = strategy.strip().replace(" ", "_").replace("/", "_")
            solver_key = f"{scratchpad_prefix}_solver_{safe_strategy}"
            result_filename = f"solver_{safe_strategy}_result.md"

            solver_prompt = (
                f"You are a SOLVER agent competing in a Best-of-N competition.\n"
                f"\n"
                f"**Problem:**\n"
                f"{body.problem}\n"
                f"\n"
                f"**Your strategy:** {strategy}\n"
                f"**Scoring criterion:** {body.scoring_criterion}\n"
                f"\n"
                f"Your tasks:\n"
                f"1. Implement a solution to the problem using the **{strategy}** strategy.\n"
                f"   - Be concrete: write actual code or a detailed algorithmic procedure.\n"
                f"   - Optimise for the scoring criterion: {body.scoring_criterion}.\n"
                f"2. Evaluate your own solution against the scoring criterion.\n"
                f"   Produce a single numeric score (integer or float).\n"
                f"3. Write `{result_filename}` with this exact format:\n"
                f"   ```markdown\n"
                f"   # Solver Result: {strategy}\n"
                f"   ## Strategy\n"
                f"   <description of your approach>\n"
                f"   ## Solution\n"
                f"   <your full solution / code>\n"
                f"   ## Self-Evaluation\n"
                f"   <how you assessed the score>\n"
                f"   SCORE: <number>\n"
                f"   ```\n"
                f"   The `SCORE: <number>` line MUST appear at the end of the file.\n"
                f"4. Store your result in the shared scratchpad:\n"
                f"   ```python\n"
                + _write_snippet(solver_key, result_filename)
                + f"\n"
                f"   ```\n"
                f"\n"
                f"Be thorough and honest in your self-evaluation. "
                f"A higher numeric score means a better solution."
            )

            solver_task = await orchestrator.submit_task(
                solver_prompt,
                required_tags=body.solver_tags or None,
                depends_on=[],
            )
            role_key = f"solver_{safe_strategy}"
            task_ids_map[role_key] = solver_task.id
            solver_task_ids.append(solver_task.id)

        # Phase 2: judge reads all solver results and picks the winner
        solver_keys_desc = "\n".join(
            f"   - strategy '{s}': key `{scratchpad_prefix}_solver_{s.strip().replace(' ', '_').replace('/', '_')}`"
            for s in body.strategies
        )
        judge_key = f"{scratchpad_prefix}_judge"

        judge_prompt = (
            f"You are the JUDGE agent in a Best-of-N competitive solver workflow.\n"
            f"\n"
            f"**Problem:**\n"
            f"{body.problem}\n"
            f"\n"
            f"**Scoring criterion:** {body.scoring_criterion}\n"
            f"**Strategies competing:** {', '.join(body.strategies)}\n"
            f"\n"
            f"Your tasks:\n"
            f"1. Set up credentials:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   ```\n"
            f"2. Read ALL solver results from the shared scratchpad:\n"
            f"   Keys to read:\n"
            f"{solver_keys_desc}\n"
            f"   Use curl to read each key:\n"
            f"   `curl -s -H \"X-API-Key: $API_KEY\" \"$WEB_BASE_URL/scratchpad/<key>\"`\n"
            f"\n"
            f"3. For each solver result:\n"
            f"   - Extract the numeric score from the `SCORE: <number>` line.\n"
            f"   - Read the solution and self-evaluation.\n"
            f"\n"
            f"4. Write `COMPETITION_RESULT.md` with this exact format:\n"
            f"   ```markdown\n"
            f"   # Competition Result\n"
            f"   ## Problem\n"
            f"   <brief summary>\n"
            f"   ## Scoring Criterion\n"
            f"   {body.scoring_criterion}\n"
            f"   ## Scores\n"
            f"   | Strategy | Score |\n"
            f"   |----------|-------|\n"
            f"   | <strategy> | <score> |\n"
            f"   ...\n"
            f"   ## Winner\n"
            f"   WINNER: <winning_strategy_name>\n"
            f"   ## Rationale\n"
            f"   <why this strategy won>\n"
            f"   ## Runner-up\n"
            f"   <second-best strategy and its score>\n"
            f"   ```\n"
            f"   The `WINNER: <strategy>` line MUST appear in the ## Winner section.\n"
            f"\n"
            f"5. Store the competition result in the shared scratchpad:\n"
            f"   ```python\n"
            + _write_snippet(judge_key, "COMPETITION_RESULT.md")
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be objective. Select the winner based solely on the numeric scores "
            f"and the scoring criterion: {body.scoring_criterion}. "
            f"If two strategies tie on score, prefer the one whose solution is "
            f"simpler and more maintainable."
        )

        judge_task = await orchestrator.submit_task(
            judge_prompt,
            required_tags=body.judge_tags or None,
            depends_on=solver_task_ids,
            reply_to=body.reply_to,
        )
        task_ids_map["judge"] = judge_task.id

        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }

    @app.get(
        "/workflows",
        summary="List all workflow runs",
        dependencies=[Depends(auth)],
    )
    async def list_workflows() -> list:
        """Return a list of all submitted workflow runs and their current status.

        Each entry contains:
        - ``id``: workflow run UUID
        - ``name``: name given at submission
        - ``task_ids``: ordered list of global orchestrator task IDs
        - ``status``: ``"pending"`` | ``"running"`` | ``"complete"`` | ``"failed"``
        - ``created_at``: Unix timestamp of submission
        - ``completed_at``: Unix timestamp when all tasks finished, or ``null``
        - ``tasks_total``: total number of tasks in the workflow
        - ``tasks_done``: tasks that have finished (succeeded + failed)
        - ``tasks_failed``: tasks that failed

        Design reference: DESIGN.md §10.20 (v0.25.0).
        """
        return orchestrator.get_workflow_manager().list_all()

    @app.get(
        "/workflows/{workflow_id}",
        summary="Get a specific workflow run status",
        dependencies=[Depends(auth)],
    )
    async def get_workflow(workflow_id: str) -> dict:
        """Return the status and task list for *workflow_id*.

        Returns 404 if the workflow ID is unknown.

        Design reference: DESIGN.md §10.20 (v0.25.0).
        """
        result = orchestrator.get_workflow_manager().status(workflow_id)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"Workflow {workflow_id!r} not found",
            )
        return result

    @app.delete(
        "/tasks/{task_id}",
        summary="Cancel a task by ID (queued or in-progress)",
        dependencies=[Depends(auth)],
    )
    async def delete_task(task_id: str) -> dict:
        """Cancel *task_id* whether it is queued or currently in-progress.

        - If the task is **queued**: removes it from the priority queue and
          publishes STATUS ``task_cancelled``.
        - If the task is **in-progress**: marks it as cancelled (tombstone),
          sends Ctrl-C to the agent via ``interrupt()``, and publishes STATUS
          ``task_cancelled``.  The eventual RESULT from the agent is silently
          discarded.
        - If the task is **already completed/failed/unknown**: returns 404.

        Returns:
        ``{"cancelled": true, "task_id": ..., "was_running": <bool>}``

        Design references:
        - Kubernetes ``kubectl delete pod`` — REST DELETE on a resource URI
        - POSIX SIGTERM/SIGKILL model; Go context.Context cancellation
        - DESIGN.md §10.22 (v0.27.0)
        """
        # Determine if the task was in-progress before cancellation attempt
        in_progress_ids = set()
        for agent in orchestrator.list_agents():
            agent_obj = orchestrator.get_agent(agent["id"])
            if agent_obj is not None and agent_obj._current_task is not None:
                in_progress_ids.add(agent_obj._current_task.id)

        cancelled = await orchestrator.cancel_task(task_id)
        if not cancelled:
            raise HTTPException(
                status_code=404,
                detail=f"Task {task_id!r} not found (already completed, unknown, or dead-lettered)",
            )
        was_running = task_id in in_progress_ids
        return {"cancelled": True, "task_id": task_id, "was_running": was_running}

    @app.delete(
        "/workflows/{workflow_id}",
        summary="Cancel all tasks in a workflow",
        dependencies=[Depends(auth)],
    )
    async def delete_workflow(workflow_id: str) -> dict:
        """Cancel all tasks belonging to *workflow_id* and mark it as cancelled.

        Cancels each task in the workflow (queued or in-progress) and sets the
        workflow status to ``"cancelled"``.

        Returns:
        ``{"workflow_id": ..., "cancelled": [...task_ids...], "already_done": [...task_ids...]}``

        - ``cancelled``: task IDs that were successfully cancelled.
        - ``already_done``: task IDs that were not found (already completed,
          dead-lettered, or unknown).

        Returns 404 if *workflow_id* is unknown.

        Design references:
        - Apache Airflow ``dag_run.update_state("cancelled")`` — bulk cancel
        - AWS Step Functions ``StopExecution`` — cancel a running state machine
        - DESIGN.md §10.22 (v0.27.0)
        """
        result = await orchestrator.cancel_workflow(workflow_id)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"Workflow {workflow_id!r} not found",
            )
        return result

    @app.post(
        "/tasks/{task_id}/cancel",
        summary="Cancel a pending task",
        dependencies=[Depends(auth)],
    )
    async def cancel_task(task_id: str) -> dict:
        """Remove *task_id* from the pending queue and discard it.

        Returns:
        - ``{"cancelled": true, "task_id": ..., "status": "cancelled"}``
          if the task was successfully removed from the queue.
        - ``{"cancelled": false, "task_id": ..., "status": "already_dispatched"}``
          if the task was not in the pending queue (already dispatched or
          currently in-flight).
        - ``404`` if the task ID has never been submitted or tracked.

        Design reference: Microsoft Azure "Asynchronous Request-Reply pattern"
        (2024): "A client can send an HTTP DELETE request on the URL provided
        by Location header when the task is submitted." We use POST on a verb
        sub-resource (action endpoint) since DELETE on /tasks/{id} could be
        ambiguous with resource deletion semantics.
        DESIGN.md §11 (v0.17.0) — task cancellation.
        """
        # Snapshot the pending queue before attempting cancellation.
        queued_ids = {t["task_id"] for t in orchestrator.list_tasks()}
        was_queued = task_id in queued_ids

        cancelled = await orchestrator.cancel_task(task_id)

        if cancelled:
            return {"cancelled": True, "task_id": task_id, "status": "cancelled"}

        if was_queued:
            # Was in queue but got dispatched between our snapshot and cancel_task().
            # This is a race — treat as already dispatched, not as 404.
            return {"cancelled": False, "task_id": task_id, "status": "already_dispatched"}

        # Task was not in the queue — determine if it was ever tracked.
        # Check in-flight tasks (dispatched but result not yet received).
        in_flight = getattr(orchestrator, "_task_started_at", {})
        if task_id in in_flight:
            return {"cancelled": False, "task_id": task_id, "status": "already_dispatched"}

        # Check completed tasks.
        completed = getattr(orchestrator, "_completed_tasks", set())
        if task_id in completed:
            return {"cancelled": False, "task_id": task_id, "status": "already_dispatched"}

        # Check DLQ — dead-lettered tasks were also "dispatched" in the broad sense.
        dlq_ids = {e.get("task_id") for e in orchestrator.list_dlq()}
        if task_id in dlq_ids:
            return {"cancelled": False, "task_id": task_id, "status": "already_dispatched"}

        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    @app.get(
        "/tasks/{task_id}",
        summary="Get a specific task by ID",
        dependencies=[Depends(auth)],
    )
    async def get_task(task_id: str) -> dict:
        """Return the status and details of a specific task by its ID.

        Searches the pending queue, waiting queue, in-progress tasks, and
        per-agent history.

        Returns:
        - ``task_id``: unique task identifier
        - ``prompt``: task prompt text
        - ``priority``: dispatch priority
        - ``status``: one of ``"queued"``, ``"waiting"``, ``"in_progress"``, ``"success"``, ``"error"``
        - ``depends_on``: list of task IDs this task depends on (if any)
        - ``blocking``: list of task IDs that are waiting on this task (if any)
        - ``max_retries``: maximum allowed retries
        - ``retry_count``: current retry attempt count
        - 404 if the task ID is unknown.

        Design reference: DESIGN.md §10.21 (v0.26.0); DESIGN.md §10.24 (v0.29.0)
        """
        # 0. Check _waiting_tasks first (tasks held for dependency resolution)
        waiting_task = orchestrator.get_waiting_task(task_id)
        if waiting_task is not None:
            blocking = orchestrator._task_blocking(task_id)
            resp: dict = {
                "task_id": task_id,
                "prompt": waiting_task.prompt,
                "priority": waiting_task.priority,
                "status": "waiting",
                "depends_on": waiting_task.depends_on,
                "max_retries": waiting_task.max_retries,
                "retry_count": waiting_task.retry_count,
                "inherit_priority": waiting_task.inherit_priority,
                "submitted_at": waiting_task.submitted_at,
                "ttl": waiting_task.ttl,
                "expires_at": waiting_task.expires_at,
            }
            if blocking:
                resp["blocking"] = blocking
            if waiting_task.required_tags:
                resp["required_tags"] = waiting_task.required_tags
            if waiting_task.target_agent:
                resp["target_agent"] = waiting_task.target_agent
            return resp

        # 1. Check pending queue
        for item in orchestrator.list_tasks():
            if item["task_id"] == task_id:
                # Enrich with retry fields from _active_tasks if present
                active = orchestrator._active_tasks.get(task_id)
                blocking = orchestrator._task_blocking(task_id)
                resp = {
                    "task_id": task_id,
                    "prompt": item["prompt"],
                    "priority": item["priority"],
                    "status": item.get("status", "queued"),
                    "depends_on": item.get("depends_on", []),
                    "max_retries": active.max_retries if active else 0,
                    "retry_count": active.retry_count if active else 0,
                    "inherit_priority": active.inherit_priority if active else True,
                    "submitted_at": item.get("submitted_at"),
                    "ttl": item.get("ttl"),
                    "expires_at": item.get("expires_at"),
                }
                if blocking:
                    resp["blocking"] = blocking
                if item.get("required_tags"):
                    resp["required_tags"] = item["required_tags"]
                if item.get("target_agent"):
                    resp["target_agent"] = item["target_agent"]
                return resp

        # 2. Check in-progress tasks
        for agent in orchestrator.list_agents():
            agent_obj = orchestrator.get_agent(agent["id"])
            if agent_obj is not None and agent_obj._current_task is not None:
                ct = agent_obj._current_task
                if ct.id == task_id:
                    blocking = orchestrator._task_blocking(task_id)
                    resp = {
                        "task_id": ct.id,
                        "prompt": ct.prompt,
                        "priority": ct.priority,
                        "status": "in_progress",
                        "depends_on": ct.depends_on,
                        "agent_id": agent["id"],
                        "max_retries": ct.max_retries,
                        "retry_count": ct.retry_count,
                        "inherit_priority": ct.inherit_priority,
                        "submitted_at": ct.submitted_at,
                        "ttl": ct.ttl,
                        "expires_at": ct.expires_at,
                    }
                    if blocking:
                        resp["blocking"] = blocking
                    return resp

        # 3. Check per-agent history
        for agent in orchestrator.list_agents():
            history = orchestrator.get_agent_history(agent["id"], limit=200) or []
            for record in history:
                if record.get("task_id") == task_id:
                    active = orchestrator._active_tasks.get(task_id)
                    blocking = orchestrator._task_blocking(task_id)
                    hist_resp: dict = {
                        "task_id": task_id,
                        "prompt": record.get("prompt", ""),
                        "priority": 0,
                        "status": record.get("status", "unknown"),
                        "agent_id": agent["id"],
                        "started_at": record.get("started_at"),
                        "finished_at": record.get("finished_at"),
                        "duration_s": record.get("duration_s"),
                        "error": record.get("error"),
                        "max_retries": active.max_retries if active else 0,
                        "retry_count": active.retry_count if active else 0,
                    }
                    if active and active.depends_on:
                        hist_resp["depends_on"] = active.depends_on
                    if blocking:
                        hist_resp["blocking"] = blocking
                    return hist_resp

        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    @app.patch(
        "/tasks/{task_id}",
        summary="Update a pending task's priority",
        dependencies=[Depends(auth)],
    )
    async def update_task_priority(task_id: str, body: TaskPriorityUpdate) -> dict:
        """Update the priority of a task that is still in the pending queue.

        Rebuilds the internal heap after the in-place mutation so the new
        priority is respected on the next dispatch cycle.

        Returns:
        - ``{"updated": true, "task_id": ..., "priority": N}``
          if the task was found in the pending queue and its priority was changed.
        - ``{"updated": false, "task_id": ...}``
          if the task is not in the pending queue (already dispatched, completed,
          or never submitted).

        Design reference: Python heapq "Priority Queue Implementation Notes"
        (https://docs.python.org/3/library/heapq.html); Liu & Layland (1973)
        "Scheduling Algorithms for Multiprogramming in a Hard Real-Time
        Environment", JACM 20(1) — live priority adjustment prevents priority
        inversion and lets operators promote urgent work without re-submitting.
        """
        updated = await orchestrator.update_task_priority(task_id, body.priority)
        if updated:
            return {"updated": True, "task_id": task_id, "priority": body.priority}
        return {"updated": False, "task_id": task_id}

    # ------------------------------------------------------------------
    # Orchestrator dispatch control (pause / resume)
    # ------------------------------------------------------------------

    @app.post(
        "/orchestrator/pause",
        summary="Pause task dispatch",
        dependencies=[Depends(auth)],
    )
    async def pause_dispatch() -> dict:
        """Pause the orchestrator dispatch loop.

        While paused, no new tasks are dequeued from the pending queue.
        In-flight tasks (already dispatched to agents) continue to run
        normally.  New tasks can still be submitted to the queue via
        ``POST /tasks`` — they will be dispatched as soon as dispatch is
        resumed.

        Idempotent: calling pause on an already-paused orchestrator is safe.

        Design reference: Google Cloud Tasks ``queues.pause`` API; Oracle
        WebLogic Server "Pause queue message operations at runtime" — queue
        pause enables maintenance, rolling deploys, and controlled draining
        without dropping in-flight work.
        DESIGN.md §11 (v0.19.0) — queue pause/resume.
        """
        orchestrator.pause()
        return {"paused": True}

    @app.post(
        "/orchestrator/resume",
        summary="Resume task dispatch",
        dependencies=[Depends(auth)],
    )
    async def resume_dispatch() -> dict:
        """Resume the orchestrator dispatch loop after a pause.

        Idempotent: calling resume on an already-running orchestrator is safe.

        After resuming, the dispatch loop immediately checks the pending queue
        and dispatches any queued tasks to idle agents.
        """
        orchestrator.resume()
        return {"paused": False}

    @app.get(
        "/orchestrator/status",
        summary="Orchestrator operational status",
        dependencies=[Depends(auth)],
    )
    async def orchestrator_status() -> dict:
        """Return operational status of the orchestrator.

        Returns:
        - ``paused``: whether dispatch is currently paused
        - ``queue_depth``: number of tasks waiting in the pending queue
        - ``agent_count``: total number of registered agents
        - ``dlq_depth``: number of tasks in the dead-letter queue
        """
        return {
            "paused": orchestrator.is_paused,
            "queue_depth": len(orchestrator.list_tasks()),
            "agent_count": len(orchestrator.list_agents()),
            "dlq_depth": len(orchestrator.list_dlq()),
        }

    @app.get(
        "/rate-limit",
        summary="Get rate limiter status",
        dependencies=[Depends(auth)],
    )
    async def get_rate_limit() -> dict:
        """Return the current rate limiter configuration and token availability.

        Fields:
        - ``enabled``: True when rate limiting is active.
        - ``rate``: refill rate in tokens per second.
        - ``burst``: bucket capacity (maximum burst size).
        - ``available_tokens``: tokens currently available (live snapshot).
        """
        return orchestrator.get_rate_limiter_status()

    @app.put(
        "/rate-limit",
        summary="Reconfigure rate limiter",
        dependencies=[Depends(auth)],
    )
    async def put_rate_limit(body: RateLimitUpdate) -> dict:
        """Create or update the token-bucket rate limiter.

        Set ``rate=0`` to disable rate limiting (unlimited throughput).
        ``burst`` is ignored when ``rate=0``.

        Returns the updated rate limiter status.
        """
        return orchestrator.reconfigure_rate_limiter(rate=body.rate, burst=body.burst)

    @app.get(
        "/orchestrator/autoscaler",
        summary="Get autoscaler status",
        dependencies=[Depends(auth)],
    )
    async def get_autoscaler_status() -> dict:
        """Return the current autoscaler state.

        Returns ``{"enabled": false, ...}`` when autoscaling is not configured
        (``autoscale_max=0`` in config).
        """
        return await orchestrator.get_autoscaler_status()

    @app.put(
        "/orchestrator/autoscaler",
        summary="Reconfigure autoscaler parameters",
        dependencies=[Depends(auth)],
    )
    async def put_autoscaler(body: AutoScalerUpdate) -> dict:
        """Update autoscaling parameters at runtime.

        Only supplied fields are changed; omit a field to leave it unchanged.
        Returns 409 when autoscaling is not enabled (``autoscale_max=0``).
        """
        try:
            result = orchestrator.reconfigure_autoscaler(
                min=body.min,
                max=body.max,
                threshold=body.threshold,
                cooldown=body.cooldown,
            )
        except ValueError:
            raise HTTPException(
                status_code=409,
                detail="Autoscaling is not enabled (autoscale_max=0 in config)",
            )
        return result

    @app.post("/agents/{agent_id}/message", summary="Send a message to an agent", dependencies=[Depends(auth)])
    async def send_message(agent_id: str, body: SendMessage) -> dict:
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        try:
            msg_type = MessageType[body.type]
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Unknown message type: {body.type!r}")
        msg = Message(
            type=msg_type,
            from_id="__user__",
            to_id=agent_id,
            payload=body.payload,
        )
        await orchestrator.bus.publish(msg)
        return {"message_id": msg.id, "to_id": agent_id}

    @app.post("/agents/new", summary="Create a new agent dynamically (no template required)", dependencies=[Depends(auth)])
    async def create_dynamic_agent(body: DynamicAgentCreate) -> dict:
        """Create and start a new ClaudeCodeAgent with the given parameters.

        Unlike ``POST /agents`` (which requires a pre-configured template_id),
        this endpoint accepts the full agent specification inline so a Director
        agent can spawn specialist workers at runtime.

        Returns the assigned agent ID and a ``"created"`` status.  Returns 409
        if an agent with the requested *agent_id* already exists.
        """
        try:
            agent = await orchestrator.create_agent(
                agent_id=body.agent_id,
                tags=body.tags or [],
                system_prompt=body.system_prompt,
                isolate=body.isolate,
                merge_on_stop=body.merge_on_stop,
                merge_target=body.merge_target,
                command=body.command,
                role=body.role,
                task_timeout=body.task_timeout,
                parent_id=body.parent_id,
            )
            return {"status": "created", "agent_id": agent.id}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/agents", summary="Spawn a sub-agent under a parent agent", dependencies=[Depends(auth)])
    async def spawn_agent(body: SpawnAgent) -> dict:
        parent = orchestrator.get_agent(body.parent_id)
        if parent is None:
            raise HTTPException(
                status_code=404, detail=f"Agent {body.parent_id!r} not found"
            )
        msg = Message(
            type=MessageType.CONTROL,
            from_id=body.parent_id,
            to_id="__orchestrator__",
            payload={
                "action": "spawn_subagent",
                "template_id": body.template_id,
            },
        )
        await orchestrator.bus.publish(msg)
        return {"status": "spawning", "parent_id": body.parent_id, "template_id": body.template_id}

    @app.post("/director/chat", summary="Send a message to the Director agent", dependencies=[Depends(auth)])
    async def director_chat(body: DirectorChat, wait: bool = False) -> dict:
        director = orchestrator.get_director()
        if director is None:
            raise HTTPException(status_code=404, detail="No director agent in this session")

        task_id = str(uuid.uuid4())

        # Prepend any buffered worker results so the Director sees them as context
        pending = orchestrator.flush_director_pending()
        if pending:
            notifications = "\n".join(f"  - {p}" for p in pending)
            prompt = f"[Completed worker tasks since last message]\n{notifications}\n\n{body.message}"
        else:
            prompt = body.message

        task = Task(id=task_id, prompt=prompt, priority=0)

        if wait:
            sub_id = f"__chat_{task_id[:8]}__"
            q = await orchestrator.bus.subscribe(sub_id, broadcast=True)
            await director.send_task(task)
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=300.0)
                    except asyncio.TimeoutError:
                        raise HTTPException(status_code=504, detail="Director response timed out")
                    q.task_done()
                    if msg.type == MessageType.RESULT and msg.payload.get("task_id") == task_id:
                        return {"task_id": task_id, "response": msg.payload.get("output", "")}
            finally:
                await orchestrator.bus.unsubscribe(sub_id)
        else:
            await director.send_task(task)
            return {"task_id": task_id}

    # ------------------------------------------------------------------
    # Shared scratchpad — key/value store for inter-agent data sharing
    # ------------------------------------------------------------------

    @app.get("/scratchpad/", summary="List all scratchpad entries", dependencies=[Depends(auth)])
    async def scratchpad_list() -> dict:
        """Return all scratchpad key-value pairs.

        The shared scratchpad implements the Blackboard architectural pattern
        (Buschmann et al., 1996): a shared working memory that multiple agents
        can read and write independently.  It is especially useful for pipeline
        workflows where one agent writes results that a downstream agent reads.

        Reference: DESIGN.md §11 (architecture) — shared scratchpad (v0.16.0)
        """
        return dict(_scratchpad)

    @app.put(
        "/scratchpad/{key}",
        summary="Write a value to the scratchpad",
        dependencies=[Depends(auth)],
    )
    async def scratchpad_put(key: str, body: ScratchpadWrite) -> dict:
        """Write *value* under *key*.  Creates or overwrites the entry."""
        _scratchpad[key] = body.value
        return {"key": key, "updated": True}

    @app.get(
        "/scratchpad/{key}",
        summary="Read a value from the scratchpad",
        dependencies=[Depends(auth)],
    )
    async def scratchpad_get(key: str) -> dict:
        """Return the value stored under *key*, or 404 if not found."""
        if key not in _scratchpad:
            raise HTTPException(status_code=404, detail=f"Scratchpad key {key!r} not found")
        return {"key": key, "value": _scratchpad[key]}

    @app.delete(
        "/scratchpad/{key}",
        summary="Delete a scratchpad entry",
        dependencies=[Depends(auth)],
    )
    async def scratchpad_delete(key: str) -> dict:
        """Remove *key* from the scratchpad.  Returns 404 if not found."""
        if key not in _scratchpad:
            raise HTTPException(status_code=404, detail=f"Scratchpad key {key!r} not found")
        del _scratchpad[key]
        return {"key": key, "deleted": True}

    # ------------------------------------------------------------------
    # Health probes (no auth required for infrastructure compatibility)
    # ------------------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    async def liveness() -> dict:
        """Liveness probe: returns 200 if the event loop is responsive."""
        return {"status": "ok", "ts": time.time()}

    @app.get("/readyz", include_in_schema=False)
    async def readiness():
        """Readiness probe: 200 when the system can accept and dispatch tasks."""
        checks: dict = {}
        ready = True

        # Dispatch loop running?
        dispatch_alive = (
            orchestrator._dispatch_task is not None
            and not orchestrator._dispatch_task.done()
        )
        checks["dispatch_loop"] = {"ready": dispatch_alive}
        if not dispatch_alive:
            ready = False

        # At least one non-error worker?
        agents = orchestrator.list_agents()
        workers = [a for a in agents if a.get("role", AgentRole.WORKER) == AgentRole.WORKER]
        error_workers = [a for a in workers if a["status"] == "ERROR"]
        agent_ready = len(workers) > 0 and len(error_workers) < len(workers)
        checks["agents"] = {
            "ready": agent_ready,
            "total": len(workers),
            "error": len(error_workers),
        }
        if not agent_ready:
            ready = False

        # Dispatch not paused?
        if orchestrator.is_paused:
            checks["dispatch_paused"] = {"ready": False}
            ready = False

        return JSONResponse(
            content={"ready": ready, "checks": checks},
            status_code=200 if ready else 503,
        )

    @app.get("/dlq", summary="Dead letter queue", dependencies=[Depends(auth)])
    async def dead_letter_queue() -> list:
        """Return tasks that could not be dispatched after exhausting retries."""
        return orchestrator.list_dlq()

    # ------------------------------------------------------------------
    # Security: Audit log endpoint
    # Reference: DESIGN.md §10.18 (v0.44.0)
    # ------------------------------------------------------------------

    @app.get("/audit-log", summary="Recent audit log entries", dependencies=[Depends(auth)])
    async def get_audit_log(limit: int = 100) -> list:
        """Return the most recent audit log entries (up to *limit*).

        Each entry records a single HTTP request: timestamp, method, path,
        client_ip, api_key_hint (first 8 chars only), status_code, duration_ms.

        Entries are stored in an in-process ring buffer of at most 1 000
        entries.  No sensitive data (full API keys, request bodies) is stored.

        Design reference:
        - Microsoft Multi-Agent Reference Architecture — Security (2025)
          https://microsoft.github.io/multi-agent-reference-architecture/docs/security/Security.html
        - DESIGN.md §10.18 (v0.44.0)
        """
        from tmux_orchestrator.security import AuditLogMiddleware
        entries = AuditLogMiddleware.get_log()
        # Return the most recent *limit* entries (newest last)
        return [e.to_dict() for e in entries[-limit:]]

    # ------------------------------------------------------------------
    # Checkpoint status (DESIGN.md §10.12 v0.45.0)
    # ------------------------------------------------------------------

    @app.get("/checkpoint/status", summary="Checkpoint store status", dependencies=[Depends(auth)])
    async def get_checkpoint_status() -> dict:
        """Return the current state of the checkpoint store.

        When ``checkpoint_enabled: true`` is set in the YAML config, this
        endpoint reports how many tasks and workflows are currently persisted
        in the SQLite checkpoint database.  This can be used to verify that
        checkpoints are being written and to diagnose resume issues.

        Returns ``{"enabled": false}`` when checkpointing is disabled.

        Reference: LangGraph checkpointer pattern (LangChain 2025);
                   DESIGN.md §10.12 (v0.45.0).
        """
        store = orchestrator.get_checkpoint_store()
        if store is None:
            return {"enabled": False}
        pending_tasks = store.load_pending_tasks()
        waiting_tasks = store.load_waiting_tasks()
        workflows = store.load_workflows()
        session_name = store.load_meta("session_name")
        return {
            "enabled": True,
            "pending_tasks": len(pending_tasks),
            "waiting_tasks": len(waiting_tasks),
            "workflows": len(workflows),
            "session_name": session_name,
            "pending_task_ids": [t.id for t in pending_tasks],
            "workflow_ids": list(workflows.keys()),
        }

    @app.get("/telemetry/status", summary="OpenTelemetry status", dependencies=[Depends(auth)])
    async def get_telemetry_status() -> dict:
        """Return the current telemetry configuration.

        When ``telemetry_enabled: true`` is set in the YAML config, this endpoint
        reports whether an OTLP exporter is configured or whether the fallback
        ConsoleSpanExporter is active.

        Returns ``{"enabled": false}`` when telemetry is disabled.

        Reference: OTel GenAI Semantic Conventions
                   https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/
                   DESIGN.md §10.14 (v0.47.0).
        """
        telemetry = orchestrator.get_telemetry()
        if telemetry is None:
            return {"enabled": False}
        otlp_endpoint = orchestrator.config.otlp_endpoint
        return {
            "enabled": True,
            "otlp_endpoint": otlp_endpoint or None,
            "exporter": "otlp" if otlp_endpoint else "console",
        }

    @app.post("/checkpoint/clear", summary="Clear all checkpoint data", dependencies=[Depends(auth)])
    async def clear_checkpoint() -> dict:
        """Wipe all checkpoint data (tasks, workflows, meta).

        Use this to reset the checkpoint state when starting fresh after a
        resume, or to discard stale checkpoints from a previous session.

        Warning: this is irreversible.  All pending/waiting task snapshots
        and workflow state will be deleted from the SQLite database.

        Reference: DESIGN.md §10.12 (v0.45.0).
        """
        store = orchestrator.get_checkpoint_store()
        if store is None:
            raise HTTPException(status_code=400, detail="Checkpointing is not enabled")
        store.clear_all()
        return {"cleared": True}

    # ------------------------------------------------------------------
    # Prometheus metrics (no auth — Prometheus scraper compatibility)
    # ------------------------------------------------------------------

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics():
        """Expose Prometheus-format metrics for the orchestrator.

        No authentication required so that Prometheus (or OpenTelemetry
        collectors) can scrape without managing credentials.  Expose this
        port only on a trusted network or bind it to localhost.

        Metrics exposed:
        - ``tmux_agent_status_total{status}`` — gauge: agent count per status
        - ``tmux_task_queue_size`` — gauge: current task queue depth
        - ``tmux_bus_drop_total{agent_id}`` — gauge: per-agent bus drop count

        Reference: prometheus_client Python library;
                   DESIGN.md §10.6 (Prometheus metrics, low priority);
                   OneUptime blog (2025-01-06) — python-custom-metrics-prometheus.
        """
        from prometheus_client import (  # noqa: PLC0415
            CONTENT_TYPE_LATEST,
            CollectorRegistry,
            Gauge,
            generate_latest,
        )
        from fastapi.responses import Response  # noqa: PLC0415

        registry = CollectorRegistry()

        # --- Agent status distribution ---
        agent_status_gauge = Gauge(
            "tmux_agent_status_total",
            "Number of agents per status",
            ["status"],
            registry=registry,
        )
        agents = orchestrator.list_agents()
        status_counts: dict[str, int] = {}
        for a in agents:
            s = a.get("status", "UNKNOWN")
            status_counts[s] = status_counts.get(s, 0) + 1
        for status_val, count in status_counts.items():
            agent_status_gauge.labels(status=status_val).set(count)

        # --- Task queue depth ---
        task_queue_gauge = Gauge(
            "tmux_task_queue_size",
            "Current number of tasks waiting in the queue",
            registry=registry,
        )
        task_queue_gauge.set(len(orchestrator.list_tasks()))

        # --- Bus drop counts ---
        bus_drop_gauge = Gauge(
            "tmux_bus_drop_total",
            "Total dropped bus messages per agent",
            ["agent_id"],
            registry=registry,
        )
        for a in agents:
            drops = a.get("bus_drops", 0)
            if drops:
                bus_drop_gauge.labels(agent_id=a["id"]).set(drops)

        output = generate_latest(registry)
        return Response(content=output, media_type=CONTENT_TYPE_LATEST)

    # ------------------------------------------------------------------
    # Webhook endpoints — outbound event notifications (v0.30.0)
    # ------------------------------------------------------------------

    @app.post(
        "/webhooks",
        summary="Register a new webhook",
        dependencies=[Depends(auth)],
    )
    async def create_webhook(body: WebhookCreate) -> dict:
        """Register a new outbound webhook.

        When a subscribed event fires, the orchestrator POSTs a JSON payload to
        the registered URL.  An optional HMAC-SHA256 signature is included in
        the ``X-Signature-SHA256`` header when ``secret`` is supplied.

        Valid event names:
        ``task_complete``, ``task_failed``, ``task_retrying``, ``task_cancelled``,
        ``task_dependency_failed``, ``task_waiting``, ``agent_status``,
        ``workflow_complete``, ``workflow_failed``, ``workflow_cancelled``, ``*``
        (wildcard — receive all events).

        Returns: ``{id, url, events, created_at}``

        Design reference: GitHub Webhooks; Stripe Webhooks; RFC 2104 HMAC;
        Zalando RESTful API Guidelines §webhook; DESIGN.md §10.25 (v0.30.0).
        """
        from tmux_orchestrator.webhook_manager import KNOWN_EVENTS  # noqa: PLC0415

        invalid = [e for e in body.events if e not in KNOWN_EVENTS]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown event name(s): {invalid!r}. "
                       f"Valid events: {sorted(KNOWN_EVENTS)!r}",
            )
        wm: "WebhookManager" = orchestrator._webhook_manager
        wh = wm.register(url=body.url, events=body.events, secret=body.secret)
        return {
            "id": wh.id,
            "url": wh.url,
            "events": wh.events,
            "created_at": wh.created_at,
        }

    @app.get(
        "/webhooks",
        summary="List all registered webhooks",
        dependencies=[Depends(auth)],
    )
    async def list_webhooks() -> list:
        """Return all registered webhooks with delivery statistics.

        Each entry contains:
        - ``id``: webhook UUID
        - ``url``: target URL
        - ``events``: subscribed event names
        - ``created_at``: Unix timestamp of registration
        - ``delivery_count``: total delivery attempts
        - ``failure_count``: total failed attempts

        Design reference: DESIGN.md §10.25 (v0.30.0).
        """
        wm: "WebhookManager" = orchestrator._webhook_manager
        return [wh.to_dict() for wh in wm.list_all()]

    @app.delete(
        "/webhooks/{webhook_id}",
        summary="Delete a webhook",
        dependencies=[Depends(auth)],
    )
    async def delete_webhook(webhook_id: str) -> dict:
        """Remove a registered webhook by ID.

        Returns 404 if the webhook ID is unknown.

        Design reference: DESIGN.md §10.25 (v0.30.0).
        """
        wm: "WebhookManager" = orchestrator._webhook_manager
        removed = wm.unregister(webhook_id)
        if not removed:
            raise HTTPException(
                status_code=404,
                detail=f"Webhook {webhook_id!r} not found",
            )
        return {"deleted": True, "id": webhook_id}

    @app.get(
        "/webhooks/{webhook_id}/deliveries",
        summary="Get recent delivery attempts for a webhook",
        dependencies=[Depends(auth)],
    )
    async def get_webhook_deliveries(webhook_id: str) -> list:
        """Return the last 20 delivery attempts for *webhook_id*.

        Each entry contains:
        - ``id``: delivery attempt UUID
        - ``webhook_id``: the webhook this delivery belongs to
        - ``event``: the event name that triggered the delivery
        - ``timestamp``: Unix timestamp of the attempt
        - ``success``: whether the delivery succeeded (HTTP 2xx)
        - ``status_code``: HTTP response status code, or null on connection error
        - ``error``: error message string, or null on success
        - ``duration_ms``: request duration in milliseconds

        Returns 404 if the webhook ID is unknown.

        Design reference: DESIGN.md §10.25 (v0.30.0).
        """
        from dataclasses import asdict  # noqa: PLC0415

        wm: "WebhookManager" = orchestrator._webhook_manager
        webhook = wm.get(webhook_id)
        if webhook is None:
            raise HTTPException(
                status_code=404,
                detail=f"Webhook {webhook_id!r} not found",
            )
        deliveries = wm.last_deliveries(webhook_id, n=20)
        return [asdict(d) for d in deliveries]

    # ------------------------------------------------------------------
    # Agent group endpoints — named pools for targeted task dispatch (v0.31.0)
    # ------------------------------------------------------------------

    @app.post(
        "/groups",
        summary="Create a named agent group",
        dependencies=[Depends(auth)],
    )
    async def create_group(body: GroupCreate) -> dict:
        """Create a new named agent group (logical pool).

        Tasks may target this group via ``target_group`` in POST /tasks,
        POST /tasks/batch, or POST /workflows.

        Returns 409 Conflict if a group with the same name already exists.

        Design references:
        - Kubernetes Node Pools / Node Groups — logical grouping of cluster nodes.
        - AWS Auto Scaling Groups — named pools of homogeneous EC2 instances.
        - Apache Mesos Roles — cluster resource partitioning by name.
        - HashiCorp Nomad Task Groups — co-located task scheduling units.
        - DESIGN.md §10.26 (v0.31.0)
        """
        gm = orchestrator.get_group_manager()
        created = gm.create(body.name, body.agent_ids)
        if not created:
            raise HTTPException(
                status_code=409,
                detail=f"Group {body.name!r} already exists",
            )
        return {"name": body.name, "agent_ids": body.agent_ids}

    @app.get(
        "/groups",
        summary="List all agent groups",
        dependencies=[Depends(auth)],
    )
    async def list_groups() -> list:
        """Return all named agent groups with member agent IDs and their statuses.

        Each entry contains:
        - ``name``: group name
        - ``agent_ids``: sorted list of member agent IDs
        - ``agents``: list of ``{id, status}`` dicts for each member

        Design reference: DESIGN.md §10.26 (v0.31.0).
        """
        gm = orchestrator.get_group_manager()
        all_agents = {a["id"]: a for a in orchestrator.list_agents()}
        result = []
        for entry in gm.list_all():
            agents_detail = [
                {"id": aid, "status": all_agents[aid]["status"]}
                if aid in all_agents
                else {"id": aid, "status": "unknown"}
                for aid in entry["agent_ids"]
            ]
            result.append({
                "name": entry["name"],
                "agent_ids": entry["agent_ids"],
                "agents": agents_detail,
            })
        return result

    @app.get(
        "/groups/{group_name}",
        summary="Get a specific agent group",
        dependencies=[Depends(auth)],
    )
    async def get_group(group_name: str) -> dict:
        """Return details for *group_name*: member agent IDs and their statuses.

        Returns 404 if the group is unknown.

        Design reference: DESIGN.md §10.26 (v0.31.0).
        """
        gm = orchestrator.get_group_manager()
        members = gm.get(group_name)
        if members is None:
            raise HTTPException(
                status_code=404,
                detail=f"Group {group_name!r} not found",
            )
        all_agents = {a["id"]: a for a in orchestrator.list_agents()}
        agents_detail = [
            {"id": aid, "status": all_agents[aid]["status"]}
            if aid in all_agents
            else {"id": aid, "status": "unknown"}
            for aid in sorted(members)
        ]
        return {
            "name": group_name,
            "agent_ids": sorted(members),
            "agents": agents_detail,
        }

    @app.delete(
        "/groups/{group_name}",
        summary="Delete an agent group",
        dependencies=[Depends(auth)],
    )
    async def delete_group(group_name: str) -> dict:
        """Remove a named agent group.

        Returns 404 if the group is unknown.  Does not affect the agents
        themselves — only the group registration is removed.

        Design reference: DESIGN.md §10.26 (v0.31.0).
        """
        gm = orchestrator.get_group_manager()
        deleted = gm.delete(group_name)
        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"Group {group_name!r} not found",
            )
        return {"deleted": True, "name": group_name}

    @app.post(
        "/groups/{group_name}/agents",
        summary="Add an agent to a group",
        dependencies=[Depends(auth)],
    )
    async def add_agent_to_group(group_name: str, body: GroupAddAgent) -> dict:
        """Add *agent_id* to the named group.

        Returns 404 if the group does not exist.  Adding an agent that is
        already a member is idempotent (no error).

        Design reference: DESIGN.md §10.26 (v0.31.0).
        """
        gm = orchestrator.get_group_manager()
        added = gm.add_agent(group_name, body.agent_id)
        if not added:
            raise HTTPException(
                status_code=404,
                detail=f"Group {group_name!r} not found",
            )
        return {"name": group_name, "agent_id": body.agent_id, "added": True}

    @app.delete(
        "/groups/{group_name}/agents/{agent_id}",
        summary="Remove an agent from a group",
        dependencies=[Depends(auth)],
    )
    async def remove_agent_from_group(group_name: str, agent_id: str) -> dict:
        """Remove *agent_id* from the named group.

        Returns 404 if the group does not exist or the agent is not a member.

        Design reference: DESIGN.md §10.26 (v0.31.0).
        """
        gm = orchestrator.get_group_manager()
        removed = gm.remove_agent(group_name, agent_id)
        if not removed:
            # Distinguish between group-not-found and agent-not-member
            if gm.get(group_name) is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Group {group_name!r} not found",
                )
            raise HTTPException(
                status_code=404,
                detail=f"Agent {agent_id!r} is not a member of group {group_name!r}",
            )
        return {"name": group_name, "agent_id": agent_id, "removed": True}

    # ------------------------------------------------------------------
    # Episodic memory — MIRIX-inspired per-agent episode log (v1.0.28)
    # DESIGN.md §10.28; arXiv:2507.07957 (Wang & Chen, 2025)
    # ------------------------------------------------------------------

    _orch_config = getattr(orchestrator, "config", None)
    _episode_store = EpisodeStore(
        root_dir=getattr(_orch_config, "mailbox_dir", "~/.tmux_orchestrator"),
        session_name=getattr(_orch_config, "session_name", "orchestrator"),
    )
    # Share the episode store with the orchestrator dispatch loop so that
    # episode auto-inject works without a second store instance.
    # Reference: DESIGN.md §10.29 (v1.0.29)
    orchestrator._episode_store = _episode_store  # type: ignore[attr-defined]

    @app.get(
        "/agents/{agent_id}/memory",
        summary="List episodic memory entries for an agent",
        dependencies=[Depends(auth)],
    )
    async def list_episodes(agent_id: str, limit: int = 20) -> list[Episode]:
        """Return the most recent *limit* episodic memory entries for *agent_id*.

        Episodic memory records past task completions with a human-readable
        summary, outcome classification (success/failure/partial), and lessons
        learned.  Entries are returned newest-first.

        Design reference:
        - Wang & Chen, "MIRIX: Multi-Agent Memory System for LLM-Based Agents",
          arXiv:2507.07957, 2025 — episodic memory achieves 35% higher accuracy
          than RAG baseline while reducing storage by 99.9%.
        - "Position: Episodic Memory is the Missing Piece for Long-Term LLM
          Agents", arXiv:2502.06975, 2025.

        The 404 is only raised when the agent is completely unknown to the
        registry.  A known agent with *no* episodes returns an empty list.
        Pass ``?limit=N`` to control how many entries are returned (default 20).
        """
        if orchestrator.get_agent(agent_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f"Agent {agent_id!r} not found",
            )
        records = _episode_store.list(agent_id, limit=min(limit, 200))
        return [Episode(**r) for r in records]

    @app.post(
        "/agents/{agent_id}/memory",
        summary="Add an episodic memory entry for an agent",
        status_code=201,
        dependencies=[Depends(auth)],
    )
    async def add_episode(agent_id: str, body: EpisodeCreate) -> Episode:
        """Append a new episodic memory entry for *agent_id*.

        Agents call this endpoint after completing a task to record what they
        accomplished, whether it succeeded, and any lessons learned.

        The ``task_id`` field is optional but recommended for correlation
        with task history (``GET /agents/{id}/history``).

        Design reference: MIRIX §4.2 — "each completed task episode is
        appended to the agent's episodic log; the log is never overwritten,
        preserving full recall history."  Contrasts with NOTES.md (single
        summary file that is overwritten on ``/summarize``).
        """
        if orchestrator.get_agent(agent_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f"Agent {agent_id!r} not found",
            )
        record = _episode_store.append(
            agent_id,
            summary=body.summary,
            outcome=body.outcome,
            lessons=body.lessons,
            task_id=body.task_id,
        )
        return Episode(**record)

    @app.delete(
        "/agents/{agent_id}/memory/{episode_id}",
        summary="Delete a specific episodic memory entry",
        dependencies=[Depends(auth)],
    )
    async def delete_episode(agent_id: str, episode_id: str) -> dict:
        """Delete the episode identified by *episode_id* from *agent_id*'s log.

        Rewrites the JSONL file atomically — all other episodes are preserved.
        Returns 404 when the agent or episode is not found.

        Use case: agents can prune obsolete or incorrect episodes to keep the
        memory store relevant.  Unlike NOTES.md, individual episodes can be
        removed without losing the entire history.
        """
        if orchestrator.get_agent(agent_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f"Agent {agent_id!r} not found",
            )
        try:
            _episode_store.delete(agent_id, episode_id)
        except EpisodeNotFoundError:
            raise HTTPException(
                status_code=404,
                detail=f"Episode {episode_id!r} not found for agent {agent_id!r}",
            )
        return {"agent_id": agent_id, "episode_id": episode_id, "deleted": True}

    # ------------------------------------------------------------------
    # WebSocket — session cookie OR API key query param
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket, key: str = "") -> None:
        session_ok = _valid_session(websocket.cookies.get("session"))
        key_ok = bool(api_key) and key == api_key
        if not session_ok and not key_ok:
            await websocket.close(code=1008)  # Policy Violation
            return
        await hub.handle(websocket)

    # ------------------------------------------------------------------
    # SSE push endpoint — real-time bus event stream
    # ------------------------------------------------------------------

    @app.get(
        "/events",
        summary="Real-time bus event stream (Server-Sent Events)",
        response_class=EventSourceResponse,
        dependencies=[Depends(auth)],
    )
    async def sse_events(request: Request):  # type: ignore[return]
        """Stream all bus events to the client as Server-Sent Events.

        Each event is a JSON object with ``type``, ``from_id``, ``to_id``,
        and ``payload`` fields.  The client can listen with the browser's
        native ``EventSource`` API.

        Authentication: session cookie OR ``X-API-Key`` header / ``?key=`` query parameter.

        Reference:
        - FastAPI SSE (v0.135+): https://fastapi.tiangolo.com/tutorial/server-sent-events/
        - DESIGN.md §10.8 — SSE push notifications (v0.12.0, 2026-03-05)
        """
        sub_id = f"__sse_{id(request)}__"
        q = await orchestrator.bus.subscribe(sub_id, broadcast=True)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Keep-alive comment every 15s to prevent proxy disconnections
                    yield ServerSentEvent(comment="keep-alive")
                    continue
                except asyncio.CancelledError:
                    break
                try:
                    q.task_done()
                    yield ServerSentEvent(
                        data={
                            "type": msg.type.value,
                            "from_id": msg.from_id,
                            "to_id": msg.to_id,
                            "payload": msg.payload,
                        },
                        event=msg.type.value.lower(),
                        id=msg.id,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("SSE: error serialising message %s", msg.id)
        finally:
            await orchestrator.bus.unsubscribe(sub_id)
            logger.debug("SSE: client disconnected, unsubscribed %s", sub_id)

    # ------------------------------------------------------------------
    # Browser UI — unconditional; JS handles auth gate
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def web_ui() -> HTMLResponse:
        return HTMLResponse(_HTML_UI)

    return app


# ---------------------------------------------------------------------------
# Embedded single-page browser UI
# ---------------------------------------------------------------------------

_HTML_UI = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>TmuxAgentOrchestrator</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0d1117;
    color: #c9d1d9;
    height: 100vh;
    display: flex;
    flex-direction: column;
  }
  header {
    background: #161b22;
    border-bottom: 1px solid #30363d;
    padding: 12px 20px;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
  }
  header h1 { font-size: 1.1rem; color: #58a6ff; font-weight: 600; }
  #status-dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: #3fb950; transition: background 0.3s;
  }
  #status-dot.disconnected { background: #f85149; }
  main {
    flex: 1;
    display: grid;
    grid-template-columns: 1fr 1fr;
    grid-template-rows: 1fr 1.6fr;
    gap: 1px;
    background: #30363d;
    overflow: hidden;
    min-height: 0;
  }
  section {
    background: #0d1117;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    min-height: 0;
  }
  .full-width { grid-column: 1 / -1; }
  .section-header {
    background: #161b22;
    padding: 8px 14px;
    font-size: 0.8rem;
    font-weight: 600;
    color: #8b949e;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    border-bottom: 1px solid #30363d;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-shrink: 0;
  }
  .badge {
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 1px 7px;
    font-size: 0.7rem;
    color: #8b949e;
  }
  .badge.director { background: #1f3447; border-color: #58a6ff; color: #58a6ff; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  thead th {
    background: #161b22;
    padding: 6px 12px;
    text-align: left;
    font-weight: 500;
    color: #8b949e;
    font-size: 0.75rem;
    position: sticky;
    top: 0;
  }
  tbody tr:hover { background: #161b22; }
  tbody td { padding: 6px 12px; border-bottom: 1px solid #21262d; }
  .tbl-wrap { overflow-y: auto; flex: 1; min-height: 0; }
  .status-idle    { color: #3fb950; }
  .status-busy    { color: #e3b341; }
  .status-error   { color: #f85149; }
  .status-stopped { color: #6e7681; }
  .role-director  { color: #58a6ff; font-size: 0.7rem; margin-left: 4px; }

  /* ── View toggle (table / tree) ── */
  .view-toggle {
    display: flex;
    gap: 4px;
  }
  .view-btn {
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 4px;
    padding: 2px 10px;
    font-size: 0.72rem;
    color: #8b949e;
    cursor: pointer;
  }
  .view-btn.active {
    background: #1f6feb;
    border-color: #388bfd;
    color: #fff;
  }

  /* ── Agent tree view ── */
  #agents-tree {
    overflow-y: auto;
    flex: 1;
    padding: 8px 14px;
    min-height: 0;
    display: none; /* hidden by default; shown when tree view is active */
  }
  .tree-node {
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .tree-node li {
    position: relative;
    padding-left: 18px;
    margin: 2px 0;
  }
  .tree-node li::before {
    content: '';
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    border-left: 1px solid #30363d;
  }
  .tree-node li:last-child::before {
    height: 0.8em;
  }
  .tree-node li::after {
    content: '';
    position: absolute;
    left: 0;
    top: 0.8em;
    width: 14px;
    border-top: 1px solid #30363d;
  }
  .tree-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 3px 6px;
    border-radius: 4px;
    font-size: 0.82rem;
    cursor: default;
  }
  .tree-item:hover { background: #161b22; }
  .tree-item-id { font-weight: 600; font-family: monospace; }
  .tree-item-role { font-size: 0.7rem; color: #8b949e; padding: 1px 5px; border-radius: 3px; background: #21262d; }
  .tree-item-role.director { color: #58a6ff; background: #1f3447; }

  /* ── Director Chat ── */
  #chat-section { display: none; }
  #chat-history {
    flex: 1;
    overflow-y: auto;
    padding: 10px 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-height: 0;
  }
  .chat-bubble {
    max-width: 80%;
    padding: 8px 12px;
    border-radius: 10px;
    font-size: 0.85rem;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .bubble-user {
    align-self: flex-end;
    background: #1f6feb;
    color: #fff;
    border-bottom-right-radius: 2px;
  }
  .bubble-director {
    align-self: flex-start;
    background: #21262d;
    border: 1px solid #30363d;
    color: #c9d1d9;
    border-bottom-left-radius: 2px;
  }
  .bubble-thinking {
    align-self: flex-start;
    background: #161b22;
    border: 1px dashed #30363d;
    color: #6e7681;
    font-style: italic;
    font-size: 0.8rem;
  }
  #chat-input-row {
    display: flex;
    gap: 8px;
    padding: 8px 10px;
    border-top: 1px solid #30363d;
    flex-shrink: 0;
  }
  #chat-input {
    flex: 1;
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 7px 11px;
    color: #c9d1d9;
    font-size: 0.9rem;
    outline: none;
  }
  #chat-input:focus { border-color: #58a6ff; }
  #chat-send-btn {
    background: #1f6feb;
    color: #fff;
    border: none;
    border-radius: 6px;
    padding: 7px 16px;
    cursor: pointer;
    font-size: 0.85rem;
    font-weight: 500;
  }
  #chat-send-btn:hover { background: #388bfd; }
  #chat-send-btn:disabled { background: #30363d; cursor: default; color: #6e7681; }

  /* ── Event Log ── */
  #log-list {
    overflow-y: auto;
    flex: 1;
    padding: 8px 14px;
    font-size: 0.8rem;
    font-family: 'Consolas', monospace;
    min-height: 0;
  }
  .log-entry {
    display: flex;
    gap: 10px;
    padding: 2px 0;
    border-bottom: 1px solid #21262d11;
  }
  .log-ts   { color: #6e7681; flex-shrink: 0; }
  .log-type { font-weight: 600; flex-shrink: 0; min-width: 60px; }
  .type-RESULT   { color: #3fb950; }
  .type-STATUS   { color: #58a6ff; }
  .type-PEER_MSG { color: #bc8cff; }
  .type-TASK     { color: #e3b341; }
  .type-CONTROL  { color: #f0883e; }

  /* ── Footer ── */
  footer {
    background: #161b22;
    border-top: 1px solid #30363d;
    padding: 10px 20px;
    display: flex;
    gap: 8px;
    align-items: center;
    flex-shrink: 0;
  }
  #task-input {
    flex: 1;
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 8px 12px;
    color: #c9d1d9;
    font-size: 0.9rem;
    outline: none;
  }
  #task-input:focus { border-color: #58a6ff; }
  button {
    background: #238636;
    color: #fff;
    border: none;
    border-radius: 6px;
    padding: 8px 16px;
    cursor: pointer;
    font-size: 0.85rem;
    font-weight: 500;
    transition: background 0.2s;
  }
  button:hover { background: #2ea043; }
  #priority-input {
    width: 70px;
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 8px 10px;
    color: #c9d1d9;
    font-size: 0.9rem;
    outline: none;
  }
  .empty-hint { color: #6e7681; font-size: 0.8rem; padding: 12px; text-align: center; }

  /* ── Agent Conversations ── */
  #conv-list {
    overflow-y: auto;
    flex: 1;
    padding: 6px 14px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-height: 0;
  }
  .conv-entry {
    display: flex;
    gap: 8px;
    align-items: baseline;
    font-size: 0.82rem;
    padding: 4px 0;
    border-bottom: 1px solid #21262d33;
    flex-wrap: wrap;
  }
  .conv-ts   { color: #6e7681; flex-shrink: 0; font-size: 0.72rem; font-family: monospace; }
  .conv-from { font-weight: 700; flex-shrink: 0; }
  .conv-arrow{ color: #6e7681; flex-shrink: 0; }
  .conv-to   { font-weight: 700; flex-shrink: 0; }
  .conv-sep  { color: #6e7681; flex-shrink: 0; }
  .conv-content { color: #c9d1d9; word-break: break-word; flex: 1; min-width: 0; }

  /* ── Auth Overlay ── */
  #auth-overlay {
    position: fixed;
    inset: 0;
    background: rgba(13, 17, 23, 0.97);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }
  #auth-box {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 12px;
    padding: 40px 48px;
    text-align: center;
    max-width: 360px;
    width: 100%;
  }
  #auth-box h2 { color: #58a6ff; margin-bottom: 12px; font-size: 1.3rem; }
  #auth-box > p { color: #8b949e; font-size: 0.9rem; margin-bottom: 24px; }
  #auth-error { color: #f85149; font-size: 0.85rem; margin-top: 12px; min-height: 1.2em; }
  .auth-btn { display: block; width: 100%; margin-bottom: 12px; padding: 10px 20px; font-size: 0.95rem; }
</style>
</head>
<body>

<!-- Auth overlay (shown when unauthenticated) -->
<div id="auth-overlay" style="display:none">
  <div id="auth-box">
    <h2>TmuxAgentOrchestrator</h2>
    <p id="auth-msg">Authenticating…</p>
    <button id="btn-register" class="auth-btn" onclick="registerPasskey()" style="display:none">Register Passkey</button>
    <button id="btn-authenticate" class="auth-btn" onclick="authenticatePasskey()" style="display:none">Sign in with Passkey</button>
    <p id="auth-error"></p>
  </div>
</div>

<header>
  <div id="status-dot" class="disconnected"></div>
  <h1>TmuxAgentOrchestrator</h1>
  <span id="conn-label" style="font-size:0.8rem;color:#8b949e">Connecting…</span>
  <button id="btn-signout" onclick="signOut()" style="margin-left:auto;padding:4px 12px;font-size:0.8rem;background:#21262d;border:1px solid #30363d;color:#c9d1d9;display:none">Sign Out</button>
</header>

<main>
  <!-- Agents panel -->
  <section>
    <div class="section-header">
      Agents <span id="agent-count" class="badge">0</span>
      <div class="view-toggle">
        <button class="view-btn active" id="btn-table-view" onclick="setAgentView('table')">List</button>
        <button class="view-btn" id="btn-tree-view" onclick="setAgentView('tree')">Tree</button>
      </div>
    </div>
    <div class="tbl-wrap" id="agents-table-wrap">
      <table>
        <thead><tr><th>ID</th><th>Role</th><th>Status</th><th>Task</th></tr></thead>
        <tbody id="agents-body"><tr><td colspan="4" class="empty-hint">Loading…</td></tr></tbody>
      </table>
    </div>
    <div id="agents-tree"></div>
  </section>

  <!-- Task queue panel -->
  <section>
    <div class="section-header">
      Task Queue <span id="task-count" class="badge">0</span>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr><th>Pri</th><th>ID</th><th>Prompt</th></tr></thead>
        <tbody id="tasks-body"><tr><td colspan="3" class="empty-hint">Loading…</td></tr></tbody>
      </table>
    </div>
  </section>

  <!-- Director Chat panel (shown only when a director agent exists) -->
  <section id="chat-section" class="full-width">
    <div class="section-header">
      Director Chat
      <span id="director-id-badge" class="badge director">—</span>
    </div>
    <div id="chat-history"></div>
    <div id="chat-input-row">
      <input id="chat-input" type="text" placeholder="Message the Director… (Enter to send)" autocomplete="off" />
      <button id="chat-send-btn" onclick="sendChat()">Send</button>
    </div>
  </section>

  <!-- Agent Conversations panel -->
  <section>
    <div class="section-header">
      Agent Conversations
      <span id="conv-count" class="badge">0</span>
    </div>
    <div id="conv-list"><div class="empty-hint">No P2P messages yet</div></div>
  </section>

  <!-- Event Log panel -->
  <section>
    <div class="section-header">
      Event Log
      <button onclick="clearLog()" style="padding:2px 10px;font-size:0.75rem;background:#21262d;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;">Clear</button>
    </div>
    <div id="log-list"></div>
  </section>
</main>

<footer>
  <input id="task-input" type="text" placeholder="Submit worker task…" />
  <input id="priority-input" type="number" value="0" min="0" title="Priority" />
  <button onclick="submitTask()">Submit Task</button>
</footer>

<script>
const API_BASE = '';

// ── Base64url helpers ──
function b64urlToBuffer(s) {
  const b = s.replace(/-/g, '+').replace(/_/g, '/')
    .padEnd(s.length + (4 - s.length % 4) % 4, '=');
  return Uint8Array.from(atob(b), c => c.charCodeAt(0)).buffer;
}
function bufferToB64url(buf) {
  return btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}

// ── SSE push notifications ──
// Replaces 3-second polling for agent/task state changes.
// Reference: DESIGN.md §10.8; FastAPI SSE (v0.135+).
let _sseSource = null;

function connectSSE() {
  if (_sseSource && _sseSource.readyState !== EventSource.CLOSED) return;
  _sseSource = new EventSource('/events');
  _sseSource.onopen = () => {
    document.getElementById('status-dot').classList.remove('disconnected');
    document.getElementById('conn-label').textContent = 'Connected (SSE)';
  };
  _sseSource.onerror = () => {
    document.getElementById('status-dot').classList.add('disconnected');
    document.getElementById('conn-label').textContent = 'SSE reconnecting…';
    // EventSource auto-reconnects; we keep a fallback poll in case it stays broken
  };
  // On any STATUS or RESULT event, refresh agent/task tables immediately
  _sseSource.addEventListener('status', (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    if (typeof data === 'string') { try { data = JSON.parse(data); } catch { return; } }
    logEntry({type: 'STATUS', from_id: data.from_id, payload: data.payload, timestamp: new Date().toISOString()});
    refreshAgents();
    refreshTasks();
    refreshAgentTree();
  });
  _sseSource.addEventListener('result', (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    if (typeof data === 'string') { try { data = JSON.parse(data); } catch { return; } }
    logEntry({type: 'RESULT', from_id: data.from_id, payload: data.payload, timestamp: new Date().toISOString()});
    refreshAgents();
    // Director response via SSE
    if (pendingChats.has(data.payload?.task_id)) {
      const bubble = pendingChats.get(data.payload.task_id);
      pendingChats.delete(data.payload.task_id);
      const output = (data.payload && data.payload.output) || '';
      bubble.className = 'chat-bubble bubble-director';
      bubble.textContent = output;
      scrollChat();
      document.getElementById('chat-send-btn').disabled = false;
      document.getElementById('chat-input').disabled = false;
    }
  });
  _sseSource.addEventListener('peer_msg', (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    if (typeof data === 'string') { try { data = JSON.parse(data); } catch { return; } }
    if (data.payload && data.payload._forwarded) {
      addConversationEntry({type: 'PEER_MSG', from_id: data.from_id, to_id: data.to_id, payload: data.payload});
    }
  });
}

function disconnectSSE() {
  if (_sseSource) { _sseSource.close(); _sseSource = null; }
}

// ── Auth ──
let _pollInterval = null;

async function checkAuth() {
  const status = await fetch('/auth/status').then(r => r.json());
  const overlay = document.getElementById('auth-overlay');
  const btnReg = document.getElementById('btn-register');
  const btnAuth = document.getElementById('btn-authenticate');
  const btnSignout = document.getElementById('btn-signout');
  document.getElementById('auth-error').textContent = '';

  if (status.authenticated) {
    overlay.style.display = 'none';
    btnSignout.style.display = 'inline-block';
    if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
      connectWS();
    }
    // SSE replaces polling for real-time agent/task updates
    connectSSE();
    refreshAgents();
    refreshTasks();
    refreshAgentTree();
    // Keep a light 30s fallback poll in case SSE misses an event
    if (!_pollInterval) {
      _pollInterval = setInterval(() => { refreshAgents(); refreshTasks(); }, 30000);
    }
  } else {
    overlay.style.display = 'flex';
    btnSignout.style.display = 'none';
    disconnectSSE();
    if (_pollInterval) { clearInterval(_pollInterval); _pollInterval = null; }
    if (!status.registered) {
      document.getElementById('auth-msg').textContent = 'No passkey registered yet.';
      btnReg.style.display = 'block';
      btnAuth.style.display = 'none';
    } else {
      document.getElementById('auth-msg').textContent = 'Sign in with your passkey.';
      btnReg.style.display = 'none';
      btnAuth.style.display = 'block';
    }
  }
}

async function registerPasskey() {
  document.getElementById('auth-error').textContent = '';
  try {
    const opts = await fetch('/auth/register-options', {method: 'POST'}).then(r => r.json());
    opts.challenge = b64urlToBuffer(opts.challenge);
    opts.user.id = b64urlToBuffer(opts.user.id);
    const cred = await navigator.credentials.create({publicKey: opts});
    const body = JSON.stringify({
      id: cred.id,
      rawId: bufferToB64url(cred.rawId),
      type: cred.type,
      response: {
        clientDataJSON: bufferToB64url(cred.response.clientDataJSON),
        attestationObject: bufferToB64url(cred.response.attestationObject),
      },
    });
    const resp = await fetch('/auth/register', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({detail: resp.statusText}));
      document.getElementById('auth-error').textContent = err.detail || 'Registration failed';
      return;
    }
    await checkAuth();
  } catch (e) {
    document.getElementById('auth-error').textContent = e.message || String(e);
  }
}

async function authenticatePasskey() {
  document.getElementById('auth-error').textContent = '';
  try {
    const opts = await fetch('/auth/authenticate-options', {method: 'POST'}).then(r => r.json());
    opts.challenge = b64urlToBuffer(opts.challenge);
    if (opts.allowCredentials) {
      opts.allowCredentials = opts.allowCredentials.map(c => ({...c, id: b64urlToBuffer(c.id)}));
    }
    const cred = await navigator.credentials.get({publicKey: opts});
    const body = JSON.stringify({
      id: cred.id,
      rawId: bufferToB64url(cred.rawId),
      type: cred.type,
      response: {
        clientDataJSON: bufferToB64url(cred.response.clientDataJSON),
        authenticatorData: bufferToB64url(cred.response.authenticatorData),
        signature: bufferToB64url(cred.response.signature),
        userHandle: cred.response.userHandle ? bufferToB64url(cred.response.userHandle) : null,
      },
    });
    const resp = await fetch('/auth/authenticate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({detail: resp.statusText}));
      document.getElementById('auth-error').textContent = err.detail || 'Authentication failed';
      return;
    }
    await checkAuth();
  } catch (e) {
    document.getElementById('auth-error').textContent = e.message || String(e);
  }
}

async function signOut() {
  await fetch('/auth/logout', {method: 'POST'});
  if (ws) { ws.close(); ws = null; }
  disconnectSSE();
  if (_pollInterval) { clearInterval(_pollInterval); _pollInterval = null; }
  await checkAuth();
}

// pending chat task_ids waiting for RESULT
const pendingChats = new Map(); // task_id -> bubble element

// ── WebSocket ──
let ws;
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const wsUrl = `${proto}://${location.host}/ws`;
  ws = new WebSocket(wsUrl);
  ws.onopen = () => {
    document.getElementById('status-dot').classList.remove('disconnected');
    document.getElementById('conn-label').textContent = 'Connected';
    logEntry({type:'CONTROL', from_id:'system', payload:{msg:'WebSocket connected'}, timestamp: new Date().toISOString()});
  };
  ws.onclose = () => {
    document.getElementById('status-dot').classList.add('disconnected');
    document.getElementById('conn-label').textContent = 'Disconnected — retrying…';
    if (_pollInterval) {  // only retry while authenticated (polling is active)
      setTimeout(connectWS, 3000);
    }
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    logEntry(msg);
    if (['RESULT','STATUS','CONTROL'].includes(msg.type)) {
      refreshAgents();
      refreshTasks();
      refreshAgentTree();
    }
    // Director response
    if (msg.type === 'RESULT' && pendingChats.has(msg.payload?.task_id)) {
      const bubble = pendingChats.get(msg.payload.task_id);
      pendingChats.delete(msg.payload.task_id);
      const output = msg.payload.output || '';
      bubble.className = 'chat-bubble bubble-director';
      bubble.textContent = output;
      scrollChat();
      document.getElementById('chat-send-btn').disabled = false;
      document.getElementById('chat-input').disabled = false;
    }
    // Agent P2P conversation (only show forwarded = actually delivered)
    if (msg.type === 'PEER_MSG' && msg.payload?._forwarded) {
      addConversationEntry(msg);
    }
  };
}

// ── Agent view toggle (list / tree) ──
let _agentView = 'table'; // 'table' or 'tree'

function setAgentView(mode) {
  _agentView = mode;
  const tableWrap = document.getElementById('agents-table-wrap');
  const treeWrap = document.getElementById('agents-tree');
  const btnTable = document.getElementById('btn-table-view');
  const btnTree = document.getElementById('btn-tree-view');
  if (mode === 'tree') {
    tableWrap.style.display = 'none';
    treeWrap.style.display = 'block';
    btnTable.classList.remove('active');
    btnTree.classList.add('active');
    refreshAgentTree();
  } else {
    tableWrap.style.display = '';
    treeWrap.style.display = 'none';
    btnTable.classList.add('active');
    btnTree.classList.remove('active');
  }
}

function statusClass(s) { return 'status-' + (s || 'stopped').toLowerCase(); }

function renderTreeNodes(nodes, depth) {
  if (!nodes || nodes.length === 0) return '';
  const items = nodes.map(node => {
    const sc = statusClass(node.status);
    const roleClass = node.role === 'director' ? 'director' : '';
    const taskHint = node.current_task
      ? `<span style="color:#6e7681;font-size:0.72rem">task:${esc(node.current_task.slice(0,8))}</span>` : '';
    const childHtml = node.children && node.children.length > 0
      ? `<ul class="tree-node">${renderTreeNodes(node.children, depth + 1)}</ul>`
      : '';
    return `<li>
      <div class="tree-item">
        <span class="tree-item-id">${esc(node.id)}</span>
        <span class="tree-item-role ${roleClass}">${esc(node.role || 'worker')}</span>
        <span class="${sc}">${esc(node.status)}</span>
        ${taskHint}
      </div>
      ${childHtml}
    </li>`;
  });
  return items.join('');
}

function refreshAgentTree() {
  if (_agentView !== 'tree') return;
  fetch(`${API_BASE}/agents/tree`)
    .then(r => {
      if (r.status === 401) { checkAuth(); return null; }
      return r.json();
    })
    .then(roots => {
      if (!roots) return;
      const wrap = document.getElementById('agents-tree');
      if (roots.length === 0) {
        wrap.innerHTML = '<div class="empty-hint">No agents</div>';
        return;
      }
      wrap.innerHTML = `<ul class="tree-node" style="padding-left:8px;margin-top:6px">${renderTreeNodes(roots, 0)}</ul>`;
    }).catch(console.error);
}

// ── Polling ──
let directorId = null;

function refreshAgents() {
  fetch(`${API_BASE}/agents`)
    .then(r => {
      if (r.status === 401) { checkAuth(); return null; }
      return r.json();
    })
    .then(agents => {
      if (!agents) return;
      document.getElementById('agent-count').textContent = agents.length;
      const body = document.getElementById('agents-body');
      if (agents.length === 0) {
        body.innerHTML = '<tr><td colspan="4" class="empty-hint">No agents</td></tr>';
        return;
      }
      body.innerHTML = agents.map(a => {
        const sc = 'status-' + a.status.toLowerCase();
        const roleLabel = a.role === 'director'
          ? '<span class="role-director">[director]</span>' : '';
        return `<tr>
          <td>${esc(a.id)}${roleLabel}</td>
          <td>${esc(a.role || 'worker')}</td>
          <td class="${sc}">${esc(a.status)}</td>
          <td>${a.current_task ? esc(a.current_task.slice(0,8)) : '—'}</td>
        </tr>`;
      }).join('');

      // Show/hide chat panel based on whether a director exists
      const director = agents.find(a => a.role === 'director');
      const chatSection = document.getElementById('chat-section');
      if (director && !directorId) {
        directorId = director.id;
        document.getElementById('director-id-badge').textContent = director.id;
        chatSection.style.display = 'flex';
        document.querySelector('main').style.gridTemplateRows = '1fr 280px 1.6fr';
      } else if (!director && directorId) {
        directorId = null;
        chatSection.style.display = 'none';
        document.querySelector('main').style.gridTemplateRows = '1fr 1.6fr';
      }
    }).catch(console.error);
}

function refreshTasks() {
  fetch(`${API_BASE}/tasks`)
    .then(r => {
      if (r.status === 401) { checkAuth(); return null; }
      return r.json();
    })
    .then(tasks => {
      if (!tasks) return;
      document.getElementById('task-count').textContent = tasks.length;
      const body = document.getElementById('tasks-body');
      if (tasks.length === 0) {
        body.innerHTML = '<tr><td colspan="3" class="empty-hint">Queue empty</td></tr>';
        return;
      }
      body.innerHTML = tasks.map(t => `<tr>
        <td>${esc(String(t.priority))}</td>
        <td>${esc(t.task_id.slice(0,8))}</td>
        <td>${esc(t.prompt.slice(0,60))}${t.prompt.length > 60 ? '…' : ''}</td>
      </tr>`).join('');
    }).catch(console.error);
}

// ── Agent Conversations ──
let convTotal = 0;

function agentColor(id) {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) & 0xffff;
  return `hsl(${h % 360}, 65%, 65%)`;
}

function addConversationEntry(msg) {
  const list = document.getElementById('conv-list');
  if (convTotal === 0) list.innerHTML = ''; // clear placeholder
  convTotal++;
  document.getElementById('conv-count').textContent = convTotal;

  const content = msg.payload?.content
    || (msg.payload ? JSON.stringify(msg.payload).replace(/"_forwarded":true,?\s*/g, '') : '');
  const ts = new Date(msg.timestamp).toLocaleTimeString();

  const div = document.createElement('div');
  div.className = 'conv-entry';
  div.innerHTML =
    `<span class="conv-ts">${ts}</span>` +
    `<span class="conv-from" style="color:${agentColor(msg.from_id)}">${esc(msg.from_id)}</span>` +
    `<span class="conv-arrow">→</span>` +
    `<span class="conv-to" style="color:${agentColor(msg.to_id || '')}">${esc(msg.to_id || '*')}</span>` +
    `<span class="conv-sep">│</span>` +
    `<span class="conv-content">${esc(String(content).slice(0, 300))}</span>`;
  list.appendChild(div);
  list.scrollTop = list.scrollHeight;
}

// ── Director Chat ──
function scrollChat() {
  const h = document.getElementById('chat-history');
  h.scrollTop = h.scrollHeight;
}

function addBubble(text, role) {
  const div = document.createElement('div');
  div.className = 'chat-bubble bubble-' + role;
  div.textContent = text;
  document.getElementById('chat-history').appendChild(div);
  scrollChat();
  return div;
}

async function sendChat() {
  if (!directorId) return;
  const inp = document.getElementById('chat-input');
  const btn = document.getElementById('chat-send-btn');
  const message = inp.value.trim();
  if (!message) { inp.focus(); return; }

  inp.value = '';
  inp.disabled = true;
  btn.disabled = true;

  addBubble(message, 'user');
  const thinkingBubble = addBubble('Thinking…', 'thinking');

  try {
    const resp = await fetch(`${API_BASE}/director/chat`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message})
    });
    if (!resp.ok) {
      thinkingBubble.className = 'chat-bubble bubble-director';
      thinkingBubble.textContent = 'Error: ' + resp.statusText;
      btn.disabled = false;
      inp.disabled = false;
      return;
    }
    const data = await resp.json();
    // Register for WebSocket result
    pendingChats.set(data.task_id, thinkingBubble);
  } catch (e) {
    thinkingBubble.className = 'chat-bubble bubble-director';
    thinkingBubble.textContent = 'Error: ' + e.message;
    btn.disabled = false;
    inp.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('chat-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
  document.getElementById('task-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') submitTask();
  });
  checkAuth();
});

// ── Worker Task submit ──
async function submitTask() {
  const inp = document.getElementById('task-input');
  const pri = document.getElementById('priority-input');
  const prompt = inp.value.trim();
  if (!prompt) { inp.focus(); return; }
  const resp = await fetch(`${API_BASE}/tasks`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({prompt, priority: parseInt(pri.value || '0', 10)})
  });
  if (resp.ok) { inp.value = ''; refreshTasks(); }
  else alert('Failed: ' + resp.statusText);
}

// ── Log ──
const MAX_LOG = 200;
function logEntry(msg) {
  const list = document.getElementById('log-list');
  const ts = new Date(msg.timestamp).toLocaleTimeString();
  const div = document.createElement('div');
  div.className = 'log-entry';
  const payload = JSON.stringify(msg.payload || {});
  div.innerHTML = `<span class="log-ts">${ts}</span>
    <span class="log-type type-${msg.type}">${msg.type}</span>
    <span>${esc(msg.from_id)} → ${esc(msg.to_id || '*')}: ${esc(payload.slice(0,120))}</span>`;
  list.prepend(div);
  while (list.children.length > MAX_LOG) list.removeChild(list.lastChild);
}
function clearLog() { document.getElementById('log-list').innerHTML = ''; }

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
</script>
</body>
</html>
"""
