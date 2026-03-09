"""Application-layer Use Case Interactors.

Provides ``SubmitTaskUseCase`` and ``CancelTaskUseCase`` — thin application
services that mediate between the Web/CLI adapter layer and the domain
``Orchestrator``.  They own the mapping from request DTOs to domain calls and
back to response DTOs, so that FastAPI handlers stay as thin HTTP-to-domain
translators with no business logic.

Clean Architecture layer rule (Martin, 2017 Ch. 22):
  domain/ ← application/ ← infrastructure/adapters/

This module depends on:
  - tmux_orchestrator.domain.task (pure domain type)
  - typing.Protocol, dataclasses, __future__ (stdlib only)
  - No libtmux, subprocess, HTTP, filesystem

The concrete orchestrator satisfies ``TaskService`` structurally (PEP 544).

Design references:
  - Martin, Robert C. "Clean Architecture" (2017) Ch. 22 — Use Case Interactor.
  - Stemmler, Khalil. "Domain-Driven Design with TypeScript: Use Cases" (2019).
    https://khalilstemmler.com/articles/enterprise-typescript-nodejs/application-layer-use-cases/
  - Milan Jovanović, "Clean Architecture in ASP.NET Core" (2024).
    https://www.milanjovanovic.tech/blog/clean-architecture-the-missing-chapter
  - PEP 544 — Protocols: Structural subtyping (static duck typing).
  - DESIGN.md §10.33 (v1.0.33 — UseCaseInteractor layer extraction)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from tmux_orchestrator.domain.task import Task


# ---------------------------------------------------------------------------
# Structural protocols (DI boundary)
# ---------------------------------------------------------------------------


@runtime_checkable
class TaskService(Protocol):
    """Structural protocol satisfied by the real ``Orchestrator``.

    Only the methods required by use cases in this module are declared.
    Concrete ``Orchestrator`` satisfies this protocol without explicit
    inheritance (PEP 544 structural subtyping).
    """

    async def submit_task(
        self,
        prompt: str,
        *,
        priority: int = 0,
        metadata: dict | None = None,
        depends_on: list[str] | None = None,
        idempotency_key: str | None = None,
        reply_to: str | None = None,
        target_agent: str | None = None,
        required_tags: list[str] | None = None,
        target_group: str | None = None,
        max_retries: int = 0,
        inherit_priority: bool = True,
        ttl: float | None = None,
        _task_id: str | None = None,
    ) -> Task:
        """Enqueue a new task and return the created ``Task`` object."""
        ...

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a queued or in-progress task.  Returns True if found."""
        ...

    def list_agents(self) -> list[dict]:
        """Return a snapshot list of agent state dicts."""
        ...

    def get_agent(self, agent_id: str) -> Any:
        """Return the agent object for *agent_id*, or None."""
        ...


# ---------------------------------------------------------------------------
# Request / Response DTOs
# ---------------------------------------------------------------------------


@dataclass
class SubmitTaskDTO:
    """Input data transfer object for ``SubmitTaskUseCase``.

    Mirrors the fields of ``TaskSubmit`` (Pydantic web model) but is a plain
    dataclass with no HTTP-layer dependency.  This allows the use case to be
    invoked from CLI, TUI, or REST without coupling to FastAPI.
    """

    prompt: str
    priority: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    idempotency_key: str | None = None
    reply_to: str | None = None
    target_agent: str | None = None
    required_tags: list[str] = field(default_factory=list)
    target_group: str | None = None
    max_retries: int = 0
    inherit_priority: bool = True
    ttl: float | None = None


@dataclass
class SubmitTaskResult:
    """Output DTO for ``SubmitTaskUseCase``."""

    task_id: str
    prompt: str
    priority: int
    max_retries: int
    retry_count: int
    inherit_priority: bool
    submitted_at: float
    ttl: float | None = None
    expires_at: float | None = None
    depends_on: list[str] = field(default_factory=list)
    reply_to: str | None = None
    target_agent: str | None = None
    required_tags: list[str] = field(default_factory=list)
    target_group: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dict suitable for JSON serialisation."""
        result: dict[str, Any] = {
            "task_id": self.task_id,
            "prompt": self.prompt,
            "priority": self.priority,
            "max_retries": self.max_retries,
            "retry_count": self.retry_count,
            "inherit_priority": self.inherit_priority,
            "submitted_at": self.submitted_at,
            "ttl": self.ttl,
            "expires_at": self.expires_at,
        }
        if self.depends_on:
            result["depends_on"] = self.depends_on
        if self.reply_to is not None:
            result["reply_to"] = self.reply_to
        if self.target_agent is not None:
            result["target_agent"] = self.target_agent
        if self.required_tags:
            result["required_tags"] = self.required_tags
        if self.target_group is not None:
            result["target_group"] = self.target_group
        return result


@dataclass
class CancelTaskDTO:
    """Input DTO for ``CancelTaskUseCase``."""

    task_id: str


@dataclass
class CancelTaskResult:
    """Output DTO for ``CancelTaskUseCase``."""

    cancelled: bool
    task_id: str
    was_running: bool

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dict suitable for JSON serialisation."""
        return {
            "cancelled": self.cancelled,
            "task_id": self.task_id,
            "was_running": self.was_running,
        }


# ---------------------------------------------------------------------------
# Use Case Interactors
# ---------------------------------------------------------------------------


class SubmitTaskUseCase:
    """Encapsulates the task-submission business logic.

    Accepts a ``TaskService`` protocol (satisfied by ``Orchestrator``) and
    maps ``SubmitTaskDTO`` → domain call → ``SubmitTaskResult``.

    Usage::

        use_case = SubmitTaskUseCase(orchestrator)
        result = await use_case.execute(SubmitTaskDTO(prompt="hello"))
        print(result.task_id)

    The FastAPI handler can delegate to this use case after converting the
    Pydantic model to ``SubmitTaskDTO``, keeping the handler free of logic.

    Prompt sanitisation (``sanitize_prompt``) is intentionally left to the
    adapter layer (FastAPI handler) so that the use case remains testable
    without a security import.

    Reference: Martin "Clean Architecture" (2017) Ch. 22.
    """

    def __init__(self, service: TaskService) -> None:
        self._service = service

    async def execute(self, dto: SubmitTaskDTO) -> SubmitTaskResult:
        """Submit *dto* and return the created task as ``SubmitTaskResult``."""
        task = await self._service.submit_task(
            dto.prompt,
            priority=dto.priority,
            metadata=dto.metadata or None,
            depends_on=dto.depends_on or None,
            idempotency_key=dto.idempotency_key,
            reply_to=dto.reply_to,
            target_agent=dto.target_agent,
            required_tags=dto.required_tags or None,
            target_group=dto.target_group,
            max_retries=dto.max_retries,
            inherit_priority=dto.inherit_priority,
            ttl=dto.ttl,
        )
        return SubmitTaskResult(
            task_id=task.id,
            prompt=task.prompt,
            priority=task.priority,
            max_retries=task.max_retries,
            retry_count=task.retry_count,
            inherit_priority=task.inherit_priority,
            submitted_at=task.submitted_at,
            ttl=task.ttl,
            expires_at=task.expires_at,
            depends_on=list(task.depends_on) if task.depends_on else [],
            reply_to=task.reply_to,
            target_agent=task.target_agent,
            required_tags=list(task.required_tags) if task.required_tags else [],
            target_group=task.target_group,
        )


class CancelTaskUseCase:
    """Encapsulates the task-cancellation business logic.

    Determines whether the task was in-progress at the time of cancellation
    (for the ``was_running`` response field) and delegates to
    ``TaskService.cancel_task``.

    Usage::

        use_case = CancelTaskUseCase(orchestrator)
        result = await use_case.execute(CancelTaskDTO(task_id="abc"))
        if not result.cancelled:
            raise HTTPException(404, "Task not found")

    Reference: Martin "Clean Architecture" (2017) Ch. 22.
    """

    def __init__(self, service: TaskService) -> None:
        self._service = service

    async def execute(self, dto: CancelTaskDTO) -> CancelTaskResult:
        """Cancel *dto.task_id* and return a ``CancelTaskResult``."""
        # Determine whether the task is currently being executed by an agent
        # before issuing the cancel, so we can report was_running accurately.
        in_progress_ids: set[str] = set()
        for agent_info in self._service.list_agents():
            agent_obj = self._service.get_agent(agent_info["id"])
            if agent_obj is not None and agent_obj._current_task is not None:
                in_progress_ids.add(agent_obj._current_task.id)

        cancelled = await self._service.cancel_task(dto.task_id)
        return CancelTaskResult(
            cancelled=cancelled,
            task_id=dto.task_id,
            was_running=dto.task_id in in_progress_ids,
        )


# ---------------------------------------------------------------------------
# GetAgentUseCase
# ---------------------------------------------------------------------------


@dataclass
class GetAgentDTO:
    """Input DTO for ``GetAgentUseCase``."""

    agent_id: str


@dataclass
class GetAgentResult:
    """Output DTO for ``GetAgentUseCase``.

    ``found`` is False when the agent does not exist.  ``agent_dict`` is the
    raw dict snapshot when found (format mirrors ``TaskService.list_agents()``
    items), or an empty dict when not found.
    """

    found: bool
    agent_id: str
    agent_dict: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return agent_dict for JSON serialisation, or an empty dict."""
        return dict(self.agent_dict)


class GetAgentUseCase:
    """Retrieve a single agent by ID.

    Encapsulates the lookup logic (search ``list_agents()`` snapshot by ID)
    so that Web, TUI, and CLI adapters can call it without duplicating the
    search loop.

    Usage::

        uc = GetAgentUseCase(orchestrator)
        result = await uc.execute(GetAgentDTO(agent_id="worker-1"))
        if not result.found:
            raise HTTPException(404, "Agent not found")
        return result.to_dict()

    This is a **query** use case — it never mutates state.

    Reference: CQRS pattern; Martin "Clean Architecture" (2017) Ch. 22.
    """

    def __init__(self, service: TaskService) -> None:
        self._service = service

    async def execute(self, dto: GetAgentDTO) -> GetAgentResult:
        """Look up *dto.agent_id* and return a ``GetAgentResult``."""
        for agent_info in self._service.list_agents():
            if agent_info.get("id") == dto.agent_id:
                return GetAgentResult(
                    found=True,
                    agent_id=dto.agent_id,
                    agent_dict=agent_info,
                )
        return GetAgentResult(found=False, agent_id=dto.agent_id)


# ---------------------------------------------------------------------------
# ListAgentsUseCase
# ---------------------------------------------------------------------------


@dataclass
class ListAgentsDTO:
    """Input DTO for ``ListAgentsUseCase``.

    Currently carries no filter fields; the use case returns all registered
    agents.  Fields can be added here in future (e.g. ``status_filter``,
    ``tag_filter``) without touching the Web layer.
    """

    pass  # no filters for v1.1.15; extend here without breaking callers


@dataclass
class ListAgentsResult:
    """Output DTO for ``ListAgentsUseCase``.

    ``items`` is a list of agent state dicts with the same shape as the items
    returned by ``TaskService.list_agents()`` and the ``GET /agents`` endpoint.

    Usage::

        uc = ListAgentsUseCase(orchestrator)
        result = await uc.execute(ListAgentsDTO())
        return result.items   # JSON-serialisable list[dict]
    """

    items: list[dict[str, Any]] = field(default_factory=list)

    def to_list(self) -> list[dict[str, Any]]:
        """Return a plain list suitable for JSON serialisation."""
        return list(self.items)


class ListAgentsUseCase:
    """Return a snapshot list of all registered agents.

    This is a **query** use case — it never mutates state and is safe to call
    concurrently.  The result is a ``ListAgentsResult`` whose ``items`` list
    mirrors ``TaskService.list_agents()``.

    Design note: separating this from the FastAPI handler lets the TUI and CLI
    adapters use the same logic without duplicating the ``list_agents()`` call.

    Usage::

        uc = ListAgentsUseCase(orchestrator)
        result = await uc.execute(ListAgentsDTO())
        return result.to_list()

    Reference: CQRS query handler pattern (cosmicpython.com ch. 12, 2025);
    DESIGN.md §10.47 (v1.1.15 — query use case layer completion).
    """

    def __init__(self, service: TaskService) -> None:
        self._service = service

    async def execute(self, dto: ListAgentsDTO) -> ListAgentsResult:  # noqa: ARG002
        """Return all agents as a ``ListAgentsResult``."""
        items = list(self._service.list_agents())
        return ListAgentsResult(items=items)
