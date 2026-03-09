"""Strangler Fig shim — re-exports from canonical location.

The implementation has moved to :mod:`tmux_orchestrator.infrastructure.telemetry`.
This module is kept for backward compatibility.
"""
from tmux_orchestrator.infrastructure.telemetry import *  # noqa: F401, F403
from tmux_orchestrator.infrastructure.telemetry import (  # noqa: F401
    RingBufferSpanExporter,
    TelemetrySetup,
    get_tracer,
    agent_span,
    task_queued_span,
    workflow_span,
)
