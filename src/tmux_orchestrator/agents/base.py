"""Abstract base class for all agents."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import libtmux

    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.messaging import Mailbox

logger = logging.getLogger(__name__)


class AgentStatus(str, Enum):
    IDLE = "IDLE"
    BUSY = "BUSY"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


@dataclass
class Task:
    id: str
    prompt: str
    priority: int = 0  # lower = higher priority
    metadata: dict[str, Any] = field(default_factory=dict)

    def __lt__(self, other: "Task") -> bool:
        return self.priority < other.priority


class Agent(ABC):
    """Lifecycle + messaging contract for all agent implementations."""

    def __init__(self, agent_id: str, bus: "Bus") -> None:
        self.id = agent_id
        self.bus = bus
        self.pane: "libtmux.Pane | None" = None
        self.mailbox: "Mailbox | None" = None
        self.status = AgentStatus.STOPPED
        self._task_queue: asyncio.Queue[Task] = asyncio.Queue()
        self._current_task: Task | None = None
        self._run_task: asyncio.Task | None = None
        self._msg_task: asyncio.Task | None = None

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
        loop = asyncio.get_event_loop()
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
        """Continuously dequeue and dispatch tasks."""
        while self.status not in (AgentStatus.STOPPED, AgentStatus.ERROR):
            task = await self._task_queue.get()
            self._current_task = task
            self.status = AgentStatus.BUSY
            logger.info("Agent %s starting task %s", self.id, task.id)
            try:
                await self._dispatch_task(task)
            except Exception as exc:  # noqa: BLE001
                logger.error("Agent %s task %s failed: %s", self.id, task.id, exc)
                self.status = AgentStatus.ERROR
            finally:
                self._task_queue.task_done()

    def _set_idle(self) -> None:
        self._current_task = None
        if self.status not in (AgentStatus.STOPPED, AgentStatus.ERROR):
            self.status = AgentStatus.IDLE
