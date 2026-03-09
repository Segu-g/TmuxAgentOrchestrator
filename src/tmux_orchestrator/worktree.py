"""Strangler Fig shim — WorktreeManager has moved to infrastructure/worktree.py.

This module re-exports everything from the canonical location so that
existing imports of ``tmux_orchestrator.worktree`` continue to work
without modification.

Canonical location: ``tmux_orchestrator.infrastructure.worktree``

Reference: DESIGN.md §10.N (v1.0.17 — infrastructure/ layer continued extraction)
"""

from tmux_orchestrator.infrastructure.worktree import WorktreeManager

__all__ = ["WorktreeManager"]
