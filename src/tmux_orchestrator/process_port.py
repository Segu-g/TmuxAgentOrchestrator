"""Strangler Fig shim — process_port has moved to infrastructure/process_port.py.

This module re-exports everything from the canonical location so that
existing imports of ``tmux_orchestrator.process_port`` continue to work
without modification.

Canonical location: ``tmux_orchestrator.infrastructure.process_port``

Reference: DESIGN.md §10.N (v1.0.17 — infrastructure/ layer continued extraction)
"""

from tmux_orchestrator.infrastructure.process_port import (
    ProcessPort,
    StdioProcessAdapter,
    TmuxProcessAdapter,
)

__all__ = ["ProcessPort", "StdioProcessAdapter", "TmuxProcessAdapter"]
