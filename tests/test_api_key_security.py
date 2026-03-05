"""Tests for API key security fix (v0.35.0).

The API key must NOT appear in __orchestrator_context__.json.
Instead it is written to __orchestrator_api_key__ with mode 0o600,
and also injected via TMUX_ORCHESTRATOR_API_KEY environment variable
(via libtmux session.set_environment).

References:
  - DESIGN.md §3 "API キー配送のセキュリティ方針"
  - OpenStack Security Guidelines "Apply Restrictive File Permissions"
  - OWASP Cheat Sheet: Secrets Management
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tmux_orchestrator.agents.base import Agent, AgentStatus
from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.slash_notify import notify_parent, _read_api_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_bus() -> MagicMock:
    bus = MagicMock(spec=Bus)
    bus.subscribe = MagicMock()
    bus.publish = MagicMock()
    return bus


def _make_mock_tmux() -> MagicMock:
    from tmux_orchestrator.tmux_interface import TmuxInterface
    tmux = MagicMock(spec=TmuxInterface)
    tmux.new_pane.return_value = MagicMock()
    tmux.send_keys.return_value = None
    tmux.watch_pane.return_value = None
    tmux.start_watcher.return_value = None
    # Expose mock session for set_environment tests
    mock_session = MagicMock()
    tmux._session = mock_session
    tmux.ensure_session.return_value = mock_session
    return tmux


# ---------------------------------------------------------------------------
# _write_context_file: api_key must NOT be in __orchestrator_context__.json
# ---------------------------------------------------------------------------


class TestContextFileExcludesApiKey:
    """The context file must not contain api_key."""

    def test_context_file_has_no_api_key_field(self, tmp_path: Path) -> None:
        """api_key must not appear in __orchestrator_context__.json."""
        bus = _make_mock_bus()
        tmux = _make_mock_tmux()
        agent = ClaudeCodeAgent(
            agent_id="worker-1",
            bus=bus,
            tmux=tmux,
            api_key="secret-key-abc123",
            session_name="test-session",
            web_base_url="http://localhost:8000",
        )
        agent._write_context_file(tmp_path)
        ctx = json.loads((tmp_path / "__orchestrator_context__.json").read_text())
        assert "api_key" not in ctx, (
            "api_key must NOT be written to __orchestrator_context__.json"
        )

    def test_context_file_still_has_other_fields(self, tmp_path: Path) -> None:
        """Context file retains non-sensitive fields after api_key removal."""
        bus = _make_mock_bus()
        tmux = _make_mock_tmux()
        agent = ClaudeCodeAgent(
            agent_id="worker-1",
            bus=bus,
            tmux=tmux,
            api_key="secret-key-abc123",
            session_name="test-session",
            web_base_url="http://localhost:8000",
        )
        agent._write_context_file(tmp_path)
        ctx = json.loads((tmp_path / "__orchestrator_context__.json").read_text())
        assert ctx["agent_id"] == "worker-1"
        assert ctx["session_name"] == "test-session"
        assert ctx["web_base_url"] == "http://localhost:8000"
        assert "worktree_path" in ctx

    def test_context_file_no_api_key_when_key_is_empty(self, tmp_path: Path) -> None:
        """When api_key='', context file has no api_key field (same as before)."""
        bus = _make_mock_bus()
        tmux = _make_mock_tmux()
        agent = ClaudeCodeAgent(
            agent_id="worker-1",
            bus=bus,
            tmux=tmux,
            api_key="",
            session_name="test-session",
            web_base_url="http://localhost:8000",
        )
        agent._write_context_file(tmp_path)
        ctx = json.loads((tmp_path / "__orchestrator_context__.json").read_text())
        assert "api_key" not in ctx


# ---------------------------------------------------------------------------
# _write_api_key_file: separate file with mode 0o600
# ---------------------------------------------------------------------------


class TestApiKeyFile:
    """__orchestrator_api_key__ must be created with restrictive permissions."""

    def test_api_key_file_created(self, tmp_path: Path) -> None:
        """When api_key is set, __orchestrator_api_key__ file must be created."""
        bus = _make_mock_bus()
        tmux = _make_mock_tmux()
        agent = ClaudeCodeAgent(
            agent_id="worker-1",
            bus=bus,
            tmux=tmux,
            api_key="secret-key-abc123",
        )
        agent._write_api_key_file(tmp_path)
        key_file = tmp_path / "__orchestrator_api_key__"
        assert key_file.exists(), "__orchestrator_api_key__ must be created"

    def test_api_key_file_contains_key(self, tmp_path: Path) -> None:
        """The key file must contain the API key (stripped)."""
        bus = _make_mock_bus()
        tmux = _make_mock_tmux()
        agent = ClaudeCodeAgent(
            agent_id="worker-1",
            bus=bus,
            tmux=tmux,
            api_key="my-secret-token-xyz",
        )
        agent._write_api_key_file(tmp_path)
        content = (tmp_path / "__orchestrator_api_key__").read_text().strip()
        assert content == "my-secret-token-xyz"

    def test_api_key_file_permissions_are_600(self, tmp_path: Path) -> None:
        """__orchestrator_api_key__ must have permissions 0o600 (owner rw only)."""
        bus = _make_mock_bus()
        tmux = _make_mock_tmux()
        agent = ClaudeCodeAgent(
            agent_id="worker-1",
            bus=bus,
            tmux=tmux,
            api_key="secret-key-abc123",
        )
        agent._write_api_key_file(tmp_path)
        key_file = tmp_path / "__orchestrator_api_key__"
        mode = stat.S_IMODE(os.stat(key_file).st_mode)
        assert mode == 0o600, (
            f"Expected 0o600 permissions, got {oct(mode)}"
        )

    def test_api_key_file_not_created_when_key_empty(self, tmp_path: Path) -> None:
        """When api_key='', __orchestrator_api_key__ must NOT be created."""
        bus = _make_mock_bus()
        tmux = _make_mock_tmux()
        agent = ClaudeCodeAgent(
            agent_id="worker-1",
            bus=bus,
            tmux=tmux,
            api_key="",
        )
        agent._write_api_key_file(tmp_path)
        key_file = tmp_path / "__orchestrator_api_key__"
        assert not key_file.exists()

    def test_api_key_file_overwritten_atomically(self, tmp_path: Path) -> None:
        """Writing a second time should overwrite the key file safely."""
        bus = _make_mock_bus()
        tmux = _make_mock_tmux()
        agent = ClaudeCodeAgent(
            agent_id="worker-1",
            bus=bus,
            tmux=tmux,
            api_key="new-key-v2",
        )
        # Write once
        agent._write_api_key_file(tmp_path)
        # Write again (simulating restart)
        agent._api_key = "new-key-v2-updated"
        agent._write_api_key_file(tmp_path)
        content = (tmp_path / "__orchestrator_api_key__").read_text().strip()
        assert content == "new-key-v2-updated"


# ---------------------------------------------------------------------------
# _read_api_key: utility to read API key in slash commands
# ---------------------------------------------------------------------------


class TestReadApiKey:
    """_read_api_key() reads api_key from env var or __orchestrator_api_key__ file."""

    def test_reads_from_env_var(self, tmp_path: Path) -> None:
        """TMUX_ORCHESTRATOR_API_KEY env var takes priority over file."""
        (tmp_path / "__orchestrator_api_key__").write_text("file-key")
        with patch.dict(os.environ, {"TMUX_ORCHESTRATOR_API_KEY": "env-key"}):
            key = _read_api_key(tmp_path)
        assert key == "env-key"

    def test_reads_from_file_when_no_env_var(self, tmp_path: Path) -> None:
        """When env var absent, reads from __orchestrator_api_key__ file."""
        (tmp_path / "__orchestrator_api_key__").write_text("file-key-from-file\n")
        env = {k: v for k, v in os.environ.items() if k != "TMUX_ORCHESTRATOR_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            key = _read_api_key(tmp_path)
        assert key == "file-key-from-file"

    def test_returns_empty_string_when_neither_present(self, tmp_path: Path) -> None:
        """Returns '' when neither env var nor key file exists."""
        env = {k: v for k, v in os.environ.items() if k != "TMUX_ORCHESTRATOR_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            key = _read_api_key(tmp_path)
        assert key == ""

    def test_strips_whitespace_from_file(self, tmp_path: Path) -> None:
        """Trailing newlines/spaces in key file are stripped."""
        (tmp_path / "__orchestrator_api_key__").write_text("  my-key  \n")
        env = {k: v for k, v in os.environ.items() if k != "TMUX_ORCHESTRATOR_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            key = _read_api_key(tmp_path)
        assert key == "my-key"


# ---------------------------------------------------------------------------
# notify_parent: reads api_key via _read_api_key (not from context JSON)
# ---------------------------------------------------------------------------


class TestNotifyParentApiKey:
    """notify_parent must use _read_api_key(), not ctx['api_key']."""

    def test_notify_parent_uses_key_file_for_auth(self, tmp_path: Path) -> None:
        """When __orchestrator_api_key__ exists, X-API-Key header is set."""
        ctx = {
            "agent_id": "worker-1",
            "web_base_url": "http://localhost:8000",
            "session_name": "orch",
            "mailbox_dir": str(tmp_path),
            "worktree_path": str(tmp_path),
        }
        (tmp_path / "__orchestrator_context__.json").write_text(json.dumps(ctx))
        (tmp_path / "__orchestrator_api_key__").write_text("key-from-file\n")

        agents_response = [{"id": "worker-1", "parent_id": "director"}]
        captured_headers: list[dict] = []

        def mock_urlopen(req, timeout=None):
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            captured_headers.append(dict(req.headers))
            if hasattr(req, "data") and req.data is not None:
                mock_resp.read.return_value = json.dumps({"message_id": "m"}).encode()
            else:
                mock_resp.read.return_value = json.dumps(agents_response).encode()
            return mock_resp

        env = {k: v for k, v in os.environ.items() if k != "TMUX_ORCHESTRATOR_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with patch("tmux_orchestrator.slash_notify._cwd", return_value=tmp_path):
                with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                    result = notify_parent(
                        event_type="plan_created",
                        extra={"description": "test"},
                    )

        assert result is True
        # Both GET and POST should include the X-API-Key header
        # (urllib.request capitalises first char: X-api-key)
        for hdrs in captured_headers:
            lowered = {k.lower(): v for k, v in hdrs.items()}
            assert "x-api-key" in lowered
            assert lowered["x-api-key"] == "key-from-file"

    def test_notify_parent_uses_env_var_for_auth(self, tmp_path: Path) -> None:
        """When TMUX_ORCHESTRATOR_API_KEY is set, it takes priority."""
        ctx = {
            "agent_id": "worker-1",
            "web_base_url": "http://localhost:8000",
            "session_name": "orch",
            "mailbox_dir": str(tmp_path),
            "worktree_path": str(tmp_path),
        }
        (tmp_path / "__orchestrator_context__.json").write_text(json.dumps(ctx))
        # Also write key file with different value
        (tmp_path / "__orchestrator_api_key__").write_text("file-key\n")

        agents_response = [{"id": "worker-1", "parent_id": "director"}]
        captured_headers: list[dict] = []

        def mock_urlopen(req, timeout=None):
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            captured_headers.append(dict(req.headers))
            if hasattr(req, "data") and req.data is not None:
                mock_resp.read.return_value = json.dumps({"message_id": "m"}).encode()
            else:
                mock_resp.read.return_value = json.dumps(agents_response).encode()
            return mock_resp

        with patch.dict(os.environ, {"TMUX_ORCHESTRATOR_API_KEY": "env-key"}):
            with patch("tmux_orchestrator.slash_notify._cwd", return_value=tmp_path):
                with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                    result = notify_parent(
                        event_type="plan_created",
                        extra={"description": "test"},
                    )

        assert result is True
        for hdrs in captured_headers:
            lowered = {k.lower(): v for k, v in hdrs.items()}
            assert lowered.get("x-api-key") == "env-key"

    def test_notify_parent_no_api_key_header_when_no_key(
        self, tmp_path: Path
    ) -> None:
        """When no API key is available, X-API-Key header is not sent."""
        ctx = {
            "agent_id": "worker-1",
            "web_base_url": "http://localhost:8000",
            "session_name": "orch",
            "mailbox_dir": str(tmp_path),
            "worktree_path": str(tmp_path),
        }
        (tmp_path / "__orchestrator_context__.json").write_text(json.dumps(ctx))

        agents_response = [{"id": "worker-1", "parent_id": "director"}]
        captured_headers: list[dict] = []

        def mock_urlopen(req, timeout=None):
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            captured_headers.append(dict(req.headers))
            if hasattr(req, "data") and req.data is not None:
                mock_resp.read.return_value = json.dumps({"message_id": "m"}).encode()
            else:
                mock_resp.read.return_value = json.dumps(agents_response).encode()
            return mock_resp

        env = {k: v for k, v in os.environ.items() if k != "TMUX_ORCHESTRATOR_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with patch("tmux_orchestrator.slash_notify._cwd", return_value=tmp_path):
                with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                    result = notify_parent(
                        event_type="plan_created",
                        extra={"description": "test"},
                    )

        assert result is True
        for hdrs in captured_headers:
            lowered = {k.lower(): v for k, v in hdrs.items()}
            assert "x-api-key" not in lowered


# ---------------------------------------------------------------------------
# gitignore: __orchestrator_api_key__ must be in .gitignore
# ---------------------------------------------------------------------------


class TestGitignore:
    """__orchestrator_api_key__ must be listed in .gitignore."""

    def test_orchestrator_api_key_in_gitignore(self) -> None:
        """Check that .gitignore contains __orchestrator_api_key__."""
        repo_root = Path(__file__).parent.parent
        gitignore = repo_root / ".gitignore"
        if not gitignore.exists():
            pytest.skip(".gitignore not found")
        content = gitignore.read_text()
        assert "__orchestrator_api_key__" in content, (
            "__orchestrator_api_key__ must be in .gitignore to prevent accidental commits"
        )
