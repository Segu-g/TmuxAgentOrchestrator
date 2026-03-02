"""Agent that manages a `claude` CLI process inside a tmux pane."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Message, MessageType

if TYPE_CHECKING:
    import libtmux

    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.messaging import Mailbox
    from tmux_orchestrator.tmux_interface import TmuxInterface
    from tmux_orchestrator.worktree import WorktreeManager

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
        worktree_manager: "WorktreeManager | None" = None,
        isolate: bool = True,
        cwd_override: Path | None = None,
        session_name: str = "orchestrator",
        web_base_url: str = "http://localhost:8000",
    ) -> None:
        super().__init__(agent_id, bus)
        self.mailbox = mailbox
        self._tmux = tmux
        self._command = command
        self._last_output: str = ""
        self._settle_count: int = 0
        self._worktree_manager = worktree_manager
        self._isolate = isolate
        self._cwd_override = cwd_override
        self._session_name = session_name
        self._web_base_url = web_base_url

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_event_loop()
        pane = await loop.run_in_executor(None, self._tmux.new_pane)
        self.pane = pane
        cwd = await self._setup_worktree()
        if cwd is not None:
            await loop.run_in_executor(None, self._write_context_file, cwd)
        launch = (
            f"cd {shlex.quote(str(cwd))} && {self._command}" if cwd else self._command
        )
        await loop.run_in_executor(None, self._tmux.send_keys, pane, launch)
        self._tmux.watch_pane(pane, self.id)
        self._tmux.start_watcher()
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(self._run_loop(), name=f"{self.id}-loop")
        await self._start_message_loop()
        logger.info("ClaudeCodeAgent %s started in pane %s", self.id, pane.id)

    def _write_context_file(self, cwd: Path) -> None:
        """Write ``__orchestrator_context__.json`` to the agent's working directory."""
        if self.mailbox is not None:
            # mailbox._root is {mailbox_dir}/{session_name}; parent recovers mailbox_dir
            mailbox_dir = str(self.mailbox._root.parent)
        else:
            mailbox_dir = str(Path.home() / ".tmux_orchestrator")
        ctx = {
            "agent_id": self.id,
            "session_name": self._session_name,
            "mailbox_dir": mailbox_dir,
            "worktree_path": str(cwd),
            "web_base_url": self._web_base_url,
        }
        (cwd / "__orchestrator_context__.json").write_text(json.dumps(ctx, indent=2))
        logger.debug("ClaudeCodeAgent %s wrote context file to %s", self.id, cwd)

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
        await self._teardown_worktree()
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
