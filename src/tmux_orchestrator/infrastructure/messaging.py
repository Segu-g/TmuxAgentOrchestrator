"""File-based mailbox for persistent agent messaging.

Infrastructure adapter for the filesystem-based message store.
This module is the canonical home for Mailbox; the old path
``tmux_orchestrator.messaging`` re-exports from here (Strangler Fig shim).

Layer: infrastructure (may depend on domain/application; must NOT be imported
by domain/ or application/).

References:
    - Cockburn, Alistair. "Hexagonal Architecture Explained" (2024)
      Output adapter: wraps an external system (filesystem) behind a stable interface.
    - Percival & Gregory, "Architecture Patterns with Python" (O'Reilly, 2020)
      Repository pattern: filesystem I/O belongs in the infrastructure layer.
    - DESIGN.md §10.N (v1.0.17 — infrastructure/ layer continued extraction)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tmux_orchestrator.bus import Message


class Mailbox:
    """Persistent per-agent message store.

    Directory layout::

        {root_dir}/{session_name}/{agent_id}/
            inbox/   {msg_id}.json   ← unread messages
            read/    {msg_id}.json   ← processed messages (after mark_read)
    """

    def __init__(self, root_dir: Path | str, session_name: str) -> None:
        self._root = Path(root_dir).expanduser() / session_name

    def _inbox(self, agent_id: str) -> Path:
        p = self._root / agent_id / "inbox"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _read_dir(self, agent_id: str) -> Path:
        p = self._root / agent_id / "read"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def write(self, agent_id: str, msg: "Message") -> Path:
        """Serialise *msg* to inbox/{msg.id}.json and return the path."""
        inbox = self._inbox(agent_id)
        path = inbox / f"{msg.id}.json"
        path.write_text(json.dumps(msg.to_dict(), indent=2))
        return path

    def read(self, agent_id: str, msg_id: str) -> dict:
        """Return the message dict for *msg_id* (checks inbox then read dir)."""
        for directory in (self._inbox(agent_id), self._read_dir(agent_id)):
            path = directory / f"{msg_id}.json"
            if path.exists():
                return json.loads(path.read_text())
        raise FileNotFoundError(f"Message {msg_id!r} not found for agent {agent_id!r}")

    def list_inbox(self, agent_id: str) -> list[str]:
        """Return sorted list of unread message IDs."""
        inbox = self._inbox(agent_id)
        return sorted(p.stem for p in inbox.glob("*.json"))

    def mark_read(self, agent_id: str, msg_id: str) -> None:
        """Move *msg_id* from inbox/ to read/."""
        src = self._inbox(agent_id) / f"{msg_id}.json"
        if not src.exists():
            return
        dst = self._read_dir(agent_id) / f"{msg_id}.json"
        shutil.move(str(src), str(dst))
