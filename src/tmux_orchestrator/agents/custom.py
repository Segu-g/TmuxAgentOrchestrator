"""Agent that runs an arbitrary command communicating via newline-delimited JSON."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Message, MessageType

if TYPE_CHECKING:
    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.messaging import Mailbox
    from tmux_orchestrator.tmux_interface import TmuxInterface
    from tmux_orchestrator.worktree import WorktreeManager

logger = logging.getLogger(__name__)


class CustomAgent(Agent):
    """Runs *command* as a subprocess; talks newline-delimited JSON over stdio.

    Protocol:
    - Orchestrator writes one JSON line to stdin: ``{"task_id": "…", "prompt": "…"}``
    - Script writes one JSON line to stdout: ``{"task_id": "…", "result": "…"}``
    """

    def __init__(
        self,
        agent_id: str,
        bus: "Bus",
        tmux: "TmuxInterface",
        *,
        command: str,
        mailbox: "Mailbox | None" = None,
        worktree_manager: "WorktreeManager | None" = None,
        isolate: bool = True,
        cwd_override: Path | None = None,
    ) -> None:
        super().__init__(agent_id, bus)
        self.mailbox = mailbox
        self._tmux = tmux
        self._command = command
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._worktree_manager = worktree_manager
        self._isolate = isolate
        self._cwd_override = cwd_override

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        cwd = await self._setup_worktree()
        self._proc = await asyncio.create_subprocess_shell(
            self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
        )
        self.status = AgentStatus.IDLE
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"{self.id}-reader"
        )
        self._run_task = asyncio.create_task(
            self._run_loop(), name=f"{self.id}-loop"
        )
        await self._start_message_loop()
        logger.info("CustomAgent %s started (pid=%s)", self.id, self._proc.pid)

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()
        if self._msg_task:
            self._msg_task.cancel()
        if self._reader_task:
            self._reader_task.cancel()
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self._proc.kill()
        await self.bus.unsubscribe(self.id)
        await self._teardown_worktree()
        logger.info("CustomAgent %s stopped", self.id)

    # ------------------------------------------------------------------
    # Task dispatch
    # ------------------------------------------------------------------

    async def _dispatch_task(self, task: Task) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError(f"Agent {self.id} process not running")
        line = json.dumps({"task_id": task.id, "prompt": task.prompt}) + "\n"
        self._proc.stdin.write(line.encode())
        await self._proc.stdin.drain()
        logger.debug("CustomAgent %s sent task %s", self.id, task.id)
        # Completion is handled by _read_loop via handle_output

    # ------------------------------------------------------------------
    # Output reading
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        """Continuously read JSON lines from stdout."""
        assert self._proc and self._proc.stdout
        while True:
            try:
                raw = await self._proc.stdout.readline()
                if not raw:
                    logger.info("CustomAgent %s stdout closed", self.id)
                    break
                await self.handle_output(raw.decode().strip())
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("CustomAgent %s read error: %s", self.id, exc)

    async def handle_output(self, text: str) -> None:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.debug("CustomAgent %s non-JSON output: %s", self.id, text)
            return

        task_id = data.get("task_id", self._current_task.id if self._current_task else "unknown")
        msg = Message(
            type=MessageType.RESULT,
            from_id=self.id,
            payload={"task_id": task_id, "result": data.get("result", data)},
        )
        await self.bus.publish(msg)
        self._set_idle()
        logger.info("CustomAgent %s published result for task %s", self.id, task_id)

    async def notify_stdin(self, notification: str) -> None:
        """Write *notification* as a line to the subprocess stdin."""
        if self._proc is None or self._proc.stdin is None:
            return
        line = notification + "\n"
        self._proc.stdin.write(line.encode())
        await self._proc.stdin.drain()
