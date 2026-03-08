"""Domain sub-package for TmuxAgentOrchestrator.

Contains pure domain types with zero external dependencies.
All types are re-exported here for convenient access via:
    from tmux_orchestrator.domain import AgentStatus, Task, Message, ...
"""

from tmux_orchestrator.domain.agent import AgentRole, AgentStatus
from tmux_orchestrator.domain.message import BROADCAST, Message, MessageType
from tmux_orchestrator.domain.task import Task

__all__ = [
    "AgentRole",
    "AgentStatus",
    "BROADCAST",
    "Message",
    "MessageType",
    "Task",
]
