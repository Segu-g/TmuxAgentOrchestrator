"""Tests for the /deliberate slash command (v1.0.32).

Verifies that the deliberate.md command file is present, well-formed, and implements
the Devil's Advocate debate pattern (DEBATE, ACL 2024; CONSENSAGENT, ACL 2025).
"""

from __future__ import annotations

import json
import os
import re
import sys
import textwrap
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _commands_dir() -> Path:
    return (
        Path(__file__).parent.parent
        / "src"
        / "tmux_orchestrator"
        / "agent_plugin"
        / "commands"
    )


def _deliberate_file() -> Path:
    return _commands_dir() / "deliberate.md"


def _read_deliberate() -> str:
    return _deliberate_file().read_text()


def _extract_python_snippet(md_content: str) -> str:
    """Extract the first Python code block from a Markdown file."""
    match = re.search(r"```python\n(.*?)```", md_content, re.DOTALL)
    assert match, "No ```python code block found in deliberate.md"
    return match.group(1)


# ---------------------------------------------------------------------------
# Structural tests
# ---------------------------------------------------------------------------


def test_deliberate_file_exists() -> None:
    """deliberate.md must exist in the plugin commands directory."""
    assert _deliberate_file().exists(), f"deliberate.md missing: {_deliberate_file()}"


def test_deliberate_file_nonempty() -> None:
    """deliberate.md must have non-empty content."""
    content = _deliberate_file().read_text().strip()
    assert content, "deliberate.md is empty"


def test_deliberate_has_python_snippet() -> None:
    """deliberate.md must contain a ```python code block."""
    content = _read_deliberate()
    assert "```python" in content, "deliberate.md has no Python code block"


def test_deliberate_has_usage_line() -> None:
    """deliberate.md must describe its usage syntax."""
    content = _read_deliberate()
    assert "/deliberate" in content, "deliberate.md must mention the /deliberate command"
    assert "<question" in content.lower() or "$ARGUMENTS" in content, (
        "deliberate.md must show question/arguments parameter"
    )


def test_deliberate_references_advocate_role() -> None:
    """deliberate.md must reference the advocate (pro) role."""
    content = _read_deliberate()
    assert "advocate" in content.lower(), "deliberate.md must mention advocate role"


def test_deliberate_references_critic_role() -> None:
    """deliberate.md must reference the critic (con) role."""
    content = _read_deliberate()
    assert "critic" in content.lower(), "deliberate.md must mention critic role"


def test_deliberate_references_deliberation_md() -> None:
    """deliberate.md must reference DELIBERATION.md as the output artifact."""
    content = _read_deliberate()
    assert "DELIBERATION.md" in content, (
        "deliberate.md must reference the DELIBERATION.md output file"
    )


def test_deliberate_references_spawn() -> None:
    """The command must spawn sub-agents (reference POST /agents or spawn)."""
    content = _read_deliberate()
    assert "spawn" in content.lower() or "subagent_spawned" in content, (
        "deliberate.md must reference sub-agent spawning"
    )


def test_deliberate_references_p2p_messaging() -> None:
    """The command must send messages to sub-agents via P2P."""
    content = _read_deliberate()
    assert "/message" in content or "send_msg" in content or "PEER_MSG" in content, (
        "deliberate.md must send messages to sub-agents"
    )


def test_deliberate_references_research() -> None:
    """deliberate.md must cite the DEBATE framework research."""
    content = _read_deliberate()
    # Should reference at least one of: ACL 2024, DEBATE, arXiv:2405.09935, Devil's Advocate
    patterns = ["ACL 2024", "DEBATE", "2405.09935", "Devil's Advocate", "devil's advocate"]
    assert any(p in content for p in patterns), (
        "deliberate.md must reference the DEBATE/Devil's Advocate research basis"
    )


def test_deliberate_snippet_reads_context_file() -> None:
    """The Python snippet must read __orchestrator_context__.json."""
    snippet = _extract_python_snippet(_read_deliberate())
    assert "__orchestrator_context__" in snippet, (
        "Python snippet must read orchestrator context file"
    )


def test_deliberate_snippet_reads_api_key_from_env() -> None:
    """The Python snippet must read TMUX_ORCHESTRATOR_API_KEY from environment."""
    snippet = _extract_python_snippet(_read_deliberate())
    assert "TMUX_ORCHESTRATOR_API_KEY" in snippet, (
        "Python snippet must read API key from TMUX_ORCHESTRATOR_API_KEY env var"
    )


def test_deliberate_snippet_sets_x_api_key_header() -> None:
    """The Python snippet must include X-API-Key in HTTP headers."""
    snippet = _extract_python_snippet(_read_deliberate())
    assert "X-API-Key" in snippet, (
        "Python snippet must set X-API-Key header for authenticated requests"
    )


def test_deliberate_snippet_handles_no_question() -> None:
    """The Python snippet must exit with error when no question is provided."""
    snippet = _extract_python_snippet(_read_deliberate())
    # Question comes from $ARGUMENTS; if empty, should exit
    assert "SystemExit" in snippet or "raise SystemExit" in snippet, (
        "Python snippet must call SystemExit when no question is given"
    )


def test_deliberate_snippet_polls_inbox() -> None:
    """The Python snippet must poll the inbox for spawn confirmations."""
    snippet = _extract_python_snippet(_read_deliberate())
    assert "subagent_spawned" in snippet, (
        "Python snippet must poll inbox for subagent_spawned events"
    )


def test_deliberate_snippet_spawns_two_agents() -> None:
    """The Python snippet must call spawn twice (advocate + critic)."""
    snippet = _extract_python_snippet(_read_deliberate())
    # spawn_subagent is called twice in the snippet
    call_count = snippet.count("spawn_subagent(")
    assert call_count >= 2, (
        f"Python snippet must spawn both advocate and critic sub-agents (found {call_count} calls)"
    )


def test_deliberate_snippet_sends_two_messages() -> None:
    """The Python snippet must send messages to both advocate and critic."""
    snippet = _extract_python_snippet(_read_deliberate())
    # send_msg is called twice (once per sub-agent)
    call_count = snippet.count("send_msg(")
    assert call_count >= 2, (
        f"Python snippet must brief both advocate and critic (found {call_count} send_msg calls)"
    )


def test_deliberate_snippet_uses_parent_id() -> None:
    """The Python snippet must pass parent_id when spawning sub-agents."""
    snippet = _extract_python_snippet(_read_deliberate())
    assert "parent_id" in snippet, (
        "Python snippet must pass parent_id so P2P is auto-granted to sub-agents"
    )


# ---------------------------------------------------------------------------
# Integration: extract and execute snippet against a mock HTTP server
# ---------------------------------------------------------------------------


class _MockOrchestratorHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that simulates POST /agents and POST /agents/{id}/message."""

    received_requests: list[dict] = []
    spawn_count: int = 0

    def log_message(self, *args: object) -> None:  # suppress output
        pass

    def do_GET(self) -> None:  # GET /agents — return template list
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        agents = [{"id": "worker-1", "status": "IDLE"}, {"id": "worker-2", "status": "IDLE"}]
        self.wfile.write(json.dumps(agents).encode())

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        _MockOrchestratorHandler.received_requests.append({"path": self.path, "body": body})

        if self.path == "/agents":
            _MockOrchestratorHandler.spawn_count += 1
            response = {"status": "ok", "agent_id": f"sub-{_MockOrchestratorHandler.spawn_count}"}
        elif "/message" in self.path:
            response = {"message_id": f"msg-{len(self.received_requests)}"}
        else:
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())


def _run_mock_server() -> tuple[HTTPServer, int]:
    server = HTTPServer(("127.0.0.1", 0), _MockOrchestratorHandler)
    port = server.server_address[1]
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def test_deliberate_snippet_executes_against_mock_server(tmp_path: Path) -> None:
    """Execute the Python snippet against a mock server and verify it calls correct endpoints."""
    # Reset state
    _MockOrchestratorHandler.received_requests.clear()
    _MockOrchestratorHandler.spawn_count = 0

    server, port = _run_mock_server()
    base_url = f"http://127.0.0.1:{port}"

    # Write context file
    ctx = {
        "agent_id": "test-caller",
        "session_name": "test-session",
        "mailbox_dir": str(tmp_path / "mailbox"),
        "worktree_path": str(tmp_path),
        "web_base_url": base_url,
    }
    ctx_file = tmp_path / "__orchestrator_context__.json"
    ctx_file.write_text(json.dumps(ctx))

    # Pre-populate inbox with spawn confirmation messages so the snippet doesn't timeout
    inbox = tmp_path / "mailbox" / "test-session" / "test-caller" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    import uuid, time as _time
    for i, sub_id in enumerate(["advocate-sub-1", "critic-sub-1"]):
        msg = {
            "id": str(uuid.uuid4()),
            "type": "STATUS",
            "from_id": "__orchestrator__",
            "to_id": "test-caller",
            "payload": {"event": "subagent_spawned", "sub_agent_id": sub_id, "parent_id": "test-caller"},
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
        msg_file = inbox / f"{msg['id']}.json"
        msg_file.write_text(json.dumps(msg))

    # Extract and prepare snippet
    snippet = _extract_python_snippet(_read_deliberate())
    snippet = snippet.replace('"""$ARGUMENTS"""', '"Should we use SQLite or PostgreSQL?"')

    # Execute snippet in a temp directory with context file present
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        env_patch = {
            "TMUX_ORCHESTRATOR_API_KEY": "test-key",
            "TMUX_ORCHESTRATOR_AGENT_ID": "",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            exec(compile(snippet, "<deliberate_snippet>", "exec"), {})
    except SystemExit as e:
        if e.code != 0 and e.code is not None:
            pytest.fail(f"Snippet exited with non-zero code: {e.code}")
    finally:
        os.chdir(old_cwd)
        server.shutdown()

    # Verify: two POST /agents spawns + two POST /agents/{id}/message sends
    spawn_calls = [r for r in _MockOrchestratorHandler.received_requests if r["path"] == "/agents"]
    message_calls = [r for r in _MockOrchestratorHandler.received_requests if "/message" in r["path"]]

    assert len(spawn_calls) == 2, (
        f"Expected 2 spawn calls, got {len(spawn_calls)}: {spawn_calls}"
    )
    assert len(message_calls) == 2, (
        f"Expected 2 message sends (advocate + critic brief), got {len(message_calls)}: {message_calls}"
    )

    # Verify both spawns include parent_id
    for sc in spawn_calls:
        assert sc["body"].get("parent_id") == "test-caller", (
            f"spawn call must include parent_id='test-caller', got: {sc['body']}"
        )

    # Verify message bodies include advocate and critic role text
    message_texts = [r["body"].get("payload", {}).get("text", "") for r in message_calls]
    advocate_found = any("ADVOCATE" in t or "advocate" in t.lower() for t in message_texts)
    critic_found = any("CRITIC" in t or "critic" in t.lower() for t in message_texts)
    assert advocate_found, f"No advocate brief sent. Message texts: {message_texts}"
    assert critic_found, f"No critic brief sent. Message texts: {message_texts}"


# ---------------------------------------------------------------------------
# Verify deliberate.md is included in "all commands" checks
# ---------------------------------------------------------------------------


def test_deliberate_in_all_commands() -> None:
    """deliberate.md must be present among all plugin command files."""
    all_commands = {f.name for f in _commands_dir().glob("*.md")}
    assert "deliberate.md" in all_commands, (
        "deliberate.md not found in plugin commands directory"
    )
