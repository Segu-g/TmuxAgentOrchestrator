"""Pydantic request/response schemas for the FastAPI web layer.

Extracted from ``web/app.py`` in v1.1.5 to reduce file size and improve
modularity.  All schemas are re-exported from ``web/app.py`` for backward
compatibility.

Design reference: DESIGN.md §10.41 (v1.1.5).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, field_validator, model_validator


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


class AgentBriefRequest(BaseModel):
    """Request body for POST /agents/{agent_id}/brief.

    Injects an out-of-band context message into a running agent's worktree.
    The orchestrator writes ``__brief__/{brief_id}.txt`` into the agent's
    worktree directory and sends ``__BRIEF__:{brief_id}`` to the agent's tmux
    pane so it can retrieve the content with the ``/read-brief`` slash command.

    Design references:
    - OpenAI Agents SDK "Context Management" (2025): adding data to agent
      instructions must go through the conversation history.
    - LangChain "Context Engineering in Agents" (2025): runtime context changes
      are transient (per-call); lifecycle changes persist to state.
    - Claude Code Hooks Reference — ``additionalContext`` injection (2025).
    - DESIGN.md §10.43 (v1.1.7)
    """

    content: str
    brief_id: str | None = None  # auto-generated UUID when None

    @field_validator("content")
    @classmethod
    def content_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content must not be empty")
        return v

    @field_validator("content")
    @classmethod
    def content_max_length(cls, v: str) -> str:
        if len(v) > 4096:
            raise ValueError("content must not exceed 4096 characters")
        return v


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
        a future release.
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

    @field_validator("topic")
    @classmethod
    def topic_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("topic must not be empty")
        return v


class SpecFirstWorkflowSubmit(BaseModel):
    """Request body for POST /workflows/spec-first — Spec-First development workflow.

    Submits a 2-agent sequential Spec-First Workflow DAG where a spec-writer
    produces a formal specification (SPEC.md) that an implementer then follows
    to write code and tests:

      - **spec-writer**: reads the requirements, produces a formal SPEC.md with
        preconditions, postconditions, invariants, type signatures, and acceptance
        criteria.  Stores the spec in the shared scratchpad.
      - **implementer**: reads SPEC.md from the scratchpad, implements the feature
        satisfying every acceptance criterion, writes tests, and runs them.

    Scratchpad keys (Blackboard pattern):

    - ``{scratchpad_prefix}_spec``  : spec-writer's SPEC.md content
    - ``{scratchpad_prefix}_impl``  : implementer's completion summary

    Artifacts produced by agents:

    - ``SPEC.md``          — formal specification (in spec-writer worktree)
    - ``<impl>.py``        — implementation (in implementer worktree)
    - ``test_<impl>.py``   — tests (in implementer worktree)

    Design references:

    - Vasilopoulos arXiv:2602.20478 "Codified Context" (2026): Formal specification
      documents help agents maintain consistency across sessions.
    - Hou et al. "Position: Trustworthy AI Agents Require Formal Methods" (2025):
      TLA+/Hoare assertions integrated into LLM agent pipelines.
    - SYSMOBENCH arXiv:2509.23130 (2025): LLM formal specification generation
      evaluated on 200 system models.
    - DESIGN.md §10.44 (v1.1.8)
    """

    topic: str
    requirements: str
    # Optional per-role required_tags for agent capability routing
    spec_tags: list[str] = []
    impl_tags: list[str] = []
    # When set, the implementer's RESULT is routed to this agent's mailbox
    reply_to: str | None = None

    @field_validator("topic")
    @classmethod
    def topic_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("topic must not be empty")
        return v

    @field_validator("requirements")
    @classmethod
    def requirements_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("requirements must not be empty")
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
# Mob Code Review Workflow
# ---------------------------------------------------------------------------

_DEFAULT_MOB_ASPECTS = ["security", "performance", "maintainability", "testing"]


class MobReviewWorkflowSubmit(BaseModel):
    """Request body for POST /workflows/mob-review — Mob Code Review.

    Submits an (N+1)-agent workflow DAG where N reviewer agents each examine the
    same code artefact from a **distinct quality dimension** in parallel, then a
    single synthesizer agent reads all reviews from the scratchpad and produces a
    unified ``MOB_REVIEW.md`` report.

    Workflow topology::

        reviewer_security       ──┐
        reviewer_performance    ──┼─→ synthesizer
        reviewer_maintainability──┤
        reviewer_testing        ──┘

    **Phase 1 — Parallel Reviewers** (``depends_on=[]``, all start simultaneously):
      One reviewer per aspect in ``aspects``.  Each reviewer writes its findings to
      a file ``review_{aspect}.md`` and stores it in the shared scratchpad under the
      key ``{prefix}_review_{aspect}``.

    **Phase 2 — Synthesizer** (``depends_on=all reviewer task IDs``):
      Reads every ``{prefix}_review_{aspect}`` key from the scratchpad, merges them
      into a structured ``MOB_REVIEW.md``, and stores the result under
      ``{prefix}_synthesis``.

    Scratchpad keys (Blackboard pattern):
    - ``{prefix}_review_{aspect}`` : per-aspect review findings (one per reviewer)
    - ``{prefix}_synthesis``       : synthesizer's ``MOB_REVIEW.md`` content

    Artefacts produced by agents:
    - ``review_{aspect}.md``  — each reviewer's aspect-specific findings
    - ``MOB_REVIEW.md``       — synthesized review report

    Design references:
    - ChatEval (arXiv:2308.07201, ICLR 2024): unique reviewer personas eliminate
      performance degradation caused by role-prompt homogeneity.
    - Agent-as-a-Judge (arXiv:2508.02994, 2025): aggregating independent judgements
      reduces variance akin to a voting committee.
    - Code in Harmony (OpenReview 2025): parallel multi-agent code quality evaluation
      outperforms sequential review when dimensions are orthogonal.
    - Multi-Agent LLM SE Refactoring (ResearchGate 2025): specialised agents for
      security, performance, and maintainability improve multi-dimensional code quality.
    - DESIGN.md §10.52 (v1.1.20)
    """

    # The code (or path description) to review
    code: str
    # Language or framework context (e.g. "Python FastAPI", "TypeScript React")
    language: str = "Python"
    # Aspects to review; default: security, performance, maintainability, testing
    aspects: list[str] = _DEFAULT_MOB_ASPECTS
    # Optional routing tags for reviewer and synthesizer agents
    reviewer_tags: list[str] = []
    synthesizer_tags: list[str] = []
    # When set, the synthesizer RESULT is routed to this agent's mailbox
    reply_to: str | None = None

    @field_validator("code")
    @classmethod
    def code_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("code must not be empty")
        return v

    @field_validator("aspects")
    @classmethod
    def aspects_must_have_two_to_eight(cls, v: list) -> list:
        if len(v) < 2:
            raise ValueError("aspects must have at least 2 entries")
        if len(v) > 8:
            raise ValueError("aspects must have at most 8 entries")
        for a in v:
            if not str(a).strip():
                raise ValueError("aspect names must not be blank")
        return v


# ---------------------------------------------------------------------------
# Iterative Review Workflow
# ---------------------------------------------------------------------------


class IterativeReviewWorkflowSubmit(BaseModel):
    """Request body for POST /workflows/iterative-review.

    Submits a 3-agent sequential pipeline workflow where an implementer writes code,
    a reviewer critiques it (Self-Refine FEEDBACK step), and a revisor produces an
    improved version (Self-Refine REFINE step).

    Workflow topology (all sequential via depends_on)::

        implementer → reviewer → revisor

    Design references:
    - Self-Refine (Madaan et al. NeurIPS 2023, arXiv:2303.17651): FEEDBACK→REFINE
      iterative loop improves output quality ~20% on average.
    - MAR: Multi-Agent Reflexion (arXiv:2512.20845, 2025): cross-agent feedback
      outperforms single-agent self-feedback.
    - RevAgent (arXiv:2511.00517, 2025): multi-stage code review pipeline with
      specialized roles per stage.
    - DESIGN.md §10.53 (v1.1.21)

    Scratchpad keys (Blackboard pattern):
    - ``{prefix}_implementation`` : implementer's initial code (written by implementer)
    - ``{prefix}_review``         : reviewer's annotated feedback (written by reviewer)
    - ``{prefix}_revised``        : revisor's improved code (written by revisor)

    Artefacts produced:
    - ``implementation.py`` / ``implementation.{ext}`` — initial implementation
    - ``review.md`` — annotated review with Self-Refine FEEDBACK format
    - ``revised.py`` / ``revised.{ext}`` — revised implementation after feedback
    """

    # Task specification — what to implement
    task: str
    # Language / framework context
    language: str = "Python"
    # Optional routing tags per role (empty = any available agent)
    implementer_tags: list[str] = []
    reviewer_tags: list[str] = []
    revisor_tags: list[str] = []
    # When set, the revisor RESULT is routed to this agent's mailbox
    reply_to: str | None = None

    @field_validator("task")
    @classmethod
    def task_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("task must not be empty")
        return v
