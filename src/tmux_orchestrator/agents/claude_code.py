"""Agent that manages a `claude` CLI process inside a tmux pane."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Message, MessageType
from tmux_orchestrator.config import AgentRole

if TYPE_CHECKING:
    import libtmux

    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.messaging import Mailbox
    from tmux_orchestrator.tmux_interface import TmuxInterface
    from tmux_orchestrator.worktree import WorktreeManager

logger = logging.getLogger(__name__)

# Patterns that indicate Claude has finished and is waiting for input.
# Adjust as the `claude` CLI evolves.
# NOTE: match end-of-line (not whole-line) so trailing terminal padding / cursor
# markers added by newer CLI versions do not prevent completion detection.
_DONE_PATTERNS = [
    re.compile(r"❯\s*$", re.MULTILINE),               # claude interactive prompt "❯"
    re.compile(r"(?<!\S)>\s*$", re.MULTILINE),         # bare ">" prompt (older versions)
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
        command: str = "env -u CLAUDECODE claude --dangerously-skip-permissions",
        mailbox: "Mailbox | None" = None,
        worktree_manager: "WorktreeManager | None" = None,
        isolate: bool = True,
        cwd_override: Path | None = None,
        session_name: str = "orchestrator",
        web_base_url: str = "http://localhost:8000",
        api_key: str = "",
        task_timeout: float | None = None,
        role: AgentRole | str = AgentRole.WORKER,
        parent_pane: "libtmux.Pane | None" = None,
        # --- Context engineering ---
        system_prompt: str | None = None,
        context_files: list[str] | None = None,
        context_files_root: Path | None = None,
        # context_spec_files: glob patterns for cold-memory specification docs.
        # Reference: Vasilopoulos arXiv:2602.20478 "Codified Context" (2026).
        context_spec_files: list[str] | None = None,
        context_spec_files_root: Path | None = None,
        # --- Capability tags ---
        tags: list[str] | None = None,
        # --- Worktree lifecycle ---
        merge_on_stop: bool = False,
        merge_target: str | None = None,
    ) -> None:
        super().__init__(agent_id, bus, task_timeout=task_timeout)
        self.mailbox = mailbox
        self._tmux = tmux
        self._command = command
        self._last_output: str = ""
        self._settle_count: int = 0
        self._worktree_manager = worktree_manager
        self._isolate = isolate
        self._merge_on_stop = merge_on_stop
        self._merge_target = merge_target
        self._cwd_override = cwd_override
        self._session_name = session_name
        self._web_base_url = web_base_url
        self._api_key = api_key
        # Normalise role to AgentRole enum for consistent comparisons
        self.role: AgentRole = AgentRole(role) if isinstance(role, str) else role
        # When set, the agent is a sub-agent and shares its parent's tmux window
        self._parent_pane: "libtmux.Pane | None" = parent_pane
        # Context engineering: per-agent context localization
        self._system_prompt: str | None = system_prompt
        self._context_files: list[str] = context_files or []
        # Root directory from which context_files paths are resolved.
        # Defaults to None (not set); callers must provide this if context_files is non-empty.
        self._context_files_root: Path | None = context_files_root
        # Cold-memory spec files: glob patterns for specification documents.
        self._context_spec_files: list[str] = context_spec_files or []
        self._context_spec_files_root: Path | None = context_spec_files_root
        # Capability tags: advertised capabilities used for smart dispatch.
        self.tags: list[str] = tags or []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._parent_pane is not None:
            # Sub-agent: split the parent's window to stay in the same tmux window
            pane = await loop.run_in_executor(
                None, self._tmux.new_subpane, self._parent_pane, self.id
            )
        else:
            # Top-level agent: each gets its own tmux window
            pane = await loop.run_in_executor(None, self._tmux.new_pane, self.id)
        self.pane = pane
        cwd = await self._setup_worktree()
        if cwd is not None:
            await loop.run_in_executor(None, self._write_context_file, cwd)
            # Write API key to a separate 0o600 file (not in __orchestrator_context__.json).
            # Also inject as tmux session environment variable so panes can inherit it.
            if self._api_key:
                await loop.run_in_executor(None, self._write_api_key_file, cwd)
                await loop.run_in_executor(
                    None, self._set_session_env_api_key
                )
            # Only write agent-specific CLAUDE.md in isolated worktrees.
            # Non-isolated agents share an existing directory that may already have
            # a project-level CLAUDE.md — overwriting it would destroy project context.
            if self._isolate:
                await loop.run_in_executor(None, self._write_agent_claude_md, cwd)
            await loop.run_in_executor(None, self._write_notes_template, cwd)
            await loop.run_in_executor(None, self._copy_context_files, cwd)
            await loop.run_in_executor(None, self._copy_context_spec_files, cwd)
            await loop.run_in_executor(None, self._write_stop_hook_settings, cwd)
        launch = (
            f"cd {shlex.quote(str(cwd))} && {self._command}" if cwd else self._command
        )
        await loop.run_in_executor(None, self._tmux.send_keys, pane, launch)
        self._tmux.watch_pane(pane, self.id)
        self._tmux.start_watcher()
        await self._wait_for_ready()
        self.status = AgentStatus.IDLE
        if self.role == AgentRole.DIRECTOR:
            await loop.run_in_executor(
                None, self._tmux.send_keys, pane, self._director_startup_prompt()
            )
        self._run_task = asyncio.create_task(self._run_loop(), name=f"{self.id}-loop")
        await self._start_message_loop()
        logger.info("ClaudeCodeAgent %s started in pane %s (role=%s)", self.id, pane.id, self.role)

    def _context_extras(self) -> dict[str, Any]:
        # NOTE: api_key is intentionally excluded from the context file.
        # It is written to a separate __orchestrator_api_key__ file (chmod 600)
        # and injected as TMUX_ORCHESTRATOR_API_KEY tmux session environment
        # variable.  See DESIGN.md §3 "API キー配送のセキュリティ方針" and
        # §10.30 for the security rationale.
        return {
            "session_name": self._session_name,
            "web_base_url": self._web_base_url,
        }

    def _write_api_key_file(self, cwd: Path) -> None:
        """Write the API key to ``__orchestrator_api_key__`` with mode 0o600.

        The file is created atomically using ``os.open()`` with ``O_CREAT |
        O_TRUNC | O_WRONLY`` and explicit permission bits, preventing the
        system umask from widening the permissions.

        If *api_key* is empty, no file is created.

        References:
          - OpenStack Security Guidelines "Apply Restrictive File Permissions"
            https://security.openstack.org/guidelines/dg_apply-restrictive-file-permissions.html
          - OWASP Secrets Management Cheat Sheet (2025)
        """
        if not self._api_key:
            return
        key_path = cwd / "__orchestrator_api_key__"
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(str(key_path), flags, 0o600)
        try:
            os.write(fd, (self._api_key + "\n").encode())
        finally:
            os.close(fd)
        logger.debug("Agent %s wrote API key file to %s", self.id, key_path)

    def _set_session_env_api_key(self) -> None:
        """Inject TMUX_ORCHESTRATOR_API_KEY as a tmux session environment variable.

        libtmux's ``Session.set_environment()`` calls ``tmux set-environment``,
        which causes subsequently created panes to inherit the variable.
        This means the API key is available in the agent's shell without being
        written to a world-readable file or appearing in shell history.

        References:
          - libtmux docs: https://libtmux.readthedocs.io/en/latest/api.html
          - tmux GitHub Discussion #3997 "Session environment variables"
            https://github.com/orgs/tmux/discussions/3997
          - DESIGN.md §3, §10.30 "API キー配送のセキュリティ方針"
        """
        if not self._api_key:
            return
        try:
            session = self._tmux.ensure_session()
            session.set_environment("TMUX_ORCHESTRATOR_API_KEY", self._api_key)
            logger.debug(
                "Agent %s set TMUX_ORCHESTRATOR_API_KEY on tmux session %s",
                self.id, session.id,
            )
        except Exception:  # noqa: BLE001
            # Non-fatal: the key file is the primary delivery mechanism;
            # the session env var is an additional layer.
            logger.warning(
                "Agent %s: could not set TMUX_ORCHESTRATOR_API_KEY on tmux session",
                self.id, exc_info=True,
            )

    # ------------------------------------------------------------------
    # Context localization — writes agent-specific files to worktree
    # ------------------------------------------------------------------

    def _write_agent_claude_md(self, cwd: Path) -> None:
        """Write an agent-specific CLAUDE.md to the worktree.

        This is the primary mechanism for **context localization**: each agent
        receives a focused, role-appropriate set of instructions rather than a
        generic prompt, minimising irrelevant tokens and 'context rot'.

        Content structure (Context Engineering principle: right-altitude guidance):
        - Role and identity (who this agent is in the hierarchy)
        - Communication protocol (how to receive/send messages)
        - Working conventions (TDD, note-taking, progress reporting)
        - Custom system_prompt if provided in config
        """
        api = self._web_base_url
        parent_section = (
            f"Your parent agent is the agent that spawned you. "
            f"Report progress and results upward using `/progress` or `/send-message`."
            if self._parent_pane is not None
            else "You are a top-level agent. Report results to the orchestrator."
        )

        role_desc = {
            AgentRole.DIRECTOR: (
                "You are the **Director** agent. Your responsibilities:\n"
                "- Discuss project goals with the user via the web UI chat\n"
                "- Break goals into concrete subtasks\n"
                "- Delegate subtasks to workers via `POST /tasks` or `/spawn-subagent`\n"
                "- Aggregate worker results and report back to the user\n"
                "- Use `/plan` before starting complex coordination work"
            ),
            AgentRole.WORKER: (
                "You are a **Worker** agent. Your responsibilities:\n"
                "- Receive tasks from the orchestrator queue or from your parent agent\n"
                "- Implement tasks using TDD: write tests first, then implement\n"
                "- Use `/plan` to break large tasks into steps before starting\n"
                "- Report completion via `/progress` when done\n"
                "- Spawn sub-agents via `/spawn-subagent` if the task is parallelisable"
            ),
        }.get(self.role, f"You are a **{self.role.value}** agent.")

        context_files_section = ""
        if self._context_files:
            files_list = "\n".join(f"- `{f}`" for f in self._context_files)
            context_files_section = (
                f"\n## Pre-loaded Context Files\n\n"
                f"The following files contain relevant context for your tasks:\n\n"
                f"{files_list}\n\n"
                f"Read these at startup to understand the project state.\n"
            )

        custom_section = (
            f"\n## Role-Specific Instructions\n\n{self._system_prompt}\n"
            if self._system_prompt
            else ""
        )

        content = f"""\
# Agent: {self.id}

> This file is auto-generated by TmuxAgentOrchestrator. Do not edit manually.
> It defines your identity, communication protocol, and working conventions.

## Identity

- **Agent ID**: `{self.id}`
- **Role**: `{self.role}`
- **Session**: `{self._session_name}`
- **Orchestrator API**: {api}

{role_desc}

{parent_section}

## Communication Protocol

### Receiving messages
When you see `__MSG__:<id>` typed into your pane, a message has arrived:
1. Run `/check-inbox` — list unread messages
2. Run `/read-message <id>` — read and mark as read

### Sending messages
- `/send-message <agent_id> <text>` — send to a specific agent
- Hierarchy rules: you may freely message your parent, your children, and your
  siblings (agents at the same level). Cross-branch communication requires
  explicit permission from the orchestrator config.

### Spawning helpers
- `/spawn-subagent <template_id>` — spawn a sub-agent in your tmux window
- `/list-agents` — list all agents and their status

### Reporting progress
- `/progress <summary>` — send a progress update to your parent agent

## Working Conventions

### Test-Driven Development (TDD)
Follow the Red → Green → Refactor cycle for all implementation work:
1. **Spec**: Use `/plan` to write acceptance criteria before coding
2. **Red**: Write a failing test that captures the requirement
3. **Green**: Write the minimal code to make the test pass
4. **Refactor**: Improve code quality without changing behaviour
5. Run tests frequently; never commit failing tests

### Structured Note-Taking
Maintain `NOTES.md` in your working directory. Update it as you work:
- Key decisions and their rationale
- Completed steps and outstanding work
- Blockers and open questions
Use `/summarize` to compress your progress into NOTES.md when context grows large.

### Task Planning
Use `/plan <description>` before starting any non-trivial task. This writes a
`PLAN.md` with steps, acceptance criteria, and test strategy.

## Context Management

- Your working directory: `{cwd}` (isolated worktree, branch: `worktree/{self.id}`)
- Context file: `__orchestrator_context__.json` (agent_id, mailbox, web_base_url)
- Notes file: `NOTES.md` (your structured scratchpad — keep it updated)
- Plan file: `PLAN.md` (created by `/plan`, deleted when task is done)
{context_files_section}{custom_section}
## Slash Command Reference

| Command | Usage | Purpose |
|---|---|---|
| `/check-inbox` | `/check-inbox` | List unread messages |
| `/read-message` | `/read-message <id>` | Read a message in full |
| `/send-message` | `/send-message <agent_id> <text>` | Send a message |
| `/spawn-subagent` | `/spawn-subagent <template_id>` | Spawn a sub-agent |
| `/list-agents` | `/list-agents` | Show all agent statuses |
| `/plan` | `/plan <description>` | Write PLAN.md before implementing |
| `/tdd` | `/tdd <feature>` | Start a TDD cycle for a feature |
| `/progress` | `/progress <summary>` | Report progress to parent |
| `/summarize` | `/summarize` | Compress context → NOTES.md |
| `/delegate` | `/delegate <task>` | Spawn sub-agents and assign subtasks |
"""
        claude_md_path = cwd / "CLAUDE.md"
        claude_md_path.write_text(content)
        logger.debug("Agent %s wrote CLAUDE.md to %s", self.id, cwd)

    def _copy_context_files(self, cwd: Path) -> None:
        """Copy ``context_files`` (relative paths) into the agent's working directory.

        Each file is copied preserving its relative directory structure so that
        the agent sees the same layout it would in the original repository.

        If ``context_files_root`` is not set and ``context_files`` is non-empty, a
        warning is emitted and the copy is skipped.  Missing individual files also
        emit a per-file warning rather than raising — callers should not crash on a
        misconfigured context path.

        Reference: DESIGN.md §5 (Context Engineering) — context localisation:
        each agent receives only the subset of files relevant to its role.
        """
        if not self._context_files:
            return
        if self._context_files_root is None:
            logger.warning(
                "Agent %s: context_files is set but context_files_root is None — "
                "cannot copy context files; set context_files_root to resolve paths.",
                self.id,
            )
            return
        root = self._context_files_root
        for rel in self._context_files:
            src = root / rel
            if not src.exists():
                logger.warning(
                    "Agent %s: context file %r not found at %s — skipping",
                    self.id, rel, src,
                )
                continue
            dest = cwd / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            logger.debug("Agent %s: copied context file %s → %s", self.id, src, dest)

    def _copy_context_spec_files(self, cwd: Path) -> None:
        """Copy cold-memory specification documents into the agent's working directory.

        Each entry in ``context_spec_files`` is treated as a glob pattern relative to
        ``context_spec_files_root``.  All matching files are copied preserving their
        relative directory structure, so the agent sees the same layout.

        Non-matching patterns are silently skipped (no warning) since globs may match
        zero files when a spec directory is empty.  Literal paths that do not exist emit
        a per-file warning instead of raising.

        Reference: Vasilopoulos arXiv:2602.20478 "Codified Context" (2026-02):
        cold-memory specification documents (Tier 3) are provided on-demand to prevent
        agents from forgetting project conventions across sessions.
        """
        if not self._context_spec_files:
            return
        if self._context_spec_files_root is None:
            logger.warning(
                "Agent %s: context_spec_files is set but context_spec_files_root is None — "
                "cannot copy spec files; set context_spec_files_root to resolve globs.",
                self.id,
            )
            return
        root = self._context_spec_files_root
        for pattern in self._context_spec_files:
            matches = list(root.glob(pattern))
            if not matches:
                # Pattern matched nothing — warn only if it looks like a literal path
                # (no glob metacharacters), otherwise silently skip.
                if not any(c in pattern for c in ("*", "?", "[")):
                    logger.warning(
                        "Agent %s: context_spec_file %r not found at %s — skipping",
                        self.id, pattern, root / pattern,
                    )
                continue
            for src in matches:
                if not src.is_file():
                    continue
                rel = src.relative_to(root)
                dest = cwd / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                logger.debug(
                    "Agent %s: copied spec file %s → %s", self.id, src, dest
                )

    def _write_stop_hook_settings(self, cwd: Path) -> None:
        """Write ``.claude/settings.local.json`` with a Stop hook HTTP handler.

        The Stop hook fires when Claude finishes responding and sends an HTTP
        POST to ``POST /agents/{agent_id}/task-complete`` on the orchestrator
        web server.  This provides deterministic completion detection instead
        of the 500 ms polling + regex fallback.

        The file is placed in ``.claude/settings.local.json`` (gitignored by
        Claude Code conventions) so it does not pollute the repository and is
        cleaned up with the worktree on agent stop.

        If ``web_base_url`` is empty (web server not started), the method is
        a no-op and completion falls back to ``_wait_for_completion`` polling.

        Reference:
        - Claude Code Hooks Reference https://code.claude.com/docs/en/hooks (2025)
        - DESIGN.md §10.12 (v0.38.0)
        """
        if not self._web_base_url:
            return

        url = f"{self._web_base_url}/agents/{self.id}/task-complete"
        settings = {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "http",
                                "url": url,
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
        }
        claude_dir = cwd / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_path = claude_dir / "settings.local.json"
        settings_path.write_text(json.dumps(settings, indent=2))
        logger.debug(
            "Agent %s wrote Stop hook settings to %s (url=%s)",
            self.id, settings_path, url,
        )

    def _write_notes_template(self, cwd: Path) -> None:
        """Write an initial NOTES.md template for structured note-taking."""
        notes_path = cwd / "NOTES.md"
        if notes_path.exists():
            return  # Don't overwrite existing notes
        notes_path.write_text(
            f"# Agent Notes — {self.id}\n\n"
            "## Current Task\n\n"
            "_No task assigned yet._\n\n"
            "## Key Decisions\n\n"
            "_Record important design choices and their rationale here._\n\n"
            "## Progress\n\n"
            "- [ ] Task received\n\n"
            "## Blockers\n\n"
            "_None._\n\n"
            "## Completed\n\n"
            "_Nothing yet._\n"
        )

    def _director_startup_prompt(self) -> str:
        api = self._web_base_url
        return (
            f"You are the Director agent for this TmuxAgentOrchestrator session.\n"
            f"Your role: have a conversation with the user to understand the project goals, "
            f"decide on a plan together, then coordinate worker agents to carry it out.\n\n"
            f"Orchestrator API: {api}\n"
            f"  Check agents:  curl -s {api}/agents\n"
            f"  Submit task:   curl -s -X POST {api}/tasks "
            f"-H 'Content-Type: application/json' "
            f"-d '{{\"prompt\":\"<task>\",\"priority\":0}}'\n"
            f"  Check queue:   curl -s {api}/tasks\n\n"
            f"Your session context is in __orchestrator_context__.json "
            f"(read it with the Read tool for agent IDs and details).\n"
            f"Incoming worker results will appear as mailbox notifications (__MSG__:<id>).\n\n"
            f"Wait for the user. When they describe what they want built:\n"
            f"1. Use /plan to break the work into concrete subtasks with acceptance criteria\n"
            f"2. Submit each subtask to a worker via the API above\n"
            f"3. Monitor progress via /list-agents and worker messages\n"
            f"4. Report results back to the user"
        )

    # ------------------------------------------------------------------
    # Lifecycle — stop
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()
        if self._msg_task:
            self._msg_task.cancel()
        if self.pane:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._tmux.send_keys, self.pane, "q", True
            )
            self._tmux.unwatch_pane(self.pane)
        await self.bus.unsubscribe(self.id)
        await self._teardown_worktree()
        logger.info("ClaudeCodeAgent %s stopped", self.id)

    # ------------------------------------------------------------------
    # Interruption
    # ------------------------------------------------------------------

    async def interrupt(self) -> bool:
        """Send Ctrl-C to the tmux pane to interrupt the running task.

        Uses libtmux ``send_keys("C-c")`` which sends the SIGINT key sequence
        to the pane's foreground process.  Returns ``True`` if a pane is
        attached, ``False`` otherwise.

        After the interrupt, the pane output will eventually settle back to
        the agent's prompt.  The ``_wait_for_completion`` poll loop in
        ``_dispatch_task`` will detect this and publish a RESULT; the
        orchestrator's ``_route_loop`` will discard the result if the task
        has been added to ``_cancelled_task_ids``.

        Design references:
        - POSIX SIGTERM/SIGKILL: send SIGINT (Ctrl-C) before forced teardown
        - Java Future.cancel(mayInterruptIfRunning=true): cooperative interruption
        - Go context.Context cancellation: caller signals intent, callee checks
        - Kubernetes Pod deletion grace period: SIGTERM → wait → SIGKILL
        - DESIGN.md §10.22 (v0.27.0)
        """
        if self.pane is None:
            return False
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self.pane.send_keys, "C-c"
        )
        logger.info("ClaudeCodeAgent %s: sent C-c interrupt to pane %s", self.id, self.pane.id)
        return True

    # ------------------------------------------------------------------
    # Task dispatch
    # ------------------------------------------------------------------

    async def _dispatch_task(self, task: Task) -> None:
        if self.pane is None:
            raise RuntimeError(f"Agent {self.id} has no pane")
        from tmux_orchestrator.security import sanitize_prompt
        safe_prompt = sanitize_prompt(task.prompt)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._tmux.send_keys, self.pane, safe_prompt
        )
        # Poll until output settles and looks like a prompt
        await self._wait_for_completion(task)

    async def _wait_for_ready(self) -> None:
        """Poll pane until claude's initial prompt appears and settles.

        Auto-accepts the workspace trust dialog if it appears.
        """
        settle = 0
        prev = ""
        while True:
            await asyncio.sleep(_POLL_INTERVAL)
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, self._tmux.capture_pane, self.pane)
            # Auto-accept the workspace trust dialog ("Yes, I trust this folder")
            if "I trust this folder" in text and "Enter to confirm" in text:
                await loop.run_in_executor(
                    None, self._tmux.send_keys, self.pane, ""
                )
                settle = 0
                prev = ""
                continue
            if text == prev:
                settle += 1
            else:
                settle = 0
                prev = text
            if settle >= _SETTLE_CYCLES and _looks_done(text):
                return

    async def _wait_for_completion(self, task: Task) -> None:
        settle = 0
        prev = ""
        while True:
            await asyncio.sleep(_POLL_INTERVAL)
            loop = asyncio.get_running_loop()
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
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._tmux.send_keys, self.pane, notification
        )


def _looks_done(text: str) -> bool:
    return any(p.search(text) for p in _DONE_PATTERNS)
