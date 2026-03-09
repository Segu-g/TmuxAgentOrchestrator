"""Strangler Fig shim — re-exports from canonical location.

The implementation has moved to :mod:`tmux_orchestrator.application.task_queue`.
This module is kept for backward compatibility.
"""
from tmux_orchestrator.application.task_queue import *  # noqa: F401, F403
from tmux_orchestrator.application.task_queue import (  # noqa: F401
    TaskQueue,
    AsyncPriorityTaskQueue,
)
