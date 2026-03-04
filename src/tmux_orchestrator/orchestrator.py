"""Central orchestrator: task queue, agent lifecycle, dispatch, and P2P routing."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING

from tmux_orchestrator.agents.base import Agent, Task
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
        # Priority queue: (priority, task) — lower priority value = dispatched first
        self._task_queue: asyncio.PriorityQueue[tuple[int, Task]] = asyncio.PriorityQueue(
            maxsize=config.task_queue_maxsize
        )
        self._paused = False
        self._dispatch_task: asyncio.Task | None = None
        self._router_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
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
        logger.info("Orchestrator started with %d agents", len(self.registry.all_agents()))

    async def stop(self) -> None:
        """Stop dispatch, routing, watchdog, and all agents."""
        internal_tasks = [
            t for t in [self._dispatch_task, self._router_task, self._watchdog_task] if t
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
        )
        if idempotency_key is not None:
            self._idempotency_keys[idempotency_key] = task.id
            self._ikey_timestamps[idempotency_key] = time.monotonic()
            self._cleanup_expired_ikeys()
        await self._task_queue.put((priority, task))
        await self.bus.publish(Message(
            type=MessageType.STATUS,
            from_id="__orchestrator__",
            payload={"event": "task_queued", "task_id": task.id, "prompt": prompt},
        ))
        logger.info("Task %s queued (priority=%d)", task.id, priority)
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
            for p, t in sorted(items, key=lambda x: x[0])
        ]

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        while True:
            if self._paused:
                await asyncio.sleep(0.5)
                continue
            try:
                _, task = await asyncio.wait_for(
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
                    await self._task_queue.put((task.priority, task))
                    await asyncio.sleep(0.2)
                continue

            agent = self.registry.find_idle_worker()
            if agent is None:
                retry_count = task.metadata.get("_retry_count", 0) + 1
                task.metadata["_retry_count"] = retry_count
                if retry_count >= self.config.dlq_max_retries:
                    await self._dead_letter(task, f"no idle agent after {retry_count} retries")
                else:
                    await self._task_queue.put((task.priority, task))
                    await asyncio.sleep(0.2)
                continue

            logger.info("Dispatching task %s → agent %s", task.id, agent.id)
            self.registry.record_busy(agent.id)
            await agent.send_task(task)
            self._task_queue.task_done()

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
                if not error:
                    task_id = msg.payload.get("task_id")
                    if task_id:
                        self._completed_tasks.add(task_id)
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

    def pause(self) -> None:
        self._paused = True
        logger.info("Dispatch paused")

    def resume(self) -> None:
        self._paused = False
        logger.info("Dispatch resumed")

    @property
    def is_paused(self) -> bool:
        return self._paused
