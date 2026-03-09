"""Tests for ClaudeCodeAgent._dispatch_task() prompt file injection (v1.1.2).

v1.1.2 introduces the UserPromptSubmit hook pattern:
1. _dispatch_task() writes the prompt to __task_prompt__<agent_id>__.txt in cwd
2. Only the short trigger ``__TASK__`` is sent via send_keys (no paste-preview risk)
3. When cwd is None (no worktree), the prompt is sent directly (fallback)

Reference: DESIGN.md §10.38 (v1.1.2 — UserPromptSubmit hook for prompt injection)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tmux_orchestrator.agents.base import Task
from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent
from tmux_orchestrator.bus import Bus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_bus() -> Bus:
    return Bus()


def make_tmux_mock():
    tmux = MagicMock()
    tmux.new_pane = MagicMock(return_value=MagicMock(id="pane-1"))
    tmux.new_subpane = MagicMock(return_value=MagicMock(id="pane-2"))
    tmux.send_keys = MagicMock()
    tmux.watch_pane = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.capture_pane = MagicMock(return_value="❯ ")
    tmux.unwatch_pane = MagicMock()
    return tmux


def make_process_mock():
    process = MagicMock()
    process.send_keys = MagicMock()
    process.get_pane_id = MagicMock(return_value="pane-1")
    return process


# ---------------------------------------------------------------------------
# Tests for _dispatch_task() prompt file injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_task_writes_prompt_file(tmp_path: Path) -> None:
    """_dispatch_task() must write the prompt to __task_prompt__<agent_id>__.txt."""
    bus = make_bus()
    agent = ClaudeCodeAgent(
        agent_id="worker-1",
        bus=bus,
        tmux=make_tmux_mock(),
        web_base_url="",
    )
    agent.pane = MagicMock()
    agent.process = make_process_mock()
    agent._cwd = tmp_path

    completion = MagicMock()
    completion.on_task_dispatch = MagicMock()
    completion.wait = AsyncMock()
    agent._completion = completion

    task = Task(id="t1", prompt="Write a function to sort a list of integers.")
    await agent._dispatch_task(task)

    prompt_file = tmp_path / "__task_prompt__worker-1__.txt"
    assert prompt_file.exists(), "Prompt file must be written to cwd"
    assert prompt_file.read_text(encoding="utf-8") == task.prompt


@pytest.mark.asyncio
async def test_dispatch_task_sends_trigger_not_prompt(tmp_path: Path) -> None:
    """_dispatch_task() must send the short trigger to send_keys, not the full prompt."""
    bus = make_bus()
    agent = ClaudeCodeAgent(
        agent_id="worker-1",
        bus=bus,
        tmux=make_tmux_mock(),
        web_base_url="",
    )
    agent.pane = MagicMock()
    process = make_process_mock()
    agent.process = process
    agent._cwd = tmp_path

    completion = MagicMock()
    completion.on_task_dispatch = MagicMock()
    completion.wait = AsyncMock()
    agent._completion = completion

    long_prompt = "x" * 2000  # would trigger paste-preview if sent directly
    task = Task(id="t1", prompt=long_prompt)
    await agent._dispatch_task(task)

    # Check the FIRST send_keys call — the trigger.
    # v1.1.3 may add a second call (Enter "") if the prompt file isn't consumed within 3s,
    # so we use call_args_list[0] rather than call_args (the last call).
    assert process.send_keys.call_args_list, "send_keys must have been called"
    first_call = process.send_keys.call_args_list[0]
    sent_text = first_call[0][0]  # first positional arg of the first call
    # Must NOT send the full prompt
    assert sent_text != long_prompt, "Full prompt must not be sent via send_keys"
    # Must send the short trigger
    assert sent_text == ClaudeCodeAgent._TASK_TRIGGER, (
        f"Expected trigger {ClaudeCodeAgent._TASK_TRIGGER!r}, got {sent_text!r}"
    )


@pytest.mark.asyncio
async def test_dispatch_task_trigger_is_short(tmp_path: Path) -> None:
    """The task trigger string must be short enough to never trigger paste-preview."""
    # Paste-preview typically triggers for text > ~100 chars or multi-line.
    trigger = ClaudeCodeAgent._TASK_TRIGGER
    assert len(trigger) < 50, f"Trigger must be short (< 50 chars), got {len(trigger)}"
    assert "\n" not in trigger, "Trigger must be single-line"


@pytest.mark.asyncio
async def test_dispatch_task_fallback_when_cwd_is_none() -> None:
    """When _cwd is None, _dispatch_task() sends the sanitized prompt directly (no file)."""
    bus = make_bus()
    agent = ClaudeCodeAgent(
        agent_id="worker-2",
        bus=bus,
        tmux=make_tmux_mock(),
        web_base_url="",
    )
    agent.pane = MagicMock()
    process = make_process_mock()
    agent.process = process
    agent._cwd = None  # no worktree

    completion = MagicMock()
    completion.on_task_dispatch = MagicMock()
    completion.wait = AsyncMock()
    agent._completion = completion

    short_prompt = "hello world"
    task = Task(id="t2", prompt=short_prompt)
    await agent._dispatch_task(task)

    send_keys_call = process.send_keys.call_args
    assert send_keys_call is not None
    sent_text = send_keys_call[0][0]
    # Fallback: prompt is sent directly (sanitized)
    assert sent_text != ClaudeCodeAgent._TASK_TRIGGER, (
        "Fallback path must not send the trigger (no cwd)"
    )
    # The sanitized prompt should be sent
    assert short_prompt in sent_text or sent_text == short_prompt, (
        f"Fallback path must send the prompt, got {sent_text!r}"
    )


@pytest.mark.asyncio
async def test_dispatch_task_calls_completion_wait(tmp_path: Path) -> None:
    """_dispatch_task() must call completion.wait() after sending keys."""
    bus = make_bus()
    agent = ClaudeCodeAgent(
        agent_id="worker-1",
        bus=bus,
        tmux=make_tmux_mock(),
        web_base_url="",
    )
    agent.pane = MagicMock()
    agent.process = make_process_mock()
    agent._cwd = tmp_path

    completion = MagicMock()
    completion.on_task_dispatch = MagicMock()
    wait_called = asyncio.Event()

    async def fake_wait(agent, task):
        wait_called.set()

    completion.wait = fake_wait
    agent._completion = completion

    task = Task(id="t1", prompt="do something")
    await agent._dispatch_task(task)

    assert wait_called.is_set(), "completion.wait() must be called after send_keys"


@pytest.mark.asyncio
async def test_dispatch_task_prompt_file_contains_exact_prompt(tmp_path: Path) -> None:
    """The prompt file must contain the exact prompt (preserving unicode, newlines)."""
    bus = make_bus()
    agent = ClaudeCodeAgent(
        agent_id="agent-a",
        bus=bus,
        tmux=make_tmux_mock(),
        web_base_url="",
    )
    agent.pane = MagicMock()
    agent.process = make_process_mock()
    agent._cwd = tmp_path

    completion = MagicMock()
    completion.on_task_dispatch = MagicMock()
    completion.wait = AsyncMock()
    agent._completion = completion

    unicode_prompt = "タスク:\n1. hello.txt を作成する\n2. 🎉\nDone."
    task = Task(id="t1", prompt=unicode_prompt)
    await agent._dispatch_task(task)

    prompt_file = tmp_path / "__task_prompt__agent-a__.txt"
    assert prompt_file.read_text(encoding="utf-8") == unicode_prompt


@pytest.mark.asyncio
async def test_dispatch_task_uses_agent_id_in_filename(tmp_path: Path) -> None:
    """The prompt file must use the agent's ID to avoid collisions in shared cwd."""
    bus = make_bus()
    agent = ClaudeCodeAgent(
        agent_id="my-special-agent",
        bus=bus,
        tmux=make_tmux_mock(),
        web_base_url="",
    )
    agent.pane = MagicMock()
    agent.process = make_process_mock()
    agent._cwd = tmp_path

    completion = MagicMock()
    completion.on_task_dispatch = MagicMock()
    completion.wait = AsyncMock()
    agent._completion = completion

    task = Task(id="t1", prompt="task prompt")
    await agent._dispatch_task(task)

    expected_file = tmp_path / "__task_prompt__my-special-agent__.txt"
    assert expected_file.exists(), (
        f"Prompt file must be named with agent ID: {expected_file}"
    )


@pytest.mark.asyncio
async def test_dispatch_task_on_task_dispatch_called_before_send_keys(tmp_path: Path) -> None:
    """completion.on_task_dispatch() must be called before send_keys."""
    bus = make_bus()
    agent = ClaudeCodeAgent(
        agent_id="worker-1",
        bus=bus,
        tmux=make_tmux_mock(),
        web_base_url="",
    )
    agent.pane = MagicMock()
    process = make_process_mock()
    agent.process = process
    agent._cwd = tmp_path

    call_order = []

    def on_dispatch(cwd, task_id):
        call_order.append("on_task_dispatch")

    process.send_keys.side_effect = lambda *a, **k: call_order.append("send_keys")

    completion = MagicMock()
    completion.on_task_dispatch = MagicMock(side_effect=on_dispatch)
    completion.wait = AsyncMock()
    agent._completion = completion

    task = Task(id="t1", prompt="do something")
    await agent._dispatch_task(task)

    assert call_order.index("on_task_dispatch") < call_order.index("send_keys"), (
        "on_task_dispatch() must be called before send_keys()"
    )
