"""TaskQueue abstraction for dependency injection in Orchestrator.

``Orchestrator`` historically created an ``asyncio.PriorityQueue`` directly in
``__init__``, coupling the application layer to a specific infrastructure
implementation.  This module extracts the queue interface as a
:class:`TaskQueue` Protocol and provides :class:`AsyncPriorityTaskQueue` as
the default production implementation.

Injecting a custom ``TaskQueue`` enables unit tests to:

* Drive dispatch without real asyncio event loops.
* Inspect enqueued tasks without accessing private queue internals.
* Simulate backpressure (bounded capacity) or ordering edge cases.

Design references
-----------------
- Gamma et al. "Design Patterns" (GoF) — Strategy pattern: the queueing
  strategy is now injectable without changing Orchestrator.
- Martin "Clean Architecture" (2017) — Dependency Inversion Principle:
  high-level modules should not depend on low-level infrastructure details.
- asyncio.Queue docs — https://docs.python.org/3/library/asyncio-queue.html
- DESIGN.md §11 "orchestrator.py のインフラ依存を依存注入（DI）化"
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tmux_orchestrator.agents.base import Task


@runtime_checkable
class TaskQueue(Protocol):
    """Minimal async queue interface consumed by :class:`Orchestrator`.

    Implementors must provide all five methods below.  The default production
    implementation is :class:`AsyncPriorityTaskQueue`.
    """

    async def put(self, item: "tuple[int, int, Task]") -> None:
        """Enqueue *item*.  Blocks if the queue is full (bounded queues)."""
        ...

    async def get(self) -> "tuple[int, int, Task]":
        """Dequeue and return the next item.  Blocks if the queue is empty."""
        ...

    def task_done(self) -> None:
        """Signal that a previously dequeued item has been processed."""
        ...

    def empty(self) -> bool:
        """Return ``True`` if the queue currently contains no items."""
        ...

    def qsize(self) -> int:
        """Return the number of items currently in the queue."""
        ...

    def full(self) -> bool:
        """Return ``True`` if the queue has reached its maximum capacity."""
        ...


class AsyncPriorityTaskQueue:
    """Production :class:`TaskQueue` backed by :class:`asyncio.PriorityQueue`.

    Priority ordering: items are ``(priority, seq, task)`` tuples.  Lower
    ``priority`` values are dequeued first; ``seq`` is a monotonically
    increasing tie-breaker that prevents :mod:`heapq` from trying to compare
    :class:`~tmux_orchestrator.agents.base.Task` objects directly.

    Parameters
    ----------
    maxsize:
        Maximum number of items the queue may hold (``0`` = unbounded,
        matching the default behaviour of ``asyncio.PriorityQueue``).
    """

    def __init__(self, maxsize: int = 0) -> None:
        self._q: asyncio.PriorityQueue[tuple[int, int, Task]] = asyncio.PriorityQueue(
            maxsize=maxsize
        )

    async def put(self, item: "tuple[int, int, Task]") -> None:
        await self._q.put(item)

    async def get(self) -> "tuple[int, int, Task]":
        return await self._q.get()

    def task_done(self) -> None:
        self._q.task_done()

    def empty(self) -> bool:
        return self._q.empty()

    def qsize(self) -> int:
        return self._q.qsize()

    def full(self) -> bool:
        return self._q.full()

    # ------------------------------------------------------------------
    # Internal access shim — allows orchestrator to read/manipulate the
    # heap directly (for cancellation and reprioritisation operations).
    # These are deliberately NOT part of the TaskQueue Protocol because
    # they expose implementation details; mock implementations can expose
    # a simple list instead.
    # ------------------------------------------------------------------

    @property
    def _queue(self):  # type: ignore[return]
        """Return the underlying heap list (same attribute as asyncio.Queue)."""
        return self._q._queue  # type: ignore[attr-defined]

    @property
    def _unfinished_tasks(self) -> int:
        return self._q._unfinished_tasks  # type: ignore[attr-defined]

    @_unfinished_tasks.setter
    def _unfinished_tasks(self, value: int) -> None:
        self._q._unfinished_tasks = value  # type: ignore[attr-defined]

    @property
    def _finished(self):  # type: ignore[return]
        return self._q._finished  # type: ignore[attr-defined]
