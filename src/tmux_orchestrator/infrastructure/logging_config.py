"""Structured JSON logging with per-task trace_id and agent_id context.

Uses Python's ``contextvars`` module so that any coroutine running within a
task dispatch context automatically includes ``trace_id`` / ``agent_id`` in
every log record — without passing them as explicit parameters.

Usage
-----
Call ``setup_json_logging(level)`` once at startup to replace the default
plaintext formatter with ``JsonFormatter``.  Then set context with the helpers
before entering async task dispatch::

    token = bind_trace(task.trace_id)
    token2 = bind_agent("worker-1")
    try:
        await _dispatch_task(task)
    finally:
        unbind(token)
        unbind(token2)

Log records produced anywhere in that async call tree will automatically
include ``trace_id`` and ``agent_id`` fields.

Reference: Kleppmann "Designing Data-Intensive Applications" (2017) Ch. 11
           (distributed tracing); SRE Book (Beyer et al. 2016) Ch. 16
           (structured/machine-readable logging); DESIGN.md §10.5 (2026-03-05).
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Context variables — one per piece of per-request context
# ---------------------------------------------------------------------------

_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")
_agent_id: contextvars.ContextVar[str] = contextvars.ContextVar("agent_id", default="")


def bind_trace(trace_id: str) -> contextvars.Token[str]:
    """Set ``trace_id`` for the current async context; returns a reset token."""
    return _trace_id.set(trace_id)


def bind_agent(agent_id: str) -> contextvars.Token[str]:
    """Set ``agent_id`` for the current async context; returns a reset token."""
    return _agent_id.set(agent_id)


def unbind(token: contextvars.Token) -> None:
    """Reset a context variable to its previous value."""
    token.var.reset(token)


def current_trace_id() -> str:
    """Return the current trace_id, or empty string if not set."""
    return _trace_id.get()


def current_agent_id() -> str:
    """Return the current agent_id, or empty string if not set."""
    return _agent_id.get()


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object.

    Fields always present: ``ts``, ``level``, ``logger``, ``msg``.
    Optional: ``trace_id``, ``agent_id`` (from context vars), ``exc`` (on exception).
    """

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if tid := _trace_id.get():
            obj["trace_id"] = tid
        if aid := _agent_id.get():
            obj["agent_id"] = aid
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        # Propagate OpenTelemetry span context into JSON logs when inside a span.
        # This correlates structured log records with OTel traces without requiring
        # a separate log exporter.  Fields: otel_trace_id, otel_span_id.
        # Reference: OTel logging spec (correlation via TraceId/SpanId fields).
        try:
            from opentelemetry import trace as _otel_trace  # lazy import

            span = _otel_trace.get_current_span()
            ctx = span.get_span_context()
            if ctx is not None and ctx.trace_id != 0:
                obj["otel_trace_id"] = format(ctx.trace_id, "032x")
                obj["otel_span_id"] = format(ctx.span_id, "016x")
        except Exception:  # pragma: no cover — OTel not installed or no span
            pass
        return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def setup_json_logging(level: int = logging.INFO) -> None:
    """Configure the root logger to emit structured JSON on stderr.

    Idempotent: clears existing handlers before installing the new one.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def setup_text_logging(level: int = logging.INFO) -> None:
    """Configure the root logger with the standard human-readable format."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
