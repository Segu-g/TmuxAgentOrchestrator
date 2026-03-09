"""Strangler Fig shim — AutoScaler has moved to infrastructure/autoscaler.py.

This module re-exports everything from the canonical location so that
existing imports of ``tmux_orchestrator.autoscaler`` continue to work
without modification.

Canonical location: ``tmux_orchestrator.infrastructure.autoscaler``

Reference: DESIGN.md §10.57 (v1.1.25 — Clean Architecture Migration Phase 3)
"""

from tmux_orchestrator.infrastructure.autoscaler import AutoScaler

__all__ = ["AutoScaler"]
