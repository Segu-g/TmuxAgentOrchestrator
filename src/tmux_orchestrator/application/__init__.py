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
        ResultStoreProtocol,
        CheckpointStoreProtocol,
        AutoScalerProtocol,
        NullResultStore,
        NullCheckpointStore,
        NullAutoScaler,
        TaskService,
        SubmitTaskDTO,
        SubmitTaskResult,
        CancelTaskDTO,
        CancelTaskResult,
        SubmitTaskUseCase,
        CancelTaskUseCase,
    )

References:
    - Martin, Robert C. "Clean Architecture" (2017) Ch. 22 — The Clean Architecture
    - Freeman & Pryce "Growing Object-Oriented Software, Guided by Tests" (2009)
    - DESIGN.md §10.N (v1.0.15 — application/ layer extraction)
"""

from tmux_orchestrator.application.infra_protocols import (
    AutoScalerProtocol,
    CheckpointStoreProtocol,
    NullAutoScaler,
    NullCheckpointStore,
    NullResultStore,
    ResultStoreProtocol,
)
from tmux_orchestrator.application.monitor_protocols import (
    ContextMonitorProtocol,
    DriftMonitorProtocol,
    NullContextMonitor,
    NullDriftMonitor,
)
from tmux_orchestrator.application.supervision import supervised_task
from tmux_orchestrator.application.use_cases import (
    CancelTaskDTO,
    CancelTaskResult,
    CancelTaskUseCase,
    GetAgentDTO,
    GetAgentResult,
    GetAgentUseCase,
    SubmitTaskDTO,
    SubmitTaskResult,
    SubmitTaskUseCase,
    TaskService,
)
from tmux_orchestrator.application.workflow_service import (
    TaskSubmitter,
    Workflow,
    WorkflowStep,
    _topological_sort,
)

__all__ = [
    "AutoScalerProtocol",
    "CancelTaskDTO",
    "CancelTaskResult",
    "CancelTaskUseCase",
    "CheckpointStoreProtocol",
    "ContextMonitorProtocol",
    "DriftMonitorProtocol",
    "GetAgentDTO",
    "GetAgentResult",
    "GetAgentUseCase",
    "NullAutoScaler",
    "NullCheckpointStore",
    "NullContextMonitor",
    "NullDriftMonitor",
    "NullResultStore",
    "ResultStoreProtocol",
    "SubmitTaskDTO",
    "SubmitTaskResult",
    "SubmitTaskUseCase",
    "TaskService",
    "TaskSubmitter",
    "Workflow",
    "WorkflowStep",
    "_topological_sort",
    "supervised_task",
]
