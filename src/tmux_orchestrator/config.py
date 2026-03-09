"""Backward-compatibility shim — canonical location is ``application/config``.

All symbols are re-exported unchanged so that existing import paths continue to work.

DESIGN.md §10.59 (v1.1.27 — Clean Architecture Phase 5)
"""
from tmux_orchestrator.application.config import *  # noqa: F401, F403
from tmux_orchestrator.application.config import (  # noqa: F401
    AgentConfig,
    AgentRole,
    OrchestratorConfig,
    WebhookConfig,
    load_config,
)
