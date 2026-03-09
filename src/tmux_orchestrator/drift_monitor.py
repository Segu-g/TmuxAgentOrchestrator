"""Strangler Fig shim — DriftMonitor has moved to infrastructure/drift_monitor.py.

This module re-exports everything from the canonical location so that
existing imports of ``tmux_orchestrator.drift_monitor`` continue to work
without modification.

Canonical location: ``tmux_orchestrator.infrastructure.drift_monitor``

Reference: DESIGN.md §10.57 (v1.1.25 — Clean Architecture Migration Phase 3)
"""

from tmux_orchestrator.infrastructure.drift_monitor import (
    DriftMonitor,
    AgentDriftStats,
    _DEFAULT_DRIFT_THRESHOLD,
    _DEFAULT_POLL,
    _DEFAULT_IDLE_THRESHOLD,
    _compute_role_score,
    _compute_idle_score,
    _compute_length_score,
    _composite_score,
    _tfidf_cosine_similarity,
    _tokenize_role,
)

__all__ = [
    "DriftMonitor",
    "AgentDriftStats",
    "_DEFAULT_DRIFT_THRESHOLD",
    "_DEFAULT_POLL",
    "_DEFAULT_IDLE_THRESHOLD",
    "_compute_role_score",
    "_compute_idle_score",
    "_compute_length_score",
    "_composite_score",
    "_tfidf_cosine_similarity",
    "_tokenize_role",
]
