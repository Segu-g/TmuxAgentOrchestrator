"""Backward-compatibility shim for trust.

The canonical implementation has moved to:
    tmux_orchestrator.infrastructure.claude_trust

Import from there for new code. This shim re-exports all public names
so that existing ``from tmux_orchestrator.trust import ...``
statements continue to work without modification (Strangler Fig pattern).

Reference: DESIGN.md §10.N (v1.0.16 — infrastructure/ layer extraction)
"""

# ruff: noqa: F401,F403
from tmux_orchestrator.infrastructure.claude_trust import (
    _DEFAULT_CLAUDE_JSON,
    _atomic_write_json,
    pre_trust_worktree,
)

__all__ = [
    "_DEFAULT_CLAUDE_JSON",
    "_atomic_write_json",
    "pre_trust_worktree",
]
