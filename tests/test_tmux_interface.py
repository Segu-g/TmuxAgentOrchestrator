"""Tests for TmuxInterface (mocked libtmux)."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, call, patch

import pytest

from tmux_orchestrator.tmux_interface import TmuxInterface, _hash
from tmux_orchestrator.infrastructure.tmux import _paste_preview_active


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
    mock_server.sessions.get.return_value = None
    mock_session = MagicMock()
    mock_server.new_session.return_value = mock_session
    mock_server_cls.return_value = mock_server

    iface = TmuxInterface(session_name="test-session")
    session = iface.ensure_session()

    mock_server.new_session.assert_called_once_with(session_name="test-session")
    assert session is mock_session


@patch("tmux_orchestrator.tmux_interface.libtmux.Server")
def test_ensure_session_kills_existing_and_creates_fresh(mock_server_cls):
    """When a session exists and the user confirms, it is killed and replaced."""
    mock_server = MagicMock()
    existing = MagicMock()
    fresh = MagicMock()
    mock_server.sessions.get.return_value = existing
    mock_server.new_session.return_value = fresh
    mock_server_cls.return_value = mock_server

    iface = TmuxInterface(session_name="existing", confirm_kill=lambda _: True)
    session = iface.ensure_session()

    existing.kill.assert_called_once()
    mock_server.new_session.assert_called_once_with(session_name="existing")
    assert session is fresh


@patch("tmux_orchestrator.tmux_interface.libtmux.Server")
def test_ensure_session_aborts_when_user_declines(mock_server_cls):
    """When the user declines the kill confirmation, a RuntimeError is raised."""
    mock_server = MagicMock()
    existing = MagicMock()
    mock_server.sessions.get.return_value = existing
    mock_server_cls.return_value = mock_server

    iface = TmuxInterface(session_name="existing", confirm_kill=lambda _: False)
    with pytest.raises(RuntimeError, match="already exists"):
        iface.ensure_session()

    existing.kill.assert_not_called()


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


@patch("tmux_orchestrator.tmux_interface.time.sleep")
@patch("tmux_orchestrator.tmux_interface.libtmux.Server")
def test_send_keys_delegates(mock_server_cls, mock_sleep):
    mock_server = MagicMock()
    mock_server_cls.return_value = mock_server

    mock_pane = MagicMock()
    # Simulate no paste-preview: capture_pane returns normal output
    mock_pane.capture_pane.return_value = ["❯"]
    iface = TmuxInterface(session_name="s")

    import time as real_time
    from tmux_orchestrator.infrastructure.tmux import _PASTE_PREVIEW_POLL_S
    t0 = real_time.monotonic()
    with patch("tmux_orchestrator.tmux_interface.time.monotonic",
               side_effect=iter([t0, t0 + _PASTE_PREVIEW_POLL_S + 1.0])):
        iface.send_keys(mock_pane, "echo hello")

    # Enter is always sent as a separate keypress after a delay
    calls = mock_pane.send_keys.call_args_list
    assert calls[0] == call("echo hello", enter=False)
    assert calls[-1] == call("", enter=True)
    assert mock_sleep.call_count >= 1


@patch("tmux_orchestrator.tmux_interface.time.sleep")
@patch("tmux_orchestrator.tmux_interface.libtmux.Server")
def test_send_keys_multiline_sends_enter_separately(mock_server_cls, mock_sleep):
    """Multi-line text must be followed by a separate Enter to escape paste-preview."""
    mock_server_cls.return_value = MagicMock()
    mock_pane = MagicMock()
    # Simulate no paste-preview
    mock_pane.capture_pane.return_value = ["❯"]
    iface = TmuxInterface(session_name="s")

    import time as real_time
    from tmux_orchestrator.infrastructure.tmux import _PASTE_PREVIEW_POLL_S
    t0 = real_time.monotonic()
    with patch("tmux_orchestrator.tmux_interface.time.monotonic",
               side_effect=iter([t0, t0 + _PASTE_PREVIEW_POLL_S + 1.0])):
        iface.send_keys(mock_pane, "line1\nline2")

    calls = mock_pane.send_keys.call_args_list
    assert calls[0] == call("line1\nline2", enter=False)
    assert calls[-1] == call("", enter=True)
    assert mock_sleep.call_count >= 1


@patch("tmux_orchestrator.tmux_interface.libtmux.Server")
def test_capture_pane_joins_lines(mock_server_cls):
    mock_server = MagicMock()
    mock_server_cls.return_value = mock_server

    mock_pane = MagicMock()
    mock_pane.capture_pane.return_value = ["line 1", "line 2", "line 3"]

    iface = TmuxInterface(session_name="s")
    result = iface.capture_pane(mock_pane)
    assert result == "line 1\nline 2\nline 3"


# ---------------------------------------------------------------------------
# _paste_preview_active unit tests
# ---------------------------------------------------------------------------


class TestPastePreviewActive:
    """Unit tests for the _paste_preview_active() helper function."""

    def test_detects_pasted_text_marker(self):
        """[Pasted text in last lines returns True."""
        output = "some prior output\n❯ [Pasted text #1]"
        assert _paste_preview_active(output) is True

    def test_detects_pasted_text_numbered_variant(self):
        """[Pasted text #2] variant is also detected."""
        output = "\n".join(["line1", "line2", "[Pasted text #2]", ""])
        assert _paste_preview_active(output) is True

    def test_no_paste_preview_returns_false(self):
        """Normal pane output returns False."""
        output = "Welcome to claude\n❯ "
        assert _paste_preview_active(output) is False

    def test_empty_output_returns_false(self):
        """Empty output returns False."""
        assert _paste_preview_active("") is False

    def test_old_scrollback_not_detected(self):
        """[Pasted text only in old scrollback (> last 10 lines) returns False."""
        # Put the [Pasted text marker far back in scrollback — beyond last 10 lines
        lines = ["[Pasted text #1]"] + ["normal output"] * 15
        output = "\n".join(lines)
        assert _paste_preview_active(output) is False

    def test_recent_pasted_text_detected(self):
        """[Pasted text in recent lines returns True."""
        lines = ["old line"] * 20 + ["❯ [Pasted text #1]"]
        output = "\n".join(lines)
        assert _paste_preview_active(output) is True


# ---------------------------------------------------------------------------
# send_keys paste-preview detection tests
# ---------------------------------------------------------------------------


@patch("tmux_orchestrator.tmux_interface.time.sleep")
@patch("tmux_orchestrator.tmux_interface.libtmux.Server")
def test_send_keys_paste_preview_sends_extra_enter(mock_server_cls, mock_sleep):
    """When paste-preview is detected, an extra Enter is sent to dismiss it."""
    mock_server_cls.return_value = MagicMock()
    mock_pane = MagicMock()
    # Simulate paste-preview showing in pane output
    mock_pane.capture_pane.return_value = ["❯ [Pasted text #1]"]
    iface = TmuxInterface(session_name="s")

    # Patch time.monotonic so poll loop runs exactly once before detecting preview
    import time as real_time
    t0 = real_time.monotonic()
    monotonic_values = iter([t0, t0 + 0.01])  # first poll within deadline
    with patch("tmux_orchestrator.tmux_interface.time.monotonic", side_effect=monotonic_values):
        iface.send_keys(mock_pane, "a " * 200)  # long prompt

    calls = mock_pane.send_keys.call_args_list
    # First call: the text itself (enter=False keyword arg)
    assert calls[0] == call("a " * 200, enter=False)
    # Must have at least 3 send_keys calls: text, dismiss-Enter, normal-Enter
    assert len(calls) >= 3
    # Last two calls must both be ("", enter=True)
    assert calls[-1] == call("", enter=True)
    assert calls[-2] == call("", enter=True)
    # sleep called at least twice (initial wait + dismiss wait)
    assert mock_sleep.call_count >= 2


@patch("tmux_orchestrator.tmux_interface.time.sleep")
@patch("tmux_orchestrator.tmux_interface.libtmux.Server")
def test_send_keys_no_paste_preview_single_enter(mock_server_cls, mock_sleep):
    """When no paste-preview is detected, only one Enter is sent."""
    mock_server_cls.return_value = MagicMock()
    mock_pane = MagicMock()
    # Simulate normal output (no paste-preview)
    mock_pane.capture_pane.return_value = ["❯ "]
    iface = TmuxInterface(session_name="s")

    # Patch time.monotonic to expire the poll loop immediately (1st call returns
    # poll_start, 2nd call returns poll_start + poll_budget so loop exits at once).
    import time as real_time
    t0 = real_time.monotonic()
    from tmux_orchestrator.infrastructure.tmux import _PASTE_PREVIEW_POLL_S
    monotonic_values = iter([t0, t0 + _PASTE_PREVIEW_POLL_S + 1.0])
    with patch("tmux_orchestrator.tmux_interface.time.monotonic", side_effect=monotonic_values):
        iface.send_keys(mock_pane, "short prompt")

    calls = mock_pane.send_keys.call_args_list
    # Exactly 2 calls: text (enter=False) + final Enter
    assert len(calls) == 2
    assert calls[0] == call("short prompt", enter=False)
    assert calls[1] == call("", enter=True)
    # sleep called at least once (initial 0.05s wait)
    assert mock_sleep.call_count >= 1
