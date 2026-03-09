"""Strangler Fig shim — canonical implementation moved to application/circuit_breaker.py.

All existing ``from tmux_orchestrator.circuit_breaker import …`` imports continue
to work unchanged.  New code should import from
``tmux_orchestrator.application.circuit_breaker`` directly.

DESIGN.md §10.56 (v1.1.24 — Clean Architecture Phase 2).
"""
from tmux_orchestrator.application.circuit_breaker import (  # noqa: F401
    BreakerState,
    CircuitBreaker,
)

__all__ = ["BreakerState", "CircuitBreaker"]
