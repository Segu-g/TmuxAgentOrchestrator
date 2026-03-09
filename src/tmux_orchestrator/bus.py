"""Strangler Fig shim — canonical implementation moved to application/bus.py.

All existing ``from tmux_orchestrator.bus import …`` imports continue to work
unchanged.  New code should import from ``tmux_orchestrator.application.bus``
directly.

DESIGN.md §10.56 (v1.1.24 — Clean Architecture Phase 2).
"""
from tmux_orchestrator.application.bus import (  # noqa: F401
    BROADCAST,
    Bus,
    Message,
    MessageType,
)

__all__ = ["BROADCAST", "Bus", "Message", "MessageType"]
