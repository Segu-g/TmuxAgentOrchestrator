"""Abstract base class for all agents."""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

import tmux_orchestrator.logging_config as log_ctx
from tmux_orchestrator.bus import Message, MessageType

# Backward-compat shims — AgentStatus and Task now live in domain/
# These re-exports preserve all existing import paths unchanged.
from tmux_orchestrator.domain.agent import AgentStatus  # noqa: F401
from tmux_orchestrator.domain.task import Task  # noqa: F401

if TYPE_CHECKING:
    import libtmux

    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.messaging import Mailbox
    from tmux_orchestrator.worktree import WorktreeManager

logger = logging.getLogger(__name__)


class Agent(ABC):
    """Lifecycle + messaging contract for all agent implementations."""

    def __init__(
        self,
        agent_id: str,
        bus: "Bus",
        *,
        task_timeout: float | None = None,
    ) -> None:
        self.id = agent_id
        self.bus = bus
        self.pane: "libtmux.Pane | None" = None
        self.mailbox: "Mailbox | None" = None
        self.status = AgentStatus.STOPPED
        self.task_timeout = task_timeout
        self._task_queue: asyncio.Queue[Task] = asyncio.Queue()
        self._current_task: Task | None = None
        self._run_task: asyncio.Task | None = None
        self._msg_task: asyncio.Task | None = None
        # Worktree isolation (set by concrete subclasses after super().__init__)
        self._worktree_manager: "WorktreeManager | None" = None
        self._isolate: bool = True
        self._merge_on_stop: bool = False
        self._merge_target: str | None = None
        self._cwd_override: Path | None = None
        self.worktree_path: Path | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def start(self) -> None:
        """Allocate resources, spin up the process, subscribe to bus."""

    @abstractmethod
    async def stop(self) -> None:
        """Tear down resources and unsubscribe."""

    # ------------------------------------------------------------------
    # Task handling
    # ------------------------------------------------------------------

    async def send_task(self, task: Task) -> None:
        """Enqueue *task* for execution."""
        await self._task_queue.put(task)

    async def interrupt(self) -> bool:
        """Interrupt the currently running task.

        Send an interrupt signal to the agent process.  Returns ``True`` if an
        interrupt was sent, ``False`` if the agent does not support interruption
        or has no active process.

        The default implementation is a no-op (returns ``False``).  Concrete
        subclasses that drive a subprocess should override this to send the
        appropriate signal (e.g., Ctrl-C / SIGINT).

        Design references:
        - POSIX SIGTERM/SIGKILL model — graceful interrupt before forceful kill
        - Java Future.cancel(mayInterruptIfRunning=true) — cooperative cancellation
        - Go context.Context cancellation — propagated cancellation signal
        - Kubernetes Pod deletion grace period — give running process time to clean up
        - DESIGN.md §10.22 (v0.27.0)
        """
        return False

    @abstractmethod
    async def _dispatch_task(self, task: Task) -> None:
        """Write the task to the agent process (pane or stdin)."""

    @abstractmethod
    async def handle_output(self, text: str) -> None:
        """Parse new pane/stdout output and publish RESULT when done."""

    @abstractmethod
    async def notify_stdin(self, notification: str) -> None:
        """Send a notification string to the agent process's stdin."""

    # ------------------------------------------------------------------
    # Internal run loop
    # ------------------------------------------------------------------

    async def _start_message_loop(self) -> None:
        """Start the message loop task that handles direct bus messages."""
        self._msg_task = asyncio.create_task(
            self._message_loop(), name=f"{self.id}-msg-loop"
        )

    async def _message_loop(self) -> None:
        """Subscribe to the bus and handle messages directed at this agent."""
        q = await self.bus.subscribe(self.id)
        loop = asyncio.get_running_loop()
        while True:
            try:
                msg = await q.get()
            except asyncio.CancelledError:
                break
            try:
                if msg.to_id == self.id:
                    if self.mailbox is not None:
                        await loop.run_in_executor(None, self.mailbox.write, self.id, msg)
                    await self.notify_stdin(f"__MSG__:{msg.id}")
            except Exception:  # noqa: BLE001
                logger.exception("Agent %s _message_loop error processing %s", self.id, msg.id)
            finally:
                q.task_done()

    async def _run_loop(self) -> None:
        """Continuously dequeue and dispatch tasks, with optional timeout enforcement."""
        while self.status not in (AgentStatus.STOPPED, AgentStatus.ERROR):
            task = await self._task_queue.get()
            self._set_busy(task)
            await self._publish_status_event("agent_busy", task_id=task.id)
            # Bind trace_id and agent_id into the async context so every log record
            # produced during this task automatically includes these fields.
            t1 = log_ctx.bind_trace(task.trace_id)
            t2 = log_ctx.bind_agent(self.id)
            logger.info("Agent %s starting task %s", self.id, task.id)
            try:
                if self.task_timeout is not None:
                    await asyncio.wait_for(
                        self._dispatch_task(task), timeout=self.task_timeout
                    )
                else:
                    await self._dispatch_task(task)
            except asyncio.TimeoutError:
                logger.error(
                    "Agent %s task %s timed out after %ss",
                    self.id, task.id, self.task_timeout,
                )
                await self._handle_task_timeout(task)
            except Exception as exc:  # noqa: BLE001
                logger.error("Agent %s task %s failed: %s", self.id, task.id, exc)
                self.status = AgentStatus.ERROR
                await self._publish_status_event("agent_error", task_id=task.id)
            finally:
                log_ctx.unbind(t2)
                log_ctx.unbind(t1)
                self._task_queue.task_done()
            if self.status == AgentStatus.IDLE:
                await self._publish_status_event("agent_idle", task_id=task.id)

    async def _handle_task_timeout(self, task: Task) -> None:
        """Publish a RESULT with error=timeout and return the agent to IDLE."""
        await self.bus.publish(Message(
            type=MessageType.RESULT,
            from_id=self.id,
            payload={"task_id": task.id, "error": "timeout", "output": None},
        ))
        self._set_idle()

    async def _publish_status_event(
        self, event: str, task_id: str | None = None
    ) -> None:
        """Publish an agent status transition event to the bus."""
        payload: dict[str, Any] = {
            "event": event,
            "agent_id": self.id,
            "status": self.status.value,
        }
        if task_id is not None:
            payload["task_id"] = task_id
        await self.bus.publish(
            Message(type=MessageType.STATUS, from_id=self.id, payload=payload)
        )

    # ------------------------------------------------------------------
    # Worktree helpers
    # ------------------------------------------------------------------

    async def _setup_worktree(self) -> Path | None:
        """Set up the agent's working directory via worktree isolation.

        Returns the path to use as cwd, or ``None`` if no isolation is active.
        Priority: ``_cwd_override`` > ``_worktree_manager`` > None.
        """
        if self._cwd_override is not None:
            # Shared parent worktree — do not register or teardown.
            return self._cwd_override
        if self._worktree_manager is None:
            return None
        loop = asyncio.get_running_loop()
        path: Path = await loop.run_in_executor(
            None,
            lambda: self._worktree_manager.setup(self.id, isolate=self._isolate),  # type: ignore[union-attr]
        )
        self.worktree_path = path
        return path

    async def _teardown_worktree(self) -> None:
        """Remove the agent's worktree (no-op when not isolated or not set up).

        When ``_merge_on_stop`` is True, the agent's worktree branch is
        squash-merged into the main repo HEAD before removal (see
        ``WorktreeManager.teardown(merge_to_base=True)``).
        """
        if self._worktree_manager is None or self.worktree_path is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: self._worktree_manager.teardown(  # type: ignore[union-attr]
                self.id,
                merge_to_base=self._merge_on_stop,
                merge_target=self._merge_target,
            ),
        )
        self.worktree_path = None

    def _write_context_file(self, cwd: Path) -> None:
        """Write per-agent context file to the agent's working directory.

        Writes ``__orchestrator_context__{agent_id}__.json`` as the primary context
        file.  This per-agent naming prevents a race condition when multiple agents
        share the same working directory (``isolate: false``): each agent's file is
        independent, so concurrent startup writes cannot overwrite each other.

        The legacy ``__orchestrator_context__.json`` is **not** written by this method.
        Slash commands discover the correct file by checking the
        ``TMUX_ORCHESTRATOR_AGENT_ID`` environment variable first, then falling
        back to the legacy name for backward compatibility.

        Design reference:
        - SEI CERT C POS38-C — race conditions with shared file names
          https://wiki.sei.cmu.edu/confluence/display/c/POS38-C.+Beware+of+race+conditions+when+using+fork+and+file+descriptors
        - Apple Developer "Race Conditions and Secure File Operations" (2024)
          https://developer.apple.com/library/archive/documentation/Security/Conceptual/SecureCodingGuide/Articles/RaceConditions.html
        - DESIGN.md §10.N (v1.0.19 — isolate:false context file race fix)
        """
        if self.mailbox is not None:
            # mailbox._root is {mailbox_dir}/{session_name}; parent recovers mailbox_dir
            mailbox_dir = str(self.mailbox._root.parent)
        else:
            mailbox_dir = str(Path.home() / ".tmux_orchestrator")
        ctx: dict[str, Any] = {
            "agent_id": self.id,
            "mailbox_dir": mailbox_dir,
            "worktree_path": str(cwd),
        }
        ctx.update(self._context_extras())
        content = json.dumps(ctx, indent=2)
        # Write per-agent file (safe for shared cwd — no race with sibling agents).
        per_agent_path = cwd / f"__orchestrator_context__{self.id}__.json"
        per_agent_path.write_text(content)
        # Also write the legacy file for single-agent scenarios and backward
        # compatibility with tools/scripts that still reference the old name.
        # In shared-cwd scenarios this file may be overwritten by sibling agents,
        # but the per-agent file is always authoritative.
        (cwd / "__orchestrator_context__.json").write_text(content)
        logger.debug("Agent %s wrote context file to %s (per-agent + legacy)", self.id, cwd)

    def _context_extras(self) -> dict[str, Any]:
        """Return additional keys for the context file. Override in subclasses."""
        return {}

    # ------------------------------------------------------------------
    # State management — all status transitions go through these methods.
    # Call sites (run loop, timeout handler, tests) must NOT write to
    # ``status`` or ``_current_task`` directly.
    # ------------------------------------------------------------------

    def _set_busy(self, task: Task) -> None:
        """Transition the agent to BUSY state for *task*."""
        self._current_task = task
        self.status = AgentStatus.BUSY

    def _set_idle(self) -> None:
        """Transition the agent back to IDLE, clearing the current task."""
        self._current_task = None
        if self.status not in (AgentStatus.STOPPED, AgentStatus.ERROR, AgentStatus.DRAINING):
            self.status = AgentStatus.IDLE
            # Always publish agent_idle so orchestrator, TUI, and WebSocket hub
            # receive consistent notification regardless of which code path triggered the transition.
            asyncio.create_task(
                self._publish_status_event("agent_idle"),
                name=f"{self.id}-idle-notify",
            )
