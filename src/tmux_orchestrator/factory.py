"""System factory: wires Bus, TmuxInterface, Orchestrator, and Agents from config.

Separates infrastructure wiring from CLI concerns (Layered Architecture principle).
The factory knows *how* to compose components; the CLI knows *when* to run them.

Design decision: `confirm_kill` is an injected callback so that the factory has
no dependency on ``typer`` or any interactive I/O library.  Test callers can
supply a lambda that always returns True; the CLI supplies ``typer.confirm``.

Reference: Fowler "Patterns of Enterprise Application Architecture" (2002) Ch. 14
           (Service Locator / Factory); DESIGN.md §10.5 (2026-03-05).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import load_config
from tmux_orchestrator.messaging import Mailbox
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.tmux_interface import TmuxInterface
from tmux_orchestrator.worktree import WorktreeManager

logger = logging.getLogger(__name__)


def build_system(
    config_path: Path,
    *,
    confirm_kill: Callable[[str], bool] | None = None,
) -> tuple[Orchestrator, Bus, TmuxInterface]:
    """Instantiate and wire all core components from *config_path*.

    Returns ``(orchestrator, bus, tmux)`` — callers are responsible for
    calling ``await orchestrator.start()`` / ``await orchestrator.stop()``
    and ``tmux.stop_watcher()`` / ``tmux.kill_session()`` at the right times.

    Parameters
    ----------
    config_path:
        Path to a YAML orchestrator config file.
    confirm_kill:
        Optional callback invoked when a tmux session with the same name already
        exists.  Receives the session name; should return ``True`` to kill the
        existing session and start fresh, ``False`` to abort.  When ``None``,
        TmuxInterface uses its default behaviour (raises ``RuntimeError`` if a
        session already exists).
    """
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent  # noqa: PLC0415

    config = load_config(config_path)
    bus = Bus()
    tmux = TmuxInterface(
        session_name=config.session_name,
        bus=bus,
        confirm_kill=confirm_kill,
    )
    mailbox = Mailbox(root_dir=config.mailbox_dir, session_name=config.session_name)

    cwd = Path.cwd()
    try:
        wm: WorktreeManager | None = WorktreeManager(cwd)
    except RuntimeError:
        logger.warning("Not inside a git repository; worktree isolation disabled")
        wm = None

    orchestrator = Orchestrator(bus=bus, tmux=tmux, config=config, worktree_manager=wm)

    for agent_cfg in config.agents:
        if agent_cfg.type == "claude_code":
            effective_timeout = (
                agent_cfg.task_timeout
                if agent_cfg.task_timeout is not None
                else (config.task_timeout or None)
            )
            agent = ClaudeCodeAgent(
                agent_id=agent_cfg.id,
                bus=bus,
                tmux=tmux,
                mailbox=mailbox,
                worktree_manager=wm,
                isolate=agent_cfg.isolate,
                session_name=config.session_name,
                web_base_url=config.web_base_url,
                task_timeout=effective_timeout,
                role=agent_cfg.role,
                command=agent_cfg.command
                or "env -u CLAUDECODE claude --dangerously-skip-permissions",
                system_prompt=agent_cfg.system_prompt,
                context_files=agent_cfg.context_files,
                context_files_root=cwd if agent_cfg.context_files else None,
            )
        else:
            raise ValueError(f"Unknown agent type: {agent_cfg.type!r}")

        orchestrator.register_agent(agent)

    return orchestrator, bus, tmux


def patch_web_url(orchestrator: Orchestrator, host: str, port: int) -> None:
    """Update ``web_base_url`` on all ``ClaudeCodeAgent`` instances.

    When ``--port`` differs from the config default, or ``--host`` is
    ``0.0.0.0``, the URL stored during construction would be wrong.  This
    corrects every registered agent in-place so that the
    ``__orchestrator_context__.json`` written on the next task reflects the
    actual listen address.
    """
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent  # noqa: PLC0415

    display_host = "localhost" if host in ("0.0.0.0", "") else host
    url = f"http://{display_host}:{port}"
    for agent in orchestrator.registry.all_agents().values():
        if isinstance(agent, ClaudeCodeAgent):
            agent._web_base_url = url
