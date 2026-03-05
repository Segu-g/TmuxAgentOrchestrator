"""Central orchestrator: task queue, agent lifecycle, dispatch, and P2P routing."""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.context_monitor import ContextMonitor
from tmux_orchestrator.group_manager import GroupManager
from tmux_orchestrator.messaging import Mailbox
from tmux_orchestrator.rate_limiter import RateLimitExceeded, TokenBucketRateLimiter
from tmux_orchestrator.registry import AgentRegistry
from tmux_orchestrator.supervision import supervised_task
from tmux_orchestrator.webhook_manager import WebhookManager

if TYPE_CHECKING:
    from tmux_orchestrator.config import AgentConfig, OrchestratorConfig
    from tmux_orchestrator.tmux_interface import TmuxInterface
    from tmux_orchestrator.worktree import WorktreeManager

logger = logging.getLogger(__name__)


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
        self._task_queue: asyncio.PriorityQueue[tuple[int, int, Task]] = asyncio.PriorityQueue(
            maxsize=config.task_queue_maxsize
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
        # Reference: Liu et al. "Lost in the Middle" TACL 2024; DESIGN.md §11 (v0.21.0)
        self._context_monitor = ContextMonitor(
            bus=bus,
            tmux=tmux,
            agents=lambda: list(self.registry.all_agents().values()),
            context_window_tokens=config.context_window_tokens,
            warn_threshold=config.context_warn_threshold,
            auto_summarize=config.context_auto_summarize,
            poll_interval=config.context_monitor_poll,
        )
        # Queue-depth autoscaler — only created when autoscale_max > 0.
        # Reference: Kubernetes HPA; Thijssen "Autonomic Computing"; AWS cooldowns.
        # DESIGN.md §10.18 (v0.23.0)
        if config.autoscale_max > 0:
            from tmux_orchestrator.autoscaler import AutoScaler
            self._autoscaler: "AutoScaler | None" = AutoScaler(self, config)
        else:
            self._autoscaler = None
        # Append-only JSONL result store — Event Sourcing pattern.
        # Enabled only when config.result_store_enabled=True to avoid
        # unexpected I/O in deployments that don't need persistence.
        # Reference: Fowler "Event Sourcing" (2005); Young CQRS (2010);
        # Hickey "The Value of Values" (Datomic, 2012). DESIGN.md §10.19 (v0.24.0)
        if config.result_store_enabled:
            from tmux_orchestrator.result_store import ResultStore
            self._result_store: "ResultStore | None" = ResultStore(
                store_dir=config.result_store_dir,
                session_name=config.session_name,
            )
        else:
            self._result_store = None
        # Workflow DAG tracker — always enabled (zero overhead when no workflows
        # are submitted).  Tracks multi-step pipelines submitted via POST /workflows.
        # Reference: Apache Airflow DAG model; Tomasulo's algorithm (IBM 1967);
        # AWS Step Functions; Prefect "Modern Data Stack". DESIGN.md §10.20 (v0.25.0)
        from tmux_orchestrator.workflow_manager import WorkflowManager
        self._workflow_manager = WorkflowManager()
        # Outbound webhook notification manager.
        # Fire-and-forget delivery of task/agent/workflow events to registered URLs.
        # Reference: GitHub Webhooks; Stripe Webhooks; RFC 2104 HMAC;
        # Zalando RESTful API Guidelines §webhook. DESIGN.md §10.25 (v0.30.0)
        self._webhook_manager = WebhookManager(timeout=config.webhook_timeout)
        # Named agent group manager — logical pools for targeted task dispatch.
        # Groups allow tasks to target a named pool instead of individual agent IDs or tags.
        # References:
        #   Kubernetes Node Pools / Node Groups; AWS Auto Scaling Groups;
        #   Apache Mesos Roles; HashiCorp Nomad Task Groups.
        # DESIGN.md §10.26 (v0.31.0)
        self._group_manager = GroupManager()
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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start all registered agents and the dispatch / routing loops."""
        self._bus_queue = await self.bus.subscribe(
            "__orchestrator__", broadcast=True
        )
        for agent in self.registry.all_agents().values():
            await agent.start()
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
        if self._autoscaler is not None:
            self._autoscaler.start()
        self._ttl_reaper_task = asyncio.create_task(
            self._ttl_reaper_loop(poll=self.config.ttl_reaper_poll),
            name="orchestrator-ttl-reaper",
        )
        logger.info("Orchestrator started with %d agents", len(self.registry.all_agents()))

    async def stop(self) -> None:
        """Stop dispatch, routing, watchdog, context monitor, and all agents."""
        if self._autoscaler is not None:
            self._autoscaler.stop()
        self._context_monitor.stop()
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

    # ------------------------------------------------------------------
    # Context monitor
    # ------------------------------------------------------------------

    def get_agent_context_stats(self, agent_id: str) -> dict | None:
        """Return context usage stats for *agent_id*, or None if not tracked."""
        return self._context_monitor.get_stats(agent_id)

    def all_agent_context_stats(self) -> list[dict]:
        """Return context usage stats for all tracked agents."""
        return self._context_monitor.all_stats()

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
        )
        # Record this task's priority for use by future dependent tasks.
        self._task_priorities[task.id] = effective_priority
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
                **({"required_tags": t.required_tags} if t.required_tags else {}),
                **({"target_agent": t.target_agent} if t.target_agent else {}),
                **({"target_group": t.target_group} if t.target_group else {}),
            }
            for p, _seq, t in sorted(items, key=lambda x: (x[0], x[1]))
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

    async def update_task_priority(self, task_id: str, new_priority: int) -> bool:
        """Update the priority of a pending task in-place.

        Locates *task_id* in the priority queue, changes its priority to
        *new_priority*, and rebuilds the heap to restore the heap invariant.
        Returns ``True`` if the task was found and updated; ``False`` if not
        found (already dispatched, completed, or never submitted).

        A ``task_priority_updated`` STATUS event is published on success.

        Design note: Python's ``heapq`` module does not provide a
        ``decrease_key`` / ``increase_key`` operation directly. The standard
        approach (Python docs "heapq — Priority Queue Implementation Notes") is
        to mark entries as invalid and add a replacement, or to rebuild the
        heap after mutating an element. We mutate the tuple in-place and call
        ``heapq.heapify`` for O(n) rebuild — acceptable for the small queue
        sizes expected (< 10 000 tasks). This is equivalent to the
        ``decrease_key`` / ``increase_key`` operations described in Sedgewick &
        Wayne "Algorithms" 4th ed. §2.4 and the RTOS priority-change pattern
        described in Liu & Layland (1973) "Scheduling Algorithms for
        Multiprogramming in a Hard Real-Time Environment".

        Reference:
        - Python heapq docs: https://docs.python.org/3/library/heapq.html
        - Liu, C.L.; Layland, J.W. (1973). "Scheduling Algorithms for
          Multiprogramming in a Hard Real-Time Environment". JACM 20(1).
        - Sedgewick & Wayne "Algorithms" 4th ed. §2.4 — Priority Queues.
        """
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

        if not found:
            return False

        # Rebuild the heap with the updated priority.
        self._task_queue._queue.clear()  # type: ignore[attr-defined]
        for item in new_items:
            self._task_queue._queue.append(item)  # type: ignore[attr-defined]
        heapq.heapify(self._task_queue._queue)  # type: ignore[attr-defined]

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
            # Track the Task object for potential retry on failure.
            self._active_tasks[task.id] = task
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
                if not error and task_id:
                    self._completed_tasks.add(task_id)
                    wf_status_before = self._workflow_manager.get_workflow_status_for_task(task_id)
                    self._workflow_manager.on_task_complete(task_id)
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
        from tmux_orchestrator.config import AgentRole

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

    # ------------------------------------------------------------------
    # Workflow DAG tracking
    # ------------------------------------------------------------------

    def get_workflow_manager(self):
        """Return the WorkflowManager instance.

        The WorkflowManager tracks multi-step workflow DAGs submitted via
        ``POST /workflows``.  It is always instantiated (never None) so callers
        can call its methods without a None check.

        Design reference: DESIGN.md §10.20 (v0.25.0).
        """
        return self._workflow_manager

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
