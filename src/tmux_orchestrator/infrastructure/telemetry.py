"""OpenTelemetry GenAI Semantic Conventions telemetry module.

Provides span helpers aligned with the OpenTelemetry GenAI Semantic Conventions
(https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/).

Key exports:
- TelemetrySetup         — wraps TracerProvider + exporter; factory from env vars
- agent_span()           — context manager for an agent invocation span
- task_queued_span()     — context manager for a task-queued span
- workflow_span()        — context manager for a workflow-level span
- get_tracer()           — returns a Tracer from setup, or a no-op tracer if setup is None
- RingBufferSpanExporter — fixed-capacity ring buffer for GET /telemetry/spans

Design references:
- OpenTelemetry GenAI Semantic Conventions
  https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/
- OpenTelemetry GenAI attributes registry
  https://opentelemetry.io/docs/specs/semconv/attributes-registry/gen-ai/
- OpenTelemetry AI Agent Observability (2025)
  https://opentelemetry.io/blog/2025/ai-agent-observability/
- DESIGN.md §10.14 (v0.47.0) — initial implementation
- DESIGN.md §10.20 (v1.1.10) — workflow_span, RingBufferSpanExporter,
  BatchSpanProcessor, gen_ai.agent.description/version, OTel→structlog propagation
"""

from __future__ import annotations

import collections
import os
import threading
from contextlib import contextmanager
from typing import Any, Generator

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode, Tracer

# GenAI semantic convention attribute keys (incubating namespace).
# Imported lazily inside helpers so that attribute names stay in one place.
try:
    from opentelemetry.semconv._incubating.attributes.gen_ai_attributes import (
        GEN_AI_AGENT_ID,
        GEN_AI_AGENT_NAME,
        GEN_AI_OPERATION_NAME,
        GEN_AI_SYSTEM,
    )
except ImportError:  # pragma: no cover — fallback if semconv package changes layout
    GEN_AI_AGENT_ID = "gen_ai.agent.id"
    GEN_AI_AGENT_NAME = "gen_ai.agent.name"
    GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
    GEN_AI_SYSTEM = "gen_ai.system"

# Additional GenAI agent attributes (development stability).
# Refs: https://opentelemetry.io/docs/specs/semconv/attributes-registry/gen-ai/
GEN_AI_AGENT_DESCRIPTION = "gen_ai.agent.description"
GEN_AI_AGENT_VERSION = "gen_ai.agent.version"

_PROMPT_MAX_LEN = 1000
_SERVICE_NAME = "tmux_orchestrator"
_TRACER_NAME = "tmux_orchestrator"
_DEFAULT_RING_BUFFER_SIZE = 200


# ---------------------------------------------------------------------------
# RingBufferSpanExporter — fixed-capacity span ring buffer
# ---------------------------------------------------------------------------


class RingBufferSpanExporter(SpanExporter):
    """Thread-safe ring-buffer SpanExporter for recent-spans REST exposure.

    Stores up to *maxsize* completed spans as JSON-serializable dicts.
    When the buffer is full, the oldest span is evicted (FIFO).

    Usage::

        buf = RingBufferSpanExporter(maxsize=200)
        provider.add_span_processor(SimpleSpanProcessor(buf))
        spans = buf.get_spans()          # list of dicts, newest-last
        buf.clear()                      # wipe all stored spans
    """

    def __init__(self, maxsize: int = _DEFAULT_RING_BUFFER_SIZE) -> None:
        self._maxsize = maxsize
        self._buffer: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=maxsize
        )
        self._lock = threading.Lock()

    @property
    def maxsize(self) -> int:
        return self._maxsize

    def export(self, spans: Any) -> SpanExportResult:
        """Convert each ReadableSpan to a dict and append to the ring buffer."""
        with self._lock:
            for span in spans:
                self._buffer.append(self._to_dict(span))
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:  # pragma: no cover
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # pragma: no cover
        return True

    def get_spans(self) -> list[dict[str, Any]]:
        """Return a snapshot of all stored spans as a list of dicts (oldest-first)."""
        with self._lock:
            return list(self._buffer)

    def clear(self) -> None:
        """Remove all stored spans."""
        with self._lock:
            self._buffer.clear()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_dict(span: ReadableSpan) -> dict[str, Any]:
        """Convert a ReadableSpan to a JSON-serializable dict."""
        ctx = span.get_span_context()
        return {
            "name": span.name,
            "trace_id": format(ctx.trace_id, "032x") if ctx else "",
            "span_id": format(ctx.span_id, "016x") if ctx else "",
            "parent_id": (
                format(span.parent.span_id, "016x")
                if span.parent
                else None
            ),
            "start_time": span.start_time,
            "end_time": span.end_time,
            "status": span.status.status_code.name if span.status else "UNSET",
            "attributes": dict(span.attributes) if span.attributes else {},
        }


# ---------------------------------------------------------------------------
# TelemetrySetup
# ---------------------------------------------------------------------------


class TelemetrySetup:
    """Wraps a TracerProvider and its exporter for dependency injection in tests.

    Usage (production)::

        setup = TelemetrySetup.from_env(service_name="my-service")
        with agent_span(setup=setup, agent_id="w1", agent_name="w1",
                        task_id="t1", prompt="hello"):
            ...

    Usage (tests)::

        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        setup = TelemetrySetup(tracer_provider=provider, exporter=exporter)

    Usage (ring buffer for GET /telemetry/spans)::

        buf = RingBufferSpanExporter(maxsize=200)
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(buf))
        setup = TelemetrySetup(tracer_provider=provider, exporter=buf)
        spans = setup.ring_buffer_exporter.get_spans()
    """

    def __init__(
        self,
        *,
        tracer_provider: TracerProvider,
        exporter: ConsoleSpanExporter | InMemorySpanExporter | RingBufferSpanExporter | object,
    ) -> None:
        self._provider = tracer_provider
        self._exporter = exporter

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_tracer(self) -> Tracer:
        """Return a Tracer backed by this setup's TracerProvider."""
        return self._provider.get_tracer(_TRACER_NAME)

    @property
    def tracer_provider(self) -> TracerProvider:
        return self._provider

    @property
    def exporter(self):
        return self._exporter

    @property
    def ring_buffer_exporter(self) -> RingBufferSpanExporter | None:
        """Return the RingBufferSpanExporter if the primary exporter is one; else None."""
        if isinstance(self._exporter, RingBufferSpanExporter):
            return self._exporter
        return None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, *, service_name: str = _SERVICE_NAME) -> "TelemetrySetup":
        """Build a TelemetrySetup from environment variables.

        Resolution order:
        1. If ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, use an OTLP/gRPC exporter
           with a ``BatchSpanProcessor`` (background async export for production).
        2. Otherwise use a ``RingBufferSpanExporter`` (ConsoleSpanExporter as
           secondary) with a ``SimpleSpanProcessor`` so that spans are immediately
           visible via ``GET /telemetry/spans`` even without a collector.

        The returned TracerProvider is *not* registered globally — callers must
        pass the ``TelemetrySetup`` object explicitly to each span helper.
        """
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()

        provider = TracerProvider()

        if endpoint:
            # Production: OTLP gRPC with BatchSpanProcessor for high throughput.
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

                otlp_exporter: object = OTLPSpanExporter(endpoint=endpoint)
                provider.add_span_processor(BatchSpanProcessor(otlp_exporter))  # type: ignore[arg-type]
                primary_exporter: object = otlp_exporter
            except ImportError:  # pragma: no cover
                primary_exporter = ConsoleSpanExporter()
                provider.add_span_processor(SimpleSpanProcessor(primary_exporter))  # type: ignore[arg-type]
        else:
            # Development/testing: RingBuffer so spans are retrievable via REST.
            ring_buf = RingBufferSpanExporter(maxsize=_DEFAULT_RING_BUFFER_SIZE)
            provider.add_span_processor(SimpleSpanProcessor(ring_buf))
            primary_exporter = ring_buf

        return cls(tracer_provider=provider, exporter=primary_exporter)


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------


def get_tracer(setup: TelemetrySetup | None) -> Tracer:
    """Return a Tracer from *setup*, or a no-op Tracer when *setup* is None.

    The no-op Tracer is the OTel SDK's built-in proxy tracer backed by a
    no-op provider — it creates spans that do nothing and never raise.
    """
    if setup is None:
        return trace.get_tracer(_TRACER_NAME)
    return setup.get_tracer()


# ---------------------------------------------------------------------------
# Span helpers
# ---------------------------------------------------------------------------


@contextmanager
def agent_span(
    *,
    setup: TelemetrySetup | None,
    agent_id: str,
    agent_name: str,
    task_id: str,
    prompt: str,
    description: str | None = None,
    version: str | None = None,
) -> Generator[trace.Span, None, None]:
    """Context manager that wraps an agent invocation in an OTel span.

    Sets the following attributes on the span:

    ========================= ======================== ==========================
    Attribute key             Value                    Source
    ========================= ======================== ==========================
    gen_ai.agent.id           *agent_id*               GEN_AI_AGENT_ID semconv
    gen_ai.agent.name         *agent_name*             GEN_AI_AGENT_NAME semconv
    gen_ai.agent.description  *description* (opt)      gen_ai.agent.description
    gen_ai.agent.version      *version* (opt)          gen_ai.agent.version
    gen_ai.system             ``"claude"``             GEN_AI_SYSTEM semconv
    gen_ai.operation.name     ``"invoke_agent"``       GEN_AI_OPERATION_NAME semconv
    tmux.task.id              *task_id*                custom
    tmux.task.prompt          *prompt* (≤1000 chars)   custom
    ========================= ======================== ==========================

    On exception the span status is set to ``ERROR`` and the exception is
    recorded before being re-raised.
    """
    tracer = get_tracer(setup)
    with tracer.start_as_current_span("invoke_agent") as span:
        span.set_attribute(GEN_AI_AGENT_ID, agent_id)
        span.set_attribute(GEN_AI_AGENT_NAME, agent_name)
        span.set_attribute(GEN_AI_SYSTEM, "claude")
        span.set_attribute(GEN_AI_OPERATION_NAME, "invoke_agent")
        span.set_attribute("tmux.task.id", task_id)
        span.set_attribute("tmux.task.prompt", prompt[:_PROMPT_MAX_LEN])
        if description is not None:
            span.set_attribute(GEN_AI_AGENT_DESCRIPTION, description)
        if version is not None:
            span.set_attribute(GEN_AI_AGENT_VERSION, version)
        try:
            yield span
        except Exception as exc:
            span.set_status(StatusCode.ERROR, str(exc))
            span.record_exception(exc)
            raise


@contextmanager
def task_queued_span(
    *,
    setup: TelemetrySetup | None,
    task_id: str,
    prompt: str,
    priority: int,
) -> Generator[trace.Span, None, None]:
    """Context manager that records a task-queued span.

    Sets the following attributes:

    ======================== ======================== ==========================
    Attribute key            Value                    Source
    ======================== ======================== ==========================
    tmux.task.id             *task_id*                custom
    tmux.task.priority       *priority*               custom
    tmux.task.prompt         *prompt* (≤1000 chars)   custom
    ======================== ======================== ==========================
    """
    tracer = get_tracer(setup)
    with tracer.start_as_current_span("task_queued") as span:
        span.set_attribute("tmux.task.id", task_id)
        span.set_attribute("tmux.task.priority", priority)
        span.set_attribute("tmux.task.prompt", prompt[:_PROMPT_MAX_LEN])
        try:
            yield span
        except Exception as exc:
            span.set_status(StatusCode.ERROR, str(exc))
            span.record_exception(exc)
            raise


@contextmanager
def workflow_span(
    *,
    setup: TelemetrySetup | None,
    workflow_id: str,
    workflow_type: str,
    phase: str | None = None,
) -> Generator[trace.Span, None, None]:
    """Context manager that wraps a workflow invocation in an OTel span.

    Workflow spans use ``gen_ai.operation.name = "invoke_agent"`` (the closest
    standard GenAI operation for agent orchestration) and carry custom
    ``tmux.workflow.*`` attributes for TmuxAgentOrchestrator-specific metadata.

    Sets the following attributes on the span:

    ======================== ======================== ==========================
    Attribute key            Value                    Source
    ======================== ======================== ==========================
    gen_ai.operation.name    ``"invoke_agent"``       GEN_AI_OPERATION_NAME semconv
    tmux.workflow.id         *workflow_id*            custom
    tmux.workflow.type       *workflow_type*          custom
    tmux.workflow.phase      *phase* (optional)       custom
    ======================== ======================== ==========================

    On exception the span status is set to ``ERROR`` and the exception is
    recorded before being re-raised.

    Reference: OTel GenAI Semantic Conventions gen-ai-spans
               https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/
    """
    tracer = get_tracer(setup)
    with tracer.start_as_current_span("invoke_workflow") as span:
        span.set_attribute(GEN_AI_OPERATION_NAME, "invoke_agent")
        span.set_attribute("tmux.workflow.id", workflow_id)
        span.set_attribute("tmux.workflow.type", workflow_type)
        if phase is not None:
            span.set_attribute("tmux.workflow.phase", phase)
        try:
            yield span
        except Exception as exc:
            span.set_status(StatusCode.ERROR, str(exc))
            span.record_exception(exc)
            raise
