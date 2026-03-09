"""Strangler Fig shim — re-exports from canonical location.

The implementation has moved to :mod:`tmux_orchestrator.infrastructure.logging_config`.
This module is kept for backward compatibility.
"""
from tmux_orchestrator.infrastructure.logging_config import *  # noqa: F401, F403
from tmux_orchestrator.infrastructure.logging_config import (  # noqa: F401
    bind_trace,
    bind_agent,
    unbind,
    current_trace_id,
    current_agent_id,
    JsonFormatter,
    setup_json_logging,
    setup_text_logging,
)
