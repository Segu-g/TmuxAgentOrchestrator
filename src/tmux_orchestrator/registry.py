"""Strangler Fig shim — canonical implementation moved to application/registry.py.

All existing ``from tmux_orchestrator.registry import …`` imports continue
to work unchanged.  New code should import from
``tmux_orchestrator.application.registry`` directly.

DESIGN.md §10.56 (v1.1.24 — Clean Architecture Phase 2).
"""
from tmux_orchestrator.application.registry import AgentRegistry  # noqa: F401

__all__ = ["AgentRegistry"]
