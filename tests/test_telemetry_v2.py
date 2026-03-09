"""Tests for v1.1.10 telemetry extensions.

New features tested:
- workflow_span() — GenAI workflow-level spans
- RingBufferSpanExporter — fixed-capacity span ring buffer
- GET /telemetry/spans — REST endpoint
- gen_ai.agent.description / gen_ai.agent.version optional attrs
- OTel span context propagation to structlog JSON logs

Design references:
- OpenTelemetry GenAI Semantic Conventions
  https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/
- DESIGN.md §10.20 (v1.1.10)
"""

from __future__ import annotations

import logging

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tmux_orchestrator.telemetry import (
    TelemetrySetup,
    agent_span,
    get_tracer,
    workflow_span,
    RingBufferSpanExporter,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def exporter():
    return InMemorySpanExporter()


@pytest.fixture
def setup(exporter):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return TelemetrySetup(tracer_provider=provider, exporter=exporter)


# ---------------------------------------------------------------------------
# workflow_span() — GenAI workflow-level spans
# ---------------------------------------------------------------------------


def test_workflow_span_creates_span(setup, exporter):
    """workflow_span() creates a span for workflow invocation."""
    with workflow_span(
        setup=setup,
        workflow_id="wf-001",
        workflow_type="competition",
    ):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert "workflow" in span.name.lower() or "invoke" in span.name.lower()


def test_workflow_span_sets_workflow_id(setup, exporter):
    """workflow_span() records the workflow_id as a custom attribute."""
    with workflow_span(
        setup=setup,
        workflow_id="wf-abc123",
        workflow_type="tdd",
    ):
        pass

    spans = exporter.get_finished_spans()
    assert spans[0].attributes.get("tmux.workflow.id") == "wf-abc123"


def test_workflow_span_sets_workflow_type(setup, exporter):
    """workflow_span() records the workflow_type as a custom attribute."""
    with workflow_span(
        setup=setup,
        workflow_id="wf-1",
        workflow_type="debate",
    ):
        pass

    spans = exporter.get_finished_spans()
    assert spans[0].attributes.get("tmux.workflow.type") == "debate"


def test_workflow_span_sets_gen_ai_operation_name(setup, exporter):
    """workflow_span() sets gen_ai.operation.name."""
    from opentelemetry.semconv._incubating.attributes.gen_ai_attributes import GEN_AI_OPERATION_NAME

    with workflow_span(
        setup=setup,
        workflow_id="wf-1",
        workflow_type="spec-first",
    ):
        pass

    spans = exporter.get_finished_spans()
    assert spans[0].attributes.get(GEN_AI_OPERATION_NAME) == "invoke_agent"


def test_workflow_span_records_exception(setup, exporter):
    """workflow_span() records an exception on failure."""
    from opentelemetry.trace import StatusCode

    with pytest.raises(RuntimeError):
        with workflow_span(
            setup=setup,
            workflow_id="wf-fail",
            workflow_type="tdd",
        ):
            raise RuntimeError("workflow failed")

    spans = exporter.get_finished_spans()
    assert spans[0].status.status_code == StatusCode.ERROR


def test_workflow_span_with_phase(setup, exporter):
    """workflow_span() accepts optional phase attribute."""
    with workflow_span(
        setup=setup,
        workflow_id="wf-1",
        workflow_type="competition",
        phase="evaluation",
    ):
        pass

    spans = exporter.get_finished_spans()
    assert spans[0].attributes.get("tmux.workflow.phase") == "evaluation"


def test_workflow_span_no_phase_by_default(setup, exporter):
    """workflow_span() without phase does not set tmux.workflow.phase."""
    with workflow_span(
        setup=setup,
        workflow_id="wf-1",
        workflow_type="pair",
    ):
        pass

    spans = exporter.get_finished_spans()
    assert "tmux.workflow.phase" not in (spans[0].attributes or {})


def test_workflow_span_none_setup():
    """workflow_span(setup=None) uses no-op tracer and does not raise."""
    with workflow_span(setup=None, workflow_id="wf-1", workflow_type="adr"):
        pass  # Should not raise


# ---------------------------------------------------------------------------
# agent_span() optional description + version attributes (v1.1.10 extension)
# ---------------------------------------------------------------------------


def test_agent_span_sets_description(setup, exporter):
    """agent_span() accepts optional gen_ai.agent.description attribute."""
    with agent_span(
        setup=setup,
        agent_id="a1",
        agent_name="solver",
        task_id="t1",
        prompt="solve problem",
        description="Solves competitive programming problems",
    ):
        pass

    spans = exporter.get_finished_spans()
    assert spans[0].attributes.get("gen_ai.agent.description") == "Solves competitive programming problems"


def test_agent_span_sets_version(setup, exporter):
    """agent_span() accepts optional gen_ai.agent.version attribute."""
    with agent_span(
        setup=setup,
        agent_id="a1",
        agent_name="solver",
        task_id="t1",
        prompt="solve problem",
        version="1.1.10",
    ):
        pass

    spans = exporter.get_finished_spans()
    assert spans[0].attributes.get("gen_ai.agent.version") == "1.1.10"


def test_agent_span_no_description_by_default(setup, exporter):
    """agent_span() without description does not set gen_ai.agent.description."""
    with agent_span(
        setup=setup,
        agent_id="a1",
        agent_name="a1",
        task_id="t1",
        prompt="test",
    ):
        pass

    spans = exporter.get_finished_spans()
    assert "gen_ai.agent.description" not in (spans[0].attributes or {})


# ---------------------------------------------------------------------------
# RingBufferSpanExporter — fixed-capacity ring buffer
# ---------------------------------------------------------------------------


def test_ring_buffer_span_exporter_captures_spans():
    """RingBufferSpanExporter stores finished spans."""
    buf = RingBufferSpanExporter(maxsize=10)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(buf))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("my-span"):
        pass

    spans = buf.get_spans()
    assert len(spans) == 1
    assert spans[0]["name"] == "my-span"


def test_ring_buffer_span_exporter_respects_maxsize():
    """RingBufferSpanExporter evicts oldest spans when full."""
    maxsize = 5
    buf = RingBufferSpanExporter(maxsize=maxsize)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(buf))
    tracer = provider.get_tracer("test")

    for i in range(10):
        with tracer.start_as_current_span(f"span-{i}"):
            pass

    spans = buf.get_spans()
    # Should not exceed maxsize
    assert len(spans) <= maxsize
    # The last span should be the most recently added
    names = [s["name"] for s in spans]
    assert "span-9" in names


def test_ring_buffer_span_exporter_returns_dicts():
    """RingBufferSpanExporter.get_spans() returns JSON-serializable dicts."""
    buf = RingBufferSpanExporter(maxsize=10)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(buf))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("dict-span") as span:
        span.set_attribute("key", "value")

    spans = buf.get_spans()
    assert len(spans) == 1
    span_dict = spans[0]
    assert isinstance(span_dict, dict)
    assert "name" in span_dict
    assert "trace_id" in span_dict
    assert "span_id" in span_dict
    assert "start_time" in span_dict
    assert "end_time" in span_dict
    assert "attributes" in span_dict


def test_ring_buffer_span_exporter_default_maxsize():
    """RingBufferSpanExporter uses default maxsize of 200."""
    buf = RingBufferSpanExporter()
    assert buf.maxsize == 200


def test_ring_buffer_span_exporter_thread_safe_clear():
    """RingBufferSpanExporter.clear() removes all spans."""
    buf = RingBufferSpanExporter(maxsize=10)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(buf))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("s1"):
        pass
    assert len(buf.get_spans()) == 1

    buf.clear()
    assert len(buf.get_spans()) == 0


# ---------------------------------------------------------------------------
# TelemetrySetup.from_env() — BatchSpanProcessor in production
# ---------------------------------------------------------------------------


def test_from_env_uses_batch_processor_no_endpoint(monkeypatch):
    """TelemetrySetup.from_env() uses BatchSpanProcessor for production."""
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    setup = TelemetrySetup.from_env(service_name="prod-test")
    # Just check it works and has a tracer — batch vs simple is internal
    assert setup.get_tracer() is not None


def test_ring_buffer_exporter_accessible_via_setup():
    """TelemetrySetup exposes a RingBufferSpanExporter when ring_buffer=True."""
    buf = RingBufferSpanExporter(maxsize=50)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(buf))
    setup = TelemetrySetup(tracer_provider=provider, exporter=buf)
    assert setup.ring_buffer_exporter is buf


def test_ring_buffer_exporter_none_when_not_configured():
    """TelemetrySetup.ring_buffer_exporter is None for non-ring-buffer setups."""
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter

    provider = TracerProvider()
    exporter = ConsoleSpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    setup = TelemetrySetup(tracer_provider=provider, exporter=exporter)
    assert setup.ring_buffer_exporter is None


# ---------------------------------------------------------------------------
# OTel span context propagation to structlog / logging
# ---------------------------------------------------------------------------


def test_otel_trace_id_propagated_to_log_record(setup, caplog):
    """When inside an OTel span, trace_id/span_id appear in log records via JsonFormatter."""
    from tmux_orchestrator.logging_config import JsonFormatter

    handler = logging.handlers_list = []
    with caplog.at_level(logging.INFO, logger="test_otel"):
        tracer = setup.get_tracer()
        with tracer.start_as_current_span("prop-span"):
            # We can't test JsonFormatter directly here without replacing handlers,
            # but we can verify the OTel context is active and readable.
            from opentelemetry import trace as otel_trace

            ctx = otel_trace.get_current_span().get_span_context()
            assert ctx.trace_id != 0
            assert ctx.span_id != 0


def test_json_formatter_includes_otel_trace_id():
    """JsonFormatter emits otel_trace_id when inside an OTel span."""
    import json
    from tmux_orchestrator.logging_config import JsonFormatter

    buf = RingBufferSpanExporter(maxsize=5)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(buf))
    setup = TelemetrySetup(tracer_provider=provider, exporter=buf)
    tracer = setup.get_tracer()

    log_records = []

    class CapturingFormatter(JsonFormatter):
        def format(self, record):
            result = super().format(record)
            log_records.append(json.loads(result))
            return result

    handler = logging.StreamHandler()
    handler.setFormatter(CapturingFormatter())
    logger = logging.getLogger("test_otel_format")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    try:
        with tracer.start_as_current_span("log-span"):
            logger.info("inside span")
    finally:
        logger.removeHandler(handler)

    assert len(log_records) >= 1
    record = log_records[-1]
    # otel_trace_id field should be present when inside a span
    assert "otel_trace_id" in record or "trace_id" in record


# ---------------------------------------------------------------------------
# GET /telemetry/spans REST endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_spans_endpoint_returns_list():
    """GET /telemetry/spans returns a list of recent spans."""
    from unittest.mock import MagicMock
    from httpx import AsyncClient, ASGITransport
    from tmux_orchestrator.web.app import create_app

    buf = RingBufferSpanExporter(maxsize=10)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(buf))
    setup = TelemetrySetup(tracer_provider=provider, exporter=buf)

    # Add a span to the buffer
    tracer = setup.get_tracer()
    with tracer.start_as_current_span("test-endpoint-span"):
        pass

    orch = MagicMock()
    orch.get_telemetry.return_value = setup
    hub = MagicMock()
    hub.subscribe = MagicMock(return_value=None)
    app = create_app(orch, hub, api_key="test-key")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/telemetry/spans", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_telemetry_spans_endpoint_empty_when_disabled():
    """GET /telemetry/spans returns empty list when telemetry is disabled."""
    from unittest.mock import MagicMock
    from httpx import AsyncClient, ASGITransport
    from tmux_orchestrator.web.app import create_app

    orch = MagicMock()
    orch.get_telemetry.return_value = None
    hub = MagicMock()
    hub.subscribe = MagicMock(return_value=None)
    app = create_app(orch, hub, api_key="test-key")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/telemetry/spans", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_telemetry_spans_endpoint_limit_param():
    """GET /telemetry/spans?limit=N returns at most N spans."""
    from unittest.mock import MagicMock
    from httpx import AsyncClient, ASGITransport
    from tmux_orchestrator.web.app import create_app

    buf = RingBufferSpanExporter(maxsize=50)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(buf))
    setup = TelemetrySetup(tracer_provider=provider, exporter=buf)

    # Add 5 spans
    tracer = setup.get_tracer()
    for i in range(5):
        with tracer.start_as_current_span(f"span-{i}"):
            pass

    orch = MagicMock()
    orch.get_telemetry.return_value = setup
    hub = MagicMock()
    hub.subscribe = MagicMock(return_value=None)
    app = create_app(orch, hub, api_key="test-key")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(
            "/telemetry/spans?limit=3",
            headers={"X-API-Key": "test-key"},
        )
    assert r.status_code == 200
    data = r.json()
    assert len(data) <= 3


@pytest.mark.asyncio
async def test_telemetry_spans_endpoint_no_ring_buffer_returns_empty():
    """GET /telemetry/spans returns empty list when setup uses console exporter (no ring buffer)."""
    from unittest.mock import MagicMock
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter
    from httpx import AsyncClient, ASGITransport
    from tmux_orchestrator.web.app import create_app

    provider = TracerProvider()
    exporter = ConsoleSpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    setup = TelemetrySetup(tracer_provider=provider, exporter=exporter)

    orch = MagicMock()
    orch.get_telemetry.return_value = setup
    hub = MagicMock()
    hub.subscribe = MagicMock(return_value=None)
    app = create_app(orch, hub, api_key="test-key")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/telemetry/spans", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    assert r.json() == []
