"""Domain sub-package for TmuxAgentOrchestrator.

Contains pure domain types with zero external dependencies.
All types are re-exported here for convenient access via:
    from tmux_orchestrator.domain import AgentStatus, Task, Message, ...
    from tmux_orchestrator.domain import WorkflowRun, WorkflowPhase, WorkflowStatus
    from tmux_orchestrator.domain import PhaseStrategy, PhaseSpec, AgentSelector
"""

from tmux_orchestrator.domain.agent import AgentRole, AgentStatus
from tmux_orchestrator.domain.message import BROADCAST, Message, MessageType
from tmux_orchestrator.domain.phase_strategy import (
    AgentSelector,
    CompetitiveStrategy,
    DebateStrategy,
    ParallelStrategy,
    PhaseSpec,
    PhaseStrategy,
    SingleStrategy,
    WorkflowPhaseStatus,
    get_strategy,
)
from tmux_orchestrator.domain.task import Task
from tmux_orchestrator.domain.workflow import WorkflowPhase, WorkflowRun, WorkflowStatus

__all__ = [
    # agent
    "AgentRole",
    "AgentStatus",
    # message
    "BROADCAST",
    "Message",
    "MessageType",
    # phase_strategy
    "AgentSelector",
    "CompetitiveStrategy",
    "DebateStrategy",
    "ParallelStrategy",
    "PhaseSpec",
    "PhaseStrategy",
    "SingleStrategy",
    "WorkflowPhaseStatus",
    "get_strategy",
    # task
    "Task",
    # workflow
    "WorkflowPhase",
    "WorkflowRun",
    "WorkflowStatus",
]
