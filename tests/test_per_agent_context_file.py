"""Tests for per-agent context file naming (v1.0.19).

When multiple agents share the same cwd (isolate: false), the context file
__orchestrator_context__.json was previously overwritten by each agent at
startup, causing the first agent to call /task-complete with the wrong
agent_id → 409 error.

Fix: write __orchestrator_context__{agent_id}__.json as the primary file.
Also write __orchestrator_context__.json for backward compatibility.

Design reference: DESIGN.md §10.N (v1.0.19 — isolate:false context file race fix)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tmux_orchestrator.agents.base import Agent, AgentStatus
from tmux_orchestrator.bus import Bus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_bus() -> Bus:
    return Bus()


class MinimalAgent(Agent):
    """Minimal concrete Agent for testing _write_context_file."""

    def _context_extras(self):
        return {"session_name": "test-session", "web_base_url": "http://localhost:8000"}

    async def start(self): ...
    async def stop(self): ...
    async def _dispatch_task(self, task): ...
    async def handle_output(self, text): ...
    async def notify_stdin(self, notification: str): ...


# ---------------------------------------------------------------------------
# Tests for _write_context_file
# ---------------------------------------------------------------------------


def test_write_context_file_creates_per_agent_file(tmp_path: Path) -> None:
    """_write_context_file must create __orchestrator_context__{agent_id}__.json."""
    bus = make_bus()
    agent = MinimalAgent("worker-1", bus)
    agent._write_context_file(tmp_path)

    per_agent = tmp_path / "__orchestrator_context__worker-1__.json"
    assert per_agent.exists(), "Per-agent context file must be created"

    ctx = json.loads(per_agent.read_text())
    assert ctx["agent_id"] == "worker-1"
    assert ctx["session_name"] == "test-session"
    assert ctx["web_base_url"] == "http://localhost:8000"
    assert ctx["worktree_path"] == str(tmp_path)


def test_write_context_file_creates_legacy_file(tmp_path: Path) -> None:
    """_write_context_file must also create __orchestrator_context__.json for backward compat."""
    bus = make_bus()
    agent = MinimalAgent("worker-1", bus)
    agent._write_context_file(tmp_path)

    legacy = tmp_path / "__orchestrator_context__.json"
    assert legacy.exists(), "Legacy context file must be created for backward compat"

    ctx = json.loads(legacy.read_text())
    assert ctx["agent_id"] == "worker-1"


def test_write_context_file_both_files_identical(tmp_path: Path) -> None:
    """Per-agent and legacy files must have identical content."""
    bus = make_bus()
    agent = MinimalAgent("worker-1", bus)
    agent._write_context_file(tmp_path)

    per_agent = tmp_path / "__orchestrator_context__worker-1__.json"
    legacy = tmp_path / "__orchestrator_context__.json"

    assert per_agent.read_text() == legacy.read_text()


def test_multiple_agents_independent_context_files(tmp_path: Path) -> None:
    """Multiple agents in the same cwd must have independent per-agent context files.

    This tests the fix for the isolate:false race condition: agent-a and agent-b
    both write to the same cwd, but each gets its own per-agent file.
    """
    bus = make_bus()
    agent_a = MinimalAgent("worker-a", bus)
    agent_b = MinimalAgent("worker-b", bus)

    agent_a._write_context_file(tmp_path)
    agent_b._write_context_file(tmp_path)

    # Per-agent files are independent
    file_a = tmp_path / "__orchestrator_context__worker-a__.json"
    file_b = tmp_path / "__orchestrator_context__worker-b__.json"

    assert file_a.exists()
    assert file_b.exists()

    ctx_a = json.loads(file_a.read_text())
    ctx_b = json.loads(file_b.read_text())

    assert ctx_a["agent_id"] == "worker-a"
    assert ctx_b["agent_id"] == "worker-b"


def test_multiple_agents_legacy_file_holds_last_writer(tmp_path: Path) -> None:
    """The legacy file is overwritten each time — per-agent files are the source of truth."""
    bus = make_bus()
    agent_a = MinimalAgent("worker-a", bus)
    agent_b = MinimalAgent("worker-b", bus)

    agent_a._write_context_file(tmp_path)
    agent_b._write_context_file(tmp_path)

    legacy = tmp_path / "__orchestrator_context__.json"
    ctx = json.loads(legacy.read_text())
    # Legacy file contains agent-b (last writer) — per-agent files are authoritative
    assert ctx["agent_id"] == "worker-b"


def test_per_agent_file_not_overwritten_by_sibling(tmp_path: Path) -> None:
    """Agent-a's per-agent file must not be touched when agent-b starts."""
    bus = make_bus()
    agent_a = MinimalAgent("worker-a", bus)
    agent_b = MinimalAgent("worker-b", bus)

    agent_a._write_context_file(tmp_path)
    file_a = tmp_path / "__orchestrator_context__worker-a__.json"
    mtime_before = file_a.stat().st_mtime

    agent_b._write_context_file(tmp_path)
    mtime_after = file_a.stat().st_mtime

    assert mtime_before == mtime_after, (
        "Agent-a's per-agent file must not be modified when agent-b starts"
    )


def test_write_context_file_with_mailbox(tmp_path: Path) -> None:
    """mailbox_dir is derived from the mailbox object when set."""
    bus = make_bus()
    agent = MinimalAgent("worker-1", bus)

    mailbox = MagicMock()
    mailbox._root = tmp_path / "session-name"
    mailbox._root.mkdir()
    agent.mailbox = mailbox

    agent._write_context_file(tmp_path)

    per_agent = tmp_path / "__orchestrator_context__worker-1__.json"
    ctx = json.loads(per_agent.read_text())
    assert ctx["mailbox_dir"] == str(tmp_path)


# ---------------------------------------------------------------------------
# Tests for API key delivery (env-var only, v1.2.18+)
# ---------------------------------------------------------------------------


def test_no_api_key_file_written_to_disk(tmp_path: Path) -> None:
    """API key must NOT be written to any file (env-var only, v1.2.18+)."""
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    bus = make_bus()
    tmux = MagicMock()
    agent = ClaudeCodeAgent("worker-1", bus, tmux, api_key="test-key-123")

    assert not hasattr(agent, "_write_api_key_file"), (
        "_write_api_key_file must not exist — API key is env-var only"
    )
    # No key files must appear after context file write
    agent._write_context_file(tmp_path)
    key_files = list(tmp_path.glob("__orchestrator_api_key__*"))
    assert key_files == [], f"No key files must be written: {key_files}"
