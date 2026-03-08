"""Backward-compatibility shim for workflow.py.

The canonical implementation has moved to:
    tmux_orchestrator.application.workflow_service

This module re-exports ``Workflow``, ``WorkflowStep``, and ``_topological_sort``
from the application layer so that existing imports continue to work without
modification.

Strangler Fig migration pattern (Fowler):
  Old path  → tmux_orchestrator.workflow (this shim)
  New path  → tmux_orchestrator.application.workflow_service (canonical)

Reference:
    - Fowler "Strangler Fig Application" (bliki, 2004)
    - Richardson "Microservices Patterns" (2018) Ch. 4 — Saga
    - DESIGN.md §10.5 (2026-03-05 workflow builder); §10.N (v1.0.15 application/)
"""
from __future__ import annotations

# Re-export from the new canonical location.
from tmux_orchestrator.application.workflow_service import (  # noqa: F401
    TaskSubmitter,
    Workflow,
    WorkflowStep,
    _topological_sort,
)

__all__ = [
    "TaskSubmitter",
    "Workflow",
    "WorkflowStep",
    "_topological_sort",
]
