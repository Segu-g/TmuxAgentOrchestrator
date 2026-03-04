"""Async in-process pub/sub message bus."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import AsyncIterator

logger = logging.getLogger(__name__)

BROADCAST = "*"  # to_id sentinel meaning "send to all"


class MessageType(str, Enum):
    TASK = "TASK"
    RESULT = "RESULT"
    STATUS = "STATUS"
    PEER_MSG = "PEER_MSG"
    CONTROL = "CONTROL"


@dataclass
class Message:
    type: MessageType
    from_id: str = ""
    to_id: str = BROADCAST
    payload: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "from_id": self.from_id,
            "to_id": self.to_id,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
        }


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
