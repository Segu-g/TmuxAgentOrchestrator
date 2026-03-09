"""Tests for ClaudeCodeAgent._wait_for_prompt_file_consumed() (v1.1.3).

v1.1.3 adds file-existence polling after sending the __TASK__ trigger:
- If the prompt file disappears within 3s → UserPromptSubmit hook fired → success
- If the file persists 3s → paste-preview may be blocking → send Enter → retry
- If the file persists after Enter retry → log warning and continue

Reference: DESIGN.md §10.39 (v1.1.3 — file-existence paste detection)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

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


def make_agent(tmp_path: Path) -> tuple[ClaudeCodeAgent, MagicMock]:
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
    return agent, process


# ---------------------------------------------------------------------------
# Tests for _wait_for_prompt_file_consumed()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_deleted_immediately_no_enter_sent(tmp_path: Path) -> None:
    """If the prompt file is deleted before the first poll, no Enter is sent."""
    agent, process = make_agent(tmp_path)

    # Create a prompt file and schedule its deletion after a tiny delay
    prompt_file = tmp_path / "__task_prompt__worker-1__.txt"
    prompt_file.write_text("task", encoding="utf-8")

    async def delete_file_soon():
        await asyncio.sleep(0.05)  # delete before first poll completes
        prompt_file.unlink(missing_ok=True)

    asyncio.create_task(delete_file_soon())
    await agent._wait_for_prompt_file_consumed(prompt_file)

    # send_keys should NOT have been called with Enter ("") by the method itself
    # (the process mock starts with no calls recorded here, since we call the
    # method directly, not via _dispatch_task)
    enter_calls = [c for c in process.send_keys.call_args_list if c == call("")]
    assert len(enter_calls) == 0, "No Enter should be sent when file deleted quickly"


@pytest.mark.asyncio
async def test_file_persists_3s_enter_sent(tmp_path: Path) -> None:
    """If the prompt file persists beyond 3s, Enter is sent once."""
    agent, process = make_agent(tmp_path)

    prompt_file = tmp_path / "__task_prompt__worker-1__.txt"
    prompt_file.write_text("task", encoding="utf-8")

    # Speed up: patch asyncio.sleep to be instant so the 30-iteration loop finishes fast
    # but the file is never deleted (simulating paste-preview blocking)
    delete_calls: list[str] = []

    original_send_keys = process.send_keys

    def mock_send_keys(text: str) -> None:
        delete_calls.append(text)
        # After Enter is "sent", delete the file to simulate hook firing
        if text == "":
            prompt_file.unlink(missing_ok=True)

    process.send_keys.side_effect = mock_send_keys

    # Patch asyncio.sleep to be nearly instant
    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        await agent._wait_for_prompt_file_consumed(prompt_file)

    # Enter ("") must have been sent exactly once
    assert "" in delete_calls, "Enter must be sent after 3s file-persistence timeout"
    assert delete_calls.count("") == 1, "Enter must be sent exactly once"


@pytest.mark.asyncio
async def test_file_deleted_after_enter_no_warning(tmp_path: Path, caplog) -> None:
    """If file deleted in second poll loop (after Enter), no warning is emitted."""
    import logging

    agent, process = make_agent(tmp_path)

    prompt_file = tmp_path / "__task_prompt__worker-1__.txt"
    prompt_file.write_text("task", encoding="utf-8")

    def mock_send_keys(text: str) -> None:
        if text == "":
            prompt_file.unlink(missing_ok=True)

    process.send_keys.side_effect = mock_send_keys

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        with caplog.at_level(logging.WARNING, logger="tmux_orchestrator.agents.claude_code"):
            await agent._wait_for_prompt_file_consumed(prompt_file)

    # No warning should be logged
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warning_records) == 0, (
        f"No warning expected when file deleted after Enter; got: {warning_records}"
    )


@pytest.mark.asyncio
async def test_file_persists_both_loops_warning_logged(tmp_path: Path, caplog) -> None:
    """If file persists through both poll loops, a warning is logged."""
    import logging

    agent, process = make_agent(tmp_path)

    prompt_file = tmp_path / "__task_prompt__worker-1__.txt"
    prompt_file.write_text("task", encoding="utf-8")
    # Never delete the file — both loops time out

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        with caplog.at_level(logging.WARNING, logger="tmux_orchestrator.agents.claude_code"):
            await agent._wait_for_prompt_file_consumed(prompt_file)

    warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("hook may not have fired" in msg or "prompt file still present" in msg
               for msg in warning_messages), (
        f"Expected hook-not-fired warning; got: {warning_messages}"
    )


@pytest.mark.asyncio
async def test_dispatch_task_calls_wait_for_prompt_file_consumed(tmp_path: Path) -> None:
    """_dispatch_task() must call _wait_for_prompt_file_consumed() when cwd is set."""
    agent, process = make_agent(tmp_path)

    wait_called_with: list[Path] = []

    async def fake_wait(prompt_file: Path) -> None:
        wait_called_with.append(prompt_file)

    agent._wait_for_prompt_file_consumed = fake_wait  # type: ignore[method-assign]

    task = Task(id="t1", prompt="do something long")
    await agent._dispatch_task(task)

    assert len(wait_called_with) == 1, "_wait_for_prompt_file_consumed must be called once"
    expected_file = tmp_path / "__task_prompt__worker-1__.txt"
    assert wait_called_with[0] == expected_file


@pytest.mark.asyncio
async def test_dispatch_task_no_wait_when_cwd_none() -> None:
    """_dispatch_task() must NOT call _wait_for_prompt_file_consumed() when cwd is None."""
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
    agent._cwd = None

    wait_called = False

    async def fake_wait(prompt_file: Path) -> None:
        nonlocal wait_called
        wait_called = True

    agent._wait_for_prompt_file_consumed = fake_wait  # type: ignore[method-assign]

    completion = MagicMock()
    completion.on_task_dispatch = MagicMock()
    completion.wait = AsyncMock()
    agent._completion = completion

    task = Task(id="t2", prompt="short")
    await agent._dispatch_task(task)

    assert not wait_called, "_wait_for_prompt_file_consumed must not be called when cwd is None"
