"""Tests for completion strategies (v1.0.x).

``ExplicitSignalStrategy`` (DIRECTOR) and ``NudgingStrategy`` (WORKER) both
use a pure spin-wait: they return only when ``_current_task`` is cleared via
an explicit ``POST /agents/{id}/task-complete`` call whose body does **not**
contain the ``stop_hook_active`` key.

Nudging (for WORKER) is triggered by the Stop hook and handled entirely in
the web endpoint — NOT inside the strategy's ``wait()`` method.

References:
- DESIGN.md §10.latest (v1.0.x Stop hook / NudgingStrategy)
- GoF Strategy pattern §5.9
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tmux_orchestrator.agents.base import Task
from tmux_orchestrator.agents.completion import (
    ExplicitSignalStrategy,
    NudgingStrategy,
    _POLL_INTERVAL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_fake_agent() -> MagicMock:
    """Build a minimal fake _AgentLike object."""
    agent = MagicMock()
    agent.id = "test-agent"
    agent.pane = MagicMock()
    agent._tmux = MagicMock()
    agent.handle_output = AsyncMock()
    agent.notify_stdin = AsyncMock()
    return agent


# ---------------------------------------------------------------------------
# ExplicitSignalStrategy — pure spin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_signal_strategy_returns_on_explicit_signal() -> None:
    """ExplicitSignalStrategy.wait() must return when _current_task is cleared."""
    strategy = ExplicitSignalStrategy()
    agent = make_fake_agent()
    task = Task(id="task-explicit", prompt="do something")
    agent._current_task = task

    async def explicit_complete():
        await asyncio.sleep(0.3)
        agent._current_task = None

    asyncio.create_task(explicit_complete())
    await asyncio.wait_for(strategy.wait(agent, task), timeout=5.0)

    # Pure spin — must not poll the pane or inject nudges
    agent._tmux.capture_pane.assert_not_called()
    agent.notify_stdin.assert_not_awaited()
    agent.handle_output.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_signal_strategy_exits_on_task_id_change() -> None:
    """wait() must return when a different task becomes current (task preempted)."""
    strategy = ExplicitSignalStrategy()
    agent = make_fake_agent()
    task = Task(id="task-original", prompt="original")
    agent._current_task = task

    async def switch_task():
        await asyncio.sleep(0.2)
        agent._current_task = Task(id="task-new", prompt="new task")

    asyncio.create_task(switch_task())
    await asyncio.wait_for(strategy.wait(agent, task), timeout=5.0)

    agent._tmux.capture_pane.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_signal_strategy_does_not_self_complete() -> None:
    """wait() must NOT call handle_output — task completion is external only."""
    strategy = ExplicitSignalStrategy()
    agent = make_fake_agent()
    task = Task(id="task-no-self-complete", prompt="do something")
    agent._current_task = task

    async def explicit_complete():
        await asyncio.sleep(0.3)
        agent._current_task = None

    asyncio.create_task(explicit_complete())
    await asyncio.wait_for(strategy.wait(agent, task), timeout=5.0)

    agent.handle_output.assert_not_awaited()


# ---------------------------------------------------------------------------
# NudgingStrategy — pure spin (nudge is in the endpoint, not here)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nudging_strategy_returns_on_explicit_signal() -> None:
    """NudgingStrategy.wait() must return when _current_task is cleared."""
    strategy = NudgingStrategy("agent-1", "http://localhost:8000")
    agent = make_fake_agent()
    task = Task(id="task-nudge", prompt="do something")
    agent._current_task = task

    async def explicit_complete():
        await asyncio.sleep(0.3)
        agent._current_task = None

    asyncio.create_task(explicit_complete())
    await asyncio.wait_for(strategy.wait(agent, task), timeout=5.0)

    # Pure spin — nudge is handled by the web endpoint on Stop hook fire
    agent._tmux.capture_pane.assert_not_called()
    agent.notify_stdin.assert_not_awaited()
    agent.handle_output.assert_not_awaited()


@pytest.mark.asyncio
async def test_nudging_strategy_exits_on_task_id_change() -> None:
    """NudgingStrategy.wait() must return when a different task becomes current."""
    strategy = NudgingStrategy("agent-2", "http://localhost:8000")
    agent = make_fake_agent()
    task = Task(id="task-nudge-original", prompt="original")
    agent._current_task = task

    async def switch_task():
        await asyncio.sleep(0.2)
        agent._current_task = Task(id="task-nudge-new", prompt="new task")

    asyncio.create_task(switch_task())
    await asyncio.wait_for(strategy.wait(agent, task), timeout=5.0)

    agent._tmux.capture_pane.assert_not_called()


@pytest.mark.asyncio
async def test_nudging_strategy_does_not_self_complete() -> None:
    """NudgingStrategy.wait() must NOT call handle_output."""
    strategy = NudgingStrategy("agent-3", "")
    agent = make_fake_agent()
    task = Task(id="task-nudge-no-self", prompt="do something")
    agent._current_task = task

    async def explicit_complete():
        await asyncio.sleep(0.3)
        agent._current_task = None

    asyncio.create_task(explicit_complete())
    await asyncio.wait_for(strategy.wait(agent, task), timeout=5.0)

    agent.handle_output.assert_not_awaited()
    agent.notify_stdin.assert_not_awaited()
