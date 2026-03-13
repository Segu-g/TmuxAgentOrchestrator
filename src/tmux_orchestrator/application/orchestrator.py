"""Central orchestrator: task queue, agent lifecycle, dispatch, and P2P routing."""

from __future__ import annotations

import asyncio
import heapq
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.application.infra_protocols import (  # noqa: F401 (re-exported)
    AutoScalerProtocol,
    CheckpointStoreProtocol,
    NullAutoScaler,
    NullCheckpointStore,
    NullResultStore,
    ResultStoreProtocol,
)
from tmux_orchestrator.application.monitor_protocols import (  # noqa: F401 (re-exported)
    ContextMonitorProtocol,
    DriftMonitorProtocol,
    NullContextMonitor,
    NullDriftMonitor,
)
from tmux_orchestrator.application.bus import Bus, Message, MessageType
from tmux_orchestrator.application.group_manager import GroupManager
from tmux_orchestrator.application.rate_limiter import RateLimitExceeded, TokenBucketRateLimiter
from tmux_orchestrator.application.registry import AgentRegistry
from tmux_orchestrator.application.supervision import supervised_task
from tmux_orchestrator.application.task_queue import AsyncPriorityTaskQueue, TaskQueue
from tmux_orchestrator.context_monitor import ContextMonitor
from tmux_orchestrator.drift_monitor import DriftMonitor
from tmux_orchestrator.messaging import Mailbox
from tmux_orchestrator.webhook_manager import WebhookManager

if TYPE_CHECKING:
    from tmux_orchestrator.application.config import AgentConfig, OrchestratorConfig
    from tmux_orchestrator.application.workflow_manager import WorkflowManager, WorkflowRun
    from tmux_orchestrator.tmux_interface import TmuxInterface
    from tmux_orchestrator.worktree import WorktreeManager

logger = logging.getLogger(__name__)

# ContextMonitorProtocol, DriftMonitorProtocol, NullContextMonitor, NullDriftMonitor
# are now defined in tmux_orchestrator.application.monitor_protocols and imported above.
# They are re-exported from this module for backward compatibility.
# DESIGN.md §10.N (v1.0.15 — application/ layer extraction, Strangler Fig pattern).


@dataclass
class BroadcastGroup:
    """Tracks a fan-out broadcast of the same task to multiple agents.

    Used by ``Orchestrator.broadcast_task()`` and polled via
    ``GET /tasks/broadcast/{id}``.

    Design references:
    - Fan-out / Fan-in concurrency pattern — distribute, collect, cancel losers.
    - Sakana AI ALE-Agent AHC058 — parallel trial-and-error with best-of-N selection.
    - Go context.Context cancellation — abort remaining workers after first result.
    - DESIGN.md §10.91 (v1.2.15)
    """

    broadcast_id: str
    mode: str  # "race" | "gather"
    task_ids: list[str] = field(default_factory=list)
    agent_ids: list[str] = field(default_factory=list)
    completed_tasks: dict[str, str] = field(default_factory=dict)  # task_id → result
    failed_tasks: set[str] = field(default_factory=set)
    cancelled: bool = False
    winner_task_id: str | None = None
    status: str = "pending"  # pending | running | complete | failed


@dataclass
class BroadcastResult:
    """Returned by ``broadcast_task()`` after submitting N tasks.

    Design reference: DESIGN.md §10.91 (v1.2.15)
    """

    broadcast_id: str
    mode: str
    task_ids: list[str]
    agent_ids: list[str]


class Orchestrator:
    """Manages the full agent lifecycle and routes all messages.

    Responsibilities:
    - Maintain a priority task queue.
    - Delegate agent-state management to ``AgentRegistry``.
    - Dispatch tasks to idle agents.
    - Gate peer-to-peer messages via the registry's permission table.
    - Forward bus events to any attached observers (TUI, web hub).
    """

    def __init__(
        self,
        bus: Bus,
        tmux: "TmuxInterface",
        config: "OrchestratorConfig",
        worktree_manager: "WorktreeManager | None" = None,
        task_queue: "TaskQueue | None" = None,
        context_monitor: "ContextMonitorProtocol | None" = None,
        drift_monitor: "DriftMonitorProtocol | None" = None,
        webhook_manager: "WebhookManager | None" = None,
        result_store: "ResultStoreProtocol | None" = None,
        checkpoint_store: "CheckpointStoreProtocol | None" = None,
        autoscaler: "AutoScalerProtocol | None" = None,
        workflow_manager: "WorkflowManager | None" = None,
        group_manager: "GroupManager | None" = None,
    ) -> None:
        self.bus = bus
        self.tmux = tmux
        self.config = config
        self._worktree_manager = worktree_manager
        # All agent-related state lives in the registry (DDD Aggregate pattern)
        self.registry = AgentRegistry(
            p2p_permissions=config.p2p_permissions,
            circuit_breaker_threshold=config.circuit_breaker_threshold,
            circuit_breaker_recovery=config.circuit_breaker_recovery,
        )
        # Priority queue: (priority, seq, task) — lower priority first; seq is
        # a monotonically increasing counter that breaks ties between tasks with
        # equal priority so the heap never tries to compare Task objects directly.
        # Without seq, heapq with Task.__lt__(always False for equal-priority items)
        # causes the same task to cycle at the heap root indefinitely.
        # Dependency-injected via task_queue parameter (TaskQueue Protocol);
        # defaults to AsyncPriorityTaskQueue for production use.
        # Reference: task_queue.py, DESIGN.md §11 "orchestrator DI 化".
        self._task_queue: TaskQueue = (
            task_queue
            if task_queue is not None
            else AsyncPriorityTaskQueue(maxsize=config.task_queue_maxsize)
        )
        self._task_seq: int = 0  # monotonically increasing enqueue counter
        self._paused = False
        self._dispatch_task: asyncio.Task | None = None
        self._router_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._recovery_task: asyncio.Task | None = None
        # Per-agent recovery attempt counters (reset on manual restart or stop)
        self._recovery_attempts: dict[str, int] = {}
        # Agents permanently failed (exhausted retries) — excluded from dispatch
        self._permanently_failed: set[str] = set()
        self._bus_queue: asyncio.Queue[Message] | None = None
        # Worker results waiting to be injected into the next Director chat turn
        self._director_pending: list[str] = []
        # Dead letter queue: tasks that could not be dispatched after max retries
        self._dlq: list[dict] = []
        # Set of task IDs that have completed successfully (used for depends_on checks)
        self._completed_tasks: set[str] = set()
        # Set of task IDs that have failed (retries exhausted, no error recovery).
        # Used to cascade dependency_failed to waiting tasks.
        # Reference: GNU Make prerequisite failure; Apache Airflow upstream_failed state.
        # DESIGN.md §10.24 (v0.29.0)
        self._failed_tasks: set[str] = set()
        # Tasks waiting for their depends_on prerequisites to be satisfied.
        # key = task_id, value = Task.  Tasks are moved to the queue when ALL
        # of their depends_on IDs appear in _completed_tasks.
        # Reference: Tomasulo's algorithm register-renaming; Dask task graph;
        # GNU Make dependency resolution; DESIGN.md §10.24 (v0.29.0)
        self._waiting_tasks: dict[str, Task] = {}
        # Reverse lookup: dep_task_id → [waiting_task_ids].
        # Used for O(1) wake-up after a dependency completes or fails.
        # Reference: Apache Spark DAG scheduler; POSIX make prerequisites.
        # DESIGN.md §10.24 (v0.29.0)
        self._task_dependents: dict[str, list[str]] = {}
        # Persistent depends_on snapshot: task_id → list[dependency task_ids].
        # Unlike _task_dependents (cleared as tasks complete), this dict is
        # append-only and never cleaned up — used by GET /workflows/{id}/dag
        # to reconstruct the original topology at query time.
        # Design reference: DESIGN.md §10.90 (v1.2.14)
        self._task_deps: dict[str, list[str]] = {}
        # Persistent task → agent assignment: task_id → agent_id.
        # Populated at dispatch time so GET /workflows/{id}/dag can show which
        # agent ran each task.  Never cleaned up (needed for completed tasks).
        # Design reference: DESIGN.md §10.90 (v1.2.14)
        self._task_agent: dict[str, str] = {}
        # Idempotency deduplication: key → task_id, with expiry timestamps
        _IKEY_TTL = 3600.0
        self._idempotency_keys: dict[str, str] = {}
        self._ikey_timestamps: dict[str, float] = {}
        self._ikey_ttl: float = _IKEY_TTL
        # Result-routing table: task_id → reply_to agent_id.
        # When a RESULT arrives for a task that has a reply_to entry, the
        # orchestrator writes the RESULT to that agent's mailbox and notifies
        # it via notify_stdin.  Implements the request-reply pattern for
        # multi-level hierarchy feedback loops.
        # Reference: "Learning Notes #15 – Request Reply Pattern | RabbitMQ" (2024)
        # Moore, David J. "A Taxonomy of Hierarchical Multi-Agent Systems" (2025)
        self._task_reply_to: dict[str, str] = {}
        # Shared mailbox used for reply_to routing (set by callers via _mailbox).
        # If None, reply_to routing falls back to agent.notify_stdin only (no file write).
        self._mailbox: "Mailbox | None" = None
        # Per-agent task history: agent_id → list of completed task records.
        # Capped at 200 entries per agent.  Records are appended in completion
        # order; get_agent_history() reverses for most-recent-first presentation.
        # Design reference: TAMAS "Beyond Black-Box Benchmarking" arXiv:2503.06745
        self._agent_history: dict[str, list[dict]] = {}
        # Tracks when each agent started its current task (for history duration).
        self._task_started_at: dict[str, float] = {}
        self._task_started_prompt: dict[str, str] = {}
        # Per-task timeout override (seconds), stored for history records.
        # Populated at dispatch time from Task.timeout; popped in _record_agent_history.
        self._task_timeout: dict[str, int] = {}
        # Active task lookup: task_id → Task object, so _route_loop can re-enqueue
        # on failure when task.retry_count < task.max_retries.
        # Reference: AWS SQS maxReceiveCount / Redrive policy; Netflix Hystrix retry;
        # Polly .NET resilience library. DESIGN.md §10.21 (v0.26.0)
        self._active_tasks: dict[str, Task] = {}
        # Tombstone set for cancelled tasks.  Tasks added here are:
        # 1. Skipped by _dispatch_loop when dequeued (queued-cancellation tombstone).
        # 2. Silently discarded by _route_loop when a RESULT arrives for an
        #    in-progress task that was cancelled via cancel_task().
        # Reference: Kubernetes Pod deletion grace period; POSIX SIGTERM/SIGKILL;
        # Go context.Context cancellation; Java Future.cancel(). DESIGN.md §10.22 (v0.27.0)
        self._cancelled_task_ids: set[str] = set()
        # Set of agent IDs that are in "drain" mode — they will not receive new tasks
        # and will be automatically stopped when their current task completes.
        # Reference: Kubernetes Pod terminationGracePeriodSeconds; HAProxy graceful
        # restart; UNIX SO_LINGER graceful socket close; AWS ECS stopTimeout.
        # DESIGN.md §10.23 (v0.28.0)
        self._draining_agents: set[str] = set()
        # Token-bucket rate limiter for task submission backpressure.
        # None → unlimited (default).  Set via set_rate_limiter() or
        # reconfigure_rate_limiter(), or auto-created from config if
        # config.rate_limit_rps > 0.
        # Reference: Tanenbaum "Computer Networks" 5th ed. §5.3 — Token Bucket;
        # DESIGN.md §10.16 (v0.20.0)
        if config.rate_limit_rps > 0:
            burst = config.rate_limit_burst or max(1, int(config.rate_limit_rps * 2))
            self._rate_limiter: TokenBucketRateLimiter | None = TokenBucketRateLimiter(
                rate=config.rate_limit_rps,
                burst=burst,
            )
        else:
            self._rate_limiter = None
        # Context window monitor: tracks pane output size, estimates token count,
        # detects NOTES.md updates, and optionally auto-injects /summarize.
        # Injected via context_monitor parameter (ContextMonitorProtocol); defaults to
        # ContextMonitor for production use.  NullContextMonitor (or any conforming object)
        # can be injected for unit tests that don't require real tmux access.
        # Reference: Liu et al. "Lost in the Middle" TACL 2024; DESIGN.md §11 (v0.21.0)
        # Reference: PEP 544 Structural subtyping; DESIGN.md §10.N (v1.0.14 — orchestrator DI)
        self._context_monitor: ContextMonitorProtocol = (
            context_monitor
            if context_monitor is not None
            else ContextMonitor(
                bus=bus,
                tmux=tmux,
                agents=lambda: list(self.registry.all_agents().values()),
                context_window_tokens=config.context_window_tokens,
                warn_threshold=config.context_warn_threshold,
                auto_summarize=config.context_auto_summarize,
                auto_compress=config.context_auto_compress,
                compress_drop_percentile=config.context_compress_drop_percentile,
                poll_interval=config.context_monitor_poll,
            )
        )
        # Drift monitor — behavioral degradation detection (Agent Stability Index subset).
        # Injected via drift_monitor parameter (DriftMonitorProtocol); defaults to
        # DriftMonitor for production use.  NullDriftMonitor (or any conforming object)
        # can be injected for unit tests that don't require real tmux access.
        # Reference: Rath arXiv:2601.04170 "Agent Drift" (2026); DESIGN.md §10.20 (v1.0.9)
        # Reference: PEP 544 Structural subtyping; DESIGN.md §10.N (v1.0.14 — orchestrator DI)
        self._drift_monitor: DriftMonitorProtocol = (
            drift_monitor
            if drift_monitor is not None
            else DriftMonitor(
                bus=bus,
                tmux=tmux,
                agents=lambda: list(self.registry.all_agents().values()),
                drift_threshold=config.drift_threshold,
                idle_threshold=config.drift_idle_threshold,
                poll_interval=config.drift_monitor_poll,
            )
        )
        # Queue-depth autoscaler — only created when autoscale_max > 0.
        # Injected via autoscaler parameter (AutoScalerProtocol); defaults to
        # AutoScaler for production use.  NullAutoScaler (or any conforming object)
        # can be injected for unit tests.
        # Reference: Kubernetes HPA; Thijssen "Autonomic Computing"; AWS cooldowns.
        # DESIGN.md §10.18 (v0.23.0); §10.35 (v1.0.35 — DI).
        if autoscaler is not None:
            self._autoscaler: "AutoScalerProtocol | None" = autoscaler
        elif config.autoscale_max > 0:
            from tmux_orchestrator.autoscaler import AutoScaler
            self._autoscaler = AutoScaler(self, config)
        else:
            self._autoscaler = None
        # Append-only JSONL result store — Event Sourcing pattern.
        # Injected via result_store parameter (ResultStoreProtocol); defaults to
        # ResultStore when config.result_store_enabled=True.  NullResultStore can be
        # injected for tests to avoid unexpected I/O.
        # Reference: Fowler "Event Sourcing" (2005); Young CQRS (2010);
        # Hickey "The Value of Values" (Datomic, 2012). DESIGN.md §10.19 (v0.24.0);
        # §10.35 (v1.0.35 — DI).
        if result_store is not None:
            self._result_store: "ResultStoreProtocol | None" = result_store
        elif config.result_store_enabled:
            from tmux_orchestrator.result_store import ResultStore
            self._result_store = ResultStore(
                store_dir=config.result_store_dir,
                session_name=config.session_name,
            )
        else:
            self._result_store = None
        # Workflow DAG tracker — always enabled (zero overhead when no workflows
        # are submitted).  Injected via workflow_manager parameter; defaults to
        # WorkflowManager().
        # Reference: Apache Airflow DAG model; Tomasulo's algorithm (IBM 1967);
        # AWS Step Functions; Prefect "Modern Data Stack". DESIGN.md §10.20 (v0.25.0);
        # §10.35 (v1.0.35 — DI).
        if workflow_manager is not None:
            self._workflow_manager: "WorkflowManager" = workflow_manager
        else:
            from tmux_orchestrator.workflow_manager import WorkflowManager  # noqa: PLC0415
            self._workflow_manager = WorkflowManager()
        # Outbound webhook notification manager.
        # Fire-and-forget delivery of task/agent/workflow events to registered URLs.
        # Injected via webhook_manager parameter; defaults to WebhookManager for
        # production use.  Pass a custom instance for testing or alternate backends.
        # Reference: GitHub Webhooks; Stripe Webhooks; RFC 2104 HMAC;
        # Zalando RESTful API Guidelines §webhook. DESIGN.md §10.25 (v0.30.0),
        # §10.34 (v1.0.34 — WebhookManager DI).
        self._webhook_manager: WebhookManager = (
            webhook_manager
            if webhook_manager is not None
            else WebhookManager(timeout=config.webhook_timeout)
        )
        # Register static webhooks defined in the YAML config at startup.
        # Dynamic webhooks may also be added at runtime via POST /webhooks.
        # Reference: DESIGN.md §10.N (v1.0.21 — static webhook config)
        for wh_cfg in config.webhooks:
            self._webhook_manager.register(
                url=wh_cfg.url,
                events=wh_cfg.events,
                secret=wh_cfg.secret,
                max_retries=wh_cfg.max_retries,
                retry_backoff_base=wh_cfg.retry_backoff_base,
            )
        # Checkpoint store — SQLite-backed fault-tolerant persistence.
        # Injected via checkpoint_store parameter (CheckpointStoreProtocol); defaults
        # to CheckpointStore when config.checkpoint_enabled=True.  NullCheckpointStore
        # can be injected for tests to avoid SQLite file creation.
        # Note: when injected, initialize() and save_meta("session_name") are NOT called
        # automatically — the caller is responsible for initialisation.
        # Reference: LangGraph checkpointer pattern (LangChain docs 2025);
        # Apache Flink Checkpoints; Chandy-Lamport (1985).
        # DESIGN.md §10.12 (v0.45.0); §10.35 (v1.0.35 — DI).
        if checkpoint_store is not None:
            self._checkpoint_store: "CheckpointStoreProtocol | None" = checkpoint_store
        elif config.checkpoint_enabled:
            from tmux_orchestrator.checkpoint_store import CheckpointStore
            _cp = CheckpointStore(db_path=config.checkpoint_db)
            _cp.initialize()
            _cp.save_meta("session_name", config.session_name)
            self._checkpoint_store = _cp
        else:
            self._checkpoint_store = None
        # OpenTelemetry tracing — GenAI Semantic Conventions.
        # When telemetry_enabled=True, agent invocations and task-queued events
        # are wrapped in OTel spans with gen_ai.* attributes so that trace data
        # can be sent to Jaeger, Datadog, or any OTLP-compatible backend.
        # Reference: OTel GenAI Semantic Conventions
        #   https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/
        # DESIGN.md §10.14 (v0.47.0)
        if config.telemetry_enabled:
            import os
            from tmux_orchestrator.telemetry import TelemetrySetup
            _prev = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            if config.otlp_endpoint and not _prev:
                os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = config.otlp_endpoint
            self._telemetry: "TelemetrySetup | None" = TelemetrySetup.from_env(
                service_name="tmux_orchestrator"
            )
            if config.otlp_endpoint and not _prev:
                # Restore env so we don't permanently pollute the process environment
                del os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]
        else:
            self._telemetry = None
        # Named agent group manager — logical pools for targeted task dispatch.
        # Groups allow tasks to target a named pool instead of individual agent IDs or tags.
        # Injected via group_manager parameter; defaults to GroupManager().
        # References:
        #   Kubernetes Node Pools / Node Groups; AWS Auto Scaling Groups;
        #   Apache Mesos Roles; HashiCorp Nomad Task Groups.
        # DESIGN.md §10.26 (v0.31.0); §10.35 (v1.0.35 — DI).
        self._group_manager: GroupManager = (
            group_manager if group_manager is not None else GroupManager()
        )
        # Load groups from config
        for grp in config.groups:
            name = grp.get("name", "")
            agent_ids = grp.get("agent_ids", [])
            if name:
                self._group_manager.create(name, agent_ids)
        # Priority tracking for inheritance: task_id → priority at submission time.
        # Populated for ALL tasks at submit_task() time.  Used to determine the
        # effective priority of dependent tasks when inherit_priority=True.
        # Only looks one level up (direct depends_on parents, not transitive closure).
        # Reference: Liu & Layland "Scheduling Algorithms for Multiprogramming in a
        # Hard Real-Time Environment" JACM (1973);
        # Apache Airflow priority_weight upstream/downstream rules (2024);
        # Sha, Rajkumar, Lehoczky "Priority Inheritance Protocols" IEEE (1990);
        # DESIGN.md §10.27 (v0.32.0)
        self._task_priorities: dict[str, int] = {}
        # TTL reaper asyncio.Task — started in start(), cancelled in stop().
        # Scans _waiting_tasks every ttl_reaper_poll seconds and expires entries
        # whose expires_at has elapsed.  The dispatch loop handles expiry for
        # tasks that are dequeued before being dispatched.
        # Reference: RabbitMQ "Time-To-Live and Expiration" docs;
        # Azure Service Bus message expiration (Microsoft Docs 2024);
        # DESIGN.md §10.28 (v0.33.0)
        self._ttl_reaper_task: asyncio.Task | None = None
        # Background agent-startup tasks created by start_agents().  Tracked
        # so that stop() can cancel any that haven't finished yet.
        self._agent_startup_tasks: list[asyncio.Task] = []
        # When True, start() does NOT call start_agents() inline — the caller
        # (web lifespan) is responsible for calling start_agents() after the
        # HTTP server is up so that SessionStart hooks can reach the server.
        self._defer_agent_start: bool = False
        # Episodic memory store — MIRIX-inspired auto-record + auto-inject (v1.0.29).
        # Set by create_app() after the store is constructed so that both the
        # web layer and the orchestrator dispatch loop share the same store instance.
        # When None, auto-record and auto-inject are silently skipped.
        # Reference: Wang & Chen "MIRIX" arXiv:2507.07957 (2025);
        # DESIGN.md §10.29 (v1.0.29)
        self._episode_store: "Any | None" = None
        # Drift auto re-brief state (v1.1.18).
        # _drift_rebrief_history: agent_id → list of {timestamp, drift_score} dicts
        # _drift_rebrief_last_sent: agent_id → monotonic time of last re-brief
        # Reference: Rath arXiv:2601.04170 "drift-aware routing" re-brief pattern;
        # arXiv:2603.03258 "goal reminder injection" for drift prevention.
        # DESIGN.md §10.50 (v1.1.18)
        self._drift_rebrief_history: dict[str, list[dict]] = {}
        self._drift_rebrief_last_sent: dict[str, float] = {}
        # Ephemeral agent tracking — IDs of agents spawned for a single phase.
        # After their task completes, they are auto-stopped and unregistered.
        # Lifecycle mirrors _draining_agents but triggered by agent_template at
        # phase dispatch time rather than by manual drain_agent() calls.
        # Design reference: DESIGN.md §10.79 (v1.2.3) — PhaseSpec.agent_template.
        # Research: Kubernetes Pod-per-Job pattern; ephemeral CI agent lifecycle.
        self._ephemeral_agents: set[str] = set()
        # Branch tracking for ephemeral agents — maps agent_id → branch_name
        # (e.g. "worker-ephemeral-abc12345" → "worktree/worker-ephemeral-abc12345").
        # Populated in spawn_ephemeral_agent() after the agent starts.
        # Consumed by the workflow router (immediate spawning) and _route_loop
        # (deferred spawning) when chain_branch=True is set on the next phase.
        # Design reference: DESIGN.md §10.80 (v1.2.4)
        self._ephemeral_agent_branches: dict[str, str] = {}
        # Task → ephemeral agent mapping (v1.2.5): maps global task_id → the
        # ephemeral agent_id that ran (or is running) that task.  Populated at
        # dispatch time in _route_loop for deferred chain_branch spawns.  Used
        # by successor phases to resolve which branch to chain from.
        # Design reference: DESIGN.md §10.81
        self._task_ephemeral_agent: dict[str, str] = {}
        # Workflow → branch list (v1.2.8): maps workflow_id → ordered list of
        # "worktree/{agent_id}" branch names for ephemeral agents spawned during
        # that workflow.  Populated in spawn_ephemeral_agent() when workflow_id
        # is provided.  Consumed by cleanup_workflow_branches() after workflow
        # reaches terminal state.  Branches are appended in spawn order so the
        # LAST branch is the final phase's accumulated state.
        # Design reference: DESIGN.md §10.84 (v1.2.8)
        self._workflow_branches: dict[str, list[str]] = {}
        # Task → workflow mapping (v1.2.8): task_id → workflow_id.
        # Populated by the workflow router AFTER run.id is known so that
        # _route_loop can pass workflow_id to spawn_ephemeral_agent() for
        # deferred chain_branch spawns (tasks already enqueued before run.id
        # was available).
        # Design reference: DESIGN.md §10.84
        self._task_workflow_id: dict[str, str] = {}
        # Per-agent consecutive failure counters (v1.2.12 — auto-restart).
        # Incremented each time a task for that agent reaches final failure (all
        # retries exhausted).  Reset to 0 on any successful task completion.
        # When counter >= AgentConfig.max_consecutive_failures (and > 0), the
        # orchestrator calls _restart_agent() to stop and recreate the agent.
        # Ephemeral agents are excluded — they are single-use and never restarted.
        # Reference: Erlang OTP one_for_one supervisor strategy (Ericsson 1996);
        # DESIGN.md §10.88 (v1.2.12)
        self._consecutive_failures: dict[str, int] = {}
        # Cumulative restart count per agent (v1.2.12).
        # Incremented in _restart_agent().  Exposed via GET /agents/{id}/stats.
        # Design reference: DESIGN.md §10.88 (v1.2.12)
        self._restart_counts: dict[str, int] = {}
        # Broadcast group registry (v1.2.15): broadcast_id → BroadcastGroup.
        # Tracks fan-out broadcast operations submitted via broadcast_task().
        # Design reference: DESIGN.md §10.91 (v1.2.15)
        self._broadcast_groups: dict[str, BroadcastGroup] = {}
        # Reverse lookup: task_id → broadcast_id (v1.2.15).
        # Populated at broadcast_task() time; used by _route_loop for O(1) lookup.
        # Design reference: DESIGN.md §10.91 (v1.2.15)
        self._task_to_broadcast: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, *, resume: bool = False) -> None:
        """Start all registered agents and the dispatch / routing loops.

        Parameters
        ----------
        resume:
            When ``True`` and ``checkpoint_enabled`` is set in config, reload
            persisted task queue and workflow state from the checkpoint store
            before starting the dispatch loop.  Tasks are re-enqueued in their
            original priority order.  This allows recovery from an unclean
            shutdown without losing queued work.

            Reference: LangGraph checkpointer resume pattern (LangChain 2025);
            Apache Flink savepoint restore (Flink stable docs).
            DESIGN.md §10.12 (v0.45.0).
        """
        self._bus_queue = await self.bus.subscribe(
            "__orchestrator__", broadcast=True
        )
        if resume and self._checkpoint_store is not None:
            await self._resume_from_checkpoint()

        # Prune stale worktree metadata before spawning agents so that a prior
        # unclean shutdown cannot cause `git worktree add` to fail with a
        # name-collision error.  Reference: DESIGN.md §10.40 (v1.1.4).
        if self._worktree_manager is not None:
            self._worktree_manager.prune_stale()

        if self._defer_agent_start:
            # Web mode: dispatch loop starts FIRST so it is ready when agents
            # become IDLE asynchronously.  Agent processes start later (via the
            # web lifespan background task) once the server is accepting requests,
            # allowing the SessionStart hook to call POST /agents/{id}/ready.
            self._start_background_tasks()
        else:
            # TUI / test mode: agents start BEFORE the dispatch loop is created.
            # This preserves the original ordering so tests can call pause()
            # between orchestrator.start() and submit_task() without the
            # dispatch loop having already begun a cycle.
            await self.start_agents()
            self._start_background_tasks()
        logger.info("Orchestrator started with %d agents", len(self.registry.all_agents()))

    def _start_background_tasks(self) -> None:
        """Create the dispatch, route, watchdog, recovery, TTL-reaper tasks."""
        self._dispatch_task = asyncio.create_task(
            supervised_task(self._dispatch_loop, "orchestrator-dispatch",
                            on_permanent_failure=self._on_internal_failure),
            name="orchestrator-dispatch",
        )
        self._router_task = asyncio.create_task(
            supervised_task(self._route_loop, "orchestrator-router",
                            on_permanent_failure=self._on_internal_failure),
            name="orchestrator-router",
        )
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(poll=self.config.watchdog_poll),
            name="orchestrator-watchdog",
        )
        self._recovery_task = asyncio.create_task(
            self._recovery_loop(
                poll=self.config.recovery_poll,
                backoff_base=self.config.recovery_backoff_base,
                max_attempts=self.config.recovery_attempts,
            ),
            name="orchestrator-recovery",
        )
        self._context_monitor.start()
        self._drift_monitor.start()
        if self._autoscaler is not None:
            self._autoscaler.start()
        self._ttl_reaper_task = asyncio.create_task(
            self._ttl_reaper_loop(poll=self.config.ttl_reaper_poll),
            name="orchestrator-ttl-reaper",
        )

    async def start_agents(self) -> None:
        """Start all registered agent processes.

        Called either directly from ``start()`` (TUI / test mode) or as a
        background task from the web lifespan AFTER the HTTP server is
        accepting requests, so that the SessionStart hook can call
        ``POST /agents/{id}/ready`` without a deadlock.

        Background agent startup tasks are tracked in ``_agent_startup_tasks``
        so that ``stop()`` can cancel any still in flight.

        When called from the web lifespan (``_defer_agent_start=True``), agents
        are started as fire-and-forget asyncio tasks so the caller can return
        immediately (the server is already accepting requests and agents will
        call ``POST /agents/{id}/ready`` when their claude session starts).

        When called directly from ``start()`` (TUI / test mode,
        ``_defer_agent_start=False``), agents are started concurrently via
        ``asyncio.gather``, which blocks until all are ready — preserving
        backward-compatible synchronous behaviour for tests and the TUI.
        """
        agents = list(self.registry.all_agents().values())
        if self._defer_agent_start:
            # Web mode: fire-and-forget; server is already up
            for agent in agents:
                task = asyncio.create_task(agent.start(), name=f"{agent.id}-startup")
                self._agent_startup_tasks.append(task)
        else:
            # TUI / test mode: start agents sequentially (preserves original
            # ordering so bus subscriptions and pause/resume tests work correctly)
            for agent in agents:
                await agent.start()

    async def _resume_from_checkpoint(self) -> None:
        """Reload persisted tasks and workflows from the checkpoint store.

        Called by start(resume=True) before the dispatch loop starts.
        Tasks are re-enqueued in priority order.  Waiting tasks are restored
        to _waiting_tasks with their dependency chains.  Workflows are
        re-registered with the WorkflowManager.

        Reference: DESIGN.md §10.12 (v0.45.0).
        """
        assert self._checkpoint_store is not None
        # Restore queued tasks
        pending = self._checkpoint_store.load_pending_tasks()
        for task in pending:
            self._task_seq += 1
            self._task_priorities[task.id] = task.priority
            await self._task_queue.put((task.priority, self._task_seq, task))
            logger.info(
                "Checkpoint restore: re-enqueued task %s (priority=%d)", task.id, task.priority
            )

        # Restore waiting (dependency-blocked) tasks
        waiting = self._checkpoint_store.load_waiting_tasks()
        for task in waiting:
            self._task_priorities[task.id] = task.priority
            self._waiting_tasks[task.id] = task
            for dep in task.depends_on:
                if dep not in self._completed_tasks:
                    self._task_dependents.setdefault(dep, []).append(task.id)
            logger.info(
                "Checkpoint restore: re-loaded waiting task %s", task.id
            )

        # Restore workflows
        workflows = self._checkpoint_store.load_workflows()
        for run in workflows.values():
            self._workflow_manager._runs[run.id] = run
            for tid in run.task_ids:
                self._workflow_manager._task_to_workflow[tid] = run.id
            logger.info(
                "Checkpoint restore: re-loaded workflow %s (%s)", run.id, run.name
            )

        if pending or waiting or workflows:
            logger.info(
                "Checkpoint resume complete: %d queued, %d waiting, %d workflows",
                len(pending), len(waiting), len(workflows),
            )

    async def stop(self) -> None:
        """Stop dispatch, routing, watchdog, context monitor, and all agents."""
        if self._autoscaler is not None:
            self._autoscaler.stop()
        self._context_monitor.stop()
        self._drift_monitor.stop()
        # Cancel any agent-startup tasks that are still in flight.
        for t in self._agent_startup_tasks:
            if not t.done():
                t.cancel()
        if self._agent_startup_tasks:
            await asyncio.gather(*self._agent_startup_tasks, return_exceptions=True)
        self._agent_startup_tasks.clear()
        internal_tasks = [
            t for t in [
                self._dispatch_task, self._router_task,
                self._watchdog_task, self._recovery_task,
                self._ttl_reaper_task,
            ] if t
        ]
        for t in internal_tasks:
            t.cancel()
        if internal_tasks:
            await asyncio.gather(*internal_tasks, return_exceptions=True)
        for agent in list(self.registry.all_agents().values()):
            await agent.stop()
        if self._bus_queue:
            await self.bus.unsubscribe("__orchestrator__")
        # Mailbox auto-cleanup: delete the session-scoped mailbox directory so
        # successive runs do not see stale messages from previous sessions.
        # Guarded by mailbox_cleanup_on_stop (default True); uses ignore_errors=True
        # so a missing or partially-written directory is silently skipped.
        # Reference: DESIGN.md §10.66 (v1.1.34)
        if self.config.mailbox_cleanup_on_stop:
            session_mailbox = Path(self.config.mailbox_dir) / self.config.session_name
            if session_mailbox.exists():
                shutil.rmtree(session_mailbox, ignore_errors=True)
                logger.info(
                    "Orchestrator: cleaned up session mailbox %s", session_mailbox
                )
        logger.info("Orchestrator stopped")

    # ------------------------------------------------------------------
    # Agent registry (thin delegators to AgentRegistry)
    # ------------------------------------------------------------------

    def register_agent(self, agent: Agent, *, parent_id: str | None = None) -> None:
        self.registry.register(agent, parent_id=parent_id)

    def unregister_agent(self, agent_id: str) -> None:
        self.registry.unregister(agent_id)

    def get_agent(self, agent_id: str) -> Agent | None:
        return self.registry.get(agent_id)

    def list_agents(self) -> list[dict]:
        return self.registry.list_all(self.bus.get_drop_counts())

    def get_agent_dict(self, agent_id: str) -> "dict | None":
        """Return a JSON-serialisable dict for a single agent by ID, or ``None``.

        Uses the same dict shape as :meth:`list_agents` for consistency.

        Delegates to :meth:`~AgentRegistry.get_one_dict` for an O(1) direct
        lookup rather than building the full list and scanning it linearly.

        Design reference: DESIGN.md §10.41 (v1.1.5).
        """
        return self.registry.get_one_dict(agent_id, self.bus.get_drop_counts())

    def get_director(self) -> "Agent | None":
        """Return the director agent, or None if no director is registered."""
        return self.registry.get_director()

    def flush_director_pending(self) -> list[str]:
        """Atomically read and clear pending director results."""
        items = self._director_pending.copy()
        self._director_pending.clear()
        return items

    # ------------------------------------------------------------------
    # Queue depth
    # ------------------------------------------------------------------

    def queue_depth(self) -> int:
        """Return the number of tasks currently waiting in the priority queue."""
        return self._task_queue.qsize()

    def queue_size(self) -> int:
        """Alias for :meth:`queue_depth` — returns pending task count.

        Provided for metrics-collector getter injection compatibility.
        Design reference: DESIGN.md §10.92 (v1.2.16)
        """
        return self._task_queue.qsize()

    def get_all_agent_statuses(self) -> dict[str, str]:
        """Return a mapping of ``{agent_id: status_string}`` for all agents.

        Used by the ``MetricsCollector`` getter injection.

        Design reference: DESIGN.md §10.92 (v1.2.16)
        """
        agents = self.list_agents()
        return {a["id"]: a.get("status", "UNKNOWN") for a in agents}

    def get_cumulative_stats(self) -> dict:
        """Return cumulative task completion statistics across all agents.

        Returns a dict with:
        - ``tasks_completed_total``: total tasks completed with status "success"
        - ``tasks_failed_total``: total tasks completed with status "error"
        - ``per_agent``: ``{agent_id: {tasks_completed, tasks_failed, error_rate}}``

        Derived from the in-memory ``_agent_history`` dict (capped at 200 per agent).
        Counts reflect only the entries currently in the ring buffer, which is
        sufficient for trend monitoring over typical agent lifetimes.

        Design reference: DESIGN.md §10.92 (v1.2.16)
        """
        total_completed = 0
        total_failed = 0
        per_agent: dict[str, dict] = {}
        for agent_id, history in self._agent_history.items():
            completed = sum(1 for r in history if r.get("status") == "success")
            failed = sum(1 for r in history if r.get("status") == "error")
            total = completed + failed
            per_agent[agent_id] = {
                "tasks_completed": completed,
                "tasks_failed": failed,
                "error_rate": round(failed / total, 3) if total > 0 else 0.0,
            }
            total_completed += completed
            total_failed += failed
        return {
            "tasks_completed_total": total_completed,
            "tasks_failed_total": total_failed,
            "per_agent": per_agent,
        }

    # ------------------------------------------------------------------
    # Autoscaler
    # ------------------------------------------------------------------

    async def get_autoscaler_status(self) -> dict:
        """Return current autoscaler status, or a disabled-stub when not active."""
        if self._autoscaler is None:
            return {
                "enabled": False,
                "agent_count": 0,
                "queue_depth": self.queue_depth(),
                "last_scale_up": None,
                "last_scale_down": None,
                "autoscaled_ids": [],
                "min": 0,
                "max": 0,
                "threshold": self.config.autoscale_threshold,
                "cooldown": self.config.autoscale_cooldown,
            }
        return await self._autoscaler.status()

    def reconfigure_autoscaler(
        self,
        *,
        min: "int | None" = None,
        max: "int | None" = None,
        threshold: "int | None" = None,
        cooldown: "float | None" = None,
    ) -> dict:
        """Reconfigure autoscaling parameters at runtime.

        Delegates to the injected ``AutoScalerProtocol`` instance.
        Raises ``ValueError`` when autoscaling is not enabled.

        Reference: DESIGN.md §10.35 (v1.0.35 — DI + public API surface).
        """
        if self._autoscaler is None:
            raise ValueError(
                "Autoscaling is not enabled (autoscale_max=0 in config or no injected autoscaler)"
            )
        return self._autoscaler.reconfigure(
            min=min,
            max=max,
            threshold=threshold,
            cooldown=cooldown,
        )

    # ------------------------------------------------------------------
    # Context monitor
    # ------------------------------------------------------------------

    def get_agent_context_stats(self, agent_id: str) -> dict | None:
        """Return context usage stats for *agent_id*, or None if not tracked."""
        return self._context_monitor.get_stats(agent_id)

    def all_agent_context_stats(self) -> list[dict]:
        """Return context usage stats for all tracked agents."""
        return self._context_monitor.all_stats()

    def get_agent_drift_stats(self, agent_id: str) -> dict | None:
        """Return drift stats for *agent_id*, or None if not yet tracked."""
        return self._drift_monitor.get_drift_stats(agent_id)

    def all_agent_drift_stats(self) -> list[dict]:
        """Return drift stats for all tracked agents."""
        return self._drift_monitor.all_drift_stats()

    def get_agent_restart_count(self, agent_id: str) -> int:
        """Return the cumulative restart count for *agent_id* (v1.2.12).

        Returns 0 when the agent has never been restarted by the auto-restart
        mechanism (either because it has not failed enough consecutive times or
        because ``max_consecutive_failures=0`` / ``supervision_enabled=False``).

        Design reference: DESIGN.md §10.88 (v1.2.12)
        """
        return self._restart_counts.get(agent_id, 0)

    def get_agent_drift_rebriefs(self, agent_id: str) -> list[dict]:
        """Return the re-brief history for *agent_id* (most-recent-first).

        Each entry contains ``timestamp`` (ISO-8601 UTC string) and
        ``drift_score`` (float) recorded at the time the re-brief was sent.

        Returns an empty list when no re-briefs have been sent for this agent.

        Reference: DESIGN.md §10.50 (v1.1.18 — drift auto re-brief)
        """
        return list(reversed(self._drift_rebrief_history.get(agent_id, [])))

    def all_drift_rebrief_stats(self) -> list[dict]:
        """Return re-brief histories for all agents that have received at least one.

        Returns a list of ``{agent_id, rebrief_count, last_sent, history}`` dicts.
        """
        result = []
        for agent_id, history in self._drift_rebrief_history.items():
            result.append({
                "agent_id": agent_id,
                "rebrief_count": len(history),
                "last_sent": history[-1]["timestamp"] if history else None,
                "history": list(reversed(history)),
            })
        return result

    async def _handle_drift_warning(self, agent_id: str, drift_score: float) -> None:
        """Send an automatic role reminder to a drifted agent.

        Called by ``_route_loop`` whenever an ``agent_drift_warning`` STATUS
        event is received from the DriftMonitor.  Respects the per-agent
        cooldown configured in ``config.drift_rebrief_cooldown`` to avoid
        spamming agents that remain drifted.

        The re-brief message is composed of:
        1. ``config.drift_rebrief_message`` — the role reminder prefix.
        2. A snippet (first 200 chars) of the agent's current task prompt, if
           the agent is currently executing a task.

        The message is delivered via ``agent.notify_stdin()`` so it appears
        directly in the agent's tmux pane — the same channel used for task
        prompts and P2P notifications.

        Reference:
            Rath arXiv:2601.04170 — "drift-aware routing" behavioral anchoring.
            arXiv:2603.03258 — "goal reminder injection" prevents contextual drift.
            DESIGN.md §10.50 (v1.1.18)
        """
        if not self.config.drift_rebrief_enabled:
            return
        now = time.monotonic()
        last_sent = self._drift_rebrief_last_sent.get(agent_id, 0.0)
        if now - last_sent < self.config.drift_rebrief_cooldown:
            logger.debug(
                "Drift re-brief for %s skipped (cooldown %.0f s remaining)",
                agent_id,
                self.config.drift_rebrief_cooldown - (now - last_sent),
            )
            return
        agent = self.registry.get(agent_id)
        if agent is None:
            return
        # Build re-brief message
        rebrief = self.config.drift_rebrief_message
        if agent._current_task is not None:
            prompt_snippet = agent._current_task.prompt[:200]
            rebrief = f"{rebrief}\n\nYour current task:\n{prompt_snippet}"
        # Send to agent pane
        try:
            await agent.notify_stdin(rebrief)
        except Exception:  # noqa: BLE001
            logger.exception("Drift re-brief: notify_stdin failed for agent %s", agent_id)
            return
        # Record
        self._drift_rebrief_last_sent[agent_id] = now
        ts = datetime.now(timezone.utc).isoformat()
        self._drift_rebrief_history.setdefault(agent_id, []).append({
            "timestamp": ts,
            "drift_score": drift_score,
        })
        logger.info(
            "Drift re-brief sent to agent %s (drift_score=%.3f)", agent_id, drift_score
        )

    # ------------------------------------------------------------------
    # Rate limiter
    # ------------------------------------------------------------------

    def set_rate_limiter(self, rl: TokenBucketRateLimiter | None) -> None:
        """Attach or detach a rate limiter for task submission.

        Pass ``None`` to remove any rate limiting (unlimited throughput).
        """
        self._rate_limiter = rl

    def get_rate_limiter_status(self) -> dict:
        """Return the current rate limiter status dict.

        When no limiter is set, returns ``{"enabled": False, ...}`` with
        zeroed fields to allow safe consumption by REST clients.
        """
        if self._rate_limiter is None:
            return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}
        return self._rate_limiter.status()

    def reconfigure_rate_limiter(self, *, rate: float, burst: int) -> dict:
        """Create or reconfigure the rate limiter in place.

        If no limiter is attached, a new one is created.  Returns the
        updated status dict.

        Setting ``rate=0`` disables the limiter (``enabled=False``).
        """
        if rate == 0.0:
            # Disable: replace with a disabled limiter so status() works cleanly
            self._rate_limiter = TokenBucketRateLimiter(rate=0.0, burst=0)
        elif self._rate_limiter is None:
            self._rate_limiter = TokenBucketRateLimiter(rate=rate, burst=burst)
        else:
            self._rate_limiter.reconfigure(rate=rate, burst=burst)
        return self.get_rate_limiter_status()

    # ------------------------------------------------------------------
    # Task submission
    # ------------------------------------------------------------------

    async def submit_task(
        self,
        prompt: str,
        *,
        priority: int = 0,
        metadata: dict | None = None,
        depends_on: list[str] | None = None,
        idempotency_key: str | None = None,
        reply_to: str | None = None,
        target_agent: str | None = None,
        required_tags: list[str] | None = None,
        target_group: str | None = None,
        wait_for_token: bool = True,
        max_retries: int = 0,
        inherit_priority: bool = True,
        ttl: float | None = None,
        timeout: int | None = None,
        _task_id: str | None = None,
    ) -> Task:
        """Submit a new task to the priority queue.

        Parameters
        ----------
        wait_for_token:
            When ``True`` (default), waits asynchronously for a rate-limit
            token if the bucket is empty.  When ``False``, raises
            ``RateLimitExceeded`` immediately if no token is available.
        """
        # ---- Rate limiting (token bucket) ----
        if self._rate_limiter is not None and self._rate_limiter.enabled:
            if wait_for_token:
                await self._rate_limiter.acquire()
            else:
                acquired = self._rate_limiter.try_acquire()
                if not acquired:
                    # Publish observability event before raising
                    await self.bus.publish(Message(
                        type=MessageType.STATUS,
                        from_id="__orchestrator__",
                        payload={
                            "event": "rate_limit_exceeded",
                            "prompt": prompt,
                            "rate": self._rate_limiter.rate,
                            "burst": self._rate_limiter.burst,
                            "available_tokens": self._rate_limiter.status()["available_tokens"],
                        },
                    ))
                    raise RateLimitExceeded(
                        rate=self._rate_limiter.rate,
                        burst=self._rate_limiter.burst,
                        available=self._rate_limiter.status()["available_tokens"],
                    )
        # Idempotency deduplication: return existing task for duplicate keys.
        if idempotency_key is not None:
            existing_id = self._idempotency_keys.get(idempotency_key)
            if existing_id is not None:
                logger.info(
                    "submit_task: duplicate idempotency_key=%r → existing task %s",
                    idempotency_key, existing_id,
                )
                return Task(id=existing_id, prompt=prompt)
        if self._task_queue.full():
            raise RuntimeError(
                f"Task queue is full (maxsize={self.config.task_queue_maxsize})"
            )
        # Priority inheritance: when inherit_priority=True and depends_on is non-empty,
        # set the effective priority to min(own_priority, min(parent priorities)).
        # This prevents high-priority dependent tasks from being blocked by the
        # lower-priority work already queued ahead of them.
        # Reference: Liu & Layland JACM (1973); Sha et al. IEEE (1990);
        # Apache Airflow priority_weight rules; DESIGN.md §10.27 (v0.32.0)
        effective_priority = priority
        if inherit_priority and depends_on:
            parent_priorities = [
                self._task_priorities[dep]
                for dep in depends_on
                if dep in self._task_priorities
            ]
            if parent_priorities:
                effective_priority = min(priority, min(parent_priorities))
        # Resolve effective TTL: per-task ttl overrides default_task_ttl
        effective_ttl = ttl if ttl is not None else self.config.default_task_ttl
        submitted_at = time.time()
        expires_at = (submitted_at + effective_ttl) if effective_ttl is not None else None
        task = Task(
            id=_task_id if _task_id is not None else str(uuid.uuid4()),
            prompt=prompt,
            priority=effective_priority,
            metadata=metadata or {},
            depends_on=depends_on or [],
            reply_to=reply_to,
            target_agent=target_agent,
            required_tags=required_tags or [],
            target_group=target_group,
            max_retries=max_retries,
            inherit_priority=inherit_priority,
            ttl=effective_ttl,
            submitted_at=submitted_at,
            expires_at=expires_at,
            timeout=timeout,
        )
        # Record this task's priority for use by future dependent tasks.
        self._task_priorities[task.id] = effective_priority
        # Record persistent depends_on snapshot for DAG visualization (v1.2.14).
        # Unlike _task_dependents (cleared on completion), _task_deps is never
        # cleaned up so GET /workflows/{id}/dag can reconstruct topology later.
        # Design reference: DESIGN.md §10.90 (v1.2.14)
        if task.depends_on:
            self._task_deps[task.id] = list(task.depends_on)
        if idempotency_key is not None:
            self._idempotency_keys[idempotency_key] = task.id
            self._ikey_timestamps[idempotency_key] = time.monotonic()
            self._cleanup_expired_ikeys()
        if reply_to is not None:
            self._task_reply_to[task.id] = reply_to
        # Dependency check: if all deps are already complete, enqueue immediately.
        # If any dep has already FAILED, fail this task immediately too.
        # Otherwise hold in _waiting_tasks until deps are satisfied.
        # Reference: GNU Make prerequisite resolution; Dask task graph scheduler;
        # Apache Spark DAG scheduler; POSIX make prerequisites.
        # DESIGN.md §10.24 (v0.29.0)
        unmet_deps = [dep for dep in task.depends_on if dep not in self._completed_tasks]
        failed_deps = [dep for dep in task.depends_on if dep in self._failed_tasks]
        if failed_deps:
            # Immediate cascade failure — do not enqueue at all
            self._failed_tasks.add(task.id)
            failed_dep = failed_deps[0]
            await self.bus.publish(Message(
                type=MessageType.STATUS,
                from_id="__orchestrator__",
                payload={
                    "event": "task_dependency_failed",
                    "task_id": task.id,
                    "failed_dep": failed_dep,
                    "error": f"dependency_failed:{failed_dep}",
                },
            ))
            logger.warning(
                "Task %s failed immediately: dependency %s already failed",
                task.id, failed_dep,
            )
        elif not unmet_deps:
            # All deps already done — enqueue immediately
            self._task_seq += 1
            await self._task_queue.put((priority, self._task_seq, task))
            # Checkpoint: persist task to SQLite for fault-tolerant recovery.
            if self._checkpoint_store is not None:
                self._checkpoint_store.save_task(task=task, queue_priority=priority)
            # OTel span: record task_queued event with GenAI semconv attributes.
            if self._telemetry is not None:
                from tmux_orchestrator.telemetry import task_queued_span
                with task_queued_span(
                    setup=self._telemetry,
                    task_id=task.id,
                    prompt=prompt,
                    priority=priority,
                ):
                    pass  # span captures submission metadata; work happens in _dispatch_loop
            await self.bus.publish(Message(
                type=MessageType.STATUS,
                from_id="__orchestrator__",
                payload={
                    "event": "task_queued",
                    "task_id": task.id,
                    "prompt": prompt,
                    **({"reply_to": reply_to} if reply_to is not None else {}),
                    **({"target_agent": target_agent} if target_agent is not None else {}),
                    **({"required_tags": required_tags} if required_tags else {}),
                    **({"target_group": target_group} if target_group is not None else {}),
                },
            ))
            logger.info("Task %s queued (priority=%d, reply_to=%s, required_tags=%s, target_group=%s)",
                        task.id, priority, reply_to, required_tags, target_group)
        else:
            # Hold task until deps are satisfied
            self._waiting_tasks[task.id] = task
            for dep in unmet_deps:
                self._task_dependents.setdefault(dep, []).append(task.id)
            # Checkpoint: persist waiting task so it survives process restart.
            if self._checkpoint_store is not None:
                self._checkpoint_store.save_waiting_task(task=task)
            await self.bus.publish(Message(
                type=MessageType.STATUS,
                from_id="__orchestrator__",
                payload={
                    "event": "task_waiting",
                    "task_id": task.id,
                    "prompt": prompt,
                    "depends_on": task.depends_on,
                    "unmet_deps": unmet_deps,
                },
            ))
            logger.info(
                "Task %s waiting for deps %s (priority=%d)",
                task.id, unmet_deps, priority,
            )
        return task

    def _cleanup_expired_ikeys(self) -> None:
        """Remove idempotency entries older than _ikey_ttl."""
        cutoff = time.monotonic() - self._ikey_ttl
        expired = [k for k, t in self._ikey_timestamps.items() if t < cutoff]
        for k in expired:
            self._idempotency_keys.pop(k, None)
            self._ikey_timestamps.pop(k, None)

    def list_tasks(self) -> list[dict]:
        """Return a snapshot of the pending task queue (non-destructive).

        Includes tasks currently in the priority queue (``status="queued"``) plus
        tasks held in ``_waiting_tasks`` waiting for dependency resolution
        (``status="waiting"``).  The waiting tasks are appended after the
        queued tasks.
        """
        items = list(self._task_queue._queue)  # type: ignore[attr-defined]
        # When using AsyncPriorityTaskQueue (v1.2.6+), filter out stale heap entries
        # that were superseded by update_priority() (lazy-deletion pattern).
        # _deleted_seqs holds the sequence numbers of stale entries.
        _deleted_seqs: set[int] = getattr(self._task_queue, "_deleted_seqs", set())
        result = [
            {
                "priority": p,
                "task_id": t.id,
                "prompt": t.prompt,
                "status": "queued",
                "depends_on": t.depends_on,
                "submitted_at": t.submitted_at,
                "ttl": t.ttl,
                "expires_at": t.expires_at,
                "timeout": t.timeout,
                **({"required_tags": t.required_tags} if t.required_tags else {}),
                **({"target_agent": t.target_agent} if t.target_agent else {}),
                **({"target_group": t.target_group} if t.target_group else {}),
            }
            for p, _seq, t in sorted(items, key=lambda x: (x[0], x[1]))
            if _seq not in _deleted_seqs
        ]
        # Append waiting tasks (held pending dependency resolution)
        for t in self._waiting_tasks.values():
            result.append({
                "priority": t.priority,
                "task_id": t.id,
                "prompt": t.prompt,
                "status": "waiting",
                "depends_on": t.depends_on,
                "submitted_at": t.submitted_at,
                "ttl": t.ttl,
                "expires_at": t.expires_at,
                **({"required_tags": t.required_tags} if t.required_tags else {}),
                **({"target_agent": t.target_agent} if t.target_agent else {}),
                **({"target_group": t.target_group} if t.target_group else {}),
            })
        return result

    def get_waiting_task(self, task_id: str) -> Task | None:
        """Return a waiting task by ID, or None if not in _waiting_tasks."""
        return self._waiting_tasks.get(task_id)

    def get_task_info(self, task_id: str) -> dict:
        """Return a status snapshot for *task_id* suitable for DAG visualization.

        Looks up the task across all tracking dicts (active, waiting, queued,
        completed, failed) and returns a unified status dict.

        Returns a dict with keys:

        - ``task_id``: the task ID
        - ``status``: one of ``"running"``, ``"waiting"``, ``"queued"``,
          ``"success"``, ``"failed"``, ``"cancelled"``  — or ``"unknown"``
          if the task ID has never been seen.
        - ``depends_on``: list of task IDs this task depends on (from
          persistent ``_task_deps``).
        - ``dependents``: currently-waiting tasks that depend on this one
          (from live ``_task_dependents``; empty once they are released).
        - ``assigned_agent``: agent ID that ran / is running this task, or
          ``None`` if not yet dispatched.
        - ``started_at``: ISO-8601 string when the task was dispatched, or
          ``None``.
        - ``finished_at``: ``None`` (not yet available without result store;
          callers wanting history should use ``get_agent_history``).

        Design reference: DESIGN.md §10.90 (v1.2.14)
        """
        deps = self._task_deps.get(task_id, [])
        dependents = list(self._task_dependents.get(task_id, []))
        assigned = self._task_agent.get(task_id)

        # Determine status from tracking sets / queues.
        if task_id in self._active_tasks:
            status = "running"
        elif task_id in self._waiting_tasks:
            status = "waiting"
        elif task_id in self._completed_tasks:
            status = "success"
        elif task_id in self._failed_tasks:
            status = "failed"
        else:
            # Check if it's in the priority queue
            in_queue = any(
                t.id == task_id
                for _, _, t in list(self._task_queue._queue)  # type: ignore[attr-defined]
            )
            status = "queued" if in_queue else "unknown"

        # started_at from monotonic clock — convert to approximate ISO timestamp
        # using the same approach as _record_agent_history.
        started_at: str | None = None
        started_ts = self._task_started_at.get(task_id)
        if started_ts is not None:
            from datetime import datetime, timezone  # noqa: PLC0415
            elapsed = time.monotonic() - started_ts
            started_at = datetime.fromtimestamp(
                datetime.now(tz=timezone.utc).timestamp() - elapsed,
                tz=timezone.utc,
            ).isoformat()

        return {
            "task_id": task_id,
            "status": status,
            "depends_on": deps,
            "dependents": dependents,
            "assigned_agent": assigned,
            "started_at": started_at,
            "finished_at": None,
        }

    async def update_task_priority(self, task_id: str, new_priority: int) -> bool:
        """Update the priority of a pending task in-place.

        Locates *task_id* in the priority queue, changes its priority to
        *new_priority*, and restores the heap invariant.
        Returns ``True`` if the task was found and updated; ``False`` if not
        found (already dispatched, completed, or never submitted).

        A ``task_priority_updated`` STATUS event is published on success.

        Implementation selects the best available strategy:
        1. If the queue implements ``update_priority()`` (lazy-deletion pattern,
           AsyncPriorityTaskQueue v1.2.6+), delegates to it for O(log n) update.
        2. Falls back to a full heap rebuild (O(n)) for injected mock queues or
           older queue implementations.

        Design references:
        - Python heapq docs "Priority Queue Implementation Notes"
          https://docs.python.org/3/library/heapq.html
        - Liu, C.L.; Layland, J.W. (1973). "Scheduling Algorithms for
          Multiprogramming in a Hard Real-Time Environment". JACM 20(1).
        - Sedgewick & Wayne "Algorithms" 4th ed. §2.4 — Priority Queues.
        - DESIGN.md §10.82 — v1.2.6 Dynamic Task Priority Update.
        """
        # Prefer the queue's built-in update_priority() which uses lazy-deletion
        # (O(log n), no heap rebuild, no duplication issues).
        if hasattr(self._task_queue, "update_priority"):
            found = self._task_queue.update_priority(task_id, new_priority)  # type: ignore[attr-defined]
        else:
            # Fallback: rebuild heap (O(n)) for injected queues without update_priority.
            items = list(self._task_queue._queue)  # type: ignore[attr-defined]
            new_items = []
            found = False
            for p, seq, t in items:
                if t.id == task_id:
                    t.priority = new_priority
                    new_items.append((new_priority, seq, t))
                    found = True
                else:
                    new_items.append((p, seq, t))

            if found:
                self._task_queue._queue.clear()  # type: ignore[attr-defined]
                for item in new_items:
                    self._task_queue._queue.append(item)  # type: ignore[attr-defined]
                heapq.heapify(self._task_queue._queue)  # type: ignore[attr-defined]

        if not found:
            return False

        await self.bus.publish(Message(
            type=MessageType.STATUS,
            from_id="__orchestrator__",
            payload={
                "event": "task_priority_updated",
                "task_id": task_id,
                "priority": new_priority,
            },
        ))
        logger.info("Task %s priority updated to %d", task_id, new_priority)
        return True

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel *task_id* whether it is queued or currently in-progress.

        Handles two cases:

        1. **In-progress** (task is in ``_active_tasks``): adds task_id to
           ``_cancelled_task_ids``, calls ``agent.interrupt()`` to send Ctrl-C,
           and publishes STATUS ``task_cancelled`` with ``was_running=True``.
           When the agent's RESULT eventually arrives, ``_route_loop`` sees
           the tombstone and silently discards it.

        2. **Queued** (task is in ``_task_queue``): removes task from the heap
           and adjusts internal counters; publishes STATUS ``task_cancelled``
           with ``was_running=False``.

        Returns ``True`` if the task was found and cancelled; ``False`` if not
        found (already completed, dead-lettered, or never submitted).

        Cancelled tasks are NOT moved to the DLQ.

        Design references:
        - Kubernetes Pod deletion grace period — SIGTERM → grace → SIGKILL
        - POSIX SIGTERM/SIGKILL model — cooperative interrupt before forced kill
        - Java Future.cancel(mayInterruptIfRunning=true) — in-flight interruption
        - Go context.Context cancellation — propagated cancellation token
        - Microsoft Azure "Asynchronous Request-Reply pattern" (2024)
        - DESIGN.md §10.22 (v0.27.0)
        """
        # Case 1: task is in-progress (dispatched to an agent).
        task = self._active_tasks.get(task_id)
        if task is not None:
            # Find the agent currently running this task
            agent = None
            for a in self.registry.all_agents().values():
                if a._current_task is not None and a._current_task.id == task_id:
                    agent = a
                    break
            # Mark as cancelled so _route_loop discards the eventual RESULT
            self._cancelled_task_ids.add(task_id)
            # Send interrupt signal to the agent process
            if agent is not None:
                await agent.interrupt()
            await self.bus.publish(Message(
                type=MessageType.STATUS,
                from_id="__orchestrator__",
                payload={
                    "event": "task_cancelled",
                    "task_id": task_id,
                    "was_running": True,
                },
            ))
            asyncio.create_task(
                self._webhook_manager.deliver("task_cancelled", {
                    "task_id": task_id,
                    "was_running": True,
                }),
                name=f"wh-task-cancelled-{task_id[:8]}",
            )
            logger.info("Task %s cancelled (in-progress, interrupt sent)", task_id)
            return True

        # Case 2: task is in _waiting_tasks (held pending dependency resolution).
        if task_id in self._waiting_tasks:
            waiting_task = self._waiting_tasks.pop(task_id)
            # Remove from all reverse-lookup lists
            for dep in waiting_task.depends_on:
                if dep in self._task_dependents:
                    try:
                        self._task_dependents[dep].remove(task_id)
                    except ValueError:
                        pass
            await self.bus.publish(Message(
                type=MessageType.STATUS,
                from_id="__orchestrator__",
                payload={
                    "event": "task_cancelled",
                    "task_id": task_id,
                    "was_running": False,
                    "was_waiting": True,
                },
            ))
            asyncio.create_task(
                self._webhook_manager.deliver("task_cancelled", {
                    "task_id": task_id,
                    "was_running": False,
                    "was_waiting": True,
                }),
                name=f"wh-task-cancelled-w-{task_id[:8]}",
            )
            logger.info("Task %s cancelled from waiting (dependency hold)", task_id)
            return True

        # Case 3: task is still queued — remove from heap directly.
        items = list(self._task_queue._queue)  # type: ignore[attr-defined]
        new_items = [(p, seq, t) for p, seq, t in items if t.id != task_id]
        if len(new_items) == len(items):
            # Task was not in the queue and not in-progress.
            return False

        # Rebuild the queue with remaining items.
        # asyncio.PriorityQueue stores items in a list heap — replace it directly.
        self._task_queue._queue.clear()  # type: ignore[attr-defined]
        for item in new_items:
            self._task_queue._queue.append(item)  # type: ignore[attr-defined]
        heapq.heapify(self._task_queue._queue)  # type: ignore[attr-defined]
        # Adjust the unfinished-tasks counter to avoid task_done() mismatch.
        # _unfinished_tasks is incremented by put() and decremented by task_done().
        # Since we removed one item without calling task_done(), decrement manually.
        if self._task_queue._unfinished_tasks > 0:  # type: ignore[attr-defined]
            self._task_queue._unfinished_tasks -= 1  # type: ignore[attr-defined]
            if self._task_queue._unfinished_tasks == 0:  # type: ignore[attr-defined]
                self._task_queue._finished.set()  # type: ignore[attr-defined]
        # Also remove from _pending so empty() / qsize() return correct values
        # (AsyncPriorityTaskQueue v1.2.6+ tracks live items in _pending).
        if hasattr(self._task_queue, "_pending"):
            self._task_queue._pending.pop(task_id, None)  # type: ignore[attr-defined]

        await self.bus.publish(Message(
            type=MessageType.STATUS,
            from_id="__orchestrator__",
            payload={
                "event": "task_cancelled",
                "task_id": task_id,
                "was_running": False,
            },
        ))
        asyncio.create_task(
            self._webhook_manager.deliver("task_cancelled", {
                "task_id": task_id,
                "was_running": False,
            }),
            name=f"wh-task-cancelled-q-{task_id[:8]}",
        )
        logger.info("Task %s cancelled from queue", task_id)
        return True

    async def broadcast_task(
        self,
        prompt: str,
        agent_ids: list[str],
        *,
        mode: str = "race",
        priority: int = 0,
        timeout: int | None = None,
    ) -> "BroadcastResult":
        """Submit the same task to multiple agents simultaneously.

        Parameters
        ----------
        prompt:
            Task prompt sent to every target agent.
        agent_ids:
            Explicit list of agent IDs to receive the task.  The caller is
            responsible for resolving tags / groups before calling this method.
        mode:
            ``"race"`` — first successful result wins; remaining tasks are
            cancelled.  ``"gather"`` — wait for ALL tasks to complete (success
            or failure) before marking the broadcast done.
        priority:
            Task priority (lower = dispatched first).
        timeout:
            Per-task timeout in seconds.  ``None`` uses the global config value.

        Returns
        -------
        BroadcastResult
            Metadata about the broadcast (id, task_ids, agent_ids, mode).

        Design references:
        - Fan-out / Fan-in concurrency pattern — distribute work, cancel losers.
        - Sakana AI ALE-Agent — parallel multi-agent trial-and-error with selection.
        - Go context.Context cancellation — abort remaining workers on first result.
        - DESIGN.md §10.91 (v1.2.15)
        """
        broadcast_id = str(uuid.uuid4())
        task_ids: list[str] = []
        for agent_id in agent_ids:
            task = await self.submit_task(
                prompt,
                priority=priority,
                target_agent=agent_id,
                timeout=timeout,
            )
            task_ids.append(task.id)
            self._task_to_broadcast[task.id] = broadcast_id

        group = BroadcastGroup(
            broadcast_id=broadcast_id,
            mode=mode,
            task_ids=list(task_ids),
            agent_ids=list(agent_ids),
            status="pending" if task_ids else "complete",
        )
        self._broadcast_groups[broadcast_id] = group
        if not task_ids:
            group.status = "complete"
        logger.info(
            "Broadcast %s submitted: %d tasks, mode=%s", broadcast_id, len(task_ids), mode
        )
        return BroadcastResult(
            broadcast_id=broadcast_id,
            mode=mode,
            task_ids=task_ids,
            agent_ids=list(agent_ids),
        )

    def get_broadcast(self, broadcast_id: str) -> "BroadcastGroup | None":
        """Return the :class:`BroadcastGroup` for *broadcast_id*, or ``None``.

        Design reference: DESIGN.md §10.91 (v1.2.15)
        """
        return self._broadcast_groups.get(broadcast_id)

    async def cancel_workflow(self, workflow_id: str) -> dict | None:
        """Cancel all tasks in *workflow_id* and mark the workflow as cancelled.

        Iterates over all task IDs registered to the workflow and cancels each
        one (queued or in-progress).  Returns a summary dict with two keys:

        - ``cancelled``: list of task IDs that were successfully cancelled.
        - ``already_done``: list of task IDs that were not found (already
          completed, dead-lettered, or unknown).

        Returns ``None`` if *workflow_id* is unknown.

        Design reference:
        - Apache Airflow ``dag_run.update_state("cancelled")`` — bulk cancel
        - AWS Step Functions ``StopExecution`` — cancel a running state machine
        - DESIGN.md §10.22 (v0.27.0)
        """
        wm = self._workflow_manager
        run = wm.get(workflow_id)
        if run is None:
            return None

        cancelled_ids: list[str] = []
        already_done_ids: list[str] = []

        for task_id in list(run.task_ids):
            ok = await self.cancel_task(task_id)
            if ok:
                cancelled_ids.append(task_id)
            else:
                already_done_ids.append(task_id)

        # Mark the workflow as cancelled regardless of individual task outcomes.
        wm.cancel(workflow_id)

        return {
            "workflow_id": workflow_id,
            "cancelled": cancelled_ids,
            "already_done": already_done_ids,
        }

    # ------------------------------------------------------------------
    # Agent drain / graceful shutdown
    # ------------------------------------------------------------------

    async def drain_agent(self, agent_id: str) -> dict:
        """Put *agent_id* into graceful drain mode.

        - IDLE: stop immediately, remove from registry, return ``{status: "stopped_immediately"}``.
        - BUSY: mark as DRAINING; auto-stopped when current task completes.
          Returns ``{status: "draining"}``.
        - DRAINING: returns ``{status: "already_draining"}`` (409 semantics).
        - STOPPED / ERROR: returns ``{status: "already_stopped"}`` (409 semantics).
        - Not found: raises ``KeyError``.

        Design references:
        - Kubernetes Pod ``terminationGracePeriodSeconds`` — allow running tasks to
          finish before the pod is killed.
        - HAProxy graceful restart — drain in-flight connections before reload.
        - UNIX ``SO_LINGER`` graceful socket close — wait for pending data before close.
        - AWS ECS ``stopTimeout`` — container stop grace period.
        - DESIGN.md §10.23 (v0.28.0)
        """
        agent = self.registry.get(agent_id)
        if agent is None:
            raise KeyError(agent_id)

        if agent.status == AgentStatus.DRAINING:
            return {"agent_id": agent_id, "status": "already_draining"}

        if agent.status in (AgentStatus.STOPPED, AgentStatus.ERROR):
            return {"agent_id": agent_id, "status": "already_stopped"}

        if agent.status == AgentStatus.IDLE:
            await agent.stop()
            self.registry.unregister(agent_id)
            self._draining_agents.discard(agent_id)
            await self.bus.publish(Message(
                type=MessageType.STATUS,
                from_id="__orchestrator__",
                payload={"event": "agent_drained", "agent_id": agent_id},
            ))
            logger.info("Agent %s drained immediately (was IDLE)", agent_id)
            return {"agent_id": agent_id, "status": "stopped_immediately"}

        # BUSY — mark as DRAINING; _route_loop will auto-stop on RESULT
        agent.status = AgentStatus.DRAINING
        self._draining_agents.add(agent_id)
        await self.bus.publish(Message(
            type=MessageType.STATUS,
            from_id="__orchestrator__",
            payload={"event": "agent_draining", "agent_id": agent_id},
        ))
        logger.info("Agent %s marked DRAINING (will stop after current task)", agent_id)
        return {"agent_id": agent_id, "status": "draining"}

    async def drain_all(self) -> dict:
        """Drain all registered agents.

        Calls ``drain_agent()`` for every registered agent and returns a summary::

            {
                "draining":       [agent_ids that are now draining (were BUSY)],
                "stopped_immediately": [agent_ids stopped immediately (were IDLE)],
                "already_stopped": [agent_ids skipped (already STOPPED/ERROR/DRAINING)],
            }

        Design reference: DESIGN.md §10.23 (v0.28.0).
        """
        draining_ids: list[str] = []
        stopped_immediately_ids: list[str] = []
        already_stopped_ids: list[str] = []

        for agent_id in list(self.registry.all_agents()):
            result = await self.drain_agent(agent_id)
            s = result["status"]
            if s == "draining":
                draining_ids.append(agent_id)
            elif s == "stopped_immediately":
                stopped_immediately_ids.append(agent_id)
            else:
                already_stopped_ids.append(agent_id)

        return {
            "draining": draining_ids,
            "stopped_immediately": stopped_immediately_ids,
            "already_stopped": already_stopped_ids,
        }

    # ------------------------------------------------------------------
    # Per-agent task history
    # ------------------------------------------------------------------

    def get_agent_history(
        self, agent_id: str, *, limit: int = 50
    ) -> list[dict] | None:
        """Return the last *limit* completed task records for *agent_id*.

        Returns ``None`` if *agent_id* is not registered.
        Each entry is a dict with fields:
          task_id, prompt, started_at, finished_at, duration_s,
          status ("success" | "error"), error (str | null).

        Ordered most-recent-first.  History is capped at 200 entries.

        Design: per-agent task history enables identifying bottlenecks and
        tracing decision paths, per TAMAS (IBM, 2025) "Beyond Black-Box
        Benchmarking: Observability, Analytics, and Optimization of Agentic
        Systems" arXiv:2503.06745.
        """
        if self.registry.get(agent_id) is None and agent_id not in self._agent_history:
            return None
        entries = self._agent_history.get(agent_id, [])
        # most-recent-first
        return list(reversed(entries[-200:]))[:limit]

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        while True:
            if self._paused:
                await asyncio.sleep(0.5)
                continue
            try:
                _, _seq, task = await asyncio.wait_for(
                    self._task_queue.get(), timeout=0.5
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            # Tombstone check: skip tasks that were cancelled while queued.
            # The task_id is placed in _cancelled_task_ids by cancel_task() when
            # the PriorityQueue does not yet contain the task (race window) or as
            # the primary cancellation mechanism for queued tasks.
            if task.id in self._cancelled_task_ids:
                self._cancelled_task_ids.discard(task.id)
                self._task_queue.task_done()
                await self.bus.publish(Message(
                    type=MessageType.STATUS,
                    from_id="__orchestrator__",
                    payload={
                        "event": "task_cancelled",
                        "task_id": task.id,
                        "was_running": False,
                    },
                ))
                logger.info("Task %s skipped (tombstone-cancelled)", task.id)
                continue

            # TTL expiry check: discard task if it has passed its expiry time.
            # Reference: RabbitMQ queue-head expiry; Azure Service Bus AbsoluteExpiryTime;
            # DESIGN.md §10.28 (v0.33.0)
            if task.expires_at is not None and time.time() > task.expires_at:
                self._task_queue.task_done()
                await self._expire_task(task, from_reaper=False)
                continue

            # --- Deferred ephemeral agent spawn for chain_branch phases (v1.2.5) ---
            # When the workflow router stored ``_ephemeral_template`` in task
            # metadata (chain_branch=True), the ephemeral agent was NOT spawned
            # at submission time.  We spawn it here — after all depends_on tasks
            # have completed — so that ``create_from_branch`` sees the predecessor
            # phase's committed files.
            #
            # The predecessor's ephemeral agent ID is resolved via:
            #   _task_ephemeral_agent[pred_task_id] → pred_ephemeral_id
            #   _ephemeral_agent_branches[pred_ephemeral_id] → source_branch
            #
            # Design reference: DESIGN.md §10.81
            if task.metadata.get("_ephemeral_template") and task.target_agent is None:
                _tmpl = task.metadata["_ephemeral_template"]
                _pred_task_ids: list[str] = task.metadata.get("_chain_pred_task_ids", [])
                _source_branch: str | None = None
                for _pred_task_id in _pred_task_ids:
                    _pred_eph_id = self._task_ephemeral_agent.get(_pred_task_id)
                    if _pred_eph_id:
                        _candidate = self._ephemeral_agent_branches.get(_pred_eph_id)
                        if _candidate:
                            _source_branch = _candidate
                            break
                # Workflow ID for branch tracking (v1.2.8): look up from
                # _task_workflow_id (populated by router after run.id is known).
                _chain_wf_id: str | None = self._task_workflow_id.get(task.id)
                try:
                    _new_eph_id = await self.spawn_ephemeral_agent(
                        _tmpl,
                        source_branch=_source_branch,
                        workflow_id=_chain_wf_id,
                    )
                    import dataclasses as _dc  # noqa: PLC0415
                    task = _dc.replace(task, target_agent=_new_eph_id)
                    # Record task → ephemeral mapping so successors can resolve branch.
                    self._task_ephemeral_agent[task.id] = _new_eph_id
                    # Update the in-flight task record so the RESULT handler knows
                    # the actual ephemeral agent that ran this task.
                    self._active_tasks[task.id] = task
                    logger.info(
                        "Deferred chain_branch spawn: task %s → ephemeral agent %s "
                        "(source_branch=%s, workflow_id=%s)",
                        task.id,
                        _new_eph_id,
                        _source_branch or "<none>",
                        _chain_wf_id or "<none>",
                    )
                except (ValueError, Exception) as _spawn_err:
                    logger.error(
                        "Deferred chain_branch spawn failed for task %s template %r: %s",
                        task.id,
                        _tmpl,
                        _spawn_err,
                    )
                    await self._dead_letter(task, f"deferred spawn failed: {_spawn_err}")
                    continue

            # --- Agent selection: respect target_agent routing ---
            if task.target_agent is not None:
                # Task must be routed to a specific agent.
                target = self.registry.get(task.target_agent)
                if target is None:
                    # Named agent does not exist — dead letter immediately.
                    await self._dead_letter(
                        task,
                        f"unknown target_agent={task.target_agent!r}",
                    )
                    continue
                if target.status != AgentStatus.IDLE:
                    # Target exists but is busy — re-queue and wait.
                    retry_count = task.metadata.get("_retry_count", 0) + 1
                    task.metadata["_retry_count"] = retry_count
                    if retry_count >= self.config.dlq_max_retries:
                        await self._dead_letter(
                            task,
                            f"target_agent={task.target_agent!r} not idle after {retry_count} retries",
                        )
                    else:
                        self._task_seq += 1
                        await self._task_queue.put((task.priority, self._task_seq, task))
                        await asyncio.sleep(0.2)
                    continue
                agent = target
            else:
                # Resolve group membership for filtering, if target_group is set
                group_members: set[str] | None = None
                if task.target_group is not None:
                    group_members = self._group_manager.get(task.target_group)
                    if group_members is None:
                        # Unknown group — dead letter immediately
                        await self._dead_letter(
                            task,
                            f"unknown target_group={task.target_group!r}",
                        )
                        continue
                agent = self.registry.find_idle_worker(
                    required_tags=task.required_tags,
                    allowed_agent_ids=group_members,
                    excluded_agent_ids=set(task.excluded_agents) if task.excluded_agents else None,
                )
            if agent is None:
                retry_count = task.metadata.get("_retry_count", 0) + 1
                task.metadata["_retry_count"] = retry_count
                if retry_count >= self.config.dlq_max_retries:
                    parts = []
                    if task.required_tags:
                        parts.append(f"required_tags={task.required_tags!r}")
                    if task.target_group:
                        parts.append(f"target_group={task.target_group!r}")
                    reason = (
                        f"no idle agent with {', '.join(parts)} after {retry_count} retries"
                        if parts
                        else f"no idle agent after {retry_count} retries"
                    )
                    await self._dead_letter(task, reason)
                else:
                    self._task_seq += 1
                    await self._task_queue.put((task.priority, self._task_seq, task))
                    await asyncio.sleep(0.2)
                continue

            logger.info("Dispatching task %s → agent %s", task.id, agent.id)
            self.registry.record_busy(agent.id)
            # Record dispatch time for history duration tracking.
            self._task_started_at[task.id] = time.monotonic()
            self._task_started_prompt[task.id] = task.prompt
            if task.timeout is not None:
                self._task_timeout[task.id] = task.timeout
            # Track the Task object for potential retry on failure.
            self._active_tasks[task.id] = task
            # Record agent assignment for DAG visualization (v1.2.14).
            # Persistent: never cleared, so completed-task nodes show their agent.
            # Design reference: DESIGN.md §10.90 (v1.2.14)
            self._task_agent[task.id] = agent.id
            # --- Episode auto-inject (v1.0.29) ---
            # If an EpisodeStore is attached and memory_inject_count > 0, prepend
            # the agent's most-recent episodes to the task prompt so the agent has
            # persistent memory from prior tasks without making manual API calls.
            # Reference: Wang & Chen "MIRIX" arXiv:2507.07957 (2025) — Active
            # Retrieval pattern; DESIGN.md §10.29 (v1.0.29).
            inject_count = getattr(self.config, "memory_inject_count", 0)
            if inject_count > 0 and self._episode_store is not None:
                try:
                    episodes = self._episode_store.list(agent.id, limit=inject_count)
                    if episodes:
                        lines = [
                            f"## 過去のタスク経験 (直近{len(episodes)}件)\n"
                        ]
                        for i, ep in enumerate(episodes, 1):
                            ts = ep.get("created_at", "")[:19]  # strip microseconds
                            summary = ep.get("summary", "")
                            outcome = ep.get("outcome", "")
                            lines.append(f"{i}. [{ts}] {summary} | outcome: {outcome}")
                        lines.append("\n---\n")
                        prefix = "\n".join(lines)
                        import dataclasses  # noqa: PLC0415
                        task = dataclasses.replace(task, prompt=prefix + task.prompt)
                        logger.debug(
                            "Episode inject: prepended %d episodes to task %s for agent %s",
                            len(episodes), task.id, agent.id,
                        )
                except Exception as _ep_err:  # noqa: BLE001
                    logger.warning(
                        "Episode inject failed for agent %s task %s: %s",
                        agent.id, task.id, _ep_err,
                    )
            # OTel span: record agent invocation with GenAI semconv attributes.
            if self._telemetry is not None:
                from tmux_orchestrator.telemetry import agent_span
                with agent_span(
                    setup=self._telemetry,
                    agent_id=agent.id,
                    agent_name=getattr(agent, "name", agent.id),
                    task_id=task.id,
                    prompt=task.prompt,
                ):
                    pass  # span closes here; task execution is async in agent run loop
            await agent.send_task(task)
            self._task_queue.task_done()
            # Yield so the agent's _run_loop can dequeue and set status=BUSY
            # before the next find_idle_worker() call.  Without this yield, all
            # tasks pile up in the first agent's queue (agent.status stays IDLE
            # until the run loop gets to run).
            await asyncio.sleep(0)

    async def _expire_task(self, task: Task, *, from_reaper: bool = False) -> None:
        """Mark *task* as expired and cascade dependency failures.

        Called from _dispatch_loop (queued expiry) and _ttl_reaper_loop
        (waiting-task expiry).  Publishes ``task_expired`` STATUS event,
        calls WorkflowManager.on_task_failed(), and cascades
        ``task_dependency_failed`` to waiting dependents via _on_dep_failed().

        Reference: RabbitMQ "Time-To-Live and Expiration" docs;
        Azure Service Bus message expiration; DESIGN.md §10.28 (v0.33.0)
        """
        expired_at = time.time()
        self._failed_tasks.add(task.id)
        self._workflow_manager.on_task_failed(task.id)
        await self.bus.publish(Message(
            type=MessageType.STATUS,
            from_id="__orchestrator__",
            payload={
                "event": "task_expired",
                "task_id": task.id,
                "expired_at": expired_at,
                "ttl": task.ttl,
                "submitted_at": task.submitted_at,
                "expires_at": task.expires_at,
                "from_reaper": from_reaper,
            },
        ))
        await self._on_dep_failed(task.id)
        logger.info(
            "Task %s expired (ttl=%.1f, expired_at=%.3f, from_reaper=%s)",
            task.id, task.ttl or 0.0, expired_at, from_reaper,
        )

    async def _dead_letter(self, task: Task, reason: str) -> None:
        """Move *task* to the dead letter queue and publish a STATUS event."""
        retry_count = task.metadata.get("_retry_count", 0)
        self._dlq.append({
            "task_id": task.id,
            "prompt": task.prompt,
            "priority": task.priority,
            "retry_count": retry_count,
            "reason": reason,
            "trace_id": task.trace_id,
        })
        self._task_queue.task_done()
        await self.bus.publish(Message(
            type=MessageType.STATUS,
            from_id="__orchestrator__",
            payload={
                "event": "task_dead_lettered",
                "task_id": task.id,
                "prompt": task.prompt,
                "retry_count": retry_count,
                "reason": reason,
            },
        ))
        logger.warning(
            "Task %s dead-lettered after %d retries: %s", task.id, retry_count, reason
        )

    def list_dlq(self) -> list[dict]:
        """Return the dead letter queue contents (snapshot)."""
        return list(self._dlq)

    # ------------------------------------------------------------------
    # Watchdog loop
    # ------------------------------------------------------------------

    async def _watchdog_loop(self, *, poll: float = 10.0) -> None:
        """Periodically detect agents stuck BUSY beyond 1.5× task_timeout.

        Publishes a synthetic RESULT with ``error="watchdog_timeout"`` so the
        existing ``_route_loop`` → ``registry.record_result`` → circuit-breaker
        path handles recovery without special-casing.

        Reference: Nygard "Release It!" (2018) Ch. 5 — Stability Patterns.
        """
        while True:
            try:
                await asyncio.sleep(poll)
            except asyncio.CancelledError:
                break
            timed_out = self.registry.find_timed_out_agents(self.config.task_timeout)
            for agent_id in timed_out:
                agent = self.registry.get(agent_id)
                if agent is None:
                    continue
                task = agent._current_task
                task_id = task.id if task else "unknown"
                logger.warning(
                    "Watchdog: agent %s has been BUSY for >%.0fs on task %s — injecting timeout",
                    agent_id, self.config.task_timeout * 1.5, task_id,
                )
                await self.bus.publish(Message(
                    type=MessageType.RESULT,
                    from_id=agent_id,
                    payload={"task_id": task_id, "error": "watchdog_timeout", "output": None},
                ))

    # ------------------------------------------------------------------
    # Task timeout escalation (v1.2.13)
    # ------------------------------------------------------------------

    async def _handle_task_timeout(self, task: "Task", timed_out_agent_id: str) -> None:
        """Re-queue or fail a timed-out task based on escalation policy.

        When ``task_escalation_enabled=True`` and the task has not yet reached
        ``max_task_escalations``, the task is re-queued at higher priority
        (priority - 1) with the timed-out agent added to ``excluded_agents``.
        A ``task_escalated`` STATUS event is published so webhooks and the TUI
        can observe the escalation.

        When escalation is disabled or exhausted, the task is allowed to fall
        through to the normal failure path (caller handles this by returning
        ``True`` = "escalated" / ``False`` = "must fail").

        Returns
        -------
        bool
            ``True`` if the task was escalated (re-queued), ``False`` if it
            must be treated as a final failure.

        Design reference:
        - GitGuardian "Celery Task Resilience" (2024) — escalating retry;
        - Temporal WorkflowTaskTimeout reassignment (2024) — avoid stuck worker;
        - Wikipedia "Aging (scheduling)" — priority bump on re-queue;
        - AWS Builders Library "Timeouts, retries and backoff with jitter" (2022);
        - DESIGN.md §10.89 (v1.2.13)
        """
        import dataclasses as _dc  # noqa: PLC0415

        cfg = self.config
        if (
            not cfg.task_escalation_enabled
            or task.escalation_count >= cfg.max_task_escalations
        ):
            return False

        new_priority = max(0, task.priority - 1)
        escalated = _dc.replace(
            task,
            escalation_count=task.escalation_count + 1,
            excluded_agents=list(task.excluded_agents) + [timed_out_agent_id],
            priority=new_priority,
        )
        self._task_seq += 1
        await self._task_queue.put((new_priority, self._task_seq, escalated))
        # Update priority tracking for inheritance.
        self._task_priorities[escalated.id] = new_priority
        # Keep active task reference updated.
        self._active_tasks[escalated.id] = escalated

        await self.bus.publish(Message(
            type=MessageType.STATUS,
            from_id="__orchestrator__",
            payload={
                "event": "task_escalated",
                "task_id": task.id,
                "escalation_count": escalated.escalation_count,
                "excluded_agents": escalated.excluded_agents,
                "new_priority": new_priority,
                "timed_out_agent_id": timed_out_agent_id,
            },
        ))
        # Webhook: task_escalated event
        asyncio.create_task(
            self._webhook_manager.deliver("task_escalated", {
                "task_id": task.id,
                "escalation_count": escalated.escalation_count,
                "excluded_agents": escalated.excluded_agents,
                "new_priority": new_priority,
                "timed_out_agent_id": timed_out_agent_id,
            }),
            name=f"wh-task-escalated-{task.id[:8]}",
        )
        logger.info(
            "Task %s escalated (attempt %d/%d) — re-queued at priority %d "
            "excluding agent %s",
            task.id,
            escalated.escalation_count,
            cfg.max_task_escalations,
            new_priority,
            timed_out_agent_id,
        )
        return True

    # ------------------------------------------------------------------
    # ERROR state recovery loop
    # ------------------------------------------------------------------

    async def _recovery_loop(
        self,
        *,
        poll: float = 2.0,
        backoff_base: float = 5.0,
        max_attempts: int = 3,
    ) -> None:
        """Detect agents in ERROR state and attempt to restart them.

        Recovery strategy (Erlang OTP supervisor restart_one_for_one pattern):
        - Poll all registered agents for ERROR status.
        - For each ERROR agent not already permanently failed:
          - Increment per-agent attempt counter.
          - If attempts > max_attempts: mark permanently failed, publish
            ``agent_recovery_failed`` STATUS event, skip.
          - Otherwise: compute exponential backoff = ``backoff_base ^ attempt``
            seconds, stop the agent, wait, restart it.
          - On success (agent reaches IDLE): reset attempt counter, publish
            ``agent_recovered`` STATUS event.

        Reference:
        - Erlang OTP supervisor behaviour: https://www.erlang.org/docs/24/design_principles/sup_princ
        - Nygard "Release It!" (2018) Ch. 5 — Stability Patterns (Timeout + Restart)
        - DESIGN.md §10.8 (v0.12.0, 2026-03-05)
        """
        while True:
            try:
                await asyncio.sleep(poll)
            except asyncio.CancelledError:
                break

            for agent_id, agent in list(self.registry.all_agents().items()):
                if agent.status != AgentStatus.ERROR:
                    continue
                if agent_id in self._permanently_failed:
                    continue

                attempt = self._recovery_attempts.get(agent_id, 0) + 1
                self._recovery_attempts[agent_id] = attempt

                if attempt > max_attempts:
                    self._permanently_failed.add(agent_id)
                    logger.error(
                        "Recovery: agent %s permanently failed after %d attempts",
                        agent_id, max_attempts,
                    )
                    await self.bus.publish(Message(
                        type=MessageType.STATUS,
                        from_id="__orchestrator__",
                        payload={
                            "event": "agent_recovery_failed",
                            "agent_id": agent_id,
                            "attempts": attempt - 1,
                        },
                    ))
                    continue

                backoff = backoff_base ** attempt
                logger.warning(
                    "Recovery: agent %s in ERROR (attempt %d/%d) — restarting in %.1fs",
                    agent_id, attempt, max_attempts, backoff,
                )

                try:
                    await agent.stop()
                except Exception:  # noqa: BLE001
                    logger.exception("Recovery: error stopping agent %s", agent_id)

                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    return

                try:
                    await agent.start()
                except Exception:  # noqa: BLE001
                    logger.exception("Recovery: error restarting agent %s", agent_id)
                    agent.status = AgentStatus.ERROR
                    continue

                # Give the agent a moment to reach IDLE
                for _ in range(int(min(backoff * 2, 10) / poll) + 5):
                    await asyncio.sleep(poll)
                    if agent.status == AgentStatus.IDLE:
                        break

                if agent.status == AgentStatus.IDLE:
                    self._recovery_attempts.pop(agent_id, None)
                    logger.info("Recovery: agent %s successfully restarted", agent_id)
                    await self.bus.publish(Message(
                        type=MessageType.STATUS,
                        from_id="__orchestrator__",
                        payload={
                            "event": "agent_recovered",
                            "agent_id": agent_id,
                            "attempt": attempt,
                        },
                    ))
                else:
                    logger.warning(
                        "Recovery: agent %s did not reach IDLE after restart (status=%s)",
                        agent_id, agent.status,
                    )

    # ------------------------------------------------------------------
    # TTL Reaper loop
    # ------------------------------------------------------------------

    async def _ttl_reaper_loop(self, *, poll: float = 1.0) -> None:
        """Background task that expires waiting tasks whose TTL has elapsed.

        Tasks that enter ``_waiting_tasks`` (held for dependency resolution)
        never pass through ``_dispatch_loop``, so a separate reaper is needed
        to expire them.  The reaper runs every *poll* seconds (default 1 s,
        configurable via ``OrchestratorConfig.ttl_reaper_poll``).

        Expired tasks are removed from ``_waiting_tasks``, added to
        ``_failed_tasks``, and cascade-failed to their own dependents via
        ``_on_dep_failed()`` (same path as task failure).  A ``task_expired``
        STATUS event is published for each expired task.

        Design decisions:
        - Only ``_waiting_tasks`` is scanned here; queued tasks are checked in
          ``_dispatch_loop`` at dequeue time (lazy expiry, more efficient than
          periodic queue scan).
        - Poll interval of 1 s is a reasonable default — sub-second TTLs are
          unusual for agentic tasks (typical TTL range: seconds to minutes).
        - Already-cancelled tasks (in ``_cancelled_task_ids``) are skipped.

        Reference: RabbitMQ TTL — "The server guarantees that expired messages
        will not be delivered" (rabbitmq.com/docs/ttl);
        Azure Service Bus ExpiresAtUtc (Microsoft Docs 2024);
        DESIGN.md §10.28 (v0.33.0)
        """
        while True:
            try:
                await asyncio.sleep(poll)
            except asyncio.CancelledError:
                break

            now = time.time()
            expired_ids = [
                tid
                for tid, t in list(self._waiting_tasks.items())
                if t.expires_at is not None and now > t.expires_at
                and tid not in self._cancelled_task_ids
            ]
            for tid in expired_ids:
                task = self._waiting_tasks.pop(tid, None)
                if task is None:
                    # Already removed by another path (race window)
                    continue
                # Clean up reverse-lookup entries for this task
                for dep in task.depends_on:
                    if dep in self._task_dependents:
                        try:
                            self._task_dependents[dep].remove(tid)
                        except ValueError:
                            pass
                await self._expire_task(task, from_reaper=True)

    # ------------------------------------------------------------------
    # Supervision callback
    # ------------------------------------------------------------------

    async def _on_internal_failure(self, name: str, exc: Exception) -> None:
        """Called when a supervised internal task exhausts all restart attempts."""
        await self.bus.publish(Message(
            type=MessageType.STATUS,
            from_id="__orchestrator__",
            payload={"event": "internal_failure", "task_name": name, "error": str(exc)},
        ))

    # ------------------------------------------------------------------
    # Message router (P2P gating)
    # ------------------------------------------------------------------

    async def _route_loop(self) -> None:
        assert self._bus_queue is not None
        while True:
            try:
                msg = await self._bus_queue.get()
            except asyncio.CancelledError:
                break
            if msg.type == MessageType.PEER_MSG and not msg.payload.get("_forwarded"):
                await self.route_message(msg)
            elif msg.type == MessageType.CONTROL and msg.to_id == "__orchestrator__":
                asyncio.create_task(self._handle_control(msg))
            elif msg.type == MessageType.RESULT:
                task_id = msg.payload.get("task_id")
                # Discard RESULTs for cancelled in-progress tasks.
                # When cancel_task() finds a task in _active_tasks it adds its id
                # to _cancelled_task_ids and sends interrupt(); the eventual RESULT
                # from the agent must be silently dropped so workflow callbacks and
                # reply_to routing are never triggered for a cancelled task.
                if task_id and task_id in self._cancelled_task_ids:
                    self._cancelled_task_ids.discard(task_id)
                    self._active_tasks.pop(task_id, None)
                    self._task_started_at.pop(task_id, None)
                    self._task_started_prompt.pop(task_id, None)
                    self._task_reply_to.pop(task_id, None)
                    self._bus_queue.task_done()
                    logger.info("Task %s result discarded (cancelled in-progress)", task_id)
                    continue
                self._buffer_director_result(msg)
                error = msg.payload.get("error")
                self.registry.record_result(msg.from_id, error=bool(error))
                # Auto-restart tracking (v1.2.12): reset consecutive failure
                # counter on success; checked below on final failure.
                if not error:
                    self._consecutive_failures[msg.from_id] = 0
                if not error and task_id:
                    self._completed_tasks.add(task_id)
                    # Checkpoint: remove completed task so it isn't re-queued on resume.
                    if self._checkpoint_store is not None:
                        self._checkpoint_store.remove_task(task_id=task_id)
                    wf_status_before = self._workflow_manager.get_workflow_status_for_task(task_id)
                    self._workflow_manager.on_task_complete(task_id)
                    # Checkpoint: update workflow state after task completes.
                    if self._checkpoint_store is not None:
                        wf_id = self._workflow_manager.get_workflow_id_for_task(task_id)
                        if wf_id and wf_id in self._workflow_manager._runs:
                            self._checkpoint_store.save_workflow(
                                run=self._workflow_manager._runs[wf_id]
                            )
                    # Clean up active task tracking on success.
                    self._active_tasks.pop(task_id, None)
                    # Webhook: task_complete event
                    asyncio.create_task(
                        self._webhook_manager.deliver("task_complete", {
                            "task_id": task_id,
                            "agent_id": msg.from_id,
                            "output": (msg.payload.get("output") or "")[:2000],
                        }),
                        name=f"wh-task-complete-{task_id[:8]}",
                    )
                    # Webhook: workflow_complete event if workflow just finished
                    wf_status_after = self._workflow_manager.get_workflow_status_for_task(task_id)
                    if wf_status_after == "complete" and wf_status_before != "complete":
                        wf_id = self._workflow_manager.get_workflow_id_for_task(task_id)
                        asyncio.create_task(
                            self._webhook_manager.deliver("workflow_complete", {
                                "workflow_id": wf_id,
                                "task_id": task_id,
                            }),
                            name=f"wh-wf-complete-{task_id[:8]}",
                        )
                    elif wf_status_after == "failed" and wf_status_before != "failed":
                        wf_id = self._workflow_manager.get_workflow_id_for_task(task_id)
                        asyncio.create_task(
                            self._webhook_manager.deliver("workflow_failed", {
                                "workflow_id": wf_id,
                                "task_id": task_id,
                            }),
                            name=f"wh-wf-failed-{task_id[:8]}",
                        )
                    # Wake up any tasks waiting on this dependency.
                    # Reference: GNU Make prerequisite resolution; Dask task graph;
                    # Apache Spark DAG scheduler. DESIGN.md §10.24 (v0.29.0)
                    await self._on_dep_satisfied(task_id)
                elif error and task_id:
                    # Task timeout escalation (v1.2.13): for watchdog_timeout errors,
                    # attempt to escalate (re-queue at higher priority, exclude the
                    # timed-out agent) before falling through to the failure path.
                    # Normal retries (max_retries) are separate from escalations —
                    # escalation is a "try a different agent" mechanism, retries are
                    # "try again on any agent".
                    task_for_escalation = self._active_tasks.get(task_id)
                    if (
                        error == "watchdog_timeout"
                        and task_for_escalation is not None
                    ):
                        escalated = await self._handle_task_timeout(
                            task_for_escalation, msg.from_id
                        )
                        if escalated:
                            self._bus_queue.task_done()
                            continue
                    # Check if task has retries remaining before dead-lettering.
                    task = self._active_tasks.get(task_id)
                    if task is not None and task.retry_count < task.max_retries:
                        # Retry: increment counter, re-enqueue, publish task_retrying event.
                        task.retry_count += 1
                        self._task_seq += 1
                        await self._task_queue.put((task.priority, self._task_seq, task))
                        self._workflow_manager.on_task_retrying(task_id)
                        await self.bus.publish(Message(
                            type=MessageType.STATUS,
                            from_id="__orchestrator__",
                            payload={
                                "event": "task_retrying",
                                "task_id": task_id,
                                "retry_count": task.retry_count,
                                "max_retries": task.max_retries,
                                "error": error,
                            },
                        ))
                        # Webhook: task_retrying event
                        asyncio.create_task(
                            self._webhook_manager.deliver("task_retrying", {
                                "task_id": task_id,
                                "retry_count": task.retry_count,
                                "max_retries": task.max_retries,
                                "error": error,
                                "agent_id": msg.from_id,
                            }),
                            name=f"wh-task-retrying-{task_id[:8]}",
                        )
                        logger.info(
                            "Task %s retry %d/%d after error: %s",
                            task_id, task.retry_count, task.max_retries, error,
                        )
                        self._bus_queue.task_done()
                        continue
                    else:
                        wf_status_before_fail = self._workflow_manager.get_workflow_status_for_task(task_id)
                        self._workflow_manager.on_task_failed(task_id)
                        self._active_tasks.pop(task_id, None)
                        # Mark as finally failed and cascade to waiting dependents.
                        # Reference: Apache Airflow upstream_failed state;
                        # GNU Make prerequisite failure propagation.
                        # DESIGN.md §10.24 (v0.29.0)
                        self._failed_tasks.add(task_id)
                        # Checkpoint: remove failed task from persistence.
                        if self._checkpoint_store is not None:
                            self._checkpoint_store.remove_task(task_id=task_id)
                        await self._on_dep_failed(task_id)
                        # Webhook: task_failed event
                        asyncio.create_task(
                            self._webhook_manager.deliver("task_failed", {
                                "task_id": task_id,
                                "agent_id": msg.from_id,
                                "error": error,
                            }),
                            name=f"wh-task-failed-{task_id[:8]}",
                        )
                        # Webhook: workflow_failed event if workflow just failed
                        wf_status_after_fail = self._workflow_manager.get_workflow_status_for_task(task_id)
                        if wf_status_after_fail == "failed" and wf_status_before_fail != "failed":
                            wf_id = self._workflow_manager.get_workflow_id_for_task(task_id)
                            asyncio.create_task(
                                self._webhook_manager.deliver("workflow_failed", {
                                    "workflow_id": wf_id,
                                    "task_id": task_id,
                                    "error": error,
                                }),
                                name=f"wh-wf-failed-err-{task_id[:8]}",
                            )
                        # Auto-restart: track consecutive failures and trigger
                        # _restart_agent() when threshold is reached.
                        # Ephemeral agents and disabled agents are handled inside
                        # _restart_agent() itself.
                        # Reference: DESIGN.md §10.88 (v1.2.12)
                        _failed_agent_id = msg.from_id
                        _failed_agent_cfg = self._get_agent_config(_failed_agent_id)
                        if (
                            _failed_agent_cfg is not None
                            and _failed_agent_cfg.max_consecutive_failures > 0
                            and _failed_agent_id not in self._ephemeral_agents
                        ):
                            self._consecutive_failures[_failed_agent_id] = (
                                self._consecutive_failures.get(_failed_agent_id, 0) + 1
                            )
                            if (
                                self._consecutive_failures[_failed_agent_id]
                                >= _failed_agent_cfg.max_consecutive_failures
                            ):
                                asyncio.ensure_future(
                                    self._restart_agent(_failed_agent_id)
                                )
                # Record task in per-agent history.
                self._record_agent_history(msg)
                # reply_to routing: deliver RESULT to the requesting agent's mailbox.
                # This closes the feedback loop for multi-level hierarchies where a
                # parent agent submits a task and needs the result in its inbox.
                if task_id:
                    asyncio.create_task(
                        self._route_result_reply(task_id, msg),
                        name=f"reply-to-route-{task_id[:8]}",
                    )
                # Broadcast group handling (v1.2.15): update BroadcastGroup state
                # when a task that belongs to a broadcast completes or fails.
                # Race mode: first success → mark winner, cancel remaining tasks.
                # Gather mode: collect all results; mark complete when all finish.
                # Design reference: DESIGN.md §10.91 (v1.2.15)
                if task_id and task_id in self._task_to_broadcast:
                    _bc_id = self._task_to_broadcast.pop(task_id)
                    _bc = self._broadcast_groups.get(_bc_id)
                    if _bc is not None and not _bc.cancelled:
                        _error = msg.payload.get("error")
                        if not _error:
                            _output = msg.payload.get("output", "") or ""
                            _bc.completed_tasks[task_id] = _output
                            _bc.status = "running"
                            if _bc.mode == "race" and _bc.winner_task_id is None:
                                # First success wins — cancel all remaining tasks.
                                _bc.winner_task_id = task_id
                                _bc.status = "complete"
                                _bc.cancelled = True
                                for _other_tid in list(_bc.task_ids):
                                    if (
                                        _other_tid != task_id
                                        and _other_tid not in _bc.completed_tasks
                                        and _other_tid not in _bc.failed_tasks
                                    ):
                                        asyncio.create_task(
                                            self.cancel_task(_other_tid),
                                            name=f"bc-cancel-{_other_tid[:8]}",
                                        )
                                logger.info(
                                    "Broadcast %s complete (race): winner=%s",
                                    _bc_id, task_id,
                                )
                            elif _bc.mode == "gather":
                                # Gather: check if ALL tasks are resolved.
                                _resolved = (
                                    len(_bc.completed_tasks) + len(_bc.failed_tasks)
                                )
                                if _resolved >= len(_bc.task_ids):
                                    _bc.status = "complete"
                                    logger.info(
                                        "Broadcast %s complete (gather): %d results",
                                        _bc_id, len(_bc.completed_tasks),
                                    )
                        else:
                            # Task failed — record and check gather completion.
                            _bc.failed_tasks.add(task_id)
                            _bc.status = "running"
                            if _bc.mode == "gather":
                                _resolved = (
                                    len(_bc.completed_tasks) + len(_bc.failed_tasks)
                                )
                                if _resolved >= len(_bc.task_ids):
                                    _bc.status = (
                                        "complete"
                                        if _bc.completed_tasks
                                        else "failed"
                                    )
                                    logger.info(
                                        "Broadcast %s %s (gather, all resolved)",
                                        _bc_id, _bc.status,
                                    )
                            elif _bc.mode == "race":
                                # All failed in race mode?
                                _all_failed = all(
                                    tid in _bc.failed_tasks
                                    or tid in _bc.completed_tasks
                                    for tid in _bc.task_ids
                                )
                                if _all_failed and not _bc.completed_tasks:
                                    _bc.status = "failed"
                                    logger.info(
                                        "Broadcast %s failed (race, all tasks failed)",
                                        _bc_id,
                                    )
                # Drain auto-stop: if the agent that sent this RESULT is in drain mode,
                # stop it now that its task has completed.
                # Reference: Kubernetes terminationGracePeriodSeconds; DESIGN.md §10.23 (v0.28.0)
                drain_agent_id = msg.from_id
                if drain_agent_id in self._draining_agents:
                    drained_agent = self.registry.get(drain_agent_id)
                    if drained_agent is not None:
                        try:
                            await drained_agent.stop()
                        except Exception:  # noqa: BLE001
                            logger.exception("Drain: error stopping agent %s", drain_agent_id)
                        self.registry.unregister(drain_agent_id)
                    self._draining_agents.discard(drain_agent_id)
                    await self.bus.publish(Message(
                        type=MessageType.STATUS,
                        from_id="__orchestrator__",
                        payload={"event": "agent_drained", "agent_id": drain_agent_id},
                    ))
                    logger.info("Agent %s drained and stopped after task completion", drain_agent_id)
                # Ephemeral auto-stop: if the agent that sent this RESULT is ephemeral,
                # stop it now that its task has completed and unregister from registry.
                # Mirrors the drain pattern but triggered by agent_template at phase
                # dispatch time rather than by manual drain_agent() calls.
                # Design reference: DESIGN.md §10.79 (v1.2.3) — PhaseSpec.agent_template.
                elif drain_agent_id in self._ephemeral_agents:
                    ephemeral_agent = self.registry.get(drain_agent_id)
                    if ephemeral_agent is not None:
                        try:
                            await ephemeral_agent.stop()
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "Ephemeral: error stopping agent %s", drain_agent_id
                            )
                        self.registry.unregister(drain_agent_id)
                    self._ephemeral_agents.discard(drain_agent_id)
                    await self.bus.publish(Message(
                        type=MessageType.STATUS,
                        from_id="__orchestrator__",
                        payload={
                            "event": "ephemeral_agent_stopped",
                            "agent_id": drain_agent_id,
                        },
                    ))
                    logger.info(
                        "Ephemeral agent %s stopped after task completion", drain_agent_id
                    )
            elif msg.type == MessageType.STATUS:
                # Webhook: agent_status event for IDLE/BUSY/ERROR transitions.
                # Fires a non-blocking background deliver for each registered webhook
                # whose event list includes "agent_status" or "*".
                # Design reference:
                #   DESIGN.md §10.N (v1.0.21 — Webhook コールバック)
                #   GitHub Webhooks best practices: fire-and-forget, at-most-once.
                event_name = msg.payload.get("event", "")
                if event_name in {"agent_busy", "agent_idle", "agent_error"}:
                    asyncio.create_task(
                        self._webhook_manager.deliver("agent_status", {
                            "agent_id": msg.payload.get("agent_id", msg.from_id),
                            "status": msg.payload.get("status"),
                            "event": event_name,
                            "task_id": msg.payload.get("task_id"),
                        }),
                        name=f"wh-agent-status-{msg.from_id[:8]}-{event_name}",
                    )
                elif event_name == "agent_drift_warning":
                    # Automatic re-brief: inject a role reminder into the drifted
                    # agent's pane.  The handler respects the per-agent cooldown
                    # to avoid spamming agents that remain below the drift threshold.
                    # Reference: Rath arXiv:2601.04170 — drift-aware routing;
                    # arXiv:2603.03258 — goal reminder injection.
                    # DESIGN.md §10.50 (v1.1.18)
                    drifted_agent_id = msg.payload.get("agent_id", "")
                    drift_score = msg.payload.get("drift_score", 0.0)
                    if drifted_agent_id:
                        asyncio.create_task(
                            self._handle_drift_warning(drifted_agent_id, drift_score),
                            name=f"drift-rebrief-{drifted_agent_id[:16]}",
                        )
            self._bus_queue.task_done()

    # ------------------------------------------------------------------
    # Dependency tracking helpers (v0.29.0)
    # ------------------------------------------------------------------

    async def _on_dep_satisfied(self, completed_task_id: str) -> None:
        """Called when *completed_task_id* succeeds.

        For each waiting task that listed *completed_task_id* as a dependency,
        check whether ALL of its dependencies are now in ``_completed_tasks``.
        If so, move the task from ``_waiting_tasks`` to the priority queue.

        Reference: GNU Make prerequisite resolution; Tomasulo's algorithm;
        Dask task graph scheduler; Apache Spark DAG scheduler.
        DESIGN.md §10.24 (v0.29.0)
        """
        waiting_ids = list(self._task_dependents.pop(completed_task_id, []))
        for waiting_id in waiting_ids:
            waiting_task = self._waiting_tasks.get(waiting_id)
            if waiting_task is None:
                # Already released or cancelled
                continue
            still_unmet = [
                dep for dep in waiting_task.depends_on
                if dep not in self._completed_tasks
            ]
            if not still_unmet:
                # All deps now satisfied — move to queue
                del self._waiting_tasks[waiting_id]
                self._task_seq += 1
                await self._task_queue.put(
                    (waiting_task.priority, self._task_seq, waiting_task)
                )
                await self.bus.publish(Message(
                    type=MessageType.STATUS,
                    from_id="__orchestrator__",
                    payload={
                        "event": "task_queued",
                        "task_id": waiting_id,
                        "prompt": waiting_task.prompt,
                        "released_by": completed_task_id,
                    },
                ))
                logger.info(
                    "Task %s released from waiting (dep %s completed)",
                    waiting_id, completed_task_id,
                )
                # Remove this waiting_id from any remaining dep reverse-lookups.
                for dep in waiting_task.depends_on:
                    if dep != completed_task_id and dep in self._task_dependents:
                        try:
                            self._task_dependents[dep].remove(waiting_id)
                        except ValueError:
                            pass

    async def _on_dep_failed(self, failed_task_id: str) -> None:
        """Called when *failed_task_id* finally fails (retries exhausted).

        Cascades failure to all tasks waiting on *failed_task_id*:
        - Removes them from ``_waiting_tasks``
        - Adds them to ``_failed_tasks``
        - Publishes STATUS ``task_dependency_failed`` for each
        - Recursively cascades to their own dependents (A→B→C cascade)

        Reference: Apache Airflow upstream_failed state; GNU Make propagated
        prerequisite failure; POSIX make prerequisites; DESIGN.md §10.24 (v0.29.0)
        """
        waiting_ids = list(self._task_dependents.pop(failed_task_id, []))
        for waiting_id in waiting_ids:
            if waiting_id not in self._waiting_tasks:
                # Already handled (e.g., cancelled)
                continue
            waiting_task = self._waiting_tasks.pop(waiting_id)
            self._failed_tasks.add(waiting_id)
            # Clean up any other reverse-lookup entries for this task
            for dep in waiting_task.depends_on:
                if dep != failed_task_id and dep in self._task_dependents:
                    try:
                        self._task_dependents[dep].remove(waiting_id)
                    except ValueError:
                        pass
            await self.bus.publish(Message(
                type=MessageType.STATUS,
                from_id="__orchestrator__",
                payload={
                    "event": "task_dependency_failed",
                    "task_id": waiting_id,
                    "failed_dep": failed_task_id,
                    "error": f"dependency_failed:{failed_task_id}",
                },
            ))
            asyncio.create_task(
                self._webhook_manager.deliver("task_dependency_failed", {
                    "task_id": waiting_id,
                    "failed_dep": failed_task_id,
                    "error": f"dependency_failed:{failed_task_id}",
                }),
                name=f"wh-dep-failed-{waiting_id[:8]}",
            )
            logger.warning(
                "Task %s failed: dependency %s failed (cascade)",
                waiting_id, failed_task_id,
            )
            # Recurse: cascade failures to tasks waiting on waiting_id
            await self._on_dep_failed(waiting_id)

    def _task_blocking(self, task_id: str) -> list[str]:
        """Return the list of task IDs that are waiting on *task_id*.

        Used by REST ``GET /tasks/{task_id}`` to expose the ``blocking`` field.
        """
        return list(self._task_dependents.get(task_id, []))

    def _buffer_director_result(self, result_msg: Message) -> None:
        """Buffer a worker RESULT for injection into the next Director chat turn."""
        if self.registry.get_director() is None:
            return
        payload = result_msg.payload
        agent_id = result_msg.from_id
        task_id = payload.get("task_id", "?")
        error = payload.get("error")
        if error:
            summary = f"[agent={agent_id} task={task_id}] ERROR: {error}"
        else:
            output = payload.get("output") or ""
            lines = output.splitlines()
            total_lines = len(lines)
            TAIL_LINES = 40
            if len(lines) > TAIL_LINES:
                tail = "\n".join(lines[-TAIL_LINES:])
                summary = f"[agent={agent_id} task={task_id} lines={TAIL_LINES}/{total_lines}]\n{tail}"
            else:
                summary = f"[agent={agent_id} task={task_id}]\n{output}"
        self._director_pending.append(summary)
        logger.debug("Buffered worker result for director: agent=%s task=%s", agent_id, task_id)

    def _record_agent_history(self, result_msg: Message) -> None:
        """Append a completed task record to *agent_id*'s history.

        Records are kept in chronological order (oldest first) and capped at
        200 entries.  ``get_agent_history()`` reverses them for the caller.

        Duration is computed using ``_task_started_at`` populated by the
        dispatch loop.  If no start time is recorded (e.g., watchdog injection),
        duration_s is None.
        """
        agent_id = result_msg.from_id
        payload = result_msg.payload
        task_id = payload.get("task_id")
        if task_id is None:
            return

        now = time.monotonic()
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        started_ts = self._task_started_at.pop(task_id, None)
        prompt = self._task_started_prompt.pop(task_id, "")
        task_timeout_val = self._task_timeout.pop(task_id, None)

        if started_ts is not None:
            duration_s = round(now - started_ts, 3)
            started_iso = datetime.fromtimestamp(
                datetime.now(tz=timezone.utc).timestamp() - duration_s,
                tz=timezone.utc,
            ).isoformat()
        else:
            duration_s = None
            started_iso = None

        error = payload.get("error") or None
        record: dict = {
            "task_id": task_id,
            "prompt": prompt,
            "started_at": started_iso,
            "finished_at": now_iso,
            "duration_s": duration_s,
            "status": "error" if error else "success",
            "error": error,
            "timeout": task_timeout_val,
        }

        history = self._agent_history.setdefault(agent_id, [])
        history.append(record)
        # Cap at 200 entries: keep the newest 200.
        if len(history) > 200:
            self._agent_history[agent_id] = history[-200:]

        # Persist to the append-only result store when enabled.
        # Event Sourcing: every task completion is an immutable fact on disk.
        # Reference: Fowler "Event Sourcing" (2005); DESIGN.md §10.19 (v0.24.0)
        if self._result_store is not None:
            result_text = (payload.get("output") or "")[:4000]
            try:
                self._result_store.append(
                    task_id=task_id,
                    agent_id=agent_id,
                    prompt=(prompt or "")[:500],
                    result_text=result_text,
                    error=error,
                    duration_s=duration_s if duration_s is not None else 0.0,
                )
            except Exception:
                logger.exception("ResultStore.append() failed for task=%s agent=%s", task_id, agent_id)

    async def _route_result_reply(self, task_id: str, result_msg: Message) -> None:
        """Deliver *result_msg* to the reply_to agent's mailbox + notify_stdin.

        When a task was submitted with ``reply_to="<agent_id>"``, the orchestrator
        records ``task_id → reply_to`` in ``_task_reply_to``.  On RESULT, this
        method looks up the mapping and:

        1. Writes the RESULT message to the reply_to agent's mailbox file.
        2. Calls ``agent.notify_stdin("__MSG__:<msg_id>")`` so the agent's
           ``_message_loop`` triggers and the operator slash commands work.

        If the reply_to agent is not registered (already stopped, or an external
        agent ID), the mailbox write is still attempted if ``self._mailbox`` is
        set, but ``notify_stdin`` is skipped gracefully.

        The ``_task_reply_to`` entry is cleaned up after delivery to prevent
        unbounded growth.

        Design: request-reply pattern with correlation IDs — the task_id is the
        correlation identifier that links the RESULT back to the originating agent.
        Reference: "Learning Notes #15 – Request Reply Pattern | RabbitMQ" (2024)
        Moore, David J. "A Taxonomy of Hierarchical Multi-Agent Systems" (2025)
        """
        reply_to_id = self._task_reply_to.pop(task_id, None)
        if reply_to_id is None:
            return

        logger.debug(
            "Result-reply: routing task %s result to agent %s", task_id, reply_to_id
        )

        # Write to mailbox if available
        if self._mailbox is not None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None, self._mailbox.write, reply_to_id, result_msg
                )
                logger.debug(
                    "Result-reply: wrote result for task %s to mailbox of %s",
                    task_id, reply_to_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Result-reply: failed to write mailbox for agent %s", reply_to_id
                )

        # Notify the agent if it is registered
        agent = self.registry.get(reply_to_id)
        if agent is not None:
            try:
                await agent.notify_stdin(f"__MSG__:{result_msg.id}")
                logger.debug(
                    "Result-reply: notified agent %s of result for task %s",
                    reply_to_id, task_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Result-reply: failed to notify_stdin for agent %s", reply_to_id
                )
        else:
            logger.warning(
                "Result-reply: reply_to agent %r not registered — mailbox written "
                "but notify_stdin skipped",
                reply_to_id,
            )

    async def route_message(self, msg: Message) -> None:
        """Forward a PEER_MSG if the sender/receiver pair is permitted."""
        permitted, reason = self.registry.is_p2p_permitted(msg.from_id, msg.to_id)

        if permitted:
            routed = Message(
                type=MessageType.PEER_MSG,
                from_id=msg.from_id,
                to_id=msg.to_id,
                payload={**msg.payload, "_forwarded": True},
            )
            await self.bus.publish(routed)
            logger.debug("P2P %s → %s forwarded (%s)", msg.from_id, msg.to_id, reason)
        else:
            logger.warning(
                "P2P %s → %s blocked (not in hierarchy or permission table)",
                msg.from_id,
                msg.to_id,
            )

    # ------------------------------------------------------------------
    # Control message handling (sub-agent spawning)
    # ------------------------------------------------------------------

    async def _handle_control(self, msg: Message) -> None:
        """Dispatch CONTROL messages addressed to ``__orchestrator__``."""
        action = msg.payload.get("action")
        if action == "spawn_subagent":
            parent_id = msg.from_id
            template_id = msg.payload.get("template_id", "")
            share_parent = msg.payload.get("share_parent_worktree", False)
            template_cfg = next(
                (a for a in self.config.agents if a.id == template_id), None
            )
            if template_cfg is None:
                logger.error(
                    "spawn_subagent: template_id %r not found in config", template_id
                )
                return
            await self._spawn_subagent(parent_id, template_cfg, share_parent=share_parent)
        elif action == "create_agent":
            # Dynamic agent creation — no pre-configured template needed.
            # Sent by a Director agent that wants to spawn a specialist worker at
            # runtime.  The CONTROL payload mirrors create_agent() keyword args.
            parent_id = msg.from_id
            try:
                await self.create_agent(
                    agent_id=msg.payload.get("agent_id"),
                    tags=msg.payload.get("tags"),
                    system_prompt=msg.payload.get("system_prompt"),
                    isolate=msg.payload.get("isolate", True),
                    merge_on_stop=msg.payload.get("merge_on_stop", False),
                    merge_target=msg.payload.get("merge_target"),
                    command=msg.payload.get("command"),
                    role=msg.payload.get("role", "worker"),
                    task_timeout=msg.payload.get("task_timeout"),
                    parent_id=parent_id,
                )
            except ValueError as exc:
                logger.error("create_agent CONTROL failed: %s", exc)
        else:
            logger.warning("Orchestrator received unknown CONTROL action: %s", action)

    async def _spawn_subagent(
        self,
        parent_id: str,
        template_cfg: "AgentConfig",
        *,
        share_parent: bool = False,
    ) -> "Agent | None":
        """Create, register, and start a sub-agent from a pre-configured template."""
        from pathlib import Path as _Path  # noqa: PLC0415

        from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

        sub_id = f"{parent_id}-sub-{uuid.uuid4().hex[:6]}"
        mailbox = Mailbox(self.config.mailbox_dir, self.config.session_name)

        parent_agent = self.registry.get(parent_id)

        cwd_override: _Path | None = None
        if share_parent and parent_agent is not None:
            cwd_override = parent_agent.worktree_path

        effective_wm = self._worktree_manager if cwd_override is None else None
        parent_pane = parent_agent.pane if parent_agent is not None else None

        agent: Agent = ClaudeCodeAgent(
            agent_id=sub_id,
            bus=self.bus,
            tmux=self.tmux,
            mailbox=mailbox,
            worktree_manager=effective_wm,
            isolate=template_cfg.isolate,
            cwd_override=cwd_override,
            session_name=self.config.session_name,
            web_base_url=self.config.web_base_url,
            api_key=self.config.api_key,
            task_timeout=template_cfg.task_timeout if template_cfg.task_timeout is not None else self.config.task_timeout,
            role=template_cfg.role,
            command=template_cfg.command or "env -u CLAUDECODE claude --dangerously-skip-permissions",
            parent_pane=parent_pane,
            system_prompt=template_cfg.system_prompt,
            context_files=template_cfg.context_files,
            context_files_root=_Path.cwd() if template_cfg.context_files else None,
            tags=template_cfg.tags,
            merge_on_stop=template_cfg.merge_on_stop,
            merge_target=template_cfg.merge_target,
        )

        self.registry.register(agent, parent_id=parent_id)
        # Explicit P2P is auto-permitted by hierarchy, but added for robustness.
        self.registry.grant_p2p(parent_id, sub_id)
        await agent.start()

        await self.bus.publish(Message(
            type=MessageType.STATUS,
            from_id="__orchestrator__",
            to_id=parent_id,
            payload={
                "event": "subagent_spawned",
                "sub_agent_id": sub_id,
                "parent_id": parent_id,
            },
        ))
        logger.info("Sub-agent %s spawned (parent=%s)", sub_id, parent_id)
        return agent

    async def create_agent(
        self,
        *,
        agent_id: str | None = None,
        tags: list[str] | None = None,
        system_prompt: str | None = None,
        isolate: bool = True,
        merge_on_stop: bool = False,
        merge_target: str | None = None,
        command: str | None = None,
        role: str = "worker",
        task_timeout: int | None = None,
        parent_id: str | None = None,
    ) -> "Agent":
        """Create, register, and start a new agent without a pre-configured template.

        Unlike ``_spawn_subagent()``, this method accepts raw parameters so a
        Director agent (or the REST API) can instantiate specialist workers at
        runtime without any pre-declared YAML configuration.

        Parameters
        ----------
        agent_id:
            Desired agent ID.  Auto-generated as ``dyn-{hex6}`` (or
            ``{parent_id}-dyn-{hex6}`` when *parent_id* is given) if omitted.
        tags:
            Capability tags for smart dispatch (FIPA-DF pattern).
        system_prompt:
            System-level prompt written into the agent's CLAUDE.md.
        isolate:
            When ``True`` (default), the agent gets an isolated git worktree.
        command:
            Custom shell command to launch the agent (defaults to the
            ``claude --dangerously-skip-permissions`` CLI).
        role:
            ``"worker"`` or ``"director"``; defaults to ``"worker"``.
        task_timeout:
            Per-agent task timeout in seconds; falls back to config default.
        parent_id:
            ID of the parent agent.  When set, hierarchy P2P is auto-granted.

        Raises
        ------
        ValueError
            If *agent_id* is already registered.
        """
        from pathlib import Path as _Path  # noqa: PLC0415

        from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent
        from tmux_orchestrator.application.config import AgentRole

        if agent_id is None:
            prefix = f"{parent_id}-dyn" if parent_id else "dyn"
            agent_id = f"{prefix}-{uuid.uuid4().hex[:6]}"

        if self.registry.get(agent_id) is not None:
            raise ValueError(f"Agent {agent_id!r} is already registered")

        mailbox = Mailbox(self.config.mailbox_dir, self.config.session_name)

        parent_pane = None
        if parent_id:
            parent_agent = self.registry.get(parent_id)
            if parent_agent is not None:
                parent_pane = parent_agent.pane

        try:
            effective_role = AgentRole(role)
        except ValueError:
            effective_role = AgentRole.WORKER

        effective_timeout = task_timeout if task_timeout is not None else self.config.task_timeout
        effective_command = command or "env -u CLAUDECODE claude --dangerously-skip-permissions"
        effective_wm = self._worktree_manager if isolate else None

        agent: Agent = ClaudeCodeAgent(
            agent_id=agent_id,
            bus=self.bus,
            tmux=self.tmux,
            mailbox=mailbox,
            worktree_manager=effective_wm,
            isolate=isolate,
            merge_on_stop=merge_on_stop,
            merge_target=merge_target,
            session_name=self.config.session_name,
            web_base_url=self.config.web_base_url,
            api_key=self.config.api_key,
            task_timeout=effective_timeout,
            role=effective_role,
            command=effective_command,
            parent_pane=parent_pane,
            system_prompt=system_prompt,
            tags=tags or [],
        )

        self.registry.register(agent, parent_id=parent_id)
        if parent_id:
            self.registry.grant_p2p(parent_id, agent_id)
        await agent.start()

        await self.bus.publish(Message(
            type=MessageType.STATUS,
            from_id="__orchestrator__",
            to_id=parent_id or "__broadcast__",
            payload={
                "event": "agent_created",
                "agent_id": agent_id,
                "parent_id": parent_id,
            },
        ))
        logger.info("Dynamic agent %s created (parent=%s, tags=%s)", agent_id, parent_id, tags)
        return agent

    async def spawn_ephemeral_agent(
        self,
        template_id: str,
        *,
        source_branch: str | None = None,
        workflow_id: str | None = None,
    ) -> str:
        """Create, register, and start an ephemeral agent from a config template.

        An ephemeral agent is a short-lived agent scoped to a single workflow
        phase.  It is created just before the phase's tasks are dispatched and
        auto-stopped once its task completes (the same drain mechanism used by
        ``drain_agent()``).

        Parameters
        ----------
        template_id:
            ``id`` of an :class:`~tmux_orchestrator.application.config.AgentConfig`
            in ``self.config.agents`` to use as the template.  The new agent
            inherits ``isolate``, ``system_prompt``, ``tags``, ``task_timeout``,
            and other fields from the template config.
        source_branch:
            Optional git branch name to branch the new agent's worktree from.
            When provided (and the template uses ``isolate=True``), the agent's
            worktree is created via :meth:`WorktreeManager.create_from_branch`
            instead of the default :meth:`WorktreeManager.setup`.  This threads
            git history through sequential phases — the new agent sees all
            commits made by the predecessor phase.

            Set by the workflow router when ``chain_branch=True`` on the current
            phase spec and a predecessor phase's ephemeral agent ID is available
            in ``_ephemeral_agent_branches``.

            When ``None`` (default), the agent's worktree starts from the repo
            HEAD (original behaviour).

        Returns
        -------
        str
            The newly created agent's ID (``f"{template_id}-ephemeral-{hex8}"``).

        Raises
        ------
        ValueError
            If no agent config with *template_id* is found.

        workflow_id:
            Optional workflow run ID.  When provided and the agent uses
            ``isolate=True``, the branch name is appended to
            ``_workflow_branches[workflow_id]`` so that
            :meth:`cleanup_workflow_branches` can delete all accumulated
            branches when the workflow completes.

            When ``None`` (default), branch tracking is skipped (existing
            behaviour for agents spawned outside a workflow context).

        Design reference: DESIGN.md §10.79 (v1.2.3), §10.81 (v1.2.5),
        §10.84 (v1.2.8 — workflow_id for branch tracking)
        Research: Kubernetes Pod-per-Job pattern; ephemeral CI agent lifecycle;
        sequential git worktree branch handoff (dredyson.com, 2025).
        """
        from pathlib import Path as _Path  # noqa: PLC0415

        from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent  # noqa: PLC0415

        # Locate the template config.
        template_cfg = next(
            (a for a in self.config.agents if a.id == template_id), None
        )
        if template_cfg is None:
            raise ValueError(
                f"spawn_ephemeral_agent: no agent config with id={template_id!r} found. "
                f"Available templates: {[a.id for a in self.config.agents]}"
            )

        ephemeral_id = f"{template_id}-ephemeral-{uuid.uuid4().hex[:8]}"

        mailbox = Mailbox(self.config.mailbox_dir, self.config.session_name)
        effective_timeout = (
            template_cfg.task_timeout
            if template_cfg.task_timeout is not None
            else self.config.task_timeout
        )
        effective_wm = self._worktree_manager if template_cfg.isolate else None

        # Ephemeral isolated agents auto-enable keep_branch_on_stop so that
        # successor chain_branch phases can branch from their committed state.
        # This can be overridden by the template config setting keep_branch_on_stop=False.
        # Design reference: DESIGN.md §10.82 (v1.2.6)
        effective_keep_branch = (
            template_cfg.keep_branch_on_stop
            or template_cfg.isolate  # auto-enable for isolated ephemeral agents
        )

        agent: Agent = ClaudeCodeAgent(
            agent_id=ephemeral_id,
            bus=self.bus,
            tmux=self.tmux,
            mailbox=mailbox,
            worktree_manager=effective_wm,
            isolate=template_cfg.isolate,
            session_name=self.config.session_name,
            web_base_url=self.config.web_base_url,
            api_key=self.config.api_key,
            task_timeout=effective_timeout,
            role=template_cfg.role,
            command=(
                template_cfg.command
                or "env -u CLAUDECODE claude --dangerously-skip-permissions"
            ),
            system_prompt=template_cfg.system_prompt,
            context_files=template_cfg.context_files,
            context_files_root=_Path.cwd() if template_cfg.context_files else None,
            context_spec_files=template_cfg.context_spec_files,
            context_spec_files_root=(
                _Path.cwd() if template_cfg.context_spec_files else None
            ),
            spec_files=template_cfg.spec_files,
            spec_files_root=_Path.cwd() if template_cfg.spec_files else None,
            tags=list(template_cfg.tags),
            merge_on_stop=False,  # ephemeral agents never merge
            cleanup_subdir=template_cfg.cleanup_subdir,
            keep_branch_on_stop=effective_keep_branch,
        )

        self.registry.register(agent)
        self._ephemeral_agents.add(ephemeral_id)

        # Branch-chain handoff (v1.2.5): when source_branch is provided and the
        # agent uses worktree isolation, tell the agent to branch from that source
        # instead of the default HEAD.  The agent's _setup_worktree() method reads
        # _source_branch and calls create_from_branch() accordingly.
        # Design reference: DESIGN.md §10.81
        if source_branch and template_cfg.isolate and effective_wm is not None:
            agent._source_branch = source_branch  # type: ignore[attr-defined]
            logger.info(
                "Ephemeral agent %s: will branch from %s (chain_branch handoff)",
                ephemeral_id,
                source_branch,
            )

        await agent.start()

        # Track the worktree branch name for sequential branch-chain handoff.
        # When the ephemeral agent uses isolate=True, it has a dedicated branch
        # named "worktree/{ephemeral_id}".  The workflow router reads this mapping
        # when chain_branch=True is set on the next sequential phase.
        # Design reference: DESIGN.md §10.80 (v1.2.4)
        branch_name = f"worktree/{ephemeral_id}" if template_cfg.isolate else ""
        if branch_name:
            self._ephemeral_agent_branches[ephemeral_id] = branch_name
            # Workflow branch tracking (v1.2.8): when a workflow_id is provided,
            # record this branch so cleanup_workflow_branches() can delete it
            # once the workflow reaches terminal state.
            if workflow_id is not None:
                self._workflow_branches.setdefault(workflow_id, []).append(branch_name)
                logger.debug(
                    "Workflow %s: tracking branch %s for cleanup on completion",
                    workflow_id,
                    branch_name,
                )

        await self.bus.publish(Message(
            type=MessageType.STATUS,
            from_id="__orchestrator__",
            payload={
                "event": "ephemeral_agent_spawned",
                "agent_id": ephemeral_id,
                "template_id": template_id,
                "branch": branch_name,
            },
        ))
        logger.info(
            "Ephemeral agent %s spawned from template %s (branch=%s)",
            ephemeral_id,
            template_id,
            branch_name or "<none>",
        )
        return ephemeral_id

    # ------------------------------------------------------------------
    # Agent auto-restart (v1.2.12)
    # ------------------------------------------------------------------

    def _get_agent_config(self, agent_id: str) -> "AgentConfig | None":
        """Return the :class:`AgentConfig` whose ``id`` matches *agent_id*, or None.

        Only static agents defined in ``config.agents`` are returned — ephemeral
        agents spawned at runtime are not in this list.

        Design reference: DESIGN.md §10.88 (v1.2.12)
        """
        return next((a for a in self.config.agents if a.id == agent_id), None)

    async def _restart_agent(self, agent_id: str) -> None:
        """Stop an unhealthy agent and start a fresh replacement with the same config.

        Called automatically when ``_consecutive_failures[agent_id]`` reaches the
        ``AgentConfig.max_consecutive_failures`` threshold and
        ``OrchestratorConfig.supervision_enabled`` is True.

        The new agent uses the same ``agent_id`` so that in-flight task routing,
        reply_to entries, and external references remain valid.

        Ephemeral agents (tracked in ``_ephemeral_agents``) are never restarted
        by this method — they are single-use by design and are cleaned up by the
        ephemeral auto-stop logic in ``_route_loop``.

        Reference: Erlang OTP one_for_one supervisor strategy (Ericsson 1996);
        AWS ECS unhealthy task replacement (AWS Blog 2023);
        Microsoft Azure Scheduler Agent Supervisor pattern (2024).
        DESIGN.md §10.88 (v1.2.12)
        """
        if not self.config.supervision_enabled:
            logger.debug(
                "Agent %s: auto-restart skipped (supervision_enabled=False)", agent_id
            )
            return

        # Never restart ephemeral agents — they are single-use.
        if agent_id in self._ephemeral_agents:
            logger.debug(
                "Agent %s: auto-restart skipped (ephemeral agent)", agent_id
            )
            return

        cfg = self._get_agent_config(agent_id)
        if cfg is None:
            logger.warning(
                "Agent %s: auto-restart skipped (no matching AgentConfig)", agent_id
            )
            return

        logger.warning(
            "Agent %s hit max_consecutive_failures=%d — restarting",
            agent_id,
            cfg.max_consecutive_failures,
        )

        # Stop the unhealthy agent.
        agent = self.registry.get(agent_id)
        if agent is not None:
            try:
                await agent.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Auto-restart: error stopping agent %s", agent_id)
            self.registry.unregister(agent_id)

        # Reset failure counter BEFORE starting the new agent so any failure
        # during startup doesn't immediately trigger another restart cycle.
        self._consecutive_failures[agent_id] = 0

        # Increment cumulative restart counter for observability.
        self._restart_counts[agent_id] = self._restart_counts.get(agent_id, 0) + 1

        # Create and start a fresh agent with the same ID and original config.
        from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent  # noqa: PLC0415
        from tmux_orchestrator.messaging import Mailbox  # noqa: PLC0415
        from pathlib import Path as _Path  # noqa: PLC0415

        mailbox = Mailbox(self.config.mailbox_dir, self.config.session_name)
        effective_timeout = (
            cfg.task_timeout
            if cfg.task_timeout is not None
            else self.config.task_timeout
        )
        effective_wm = self._worktree_manager if cfg.isolate else None

        new_agent: Agent = ClaudeCodeAgent(
            agent_id=agent_id,
            bus=self.bus,
            tmux=self.tmux,
            mailbox=mailbox,
            worktree_manager=effective_wm,
            isolate=cfg.isolate,
            session_name=self.config.session_name,
            web_base_url=self.config.web_base_url,
            api_key=self.config.api_key,
            task_timeout=effective_timeout,
            role=cfg.role,
            command=(
                cfg.command
                or "env -u CLAUDECODE claude --dangerously-skip-permissions"
            ),
            system_prompt=cfg.system_prompt,
            context_files=cfg.context_files,
            context_files_root=_Path.cwd() if cfg.context_files else None,
            context_spec_files=cfg.context_spec_files,
            context_spec_files_root=(
                _Path.cwd() if cfg.context_spec_files else None
            ),
            spec_files=cfg.spec_files,
            spec_files_root=_Path.cwd() if cfg.spec_files else None,
            tags=list(cfg.tags),
            merge_on_stop=cfg.merge_on_stop,
            merge_target=cfg.merge_target,
            cleanup_subdir=cfg.cleanup_subdir,
            keep_branch_on_stop=cfg.keep_branch_on_stop,
        )

        self.registry.register(new_agent)
        await new_agent.start()

        # Publish restart event for TUI / web hub observers.
        await self.bus.publish(Message(
            type=MessageType.STATUS,
            from_id="__orchestrator__",
            payload={
                "event": "agent_restarted",
                "agent_id": agent_id,
                "reason": "max_consecutive_failures",
                "restart_count": self._restart_counts[agent_id],
            },
        ))
        logger.info(
            "Agent %s successfully restarted (restart_count=%d)",
            agent_id,
            self._restart_counts[agent_id],
        )

    # ------------------------------------------------------------------
    # Workflow DAG tracking
    # ------------------------------------------------------------------

    def get_worktree_manager(self):
        """Return the WorktreeManager instance, or ``None`` if not configured.

        The WorktreeManager provides git worktree lifecycle operations for
        isolated agents.  It is ``None`` when the orchestrator is started
        without a git repository context (e.g. in unit tests or when all
        agents use ``isolate=False``).

        Used by the workflow router to call :meth:`WorktreeManager.create_from_branch`
        for sequential branch-chain phases (``chain_branch=True``).

        Design reference: DESIGN.md §10.80 (v1.2.4)
        """
        return self._worktree_manager

    def get_workflow_manager(self):
        """Return the WorkflowManager instance.

        The WorkflowManager tracks multi-step workflow DAGs submitted via
        ``POST /workflows``.  It is always instantiated (never None) so callers
        can call its methods without a None check.

        Design reference: DESIGN.md §10.20 (v0.25.0).
        """
        return self._workflow_manager

    async def cleanup_workflow_branches(
        self, workflow_id: str, *, merge_final_to_main: bool = False
    ) -> list[str]:
        """Delete accumulated worktree branches for a completed workflow.

        All ephemeral agents spawned with a matching *workflow_id* had their
        branch names recorded in ``_workflow_branches[workflow_id]`` at spawn
        time (via :meth:`spawn_ephemeral_agent`).  This method:

        1. Pops the branch list for *workflow_id* from ``_workflow_branches``.
        2. Optionally merges the LAST branch into the configured default branch
           (``"main"``) when *merge_final_to_main* is ``True``.
        3. Deletes all tracked branches via :meth:`WorktreeManager.delete_branch`.

        When no WorktreeManager is configured (e.g. in tests or non-git
        environments) the method returns an empty list without raising.

        Parameters
        ----------
        workflow_id:
            The workflow run ID whose branches should be cleaned up.
        merge_final_to_main:
            When ``True``, attempt to merge the last accumulated branch into
            the default branch before deleting all branches.  Useful for
            chain_branch workflows where the final phase's committed files
            should land on ``main``.  Merge failures are logged but do not
            prevent the cleanup from continuing.

        Returns
        -------
        list[str]
            Branch names that were successfully deleted.

        Design reference: DESIGN.md §10.84 (v1.2.8)
        Research: jessfraz/branch-cleanup-action (github.com, 2025);
        Atlassian Trunk-based Development (atlassian.com, 2025);
        JetBrains TeamCity branching strategy (jetbrains.com, 2025).
        """
        branches = self._workflow_branches.pop(workflow_id, [])
        if not branches:
            return []

        wm = self._worktree_manager
        if wm is None:
            return []

        loop = asyncio.get_event_loop()

        if merge_final_to_main and branches:
            final_branch = branches[-1]
            try:
                await loop.run_in_executor(
                    None, lambda: wm.merge_branch_to_main(final_branch)
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "cleanup_workflow_branches: failed to merge final branch %r to main",
                    final_branch,
                )

        # Wait briefly for any still-running ephemeral agents whose worktrees
        # reference these branches to stop and release their worktree locks.
        # Ephemeral auto-stop (in _route_loop) runs AFTER _update_status triggers
        # this coroutine, so the last agent's stop() may still be in progress.
        # We poll for up to 10 s; in practice agents stop within 1-3 s.
        # Design reference: DESIGN.md §10.84 (v1.2.8)
        deadline = loop.time() + 10.0
        while loop.time() < deadline:
            still_running = [
                aid for aid in self._ephemeral_agents
                if self._ephemeral_agent_branches.get(aid, "") in branches
            ]
            if not still_running:
                break
            await asyncio.sleep(0.5)

        # Prune stale worktree admin files so that branches previously held by
        # ephemeral worktrees (which have already been removed by agent.stop())
        # are no longer "locked" — otherwise git branch -D fails silently.
        try:
            await loop.run_in_executor(None, wm.prune_stale)
        except Exception:  # noqa: BLE001
            logger.debug("cleanup_workflow_branches: prune_stale() failed (non-fatal)")

        deleted: list[str] = []
        for branch in branches:
            try:
                await loop.run_in_executor(None, lambda b=branch: wm.delete_branch(b))
                deleted.append(branch)
                logger.info(
                    "cleanup_workflow_branches: deleted branch %r (workflow %s)",
                    branch,
                    workflow_id,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "cleanup_workflow_branches: could not delete branch %r "
                    "(may already be gone)",
                    branch,
                )
        return deleted

    def checkpoint_workflow(self, run: "WorkflowRun") -> None:
        """Persist a workflow run snapshot to the checkpoint store (if enabled).

        This is a thin helper for callers (e.g. web/app.py) that submit
        workflows via ``get_workflow_manager().submit()`` and want the run
        state to survive a process restart.  It is a no-op when
        ``checkpoint_enabled`` is False.

        Reference: DESIGN.md §10.12 (v0.45.0).
        """
        if self._checkpoint_store is not None:
            self._checkpoint_store.save_workflow(run=run)

    def get_checkpoint_store(self):
        """Return the CheckpointStore instance, or None if not enabled."""
        return self._checkpoint_store

    def get_telemetry(self):
        """Return the TelemetrySetup instance, or None if not enabled."""
        return self._telemetry

    def get_group_manager(self) -> GroupManager:
        """Return the GroupManager instance.

        The GroupManager maintains named agent groups (logical pools) for
        targeted task dispatch.  It is always instantiated (never None).

        Design reference: DESIGN.md §10.26 (v0.31.0).
        """
        return self._group_manager

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Manual agent reset
    # ------------------------------------------------------------------

    async def reset_agent(self, agent_id: str) -> None:
        """Manually reset an agent that is in ERROR or permanently-failed state.

        Clears the permanently-failed flag and recovery attempt counter for
        *agent_id*, then stops and restarts the agent so it returns to IDLE.
        This allows operators to recover an agent that exhausted automatic
        retry attempts without restarting the entire orchestrator.

        Raises ``KeyError`` if *agent_id* is not registered.

        Design: action sub-resource pattern — POST to a verb endpoint
        (``/agents/{id}/reset``) rather than a state-replacement PUT, because
        the reset is an imperative side-effectful action, not a pure resource
        update.  Reference: Nordic APIs "Designing a True REST State Machine";
        DESIGN.md §11.
        """
        agent = self.registry.get(agent_id)
        if agent is None:
            raise KeyError(agent_id)

        # Clear recovery bookkeeping so the auto-recovery loop can retry again
        self._permanently_failed.discard(agent_id)
        self._recovery_attempts.pop(agent_id, None)

        try:
            await agent.stop()
        except Exception:  # noqa: BLE001
            logger.exception("reset_agent: error stopping agent %s", agent_id)

        try:
            await agent.start()
        except Exception:  # noqa: BLE001
            logger.exception("reset_agent: error restarting agent %s", agent_id)
            agent.status = AgentStatus.ERROR
            raise

        await self.bus.publish(Message(
            type=MessageType.STATUS,
            from_id="__orchestrator__",
            payload={
                "event": "agent_reset",
                "agent_id": agent_id,
            },
        ))
        logger.info("Orchestrator manually reset agent %s", agent_id)

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------

    def pause(self) -> None:
        self._paused = True
        logger.info("Dispatch paused")

    def resume(self) -> None:
        self._paused = False
        logger.info("Dispatch resumed")

    @property
    def is_paused(self) -> bool:
        return self._paused
