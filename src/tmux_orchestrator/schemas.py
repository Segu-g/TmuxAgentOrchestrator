"""Typed payload schemas for bus messages.

Each ``MessageType`` variant has a corresponding Pydantic model that documents
and validates the expected payload structure.  The schemas are additive — the
bus itself still carries ``dict`` payloads for backward compatibility, but call
sites can use ``parse_payload()`` to get a validated, typed model.

Reference: Pydantic v2 model validation (https://docs.pydantic.dev/latest/)
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class _BasePayload(BaseModel):
    model_config = {"extra": "allow"}  # forward-compatible: unknown keys ignored


# ---------------------------------------------------------------------------
# STATUS payloads
# ---------------------------------------------------------------------------


class TaskQueuedPayload(_BasePayload):
    event: Literal["task_queued"]
    task_id: str
    prompt: str


class AgentBusyPayload(_BasePayload):
    event: Literal["agent_busy"]
    agent_id: str
    status: str
    task_id: str | None = None


class AgentIdlePayload(_BasePayload):
    event: Literal["agent_idle"]
    agent_id: str
    status: str
    task_id: str | None = None


class AgentErrorPayload(_BasePayload):
    event: Literal["agent_error"]
    agent_id: str
    status: str
    task_id: str | None = None


class SubagentSpawnedPayload(_BasePayload):
    event: Literal["subagent_spawned"]
    sub_agent_id: str
    parent_id: str


class TaskDeadLetteredPayload(_BasePayload):
    event: Literal["task_dead_lettered"]
    task_id: str
    prompt: str
    retry_count: int
    reason: str


# ---------------------------------------------------------------------------
# RESULT payloads
# ---------------------------------------------------------------------------


class TaskResultPayload(_BasePayload):
    task_id: str
    output: str | None = None
    error: str | None = None

    @field_validator("output", "error", mode="before")
    @classmethod
    def coerce_to_str(cls, v: Any) -> str | None:
        """Coerce non-string output/error values to str for robustness against malformed messages."""
        if v is None:
            return None
        if isinstance(v, str):
            return v
        return str(v)


# ---------------------------------------------------------------------------
# PEER_MSG payloads
# ---------------------------------------------------------------------------


class PeerMessagePayload(_BasePayload):
    text: str = ""
    _forwarded: bool = False


# ---------------------------------------------------------------------------
# CONTROL payloads
# ---------------------------------------------------------------------------


class SpawnSubagentPayload(_BasePayload):
    action: Literal["spawn_subagent"]
    template_id: str
    share_parent_worktree: bool = False


# ---------------------------------------------------------------------------
# Registry: MessageType → payload model
# ---------------------------------------------------------------------------

_STATUS_PAYLOADS: dict[str, type[_BasePayload]] = {
    "task_queued": TaskQueuedPayload,
    "agent_busy": AgentBusyPayload,
    "agent_idle": AgentIdlePayload,
    "agent_error": AgentErrorPayload,
    "subagent_spawned": SubagentSpawnedPayload,
    "task_dead_lettered": TaskDeadLetteredPayload,
}


def parse_status_payload(payload: dict[str, Any]) -> _BasePayload:
    """Validate and return a typed STATUS payload model."""
    event = payload.get("event", "")
    model_cls = _STATUS_PAYLOADS.get(event, _BasePayload)
    return model_cls.model_validate(payload)


def parse_result_payload(payload: dict[str, Any]) -> TaskResultPayload:
    """Validate and return a typed RESULT payload model."""
    return TaskResultPayload.model_validate(payload)
