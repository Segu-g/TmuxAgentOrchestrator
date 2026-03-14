"""System factory: wires Bus, TmuxInterface, Orchestrator, and Agents from config.

Separates infrastructure wiring from CLI concerns (Layered Architecture principle).
The factory knows *how* to compose components; the CLI knows *when* to run them.

Design decision: `confirm_kill` is an injected callback so that the factory has
no dependency on ``typer`` or any interactive I/O library.  Test callers can
supply a lambda that always returns True; the CLI supplies ``typer.confirm``.

Reference: Fowler "Patterns of Enterprise Application Architecture" (2002) Ch. 14
           (Service Locator / Factory); DESIGN.md §10.5 (2026-03-05).

Note: This is the canonical implementation location (application/factory.py).
      The root factory.py is a backward-compat shim that re-exports from here.
      DESIGN.md §10.60 (v1.1.28 — Clean Architecture Phase 6: factory.py migration)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from tmux_orchestrator.application.bus import Bus
from tmux_orchestrator.application.config import load_config
from tmux_orchestrator.messaging import Mailbox
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.tmux_interface import TmuxInterface
from tmux_orchestrator.worktree import WorktreeManager

logger = logging.getLogger(__name__)


def _resolve_system_prompt(agent_cfg, config_path: Path) -> str | None:
    """Resolve the effective system prompt for an agent.

    Priority:
    1. ``agent_cfg.system_prompt`` — explicit inline prompt wins.
    2. ``agent_cfg.system_prompt_file`` — read content from file.
    3. ``None`` — no system prompt.

    Relative paths in ``system_prompt_file`` are resolved from the directory
    containing *config_path*.

    Raises FileNotFoundError if the file does not exist.
    """
    if agent_cfg.system_prompt is not None:
        return agent_cfg.system_prompt
    if agent_cfg.system_prompt_file is not None:
        sp_path = Path(agent_cfg.system_prompt_file)
        if not sp_path.is_absolute():
            sp_path = config_path.parent / sp_path
        if not sp_path.exists():
            raise FileNotFoundError(
                f"system_prompt_file not found: {sp_path!r} "
                f"(specified in agent '{agent_cfg.id}')"
            )
        return sp_path.read_text()
    return None


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

    # Determine the base directory for git worktree operations.
    # Priority:
    #   1. config.repo_root — explicit override (resolves the "cwd=PROJECT_ROOT" demo bug
    #      where launching from a non-repo directory caused worktrees to land in the wrong repo).
    #   2. config_path.parent — directory containing the YAML config file (good heuristic: the
    #      config is typically committed inside the target repo, so its parent is a .git ancestor).
    #   3. Path.cwd() — legacy fallback for callers that don't set repo_root and launch from
    #      inside the target repo (pre-v1.0.0 behaviour; preserved for backwards compatibility).
    #
    # Reference: DESIGN.md §10.17 (v1.0.0 — worktree cwd bug fix)
    cwd = Path.cwd()
    wm_base: Path
    if config.repo_root is not None:
        wm_base = config.repo_root
    elif WorktreeManager.find_repo_root(config_path.parent) is not None:
        wm_base = config_path.parent
    else:
        wm_base = cwd
    try:
        wm: WorktreeManager | None = WorktreeManager(wm_base)
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
                api_key=config.api_key,
                task_timeout=effective_timeout,
                role=agent_cfg.role,
                command=agent_cfg.command
                or "env -u CLAUDECODE claude --dangerously-skip-permissions",
                system_prompt=_resolve_system_prompt(agent_cfg, config_path),
                context_files=agent_cfg.context_files,
                context_files_root=cwd if agent_cfg.context_files else None,
                context_spec_files=agent_cfg.context_spec_files,
                context_spec_files_root=cwd if agent_cfg.context_spec_files else None,
                spec_files=agent_cfg.spec_files,
                spec_files_root=cwd if agent_cfg.spec_files else None,
                tags=agent_cfg.tags,
                merge_on_stop=agent_cfg.merge_on_stop,
                merge_target=agent_cfg.merge_target,
                cleanup_subdir=agent_cfg.cleanup_subdir,
                keep_branch_on_stop=agent_cfg.keep_branch_on_stop,
                role_rules_file=agent_cfg.role_rules_file,
            )
        else:
            raise ValueError(f"Unknown agent type: {agent_cfg.type!r}")

        orchestrator.register_agent(agent)

        # Pre-register agent into named groups listed in its AgentConfig.
        # Groups that don't exist yet are created on-the-fly so that
        # agent-level group membership works even without a top-level groups:
        # entry in the YAML (auto-create semantics).
        # DESIGN.md §10.26 (v0.31.0)
        gm = orchestrator.get_group_manager()
        for group_name in agent_cfg.groups:
            if group_name not in gm:
                gm.create(group_name)
            gm.add_agent(group_name, agent_cfg.id)

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


def patch_api_key(orchestrator: Orchestrator, api_key: str) -> None:
    """Update ``api_key`` on all ``ClaudeCodeAgent`` instances and the orchestrator config.

    Called after the web server is started so that the API key (which may have been
    auto-generated if not supplied on the CLI) is propagated to the orchestrator config
    and to each agent's context file.

    This ensures that ``notify_parent()``, ``/progress``, and other slash commands that
    call the REST API will include the correct ``X-API-Key`` header.
    """
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent  # noqa: PLC0415

    orchestrator.config.api_key = api_key
    for agent in orchestrator.registry.all_agents().values():
        if isinstance(agent, ClaudeCodeAgent):
            agent._api_key = api_key


__all__ = ["build_system", "patch_api_key", "patch_web_url"]
