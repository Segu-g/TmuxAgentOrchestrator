"""Strangler Fig shim — canonical implementation moved to application/workflow_manager.py.

All existing ``from tmux_orchestrator.workflow_manager import …`` imports continue
to work unchanged.  New code should import from
``tmux_orchestrator.application.workflow_manager`` directly.

DESIGN.md §10.56 (v1.1.24 — Clean Architecture Phase 2).
"""
from tmux_orchestrator.application.workflow_manager import (  # noqa: F401
    WorkflowManager,
    WorkflowRun,
    validate_dag,
)

__all__ = ["WorkflowManager", "WorkflowRun", "validate_dag"]
