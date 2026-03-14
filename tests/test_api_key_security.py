"""Tests for API key security (env-var-only delivery, v1.2.18+).

The API key is delivered exclusively via the TMUX_ORCHESTRATOR_API_KEY
environment variable (set by libtmux new-window -e KEY=VALUE at agent startup).
File-based fallbacks were removed as an unnecessary security risk.

References:
  - docs/security.md — APIキー配送セキュリティ方針
  - OWASP Secrets Management Cheat Sheet (2025)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
        agent = ClaudeCodeAgent(
            agent_id="worker-1",
            bus=_make_mock_bus(),
            tmux=_make_mock_tmux(),
            api_key="secret-key-abc123",
            session_name="test-session",
            web_base_url="http://localhost:8000",
        )
        agent._write_context_file(tmp_path)
        ctx = json.loads((tmp_path / "__orchestrator_context__.json").read_text())
        assert "api_key" not in ctx

    def test_context_file_still_has_other_fields(self, tmp_path: Path) -> None:
        agent = ClaudeCodeAgent(
            agent_id="worker-1",
            bus=_make_mock_bus(),
            tmux=_make_mock_tmux(),
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


# ---------------------------------------------------------------------------
# _write_api_key_file: must NOT exist
# ---------------------------------------------------------------------------


class TestNoApiKeyFileWritten:
    """ClaudeCodeAgent must not write API key files to disk."""

    def test_no_api_key_file_written(self, tmp_path: Path) -> None:
        """_write_api_key_file method must not exist on ClaudeCodeAgent."""
        agent = ClaudeCodeAgent(
            agent_id="worker-1",
            bus=_make_mock_bus(),
            tmux=_make_mock_tmux(),
            api_key="secret-key-abc123",
        )
        assert not hasattr(agent, "_write_api_key_file"), (
            "_write_api_key_file must be removed — API key is env-var only"
        )

    def test_no_key_file_artifacts_in_worktree(self, tmp_path: Path) -> None:
        """No __orchestrator_api_key__ files should appear after context write."""
        agent = ClaudeCodeAgent(
            agent_id="worker-1",
            bus=_make_mock_bus(),
            tmux=_make_mock_tmux(),
            api_key="secret-key-abc123",
            session_name="s",
            web_base_url="http://localhost:8000",
        )
        agent._write_context_file(tmp_path)
        key_files = list(tmp_path.glob("__orchestrator_api_key__*"))
        assert key_files == [], f"Unexpected key file(s) on disk: {key_files}"


# ---------------------------------------------------------------------------
# _read_api_key: env var only
# ---------------------------------------------------------------------------


class TestReadApiKey:
    """_read_api_key() must read from env var only."""

    def test_reads_from_env_var(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"TMUX_ORCHESTRATOR_API_KEY": "env-key"}):
            key = _read_api_key(tmp_path)
        assert key == "env-key"

    def test_returns_empty_string_when_no_env_var(self, tmp_path: Path) -> None:
        env = {k: v for k, v in os.environ.items() if k != "TMUX_ORCHESTRATOR_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            key = _read_api_key(tmp_path)
        assert key == ""

    def test_ignores_key_file_if_present(self, tmp_path: Path) -> None:
        """Even if a key file exists on disk, it must not be read."""
        (tmp_path / "__orchestrator_api_key__").write_text("file-key\n")
        env = {k: v for k, v in os.environ.items() if k != "TMUX_ORCHESTRATOR_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            key = _read_api_key(tmp_path)
        assert key == "", "File-based fallback must be ignored"


# ---------------------------------------------------------------------------
# notify_parent: uses env var for auth
# ---------------------------------------------------------------------------


class TestNotifyParentApiKey:
    """notify_parent must use TMUX_ORCHESTRATOR_API_KEY env var."""

    def _make_ctx(self, tmp_path: Path) -> None:
        ctx = {
            "agent_id": "worker-1",
            "web_base_url": "http://localhost:8000",
            "session_name": "orch",
            "mailbox_dir": str(tmp_path),
            "worktree_path": str(tmp_path),
        }
        (tmp_path / "__orchestrator_context__.json").write_text(json.dumps(ctx))

    def _mock_urlopen(self, agents_response: list) -> tuple:
        captured: list[dict] = []

        def mock_urlopen(req, timeout=None):
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            captured.append(dict(req.headers))
            if hasattr(req, "data") and req.data is not None:
                mock_resp.read.return_value = json.dumps({"message_id": "m"}).encode()
            else:
                mock_resp.read.return_value = json.dumps(agents_response).encode()
            return mock_resp

        return mock_urlopen, captured

    def test_env_var_sent_as_header(self, tmp_path: Path) -> None:
        self._make_ctx(tmp_path)
        agents = [{"id": "worker-1", "parent_id": "director"}]
        fn, captured = self._mock_urlopen(agents)
        with patch.dict(os.environ, {"TMUX_ORCHESTRATOR_API_KEY": "env-key"}):
            with patch("tmux_orchestrator.slash_notify._cwd", return_value=tmp_path):
                with patch("urllib.request.urlopen", side_effect=fn):
                    result = notify_parent(event_type="plan_created", extra={"description": "t"})
        assert result is True
        for hdrs in captured:
            lowered = {k.lower(): v for k, v in hdrs.items()}
            assert lowered.get("x-api-key") == "env-key"

    def test_no_key_header_when_no_env_var(self, tmp_path: Path) -> None:
        self._make_ctx(tmp_path)
        agents = [{"id": "worker-1", "parent_id": "director"}]
        fn, captured = self._mock_urlopen(agents)
        env = {k: v for k, v in os.environ.items() if k != "TMUX_ORCHESTRATOR_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with patch("tmux_orchestrator.slash_notify._cwd", return_value=tmp_path):
                with patch("urllib.request.urlopen", side_effect=fn):
                    result = notify_parent(event_type="plan_created", extra={"description": "t"})
        assert result is True
        for hdrs in captured:
            lowered = {k.lower(): v for k, v in hdrs.items()}
            assert "x-api-key" not in lowered

    def test_file_on_disk_not_used(self, tmp_path: Path) -> None:
        """Key file present on disk must not be used (env var only)."""
        self._make_ctx(tmp_path)
        (tmp_path / "__orchestrator_api_key__").write_text("file-key\n")
        agents = [{"id": "worker-1", "parent_id": "director"}]
        fn, captured = self._mock_urlopen(agents)
        env = {k: v for k, v in os.environ.items() if k != "TMUX_ORCHESTRATOR_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with patch("tmux_orchestrator.slash_notify._cwd", return_value=tmp_path):
                with patch("urllib.request.urlopen", side_effect=fn):
                    result = notify_parent(event_type="plan_created", extra={"description": "t"})
        assert result is True
        for hdrs in captured:
            lowered = {k.lower(): v for k, v in hdrs.items()}
            assert "x-api-key" not in lowered, "File-based key must NOT be used"


# ---------------------------------------------------------------------------
# gitignore: key file patterns still in .gitignore (belt-and-suspenders)
# ---------------------------------------------------------------------------


class TestGitignore:
    def test_orchestrator_api_key_in_gitignore(self) -> None:
        repo_root = Path(__file__).parent.parent
        gitignore = repo_root / ".gitignore"
        if not gitignore.exists():
            pytest.skip(".gitignore not found")
        content = gitignore.read_text()
        assert "__orchestrator_api_key__" in content
