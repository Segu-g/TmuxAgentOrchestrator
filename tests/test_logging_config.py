"""Tests for structured JSON logging (logging_config.py)."""
from __future__ import annotations

import json
import logging

import pytest

from tmux_orchestrator.logging_config import (
    JsonFormatter,
    bind_agent,
    bind_trace,
    current_agent_id,
    current_trace_id,
    setup_json_logging,
    unbind,
)


# ---------------------------------------------------------------------------
# JsonFormatter
# ---------------------------------------------------------------------------


def _make_record(msg: str = "hello", level: int = logging.INFO) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=level,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )
    return record


def test_json_formatter_basic_fields():
    fmt = JsonFormatter()
    output = fmt.format(_make_record("test message"))
    obj = json.loads(output)
    assert obj["level"] == "INFO"
    assert obj["logger"] == "test.logger"
    assert obj["msg"] == "test message"
    assert "ts" in obj


def test_json_formatter_no_trace_by_default():
    fmt = JsonFormatter()
    obj = json.loads(fmt.format(_make_record()))
    assert "trace_id" not in obj
    assert "agent_id" not in obj


def test_json_formatter_includes_trace_when_bound():
    token = bind_trace("abc123")
    try:
        fmt = JsonFormatter()
        obj = json.loads(fmt.format(_make_record()))
        assert obj["trace_id"] == "abc123"
    finally:
        unbind(token)


def test_json_formatter_includes_agent_when_bound():
    token = bind_agent("worker-1")
    try:
        fmt = JsonFormatter()
        obj = json.loads(fmt.format(_make_record()))
        assert obj["agent_id"] == "worker-1"
    finally:
        unbind(token)


def test_json_formatter_includes_exc_info():
    fmt = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = _make_record("oops")
        record.exc_info = sys.exc_info()
        obj = json.loads(fmt.format(record))
        assert "exc" in obj
        assert "ValueError" in obj["exc"]


def test_json_formatter_output_is_single_line():
    fmt = JsonFormatter()
    output = fmt.format(_make_record("multi\nline"))
    # The JSON encoding should produce a single line (newline in msg is escaped)
    assert "\n" not in output


# ---------------------------------------------------------------------------
# Context variable helpers
# ---------------------------------------------------------------------------


def test_bind_trace_and_unbind():
    assert current_trace_id() == ""
    token = bind_trace("trace-xyz")
    assert current_trace_id() == "trace-xyz"
    unbind(token)
    assert current_trace_id() == ""


def test_bind_agent_and_unbind():
    assert current_agent_id() == ""
    token = bind_agent("agent-42")
    assert current_agent_id() == "agent-42"
    unbind(token)
    assert current_agent_id() == ""


def test_nested_bind_restores_outer():
    outer = bind_trace("outer")
    inner = bind_trace("inner")
    assert current_trace_id() == "inner"
    unbind(inner)
    assert current_trace_id() == "outer"
    unbind(outer)
    assert current_trace_id() == ""


# ---------------------------------------------------------------------------
# setup_json_logging
# ---------------------------------------------------------------------------


def test_setup_json_logging_installs_handler():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    try:
        setup_json_logging(logging.WARNING)
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.WARNING
    finally:
        root.handlers = original_handlers
