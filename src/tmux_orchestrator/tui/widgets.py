"""Textual widgets for the TmuxAgentOrchestrator TUI."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import DataTable, Label, RichLog, Static

if TYPE_CHECKING:
    from tmux_orchestrator.orchestrator import Orchestrator


class AgentPanel(Widget):
    """Displays the live status table of all registered agents."""

    DEFAULT_CSS = """
    AgentPanel {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    AgentPanel > Label {
        text-style: bold;
        color: $accent;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Agents")
        yield DataTable(id="agents-table")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("ID", "Status", "Current Task")

    def refresh_agents(self, agents: list[dict]) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for a in agents:
            status = a["status"]
            style = {
                "IDLE": "green",
                "BUSY": "yellow",
                "ERROR": "red",
                "STOPPED": "dim",
            }.get(status, "white")
            table.add_row(
                a["id"],
                f"[{style}]{status}[/{style}]",
                a["current_task"] or "—",
            )


class TaskQueuePanel(Widget):
    """Displays the pending task queue."""

    DEFAULT_CSS = """
    TaskQueuePanel {
        height: 1fr;
        border: round $secondary;
        padding: 0 1;
    }
    TaskQueuePanel > Label {
        text-style: bold;
        color: $accent;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Task Queue")
        yield DataTable(id="tasks-table")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Priority", "Task ID", "Prompt")

    def refresh_tasks(self, tasks: list[dict]) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for t in tasks:
            prompt_preview = t["prompt"][:40] + ("…" if len(t["prompt"]) > 40 else "")
            table.add_row(str(t["priority"]), t["task_id"][:8], prompt_preview)


class LogPanel(Widget):
    """Scrolling log of bus events."""

    DEFAULT_CSS = """
    LogPanel {
        height: 10;
        border: round $surface;
        padding: 0 1;
    }
    LogPanel > Label {
        text-style: bold;
        color: $accent;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Logs")
        yield RichLog(id="log", markup=True, wrap=True)

    def append(self, text: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        log = self.query_one(RichLog)
        log.write(f"[dim]{ts}[/dim] {text}")


class StatusBar(Static):
    """Bottom status bar showing pause/run state."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
    }
    """

    paused: reactive[bool] = reactive(False)

    def render(self) -> str:
        state = "[yellow]PAUSED[/yellow]" if self.paused else "[green]RUNNING[/green]"
        return (
            f" {state}  "
            "[bold]n[/bold] new task  "
            "[bold]k[/bold] kill agent  "
            "[bold]p[/bold] pause/resume  "
            "[bold]q[/bold] quit"
        )
