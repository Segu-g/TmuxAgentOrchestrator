"""Application sub-package for TmuxAgentOrchestrator.

Contains use-cases and application services that depend on domain types
and DI protocols, but have NO direct dependency on infrastructure
(tmux, libtmux, subprocess, filesystem, HTTP).

Layer rule (Clean Architecture — Martin, 2017):
  domain/ ← application/ ← infrastructure/

Public re-exports:
    from tmux_orchestrator.application import (
        TaskSubmitter,
        WorkflowStep,
        Workflow,
        supervised_task,
        ContextMonitorProtocol,
        DriftMonitorProtocol,
        NullContextMonitor,
        NullDriftMonitor,
    )

References:
    - Martin, Robert C. "Clean Architecture" (2017) Ch. 22 — The Clean Architecture
    - Freeman & Pryce "Growing Object-Oriented Software, Guided by Tests" (2009)
    - DESIGN.md §10.N (v1.0.15 — application/ layer extraction)
"""

from tmux_orchestrator.application.monitor_protocols import (
    ContextMonitorProtocol,
    DriftMonitorProtocol,
    NullContextMonitor,
    NullDriftMonitor,
)
from tmux_orchestrator.application.supervision import supervised_task
from tmux_orchestrator.application.workflow_service import (
    TaskSubmitter,
    Workflow,
    WorkflowStep,
    _topological_sort,
)

__all__ = [
    "ContextMonitorProtocol",
    "DriftMonitorProtocol",
    "NullContextMonitor",
    "NullDriftMonitor",
    "TaskSubmitter",
    "Workflow",
    "WorkflowStep",
    "_topological_sort",
    "supervised_task",
]
