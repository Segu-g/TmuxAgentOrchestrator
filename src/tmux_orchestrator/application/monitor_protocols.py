"""Application-layer monitor DI protocols and Null Object implementations.

Defines structural interfaces (PEP 544 Protocols) for context-window and
behavioral-drift monitors.  The real infrastructure implementations
(ContextMonitor, DriftMonitor) are in the infrastructure layer; only the
*contracts* and *no-op stubs* live here.

Layer rule: this module imports ONLY typing + stdlib — no infrastructure.

Null Object pattern: NullContextMonitor and NullDriftMonitor satisfy their
respective protocols with no-op methods, allowing tests and production code
to inject them where the real monitors are not needed.

Reference:
    - PEP 544 — Protocols: Structural subtyping (static duck typing)
    - Fowler "Refactoring" (1999) — Null Object pattern
    - Martin "Clean Architecture" (2017) — Dependency Inversion Principle
    - Liu et al. "Lost in the Middle" TACL 2024 (context window motivation)
    - Rath arXiv:2601.04170 "Agent Drift" (2026) (drift monitor motivation)
    - DESIGN.md §10.N (v1.0.14 — orchestrator full DI); §10.N (v1.0.15 — application/)
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ContextMonitorProtocol(Protocol):
    """Structural interface for context-window monitors.

    Any object that implements ``start()``, ``stop()``, ``get_stats()``, and
    ``all_stats()`` satisfies this protocol — no inheritance required.

    The real implementation is :class:`~tmux_orchestrator.context_monitor.ContextMonitor`.
    Tests may inject a :class:`NullContextMonitor` or any other conforming object.

    Reference: PEP 544 — Protocols: Structural subtyping (static duck typing).
    DESIGN.md §10.N (v1.0.14 — orchestrator full DI; v1.0.15 — application/).
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def get_stats(self, agent_id: str) -> "dict[str, Any] | None": ...
    def all_stats(self) -> "list[dict[str, Any]]": ...


@runtime_checkable
class DriftMonitorProtocol(Protocol):
    """Structural interface for behavioral-drift monitors.

    The real implementation is :class:`~tmux_orchestrator.drift_monitor.DriftMonitor`.
    Tests may inject a :class:`NullDriftMonitor` or any other conforming object.

    Reference: PEP 544 — Protocols: Structural subtyping (static duck typing).
    DESIGN.md §10.N (v1.0.14 — orchestrator full DI; v1.0.15 — application/).
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def get_drift_stats(self, agent_id: str) -> "dict[str, Any] | None": ...
    def all_drift_stats(self) -> "list[dict[str, Any]]": ...


class NullContextMonitor:
    """No-op context monitor satisfying :class:`ContextMonitorProtocol`.

    For use in unit tests and configurations where context monitoring is
    not required.  All query methods return empty results.

    Reference: Fowler "Refactoring" (1999) — Null Object pattern.
    """

    def start(self) -> None:  # noqa: D401
        """No-op."""

    def stop(self) -> None:  # noqa: D401
        """No-op."""

    def get_stats(self, agent_id: str) -> "dict[str, Any] | None":
        return None

    def all_stats(self) -> "list[dict[str, Any]]":
        return []


class NullDriftMonitor:
    """No-op drift monitor satisfying :class:`DriftMonitorProtocol`.

    For use in unit tests and configurations where drift detection is
    not required.  All query methods return empty results.

    Reference: Fowler "Refactoring" (1999) — Null Object pattern.
    """

    def start(self) -> None:  # noqa: D401
        """No-op."""

    def stop(self) -> None:  # noqa: D401
        """No-op."""

    def get_drift_stats(self, agent_id: str) -> "dict[str, Any] | None":
        return None

    def all_drift_stats(self) -> "list[dict[str, Any]]":
        return []
