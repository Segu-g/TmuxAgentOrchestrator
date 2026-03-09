"""Backward-compatibility shim — canonical location is ``application/orchestrator``.

All symbols are re-exported unchanged so that existing import paths continue to work.

DESIGN.md §10.59 (v1.1.27 — Clean Architecture Phase 5)
"""
from tmux_orchestrator.application.orchestrator import *  # noqa: F401, F403
from tmux_orchestrator.application.orchestrator import (  # noqa: F401
    Orchestrator,
)
# Re-export protocol types that were historically importable from this module
from tmux_orchestrator.application.infra_protocols import (  # noqa: F401
    AutoScalerProtocol,
    CheckpointStoreProtocol,
    NullAutoScaler,
    NullCheckpointStore,
    NullResultStore,
    ResultStoreProtocol,
)
from tmux_orchestrator.application.monitor_protocols import (  # noqa: F401
    ContextMonitorProtocol,
    DriftMonitorProtocol,
    NullContextMonitor,
    NullDriftMonitor,
)
