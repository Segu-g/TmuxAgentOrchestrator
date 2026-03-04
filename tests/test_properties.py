"""Property-based tests for the Bus and Orchestrator invariants.

Uses Hypothesis to generate adversarial inputs and verify that core
invariants hold regardless of message content, subscriber ordering, or
task priority values.

Reference: Hypothesis documentation (https://hypothesis.readthedocs.io/),
           TLA+ community practice of specifying invariants before testing.
"""
from __future__ import annotations

import asyncio
import secrets
import string
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tmux_orchestrator.agents.base import AgentStatus, Task
from tmux_orchestrator.bus import BROADCAST, Bus, Message, MessageType
from tmux_orchestrator.circuit_breaker import BreakerState, CircuitBreaker
from tmux_orchestrator.schemas import parse_result_payload, parse_status_payload


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

agent_id = st.text(
    alphabet=string.ascii_lowercase + string.digits + "-",
    min_size=1,
    max_size=32,
)

printable_text = st.text(alphabet=string.printable, min_size=0, max_size=500)

priority = st.integers(min_value=0, max_value=100)

task_metadata = st.dictionaries(
    st.text(alphabet=string.ascii_letters, min_size=1, max_size=16),
    st.one_of(st.integers(), st.text(min_size=0, max_size=64), st.booleans()),
    max_size=5,
)


# ---------------------------------------------------------------------------
# Task invariants
# ---------------------------------------------------------------------------


@given(
    tid=st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=64),
    prompt=printable_text,
    prio=priority,
    meta=task_metadata,
)
def test_task_trace_id_always_16_hex_chars(tid, prompt, prio, meta):
    """Task.trace_id is always a 16-character hex string (8 bytes)."""
    t = Task(id=tid, prompt=prompt, priority=prio, metadata=meta)
    assert len(t.trace_id) == 16
    assert all(c in "0123456789abcdef" for c in t.trace_id)


@given(
    prompts=st.lists(printable_text, min_size=2, max_size=10),
)
def test_task_trace_ids_are_unique_across_instances(prompts):
    """Independently created Tasks always have distinct trace_ids."""
    tasks = [Task(id=f"t{i}", prompt=p) for i, p in enumerate(prompts)]
    ids = [t.trace_id for t in tasks]
    assert len(ids) == len(set(ids)), f"Duplicate trace_ids found: {ids}"


@given(a_prio=priority, b_prio=priority)
def test_task_ordering_consistent_with_priority(a_prio, b_prio):
    """Lower priority value means higher dispatch urgency (Task.__lt__)."""
    a = Task(id="a", prompt="x", priority=a_prio)
    b = Task(id="b", prompt="y", priority=b_prio)
    if a_prio < b_prio:
        assert a < b
    elif a_prio > b_prio:
        assert b < a
    else:
        assert not (a < b) and not (b < a)


# ---------------------------------------------------------------------------
# CircuitBreaker invariants
# ---------------------------------------------------------------------------


@given(failures=st.integers(min_value=0, max_value=20), threshold=st.integers(min_value=1, max_value=5))
def test_circuit_opens_exactly_at_threshold(failures, threshold):
    """After exactly `threshold` failures, state transitions to OPEN."""
    cb = CircuitBreaker("agent-x", failure_threshold=threshold, recovery_timeout=9999.0)
    for _ in range(failures):
        cb.record_failure()
    if failures >= threshold:
        assert cb.state == BreakerState.OPEN
    else:
        assert cb.state == BreakerState.CLOSED


@given(threshold=st.integers(min_value=1, max_value=10))
def test_success_in_closed_never_opens_breaker(threshold):
    """Recording successes in CLOSED state never transitions to OPEN."""
    cb = CircuitBreaker("agent-x", failure_threshold=threshold, recovery_timeout=9999.0)
    for _ in range(threshold * 3):
        cb.record_success()
    assert cb.state == BreakerState.CLOSED


# ---------------------------------------------------------------------------
# Schema parsing invariants
# ---------------------------------------------------------------------------


@given(
    task_id=st.text(min_size=1, max_size=64),
    output=st.one_of(st.none(), printable_text),
    error=st.one_of(st.none(), printable_text),
    extra=task_metadata,
)
def test_parse_result_payload_never_raises(task_id, output, error, extra):
    """parse_result_payload should never raise on any dict that has task_id."""
    payload: dict[str, Any] = {"task_id": task_id}
    if output is not None:
        payload["output"] = output
    if error is not None:
        payload["error"] = error
    payload.update(extra)
    result = parse_result_payload(payload)
    assert result.task_id == task_id


@given(extra=task_metadata)
def test_parse_status_payload_unknown_event_never_raises(extra):
    """Unknown events fall back to the base model and never raise."""
    from pydantic import ValidationError
    payload = {"event": "unknown_event_xyz_hypothesis", **extra}
    result = parse_status_payload(payload)
    assert result is not None


def test_parse_status_payload_known_event_requires_fields():
    """Known events raise ValidationError when required fields are absent."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        parse_status_payload({"event": "task_queued"})  # missing task_id, prompt


def test_parse_status_payload_known_event_succeeds_with_valid_data():
    """Known events succeed when all required fields are present."""
    result = parse_status_payload({"event": "task_queued", "task_id": "t1", "prompt": "hi"})
    assert result.event == "task_queued"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Bus invariants (synchronous helpers where possible)
# ---------------------------------------------------------------------------


def test_bus_drop_counts_monotonically_increase(tmp_path):
    """Drop counts never decrease; they only increase or stay the same."""
    import asyncio

    async def _inner():
        bus = Bus()
        # Subscribe with tiny queue to force drops
        q = await bus.subscribe("small-agent", maxsize=2)
        msg = Message(type=MessageType.STATUS, from_id="x", payload={"event": "test"})
        # Publish more than maxsize messages
        for _ in range(10):
            await bus.publish(msg)
        drops = bus.get_drop_counts().get("small-agent", 0)
        assert drops >= 0
        # Publish more and verify counts never decrease
        for _ in range(5):
            await bus.publish(msg)
        drops2 = bus.get_drop_counts().get("small-agent", 0)
        assert drops2 >= drops

    asyncio.run(_inner())


@given(
    sub_ids=st.lists(
        st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=16),
        min_size=1,
        max_size=10,
        unique=True,
    )
)
@settings(max_examples=20)
def test_bus_subscribe_unsubscribe_no_leak(sub_ids):
    """After unsubscribing all subscribers, bus internal state is empty."""
    async def _inner():
        bus = Bus()
        for sid in sub_ids:
            await bus.subscribe(sid)
        for sid in sub_ids:
            await bus.unsubscribe(sid)
        assert len(bus._queues) == 0
        assert len(bus._broadcast_queues) == 0

    asyncio.run(_inner())
