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

from tmux_orchestrator.factory import build_system, patch_api_key, patch_web_url
from tmux_orchestrator.logging_config import setup_json_logging, setup_text_logging

app = typer.Typer(
    name="tmux-orchestrator",
    help="Orchestrate AI agents inside tmux panes.",
    add_completion=False,
)


def _setup_logging(verbose: bool, json_logs: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    if json_logs:
        setup_json_logging(level)
    else:
        setup_text_logging(level)


_logger = logging.getLogger(__name__)


def _build_system(config_path: Path):  # type: ignore[return]
    """Instantiate all core components from a config file."""
    def _confirm_kill(session_name: str) -> bool:
        return typer.confirm(
            f"tmux session '{session_name}' already exists. Kill it and start fresh?",
            default=False,
        )

    try:
        return build_system(config_path, confirm_kill=_confirm_kill)
    except ValueError as exc:
        typer.echo(f"[error] {exc}", err=True)
        raise typer.Exit(1) from exc


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
    json_logs: Annotated[bool, typer.Option("--json-logs", help="Emit structured JSON logs")] = False,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Resume from last checkpoint (requires checkpoint_enabled in config)"),
    ] = False,
) -> None:
    """Launch the FastAPI web server (REST + WebSocket + browser UI)."""
    _setup_logging(verbose, json_logs=json_logs)
    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.web.ws import WebSocketHub

    if api_key is None:
        api_key = secrets.token_urlsafe(24)

    orchestrator, bus, tmux = _build_system(config)
    patch_web_url(orchestrator, host, port)
    patch_api_key(orchestrator, api_key)
    hub = WebSocketHub(bus=bus)

    async def _startup() -> None:
        await orchestrator.start(resume=resume)

    async def _shutdown() -> None:
        await orchestrator.stop()
        tmux.stop_watcher()
        tmux.kill_session()

    # Pass on_startup / on_shutdown into create_app's lifespan so that the
    # orchestrator starts inside the FastAPI lifespan context.  Using
    # router.on_startup is NOT sufficient when a lifespan context manager is
    # defined — FastAPI ≥ 0.93 ignores router startup hooks in that case.
    fastapi_app = create_app(
        orchestrator=orchestrator,
        hub=hub,
        api_key=api_key,
        on_startup=_startup,
        on_shutdown=_shutdown,
        cors_origins=orchestrator.config.cors_origins,
    )

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
    json_logs: Annotated[bool, typer.Option("--json-logs", help="Emit structured JSON logs")] = False,
) -> None:
    """Start agents headlessly; optionally submit one task and print the result."""
    _setup_logging(verbose, json_logs=json_logs)
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
