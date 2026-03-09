"""Backward-compatibility shim — canonical location is ``application/schemas``.

All symbols are re-exported unchanged so that existing import paths continue to work.

DESIGN.md §10.59 (v1.1.27 — Clean Architecture Phase 5)
"""
from tmux_orchestrator.application.schemas import *  # noqa: F401, F403
from tmux_orchestrator.application.schemas import (  # noqa: F401
    _BasePayload,
    _STATUS_PAYLOADS,
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
