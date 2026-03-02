"""Tests for agent-level behaviour (context file, timeout, etc.)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tmux_orchestrator.agents.custom import CustomAgent
from tmux_orchestrator.bus import Bus


def make_tmux_mock() -> MagicMock:
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    return tmux


# ---------------------------------------------------------------------------
# CustomAgent context file
# ---------------------------------------------------------------------------


async def test_custom_agent_writes_context_file(tmp_path: Path) -> None:
    """CustomAgent writes __orchestrator_context__.json when given a cwd_override."""
    bus = Bus()
    tmux = make_tmux_mock()

    # Use a simple echo script that acts as a valid custom agent
    agent = CustomAgent(
        agent_id="ctx-test",
        bus=bus,
        tmux=tmux,
        command="cat",  # reads stdin, echoes nothing — we just want startup behaviour
        cwd_override=tmp_path,
    )

    await agent.start()
    try:
        ctx_file = tmp_path / "__orchestrator_context__.json"
        assert ctx_file.exists(), "Context file was not written"
        ctx = json.loads(ctx_file.read_text())
        assert ctx["agent_id"] == "ctx-test"
        assert ctx["worktree_path"] == str(tmp_path)
    finally:
        await agent.stop()
