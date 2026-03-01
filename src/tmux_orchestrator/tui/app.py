"""Textual TUI application root."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input

from tmux_orchestrator.bus import Message, MessageType
from tmux_orchestrator.tui.widgets import AgentPanel, LogPanel, StatusBar, TaskQueuePanel

if TYPE_CHECKING:
    from tmux_orchestrator.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class OrchestratorApp(App):
    """Main TUI for TmuxAgentOrchestrator."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #top-row {
        height: 1fr;
        layout: horizontal;
    }
    AgentPanel {
        width: 2fr;
    }
    TaskQueuePanel {
        width: 1fr;
    }
    LogPanel {
        height: 12;
    }
    Input {
        display: none;
    }
    Input.visible {
        display: block;
    }
    """

    BINDINGS = [
        Binding("n", "new_task", "New Task"),
        Binding("k", "kill_agent", "Kill Agent"),
        Binding("p", "toggle_pause", "Pause/Resume"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "cancel_input", "Cancel", show=False),
    ]

    def __init__(self, orchestrator: "Orchestrator") -> None:
        super().__init__()
        self.orchestrator = orchestrator
        self._refresh_interval = 1.0  # seconds

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            with Horizontal(id="top-row"):
                yield AgentPanel()
                yield TaskQueuePanel()
            yield LogPanel()
            yield Input(placeholder="Enter task prompt…", id="task-input")
            yield StatusBar()
        yield Footer()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def on_mount(self) -> None:
        self.set_interval(self._refresh_interval, self._refresh_ui)
        self._bus_task = asyncio.create_task(
            self._listen_bus(), name="tui-bus-listener"
        )

    async def _refresh_ui(self) -> None:
        self.query_one(AgentPanel).refresh_agents(self.orchestrator.list_agents())
        self.query_one(TaskQueuePanel).refresh_tasks(self.orchestrator.list_tasks())
        status_bar = self.query_one(StatusBar)
        status_bar.paused = self.orchestrator.is_paused

    async def _listen_bus(self) -> None:
        q = await self.orchestrator.bus.subscribe("__tui__", broadcast=True)
        async for msg in self.orchestrator.bus.iter_messages(q):
            self._handle_bus_msg(msg)

    def _handle_bus_msg(self, msg: Message) -> None:
        log = self.query_one(LogPanel)
        if msg.type == MessageType.RESULT:
            task_id = msg.payload.get("task_id", "?")[:8]
            log.append(f"[green]RESULT[/green] {msg.from_id} task {task_id} done")
        elif msg.type == MessageType.STATUS:
            event = msg.payload.get("event", "")
            if event == "task_queued":
                tid = msg.payload.get("task_id", "?")[:8]
                log.append(f"[blue]QUEUED[/blue] task {tid}")
        elif msg.type == MessageType.PEER_MSG:
            log.append(
                f"[magenta]P2P[/magenta] {msg.from_id} → {msg.to_id}: "
                f"{str(msg.payload)[:60]}"
            )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_new_task(self) -> None:
        inp = self.query_one("#task-input", Input)
        inp.add_class("visible")
        inp.focus()

    def action_kill_agent(self) -> None:
        agents = self.orchestrator.list_agents()
        if not agents:
            self.query_one(LogPanel).append("[red]No agents to kill[/red]")
            return
        # Kill the first non-stopped agent for simplicity; a real UI would prompt
        for a in agents:
            if a["status"] != "STOPPED":
                agent_obj = self.orchestrator.get_agent(a["id"])
                if agent_obj:
                    asyncio.create_task(agent_obj.stop())
                    self.query_one(LogPanel).append(
                        f"[red]KILLED[/red] agent {a['id']}"
                    )
                    return

    def action_toggle_pause(self) -> None:
        if self.orchestrator.is_paused:
            self.orchestrator.resume()
            self.query_one(LogPanel).append("[green]Dispatch resumed[/green]")
        else:
            self.orchestrator.pause()
            self.query_one(LogPanel).append("[yellow]Dispatch paused[/yellow]")

    def action_cancel_input(self) -> None:
        inp = self.query_one("#task-input", Input)
        inp.remove_class("visible")
        inp.value = ""

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        if prompt:
            task = await self.orchestrator.submit_task(prompt)
            self.query_one(LogPanel).append(
                f"[blue]SUBMITTED[/blue] task {task.id[:8]}: {prompt[:40]}"
            )
        self.action_cancel_input()
