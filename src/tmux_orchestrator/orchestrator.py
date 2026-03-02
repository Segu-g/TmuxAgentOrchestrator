"""Central orchestrator: task queue, agent registry, dispatch, and P2P routing."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import BROADCAST, Bus, Message, MessageType
from tmux_orchestrator.messaging import Mailbox

if TYPE_CHECKING:
    from tmux_orchestrator.config import AgentConfig, OrchestratorConfig
    from tmux_orchestrator.tmux_interface import TmuxInterface
    from tmux_orchestrator.worktree import WorktreeManager

logger = logging.getLogger(__name__)


class Orchestrator:
    """Manages the full agent lifecycle and routes all messages.

    Responsibilities:
    - Maintain a priority task queue.
    - Register / deregister agents.
    - Dispatch tasks to idle agents.
    - Gate peer-to-peer messages via a configurable permission table.
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
        self._agents: dict[str, Agent] = {}
        # Priority queue: (priority, task) — lower priority value = dispatched first
        self._task_queue: asyncio.PriorityQueue[tuple[int, Task]] = asyncio.PriorityQueue()
        self._p2p: set[frozenset[str]] = {
            frozenset(pair) for pair in config.p2p_permissions
        }
        self._paused = False
        self._dispatch_task: asyncio.Task | None = None
        self._router_task: asyncio.Task | None = None
        self._bus_queue: asyncio.Queue[Message] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start all registered agents and the dispatch / routing loops."""
        self._bus_queue = await self.bus.subscribe(
            "__orchestrator__", broadcast=True
        )
        for agent in self._agents.values():
            await agent.start()
        self._dispatch_task = asyncio.create_task(
            self._dispatch_loop(), name="orchestrator-dispatch"
        )
        self._router_task = asyncio.create_task(
            self._route_loop(), name="orchestrator-router"
        )
        logger.info("Orchestrator started with %d agents", len(self._agents))

    async def stop(self) -> None:
        """Stop dispatch, routing, and all agents."""
        if self._dispatch_task:
            self._dispatch_task.cancel()
        if self._router_task:
            self._router_task.cancel()
        for agent in list(self._agents.values()):
            await agent.stop()
        if self._bus_queue:
            await self.bus.unsubscribe("__orchestrator__")
        logger.info("Orchestrator stopped")

    # ------------------------------------------------------------------
    # Agent registry
    # ------------------------------------------------------------------

    def register_agent(self, agent: Agent) -> None:
        self._agents[agent.id] = agent
        logger.debug("Registered agent %s", agent.id)

    def unregister_agent(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    def get_agent(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def list_agents(self) -> list[dict]:
        return [
            {
                "id": a.id,
                "status": a.status.value,
                "current_task": a._current_task.id if a._current_task else None,
            }
            for a in self._agents.values()
        ]

    # ------------------------------------------------------------------
    # Task submission
    # ------------------------------------------------------------------

    async def submit_task(
        self, prompt: str, *, priority: int = 0, metadata: dict | None = None
    ) -> Task:
        task = Task(
            id=str(uuid.uuid4()),
            prompt=prompt,
            priority=priority,
            metadata=metadata or {},
        )
        await self._task_queue.put((priority, task))
        msg = Message(
            type=MessageType.STATUS,
            from_id="__orchestrator__",
            payload={"event": "task_queued", "task_id": task.id, "prompt": prompt},
        )
        await self.bus.publish(msg)
        logger.info("Task %s queued (priority=%d)", task.id, priority)
        return task

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

            agent = self._find_idle_agent()
            if agent is None:
                # No idle agent — put back and wait
                await self._task_queue.put((task.priority, task))
                await asyncio.sleep(0.2)
                continue

            logger.info("Dispatching task %s → agent %s", task.id, agent.id)
            await agent.send_task(task)
            self._task_queue.task_done()

    def _find_idle_agent(self) -> Agent | None:
        for agent in self._agents.values():
            if agent.status == AgentStatus.IDLE:
                return agent
        return None

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
            self._bus_queue.task_done()

    async def route_message(self, msg: Message) -> None:
        """Forward a PEER_MSG if the sender/receiver pair is permitted.

        ``__user__`` (Web API) is always permitted to reach any agent.
        """
        if msg.from_id == "__user__":
            permitted = True
        else:
            pair = frozenset({msg.from_id, msg.to_id})
            permitted = pair in self._p2p

        if permitted:
            # Mark as forwarded so the route loop doesn't re-process it.
            routed = Message(
                type=MessageType.PEER_MSG,
                from_id=msg.from_id,
                to_id=msg.to_id,
                payload={**msg.payload, "_forwarded": True},
            )
            await self.bus.publish(routed)
            logger.debug("P2P %s → %s forwarded", msg.from_id, msg.to_id)
        else:
            logger.warning(
                "P2P %s → %s blocked (not in permission table)",
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
            # Resolve the pre-configured agent definition.
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
        """Create, register, and start a sub-agent from a pre-configured template.

        The sub-agent is a new instance of *template_cfg* with a unique ID.
        P2P messaging between the parent and the new sub-agent is automatically
        granted.
        """
        from pathlib import Path as _Path  # noqa: PLC0415

        from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

        sub_id = f"{parent_id}-sub-{uuid.uuid4().hex[:6]}"
        mailbox = Mailbox(self.config.mailbox_dir, self.config.session_name)

        # Determine cwd_override for share_parent_worktree option.
        cwd_override: _Path | None = None
        if share_parent:
            parent_agent = self._agents.get(parent_id)
            if parent_agent is not None:
                cwd_override = parent_agent.worktree_path

        # When cwd_override is provided, no worktree management is needed.
        effective_wm = self._worktree_manager if cwd_override is None else None

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
            task_timeout=self.config.task_timeout,
        )

        self.register_agent(agent)
        self._p2p.add(frozenset({parent_id, sub_id}))
        await agent.start()

        status_msg = Message(
            type=MessageType.STATUS,
            from_id="__orchestrator__",
            to_id=parent_id,
            payload={
                "event": "subagent_spawned",
                "sub_agent_id": sub_id,
                "parent_id": parent_id,
            },
        )
        await self.bus.publish(status_msg)
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
