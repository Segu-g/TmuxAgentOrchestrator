"""Application-layer infrastructure DI protocols and Null Object implementations.

Defines structural interfaces (PEP 544 Protocols) for infrastructure components
that were previously inline-imported inside ``Orchestrator.__init__``:

- ``ResultStoreProtocol``     — append-only JSONL result persistence
- ``CheckpointStoreProtocol`` — SQLite-backed state snapshots
- ``AutoScalerProtocol``      — queue-depth-triggered agent pool scaling

The real infrastructure implementations (``ResultStore``, ``CheckpointStore``,
``AutoScaler``) live in the infrastructure layer; only the *contracts* and
*no-op stubs* live here.

``WorkflowManager`` and ``GroupManager`` are simple enough that no Null Object
is needed — their default empty state is already a valid no-op for tests.

Layer rule: this module imports ONLY typing + stdlib — no infrastructure.

Null Object pattern: ``Null*`` classes satisfy their respective protocols with
no-op / empty-return methods, enabling fast isolated unit tests.

Reference:
    - PEP 544 — Protocols: Structural subtyping (static duck typing)
    - Fowler "Refactoring" (1999) — Null Object pattern
    - Martin "Clean Architecture" (2017) — Dependency Inversion Principle
    - Glukhov "Dependency Injection: a Python Way" (glukhov.org, 2025-12)
    - Glukhov "Python Design Patterns for Clean Architecture" (2025-11)
    - DESIGN.md §10.35 (v1.0.35 — orchestrator infra DI)
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# ResultStore Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ResultStoreProtocol(Protocol):
    """Structural interface for task-result persistence.

    The real implementation is :class:`~tmux_orchestrator.result_store.ResultStore`.
    Tests may inject a :class:`NullResultStore` or any other conforming object.

    Reference: PEP 544; Fowler "Event Sourcing" (2005); DESIGN.md §10.19 (v0.24.0).
    """

    def append(
        self,
        *,
        task_id: str,
        agent_id: str,
        prompt: str,
        result_text: str,
        error: "str | None",
        duration_s: float,
    ) -> None: ...

    def all_dates(self) -> "list[str]": ...

    def query(
        self,
        *,
        date: "str | None" = None,
        agent_id: "str | None" = None,
        task_id: "str | None" = None,
        limit: int = 100,
    ) -> "list[dict[str, Any]]": ...


# ---------------------------------------------------------------------------
# CheckpointStore Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CheckpointStoreProtocol(Protocol):
    """Structural interface for SQLite-backed checkpoint persistence.

    The real implementation is
    :class:`~tmux_orchestrator.checkpoint_store.CheckpointStore`.
    Tests may inject a :class:`NullCheckpointStore` or any other conforming object.

    All method signatures use forward-reference strings to avoid importing
    infrastructure types (Task, WorkflowRun) at module load time — keeping this
    file's imports stdlib-only (Clean Architecture layer rule).

    Reference: LangGraph checkpointer pattern; Chandy-Lamport (1985);
    DESIGN.md §10.12 (v0.45.0).
    """

    def initialize(self) -> None: ...
    def close(self) -> None: ...
    def save_task(self, *, task: Any, queue_priority: int) -> None: ...
    def remove_task(self, *, task_id: str) -> None: ...
    def load_pending_tasks(self) -> "list[Any]": ...
    def clear_tasks(self) -> None: ...
    def save_waiting_task(self, *, task: Any) -> None: ...
    def remove_waiting_task(self, *, task_id: str) -> None: ...
    def load_waiting_tasks(self) -> "list[Any]": ...
    def clear_waiting_tasks(self) -> None: ...
    def save_workflow(self, *, run: Any) -> None: ...
    def remove_workflow(self, *, workflow_id: str) -> None: ...
    def load_workflows(self) -> "dict[str, Any]": ...
    def clear_workflows(self) -> None: ...
    def save_meta(self, key: str, value: str) -> None: ...
    def load_meta(self, key: str, *, default: "str | None" = None) -> "str | None": ...
    def clear_all(self) -> None: ...


# ---------------------------------------------------------------------------
# AutoScaler Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AutoScalerProtocol(Protocol):
    """Structural interface for queue-depth-triggered agent pool autoscalers.

    The real implementation is :class:`~tmux_orchestrator.autoscaler.AutoScaler`.
    Tests may inject a :class:`NullAutoScaler` or any other conforming object.

    Reference: Kubernetes HPA; DESIGN.md §10.18 (v0.23.0).
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...

    async def status(self) -> "dict[str, Any]": ...

    def reconfigure(
        self,
        *,
        min: "int | None" = None,
        max: "int | None" = None,
        threshold: "int | None" = None,
        cooldown: "float | None" = None,
    ) -> "dict[str, Any]": ...


# ---------------------------------------------------------------------------
# Null Object implementations
# ---------------------------------------------------------------------------


class NullResultStore:
    """No-op result store satisfying :class:`ResultStoreProtocol`.

    For use in unit tests and configurations where result persistence is
    not required.  All write operations are silent no-ops; read operations
    return empty results.

    Reference: Fowler "Refactoring" (1999) — Null Object pattern.
    """

    def append(
        self,
        *,
        task_id: str,
        agent_id: str,
        prompt: str,
        result_text: str,
        error: "str | None",
        duration_s: float,
    ) -> None:
        """No-op."""

    def all_dates(self) -> "list[str]":
        return []

    def query(
        self,
        *,
        date: "str | None" = None,
        agent_id: "str | None" = None,
        task_id: "str | None" = None,
        limit: int = 100,
    ) -> "list[dict[str, Any]]":
        return []


class NullCheckpointStore:
    """No-op checkpoint store satisfying :class:`CheckpointStoreProtocol`.

    For use in unit tests and configurations where checkpoint persistence is
    not required.  All operations are silent no-ops or return empty data.

    Reference: Fowler "Refactoring" (1999) — Null Object pattern.
    """

    def initialize(self) -> None:
        """No-op."""

    def close(self) -> None:
        """No-op."""

    def save_task(self, *, task: Any, queue_priority: int) -> None:
        """No-op."""

    def remove_task(self, *, task_id: str) -> None:
        """No-op."""

    def load_pending_tasks(self) -> "list[Any]":
        return []

    def clear_tasks(self) -> None:
        """No-op."""

    def save_waiting_task(self, *, task: Any) -> None:
        """No-op."""

    def remove_waiting_task(self, *, task_id: str) -> None:
        """No-op."""

    def load_waiting_tasks(self) -> "list[Any]":
        return []

    def clear_waiting_tasks(self) -> None:
        """No-op."""

    def save_workflow(self, *, run: Any) -> None:
        """No-op."""

    def remove_workflow(self, *, workflow_id: str) -> None:
        """No-op."""

    def load_workflows(self) -> "dict[str, Any]":
        return {}

    def clear_workflows(self) -> None:
        """No-op."""

    def save_meta(self, key: str, value: str) -> None:
        """No-op."""

    def load_meta(self, key: str, *, default: "str | None" = None) -> "str | None":
        return default

    def clear_all(self) -> None:
        """No-op."""


class NullAutoScaler:
    """No-op autoscaler satisfying :class:`AutoScalerProtocol`.

    For use in unit tests and configurations where autoscaling is not
    enabled.  Lifecycle methods are no-ops; ``status()`` returns a
    disabled-stub dict.

    Reference: Fowler "Refactoring" (1999) — Null Object pattern.
    """

    def start(self) -> None:
        """No-op."""

    def stop(self) -> None:
        """No-op."""

    async def status(self) -> "dict[str, Any]":
        return {
            "enabled": False,
            "agent_count": 0,
            "queue_depth": 0,
            "last_scale_up": None,
            "last_scale_down": None,
            "autoscaled_ids": [],
            "min": 0,
            "max": 0,
            "threshold": 0,
            "cooldown": 0.0,
        }

    def reconfigure(
        self,
        *,
        min: "int | None" = None,
        max: "int | None" = None,
        threshold: "int | None" = None,
        cooldown: "float | None" = None,
    ) -> "dict[str, Any]":
        """No-op — returns the requested parameters (or 0 for omitted ones)."""
        return {
            "min": min if min is not None else 0,
            "max": max if max is not None else 0,
            "threshold": threshold if threshold is not None else 0,
            "cooldown": cooldown if cooldown is not None else 0.0,
        }
