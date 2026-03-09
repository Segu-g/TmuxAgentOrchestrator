"""Strangler Fig shim — re-exports from canonical location.

The implementation has moved to :mod:`tmux_orchestrator.infrastructure.webhook_manager`.
This module is kept for backward compatibility.
"""
from tmux_orchestrator.infrastructure.webhook_manager import *  # noqa: F401, F403
from tmux_orchestrator.infrastructure.webhook_manager import (  # noqa: F401
    KNOWN_EVENTS,
    WebhookDelivery,
    Webhook,
    WebhookManager,
)
