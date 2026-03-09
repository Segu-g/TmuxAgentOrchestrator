"""Strangler Fig shim — ContextMonitor has moved to infrastructure/context_monitor.py.

This module re-exports everything from the canonical location so that
existing imports of ``tmux_orchestrator.context_monitor`` continue to work
without modification.

Canonical location: ``tmux_orchestrator.infrastructure.context_monitor``

Reference: DESIGN.md §10.57 (v1.1.25 — Clean Architecture Migration Phase 3)
"""

from tmux_orchestrator.infrastructure.context_monitor import (
    ContextMonitor,
    AgentContextStats,
    _CHARS_PER_TOKEN,
    _DEFAULT_CONTEXT_WINDOW_TOKENS,
    _DEFAULT_WARN_THRESHOLD,
    _DEFAULT_POLL,
)

__all__ = [
    "ContextMonitor",
    "AgentContextStats",
    "_CHARS_PER_TOKEN",
    "_DEFAULT_CONTEXT_WINDOW_TOKENS",
    "_DEFAULT_WARN_THRESHOLD",
    "_DEFAULT_POLL",
]
