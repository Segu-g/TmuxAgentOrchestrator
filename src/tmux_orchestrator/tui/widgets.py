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
    """Bottom status bar showing pause/run state and aggregate agent stats."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
    }
    """

    paused: reactive[bool] = reactive(False)
    tasks_completed: reactive[int] = reactive(0)
    active_agents: reactive[int] = reactive(0)
    high_error_agents: reactive[list] = reactive([])

    def update_stats(
        self,
        *,
        tasks_completed: int,
        active_agents: int,
        high_error_agents: list[str],
    ) -> None:
        """Update aggregate stats displayed in the status bar.

        Parameters
        ----------
        tasks_completed:
            Total tasks completed across all agents (success only).
        active_agents:
            Number of agents currently in IDLE or BUSY state (i.e. not stopped).
        high_error_agents:
            List of agent IDs whose error_rate exceeds 20%.  Shown as a
            warning indicator to alert operators.
        """
        self.tasks_completed = tasks_completed
        self.active_agents = active_agents
        self.high_error_agents = list(high_error_agents)

    def render(self) -> str:
        state = "[yellow]PAUSED[/yellow]" if self.paused else "[green]RUNNING[/green]"
        stats_parts = [
            f"agents:[cyan]{self.active_agents}[/cyan]",
            f"done:[cyan]{self.tasks_completed}[/cyan]",
        ]
        if self.high_error_agents:
            warn_ids = ",".join(self.high_error_agents[:3])
            if len(self.high_error_agents) > 3:
                warn_ids += f"+{len(self.high_error_agents) - 3}"
            stats_parts.append(f"[red]ERR%↑:[/red][red]{warn_ids}[/red]")
        stats_str = "  ".join(stats_parts)
        return (
            f" {state}  {stats_str}  "
            "[bold]n[/bold] new task  "
            "[bold]k[/bold] kill agent  "
            "[bold]p[/bold] pause/resume  "
            "[bold]q[/bold] quit"
        )
