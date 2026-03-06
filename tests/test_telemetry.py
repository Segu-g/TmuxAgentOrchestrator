"""Tests for OpenTelemetry GenAI Semantic Conventions telemetry module.

Design references:
- OpenTelemetry GenAI Semantic Conventions
  https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/
- opentelemetry.sdk.trace (Python docs)
- DESIGN.md §10.14 (v0.47.0)
"""

from __future__ import annotations

import time

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor


from tmux_orchestrator.telemetry import (
    TelemetrySetup,
    agent_span,
    task_queued_span,
    get_tracer,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def exporter():
    """An in-memory span exporter for test assertions."""
    return InMemorySpanExporter()


@pytest.fixture
def tracer_provider(exporter):
    """A TracerProvider that uses the in-memory exporter."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


@pytest.fixture
def setup(tracer_provider, exporter):
    """Return a TelemetrySetup backed by in-memory exporter."""
    return TelemetrySetup(tracer_provider=tracer_provider, exporter=exporter)


# ---------------------------------------------------------------------------
# TelemetrySetup
# ---------------------------------------------------------------------------


def test_telemetry_setup_has_tracer(setup):
    """TelemetrySetup provides a get_tracer() method."""
    tracer = setup.get_tracer()
    assert tracer is not None


def test_telemetry_setup_can_create_span(setup, exporter):
    """TelemetrySetup creates spans that are exported."""
    tracer = setup.get_tracer()
    with tracer.start_as_current_span("test-span") as span:
        span.set_attribute("test.key", "value")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "test-span"


# ---------------------------------------------------------------------------
# agent_span() — GenAI agent invocation span
# ---------------------------------------------------------------------------


def test_agent_span_creates_span(setup, exporter):
    """agent_span() creates an 'invoke_agent' span with GenAI attributes."""
    with agent_span(
        setup=setup,
        agent_id="worker-1",
        agent_name="worker-1",
        task_id="task-abc",
        prompt="Write a hello world program",
    ):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert "invoke_agent" in span.name


def test_agent_span_sets_gen_ai_agent_id(setup, exporter):
    """agent_span() sets gen_ai.agent.id attribute."""
    from opentelemetry.semconv._incubating.attributes.gen_ai_attributes import GEN_AI_AGENT_ID
    with agent_span(
        setup=setup,
        agent_id="worker-2",
        agent_name="worker-2",
        task_id="task-xyz",
        prompt="test",
    ):
        pass

    spans = exporter.get_finished_spans()
    assert spans[0].attributes.get(GEN_AI_AGENT_ID) == "worker-2"


def test_agent_span_sets_gen_ai_agent_name(setup, exporter):
    """agent_span() sets gen_ai.agent.name attribute."""
    from opentelemetry.semconv._incubating.attributes.gen_ai_attributes import GEN_AI_AGENT_NAME
    with agent_span(
        setup=setup,
        agent_id="agent-1",
        agent_name="my-agent",
        task_id="t1",
        prompt="test",
    ):
        pass

    spans = exporter.get_finished_spans()
    assert spans[0].attributes.get(GEN_AI_AGENT_NAME) == "my-agent"


def test_agent_span_sets_gen_ai_system(setup, exporter):
    """agent_span() sets gen_ai.system to 'claude'."""
    from opentelemetry.semconv._incubating.attributes.gen_ai_attributes import GEN_AI_SYSTEM
    with agent_span(
        setup=setup,
        agent_id="a1",
        agent_name="a1",
        task_id="t1",
        prompt="test",
    ):
        pass

    spans = exporter.get_finished_spans()
    assert spans[0].attributes.get(GEN_AI_SYSTEM) == "claude"


def test_agent_span_sets_task_id(setup, exporter):
    """agent_span() sets tmux.task.id custom attribute."""
    with agent_span(
        setup=setup,
        agent_id="a1",
        agent_name="a1",
        task_id="my-task-001",
        prompt="test",
    ):
        pass

    spans = exporter.get_finished_spans()
    assert spans[0].attributes.get("tmux.task.id") == "my-task-001"


def test_agent_span_sets_operation_name(setup, exporter):
    """agent_span() sets gen_ai.operation.name to 'invoke_agent'."""
    from opentelemetry.semconv._incubating.attributes.gen_ai_attributes import GEN_AI_OPERATION_NAME
    with agent_span(
        setup=setup,
        agent_id="a1",
        agent_name="a1",
        task_id="t1",
        prompt="test",
    ):
        pass

    spans = exporter.get_finished_spans()
    assert spans[0].attributes.get(GEN_AI_OPERATION_NAME) == "invoke_agent"


def test_agent_span_records_exception(setup, exporter):
    """agent_span() records an exception when one is raised inside it."""
    from opentelemetry.trace import StatusCode

    with pytest.raises(ValueError):
        with agent_span(
            setup=setup,
            agent_id="a1",
            agent_name="a1",
            task_id="t1",
            prompt="test",
        ):
            raise ValueError("agent error")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR


def test_agent_span_with_prompt_attribute(setup, exporter):
    """agent_span() stores the prompt (truncated to 1000 chars) as attribute."""
    long_prompt = "x" * 2000
    with agent_span(
        setup=setup,
        agent_id="a1",
        agent_name="a1",
        task_id="t1",
        prompt=long_prompt,
    ):
        pass

    spans = exporter.get_finished_spans()
    prompt_attr = spans[0].attributes.get("tmux.task.prompt", "")
    assert len(prompt_attr) <= 1000


def test_agent_span_span_duration(setup, exporter):
    """agent_span() records a non-zero duration."""
    with agent_span(
        setup=setup,
        agent_id="a1",
        agent_name="a1",
        task_id="t1",
        prompt="test",
    ):
        time.sleep(0.01)

    spans = exporter.get_finished_spans()
    span = spans[0]
    duration_ns = span.end_time - span.start_time
    assert duration_ns > 0


# ---------------------------------------------------------------------------
# task_queued_span() — task submission span
# ---------------------------------------------------------------------------


def test_task_queued_span_creates_span(setup, exporter):
    """task_queued_span() creates a span for task submission."""
    with task_queued_span(
        setup=setup,
        task_id="t-queue-001",
        prompt="hello world",
        priority=0,
    ):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert "task_queued" in spans[0].name or "queue" in spans[0].name.lower()


def test_task_queued_span_sets_task_id(setup, exporter):
    """task_queued_span() records the task_id."""
    with task_queued_span(
        setup=setup,
        task_id="unique-task-id",
        prompt="test",
        priority=5,
    ):
        pass

    spans = exporter.get_finished_spans()
    assert spans[0].attributes.get("tmux.task.id") == "unique-task-id"


def test_task_queued_span_sets_priority(setup, exporter):
    """task_queued_span() records the task priority."""
    with task_queued_span(
        setup=setup,
        task_id="t1",
        prompt="test",
        priority=3,
    ):
        pass

    spans = exporter.get_finished_spans()
    assert spans[0].attributes.get("tmux.task.priority") == 3


# ---------------------------------------------------------------------------
# module-level get_tracer()
# ---------------------------------------------------------------------------


def test_get_tracer_returns_tracer_when_setup_initialized():
    """get_tracer(setup) returns a valid Tracer from the provided setup."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    s = TelemetrySetup(tracer_provider=provider, exporter=exporter)
    tracer = get_tracer(s)
    assert tracer is not None


def test_get_tracer_returns_noop_when_no_setup():
    """get_tracer(None) returns a no-op tracer that doesn't raise."""
    tracer = get_tracer(None)
    # Should not raise — creates no-op spans
    with tracer.start_as_current_span("noop-span"):
        pass


# ---------------------------------------------------------------------------
# TelemetrySetup.from_env() — factory from environment variables
# ---------------------------------------------------------------------------


def test_from_env_no_endpoint_returns_setup(monkeypatch):
    """TelemetrySetup.from_env() with no OTLP endpoint uses ConsoleSpanExporter."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    setup = TelemetrySetup.from_env(service_name="test-service")
    assert setup is not None
    tracer = setup.get_tracer()
    assert tracer is not None


def test_from_env_with_endpoint_env_var(monkeypatch):
    """TelemetrySetup.from_env() reads OTEL_EXPORTER_OTLP_ENDPOINT."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    # Should not raise even if no collector is running — we just configure it
    # The actual connection only happens on export
    setup = TelemetrySetup.from_env(service_name="test-service")
    assert setup is not None
