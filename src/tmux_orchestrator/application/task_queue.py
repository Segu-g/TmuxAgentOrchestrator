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
import dataclasses
import heapq
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tmux_orchestrator.domain.task import Task


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

    Dynamic priority update support (v1.2.6):
    Uses the **lazy-deletion** pattern from the Python heapq documentation
    "Priority Queue Implementation Notes":
    - ``_pending`` maps ``task_id`` → ``(priority, seq, task)`` for all live
      (not-yet-dispatched) items.
    - ``_deleted_seqs`` is a set of sequence numbers whose heap entries are stale
      (superseded by a priority re-insertion).  ``get()`` silently skips them.
    - ``update_priority()`` marks the old seq as deleted, re-inserts with a new seq.
    - Using seq (not task_id) as the staleness key ensures the freshly re-inserted
      entry (same task_id, new seq) is never mistakenly skipped.

    References:
    - Python heapq docs "Priority Queue Implementation Notes"
      https://docs.python.org/3/library/heapq.html
    - Sedgewick & Wayne "Algorithms" 4th ed. §2.4 — Priority Queues
    - Liu & Layland (1973) "Scheduling Algorithms for Multiprogramming in a
      Hard Real-Time Environment". JACM 20(1).
    - DESIGN.md §10.82 — v1.2.6 Dynamic Task Priority Update

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
        # Lazy-deletion support for update_priority():
        # _pending maps task_id → (priority, seq, task) for items currently in the queue.
        # _deleted_seqs holds *sequence numbers* of stale heap entries superseded by
        # a priority re-insertion.  Using seq (not task_id) ensures the re-inserted
        # entry (which has the same task_id but a different seq) is NOT skipped.
        # Reference: Python heapq docs "Priority Queue Implementation Notes"
        #   https://docs.python.org/3/library/heapq.html
        self._pending: dict[str, tuple[int, int, "Task"]] = {}
        self._deleted_seqs: set[int] = set()
        self._update_seq: int = 0  # counter for unique seq numbers on re-insertion

    async def put(self, item: "tuple[int, int, Task]") -> None:
        priority, seq, task = item
        self._pending[task.id] = item
        await self._q.put(item)

    async def get(self) -> "tuple[int, int, Task]":
        while True:
            item = await self._q.get()
            _priority, seq, task = item
            if seq in self._deleted_seqs:
                # This entry was superseded by a priority update — discard and continue.
                self._deleted_seqs.discard(seq)
                # We must call task_done to keep the internal counter consistent.
                self._q.task_done()
                continue
            # Remove from _pending now that it has been dispatched.
            self._pending.pop(task.id, None)
            return item

    def task_done(self) -> None:
        self._q.task_done()

    def empty(self) -> bool:
        return len(self._pending) == 0

    def qsize(self) -> int:
        """Return the number of *live* (non-deleted) items in the queue."""
        return len(self._pending)

    def full(self) -> bool:
        return self._q.full()

    # ------------------------------------------------------------------
    # Priority update — lazy deletion pattern
    # Reference: Python heapq docs "Priority Queue Implementation Notes"
    #   https://docs.python.org/3/library/heapq.html
    # Reference: Sedgewick & Wayne "Algorithms" 4th ed. §2.4
    # ------------------------------------------------------------------

    def update_priority(self, task_id: str, new_priority: int) -> bool:
        """Update the priority of a pending (not yet dispatched) task.

        Uses the lazy-deletion pattern:
        1. Look up the task in ``_pending``.
        2. Mark the old heap entry as deleted via ``_deleted_seqs``.
        3. Create an updated Task with the new priority.
        4. Re-insert into the heap with the new priority using ``heapq.heappush``.

        Returns ``True`` if found and updated; ``False`` if the task is not
        currently pending (already dispatched, completed, or unknown).

        This method is synchronous because it only manipulates the internal
        heap data structure directly — no awaiting is required.  Thread safety
        is the caller's responsibility (single asyncio event loop assumed).

        Design reference: DESIGN.md §10.82 — v1.2.6 Dynamic Task Priority Update.
        """
        if task_id not in self._pending:
            return False

        old_priority, old_seq, task = self._pending[task_id]

        # Mark the OLD heap entry's sequence number as stale — get() will discard it.
        # Using seq (not task_id) ensures the re-inserted entry is not mistakenly skipped.
        self._deleted_seqs.add(old_seq)

        # Build an updated Task with the new priority.
        # Use a NEW sequence number so the new entry is distinguishable from the old one.
        # We use negative values to avoid collisions with positive seq numbers from put().
        self._update_seq -= 1
        new_seq = self._update_seq
        updated_task = dataclasses.replace(task, priority=new_priority)
        new_item = (new_priority, new_seq, updated_task)

        # Update _pending immediately so callers see the new priority.
        self._pending[task_id] = new_item

        # Push directly onto the internal heap to avoid the async boundary.
        # asyncio.PriorityQueue._queue is the heapq list.
        heapq.heappush(self._q._queue, new_item)  # type: ignore[attr-defined]
        # Adjust the internal unfinished_tasks counter: we added one entry to
        # the raw heap but didn't call put(), so we account for it manually.
        self._q._unfinished_tasks += 1  # type: ignore[attr-defined]
        # Signal the not-empty condition so any waiting get() is woken up.
        self._q._finished.clear()  # type: ignore[attr-defined]
        self._q._wakeup_next(self._q._getters)  # type: ignore[attr-defined]

        return True

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
