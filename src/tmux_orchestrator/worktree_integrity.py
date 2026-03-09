"""Backward-compatibility shim for worktree_integrity.

The canonical module is now:
    tmux_orchestrator.infrastructure.worktree_integrity

This shim re-exports all public names from the canonical location.
It will be removed in a future version once all internal callers have been
updated to the canonical import path.

Strangler Fig Pattern Reference:
    - Martin Fowler, "StranglerFigApplication" (2004)
      https://martinfowler.com/bliki/StranglerFigApplication.html
    - Shopify Engineering, "Refactoring Legacy Code with the Strangler Fig Pattern"
      https://shopify.engineering/refactoring-legacy-code-strangler-fig-pattern
    - DESIGN.md §10.N (v1.0.18 — worktree_integrity migration)

Do NOT add new code here. All new functionality belongs in:
    tmux_orchestrator.infrastructure.worktree_integrity
"""
from tmux_orchestrator.infrastructure.worktree_integrity import (  # noqa: F401
    WorktreeIntegrityChecker,
    WorktreeStatus,
    _run_git,  # noqa: F401 — exported for tests that import it directly
)

__all__ = [
    "WorktreeStatus",
    "WorktreeIntegrityChecker",
]
