"""Application sub-package for TmuxAgentOrchestrator.

Contains use-cases and application services that depend on domain types
and DI protocols, but have NO direct dependency on infrastructure
(tmux, libtmux, subprocess, filesystem, HTTP).

Layer rule (Clean Architecture — Martin, 2017):
  domain/ ← application/ ← infrastructure/

Public re-exports:
    from tmux_orchestrator.application import (
        Bus,
        AgentRegistry,
        WorkflowManager,
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
        ListAgentsUseCase,
        GetAgentUseCase,
    )

References:
    - Martin, Robert C. "Clean Architecture" (2017) Ch. 22 — The Clean Architecture
    - Freeman & Pryce "Growing Object-Oriented Software, Guided by Tests" (2009)
    - Percival, Gregory "Architecture Patterns with Python" (2020) Ch. 8 — Message Bus
    - DESIGN.md §10.N (v1.0.15 — application/ layer extraction)
    - DESIGN.md §10.56 (v1.1.24 — Clean Architecture Phase 2)
    - DESIGN.md §10.59 (v1.1.27 — Clean Architecture Phase 5)
"""

from tmux_orchestrator.application.bus import Bus
from tmux_orchestrator.application.circuit_breaker import BreakerState, CircuitBreaker
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
    ListAgentsDTO,
    ListAgentsResult,
    ListAgentsUseCase,
    SubmitTaskDTO,
    SubmitTaskResult,
    SubmitTaskUseCase,
    TaskService,
)
from tmux_orchestrator.application.registry import AgentRegistry
from tmux_orchestrator.application.workflow_manager import WorkflowManager, WorkflowRun, validate_dag
from tmux_orchestrator.application.workflow_service import (
    TaskSubmitter,
    Workflow,
    WorkflowStep,
    _topological_sort,
)
from tmux_orchestrator.application.schemas import (
    AgentBusyPayload,
    AgentErrorPayload,
    AgentIdlePayload,
    DriftWarningPayload,
    Episode,
    EpisodeCreate,
    PeerMessagePayload,
    SpawnSubagentPayload,
    SubagentSpawnedPayload,
    TaskDeadLetteredPayload,
    TaskQueuedPayload,
    TaskResultPayload,
    parse_result_payload,
    parse_status_payload,
)
from tmux_orchestrator.application.config import (
    AgentConfig,
    OrchestratorConfig,
    WebhookConfig,
    load_config,
)
# NOTE: Orchestrator, build_system, patch_api_key, patch_web_url are NOT imported here
# to avoid a deep circular import chain (orchestrator → agents.base → logging_config →
# infrastructure → bus → application/__init__ → orchestrator).
# Import them directly from their canonical modules:
#   from tmux_orchestrator.application.orchestrator import Orchestrator
#   from tmux_orchestrator.application.factory import build_system, patch_api_key, patch_web_url

__all__ = [
    "AgentRegistry",
    "AutoScalerProtocol",
    "BreakerState",
    "Bus",
    "CancelTaskDTO",
    "CancelTaskResult",
    "CancelTaskUseCase",
    "CheckpointStoreProtocol",
    "CircuitBreaker",
    "ContextMonitorProtocol",
    "DriftMonitorProtocol",
    "GetAgentDTO",
    "GetAgentResult",
    "GetAgentUseCase",
    "ListAgentsDTO",
    "ListAgentsResult",
    "ListAgentsUseCase",
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
    "WorkflowManager",
    "WorkflowRun",
    "WorkflowStep",
    "_topological_sort",
    "supervised_task",
    "validate_dag",
    # Phase 5 additions
    "AgentBusyPayload",
    "AgentConfig",
    "AgentErrorPayload",
    "AgentIdlePayload",
    "DriftWarningPayload",
    "Episode",
    "EpisodeCreate",
    "OrchestratorConfig",
    "PeerMessagePayload",
    "SpawnSubagentPayload",
    "SubagentSpawnedPayload",
    "TaskDeadLetteredPayload",
    "TaskQueuedPayload",
    "TaskResultPayload",
    "WebhookConfig",
    "load_config",
    "parse_result_payload",
    "parse_status_payload",
]
