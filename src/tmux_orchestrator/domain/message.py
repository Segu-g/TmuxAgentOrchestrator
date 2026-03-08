"""Pure domain types for inter-agent messaging.

This module has ZERO external dependencies — only Python stdlib is imported.
It is the authoritative definition of MessageType, Message, and BROADCAST.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

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
