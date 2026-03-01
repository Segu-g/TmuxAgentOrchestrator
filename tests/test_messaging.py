"""Unit tests for the Mailbox class."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tmux_orchestrator.bus import Message, MessageType
from tmux_orchestrator.messaging import Mailbox


@pytest.fixture
def mailbox(tmp_path: Path) -> Mailbox:
    return Mailbox(root_dir=tmp_path, session_name="test-session")


def _make_msg(to_id: str = "agent-b", text: str = "hello") -> Message:
    return Message(
        type=MessageType.PEER_MSG,
        from_id="agent-a",
        to_id=to_id,
        payload={"text": text},
    )


class TestMailboxWrite:
    def test_write_creates_file(self, mailbox: Mailbox) -> None:
        msg = _make_msg()
        path = mailbox.write("agent-b", msg)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["id"] == msg.id
        assert data["payload"]["text"] == "hello"

    def test_write_in_inbox(self, mailbox: Mailbox) -> None:
        msg = _make_msg()
        path = mailbox.write("agent-b", msg)
        assert "inbox" in str(path)


class TestMailboxRead:
    def test_read_from_inbox(self, mailbox: Mailbox) -> None:
        msg = _make_msg()
        mailbox.write("agent-b", msg)
        data = mailbox.read("agent-b", msg.id)
        assert data["id"] == msg.id

    def test_read_missing_raises(self, mailbox: Mailbox) -> None:
        with pytest.raises(FileNotFoundError):
            mailbox.read("agent-b", "nonexistent-id")

    def test_read_after_mark_read(self, mailbox: Mailbox) -> None:
        msg = _make_msg()
        mailbox.write("agent-b", msg)
        mailbox.mark_read("agent-b", msg.id)
        # Should still be readable from the read/ dir
        data = mailbox.read("agent-b", msg.id)
        assert data["id"] == msg.id


class TestMailboxListInbox:
    def test_empty_inbox(self, mailbox: Mailbox) -> None:
        assert mailbox.list_inbox("agent-b") == []

    def test_lists_messages(self, mailbox: Mailbox) -> None:
        msg1 = _make_msg()
        msg2 = _make_msg(text="world")
        mailbox.write("agent-b", msg1)
        mailbox.write("agent-b", msg2)
        ids = mailbox.list_inbox("agent-b")
        assert set(ids) == {msg1.id, msg2.id}

    def test_mark_read_removes_from_inbox(self, mailbox: Mailbox) -> None:
        msg = _make_msg()
        mailbox.write("agent-b", msg)
        mailbox.mark_read("agent-b", msg.id)
        assert mailbox.list_inbox("agent-b") == []


class TestMailboxMarkRead:
    def test_mark_read_moves_file(self, mailbox: Mailbox, tmp_path: Path) -> None:
        msg = _make_msg()
        mailbox.write("agent-b", msg)
        mailbox.mark_read("agent-b", msg.id)
        read_path = tmp_path / "test-session" / "agent-b" / "read" / f"{msg.id}.json"
        assert read_path.exists()

    def test_mark_read_nonexistent_noop(self, mailbox: Mailbox) -> None:
        # Should not raise
        mailbox.mark_read("agent-b", "nonexistent-id")
