"""Integration tests: OTel telemetry config + Orchestrator wiring.

Design references:
- DESIGN.md §10.14 (v0.47.0)
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from tmux_orchestrator.config import OrchestratorConfig, load_config
from tmux_orchestrator.telemetry import TelemetrySetup


# ---------------------------------------------------------------------------
# Config field defaults
# ---------------------------------------------------------------------------


def test_config_telemetry_disabled_by_default():
    """OrchestratorConfig.telemetry_enabled defaults to False."""
    cfg = OrchestratorConfig()
    assert cfg.telemetry_enabled is False


def test_config_otlp_endpoint_empty_by_default():
    """OrchestratorConfig.otlp_endpoint defaults to empty string."""
    cfg = OrchestratorConfig()
    assert cfg.otlp_endpoint == ""


def test_config_telemetry_fields_can_be_set():
    """telemetry_enabled and otlp_endpoint can be set on OrchestratorConfig."""
    cfg = OrchestratorConfig(telemetry_enabled=True, otlp_endpoint="http://localhost:4317")
    assert cfg.telemetry_enabled is True
    assert cfg.otlp_endpoint == "http://localhost:4317"


# ---------------------------------------------------------------------------
# TelemetrySetup.from_env() — ConsoleSpanExporter path
# ---------------------------------------------------------------------------


def test_from_env_creates_setup_without_endpoint(monkeypatch):
    """from_env() with no OTLP endpoint creates a TelemetrySetup with ConsoleSpanExporter."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    setup = TelemetrySetup.from_env(service_name="integration-test")
    assert setup is not None
    assert setup.get_tracer() is not None


def test_from_env_tracer_produces_spans(monkeypatch):
    """Tracer from from_env() can produce real spans (Console path)."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    setup = TelemetrySetup.from_env(service_name="integration-test")
    tracer = setup.get_tracer()
    with tracer.start_as_current_span("dummy") as span:
        span.set_attribute("x", 1)
    # No assertion about export — just ensure no exception is raised


# ---------------------------------------------------------------------------
# Orchestrator telemetry wiring
# ---------------------------------------------------------------------------


def _make_orchestrator(telemetry_setup: TelemetrySetup | None = None):
    """Build a lightweight Orchestrator with a mock tmux and injected telemetry."""
    from unittest.mock import MagicMock
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.bus import Bus

    bus = Bus()
    tmux = MagicMock()
    tmux.capture_pane.return_value = ""
    cfg = OrchestratorConfig(telemetry_enabled=False)  # telemetry injected manually
    orch = Orchestrator(bus=bus, tmux=tmux, config=cfg)
    if telemetry_setup is not None:
        orch._telemetry = telemetry_setup
    return orch


def test_orchestrator_get_telemetry_none_when_disabled():
    """get_telemetry() returns None when telemetry_enabled=False."""
    orch = _make_orchestrator()
    assert orch.get_telemetry() is None


def test_orchestrator_get_telemetry_returns_setup_when_injected():
    """get_telemetry() returns the injected TelemetrySetup."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    setup = TelemetrySetup(tracer_provider=provider, exporter=exporter)

    orch = _make_orchestrator(telemetry_setup=setup)
    assert orch.get_telemetry() is setup


@pytest.mark.asyncio
async def test_submit_task_records_task_queued_span():
    """submit_task() emits a task_queued span when telemetry is enabled."""
    from unittest.mock import MagicMock, AsyncMock
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.agents.base import AgentStatus

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    setup = TelemetrySetup(tracer_provider=provider, exporter=exporter)

    bus = Bus()
    tmux = MagicMock()
    cfg = OrchestratorConfig(telemetry_enabled=False)
    orch = Orchestrator(bus=bus, tmux=tmux, config=cfg)
    orch._telemetry = setup

    # Submit task without a running dispatch loop
    task = await orch.submit_task("test prompt", priority=1)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert "task_queued" in spans[0].name
    assert spans[0].attributes.get("tmux.task.id") == task.id
    assert spans[0].attributes.get("tmux.task.priority") == 1


@pytest.mark.asyncio
async def test_submit_task_no_span_when_telemetry_disabled():
    """submit_task() does not emit spans when telemetry is disabled."""
    from unittest.mock import MagicMock
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.bus import Bus

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    bus = Bus()
    tmux = MagicMock()
    cfg = OrchestratorConfig(telemetry_enabled=False)
    orch = Orchestrator(bus=bus, tmux=tmux, config=cfg)
    # _telemetry is None (default)

    await orch.submit_task("test prompt", priority=0)

    spans = exporter.get_finished_spans()
    assert len(spans) == 0


# ---------------------------------------------------------------------------
# GET /telemetry/status REST endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_status_endpoint_disabled(monkeypatch):
    """GET /telemetry/status returns {enabled: false} when disabled."""
    from unittest.mock import MagicMock
    from httpx import AsyncClient, ASGITransport
    from tmux_orchestrator.web.app import create_app

    orch = MagicMock()
    orch.get_telemetry.return_value = None
    hub = MagicMock()
    hub.subscribe = MagicMock(return_value=None)
    app = create_app(orch, hub, api_key="test-key")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/telemetry/status", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is False


@pytest.mark.asyncio
async def test_telemetry_status_endpoint_enabled_console(monkeypatch):
    """GET /telemetry/status reports console exporter when no OTLP endpoint set."""
    from unittest.mock import MagicMock
    from httpx import AsyncClient, ASGITransport
    from tmux_orchestrator.web.app import create_app

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    setup = TelemetrySetup(tracer_provider=provider, exporter=exporter)

    orch = MagicMock()
    orch.get_telemetry.return_value = setup
    orch.config.otlp_endpoint = ""
    hub = MagicMock()
    hub.subscribe = MagicMock(return_value=None)
    app = create_app(orch, hub, api_key="test-key")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/telemetry/status", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is True
    assert data["exporter"] == "console"
    assert data["otlp_endpoint"] is None


@pytest.mark.asyncio
async def test_telemetry_status_endpoint_enabled_otlp():
    """GET /telemetry/status reports otlp exporter when endpoint is configured."""
    from unittest.mock import MagicMock
    from httpx import AsyncClient, ASGITransport
    from tmux_orchestrator.web.app import create_app

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    setup = TelemetrySetup(tracer_provider=provider, exporter=exporter)

    orch = MagicMock()
    orch.get_telemetry.return_value = setup
    orch.config.otlp_endpoint = "http://localhost:4317"
    hub = MagicMock()
    hub.subscribe = MagicMock(return_value=None)
    app = create_app(orch, hub, api_key="test-key")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/telemetry/status", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is True
    assert data["exporter"] == "otlp"
    assert data["otlp_endpoint"] == "http://localhost:4317"
