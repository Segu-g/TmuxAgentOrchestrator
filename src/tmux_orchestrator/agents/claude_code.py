"""Agent that manages a `claude` CLI process inside a tmux pane."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Message, MessageType

if TYPE_CHECKING:
    import libtmux

    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.messaging import Mailbox
    from tmux_orchestrator.tmux_interface import TmuxInterface

logger = logging.getLogger(__name__)

# Patterns that indicate Claude has finished and is waiting for input.
# Adjust as the `claude` CLI evolves.
_DONE_PATTERNS = [
    re.compile(r"^\s*>\s*$", re.MULTILINE),          # bare prompt ">"
    re.compile(r"Human:\s*$", re.MULTILINE),           # Human: prompt
    re.compile(r"\$\s*$", re.MULTILINE),               # shell prompt fallback
]

_POLL_INTERVAL = 0.5  # seconds between output checks
_SETTLE_CYCLES = 3    # consecutive unchanged polls before declaring done


class ClaudeCodeAgent(Agent):
    """Drives `claude` CLI (or any REPL) inside a dedicated tmux pane."""

    def __init__(
        self,
        agent_id: str,
        bus: "Bus",
        tmux: "TmuxInterface",
        *,
        command: str = "claude --no-pager",
        mailbox: "Mailbox | None" = None,
    ) -> None:
        super().__init__(agent_id, bus)
        self.mailbox = mailbox
        self._tmux = tmux
        self._command = command
        self._last_output: str = ""
        self._settle_count: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_event_loop()
        pane = await loop.run_in_executor(None, self._tmux.new_pane)
        self.pane = pane
        await loop.run_in_executor(None, self._tmux.send_keys, pane, self._command)
        self._tmux.watch_pane(pane, self.id)
        self._tmux.start_watcher()
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(self._run_loop(), name=f"{self.id}-loop")
        await self._start_message_loop()
        logger.info("ClaudeCodeAgent %s started in pane %s", self.id, pane.id)

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()
        if self._msg_task:
            self._msg_task.cancel()
        if self.pane:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, self._tmux.send_keys, self.pane, "q", True
            )
            self._tmux.unwatch_pane(self.pane)
        await self.bus.unsubscribe(self.id)
        logger.info("ClaudeCodeAgent %s stopped", self.id)

    # ------------------------------------------------------------------
    # Task dispatch
    # ------------------------------------------------------------------

    async def _dispatch_task(self, task: Task) -> None:
        if self.pane is None:
            raise RuntimeError(f"Agent {self.id} has no pane")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self._tmux.send_keys, self.pane, task.prompt
        )
        # Poll until output settles and looks like a prompt
        await self._wait_for_completion(task)

    async def _wait_for_completion(self, task: Task) -> None:
        settle = 0
        prev = ""
        while True:
            await asyncio.sleep(_POLL_INTERVAL)
            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(
                None, self._tmux.capture_pane, self.pane
            )
            if text == prev:
                settle += 1
            else:
                settle = 0
                prev = text
            if settle >= _SETTLE_CYCLES and _looks_done(text):
                await self.handle_output(text)
                return

    # ------------------------------------------------------------------
    # Output handling
    # ------------------------------------------------------------------

    async def handle_output(self, text: str) -> None:
        task_id = self._current_task.id if self._current_task else "unknown"
        msg = Message(
            type=MessageType.RESULT,
            from_id=self.id,
            payload={"task_id": task_id, "output": text},
        )
        await self.bus.publish(msg)
        self._set_idle()
        logger.info("ClaudeCodeAgent %s published result for task %s", self.id, task_id)

    async def notify_stdin(self, notification: str) -> None:
        """Send *notification* to the tmux pane via send_keys."""
        if self.pane is None:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self._tmux.send_keys, self.pane, notification
        )


def _looks_done(text: str) -> bool:
    return any(p.search(text) for p in _DONE_PATTERNS)
