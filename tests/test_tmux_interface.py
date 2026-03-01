"""Tests for TmuxInterface (mocked libtmux)."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from tmux_orchestrator.tmux_interface import TmuxInterface, _hash


# ---------------------------------------------------------------------------
# Unit tests that don't require a real tmux server
# ---------------------------------------------------------------------------


def test_hash_deterministic():
    assert _hash("hello") == _hash("hello")
    assert _hash("hello") != _hash("world")


def test_hash_uses_md5():
    text = "test content"
    expected = hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()
    assert _hash(text) == expected


@patch("tmux_orchestrator.tmux_interface.libtmux.Server")
def test_ensure_session_creates_new(mock_server_cls):
    mock_server = MagicMock()
    mock_server.find_where.return_value = None
    mock_session = MagicMock()
    mock_server.new_session.return_value = mock_session
    mock_server_cls.return_value = mock_server

    iface = TmuxInterface(session_name="test-session")
    session = iface.ensure_session()

    mock_server.new_session.assert_called_once_with(session_name="test-session")
    assert session is mock_session


@patch("tmux_orchestrator.tmux_interface.libtmux.Server")
def test_ensure_session_attaches_existing(mock_server_cls):
    mock_server = MagicMock()
    existing = MagicMock()
    mock_server.find_where.return_value = existing
    mock_server_cls.return_value = mock_server

    iface = TmuxInterface(session_name="existing")
    session = iface.ensure_session()

    mock_server.new_session.assert_not_called()
    assert session is existing


@patch("tmux_orchestrator.tmux_interface.libtmux.Server")
def test_watch_and_unwatch_pane(mock_server_cls):
    mock_server = MagicMock()
    mock_server_cls.return_value = mock_server

    mock_pane = MagicMock()
    mock_pane.id = "%42"
    mock_pane.capture_pane.return_value = ["line1", "line2"]

    iface = TmuxInterface(session_name="s")

    # Patch capture_pane to return known text
    with patch.object(iface, "capture_pane", return_value="line1\nline2"):
        iface.watch_pane(mock_pane, "agent-1")
        assert "%42" in iface._watched
        iface.unwatch_pane(mock_pane)
        assert "%42" not in iface._watched


@patch("tmux_orchestrator.tmux_interface.libtmux.Server")
def test_send_keys_delegates(mock_server_cls):
    mock_server = MagicMock()
    mock_server_cls.return_value = mock_server

    mock_pane = MagicMock()
    iface = TmuxInterface(session_name="s")
    iface.send_keys(mock_pane, "echo hello")

    mock_pane.send_keys.assert_called_once_with("echo hello", enter=True)


@patch("tmux_orchestrator.tmux_interface.libtmux.Server")
def test_capture_pane_joins_lines(mock_server_cls):
    mock_server = MagicMock()
    mock_server_cls.return_value = mock_server

    mock_pane = MagicMock()
    mock_pane.capture_pane.return_value = ["line 1", "line 2", "line 3"]

    iface = TmuxInterface(session_name="s")
    result = iface.capture_pane(mock_pane)
    assert result == "line 1\nline 2\nline 3"
