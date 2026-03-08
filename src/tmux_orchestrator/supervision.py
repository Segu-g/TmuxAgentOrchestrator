"""Backward-compatibility shim for supervision.py.

The canonical implementation has moved to:
    tmux_orchestrator.application.supervision

This module re-exports ``supervised_task`` from the application layer so that
existing imports (``from tmux_orchestrator.supervision import supervised_task``)
continue to work without modification.

Strangler Fig migration pattern (Fowler):
  Old path  → tmux_orchestrator.supervision (this shim)
  New path  → tmux_orchestrator.application.supervision (canonical)

Reference:
    - Fowler "Strangler Fig Application" (bliki, 2004)
    - DESIGN.md §10.N (v1.0.15 — application/ layer extraction)
"""
from __future__ import annotations

# Re-export everything from the new canonical location.
# Using explicit names keeps IDE auto-complete and static analysis happy.
from tmux_orchestrator.application.supervision import (  # noqa: F401
    _BACKOFF,
    supervised_task,
)

__all__ = ["supervised_task", "_BACKOFF"]
