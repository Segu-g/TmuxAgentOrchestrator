"""Tests for the worktree_integrity Strangler Fig shim.

Verifies that:
1. The old import path still works (backward compatibility).
2. The canonical import path works.
3. Old and canonical imports resolve to the same class objects (shim identity).
4. infrastructure.__init__ re-exports the integrity classes.

References:
    - Martin Fowler, "StranglerFigApplication" (2004)
    - DESIGN.md §10.N (v1.0.18 — worktree_integrity migration to infrastructure/)
"""
from __future__ import annotations

import pytest


class TestShimExports:
    def test_old_path_exports_worktree_status(self) -> None:
        """WorktreeStatus is importable from the old (shim) path."""
        from tmux_orchestrator.worktree_integrity import WorktreeStatus

        assert WorktreeStatus is not None
        assert hasattr(WorktreeStatus, "to_dict")

    def test_old_path_exports_checker(self) -> None:
        """WorktreeIntegrityChecker is importable from the old (shim) path."""
        from tmux_orchestrator.worktree_integrity import WorktreeIntegrityChecker

        assert WorktreeIntegrityChecker is not None
        assert hasattr(WorktreeIntegrityChecker, "check_path")

    def test_canonical_path_exports_worktree_status(self) -> None:
        """WorktreeStatus is importable from the canonical path."""
        from tmux_orchestrator.infrastructure.worktree_integrity import WorktreeStatus

        assert WorktreeStatus is not None

    def test_canonical_path_exports_checker(self) -> None:
        """WorktreeIntegrityChecker is importable from the canonical path."""
        from tmux_orchestrator.infrastructure.worktree_integrity import (
            WorktreeIntegrityChecker,
        )

        assert WorktreeIntegrityChecker is not None


class TestShimIdentity:
    def test_worktree_status_same_class(self) -> None:
        """Old and canonical import of WorktreeStatus resolve to the same class."""
        from tmux_orchestrator.infrastructure.worktree_integrity import (
            WorktreeStatus as Canonical,
        )
        from tmux_orchestrator.worktree_integrity import WorktreeStatus as Shim

        assert Shim is Canonical

    def test_worktree_integrity_checker_same_class(self) -> None:
        """Old and canonical import of WorktreeIntegrityChecker resolve to the same class."""
        from tmux_orchestrator.infrastructure.worktree_integrity import (
            WorktreeIntegrityChecker as Canonical,
        )
        from tmux_orchestrator.worktree_integrity import (
            WorktreeIntegrityChecker as Shim,
        )

        assert Shim is Canonical


class TestInfrastructureInitReexports:
    def test_infrastructure_init_has_worktree_status(self) -> None:
        """infrastructure.__init__ re-exports WorktreeStatus."""
        from tmux_orchestrator import infrastructure

        assert hasattr(infrastructure, "WorktreeStatus")

    def test_infrastructure_init_has_checker(self) -> None:
        """infrastructure.__init__ re-exports WorktreeIntegrityChecker."""
        from tmux_orchestrator import infrastructure

        assert hasattr(infrastructure, "WorktreeIntegrityChecker")

    def test_infrastructure_init_checker_same_class(self) -> None:
        """infrastructure.WorktreeIntegrityChecker is same as canonical."""
        from tmux_orchestrator import infrastructure
        from tmux_orchestrator.infrastructure.worktree_integrity import (
            WorktreeIntegrityChecker,
        )

        assert infrastructure.WorktreeIntegrityChecker is WorktreeIntegrityChecker

    def test_infrastructure_init_status_same_class(self) -> None:
        """infrastructure.WorktreeStatus is same as canonical."""
        from tmux_orchestrator import infrastructure
        from tmux_orchestrator.infrastructure.worktree_integrity import WorktreeStatus

        assert infrastructure.WorktreeStatus is WorktreeStatus
