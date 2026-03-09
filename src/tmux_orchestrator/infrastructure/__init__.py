"""Infrastructure sub-package for TmuxAgentOrchestrator.

Contains adapters for external systems: tmux, filesystem (Claude trust),
HTTP clients, and other I/O boundaries.

Layer rule (Clean Architecture — Martin, 2017):
  domain/ ← application/ ← infrastructure/

infrastructure/ MAY import from domain/ and application/.
domain/ and application/ MUST NOT import from infrastructure/.

Public re-exports:
    from tmux_orchestrator.infrastructure import (
        TmuxInterface,
        PaneOutputEvent,
        POLL_INTERVAL,
        pre_trust_worktree,
    )

References:
    - Martin, Robert C. "Clean Architecture" (2017) Ch. 22
    - Cockburn, Alistair. "Hexagonal Architecture" (ports and adapters)
    - DESIGN.md §10.N (v1.0.16 — infrastructure/ layer extraction)
"""

from tmux_orchestrator.infrastructure.claude_trust import pre_trust_worktree
from tmux_orchestrator.infrastructure.tmux import (
    POLL_INTERVAL,
    PaneOutputEvent,
    TmuxInterface,
)

__all__ = [
    "POLL_INTERVAL",
    "PaneOutputEvent",
    "TmuxInterface",
    "pre_trust_worktree",
]
