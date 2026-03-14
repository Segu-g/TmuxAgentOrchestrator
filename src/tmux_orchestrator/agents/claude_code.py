"""Agent that manages a `claude` CLI process inside a tmux pane."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.agents.completion import (
    _POLL_INTERVAL,
    make_completion_strategy,
)
from tmux_orchestrator.bus import Message, MessageType
from tmux_orchestrator.config import AgentRole
from tmux_orchestrator.trust import pre_trust_worktree

from tmux_orchestrator.infrastructure.process_port import (
    ProcessPort,
    TmuxProcessAdapter,
)

if TYPE_CHECKING:
    import libtmux

    from tmux_orchestrator.agents.completion import CompletionStrategy
    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.messaging import Mailbox
    from tmux_orchestrator.tmux_interface import TmuxInterface
    from tmux_orchestrator.worktree import WorktreeManager

logger = logging.getLogger(__name__)


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
        # spec_files: YAML convention files injected into CLAUDE.md (hot-memory).
        # Rules from each file appear in a ## Codified Specs section so agents see
        # them on their very first context load without extra explicit I/O.
        # Reference: DESIGN.md §10.86 (v1.2.10).
        spec_files: list[str] | None = None,
        spec_files_root: Path | None = None,
        # --- Capability tags ---
        tags: list[str] | None = None,
        # --- Worktree lifecycle ---
        merge_on_stop: bool = False,
        merge_target: str | None = None,
        # cleanup_subdir: when True (default) and isolate=False, the
        # .agent/{agent_id}/ subdir is deleted with shutil.rmtree() on stop().
        # Set to False to preserve the subdir for post-mortem inspection.
        # Has no effect when isolate=True (worktree lifecycle handled by WorktreeManager).
        # Reference: DESIGN.md §10.69 (v1.1.37)
        cleanup_subdir: bool = True,
        # keep_branch_on_stop: when True and isolate=True, the worktree filesystem
        # is removed but the git branch is preserved.  Used for branch-chain
        # handoffs where the successor phase needs the committed state.
        # Reference: DESIGN.md §10.82 (v1.2.6)
        keep_branch_on_stop: bool = False,
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
        self._cleanup_subdir = cleanup_subdir
        self._keep_branch_on_stop = keep_branch_on_stop
        self._cwd_override = cwd_override
        self._session_name = session_name
        self._web_base_url = web_base_url
        self._api_key = api_key
        # Normalise role to AgentRole enum for consistent comparisons
        self.role: AgentRole = AgentRole(role) if isinstance(role, str) else role
        # When set, the agent is a sub-agent and shares its parent's tmux window.
        # Still typed as libtmux.Pane because new_subpane() requires the raw pane
        # object; TmuxInterface.new_subpane is an infrastructure operation outside
        # the ProcessPort scope.
        self._parent_pane: "libtmux.Pane | None" = parent_pane
        # ProcessPort interface — set in start(); wraps the raw libtmux.Pane.
        # All send_keys / capture_pane / send_interrupt / pane_id operations on the
        # running claude process go through this port (not self._raw_pane directly).
        # Reference: DESIGN.md §10.34 (v1.0.34).
        self.process: "ProcessPort | None" = None
        # Context engineering: per-agent context localization
        self._system_prompt: str | None = system_prompt
        self._context_files: list[str] = context_files or []
        # Root directory from which context_files paths are resolved.
        # Defaults to None (not set); callers must provide this if context_files is non-empty.
        self._context_files_root: Path | None = context_files_root
        # Cold-memory spec files: glob patterns for specification documents.
        self._context_spec_files: list[str] = context_spec_files or []
        self._context_spec_files_root: Path | None = context_spec_files_root
        # Hot-memory spec files: YAML convention files injected into CLAUDE.md.
        # Reference: Vasilopoulos arXiv:2602.20478 "Codified Context" §3 (2026).
        self._spec_files: list[str] = spec_files or []
        self._spec_files_root: Path | None = spec_files_root
        # Capability tags: advertised capabilities used for smart dispatch.
        self.tags: list[str] = tags or []
        # Working directory set after worktree setup; passed to completion strategy.
        self._cwd: Path | None = None
        # Completion detection strategy: how we know the task is done.
        self._completion: "CompletionStrategy" = make_completion_strategy(
            self.role, agent_id, self._web_base_url
        )
        # Set by _write_startup_hook(); signalled by POST /agents/{id}/ready.
        self._startup_ready: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _agent_work_dir(self, cwd: Path) -> Path:
        """Return the per-agent working directory for file writes and claude launch.

        When ``isolate=True`` the agent already has its own worktree, so cwd
        itself is used unchanged.

        When ``isolate=False`` multiple agents share the same cwd.  To prevent
        file collisions (especially ``.claude/settings.local.json`` for Stop
        hooks), each agent uses its own subdir ``.agent/{agent_id}/`` under
        the shared cwd.  This subdir becomes the cwd that claude is launched
        from, giving each agent an independent Claude Code project directory.

        Reference: DESIGN.md §10.67 (v1.1.35 — per-agent subdir isolation)
        """
        if self._isolate:
            return cwd
        subdir = cwd / ".agent" / self.id
        subdir.mkdir(parents=True, exist_ok=True)
        return subdir

    async def start(self) -> None:
        self._record_start_time()
        loop = asyncio.get_running_loop()
        # Per-agent env vars are injected via libtmux's new_window/split
        # ``environment`` parameter (maps to ``tmux new-window -e KEY=VALUE``).
        # This is pane-local, so concurrent agent starts cannot race.
        # The shared API key is also included here for convenience.
        pane_env: dict[str, str] = {
            "TMUX_ORCHESTRATOR_AGENT_ID": self.id,
            "TMUX_ORCHESTRATOR_WEB_BASE_URL": self._web_base_url,
        }
        if self._api_key:
            pane_env["TMUX_ORCHESTRATOR_API_KEY"] = self._api_key
        if self._parent_pane is not None:
            # Sub-agent: split the parent's window to stay in the same tmux window
            pane = await loop.run_in_executor(
                None, self._tmux.new_subpane, self._parent_pane, self.id, pane_env
            )
        else:
            # Top-level agent: each gets its own tmux window
            pane = await loop.run_in_executor(
                None, self._tmux.new_pane, self.id, pane_env
            )
        # Keep the raw libtmux.Pane reference for watch/unwatch operations (TmuxInterface
        # infrastructure); expose all process interactions via the ProcessPort abstraction.
        # Reference: DESIGN.md §10.34 (v1.0.34 — ProcessPort as canonical interface).
        self.pane = pane
        self.process = TmuxProcessAdapter(pane=pane, tmux=self._tmux)
        cwd = await self._setup_worktree()
        self._cwd = cwd
        if cwd is not None:
            # Determine the effective working directory for this agent.
            # When isolate=False, agents share cwd — each gets its own subdir
            # .agent/{agent_id}/ to avoid file collisions (especially Stop hook
            # settings.local.json).  When isolate=True, cwd is already exclusive.
            # Reference: DESIGN.md §10.67 (v1.1.35)
            agent_dir = await loop.run_in_executor(None, self._agent_work_dir, cwd)
            self._cwd = agent_dir
            await loop.run_in_executor(None, self._write_context_file, agent_dir)
            # Write agent-specific CLAUDE.md:
            # - isolate=True (own worktree): always write
            # - isolate=False (shared cwd): write inside agent_dir (.agent/{id}/)
            #   so the parent cwd CLAUDE.md is never touched
            await loop.run_in_executor(None, self._write_agent_claude_md, agent_dir)
            await loop.run_in_executor(None, self._write_notes_template, agent_dir)
            # Context files are shared project resources — copy to shared cwd,
            # not to the per-agent subdir (so all agents can read them).
            await loop.run_in_executor(None, self._copy_context_files, cwd)
            await loop.run_in_executor(None, self._copy_context_spec_files, cwd)
            # Copy slash commands so agents can use /task-complete etc. without
            # the namespace prefix (plain /task-complete instead of
            # /tmux-orchestrator:task-complete).  Does not overwrite existing files.
            await loop.run_in_executor(None, self._copy_commands, agent_dir)
            await loop.run_in_executor(None, self._completion.on_start, agent_dir)
            # Pre-trust the agent's working directory so Claude Code does not show
            # the interactive "Do you trust the files in this folder?" prompt.
            # That prompt blocks _wait_for_ready() (SessionStart hook) causing
            # a 60-second timeout.  Writing hasTrustDialogAccepted=true to
            # ~/.claude.json before launching claude prevents the dialog.
            # Reference: trust.py module, GitHub Issue #23109, #2147.
            await loop.run_in_executor(None, pre_trust_worktree, agent_dir)
            if self._web_base_url:
                self._startup_ready = asyncio.Event()
                # SessionStart hook is in the agent plugin (loaded via --plugin-dir).
        else:
            agent_dir = None
        plugin_dir = Path(__file__).parent.parent / "agent_plugin"
        command = self._command
        if plugin_dir.is_dir():
            command = f"{command} --plugin-dir {shlex.quote(str(plugin_dir))}"
        launch = (
            f"cd {shlex.quote(str(agent_dir))} && {command}"
            if agent_dir
            else command
        )
        self.process.send_keys(launch)
        self._tmux.watch_pane(pane, self.id)
        self._tmux.start_watcher()
        await self._wait_for_ready()
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(self._run_loop(), name=f"{self.id}-loop")
        await self._start_message_loop()
        logger.info(
            "ClaudeCodeAgent %s started in pane %s (role=%s)",
            self.id,
            self.process.get_pane_id(),
            self.role,
        )

    def _context_extras(self) -> dict[str, Any]:
        # NOTE: api_key is intentionally excluded from the context file.
        # It is delivered exclusively via the TMUX_ORCHESTRATOR_API_KEY
        # environment variable (set via libtmux new-window -e KEY=VALUE).
        # No key file is written to disk.
        return {
            "session_name": self._session_name,
            "web_base_url": self._web_base_url,
        }

    # ------------------------------------------------------------------
    # Context localization — writes agent-specific files to worktree
    # ------------------------------------------------------------------

    def _load_spec_files(self) -> str:
        """Load YAML spec files and format as a ## Codified Specs CLAUDE.md section.

        Each YAML file may contain any combination of:
          - ``name``: section heading (falls back to filename stem)
          - ``description``: one-line summary
          - ``rules``: list of constraint strings — each rendered as a bullet
          - ``examples``: list of short code examples — each rendered as sub-bullet

        Files that do not exist or fail to parse are silently skipped.  The whole
        section is omitted (empty string returned) when ``spec_files`` is empty.

        Root resolution:
          When ``spec_files_root`` is set, relative paths are resolved against it.
          When ``spec_files_root`` is None (no worktree set yet), relative paths
          are resolved against ``Path.cwd()`` at call time.  Absolute paths are
          used as-is in both cases.

        References:
          Vasilopoulos et al. arXiv:2602.20478 §3 "Hot-memory constitution" (2026)
          arXiv:2602.02584 "Constitutional Spec-Driven Development" (2026)
          DESIGN.md §10.86 (v1.2.10)
        """
        if not self._spec_files:
            return ""
        import yaml as _yaml  # noqa: PLC0415 — optional dep, already in pyproject.toml

        lines: list[str] = [
            "\n## Codified Specs\n\n",
            "The following project conventions MUST be followed:\n",
        ]
        root = self._spec_files_root or Path.cwd()
        for spec_path_str in self._spec_files:
            path = Path(spec_path_str)
            if not path.is_absolute():
                path = root / spec_path_str
            if not path.exists():
                logger.debug(
                    "Agent %s: spec_file %r not found at %s — skipping",
                    self.id, spec_path_str, path,
                )
                continue
            try:
                spec = _yaml.safe_load(path.read_text())
            except Exception:  # noqa: BLE001 — tolerate malformed YAML
                logger.warning(
                    "Agent %s: failed to parse spec_file %s — skipping", self.id, path
                )
                continue
            if not isinstance(spec, dict):
                continue
            section_name = spec.get("name") or path.stem
            lines.append(f"\n### {section_name}\n")
            if desc := spec.get("description"):
                lines.append(f"{desc}\n\n")
            rules = spec.get("rules")
            if rules and isinstance(rules, list):
                for rule in rules:
                    lines.append(f"- {rule}\n")
            examples = spec.get("examples")
            if examples and isinstance(examples, list):
                lines.append("\nExamples:\n")
                for ex in examples:
                    lines.append(f"  - `{ex}`\n")
        return "".join(lines)

    def _load_role_rules(self) -> str:
        """Load role-specific rules from ``agent_plugin/rules/{role}.md`` and return as a string.

        Returns the file content under a ``## Role Rules`` section heading, ready to
        embed at the end of CLAUDE.md.  Embedding into CLAUDE.md makes the rules
        auto-compact-resistant: CLAUDE.md is reloaded in full after auto-compact,
        so the rules survive context compaction without an additional file copy step.

        Returns an empty string when:
        - No rules file exists for the agent's role (silently skipped).

        Reference: DESIGN.md §10.95 (v1.2.20 — rules embedded in CLAUDE.md)
        """
        rules_src_dir = Path(__file__).parent.parent / "agent_plugin" / "rules"
        src = rules_src_dir / f"{self.role.value}.md"
        if not src.exists():
            logger.debug(
                "Agent %s: no built-in rules file for role %r at %s — skipping",
                self.id, self.role.value, src,
            )
            return ""
        try:
            rules_content = src.read_text()
        except OSError:
            logger.warning(
                "Agent %s: could not read role rules file %s — skipping", self.id, src
            )
            return ""
        return f"\n## Role Rules\n\n{rules_content}\n"

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

        director_api_block = (
            f"\n### API Quick Reference (Director)\n\n"
            f"```bash\n"
            f"# Check all agents\n"
            f"curl -s {api}/agents -H 'X-Api-Key: $TMUX_ORCHESTRATOR_API_KEY'\n\n"
            f"# Submit task to a specific agent\n"
            f"curl -s -X POST {api}/tasks "
            f"-H 'Content-Type: application/json' "
            f"-H 'X-Api-Key: $TMUX_ORCHESTRATOR_API_KEY' "
            f"-d '{{\"prompt\":\"<task>\",\"target_agent\":\"<id>\",\"priority\":0}}'\n\n"
            f"# Check task queue\n"
            f"curl -s {api}/tasks -H 'X-Api-Key: $TMUX_ORCHESTRATOR_API_KEY'\n"
            f"```\n\n"
            f"Your context is in `__orchestrator_context__.json`. "
            f"Incoming worker completions arrive as mailbox notifications (`__MSG__:<id>`).\n\n"
            f"**Workflow**: use `/plan` → spawn workers → monitor with `/check-inbox` → aggregate results.\n\n"
            f"### Task Completion (IMPORTANT)\n\n"
            f"You are a Director agent: your task spans multiple Claude responses.\n"
            f"The orchestrator does **not** auto-detect your completion — you must\n"
            f"signal it explicitly once ALL workers have finished and all artefacts\n"
            f"are committed:\n\n"
            f"```bash\n"
            f"curl -s -X POST {api}/agents/{self.id}/task-complete \\\n"
            f"  -H 'X-Api-Key: $TMUX_ORCHESTRATOR_API_KEY' \\\n"
            f"  -H 'Content-Type: application/json' \\\n"
            f"  -d '{{\"output\": \"<one-line summary of what was accomplished>\"}}'\n"
            f"```\n\n"
            f"Call this **only once**, after confirming all workers are IDLE\n"
            f"(`GET {api}/agents`) and all expected files exist in the repo."
        )
        role_desc = {
            AgentRole.DIRECTOR: (
                "You are the **Director** agent. Your responsibilities:\n"
                "- Receive goals from the orchestrator queue\n"
                "- Break goals into concrete subtasks\n"
                "- Delegate subtasks to workers via `/spawn-subagent` or `POST /tasks`\n"
                "- Wait for all workers to complete before finalising\n"
                "- Aggregate worker results and commit a summary\n"
                "- Use `/plan` before starting complex coordination work"
                + director_api_block
            ),
            AgentRole.WORKER: (
                "You are a **Worker** agent. Your responsibilities:\n"
                "- Receive tasks from the orchestrator queue or from your parent agent\n"
                "- Implement tasks using TDD: write tests first, then implement\n"
                "- Use `/plan` to break large tasks into steps before starting\n"
                "- Report completion via `/progress` when done\n"
                "- Spawn sub-agents via `/spawn-subagent` if the task is parallelisable\n\n"
                "### Task Completion (IMPORTANT)\n\n"
                "When you have finished ALL work for your current task, signal completion by calling:\n\n"
                "    /task-complete <one-line summary of what was accomplished>\n\n"
                "Call this ONLY ONCE, after all files are committed and tests pass.\n"
                "Do NOT call it mid-task or before your work is complete."
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

        # Context Engineering Strategies section (Part B, v1.1.3)
        # Reference: DESIGN.md §10.39; Zilliz/LangChain 4-strategy framework (2025)
        role_strategy_hint = (
            "**Recommended sequence (Worker)**: Write (NOTES.md/PLAN.md) → "
            "Select (read context_files + NOTES.md at task start) → "
            "Compress (/summarize when context > 60%) → "
            "Isolate (/spawn-subagent for large sub-tasks)"
            if self.role == AgentRole.WORKER
            else "**Recommended sequence (Director)**: Write (PLAN.md + scratchpad) → "
            "Select (read worker results) → "
            "Isolate (spawn sub-agents per phase, each with its own worktree)"
        )
        context_strategies_section = f"""
## Context Engineering Strategies

> Reference: Zilliz "Context Engineering for AI Agents" (2025);
> LangChain "Context Engineering for Agents" (2025);
> Algomatic Tech "AIエージェントを支える技術" (2025)

Use these four strategies to keep your context window focused and avoid context rot:

| Strategy | When to use | How |
|----------|-------------|-----|
| **Write** | Produce output for future reference | Update NOTES.md, write PLAN.md, `PUT /scratchpad/key` |
| **Select** | Pull relevant information in | Read `context_files`, re-read NOTES.md at task start |
| **Compress** | Reduce token count | Call `/summarize` when context > 60% full |
| **Isolate** | Split into independent sub-contexts | `/spawn-subagent` + each sub-agent gets its own worktree |

{role_strategy_hint}

**Signs of context rot**: repeating yourself, forgetting earlier decisions, missing files
you already created. If you notice these, run `/summarize` immediately.
"""

        # Artifact persistence section: shown only when running in an isolated
        # worktree (isolate=True).  Instructs the agent to commit output files
        # to git before calling /task-complete.
        # Design reference: DESIGN.md §10.82 (v1.2.6 — branch artifact persistence)
        artifact_persistence_section = ""
        if self._isolate:
            artifact_persistence_section = f"""
## Artifact Persistence

You are running in an isolated git worktree. Your worktree **filesystem is deleted**
when you stop, but git commits on your branch are preserved.

To ensure your output files survive after you stop, you MUST commit them to git
before calling `/task-complete`:

```bash
git add -A
git commit -m "artifacts: <brief description of what you produced>"
```

**This is required** — any file that is not committed will be lost when your
worktree is removed. The git commit is the only persistent record of your work.

Successor phases in a pipeline can read your committed files via:

```bash
git show worktree/{self.id}:<path/to/file>
```

or by branching from your branch (chain_branch workflow).
"""

        # Codified Specs section: injected when spec_files are configured.
        # spec_files_root is set only when not already specified by the factory.
        # When the factory passes spec_files_root=server_cwd, that root is used
        # (spec files are relative to the project root where config lives).
        # When spec_files_root is None, fall back to the agent's worktree cwd.
        # Reference: DESIGN.md §10.86 (v1.2.10)
        if self._spec_files_root is None and self._spec_files:
            self._spec_files_root = cwd
        codified_specs_section = self._load_spec_files()

        # Role rules: embedded from agent_plugin/rules/{role}.md into CLAUDE.md.
        # Embedding into CLAUDE.md makes rules auto-compact-resistant — CLAUDE.md
        # is reloaded in full after auto-compact, so rules survive context compaction.
        # This replaces the .claude/rules/ copy approach (v1.2.19) which conflicted
        # with shared worktrees and was not reloaded after auto-compact.
        # Reference: DESIGN.md §10.95 (v1.2.20)
        role_rules_section = self._load_role_rules()

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
- Slash commands: available in `.claude/commands/` — use plain `/task-complete` etc.
{context_files_section}{custom_section}{context_strategies_section}{artifact_persistence_section}{codified_specs_section}
## Slash Command Reference

Slash commands are copied into `.claude/commands/` at agent startup so you can
use the plain form without a namespace prefix.

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
| `/task-complete` | `/task-complete <summary>` | Signal task completion to orchestrator |
{role_rules_section}"""
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

    def _copy_commands(self, cwd: Path) -> None:
        """Copy agent_plugin slash-command definitions into ``{cwd}/.claude/commands/``.

        This makes commands available as plain ``/task-complete`` (without the
        ``/tmux-orchestrator:`` namespace prefix that the ``--plugin-dir`` flag
        requires).  Both forms remain functional after the copy — using
        ``--plugin-dir`` for the namespace prefix and the local copy for the plain
        form is non-conflicting.

        Files are copied only if the destination does not already exist, so any
        agent-level customisations are preserved.  If the source ``commands``
        directory does not exist (e.g. development environment without the plugin),
        the method is a no-op rather than raising.

        This is invoked by ``start()`` after worktree setup and before launching
        the ``claude`` process so that the commands are available from the first
        response turn.
        """
        commands_src = Path(__file__).parent.parent / "agent_plugin" / "commands"
        if not commands_src.is_dir():
            logger.debug(
                "Agent %s: agent_plugin/commands not found at %s — skipping command copy",
                self.id, commands_src,
            )
            return
        commands_dst = cwd / ".claude" / "commands"
        commands_dst.mkdir(parents=True, exist_ok=True)
        for cmd_file in sorted(commands_src.glob("*.md")):
            dest = commands_dst / cmd_file.name
            if not dest.exists():
                shutil.copy2(cmd_file, dest)
                logger.debug(
                    "Agent %s: copied slash command %s → %s", self.id, cmd_file.name, dest
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

    # ------------------------------------------------------------------
    # Lifecycle — stop
    # ------------------------------------------------------------------

    def _cleanup_agent_subdir(self) -> None:
        """Delete the per-agent ``.agent/{agent_id}/`` subdir on stop when ``cleanup_subdir=True``.

        This is only meaningful for ``isolate=False`` agents: when multiple agents
        share a cwd, each gets its own ``.agent/{agent_id}/`` directory (created by
        ``_agent_work_dir()``).  That directory is a transient workspace — analogous
        to a ``tempfile.TemporaryDirectory`` — and should be removed when the agent
        stops to avoid accumulating stale artefacts across successive runs.

        No-op conditions (all silently skipped):
        - ``isolate=True``: worktree lifecycle is managed by ``WorktreeManager.teardown()``.
        - ``cleanup_subdir=False``: caller explicitly opted out.
        - ``self._cwd`` is ``None``: agent was never fully started (e.g. start() failed).
        - The subdir does not exist: already removed or never created.

        ``shutil.rmtree(ignore_errors=True)`` is used so that partially-written
        or already-removed directories are silently tolerated (idempotent shutdown).

        Reference: DESIGN.md §10.69 (v1.1.37 — .agent/{id}/ cleanup on stop)
        """
        if self._isolate:
            return  # isolate=True: WorktreeManager handles teardown
        if not self._cleanup_subdir:
            return  # opt-out
        if self._cwd is None:
            return  # never fully started
        # self._cwd is the agent subdir (.agent/{agent_id}/) for isolate=False agents.
        # Confirm this is actually inside a .agent/ directory before deleting.
        if self._cwd.parent.name != ".agent":
            # Unexpected layout; do not delete to avoid data loss.
            logger.warning(
                "Agent %s: _cleanup_agent_subdir: cwd %s is not inside .agent/ — skipping cleanup",
                self.id,
                self._cwd,
            )
            return
        shutil.rmtree(self._cwd, ignore_errors=True)
        logger.info(
            "Agent %s: removed per-agent subdir %s (cleanup_subdir=True)",
            self.id,
            self._cwd,
        )

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()
        if self._msg_task:
            self._msg_task.cancel()
        if self.pane:
            loop = asyncio.get_running_loop()
            # Use process port for send_keys; keep raw pane for unwatch (infrastructure).
            if self.process is not None:
                await loop.run_in_executor(None, self.process.send_keys, "q")
            else:
                await loop.run_in_executor(
                    None, self._tmux.send_keys, self.pane, "q", True
                )
            self._tmux.unwatch_pane(self.pane)
        await self.bus.unsubscribe(self.id)
        # Delegate completion-strategy cleanup (e.g. remove Stop hook settings file).
        # For non-isolated agents the worktree is NOT deleted by _teardown_worktree(),
        # so the strategy must remove any files it wrote to avoid stale hooks.
        # Use self._cwd (the effective agent working directory) rather than
        # self.worktree_path: for isolate=False agents, self._cwd is the
        # .agent/{agent_id}/ subdir where settings.local.json was written.
        cleanup_dir = self._cwd or self.worktree_path
        if cleanup_dir is not None:
            self._completion.on_stop(cleanup_dir)
        await self._teardown_worktree()
        self._cleanup_agent_subdir()
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
        if self.process is not None:
            # Preferred: use ProcessPort.send_interrupt() — no direct libtmux dependency.
            # Reference: DESIGN.md §10.34 (v1.0.34).
            await loop.run_in_executor(None, self.process.send_interrupt)
            pane_label = self.process.get_pane_id()
        else:
            # Fallback: direct libtmux.Pane call for code paths that set self.pane
            # without going through start() (e.g. legacy tests).
            await loop.run_in_executor(None, self.pane.send_keys, "C-c")
            pane_label = getattr(self.pane, "id", str(self.pane))
        logger.info(
            "ClaudeCodeAgent %s: sent C-c interrupt to pane %s",
            self.id,
            pane_label,
        )
        return True

    # ------------------------------------------------------------------
    # Task dispatch
    # ------------------------------------------------------------------

    # Trigger string sent via send_keys when UserPromptSubmit hook is active.
    # Short enough to never trigger paste-preview mode (~10 chars, well under the
    # ~100-char threshold at which tmux starts treating input as a paste).
    _TASK_TRIGGER = "__TASK__"

    async def _dispatch_task(self, task: Task) -> None:
        if self.pane is None or self.process is None:
            raise RuntimeError(f"Agent {self.id} has no pane")
        loop = asyncio.get_running_loop()
        # Notify the completion strategy before sending the prompt so it can
        # update any per-task configuration (e.g. task-scoped stop hook URL).
        if self._cwd is not None:
            await loop.run_in_executor(
                None, self._completion.on_task_dispatch, self._cwd, task.id
            )
        from tmux_orchestrator.security import sanitize_prompt

        if self._cwd is not None:
            # Write the full prompt to a file for the UserPromptSubmit hook to
            # inject as additionalContext.  Only the short trigger is sent via
            # send_keys, avoiding tmux paste-preview entirely.
            # Reference: DESIGN.md §10.38 (v1.1.2 — UserPromptSubmit hook injection)
            prompt_file = self._cwd / f"__task_prompt__{self.id}__.txt"
            # Use lambda to pass encoding as keyword arg (cleaner than positional).
            await loop.run_in_executor(
                None,
                lambda p=prompt_file, t=task.prompt: p.write_text(t, encoding="utf-8"),
            )
            keys_to_send = self._TASK_TRIGGER
        else:
            # Fallback: no cwd (no worktree) → send the sanitized prompt directly.
            # v1.1.1 paste-preview polling in send_keys() handles this path.
            keys_to_send = sanitize_prompt(task.prompt)

        # Use ProcessPort instead of self._tmux.send_keys(self.pane, ...)
        # Reference: DESIGN.md §10.34 (v1.0.34 — ProcessPort canonical interface).
        await loop.run_in_executor(None, self.process.send_keys, keys_to_send)

        # File-existence check: confirm UserPromptSubmit hook fired.
        # The hook deletes the prompt file after reading it, so a missing file means
        # the hook fired and the prompt was delivered.  If the file persists after 3s,
        # a paste-preview dialog is likely blocking input — send Enter to dismiss it.
        # Reference: DESIGN.md §10.39 (v1.1.3 — file-existence paste detection)
        if self._cwd is not None and keys_to_send == self._TASK_TRIGGER:
            await self._wait_for_prompt_file_consumed(prompt_file)

        await self._completion.wait(self, task)

    async def _wait_for_prompt_file_consumed(self, prompt_file: Path) -> None:
        """Poll for prompt file deletion to confirm UserPromptSubmit hook fired.

        After sending the ``__TASK__`` trigger, the ``UserPromptSubmit`` hook reads
        and deletes the prompt file.  Polling for file absence is a race-free way to
        confirm that the hook fired and the task prompt was delivered to Claude.

        If the file is still present after 3 seconds (30 × 100 ms), a paste-preview
        dialog may be blocking the input.  Sending Enter dismisses the dialog and
        re-triggers the hook.

        This replaces the pane-output regex polling used in v1.1.1, which was
        fragile against timing variations and terminal state differences.

        Reference: DESIGN.md §10.39 (v1.1.3)
        """
        for _ in range(30):  # 30 × 100 ms = 3 s
            await asyncio.sleep(0.1)
            if not prompt_file.exists():
                return  # Hook fired, prompt delivered
        # File still exists — paste-preview likely blocking
        logger.debug(
            "Agent %s: prompt file still present after 3 s — sending Enter to dismiss paste-preview",
            self.id,
        )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.process.send_keys, "")
        # Wait again for hook to fire after Enter
        for _ in range(30):
            await asyncio.sleep(0.1)
            if not prompt_file.exists():
                return  # Hook fired after Enter
        logger.warning(
            "Agent %s: prompt file still present after Enter retry — "
            "UserPromptSubmit hook may not have fired",
            self.id,
        )

    async def _wait_for_ready(self) -> None:
        """Wait for claude to signal readiness via the ``SessionStart`` hook.

        ``ClaudeCodeAgent.start()`` writes a ``SessionStart`` hook that calls
        ``POST /agents/{id}/ready`` via ``curl``.  Claude Code fires this hook
        synchronously at session start, before the agentic loop begins.  The
        REST endpoint sets ``_startup_ready`` and this method returns.

        If no web server is configured (``web_base_url`` empty), ``_startup_ready``
        is ``None`` and this method returns immediately.
        """
        if self._startup_ready is None:
            return
        await asyncio.wait_for(self._startup_ready.wait(), timeout=60.0)

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
        if self.pane is None or self.process is None:
            return
        loop = asyncio.get_running_loop()
        # Use ProcessPort instead of self._tmux.send_keys(self.pane, ...).
        # Reference: DESIGN.md §10.34 (v1.0.34 — ProcessPort canonical interface).
        await loop.run_in_executor(None, self.process.send_keys, notification)


