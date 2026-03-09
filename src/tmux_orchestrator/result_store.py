"""Strangler Fig shim — ResultStore has moved to infrastructure/result_store.py.

This module re-exports everything from the canonical location so that
existing imports of ``tmux_orchestrator.result_store`` continue to work
without modification.

Canonical location: ``tmux_orchestrator.infrastructure.result_store``

Reference: DESIGN.md §10.57 (v1.1.25 — Clean Architecture Migration Phase 3)
"""

from tmux_orchestrator.infrastructure.result_store import ResultStore

__all__ = ["ResultStore"]
