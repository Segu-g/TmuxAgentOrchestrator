"""Infrastructure sub-package for TmuxAgentOrchestrator.

Contains adapters for external systems: tmux, filesystem (Claude trust),
git worktrees, process ports (pane I/O), file-based messaging, and
git worktree integrity checking.

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
        WorktreeManager,
        ProcessPort,
        TmuxProcessAdapter,
        StdioProcessAdapter,
        Mailbox,
        WorktreeStatus,
        WorktreeIntegrityChecker,
    )

References:
    - Martin, Robert C. "Clean Architecture" (2017) Ch. 22
    - Cockburn, Alistair. "Hexagonal Architecture" (ports and adapters)
    - DESIGN.md §10.N (v1.0.16 — infrastructure/ layer extraction)
    - DESIGN.md §10.N (v1.0.17 — infrastructure/ layer continued extraction)
    - DESIGN.md §10.N (v1.0.18 — worktree_integrity migration to infrastructure/)
"""

from tmux_orchestrator.infrastructure.claude_trust import pre_trust_worktree
from tmux_orchestrator.infrastructure.messaging import Mailbox
from tmux_orchestrator.infrastructure.process_port import (
    ProcessPort,
    StdioProcessAdapter,
    TmuxProcessAdapter,
)
from tmux_orchestrator.infrastructure.tmux import (
    POLL_INTERVAL,
    PaneOutputEvent,
    TmuxInterface,
)
from tmux_orchestrator.infrastructure.worktree import WorktreeManager
from tmux_orchestrator.infrastructure.worktree_integrity import (
    WorktreeIntegrityChecker,
    WorktreeStatus,
)

__all__ = [
    "POLL_INTERVAL",
    "Mailbox",
    "PaneOutputEvent",
    "ProcessPort",
    "StdioProcessAdapter",
    "TmuxInterface",
    "TmuxProcessAdapter",
    "WorktreeIntegrityChecker",
    "WorktreeManager",
    "WorktreeStatus",
    "pre_trust_worktree",
]
