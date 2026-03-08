"""Completion detection strategies for ClaudeCodeAgent.

Separates *how we know a task is done* from the agent's tmux pane management,
worktree setup, and context engineering.

Two strategies are provided:

- ``StopHookStrategy``      — DEPRECATED (internal use / tests only).  Writes a
  Claude Code Stop hook; formerly used as a completion trigger which was
  semantically wrong (fires after every response turn, not on task completion).
- ``NudgingStrategy``       — WORKER strategy.  Writes the Stop hook so the
  endpoint receives a callback when Claude finishes each response turn.  The
  endpoint detects the Stop hook source by the presence of ``stop_hook_active``
  in the request body and sends a nudge via ``notify_stdin`` instead of
  completing the task.  Task completion still requires an explicit
  ``/task-complete`` call.
- ``ExplicitSignalStrategy`` — DIRECTOR strategy.  No Stop hook is written.
  The task ends only when the agent calls ``POST /agents/{id}/task-complete``
  with a body that does **not** contain ``stop_hook_active``.

Usage inside ``ClaudeCodeAgent``::

    self._completion = make_completion_strategy(self.role, agent_id, web_base_url)

    # startup
    self._completion.on_start(cwd)

    # per-task dispatch
    self._completion.on_task_dispatch(cwd, task.id)
    await self._completion.wait(self, task)

    # shutdown
    self._completion.on_stop(cwd)

Reference:
- Strategy pattern: GoF Design Patterns §5.9 (Gamma et al., 1994)
- Claude Code Hooks: https://code.claude.com/docs/en/hooks (2025)
- DESIGN.md §10.12 (v0.38.0), §10.latest (v1.0.8)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tmux_orchestrator.agents.base import Task
    from tmux_orchestrator.config import AgentRole
    from tmux_orchestrator.tmux_interface import TmuxInterface

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt-detection patterns (shared by StopHookStrategy and _wait_for_ready)
# ---------------------------------------------------------------------------

# Patterns that indicate Claude has finished and is waiting for input.
# NOTE: match end-of-line (not whole-line) so trailing terminal padding /
# cursor markers added by newer CLI versions do not prevent detection.
_DONE_PATTERNS = [
    re.compile(r"❯\s*$", re.MULTILINE),           # claude interactive prompt
    re.compile(r"(?<!\S)>\s*$", re.MULTILINE),     # bare ">" (older versions)
    re.compile(r"Human:\s*$", re.MULTILINE),        # Human: conversation prompt
    re.compile(r"(?m)^\$\s*$"),                     # shell prompt (whole line)
]

_POLL_INTERVAL = 0.5   # seconds between output checks
_SETTLE_CYCLES = 3     # consecutive unchanged polls before declaring done


def looks_done(text: str) -> bool:
    """Return True if *text* ends with a recognised idle prompt."""
    # Pasted-text preview — Claude CLI awaits user confirmation; not done yet.
    if "[Pasted text #" in text:
        return False
    return any(p.search(text) for p in _DONE_PATTERNS)


# ---------------------------------------------------------------------------
# Protocol: what strategies need from the agent
# ---------------------------------------------------------------------------


@runtime_checkable
class _AgentLike(Protocol):
    """Narrow interface that CompletionStrategy implementations depend on."""

    id: str
    pane: Any  # libtmux.Pane
    _current_task: "Task | None"
    _tmux: "TmuxInterface"

    async def handle_output(self, text: str) -> None: ...
    async def notify_stdin(self, notification: str) -> None: ...


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class CompletionStrategy(ABC):
    """Abstract policy for detecting when an agent's current task is done.

    Each lifecycle hook has a default no-op implementation so that
    ``ExplicitSignalStrategy`` (and future strategies) only need to override
    the parts they care about.
    """

    def on_start(self, cwd: Path) -> None:
        """Called after worktree setup. Write any config files needed."""

    def on_task_dispatch(self, cwd: Path, task_id: str) -> None:
        """Called just before sending the task prompt to the pane."""

    def on_stop(self, cwd: Path) -> None:
        """Called on agent shutdown. Clean up any config files."""

    @abstractmethod
    async def wait(self, agent: _AgentLike, task: "Task") -> None:
        """Block until the task is done or ``agent._current_task`` changes."""


# ---------------------------------------------------------------------------
# Worker strategy: Stop hook + polling fallback
# ---------------------------------------------------------------------------


class StopHookStrategy(CompletionStrategy):
    """DEPRECATED — internal / test use only.  Not used by ``make_completion_strategy()``.

    Completion via Claude Code's Stop hook, with pane-polling as fallback.

    Writes ``.claude/settings.local.json`` containing an HTTP Stop hook URL.
    The hook fires after every Claude response and notifies the orchestrator's
    ``POST /agents/{id}/task-complete`` endpoint.

    **Why deprecated**: the Stop hook is a *response-level* event ("Claude
    finished one response turn"), not a *task-level* event ("the agent has
    finished all work").  Workers that need multiple response turns (tool calls,
    multi-step implementation, etc.) would be incorrectly marked done after the
    first response.  Use ``ExplicitSignalStrategy`` (via ``/task-complete``)
    instead.

    A ``?task_id=<id>`` query parameter is added at dispatch time so that stop
    hooks fired by *previous* tasks carry a stale URL and are rejected by the
    endpoint — preventing spurious completions when a second task arrives
    before the first hook has fired.

    If the hook does not fire (unreachable server, bad API key, etc.), pane
    output polling is used as a fallback and a warning is emitted.
    """

    def __init__(self, agent_id: str, web_base_url: str) -> None:
        self._agent_id = agent_id
        self._web_base_url = web_base_url

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_settings(self, url: str) -> dict:
        return {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "http",
                                "url": url,
                                "timeout": 5,
                                # $TMUX_ORCHESTRATOR_API_KEY is injected into the
                                # tmux session env before the pane is created.
                                # allowedEnvVars is required for Claude Code to
                                # expand $VAR references in hook headers.
                                "headers": {
                                    "X-Api-Key": "$TMUX_ORCHESTRATOR_API_KEY",
                                },
                                "allowedEnvVars": ["TMUX_ORCHESTRATOR_API_KEY"],
                            }
                        ],
                    }
                ]
            }
        }

    def _write(self, cwd: Path, url: str) -> None:
        claude_dir = cwd / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "settings.local.json").write_text(
            json.dumps(self._build_settings(url), indent=2)
        )

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    def on_start(self, cwd: Path) -> None:
        """Write the initial Stop hook settings (no task_id yet)."""
        if not self._web_base_url:
            return
        url = f"{self._web_base_url}/agents/{self._agent_id}/task-complete"
        self._write(cwd, url)
        logger.debug(
            "Agent %s: wrote Stop hook settings → %s",
            self._agent_id, cwd / ".claude" / "settings.local.json",
        )

    def on_task_dispatch(self, cwd: Path, task_id: str) -> None:
        """Rewrite settings with a task-scoped URL to reject stale hooks."""
        if not self._web_base_url:
            return
        url = (
            f"{self._web_base_url}/agents/{self._agent_id}"
            f"/task-complete?task_id={task_id}"
        )
        self._write(cwd, url)
        logger.debug(
            "Agent %s: updated Stop hook for task %s", self._agent_id, task_id
        )

    def on_stop(self, cwd: Path) -> None:
        """Remove the settings file so stale hooks cannot fire after shutdown."""
        (cwd / ".claude" / "settings.local.json").unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Completion waiting
    # ------------------------------------------------------------------

    async def wait(self, agent: _AgentLike, task: "Task") -> None:
        """Poll pane output until settled + looks done, or hook clears the task."""
        settle = 0
        prev = ""
        while True:
            await asyncio.sleep(_POLL_INTERVAL)
            # Stop hook (or explicit POST /task-complete) may have already
            # called handle_output() and cleared _current_task.
            if agent._current_task is None or agent._current_task.id != task.id:
                return
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(
                None, agent._tmux.capture_pane, agent.pane
            )
            if text == prev:
                settle += 1
            else:
                settle = 0
                prev = text
            if settle >= _SETTLE_CYCLES and looks_done(text):
                logger.warning(
                    "Agent %s: task %s completed via polling fallback — "
                    "Stop hook did not fire (check web_base_url, API key, "
                    "and .claude/settings.local.json in the worktree)",
                    agent.id,
                    task.id,
                )
                await agent.handle_output(text)
                return


# ---------------------------------------------------------------------------
# Director strategy: explicit signal only
# ---------------------------------------------------------------------------


class ExplicitSignalStrategy(CompletionStrategy):
    """Completion only via an explicit ``POST /agents/{id}/task-complete`` call.

    Used for DIRECTOR agents.  Directors must call the ``/task-complete``
    slash command (or the REST endpoint directly) once ALL task work is
    finished and artefacts are committed.

    This is the correct semantic: task completion is a deliberate signal from
    the agent, not an automatic side-effect of a Claude response ending.

    The Stop hook (``StopHookStrategy``) is not used — it fires after every
    response turn, which causes false completions for multi-turn work.
    """

    async def wait(self, agent: _AgentLike, task: "Task") -> None:
        """Spin until ``_current_task`` is cleared by an explicit signal.

        This is a pure spin-wait: no pane polling, no nudge injection.
        The task ends only when ``POST /agents/{id}/task-complete`` is called
        without a ``stop_hook_active`` key in the request body.
        """
        while True:
            await asyncio.sleep(_POLL_INTERVAL)
            if agent._current_task is None or agent._current_task.id != task.id:
                return


# ---------------------------------------------------------------------------
# Worker strategy: Stop hook as nudge trigger + explicit signal for completion
# ---------------------------------------------------------------------------


class NudgingStrategy(CompletionStrategy):
    """Completion via explicit ``/task-complete``; Stop hook used as a nudge trigger.

    Used for WORKER agents.  Writes ``.claude/settings.local.json`` so the
    Stop hook fires when Claude finishes a response turn.  The web endpoint
    (``POST /agents/{id}/task-complete``) detects Stop hook calls by the
    presence of ``stop_hook_active`` in the request body and sends a nudge
    via ``notify_stdin`` instead of completing the task.

    Task completion only happens when the agent explicitly calls
    ``/task-complete`` — i.e., when the endpoint receives a request body that
    does **not** contain the ``stop_hook_active`` key.

    The ``wait()`` method is a pure spin-wait, identical to
    ``ExplicitSignalStrategy``.  All nudge logic lives in the web endpoint.
    """

    def __init__(self, agent_id: str, web_base_url: str) -> None:
        self._hook = StopHookStrategy(agent_id, web_base_url)

    def on_start(self, cwd: Path) -> None:
        self._hook.on_start(cwd)

    def on_task_dispatch(self, cwd: Path, task_id: str) -> None:
        self._hook.on_task_dispatch(cwd, task_id)

    def on_stop(self, cwd: Path) -> None:
        self._hook.on_stop(cwd)

    async def wait(self, agent: _AgentLike, task: "Task") -> None:
        """Spin until ``_current_task`` is cleared by an explicit signal.

        The Stop hook fires when Claude finishes a response; this is handled
        by the web endpoint which sends a nudge.  This method simply waits for
        the explicit ``/task-complete`` signal.
        """
        while True:
            await asyncio.sleep(_POLL_INTERVAL)
            if agent._current_task is None or agent._current_task.id != task.id:
                return


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_completion_strategy(
    role: "AgentRole",
    agent_id: str,
    web_base_url: str,
) -> CompletionStrategy:
    """Return the appropriate ``CompletionStrategy`` for the given agent role.

    - **WORKER** → ``NudgingStrategy``: writes the Stop hook so the endpoint
      can nudge the agent when Claude goes idle without calling ``/task-complete``.
    - **DIRECTOR** → ``ExplicitSignalStrategy``: no Stop hook; completion is
      purely via the explicit ``/task-complete`` slash command.

    In both cases the Stop hook is **never** used to complete a task — it only
    triggers a nudge (for workers).  Task completion always requires an
    explicit ``POST /agents/{id}/task-complete`` call whose body does not
    contain the ``stop_hook_active`` key.
    """
    from tmux_orchestrator.config import AgentRole

    if role == AgentRole.WORKER:
        return NudgingStrategy(agent_id, web_base_url)
    return ExplicitSignalStrategy()
