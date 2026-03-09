"""Strangler Fig shim — Mailbox has moved to infrastructure/messaging.py.

This module re-exports everything from the canonical location so that
existing imports of ``tmux_orchestrator.messaging`` continue to work
without modification.

Canonical location: ``tmux_orchestrator.infrastructure.messaging``

Reference: DESIGN.md §10.N (v1.0.17 — infrastructure/ layer continued extraction)
"""

from tmux_orchestrator.infrastructure.messaging import Mailbox

__all__ = ["Mailbox"]
