"""CLI entry point for TmuxAgentOrchestrator."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
import uvicorn

from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import load_config
from tmux_orchestrator.messaging import Mailbox
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.tmux_interface import TmuxInterface
from tmux_orchestrator.worktree import WorktreeManager

app = typer.Typer(
    name="tmux-orchestrator",
    help="Orchestrate AI agents inside tmux panes.",
    add_completion=False,
)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


_logger = logging.getLogger(__name__)


def _build_system(config_path: Path) -> tuple[Orchestrator, Bus, TmuxInterface]:
    """Instantiate all core components from a config file."""
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent
    from tmux_orchestrator.agents.custom import CustomAgent

    config = load_config(config_path)
    bus = Bus()
    tmux = TmuxInterface(session_name=config.session_name, bus=bus)
    mailbox = Mailbox(root_dir=config.mailbox_dir, session_name=config.session_name)

    try:
        wm: WorktreeManager | None = WorktreeManager(Path.cwd())
    except RuntimeError:
        _logger.warning("Not inside a git repository; worktree isolation disabled")
        wm = None

    orchestrator = Orchestrator(bus=bus, tmux=tmux, config=config, worktree_manager=wm)

    for agent_cfg in config.agents:
        if agent_cfg.type == "claude_code":
            agent = ClaudeCodeAgent(
                agent_id=agent_cfg.id,
                bus=bus,
                tmux=tmux,
                mailbox=mailbox,
                worktree_manager=wm,
                isolate=agent_cfg.isolate,
                session_name=config.session_name,
                web_base_url=config.web_base_url,
            )
        elif agent_cfg.type == "custom":
            if not agent_cfg.command:
                typer.echo(
                    f"[error] Agent {agent_cfg.id!r} has type=custom but no command",
                    err=True,
                )
                raise typer.Exit(1)
            agent = CustomAgent(
                agent_id=agent_cfg.id,
                bus=bus,
                tmux=tmux,
                command=agent_cfg.command,
                mailbox=mailbox,
                worktree_manager=wm,
                isolate=agent_cfg.isolate,
            )
        else:
            typer.echo(f"[error] Unknown agent type: {agent_cfg.type!r}", err=True)
            raise typer.Exit(1)

        orchestrator.register_agent(agent)

    return orchestrator, bus, tmux


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def tui(
    config: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to YAML config file"),
    ] = Path("examples/basic_config.yaml"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Launch the Textual TUI."""
    _setup_logging(verbose)
    from tmux_orchestrator.tui.app import OrchestratorApp

    orchestrator, bus, tmux = _build_system(config)

    async def _main() -> None:
        await orchestrator.start()
        try:
            tui_app = OrchestratorApp(orchestrator)
            await tui_app.run_async()
        finally:
            await orchestrator.stop()
            tmux.stop_watcher()

    asyncio.run(_main())


@app.command()
def web(
    config: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to YAML config file"),
    ] = Path("examples/basic_config.yaml"),
    host: Annotated[str, typer.Option("--host", "-H")] = "0.0.0.0",
    port: Annotated[int, typer.Option("--port", "-p")] = 8000,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Launch the FastAPI web server (REST + WebSocket + browser UI)."""
    _setup_logging(verbose)
    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.web.ws import WebSocketHub

    orchestrator, bus, tmux = _build_system(config)
    hub = WebSocketHub(bus=bus)
    fastapi_app = create_app(orchestrator=orchestrator, hub=hub)

    async def _startup() -> None:
        await orchestrator.start()

    async def _shutdown() -> None:
        await orchestrator.stop()
        tmux.stop_watcher()

    fastapi_app.add_event_handler("startup", _startup)
    fastapi_app.add_event_handler("shutdown", _shutdown)

    typer.echo(f"Starting web server at http://{host}:{port}")
    uvicorn.run(fastapi_app, host=host, port=port, log_level="warning")


@app.command()
def run(
    config: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to YAML config file"),
    ] = Path("examples/basic_config.yaml"),
    prompt: Annotated[
        Optional[str],
        typer.Option("--prompt", help="Submit a single task and wait for result"),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Start agents headlessly; optionally submit one task and print the result."""
    _setup_logging(verbose)
    logger = logging.getLogger(__name__)

    orchestrator, bus, tmux = _build_system(config)

    async def _main() -> None:
        await orchestrator.start()
        if prompt:
            result_q = await bus.subscribe("__cli__", broadcast=False)
            task = await orchestrator.submit_task(prompt)
            logger.info("Submitted task %s", task.id)
            # Wait for the result
            async for msg in bus.iter_messages(result_q):
                from tmux_orchestrator.bus import MessageType

                if msg.type == MessageType.RESULT and msg.payload.get("task_id") == task.id:
                    output = msg.payload.get("output") or msg.payload.get("result")
                    typer.echo(output)
                    break
            await bus.unsubscribe("__cli__")
        else:
            typer.echo("Agents running. Press Ctrl-C to stop.")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
        await orchestrator.stop()
        tmux.stop_watcher()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Allow `python -m tmux_orchestrator` usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
