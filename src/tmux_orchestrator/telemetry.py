"""OpenTelemetry GenAI Semantic Conventions telemetry module.

Provides span helpers aligned with the OpenTelemetry GenAI Semantic Conventions
(https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/).

Key exports:
- TelemetrySetup       — wraps TracerProvider + exporter; factory from env vars
- agent_span()         — context manager for an agent invocation span
- task_queued_span()   — context manager for a task-queued span
- get_tracer()         — returns a Tracer from setup, or a no-op tracer if setup is None

Design references:
- OpenTelemetry GenAI Semantic Conventions
  https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/
- opentelemetry.sdk.trace (Python SDK docs)
- DESIGN.md §10.14 (v0.47.0)
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
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

_PROMPT_MAX_LEN = 1000
_SERVICE_NAME = "tmux_orchestrator"
_TRACER_NAME = "tmux_orchestrator"


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
    """

    def __init__(
        self,
        *,
        tracer_provider: TracerProvider,
        exporter: ConsoleSpanExporter | InMemorySpanExporter | object,
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

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, *, service_name: str = _SERVICE_NAME) -> "TelemetrySetup":
        """Build a TelemetrySetup from environment variables.

        If ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, use an OTLP/gRPC exporter.
        Otherwise fall back to a ConsoleSpanExporter (writes JSON to stdout).

        The returned TracerProvider is *not* registered globally — callers must
        pass the ``TelemetrySetup`` object explicitly to each span helper.
        """
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()

        if endpoint:
            # Lazy import so the module works even without the gRPC extras
            # installed when no endpoint is configured.
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

                exporter: object = OTLPSpanExporter(endpoint=endpoint)
            except ImportError:  # pragma: no cover
                exporter = ConsoleSpanExporter()
        else:
            exporter = ConsoleSpanExporter()

        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))  # type: ignore[arg-type]
        return cls(tracer_provider=provider, exporter=exporter)


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
) -> Generator[trace.Span, None, None]:
    """Context manager that wraps an agent invocation in an OTel span.

    Sets the following attributes on the span:

    ==================== ======================== ==========================
    Attribute key        Value                    Source
    ==================== ======================== ==========================
    gen_ai.agent.id      *agent_id*               GEN_AI_AGENT_ID semconv
    gen_ai.agent.name    *agent_name*             GEN_AI_AGENT_NAME semconv
    gen_ai.system        ``"claude"``             GEN_AI_SYSTEM semconv
    gen_ai.operation.name``"invoke_agent"``       GEN_AI_OPERATION_NAME semconv
    tmux.task.id         *task_id*                custom
    tmux.task.prompt     *prompt* (≤1000 chars)   custom
    ==================== ======================== ==========================

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
