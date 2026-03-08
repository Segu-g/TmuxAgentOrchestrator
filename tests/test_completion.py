"""Tests for ExplicitSignalStrategy nudge behaviour (v1.0.1+).

When the agent pane settles at an idle prompt (e.g. '❯') without the agent
having called /task-complete, ExplicitSignalStrategy injects a nudge message
via ``notify_stdin`` to remind the agent to signal completion.

Key invariants tested:
- A nudge IS sent when pane settles idle and task is not complete.
- The nudge is sent at most once per settle period (no spamming).
- After pane becomes active again the nudge flag resets, allowing another nudge.
- Task completion is NEVER auto-triggered — only an explicit _current_task = None
  (via POST /agents/{id}/task-complete) ends the task.

References:
- DESIGN.md §10.latest (v1.0.1 nudge feature)
- GoF Strategy pattern §5.9
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tmux_orchestrator.agents.base import Task
from tmux_orchestrator.agents.completion import (
    ExplicitSignalStrategy,
    _POLL_INTERVAL,
    _SETTLE_CYCLES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_fake_agent(pane_outputs: list[str]) -> MagicMock:
    """Build a fake _AgentLike object whose pane cycles through *pane_outputs*.

    Each call to ``capture_pane`` returns the next item in *pane_outputs*;
    once exhausted, the last value is repeated.
    """
    tmux = MagicMock()
    call_count = [0]

    def capture_pane(pane):  # noqa: ANN001
        idx = min(call_count[0], len(pane_outputs) - 1)
        call_count[0] += 1
        return pane_outputs[idx]

    tmux.capture_pane = MagicMock(side_effect=capture_pane)

    agent = MagicMock()
    agent.id = "test-agent"
    agent.pane = MagicMock()
    agent._tmux = tmux
    agent.handle_output = AsyncMock()
    agent.notify_stdin = AsyncMock()
    return agent


# ---------------------------------------------------------------------------
# Test: nudge is sent when pane settles at idle prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_signal_strategy_nudges_idle_agent() -> None:
    """When pane settles at idle ('❯') and task is not complete, a nudge must be sent."""
    strategy = ExplicitSignalStrategy()

    # Pane is always idle — will settle after _SETTLE_CYCLES polls
    agent = make_fake_agent(["❯ "])
    task = Task(id="task-nudge-test", prompt="do something")
    agent._current_task = task

    # Explicit completion after enough time for nudge to fire
    # We need: _SETTLE_CYCLES * _POLL_INTERVAL for pane to settle + 1 extra poll for nudge
    settle_time = (_SETTLE_CYCLES + 2) * _POLL_INTERVAL

    async def explicit_complete():
        await asyncio.sleep(settle_time + 0.3)
        agent._current_task = None

    asyncio.create_task(explicit_complete())

    await asyncio.wait_for(strategy.wait(agent, task), timeout=10.0)

    # notify_stdin must have been called with the nudge
    agent.notify_stdin.assert_awaited()
    call_args = agent.notify_stdin.call_args_list[0][0][0]
    assert "__ORCHESTRATOR__" in call_args, "Nudge must start with __ORCHESTRATOR__"
    assert "/task-complete" in call_args, "Nudge must mention /task-complete"
    assert task.id[:8] in call_args, "Nudge must include the task_id prefix"

    # handle_output must NOT have been called — task not auto-completed
    agent.handle_output.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test: nudge is not sent twice without activity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_signal_strategy_does_not_nudge_twice_without_activity() -> None:
    """Nudge must not repeat until pane shows activity again."""
    strategy = ExplicitSignalStrategy()

    # Pane is always idle — will settle and nudge once, then stay settled
    agent = make_fake_agent(["❯ "])
    task = Task(id="task-no-double-nudge", prompt="do something")
    agent._current_task = task

    # Let the nudge fire, then wait another full settle period, then complete
    settle_time = (_SETTLE_CYCLES + 2) * _POLL_INTERVAL
    total_wait = settle_time * 2 + 0.3  # two settle periods to verify no second nudge

    async def explicit_complete():
        await asyncio.sleep(total_wait)
        agent._current_task = None

    asyncio.create_task(explicit_complete())

    await asyncio.wait_for(strategy.wait(agent, task), timeout=15.0)

    # notify_stdin must have been called exactly once — no duplicate nudge
    assert agent.notify_stdin.await_count == 1, (
        f"Expected exactly 1 nudge but got {agent.notify_stdin.await_count}"
    )


# ---------------------------------------------------------------------------
# Test: nudge flag resets after pane becomes active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_signal_strategy_resets_nudge_on_activity() -> None:
    """After pane becomes active again, nudge flag resets and can fire again."""
    strategy = ExplicitSignalStrategy()

    # Pane sequence:
    #   - "❯ " repeated for _SETTLE_CYCLES+1 polls → first nudge fires, settle resets
    #   - "working..." → pane becomes active, nudge_sent resets
    #   - "❯ " repeated for _SETTLE_CYCLES+1 polls → second nudge fires
    idle = "❯ "
    active = "working on something..."

    # Build a sequence that triggers: idle→nudge, activity→reset, idle→nudge again
    n = _SETTLE_CYCLES + 1
    outputs = (
        [idle] * n        # first settle → first nudge
        + [active] * 2    # activity → nudge_sent reset
        + [idle] * n      # second settle → second nudge
        + [idle] * 100    # stay idle until explicit completion
    )
    agent = make_fake_agent(outputs)
    task = Task(id="task-reset-nudge", prompt="do something")
    agent._current_task = task

    # Let both nudges fire before explicit completion
    total_time = (n * 2 + 4) * _POLL_INTERVAL + 0.5

    async def explicit_complete():
        await asyncio.sleep(total_time)
        agent._current_task = None

    asyncio.create_task(explicit_complete())

    await asyncio.wait_for(strategy.wait(agent, task), timeout=15.0)

    # notify_stdin must have been called at least twice (nudge reset after activity)
    assert agent.notify_stdin.await_count >= 2, (
        f"Expected at least 2 nudges after activity reset, "
        f"got {agent.notify_stdin.await_count}"
    )

    # Task must still not have been auto-completed
    agent.handle_output.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test: no nudge when pane has not settled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_signal_strategy_no_nudge_while_pane_active() -> None:
    """No nudge must be sent while the pane output is changing."""
    strategy = ExplicitSignalStrategy()

    # Pane keeps changing — never settles
    constantly_changing = [f"output line {i}" for i in range(50)]
    agent = make_fake_agent(constantly_changing)
    task = Task(id="task-active-pane", prompt="do something")
    agent._current_task = task

    # Complete quickly before pane settles
    async def explicit_complete():
        await asyncio.sleep(0.2)
        agent._current_task = None

    asyncio.create_task(explicit_complete())

    await asyncio.wait_for(strategy.wait(agent, task), timeout=5.0)

    # No nudge should have been sent (pane never settled)
    agent.notify_stdin.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test: no nudge when pane looks done but pane content is "pasted text" guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_signal_strategy_no_nudge_for_pasted_text_prompt() -> None:
    """When pane contains '[Pasted text #' guard, looks_done returns False — no nudge."""
    strategy = ExplicitSignalStrategy()

    # Pane output that looks settled but contains the paste guard
    paste_guard_text = "❯ [Pasted text #1 lines]"
    agent = make_fake_agent([paste_guard_text])
    task = Task(id="task-paste-guard", prompt="do something")
    agent._current_task = task

    # Complete quickly
    async def explicit_complete():
        await asyncio.sleep((_SETTLE_CYCLES + 2) * _POLL_INTERVAL)
        agent._current_task = None

    asyncio.create_task(explicit_complete())

    await asyncio.wait_for(strategy.wait(agent, task), timeout=10.0)

    # No nudge should have been sent (pasted text guard prevents looks_done)
    agent.notify_stdin.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test: task only completes via explicit signal, not nudge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_signal_strategy_nudge_does_not_complete_task() -> None:
    """The nudge must not clear _current_task — task completion requires explicit signal."""
    strategy = ExplicitSignalStrategy()

    agent = make_fake_agent(["❯ "])
    task = Task(id="task-no-auto-complete", prompt="do something")
    agent._current_task = task

    settle_time = (_SETTLE_CYCLES + 2) * _POLL_INTERVAL

    # Let nudge fire, then verify task is still open, then complete explicitly
    nudge_observed = asyncio.Event()
    original_notify_stdin = agent.notify_stdin

    async def spy_notify_stdin(notification: str) -> None:
        nudge_observed.set()
        await original_notify_stdin(notification)

    agent.notify_stdin = spy_notify_stdin

    async def explicit_complete():
        # Wait for nudge to fire
        await nudge_observed.wait()
        # Give one more poll cycle so we can check task is still open
        await asyncio.sleep(_POLL_INTERVAL)
        # Verify task is still active (nudge did not auto-complete)
        assert agent._current_task is not None, (
            "Nudge must NOT clear _current_task — task must remain open"
        )
        assert agent._current_task.id == task.id
        # Now complete explicitly
        agent._current_task = None

    asyncio.create_task(explicit_complete())

    await asyncio.wait_for(strategy.wait(agent, task), timeout=10.0)

    # handle_output must NOT have been called
    agent.handle_output.assert_not_awaited()
