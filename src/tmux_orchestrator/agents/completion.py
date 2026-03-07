"""Completion detection strategies for ClaudeCodeAgent.

Separates *how we know a task is done* from the agent's tmux pane management,
worktree setup, and context engineering.

Two strategies are provided:

- ``StopHookStrategy``   — worker agents: write a Claude Code Stop hook that
  fires an HTTP POST to the orchestrator after each Claude response.  Falls
  back to pane-output polling if the hook doesn't fire.
- ``ExplicitSignalStrategy`` — director agents: skip the Stop hook entirely
  (it fires after every response, causing false positives for multi-turn work)
  and wait for an explicit ``POST /agents/{id}/task-complete`` call instead.

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
    """Completion via Claude Code's Stop hook, with pane-polling as fallback.

    Writes ``.claude/settings.local.json`` containing an HTTP Stop hook URL.
    The hook fires after every Claude response and notifies the orchestrator's
    ``POST /agents/{id}/task-complete`` endpoint.

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

    Used for Director agents that coordinate across multiple Claude responses.
    The Stop hook fires after every response — using it would mark the task
    done after the first director response rather than after all workers finish.

    Directors must call the task-complete endpoint themselves (e.g. via curl)
    once they have confirmed all sub-agents are IDLE and artefacts are committed.
    """

    async def wait(self, agent: _AgentLike, task: "Task") -> None:
        """Spin until ``_current_task`` is cleared by an explicit signal."""
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
    """Return the appropriate ``CompletionStrategy`` for the given agent role."""
    from tmux_orchestrator.config import AgentRole  # local import avoids cycles

    if role == AgentRole.DIRECTOR:
        return ExplicitSignalStrategy()
    return StopHookStrategy(agent_id, web_base_url)
