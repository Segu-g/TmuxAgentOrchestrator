"""Pure domain types for agent identity and status.

This module has ZERO external dependencies — only Python stdlib is imported.
It is the authoritative definition of AgentStatus and AgentRole.
"""

from __future__ import annotations

from enum import Enum


class AgentStatus(str, Enum):
    """Lifecycle status of an orchestrated agent."""

    IDLE = "IDLE"
    BUSY = "BUSY"
    ERROR = "ERROR"
    STOPPED = "STOPPED"
    DRAINING = "DRAINING"


class AgentRole(str, Enum):
    """Role assigned to an agent in the orchestrator hierarchy."""

    WORKER = "worker"
    DIRECTOR = "director"
    # TDD specialist roles (v1.2.22)
    TESTER = "tester"    # Red phase — writes failing tests
    CODER = "coder"      # Green phase — makes tests pass with minimal code
    REVIEWER = "reviewer"  # Refactor phase — improves code quality
