"""Strangler Fig shim — EpisodeStore has moved to infrastructure/episode_store.py.

This module re-exports everything from the canonical location so that
existing imports of ``tmux_orchestrator.episode_store`` continue to work
without modification.

Canonical location: ``tmux_orchestrator.infrastructure.episode_store``

Reference: DESIGN.md §10.57 (v1.1.25 — Clean Architecture Migration Phase 3)
"""

from tmux_orchestrator.infrastructure.episode_store import (
    EpisodeStore,
    EpisodeNotFoundError,
)

__all__ = ["EpisodeStore", "EpisodeNotFoundError"]
