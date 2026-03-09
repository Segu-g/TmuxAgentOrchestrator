"""Backward-compatibility shim for tmux_interface.

The canonical implementation has moved to:
    tmux_orchestrator.infrastructure.tmux

Import from there for new code. This shim re-exports all public names
so that existing ``from tmux_orchestrator.tmux_interface import ...``
statements continue to work without modification (Strangler Fig pattern).

Reference: DESIGN.md §10.N (v1.0.16 — infrastructure/ layer extraction)
"""

# ruff: noqa: F401,F403
import libtmux  # re-exported so existing @patch("tmux_orchestrator.tmux_interface.libtmux.Server") works
import time  # re-exported so existing @patch("tmux_orchestrator.tmux_interface.time.sleep") works

from tmux_orchestrator.infrastructure.tmux import (
    POLL_INTERVAL,
    PaneOutputEvent,
    TmuxInterface,
    _hash,
)

__all__ = [
    "POLL_INTERVAL",
    "PaneOutputEvent",
    "TmuxInterface",
    "_hash",
]
