"""CLI entry point for TmuxAgentOrchestrator."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sys
import urllib.error
import urllib.request
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

    config = load_config(config_path)
    bus = Bus()

    def _confirm_kill(session_name: str) -> bool:
        return typer.confirm(
            f"tmux session '{session_name}' already exists. Kill it and start fresh?",
            default=False,
        )

    tmux = TmuxInterface(session_name=config.session_name, bus=bus, confirm_kill=_confirm_kill)
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
                task_timeout=config.task_timeout or None,
                role=agent_cfg.role,
            )
        else:
            typer.echo(f"[error] Unknown agent type: {agent_cfg.type!r}", err=True)
            raise typer.Exit(1)

        orchestrator.register_agent(agent)

    return orchestrator, bus, tmux


def _patch_web_url(orchestrator: Orchestrator, host: str, port: int) -> None:
    """Update the web_base_url on all ClaudeCodeAgents to match the actual listen address.

    When ``--port`` differs from the config default, or ``--host`` is ``0.0.0.0``,
    the stored URL would be wrong.  This corrects every registered agent in-place
    so that the ``__orchestrator_context__.json`` re-written on the next task will
    reflect the real address.
    """
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent  # noqa: PLC0415

    display_host = "localhost" if host in ("0.0.0.0", "") else host
    url = f"http://{display_host}:{port}"
    for agent in orchestrator._agents.values():
        if isinstance(agent, ClaudeCodeAgent):
            agent._web_base_url = url


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
            tmux.kill_session()

    asyncio.run(_main())


@app.command()
def web(
    config: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to YAML config file"),
    ] = Path("examples/basic_config.yaml"),
    host: Annotated[str, typer.Option("--host", "-H")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p")] = 8000,
    api_key: Annotated[
        Optional[str],
        typer.Option("--api-key", "-k", help="API key for auth (auto-generated if omitted)"),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Launch the FastAPI web server (REST + WebSocket + browser UI)."""
    _setup_logging(verbose)
    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.web.ws import WebSocketHub

    if api_key is None:
        api_key = secrets.token_urlsafe(24)

    orchestrator, bus, tmux = _build_system(config)
    _patch_web_url(orchestrator, host, port)
    hub = WebSocketHub(bus=bus)
    fastapi_app = create_app(orchestrator=orchestrator, hub=hub, api_key=api_key)

    async def _startup() -> None:
        await orchestrator.start()

    async def _shutdown() -> None:
        await orchestrator.stop()
        tmux.stop_watcher()
        tmux.kill_session()

    fastapi_app.add_event_handler("startup", _startup)
    fastapi_app.add_event_handler("shutdown", _shutdown)

    display_host = "localhost" if host in ("0.0.0.0", "") else host
    typer.echo(f"Web UI:  http://{display_host}:{port}/")
    typer.echo(f"API key: {api_key}  (for CLI / agents)")
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
        tmux.kill_session()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


@app.command()
def chat(
    url: Annotated[str, typer.Option("--url", "-u", help="Orchestrator web URL")] = "http://localhost:8000",
    api_key: Annotated[
        Optional[str],
        typer.Option("--api-key", "-k", help="API key (required when server uses auth)"),
    ] = None,
    message: Annotated[Optional[str], typer.Option("--message", "-m", help="Single message (non-interactive)")] = None,
    timeout: Annotated[int, typer.Option("--timeout", "-t", help="Response timeout in seconds")] = 300,
) -> None:
    """Chat with the Director agent via the web API."""

    def send(msg: str) -> str:
        payload = json.dumps({"message": msg}).encode()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        req = urllib.request.Request(
            f"{url}/director/chat?wait=true",
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read()).get("response", "")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise typer.BadParameter(f"HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise typer.BadParameter(f"Cannot reach {url}: {e.reason}") from e

    if message:
        typer.echo(send(message))
        return

    typer.echo(f"Director Chat at {url}  (Ctrl-C or empty line × 2 to exit)\n")
    empty_count = 0
    while True:
        try:
            msg = typer.prompt("You")
        except (KeyboardInterrupt, EOFError):
            break
        if not msg.strip():
            empty_count += 1
            if empty_count >= 2:
                break
            continue
        empty_count = 0
        typer.echo("Director: ", nl=False)
        try:
            response = send(msg)
            typer.echo(response)
        except typer.BadParameter as e:
            typer.echo(f"[error] {e}", err=True)
        typer.echo()


# ---------------------------------------------------------------------------
# Allow `python -m tmux_orchestrator` usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
