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
from tmux_orchestrator.messaging import Mailbox
from tmux_orchestrator.registry import AgentRegistry
from tmux_orchestrator.supervision import supervised_task

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
        logger.info("Orchestrator started with %d agents", len(self.registry.all_agents()))

    async def stop(self) -> None:
        """Stop dispatch, routing, watchdog, and all agents."""
        internal_tasks = [
            t for t in [
                self._dispatch_task, self._router_task,
                self._watchdog_task, self._recovery_task,
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
    ) -> Task:
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
        task = Task(
            id=str(uuid.uuid4()),
            prompt=prompt,
            priority=priority,
            metadata=metadata or {},
            depends_on=depends_on or [],
            reply_to=reply_to,
            target_agent=target_agent,
        )
        if idempotency_key is not None:
            self._idempotency_keys[idempotency_key] = task.id
            self._ikey_timestamps[idempotency_key] = time.monotonic()
            self._cleanup_expired_ikeys()
        if reply_to is not None:
            self._task_reply_to[task.id] = reply_to
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
            },
        ))
        logger.info("Task %s queued (priority=%d, reply_to=%s)", task.id, priority, reply_to)
        return task

    def _cleanup_expired_ikeys(self) -> None:
        """Remove idempotency entries older than _ikey_ttl."""
        cutoff = time.monotonic() - self._ikey_ttl
        expired = [k for k, t in self._ikey_timestamps.items() if t < cutoff]
        for k in expired:
            self._idempotency_keys.pop(k, None)
            self._ikey_timestamps.pop(k, None)

    def list_tasks(self) -> list[dict]:
        """Return a snapshot of the pending task queue (non-destructive)."""
        items = list(self._task_queue._queue)  # type: ignore[attr-defined]
        return [
            {"priority": p, "task_id": t.id, "prompt": t.prompt}
            for p, _seq, t in sorted(items, key=lambda x: (x[0], x[1]))
        ]

    async def cancel_task(self, task_id: str) -> bool:
        """Remove *task_id* from the pending queue.

        Returns True if the task was found and removed; False if not found
        (already dispatched, never submitted, or already completed).

        Cancelled tasks are discarded — they are NOT moved to the DLQ.
        A ``task_cancelled`` STATUS event is published on successful cancellation.

        Design: task cancellation via REST DELETE/POST follows the async
        request-reply pattern described in Microsoft Azure Architecture Center
        "Asynchronous Request-Reply pattern" (2024).
        """
        # Snapshot the underlying heap and rebuild it without the cancelled task.
        items = list(self._task_queue._queue)  # type: ignore[attr-defined]
        new_items = [(p, seq, t) for p, seq, t in items if t.id != task_id]
        if len(new_items) == len(items):
            # Task was not in the queue.
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
            },
        ))
        logger.info("Task %s cancelled from queue", task_id)
        return True

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

            # Check task dependency graph: re-queue if any dependency is not yet done.
            unmet = [dep for dep in task.depends_on if dep not in self._completed_tasks]
            if unmet:
                retry_count = task.metadata.get("_retry_count", 0) + 1
                task.metadata["_retry_count"] = retry_count
                if retry_count >= self.config.dlq_max_retries:
                    await self._dead_letter(
                        task, f"unmet dependencies {unmet} after {retry_count} retries"
                    )
                else:
                    self._task_seq += 1
                    await self._task_queue.put((task.priority, self._task_seq, task))
                    # Short yield so the route loop can process RESULTs and update
                    # _completed_tasks between re-queue attempts.  0.05s (was 0.2s)
                    # prevents busy-spinning without causing O(n²) pipeline delay.
                    await asyncio.sleep(0.05)
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
                agent = self.registry.find_idle_worker()
            if agent is None:
                retry_count = task.metadata.get("_retry_count", 0) + 1
                task.metadata["_retry_count"] = retry_count
                if retry_count >= self.config.dlq_max_retries:
                    await self._dead_letter(task, f"no idle agent after {retry_count} retries")
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
            await agent.send_task(task)
            self._task_queue.task_done()
            # Yield so the agent's _run_loop can dequeue and set status=BUSY
            # before the next find_idle_worker() call.  Without this yield, all
            # tasks pile up in the first agent's queue (agent.status stays IDLE
            # until the run loop gets to run).
            await asyncio.sleep(0)

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
                self._buffer_director_result(msg)
                error = msg.payload.get("error")
                self.registry.record_result(msg.from_id, error=bool(error))
                task_id = msg.payload.get("task_id")
                if not error and task_id:
                    self._completed_tasks.add(task_id)
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
            self._bus_queue.task_done()

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
            task_timeout=template_cfg.task_timeout if template_cfg.task_timeout is not None else self.config.task_timeout,
            role=template_cfg.role,
            command=template_cfg.command or "env -u CLAUDECODE claude --dangerously-skip-permissions",
            parent_pane=parent_pane,
            system_prompt=template_cfg.system_prompt,
            context_files=template_cfg.context_files,
            context_files_root=_Path.cwd() if template_cfg.context_files else None,
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
