"""Async in-process pub/sub message bus.

Canonical location: tmux_orchestrator.application.bus

Pure application-layer component — no tmux, no HTTP, no filesystem.
Depends only on domain/ types (Message, MessageType, BROADCAST) and stdlib.

The root-level shim ``bus.py`` re-exports everything from here for backward compat.

Design references:
- Percival, Gregory "Architecture Patterns with Python", O'Reilly Ch.8
  — Events and the Message Bus (2020)
- Hash Block, "How I Built an In-Memory Pub/Sub Engine in Python With Only 80 Lines",
  Medium (2024)
- DESIGN.md §10.56 (v1.1.24 — Clean Architecture Phase 2).
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

# Backward-compat shims — MessageType, Message, and BROADCAST now live in domain/
# These re-exports preserve all existing import paths unchanged.
from tmux_orchestrator.domain.message import BROADCAST, Message, MessageType  # noqa: F401

logger = logging.getLogger(__name__)


class Bus:
    """Fan-out async message bus.

    Subscribers register with an *agent_id*.  When a message is published:
    - If ``msg.to_id == BROADCAST`` it is delivered to every subscriber.
    - Otherwise it is delivered only to the subscriber whose id matches
      ``msg.to_id``, plus any ``BROADCAST`` subscribers (e.g. loggers/web hub).
    """

    def __init__(self) -> None:
        # agent_id → queue
        self._queues: dict[str, asyncio.Queue[Message]] = {}
        self._broadcast_queues: set[str] = set()
        self._lock = asyncio.Lock()
        self._drop_counts: dict[str, int] = {}  # subscriber_id → count of dropped messages

    async def subscribe(
        self, agent_id: str, *, broadcast: bool = False, maxsize: int = 256
    ) -> asyncio.Queue[Message]:
        """Register *agent_id* and return its message queue.

        Pass ``broadcast=True`` to receive all messages regardless of to_id.
        """
        async with self._lock:
            q: asyncio.Queue[Message] = asyncio.Queue(maxsize=maxsize)
            self._queues[agent_id] = q
            if broadcast:
                self._broadcast_queues.add(agent_id)
        return q

    async def unsubscribe(self, agent_id: str) -> None:
        async with self._lock:
            self._queues.pop(agent_id, None)
            self._broadcast_queues.discard(agent_id)

    async def publish(self, msg: Message) -> None:
        """Publish *msg* to all relevant subscribers (non-blocking)."""
        async with self._lock:
            queues = list(self._queues.items())
            broadcast_ids = set(self._broadcast_queues)

        for sub_id, q in queues:
            if (
                msg.to_id == BROADCAST
                or msg.to_id == sub_id
                or sub_id in broadcast_ids
            ):
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    self._drop_counts[sub_id] = self._drop_counts.get(sub_id, 0) + 1
                    logger.warning(
                        "Bus queue full for subscriber %s — dropped message (total drops: %d)",
                        sub_id,
                        self._drop_counts[sub_id],
                    )

    def get_drop_counts(self) -> dict[str, int]:
        """Return a snapshot of per-subscriber message drop counts."""
        return dict(self._drop_counts)

    async def iter_messages(self, queue: asyncio.Queue[Message]) -> AsyncIterator[Message]:
        """Async-iterate over messages from a previously subscribed queue."""
        while True:
            msg = await queue.get()
            yield msg
            queue.task_done()


__all__ = ["Bus", "BROADCAST", "Message", "MessageType"]
