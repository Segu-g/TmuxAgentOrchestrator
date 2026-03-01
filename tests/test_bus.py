"""Tests for the async message bus."""

from __future__ import annotations

import asyncio

import pytest

from tmux_orchestrator.bus import BROADCAST, Bus, Message, MessageType


@pytest.fixture
def bus() -> Bus:
    return Bus()


async def test_broadcast_delivery(bus: Bus) -> None:
    """A BROADCAST message reaches every subscriber."""
    q_a = await bus.subscribe("agent-a")
    q_b = await bus.subscribe("agent-b")

    msg = Message(type=MessageType.STATUS, from_id="system", payload={"x": 1})
    await bus.publish(msg)

    assert q_a.qsize() == 1
    assert q_b.qsize() == 1
    m_a = q_a.get_nowait()
    assert m_a.id == msg.id


async def test_directed_delivery(bus: Bus) -> None:
    """A directed message reaches only the target subscriber."""
    q_a = await bus.subscribe("agent-a")
    q_b = await bus.subscribe("agent-b")

    msg = Message(type=MessageType.TASK, from_id="orch", to_id="agent-a", payload={})
    await bus.publish(msg)

    assert q_a.qsize() == 1
    assert q_b.qsize() == 0


async def test_broadcast_subscriber_receives_all(bus: Bus) -> None:
    """A subscriber registered with broadcast=True receives directed messages too."""
    q_hub = await bus.subscribe("hub", broadcast=True)
    q_agent = await bus.subscribe("agent-x")

    msg = Message(type=MessageType.RESULT, from_id="agent-x", to_id="agent-x", payload={})
    await bus.publish(msg)

    # Both hub (broadcast) and agent-x (directed) should receive it
    assert q_hub.qsize() == 1
    assert q_agent.qsize() == 1


async def test_unsubscribe(bus: Bus) -> None:
    """Unsubscribed agent no longer receives messages."""
    q = await bus.subscribe("gone")
    await bus.unsubscribe("gone")

    msg = Message(type=MessageType.STATUS, from_id="orch", payload={})
    await bus.publish(msg)

    assert q.qsize() == 0


async def test_queue_full_drops_message(bus: Bus) -> None:
    """When a subscriber's queue is full the message is dropped without error."""
    q = await bus.subscribe("slow", maxsize=1)
    # Fill the queue
    await bus.publish(Message(type=MessageType.STATUS, from_id="x", payload={}))
    # This should not raise
    await bus.publish(Message(type=MessageType.STATUS, from_id="x", payload={}))
    assert q.qsize() == 1  # still 1; second was dropped


async def test_message_iter(bus: Bus) -> None:
    """iter_messages yields messages in order."""
    q = await bus.subscribe("consumer")
    payloads = [{"n": i} for i in range(3)]
    for p in payloads:
        await bus.publish(Message(type=MessageType.STATUS, from_id="src", payload=p))

    received = []
    async for msg in bus.iter_messages(q):
        received.append(msg.payload["n"])
        if len(received) == 3:
            break

    assert received == [0, 1, 2]
