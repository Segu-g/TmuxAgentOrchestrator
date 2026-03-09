"""Tests for ProcessPort protocol and adapter implementations.

Design references:
- PEP 544 Protocols: Structural subtyping (peps.python.org/pep-0544/)
- Martin "Clean Architecture" (2017) Ch.22 — Port & Adapter pattern
- "Hexagonal Architecture in Python" SoftwarePatternLexicon (2025)
- "Hexagonal Architecture Design: Python Ports and Adapters" johal.in (2026)
- DESIGN.md §10.13 (v0.46.0), §10.34 (v1.0.34 — send_interrupt/get_pane_id)
"""

from __future__ import annotations

import asyncio

import pytest

from tmux_orchestrator.process_port import ProcessPort, StdioProcessAdapter


# ---------------------------------------------------------------------------
# ProcessPort is a Protocol (structural subtyping)
# ---------------------------------------------------------------------------


def test_process_port_is_protocol():
    """ProcessPort is a typing.Protocol class."""
    from typing import Protocol
    assert issubclass(ProcessPort, Protocol)


def test_process_port_has_required_methods():
    """ProcessPort defines send_keys, capture_pane, send_interrupt, get_pane_id."""
    assert hasattr(ProcessPort, "send_keys")
    assert hasattr(ProcessPort, "capture_pane")
    assert hasattr(ProcessPort, "send_interrupt")
    assert hasattr(ProcessPort, "get_pane_id")


# ---------------------------------------------------------------------------
# StdioProcessAdapter — in-process fake for testing
# ---------------------------------------------------------------------------


def test_stdio_adapter_initial_state():
    """StdioProcessAdapter starts with empty output."""
    adapter = StdioProcessAdapter()
    assert adapter.capture_pane() == ""


def test_stdio_adapter_send_keys():
    """send_keys() appends text to the adapter's internal buffer."""
    adapter = StdioProcessAdapter()
    adapter.send_keys("hello world")
    assert "hello world" in adapter.capture_pane()


def test_stdio_adapter_send_keys_multiple():
    """Multiple send_keys() calls accumulate in the output buffer."""
    adapter = StdioProcessAdapter()
    adapter.send_keys("line 1")
    adapter.send_keys("line 2")
    output = adapter.capture_pane()
    assert "line 1" in output
    assert "line 2" in output


def test_stdio_adapter_send_keys_no_enter():
    """send_keys(enter=False) appends text without a newline separator."""
    adapter = StdioProcessAdapter()
    adapter.send_keys("partial", enter=False)
    output = adapter.capture_pane()
    assert "partial" in output


def test_stdio_adapter_set_output():
    """set_output() replaces the entire output buffer."""
    adapter = StdioProcessAdapter()
    adapter.send_keys("old content")
    adapter.set_output("new content\n❯")
    output = adapter.capture_pane()
    assert output == "new content\n❯"
    assert "old content" not in output


def test_stdio_adapter_append_output():
    """append_output() adds text to the output buffer."""
    adapter = StdioProcessAdapter()
    adapter.set_output("initial")
    adapter.append_output("\nappended")
    assert "initial" in adapter.capture_pane()
    assert "appended" in adapter.capture_pane()


def test_stdio_adapter_sent_keys_history():
    """StdioProcessAdapter records all sent keys for assertion in tests."""
    adapter = StdioProcessAdapter()
    adapter.send_keys("task prompt")
    adapter.send_keys("another command")
    history = adapter.sent_keys_history()
    assert "task prompt" in history
    assert "another command" in history


def test_stdio_adapter_clear_history():
    """clear_history() resets sent keys and output buffer."""
    adapter = StdioProcessAdapter()
    adapter.send_keys("foo")
    adapter.set_output("bar")
    adapter.clear()
    assert adapter.capture_pane() == ""
    assert adapter.sent_keys_history() == []


# ---------------------------------------------------------------------------
# Structural subtyping: StdioProcessAdapter satisfies ProcessPort
# ---------------------------------------------------------------------------


def test_stdio_adapter_satisfies_process_port_protocol():
    """StdioProcessAdapter is structurally compatible with ProcessPort."""
    adapter = StdioProcessAdapter()
    # Static check: adapter can be used wherever ProcessPort is expected
    # Runtime check via isinstance with @runtime_checkable
    assert isinstance(adapter, ProcessPort)


def test_custom_class_satisfies_process_port_via_duck_typing():
    """Any class with all four ProcessPort methods satisfies the protocol."""

    class MinimalAdapter:
        def send_keys(self, keys: str, enter: bool = True) -> None:
            pass

        def capture_pane(self) -> str:
            return "output"

        def send_interrupt(self) -> None:
            pass

        def get_pane_id(self) -> str:
            return "test-pane"

    adapter = MinimalAdapter()
    assert isinstance(adapter, ProcessPort)


def test_missing_method_does_not_satisfy_process_port():
    """A class without all required methods does not satisfy ProcessPort."""

    class Incomplete:
        def send_keys(self, keys: str, enter: bool = True) -> None:
            pass

        def capture_pane(self) -> str:
            return "output"

        # Missing send_interrupt and get_pane_id

    adapter = Incomplete()
    assert not isinstance(adapter, ProcessPort)


# ---------------------------------------------------------------------------
# TmuxProcessAdapter
# ---------------------------------------------------------------------------


def test_tmux_adapter_wraps_pane():
    """TmuxProcessAdapter wraps a pane object and delegates to it."""
    from tmux_orchestrator.process_port import TmuxProcessAdapter
    from unittest.mock import MagicMock

    mock_tmux = MagicMock()
    mock_pane = MagicMock()
    mock_tmux.capture_pane.return_value = "captured output"

    adapter = TmuxProcessAdapter(pane=mock_pane, tmux=mock_tmux)
    # send_keys delegates to tmux.send_keys
    adapter.send_keys("test command")
    mock_tmux.send_keys.assert_called_once_with(mock_pane, "test command")

    # capture_pane delegates to tmux.capture_pane
    result = adapter.capture_pane()
    mock_tmux.capture_pane.assert_called_once_with(mock_pane)
    assert result == "captured output"


def test_tmux_adapter_satisfies_process_port():
    """TmuxProcessAdapter satisfies ProcessPort protocol."""
    from tmux_orchestrator.process_port import TmuxProcessAdapter
    from unittest.mock import MagicMock

    adapter = TmuxProcessAdapter(pane=MagicMock(), tmux=MagicMock())
    assert isinstance(adapter, ProcessPort)


# ---------------------------------------------------------------------------
# TmuxInterface integration helpers
# ---------------------------------------------------------------------------


def test_tmux_interface_has_create_adapter_method():
    """TmuxInterface provides a create_process_adapter() factory method."""
    from tmux_orchestrator.tmux_interface import TmuxInterface
    assert hasattr(TmuxInterface, "create_process_adapter")


# ---------------------------------------------------------------------------
# v1.0.34 — send_interrupt() and get_pane_id() additions
# ---------------------------------------------------------------------------


def test_stdio_adapter_send_interrupt_records_ctrl_c():
    """StdioProcessAdapter.send_interrupt() records 'C-c' in sent keys history."""
    adapter = StdioProcessAdapter()
    adapter.send_interrupt()
    assert "C-c" in adapter.sent_keys_history()


def test_stdio_adapter_send_interrupt_does_not_modify_output():
    """StdioProcessAdapter.send_interrupt() does not change the output buffer."""
    adapter = StdioProcessAdapter()
    adapter.set_output("some output ❯")
    adapter.send_interrupt()
    assert adapter.capture_pane() == "some output ❯"


def test_stdio_adapter_get_pane_id_returns_stdio():
    """StdioProcessAdapter.get_pane_id() returns the fixed string 'stdio'."""
    adapter = StdioProcessAdapter()
    assert adapter.get_pane_id() == "stdio"


def test_stdio_adapter_satisfies_full_process_port():
    """StdioProcessAdapter satisfies ProcessPort including new methods."""
    adapter = StdioProcessAdapter()
    assert isinstance(adapter, ProcessPort)


def test_tmux_adapter_send_interrupt_calls_pane_send_keys():
    """TmuxProcessAdapter.send_interrupt() calls pane.send_keys('C-c')."""
    from tmux_orchestrator.process_port import TmuxProcessAdapter
    from unittest.mock import MagicMock

    mock_pane = MagicMock()
    mock_tmux = MagicMock()
    adapter = TmuxProcessAdapter(pane=mock_pane, tmux=mock_tmux)
    adapter.send_interrupt()
    mock_pane.send_keys.assert_called_once_with("C-c")


def test_tmux_adapter_get_pane_id_returns_pane_id_attr():
    """TmuxProcessAdapter.get_pane_id() returns pane.id from libtmux."""
    from tmux_orchestrator.process_port import TmuxProcessAdapter
    from unittest.mock import MagicMock

    mock_pane = MagicMock()
    mock_pane.id = "%42"
    mock_tmux = MagicMock()
    adapter = TmuxProcessAdapter(pane=mock_pane, tmux=mock_tmux)
    assert adapter.get_pane_id() == "%42"


def test_tmux_adapter_satisfies_full_process_port():
    """TmuxProcessAdapter satisfies full ProcessPort including new methods."""
    from tmux_orchestrator.process_port import TmuxProcessAdapter
    from unittest.mock import MagicMock

    adapter = TmuxProcessAdapter(pane=MagicMock(), tmux=MagicMock())
    assert isinstance(adapter, ProcessPort)
