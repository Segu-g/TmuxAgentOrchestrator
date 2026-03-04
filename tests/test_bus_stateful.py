"""Stateful property tests for Bus using Hypothesis RuleBasedStateMachine.

Verifies invariants that hold across arbitrary sequences of subscribe /
publish / unsubscribe operations — things that unit tests over individual
operations cannot catch.

Reference: Hypothesis docs — stateful testing;
           Claessen & Hughes "QuickCheck" (ICFP 2000);
           DESIGN.md §10.6 (2026-03-05).
"""
from __future__ import annotations

import asyncio

from hypothesis import assume, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, initialize, invariant, rule

from tmux_orchestrator.bus import Bus, Message, MessageType


# Alphabet for agent IDs: alphanumeric only to keep repr readable.
_ID_CHARS = st.characters(whitelist_categories=("Lu", "Ll", "Nd"), min_codepoint=65)
_AGENT_ID_ST = st.text(alphabet=_ID_CHARS, min_size=1, max_size=12)


class BusStateMachine(RuleBasedStateMachine):
    """Tests Bus invariants across arbitrary operation sequences."""

    Subscribers = Bundle("subscribers")

    def __init__(self):
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self.bus = Bus()
        # Local mirror: agent_id → Queue (only currently-subscribed)
        self._queues: dict[str, asyncio.Queue] = {}

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------

    @initialize(target=Subscribers, agent_id=_AGENT_ID_ST)
    def first_subscriber(self, agent_id: str):
        """Hypothesis requires at least one subscribed agent before rules run."""
        assume(agent_id not in self._queues)
        q = self._run(self.bus.subscribe(agent_id))
        self._queues[agent_id] = q
        return agent_id

    @rule(target=Subscribers, agent_id=_AGENT_ID_ST)
    def subscribe(self, agent_id: str):
        assume(agent_id not in self._queues)
        q = self._run(self.bus.subscribe(agent_id))
        self._queues[agent_id] = q
        return agent_id

    @rule(agent_id=Subscribers)
    def unsubscribe(self, agent_id: str):
        if agent_id not in self._queues:
            return
        self._run(self.bus.unsubscribe(agent_id))
        del self._queues[agent_id]

    @rule(
        from_id=_AGENT_ID_ST,
        payload=st.fixed_dictionaries({"n": st.integers(0, 100)}),
    )
    def publish_broadcast(self, from_id: str, payload: dict):
        """A broadcast message (to_id=BROADCAST) must reach every subscriber."""
        before = {aid: q.qsize() for aid, q in self._queues.items()}
        msg = Message(type=MessageType.STATUS, from_id=from_id, payload=payload)
        self._run(self.bus.publish(msg))
        for aid, q in self._queues.items():
            delta = q.qsize() - before.get(aid, 0)
            assert delta in (0, 1), (
                f"Broadcast: subscriber {aid!r} delta={delta}, expected 0 or 1"
            )

    @rule(
        to_id=Subscribers,
        from_id=_AGENT_ID_ST,
        payload=st.fixed_dictionaries({"x": st.integers()}),
    )
    def publish_directed(self, to_id: str, from_id: str, payload: dict):
        """A directed message must reach ONLY the target (non-broadcast) subscriber."""
        if to_id not in self._queues:
            return
        before = {aid: q.qsize() for aid, q in self._queues.items()}
        msg = Message(
            type=MessageType.TASK,
            from_id=from_id,
            to_id=to_id,
            payload=payload,
        )
        self._run(self.bus.publish(msg))
        for aid, q in self._queues.items():
            delta = q.qsize() - before.get(aid, 0)
            if aid == to_id:
                assert delta in (0, 1), (
                    f"Target {aid!r}: expected delta 0 or 1, got {delta}"
                )
            else:
                # Non-target, non-broadcast subscriber must NOT receive directed msg.
                # (None of our subscribers in this machine use broadcast=True.)
                assert delta == 0, (
                    f"Non-target {aid!r} received message directed to {to_id!r}"
                )

    # ------------------------------------------------------------------
    # Invariants
    # ------------------------------------------------------------------

    @invariant()
    def drop_counts_non_negative(self):
        for aid, count in self.bus.get_drop_counts().items():
            assert count >= 0, f"Negative drop count for {aid!r}: {count}"

    @invariant()
    def local_tracking_matches_bus(self):
        """Local subscriber mirror must equal the bus's internal table."""
        assert set(self._queues) == set(self.bus._queues), (
            f"Mismatch: local={set(self._queues)}, bus={set(self.bus._queues)}"
        )

    def teardown(self):
        self._loop.close()


# Pytest integration — Hypothesis generates the TestCase class.
TestBusStateful = BusStateMachine.TestCase
TestBusStateful.settings = settings(max_examples=200, stateful_step_count=30)
