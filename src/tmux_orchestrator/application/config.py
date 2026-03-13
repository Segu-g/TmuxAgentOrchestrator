"""YAML config loader and dataclasses for TmuxAgentOrchestrator."""

from __future__ import annotations

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# Backward-compat shim — AgentRole now lives in domain/
# This re-export preserves all existing import paths unchanged.
from tmux_orchestrator.domain.agent import AgentRole  # noqa: F401


@dataclass
class AgentConfig:
    id: str
    type: Literal["claude_code"]
    isolate: bool = True        # False → share main repo working tree
    role: AgentRole = AgentRole.WORKER
    task_timeout: int | None = None   # overrides OrchestratorConfig.task_timeout when set
    command: str | None = None  # custom command (defaults to claude CLI)
    # --- Context engineering fields ---
    system_prompt: str | None = None  # injected into agent's CLAUDE.md at startup
    # system_prompt_file: path to a Markdown role template file (relative to
    # config file directory or absolute).  Content is read at build_system() time
    # and used as system_prompt when system_prompt is not explicitly set.
    # Reference: ChatEval ICLR 2024 (arXiv:2308.07201) — role diversity critical.
    # Reference: CONSENSAGENT ACL 2025 — sycophancy suppression via role prompts.
    system_prompt_file: str | None = None
    context_files: list[str] = field(default_factory=list)  # paths (relative to cwd) to pre-load
    # context_spec_files: glob patterns for cold-memory specification documents
    # (Vasilopoulos arXiv:2602.20478 "Codified Context" 2026: 3rd-tier cold memory).
    # Each pattern is expanded relative to context_spec_files_root (defaults to cwd).
    # Matched files are copied into the agent worktree at agent start time.
    # Recommended location: .claude/specs/ (architecture.md, decisions/*.md, etc.)
    context_spec_files: list[str] = field(default_factory=list)
    # spec_files: paths to YAML convention/constraint files whose rules are injected
    # into the agent's CLAUDE.md under a "## Codified Specs" section (hot-memory).
    # Each file must be a YAML document with optional fields:
    #   name: str, description: str, rules: list[str], examples: list[str]
    # Paths are resolved relative to cwd (or absolute).
    #
    # Rationale (hot-memory vs cold-memory):
    #   context_spec_files copies raw YAML files into the worktree — the agent must
    #   explicitly read them (cold-memory, Tier-3 in Vasilopoulos arXiv:2602.20478).
    #   spec_files injects parsed rule text directly into CLAUDE.md so every agent
    #   sees the conventions on its very first context load without extra I/O.
    #
    # Reference:
    #   Vasilopoulos et al. arXiv:2602.20478 §3 "Hot-memory constitution" (2026)
    #   Thoughtworks "Spec-driven development" (2025)
    #   arXiv:2602.02584 "Constitutional Spec-Driven Development" (2026)
    #   DESIGN.md §10.86 (v1.2.10)
    spec_files: list[str] = field(default_factory=list)
    # --- Capability tags for smart dispatch ---
    # Tasks with required_tags are only dispatched to agents whose tags include
    # ALL required tags.  Reference: FIPA Directory Facilitator (2002),
    # Kubernetes Node Affinity (nodeSelector pattern).
    tags: list[str] = field(default_factory=list)
    # --- Group membership (pre-registration at startup) ---
    # Agent is automatically added to each named group when the orchestrator starts.
    # Groups must exist in OrchestratorConfig.groups (or be created at runtime).
    # Reference: Kubernetes Node Pool labels; AWS Auto Scaling Group tags.
    # DESIGN.md §10.26 (v0.31.0)
    groups: list[str] = field(default_factory=list)
    # --- Worktree lifecycle ---
    # When True and isolate=True, the orchestrator squash-merges the agent's
    # worktree branch into the main repo HEAD before teardown.  Commits made
    # by the agent inside its worktree therefore land on the original branch
    # automatically.  Set to False (default) to delete commits on stop.
    merge_on_stop: bool = False
    merge_target: str | None = None  # target branch for merge_on_stop; None = merge into current HEAD
    # cleanup_subdir: when True (default) and isolate=False, the per-agent
    #   .agent/{agent_id}/ subdirectory is deleted with shutil.rmtree() on
    #   agent stop().  Set to False to retain the subdir for post-mortem
    #   inspection or debugging.
    #
    # Rationale: the .agent/{id}/ subdir is created for the lifetime of the
    #   agent (similar to a TemporaryDirectory context manager), so it should
    #   be cleaned up automatically.  For isolate=True agents this field has
    #   no effect because the worktree is managed by WorktreeManager.teardown().
    #
    # Reference: Python docs "tempfile.TemporaryDirectory" cleanup semantics;
    #   DESIGN.md §10.69 (v1.1.37 — .agent/{id}/ cleanup on stop)
    cleanup_subdir: bool = True
    # keep_branch_on_stop: when True and isolate=True, the worktree filesystem
    #   is removed on stop but the git branch ("worktree/{agent_id}") is kept.
    #   This allows successor phases or post-mortem inspection to read the agent's
    #   committed artifacts from git history even after the worktree is gone.
    #
    # When False (default), both the worktree filesystem and the branch are
    #   deleted on stop (existing behaviour, WorktreeManager.teardown()).
    # When True, only the filesystem is removed (WorktreeManager.keep_branch()).
    #
    # This is automatically set to True for ephemeral agents in branch-chain
    #   workflows (chain_branch=True) so that successor phases can branch from
    #   the predecessor's committed state via create_from_branch().
    #
    # Reference: git-worktree(1) — "git worktree remove" keeps the branch;
    #   DESIGN.md §10.82 (v1.2.6 — branch artifact persistence)
    keep_branch_on_stop: bool = False
    # max_consecutive_failures: when > 0, the orchestrator automatically stops
    #   and restarts this agent after this many consecutive task failures.
    #   A "consecutive failure" is a RESULT message with error != None after all
    #   retry attempts are exhausted (i.e. the task enters the dead-letter queue).
    #   The counter resets to 0 on any successful task completion.
    #   Set to 0 (default) to disable auto-restart for this agent.
    #
    # Rationale: complements the CircuitBreaker (which blocks dispatch) with
    #   proactive remediation — recycling a stuck agent process so it can accept
    #   new work again.  Mirrors the Erlang OTP one_for_one restart strategy
    #   (Ericsson 1996) and AWS ECS unhealthy task replacement (AWS 2023).
    #
    # Reference: Erlang/OTP supervisor behaviour https://www.erlang.org/doc/system/sup_princ.html
    #   AWS ECS task replacement https://aws.amazon.com/blogs/containers/a-deep-dive-into-amazon-ecs-task-health-and-task-replacement/
    #   DESIGN.md §10.88 (v1.2.12)
    max_consecutive_failures: int = 3


@dataclass
class WebhookConfig:
    """Configuration for a single outbound webhook endpoint.

    Loaded from the ``webhooks`` list in the YAML config.
    Each entry registers one webhook at server startup via WebhookManager.register().

    Fields:
        url:    HTTP(S) endpoint that receives POST requests.
                Supports ``${ENV_VAR}`` expansion at load time.
        events: List of event names to deliver (e.g. ``["agent_status", "task_complete"]``).
                Use ``["*"]`` to receive all events.
        secret: Optional HMAC-SHA256 signing secret.  When set, each delivery
                includes an ``X-Signature-SHA256`` header for verification.
                Supports ``${ENV_VAR}`` expansion at load time.
        max_retries: Maximum number of retry attempts after the initial failure.
                Default 3.  Set to 0 to disable retries (fire-and-forget).
        retry_backoff_base: Base value (seconds) for exponential backoff calculation.
                Effective sleep = min(60, retry_backoff_base * 2^attempt) with equal jitter.
                Default 1.0.

    Reference:
        GitHub Webhooks https://docs.github.com/en/webhooks
        Stripe Webhooks https://docs.stripe.com/webhooks
        AWS Exponential Backoff and Jitter https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
        DESIGN.md §10.N (v1.0.21); §10.N (v1.0.22 — retry + env expansion)
    """

    url: str
    events: list[str] = field(default_factory=list)
    secret: str | None = None
    max_retries: int = 3
    retry_backoff_base: float = 1.0


@dataclass
class OrchestratorConfig:
    session_name: str = "orchestrator"
    agents: list[AgentConfig] = field(default_factory=list)
    # Each entry is a pair [agent_id_a, agent_id_b] — bidirectional permission
    p2p_permissions: list[tuple[str, str]] = field(default_factory=list)
    task_timeout: int = 120
    # mailbox_dir: base directory for agent mailboxes.
    # Default is a project-relative path ".orchestrator/mailbox", resolved at
    # load_config() time against the server cwd (or Path.cwd() when omitted).
    # Absolute paths (including ~ expansions) are used as-is.
    # Reference: DESIGN.md §10.62 (v1.1.30 — project-scoped mailbox directory)
    mailbox_dir: str = ".orchestrator/mailbox"
    web_base_url: str = "http://localhost:8000"
    circuit_breaker_threshold: int = 3
    circuit_breaker_recovery: float = 60.0
    dlq_max_retries: int = 50  # re-queue attempts before dead-lettering a task
    task_queue_maxsize: int = 0  # 0 = unbounded; >0 = bounded (submit_task raises when full)
    watchdog_poll: float = 30.0  # seconds between watchdog checks (lower in tests)
    # --- ERROR state auto-recovery ---
    recovery_attempts: int = 3   # max restart attempts per agent before giving up
    recovery_backoff_base: float = 5.0  # seconds; attempt N waits backoff_base^N seconds
    recovery_poll: float = 2.0   # seconds between recovery checks
    # --- Rate limiting (token bucket) ---
    # rate_limit_rps: token refill rate in tasks per second; 0 = unlimited (default)
    # rate_limit_burst: bucket capacity; defaults to 2× rps or 1 when rps == 0
    # Reference: Tanenbaum "Computer Networks" 5th ed. §5.3; DESIGN.md §10.16 (v0.20.0)
    rate_limit_rps: float = 0.0
    rate_limit_burst: int = 0
    # --- Context window monitoring ---
    # context_window_tokens: total context window size (default 200 000 for Claude Sonnet/Opus).
    # context_warn_threshold: fraction (0-1) at which context_warning is published (default 0.75).
    # context_auto_summarize: when True, /summarize is injected into agent pane at threshold.
    # context_auto_compress: when True, TF-IDF compression is run automatically at threshold and
    #   the compressed context is injected into the agent pane as a __COMPRESS_CONTEXT__ notification.
    #   This is the upper-level alternative to context_auto_summarize (extractive vs generative).
    # context_compress_drop_percentile: fraction of low-relevance lines to drop (default 0.40 = 40%).
    # context_monitor_poll: poll interval in seconds (default 5.0).
    # Reference: Liu et al. "Lost in the Middle" TACL 2024 https://arxiv.org/abs/2307.03172
    # Reference: ACON arXiv:2510.00615 (Kang et al. 2025) — threshold-based context compression
    # Reference: Focus Agent arXiv:2601.07190 (Verma 2026) — autonomous context compression
    # Reference: Anthropic context windows docs https://platform.claude.com/docs/en/build-with-claude/context-windows
    context_window_tokens: int = 200_000
    context_warn_threshold: float = 0.75
    context_auto_summarize: bool = False
    context_auto_compress: bool = False
    context_compress_drop_percentile: float = 0.40
    context_monitor_poll: float = 5.0
    # --- Drift monitoring ---
    # drift_threshold: composite drift score below which agent_drift_warning fires (default 0.6).
    # drift_idle_threshold: seconds of unchanged pane output before idle_score reaches 0.0 (default 300).
    # drift_monitor_poll: poll interval in seconds (default 10.0).
    # Reference: Rath arXiv:2601.04170 "Agent Drift" (2026) — ASI framework
    drift_threshold: float = 0.6
    drift_idle_threshold: float = 300.0
    drift_monitor_poll: float = 10.0
    # --- Drift auto re-brief ---
    # drift_rebrief_enabled: when True, the orchestrator automatically sends a
    #   role reminder to agents whose drift score drops below drift_threshold.
    # drift_rebrief_cooldown: minimum seconds between successive re-briefs for
    #   the same agent (default 60).  Prevents spam when the agent remains drifted.
    # drift_rebrief_message: prefix injected before the task prompt snippet.
    #   The final message is: "{drift_rebrief_message}\n\nYour current task:\n{prompt[:200]}"
    # Reference: Rath arXiv:2601.04170 — "drift-aware routing" re-brief pattern.
    # Reference: arXiv:2603.03258 — "goal reminder injection" for drift prevention.
    # DESIGN.md §10.50 (v1.1.18)
    drift_rebrief_enabled: bool = True
    drift_rebrief_cooldown: float = 60.0
    drift_rebrief_message: str = (
        "⚠ ROLE REMINDER from orchestrator: You appear to be drifting from your "
        "assigned role. Please refocus on your task."
    )
    # --- Queue-depth autoscaling ---
    # autoscale_min: minimum number of autoscaled agents (0 = scale to zero).
    # autoscale_max: maximum number of autoscaled agents (0 = disabled).
    # autoscale_threshold: queue depth per idle agent before scaling up.
    # autoscale_cooldown: seconds of queue-empty before scaling down.
    # autoscale_poll: seconds between scale checks.
    # autoscale_agent_tags: capability tags assigned to auto-created agents.
    # autoscale_system_prompt: system prompt for auto-created agents.
    # References:
    #   Kubernetes HPA https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/
    #   Thijssen "Autonomic Computing" (MIT Press, 2009) — MAPE-K loop
    #   AWS Auto Scaling cooldowns https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-cooldowns.html
    autoscale_min: int = 0
    autoscale_max: int = 0          # 0 = autoscaling disabled
    autoscale_threshold: int = 3
    autoscale_cooldown: float = 30.0
    autoscale_poll: float = 5.0
    autoscale_agent_tags: list[str] = field(default_factory=list)
    autoscale_system_prompt: str | None = None
    # --- Task result persistence (Event Sourcing / CQRS pattern) ---
    # result_store_enabled: when True, every RESULT message is appended to a
    #   JSONL file on disk.  Disabled by default to avoid unexpected I/O.
    # result_store_dir: directory where JSONL files are written.
    #   Layout: {result_store_dir}/{session_name}/{YYYY-MM-DD}.jsonl
    # References:
    #   Martin Fowler "Event Sourcing" (2005)
    #   Greg Young "CQRS Documents" (2010)
    #   Rich Hickey "The Value of Values" (Datomic, 2012)
    result_store_enabled: bool = False
    result_store_dir: str = "~/.tmux_orchestrator/results"
    # --- Named agent groups ---
    # groups: list of {name, agent_ids} dicts describing pre-configured agent pools.
    # Groups allow tasks to target a named pool instead of individual agents or tags.
    # References:
    #   Kubernetes Node Pools / Node Groups (GKE, EKS, AKS)
    #   AWS Auto Scaling Groups https://docs.aws.amazon.com/autoscaling/ec2/userguide/auto-scaling-groups.html
    #   Apache Mesos Roles https://mesos.apache.org/documentation/latest/roles/
    #   HashiCorp Nomad Task Groups https://developer.hashicorp.com/nomad/docs/job-specification/group
    # DESIGN.md §10.26 (v0.31.0)
    groups: list[dict] = field(default_factory=list)
    # --- Webhook notifications ---
    # webhook_timeout: HTTP timeout (seconds) per delivery attempt.
    # webhooks: pre-configured webhook endpoints registered at server startup.
    #   Each entry is a WebhookConfig with url, events, and optional secret.
    #   Webhooks registered here supplement any dynamically added via POST /webhooks.
    # Reference: GitHub Webhooks; Stripe Webhooks; RFC 2104 HMAC;
    # Zalando RESTful API Guidelines §webhook; Shopify webhook verification.
    # DESIGN.md §10.25 (v0.30.0); §10.N (v1.0.21 — static webhook config)
    webhook_timeout: float = 5.0
    webhooks: list[WebhookConfig] = field(default_factory=list)
    # --- Task TTL (Time-to-Live / expiry) ---
    # default_task_ttl: global default TTL in seconds applied to tasks that do
    #   not set an explicit ttl.  None = no default (tasks never expire unless
    #   ttl is set per-task).
    # ttl_reaper_poll: poll interval (seconds) for the background reaper that
    #   scans _waiting_tasks for expired entries.
    # Reference: RabbitMQ TTL docs (https://www.rabbitmq.com/docs/ttl);
    # Azure Service Bus message expiration (Microsoft Docs 2024);
    # AWS SQS MessageRetentionPeriod; Dapr pubsub-message-ttl;
    # DESIGN.md §10.28 (v0.33.0)
    default_task_ttl: float | None = None
    ttl_reaper_poll: float = 1.0
    # --- Episodic memory auto-record + auto-inject (MIRIX pattern, v1.0.29) ---
    # memory_auto_record: when True, automatically append an episode to the
    #   EpisodeStore on every explicit task-complete call.  The episode summary
    #   is the output string submitted by the agent.
    # memory_inject_count: number of most-recent episodes to prepend to the
    #   task prompt at dispatch time.  0 = disabled (no injection).
    # References:
    #   Wang & Chen, "MIRIX", arXiv:2507.07957 (2025) — Active Retrieval pattern
    #   "PlugMem", arXiv:2603.03296 (2025) — task-agnostic memory injection
    #   "Design Patterns for Long-Term Memory", Serokell Blog (2025)
    #   DESIGN.md §10.29 (v1.0.29)
    memory_auto_record: bool = True
    memory_inject_count: int = 5
    # --- Web API key (written to agent context files for authenticated REST calls) ---
    # When the web server is started with an API key, the key is stored here so that
    # agents can include it in REST requests (notify_parent, /progress, /send-message).
    # Empty string = no authentication required.
    # Reference: RFC 7235 HTTP Authentication; DESIGN.md §10.29 (v0.34.0)
    api_key: str = ""
    # --- CORS policy ---
    # cors_origins: list of allowed origins for CORSMiddleware.
    # Default is loopback-only (localhost / 127.0.0.1) to prevent cross-site
    # requests from arbitrary web pages.
    # Reference: OWASP CORS cheat sheet; DESIGN.md §10.18 (v0.44.0)
    cors_origins: list[str] = field(default_factory=lambda: [
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:8000",
    ])
    # --- Checkpoint persistence (SQLite-backed fault-tolerant restart) ---
    # checkpoint_enabled: when True, task queue and workflow state are saved
    #   to a SQLite database after each enqueue/dequeue operation.
    # checkpoint_db: path to the SQLite checkpoint database file.
    #   Supports ~ expansion.  Defaults to ~/.tmux_orchestrator/checkpoint.db
    # References:
    #   LangGraph checkpointer + AsyncSqliteSaver (LangChain docs 2025)
    #   Apache Flink Checkpoints vs Savepoints (Flink stable docs)
    #   Chandy-Lamport distributed snapshots (1985)
    #   DESIGN.md §10.12 (v0.45.0)
    checkpoint_enabled: bool = False
    checkpoint_db: str = "~/.tmux_orchestrator/checkpoint.db"
    # --- OpenTelemetry tracing (GenAI Semantic Conventions) ---
    # telemetry_enabled: when True, the orchestrator creates OTel spans for
    #   agent invocations and task-queued events using the GenAI semconv.
    # otlp_endpoint: OTLP/gRPC endpoint for span export (e.g. "http://localhost:4317").
    #   Empty string = ConsoleSpanExporter (stdout JSON).
    #   Also overridden by OTEL_EXPORTER_OTLP_ENDPOINT environment variable.
    # References:
    #   OTel GenAI Semantic Conventions https://opentelemetry.io/docs/specs/semconv/gen-ai/
    #   opentelemetry-python SDK https://opentelemetry-python.readthedocs.io/
    #   DESIGN.md §10.14 (v0.47.0)
    telemetry_enabled: bool = False
    otlp_endpoint: str = ""
    # --- Mailbox auto-cleanup on stop ---
    # mailbox_cleanup_on_stop: when True (default), Orchestrator.stop() deletes the
    #   session-scoped mailbox directory ({mailbox_dir}/{session_name}/) after all
    #   agents have been stopped.  Set to False to retain mailbox files across
    #   restarts (useful for debugging or post-mortem inspection of messages).
    #
    # Rationale: mailbox directories are session-scoped (transient queues pattern —
    # RabbitMQ docs 2025); they accumulate stale messages across successive demo runs
    # and reduce test reproducibility.  Deleting them at stop() mirrors the "transient
    # queue" lifecycle: created on first message, deleted on consumer disconnect.
    #
    # Implementation uses shutil.rmtree(..., ignore_errors=True) so that missing or
    # partially-written directories are silently skipped (safe for idempotent shutdown).
    #
    # References:
    #   RabbitMQ "Queues" (https://www.rabbitmq.com/docs/queues, 2025) — transient queue pattern
    #   Python docs "tempfile" — TemporaryDirectory cleanup (https://docs.python.org/3/library/tempfile.html)
    #   Designing for Graceful Shutdown (https://medium.com/@jusuftopic, 2025)
    #   DESIGN.md §10.66 (v1.1.34)
    mailbox_cleanup_on_stop: bool = True
    # --- Scratchpad file persistence (write-through cache) ---
    # scratchpad_dir: directory where scratchpad key files are written.
    #   Layout: {scratchpad_dir}/{key}  (one file per key, flat directory)
    #   File format: JSON-serialised value (human-readable via cat).
    #   Default is a project-relative path ".orchestrator/scratchpad", resolved
    #   at load_config() time against the server cwd (consistent with mailbox_dir).
    #   Absolute paths (including ~ expansions) are used as-is.
    #
    # Rationale: agents use the scratchpad for pipeline handoffs (one agent writes,
    #   another reads).  Without persistence these handoffs are lost on server restart,
    #   breaking resume-after-restart scenarios.  Write-through to files provides a
    #   human-inspectable view and free persistence with no extra dependencies.
    #
    # References:
    #   ActiveState Recipe 579097 — atomic file write (write + rename pattern)
    #   simplekv FilesystemStore — one file per key design
    #   DESIGN.md §10.77 (v1.2.1)
    scratchpad_dir: str = ".orchestrator/scratchpad"
    # --- Worktree root override ---
    # repo_root: when set, the WorktreeManager uses this path (rather than Path.cwd())
    #   as the base for git worktree operations.  Useful when the server is launched
    #   from a directory that is not the target git repository (e.g. demo scripts that
    #   run from ~/Demonstration/vX.Y.Z/).  Must point to a directory that contains
    #   (or is an ancestor of) a .git directory.
    #
    #   In YAML: ``repo_root: /home/user/Projects/MyRepo``
    #   Programmatically: OrchestratorConfig(repo_root=Path("/home/user/Projects/MyRepo"))
    #
    #   When None (default), the factory falls back to Path.cwd() — preserving the
    #   pre-v1.0.0 behaviour for callers that launch the server from the repo root.
    #
    # Reference: DESIGN.md §10.17 (v1.0.0 — worktree cwd bug fix)
    repo_root: "Path | None" = None
    # --- Agent auto-restart supervision (v1.2.12) ---
    # supervision_enabled: global kill-switch for agent auto-restart.
    #   When False, _restart_agent() is a no-op regardless of per-agent
    #   max_consecutive_failures settings.  Use this to disable auto-restart
    #   across all agents without modifying individual AgentConfig entries.
    #
    # Reference: Erlang OTP supervisor behaviour — intensity/period limit;
    #   DESIGN.md §10.88 (v1.2.12)
    supervision_enabled: bool = True
    # --- Task timeout escalation (v1.2.13) ---
    # task_escalation_enabled: when True (default), a task that is timed out by the
    #   watchdog is re-queued instead of immediately failed.  The re-queued task has
    #   its priority bumped (lower number = higher urgency) and the agent that timed
    #   it out is added to excluded_agents so it is not dispatched there again.
    #   After max_task_escalations escalations the task is finally dead-lettered.
    #   Set to False to restore the pre-v1.2.13 behaviour (immediate failure on
    #   watchdog_timeout).
    #
    # max_task_escalations: maximum number of times a single task may be re-queued
    #   due to timeout before being permanently failed.  Default 2.
    #
    # Reference: GitGuardian "Celery Task Resilience" (2024) — escalating retry;
    #   Temporal WorkflowTaskTimeout reassignment (2024) — avoid stuck worker;
    #   Wikipedia "Aging (scheduling)" — priority bump on re-queue;
    #   AWS Builders Library "Timeouts, retries and backoff with jitter" (2022);
    #   DESIGN.md §10.89 (v1.2.13)
    task_escalation_enabled: bool = True
    max_task_escalations: int = 2
    # --- Workflow branch cleanup on completion ---
    # workflow_branch_cleanup: when True (default), the orchestrator automatically
    #   deletes worktree branches accumulated by ephemeral agents once the workflow
    #   reaches "complete" or "failed" status.
    #
    # Background: ephemeral chain_branch agents use keep_branch_on_stop=True so
    #   that successor phases can branch from their committed state (v1.2.6).
    #   After the workflow completes, these branches are no longer needed and
    #   accumulate indefinitely without this cleanup.
    #
    # Set workflow_branch_cleanup=False to preserve branches for post-mortem
    #   inspection (debugging, audit trails, etc.).
    #
    # Reference: DESIGN.md §10.84 (v1.2.8)
    # Research: jessfraz/branch-cleanup-action (github.com, 2025);
    #   Jenkins Multibranch Pipeline stale branch hygiene (pankajaswal, 2025).
    workflow_branch_cleanup: bool = True

    def __post_init__(self) -> None:
        """Validate cross-field constraints after dataclass initialisation.

        Invariants enforced:
        - When ``watchdog_poll < task_timeout`` (i.e. the watchdog is intended
          to be active), ``watchdog_poll`` must be <= ``task_timeout / 3`` so
          the watchdog has at least 3 chances to fire within a task's lifetime.
          If the ratio is violated the watchdog checks too infrequently,
          causing tasks to time out far later than the configured
          ``task_timeout``.
        - When ``watchdog_poll >= task_timeout`` the watchdog is effectively
          disabled (no task can complete a watchdog cycle before timing out),
          which is a valid test/development configuration — no error is raised.

        Fail-Fast principle: Netflix "Principles of Chaos Engineering" (2016)
        recommends surfacing configuration errors at startup rather than during
        operation.  Raising ``ValueError`` here ensures the process exits with
        a clear message instead of silently misbehaving.

        Reference: DESIGN.md §10.33 (v1.0.33 — watchdog_poll validator)
        """
        # task_timeout=0 is meaningless (every task would time out immediately).
        if self.task_timeout <= 0:
            raise ValueError(
                f"task_timeout must be > 0 (got {self.task_timeout}s)."
            )
        # Only enforce the ratio when the watchdog is intended to be active.
        if self.watchdog_poll < self.task_timeout:
            max_watchdog_poll = self.task_timeout / 3
            if self.watchdog_poll > max_watchdog_poll:
                raise ValueError(
                    f"watchdog_poll ({self.watchdog_poll}s) must be <= "
                    f"task_timeout / 3 ({max_watchdog_poll:.1f}s); "
                    f"task_timeout={self.task_timeout}s. "
                    "Increase task_timeout or decrease watchdog_poll."
                )


def _resolve_dir(raw: str, cwd: Path) -> str:
    """Resolve a path string to an absolute string.

    Resolution rules (applied in order):
    1. If *raw* starts with ``~``, apply ``Path.expanduser()`` — the home-relative
       path is used as-is (unchanged by *cwd*).
    2. If *raw* is already absolute, return it unchanged.
    3. Otherwise, join *cwd* and *raw* and return the absolute string.

    This allows legacy ``~/.tmux_orchestrator`` values to keep working while
    new relative defaults (``.orchestrator/mailbox``) are resolved against the
    server's working directory, isolating mailbox data per project.

    Reference: DESIGN.md §10.62 (v1.1.30 — project-scoped mailbox directory)
    """
    p = Path(raw)
    if str(raw).startswith("~"):
        return str(p.expanduser())
    if p.is_absolute():
        return str(p)
    return str(cwd / p)


def load_config(path: str | Path, cwd: Path | str | None = None) -> OrchestratorConfig:
    """Load and validate an orchestrator config from a YAML file.

    Parameters
    ----------
    path:
        Path to the YAML config file.
    cwd:
        Base directory used to resolve relative path values (``mailbox_dir``,
        ``result_store_dir``, ``checkpoint_db``).  When *None*, defaults to
        ``Path.cwd()`` at call time — matching the server's working directory.

    """
    import os  # noqa: PLC0415
    effective_cwd = Path(cwd) if cwd is not None else Path.cwd()

    data = yaml.safe_load(Path(path).read_text())

    agents = [
        AgentConfig(
            id=a["id"],
            type=a["type"],
            isolate=a.get("isolate", True),
            role=AgentRole(a.get("role", "worker")),
            task_timeout=a.get("task_timeout"),
            command=a.get("command"),
            system_prompt=a.get("system_prompt"),
            system_prompt_file=a.get("system_prompt_file"),
            context_files=a.get("context_files", []),
            context_spec_files=a.get("context_spec_files", []),
            spec_files=a.get("spec_files", []),
            tags=a.get("tags", []),
            groups=a.get("groups", []),
            merge_on_stop=a.get("merge_on_stop", False),
            merge_target=a.get("merge_target"),
            cleanup_subdir=a.get("cleanup_subdir", True),
            max_consecutive_failures=a.get("max_consecutive_failures", 3),
        )
        for a in data.get("agents", [])
    ]

    p2p = [tuple(pair) for pair in data.get("p2p_permissions", [])]

    webhooks = [
        WebhookConfig(
            # Expand ${ENV_VAR} / $ENV_VAR patterns in url and secret at load time.
            # os.path.expandvars leaves undefined variables unchanged.
            # Reference: AWS Builders Library "Timeouts, retries and backoff with jitter";
            # DESIGN.md §10.N (v1.0.22 — env var expansion in YAML webhook config)
            url=os.path.expandvars(w["url"]),
            events=w.get("events", []),
            secret=os.path.expandvars(w["secret"]) if w.get("secret") else None,
            max_retries=int(w.get("max_retries", 3)),
            retry_backoff_base=float(w.get("retry_backoff_base", 1.0)),
        )
        for w in data.get("webhooks", [])
    ]

    return OrchestratorConfig(
        session_name=data.get("session_name", "orchestrator"),
        agents=agents,
        p2p_permissions=p2p,  # type: ignore[arg-type]
        task_timeout=data.get("task_timeout", 120),
        mailbox_dir=_resolve_dir(
            data.get("mailbox_dir", ".orchestrator/mailbox"), effective_cwd
        ),
        web_base_url=data.get("web_base_url", "http://localhost:8000"),
        circuit_breaker_threshold=data.get("circuit_breaker_threshold", 3),
        circuit_breaker_recovery=data.get("circuit_breaker_recovery", 60.0),
        dlq_max_retries=data.get("dlq_max_retries", 50),
        watchdog_poll=data.get("watchdog_poll", 30.0),
        recovery_attempts=data.get("recovery_attempts", 3),
        recovery_backoff_base=data.get("recovery_backoff_base", 5.0),
        recovery_poll=data.get("recovery_poll", 2.0),
        rate_limit_rps=data.get("rate_limit_rps", 0.0),
        rate_limit_burst=data.get("rate_limit_burst", 0),
        context_window_tokens=data.get("context_window_tokens", 200_000),
        context_warn_threshold=data.get("context_warn_threshold", 0.75),
        context_auto_summarize=data.get("context_auto_summarize", False),
        context_auto_compress=data.get("context_auto_compress", False),
        context_compress_drop_percentile=data.get("context_compress_drop_percentile", 0.40),
        context_monitor_poll=data.get("context_monitor_poll", 5.0),
        drift_threshold=data.get("drift_threshold", 0.6),
        drift_idle_threshold=data.get("drift_idle_threshold", 300.0),
        drift_monitor_poll=data.get("drift_monitor_poll", 10.0),
        drift_rebrief_enabled=data.get("drift_rebrief_enabled", True),
        drift_rebrief_cooldown=data.get("drift_rebrief_cooldown", 60.0),
        drift_rebrief_message=data.get(
            "drift_rebrief_message",
            (
                "⚠ ROLE REMINDER from orchestrator: You appear to be drifting from your "
                "assigned role. Please refocus on your task."
            ),
        ),
        autoscale_min=data.get("autoscale_min", 0),
        autoscale_max=data.get("autoscale_max", 0),
        autoscale_threshold=data.get("autoscale_threshold", 3),
        autoscale_cooldown=data.get("autoscale_cooldown", 30.0),
        autoscale_poll=data.get("autoscale_poll", 5.0),
        autoscale_agent_tags=data.get("autoscale_agent_tags", []),
        autoscale_system_prompt=data.get("autoscale_system_prompt"),
        result_store_enabled=data.get("result_store_enabled", False),
        result_store_dir=_resolve_dir(
            data.get("result_store_dir", "~/.tmux_orchestrator/results"), effective_cwd
        ),
        groups=data.get("groups", []),
        webhook_timeout=data.get("webhook_timeout", 5.0),
        webhooks=webhooks,
        default_task_ttl=data.get("default_task_ttl"),
        ttl_reaper_poll=data.get("ttl_reaper_poll", 1.0),
        cors_origins=data.get("cors_origins", [
            "http://localhost",
            "http://localhost:8000",
            "http://127.0.0.1",
            "http://127.0.0.1:8000",
        ]),
        checkpoint_enabled=data.get("checkpoint_enabled", False),
        checkpoint_db=_resolve_dir(
            data.get("checkpoint_db", "~/.tmux_orchestrator/checkpoint.db"), effective_cwd
        ),
        telemetry_enabled=data.get("telemetry_enabled", False),
        otlp_endpoint=data.get("otlp_endpoint", ""),
        memory_auto_record=data.get("memory_auto_record", True),
        memory_inject_count=data.get("memory_inject_count", 5),
        mailbox_cleanup_on_stop=data.get("mailbox_cleanup_on_stop", True),
        scratchpad_dir=_resolve_dir(
            data.get("scratchpad_dir", ".orchestrator/scratchpad"), effective_cwd
        ),
        repo_root=Path(data["repo_root"]).expanduser().resolve() if data.get("repo_root") else None,
        supervision_enabled=data.get("supervision_enabled", True),
        workflow_branch_cleanup=data.get("workflow_branch_cleanup", True),
        task_escalation_enabled=data.get("task_escalation_enabled", True),
        max_task_escalations=data.get("max_task_escalations", 2),
    )
