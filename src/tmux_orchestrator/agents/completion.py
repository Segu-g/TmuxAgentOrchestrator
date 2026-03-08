"""Completion detection strategies for ClaudeCodeAgent.

Separates *how we know a task is done* from the agent's tmux pane management,
worktree setup, and context engineering.

Two strategies are provided:

- ``NudgingStrategy``        — WORKER strategy.  Writes the Stop hook so the
  endpoint receives a callback when Claude finishes each response turn.  The
  endpoint detects the Stop hook source by the presence of ``stop_hook_active``
  in the request body and sends a nudge via ``notify_stdin`` instead of
  completing the task.  Task completion still requires an explicit
  ``/task-complete`` call.
- ``ExplicitSignalStrategy`` — DIRECTOR strategy.  No Stop hook is written.
  The task ends only when the agent calls ``POST /agents/{id}/task-complete``
  with a body that does **not** contain ``stop_hook_active``.

Startup detection (both roles) is handled separately via a ``SessionStart``
hook written by ``ClaudeCodeAgent.start()`` itself, pointing at
``POST /agents/{id}/ready``.  This is independent of task-completion strategy.

Usage inside ``ClaudeCodeAgent``::

    self._completion = make_completion_strategy(self.role, agent_id, web_base_url)

    # startup (SessionStart hook written by ClaudeCodeAgent.start())
    self._completion.on_start(cwd)   # no-op for both strategies

    # per-task dispatch
    self._completion.on_task_dispatch(cwd, task.id)
    await self._completion.wait(self, task)

    # shutdown
    self._completion.on_stop(cwd)

Reference:
- Strategy pattern: GoF Design Patterns §5.9 (Gamma et al., 1994)
- Claude Code Hooks: https://code.claude.com/docs/en/hooks (2025)
- DESIGN.md §10.latest (v1.0.x)
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tmux_orchestrator.agents.base import Task
    from tmux_orchestrator.config import AgentRole
    from tmux_orchestrator.tmux_interface import TmuxInterface

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 0.5   # seconds between spin-wait ticks


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

    Each lifecycle hook has a default no-op implementation so that concrete
    strategies only need to override the parts they care about.
    """

    def on_start(self, cwd: Path) -> None:
        """Called after worktree setup. Write any task-level config files needed."""

    def on_task_dispatch(self, cwd: Path, task_id: str) -> None:
        """Called just before sending the task prompt to the pane."""

    def on_stop(self, cwd: Path) -> None:
        """Called on agent shutdown. Clean up any config files."""

    @abstractmethod
    async def wait(self, agent: _AgentLike, task: "Task") -> None:
        """Block until the task is done or ``agent._current_task`` changes."""


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

    No Stop hook is written — there is no nudge mechanism for Directors.
    """

    async def wait(self, agent: _AgentLike, task: "Task") -> None:
        """Spin until ``_current_task`` is cleared by an explicit signal.

        Pure spin-wait: no pane polling, no nudge injection.
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


def _build_stop_hook_settings(url: str) -> dict:
    """Return the .claude/settings.local.json dict for a Stop hook pointing at *url*."""
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
                            # $TMUX_ORCHESTRATOR_API_KEY is injected into the tmux
                            # session env before the pane is created.
                            # allowedEnvVars is required for Claude Code to expand
                            # $VAR references in hook headers.
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


def _write_settings(cwd: Path, settings: dict) -> None:
    """Write *settings* to ``{cwd}/.claude/settings.local.json``."""
    claude_dir = cwd / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.local.json").write_text(json.dumps(settings, indent=2))


class NudgingStrategy(CompletionStrategy):
    """Completion via explicit ``/task-complete``; Stop hook used as a nudge trigger.

    Used for WORKER agents.  On task dispatch, writes
    ``.claude/settings.local.json`` so the Stop hook fires when Claude
    finishes a response turn.  The web endpoint
    (``POST /agents/{id}/task-complete``) detects Stop hook calls by the
    presence of ``stop_hook_active`` in the request body and sends a nudge
    via ``notify_stdin`` instead of completing the task.

    Task completion only happens when the agent explicitly calls
    ``/task-complete`` — i.e., when the endpoint receives a request body that
    does **not** contain the ``stop_hook_active`` key.

    Note: startup detection is handled separately via the ``SessionStart``
    hook written by ``ClaudeCodeAgent._write_startup_hook()``, not here.
    ``on_start()`` is therefore a no-op.

    The ``wait()`` method is a pure spin-wait, identical to
    ``ExplicitSignalStrategy``.  All nudge logic lives in the web endpoint.
    """

    def __init__(self, agent_id: str, web_base_url: str) -> None:
        self._agent_id = agent_id
        self._web_base_url = web_base_url

    def on_task_dispatch(self, cwd: Path, task_id: str) -> None:
        """Write Stop hook settings with a task-scoped URL.

        The ``?task_id=<id>`` parameter ensures stale hooks from a previous
        task are rejected by the endpoint.
        """
        if not self._web_base_url:
            return
        url = (
            f"{self._web_base_url}/agents/{self._agent_id}"
            f"/task-complete?task_id={task_id}"
        )
        _write_settings(cwd, _build_stop_hook_settings(url))
        logger.debug(
            "Agent %s: wrote Stop hook for task %s → %s",
            self._agent_id, task_id, cwd / ".claude" / "settings.local.json",
        )

    def on_stop(self, cwd: Path) -> None:
        """Remove the settings file so stale Stop hooks cannot fire after shutdown."""
        (cwd / ".claude" / "settings.local.json").unlink(missing_ok=True)

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

    - **WORKER** → ``NudgingStrategy``: writes the Stop hook on each task
      dispatch so the endpoint can nudge the agent when Claude goes idle
      without calling ``/task-complete``.
    - **DIRECTOR** → ``ExplicitSignalStrategy``: no Stop hook; completion is
      purely via the explicit ``/task-complete`` slash command.

    In both cases the Stop hook is **never** used to complete a task — it only
    triggers a nudge (for workers).  Task completion always requires an
    explicit ``POST /agents/{id}/task-complete`` call whose body does not
    contain the ``stop_hook_active`` key.

    Startup detection for all roles is handled by ``ClaudeCodeAgent`` via the
    ``SessionStart`` hook and ``POST /agents/{id}/ready`` endpoint.
    """
    from tmux_orchestrator.config import AgentRole

    if role == AgentRole.WORKER:
        return NudgingStrategy(agent_id, web_base_url)
    return ExplicitSignalStrategy()
