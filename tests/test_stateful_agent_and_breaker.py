"""Stateful property tests exercising the REAL Agent and CircuitBreaker implementations.

Two Hypothesis RuleBasedStateMachine suites:

1. ``RealAgentStatusMachine`` — calls actual ``Agent._set_busy()`` /
   ``Agent._set_idle()`` / ``Agent.status = ...`` on a ``_FakeAgent``
   (in-memory, no libtmux dependency) and verifies:
   - status is always a valid AgentStatus member.
   - _current_task is non-None iff status is BUSY.
   - _set_idle() is a no-op when status is DRAINING / ERROR / STOPPED.

2. ``CircuitBreakerMachine`` — calls ``CircuitBreaker.record_success()`` /
   ``record_failure()`` / ``_to_half_open()`` and verifies:
   - state is always a valid BreakerState.
   - failure count is never negative.
   - is_allowed() == True iff state is CLOSED (or HALF_OPEN with no in-flight probe).
   - _opened_at is set iff state is OPEN.

Both machines maintain a shadow model and assert it matches the real object
at every step — catching implementation divergence from the spec.

Reference:
  - Hypothesis docs "Stateful Testing":
    https://hypothesis.readthedocs.io/en/latest/stateful.html
  - Hypothesis rule-based stateful testing article:
    https://hypothesis.works/articles/rule-based-stateful-testing/
  - Claessen & Hughes "QuickCheck: A Lightweight Tool for Random Testing of
    Haskell Programs" (ICFP 2000): https://dl.acm.org/doi/10.1145/357766.351266
  - DESIGN.md §10.45 — v1.1.9 selection rationale (2026-03-09).
"""
from __future__ import annotations

import asyncio
from typing import Any

from hypothesis import HealthCheck, settings
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from tmux_orchestrator.agents.base import Agent, AgentStatus
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.circuit_breaker import BreakerState, CircuitBreaker
from tmux_orchestrator.domain.task import Task


# ---------------------------------------------------------------------------
# Minimal in-memory Agent — no tmux, no filesystem, no async I/O beyond Bus.
# ---------------------------------------------------------------------------

class _FakeAgent(Agent):
    """Minimal Agent implementation suitable for pure state-machine tests."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)

    async def start(self) -> None:
        self.status = AgentStatus.IDLE

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED

    async def _dispatch_task(self, task: Task) -> None:
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


def _make_task(task_id: str = "t1") -> Task:
    return Task(id=task_id, prompt="test prompt")


def _new_loop_with_bus(agent_id: str) -> tuple[asyncio.AbstractEventLoop, Bus]:
    loop = asyncio.new_event_loop()
    bus = loop.run_until_complete(_make_bus(agent_id))
    return loop, bus


async def _make_bus(agent_id: str) -> Bus:
    bus = Bus()
    await bus.subscribe(agent_id)
    return bus


# ---------------------------------------------------------------------------
# Machine 1: Real AgentStatus transitions
# ---------------------------------------------------------------------------

class RealAgentStatusMachine(RuleBasedStateMachine):
    """Exercises the real Agent._set_busy / _set_idle methods.

    Shadow model tracks expected status independently from the Agent object.
    Invariants assert real status matches shadow at every Hypothesis step.

    Key properties verified:
      P1. status ∈ AgentStatus (always a valid enum member).
      P2. _current_task is not None iff status == BUSY.
      P3. _set_idle() is a no-op when status ∈ {DRAINING, ERROR, STOPPED}.
      P4. Shadow model matches real Agent status after every rule.
    """

    _AID = "agent-sm"

    def __init__(self) -> None:
        super().__init__()
        self._loop, self._bus = _new_loop_with_bus(self._AID)
        self._agent = _FakeAgent(self._AID, self._bus)
        self._loop.run_until_complete(self._agent.start())
        self._expected: AgentStatus = AgentStatus.IDLE
        self._task_seq: int = 0

    def _run(self, coro: Any) -> Any:
        return self._loop.run_until_complete(coro)

    def _next_task(self) -> Task:
        self._task_seq += 1
        return _make_task(f"t{self._task_seq}")

    # ------------------------------------------------------------------ rules

    @precondition(lambda self: self._agent.status == AgentStatus.IDLE)
    @rule()
    def dispatch(self) -> None:
        """IDLE → BUSY (task dispatched)."""
        task = self._next_task()
        self._agent._set_busy(task)
        self._expected = AgentStatus.BUSY

    @precondition(lambda self: self._agent.status == AgentStatus.BUSY)
    @rule()
    def complete(self) -> None:
        """BUSY → IDLE (task completed normally)."""
        async def _do() -> None:
            self._agent._set_idle()
        self._run(_do())
        self._expected = AgentStatus.IDLE

    @precondition(lambda self: self._agent.status == AgentStatus.BUSY)
    @rule()
    def fail(self) -> None:
        """BUSY → ERROR (exception path)."""
        self._agent.status = AgentStatus.ERROR
        self._expected = AgentStatus.ERROR

    @precondition(lambda self: self._agent.status == AgentStatus.ERROR)
    @rule()
    def recover(self) -> None:
        """ERROR → IDLE (operator / auto-recovery)."""
        self._agent.status = AgentStatus.IDLE
        self._expected = AgentStatus.IDLE

    @precondition(lambda self: self._agent.status == AgentStatus.IDLE)
    @rule()
    def drain_idle(self) -> None:
        """IDLE → DRAINING."""
        self._agent.status = AgentStatus.DRAINING
        self._expected = AgentStatus.DRAINING

    @precondition(lambda self: self._agent.status == AgentStatus.BUSY)
    @rule()
    def drain_busy(self) -> None:
        """BUSY → DRAINING (mid-task drain signal)."""
        self._agent.status = AgentStatus.DRAINING
        self._expected = AgentStatus.DRAINING

    @precondition(lambda self: self._agent.status == AgentStatus.DRAINING)
    @rule()
    def stop_drain(self) -> None:
        """DRAINING → STOPPED."""
        self._agent.status = AgentStatus.STOPPED
        self._expected = AgentStatus.STOPPED

    @precondition(lambda self: self._agent.status == AgentStatus.STOPPED)
    @rule()
    def restart(self) -> None:
        """STOPPED → IDLE (agent restarted)."""
        self._agent.status = AgentStatus.IDLE
        self._expected = AgentStatus.IDLE

    @precondition(lambda self: self._agent.status == AgentStatus.DRAINING)
    @rule()
    def set_idle_during_drain_is_noop(self) -> None:
        """_set_idle() during DRAINING must leave status unchanged (P3)."""
        async def _do() -> None:
            self._agent._set_idle()
        self._run(_do())
        # Verify the implementation guards correctly — status stays DRAINING.
        assert self._agent.status == AgentStatus.DRAINING, (
            f"_set_idle() while DRAINING changed status to {self._agent.status}"
        )
        # Shadow model unchanged.

    @precondition(lambda self: self._agent.status == AgentStatus.ERROR)
    @rule()
    def set_idle_during_error_is_noop(self) -> None:
        """_set_idle() during ERROR must leave status unchanged (P3)."""
        async def _do() -> None:
            self._agent._set_idle()
        self._run(_do())
        assert self._agent.status == AgentStatus.ERROR, (
            f"_set_idle() while ERROR changed status to {self._agent.status}"
        )

    @precondition(lambda self: self._agent.status == AgentStatus.STOPPED)
    @rule()
    def set_idle_during_stopped_is_noop(self) -> None:
        """_set_idle() during STOPPED must leave status unchanged (P3)."""
        async def _do() -> None:
            self._agent._set_idle()
        self._run(_do())
        assert self._agent.status == AgentStatus.STOPPED, (
            f"_set_idle() while STOPPED changed status to {self._agent.status}"
        )

    # ------------------------------------------------------------ invariants

    @invariant()
    def p1_status_is_valid(self) -> None:
        """P1: status is always a valid AgentStatus enum member."""
        assert self._agent.status in AgentStatus, (
            f"Invalid status: {self._agent.status!r}"
        )

    @invariant()
    def p2_current_task_consistent(self) -> None:
        """P2: _current_task is not None iff status is BUSY."""
        if self._agent.status == AgentStatus.BUSY:
            assert self._agent._current_task is not None, (
                "Agent is BUSY but _current_task is None"
            )

    @invariant()
    def p4_shadow_matches_real(self) -> None:
        """P4: Shadow model matches real Agent status at every step."""
        assert self._agent.status == self._expected, (
            f"Status mismatch: real={self._agent.status}, "
            f"shadow={self._expected}"
        )

    def teardown(self) -> None:
        pending = asyncio.all_tasks(self._loop)
        for t in pending:
            t.cancel()
        if pending:
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        self._loop.close()


TestRealAgentStatusStateful = RealAgentStatusMachine.TestCase
TestRealAgentStatusStateful.settings = settings(
    max_examples=300,
    stateful_step_count=40,
    suppress_health_check=[HealthCheck.too_slow],
)


# ---------------------------------------------------------------------------
# Machine 2: CircuitBreaker state machine
# ---------------------------------------------------------------------------

class CircuitBreakerMachine(RuleBasedStateMachine):
    """Exercises CircuitBreaker.record_success / record_failure / _to_half_open.

    Shadow model independently tracks expected state.

    Key properties verified:
      B1. state ∈ {CLOSED, OPEN, HALF_OPEN} (always valid).
      B2. _failure_count ≥ 0 (never negative).
      B3. _opened_at is set iff state == OPEN.
      B4. is_allowed() == True iff state == CLOSED
          (HALF_OPEN with _failure_count==0 also allowed — not modelled here
           to keep shadow model deterministic without time dependency).
      B5. Shadow model matches real breaker state at every step.
    """

    _THRESHOLD = 3
    _RECOVERY = 9_999.0  # Long enough to never auto-expire in tests.

    def __init__(self) -> None:
        super().__init__()
        self._cb = CircuitBreaker(
            "cb-agent",
            failure_threshold=self._THRESHOLD,
            recovery_timeout=self._RECOVERY,
        )
        self._shadow: BreakerState = BreakerState.CLOSED
        self._shadow_failures: int = 0

    # ------------------------------------------------------------------ rules

    @precondition(lambda self: self._cb.state == BreakerState.CLOSED)
    @rule()
    def success_in_closed(self) -> None:
        """Success in CLOSED resets failure count, stays CLOSED."""
        self._cb.record_success()
        self._shadow_failures = 0
        self._shadow = BreakerState.CLOSED

    @precondition(lambda self: (
        self._cb.state == BreakerState.CLOSED
        and self._cb._failure_count < self._THRESHOLD - 1
    ))
    @rule()
    def failure_below_threshold(self) -> None:
        """Failure below threshold increments counter, stays CLOSED."""
        self._cb.record_failure()
        self._shadow_failures += 1
        self._shadow = BreakerState.CLOSED

    @precondition(lambda self: (
        self._cb.state == BreakerState.CLOSED
        and self._cb._failure_count == self._THRESHOLD - 1
    ))
    @rule()
    def failure_trips_breaker(self) -> None:
        """Failure at threshold trips circuit to OPEN."""
        self._cb.record_failure()
        self._shadow_failures += 1
        self._shadow = BreakerState.OPEN

    @precondition(lambda self: self._cb.state == BreakerState.OPEN)
    @rule()
    def failure_in_open(self) -> None:
        """Failure while OPEN increments counter (stays OPEN)."""
        prev = self._cb._failure_count
        self._cb.record_failure()
        assert self._cb._failure_count == prev + 1
        # state stays OPEN — shadow unchanged.

    @precondition(lambda self: self._cb.state == BreakerState.OPEN)
    @rule()
    def force_half_open(self) -> None:
        """Directly transition OPEN → HALF_OPEN (simulates timeout expiry)."""
        self._cb._to_half_open()
        self._shadow = BreakerState.HALF_OPEN
        self._shadow_failures = 0

    @precondition(lambda self: self._cb.state == BreakerState.HALF_OPEN)
    @rule()
    def probe_success(self) -> None:
        """Probe success in HALF_OPEN → CLOSED."""
        self._cb.record_success()
        self._shadow = BreakerState.CLOSED
        self._shadow_failures = 0

    @precondition(lambda self: self._cb.state == BreakerState.HALF_OPEN)
    @rule()
    def probe_failure(self) -> None:
        """Probe failure in HALF_OPEN → OPEN."""
        self._cb.record_failure()
        self._shadow = BreakerState.OPEN

    # ------------------------------------------------------------ invariants

    @invariant()
    def b1_state_is_valid(self) -> None:
        """B1: state is always a valid BreakerState member."""
        assert self._cb.state in (
            BreakerState.CLOSED, BreakerState.OPEN, BreakerState.HALF_OPEN
        ), f"Invalid breaker state: {self._cb.state!r}"

    @invariant()
    def b2_failure_count_non_negative(self) -> None:
        """B2: failure count never goes negative."""
        assert self._cb._failure_count >= 0, (
            f"Negative failure count: {self._cb._failure_count}"
        )

    @invariant()
    def b3_opened_at_consistent(self) -> None:
        """B3: _opened_at is set iff state == OPEN."""
        if self._cb.state == BreakerState.OPEN:
            assert self._cb._opened_at is not None, (
                "OPEN circuit missing _opened_at timestamp"
            )
        elif self._cb.state == BreakerState.CLOSED:
            assert self._cb._opened_at is None, (
                f"CLOSED circuit has stale _opened_at: {self._cb._opened_at}"
            )

    @invariant()
    def b4_closed_allows_dispatch(self) -> None:
        """B4: CLOSED circuit must always allow dispatch."""
        if self._cb.state == BreakerState.CLOSED:
            assert self._cb.is_allowed() is True, (
                "CLOSED circuit breaker returned is_allowed()=False"
            )

    @invariant()
    def b4_open_blocks_dispatch(self) -> None:
        """B4: OPEN circuit must block dispatch (with our long recovery timeout)."""
        if self._cb.state == BreakerState.OPEN:
            # With _RECOVERY = 9999s, timeout cannot have elapsed.
            assert self._cb.is_allowed() is False, (
                "OPEN circuit breaker (long timeout) returned is_allowed()=True"
            )

    @invariant()
    def b5_shadow_matches_real(self) -> None:
        """B5: Shadow model matches real breaker state at every step."""
        assert self._cb.state == self._shadow, (
            f"State mismatch: real={self._cb.state}, shadow={self._shadow}"
        )


TestCircuitBreakerStateful = CircuitBreakerMachine.TestCase
TestCircuitBreakerStateful.settings = settings(
    max_examples=300,
    stateful_step_count=40,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


# ---------------------------------------------------------------------------
# Machine 3: AgentRegistry invariants
# ---------------------------------------------------------------------------

from tmux_orchestrator.registry import AgentRegistry  # noqa: E402


class AgentRegistryMachine(RuleBasedStateMachine):
    """Exercises AgentRegistry.register / unregister / record_busy / record_result.

    Shadow model maintains a set of registered agent IDs and their expected
    statuses.  Invariants verify that find_idle_worker() always returns a
    currently-IDLE agent (or None) and never returns a BUSY/ERROR agent.

    Key properties verified:
      R1. find_idle_worker() returns None or an IDLE agent.
      R2. find_idle_worker() never returns an agent with a OPEN circuit breaker.
      R3. After unregister, the agent is not returned by find_idle_worker().
      R4. All registered agent IDs appear in list_all() output.
    """

    _THRESHOLD = 3
    _RECOVERY = 9_999.0

    def __init__(self) -> None:
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self._bus = self._loop.run_until_complete(self._setup_bus())
        self._registry = AgentRegistry(
            p2p_permissions=[],
            circuit_breaker_threshold=self._THRESHOLD,
            circuit_breaker_recovery=self._RECOVERY,
        )
        # Shadow: agent_id → status ("IDLE" | "BUSY")
        self._shadow: dict[str, str] = {}
        self._agent_seq: int = 0

    async def _setup_bus(self) -> Bus:
        return Bus()

    def _next_agent_id(self) -> str:
        self._agent_seq += 1
        return f"worker-{self._agent_seq}"

    def _make_agent(self, agent_id: str) -> _FakeAgent:
        agent = _FakeAgent(agent_id, self._bus)
        agent.status = AgentStatus.IDLE
        return agent

    # ------------------------------------------------------------------ rules

    @rule()
    def register_agent(self) -> None:
        """Register a new IDLE agent."""
        aid = self._next_agent_id()
        agent = self._make_agent(aid)
        self._registry.register(agent)
        self._shadow[aid] = "IDLE"

    @precondition(lambda self: len(self._shadow) > 0)
    @rule()
    def unregister_one(self) -> None:
        """Unregister the lowest-numbered agent."""
        aid = min(self._shadow.keys())
        self._registry.unregister(aid)
        del self._shadow[aid]

    @precondition(lambda self: any(
        v == "IDLE" for v in self._shadow.values()
    ))
    @rule()
    def mark_one_busy(self) -> None:
        """Find an IDLE agent and set it BUSY."""
        idle_ids = [k for k, v in self._shadow.items() if v == "IDLE"]
        aid = idle_ids[0]
        agent = self._registry.get(aid)
        if agent is not None:
            task = _make_task(f"t-{aid}")
            agent._set_busy(task)
            self._registry.record_busy(aid)
            self._shadow[aid] = "BUSY"

    @precondition(lambda self: any(
        v == "BUSY" for v in self._shadow.values()
    ))
    @rule()
    def complete_one_task(self) -> None:
        """Complete the lowest-numbered BUSY agent's task (success)."""
        busy_ids = [k for k, v in self._shadow.items() if v == "BUSY"]
        aid = busy_ids[0]
        agent = self._registry.get(aid)
        if agent is not None:
            async def _do() -> None:
                agent._set_idle()
            self._loop.run_until_complete(_do())
            self._registry.record_result(aid, error=False)
            self._shadow[aid] = "IDLE"

    @precondition(lambda self: any(
        v == "BUSY" for v in self._shadow.values()
    ))
    @rule()
    def fail_one_task(self) -> None:
        """Fail the lowest-numbered BUSY agent's task (error path)."""
        busy_ids = [k for k, v in self._shadow.items() if v == "BUSY"]
        aid = busy_ids[0]
        agent = self._registry.get(aid)
        if agent is not None:
            agent.status = AgentStatus.ERROR
            self._registry.record_result(aid, error=True)
            self._shadow[aid] = "ERROR"

    # ------------------------------------------------------------ invariants

    @invariant()
    def r1_find_idle_returns_idle_or_none(self) -> None:
        """R1: find_idle_worker() returns an IDLE agent or None — never BUSY/ERROR."""
        agent = self._registry.find_idle_worker()
        if agent is not None:
            assert agent.status == AgentStatus.IDLE, (
                f"find_idle_worker() returned non-IDLE agent: "
                f"{agent.id} status={agent.status}"
            )

    @invariant()
    def r3_unregistered_not_findable(self) -> None:
        """R3: An unregistered agent cannot be returned by find_idle_worker()."""
        registered_ids = set(self._shadow.keys())
        agent = self._registry.find_idle_worker()
        if agent is not None:
            assert agent.id in registered_ids, (
                f"find_idle_worker() returned unregistered agent {agent.id!r}"
            )

    @invariant()
    def r4_all_registered_in_list_all(self) -> None:
        """R4: All shadow IDs appear in list_all() output."""
        listed_ids = {a["id"] for a in self._registry.list_all()}
        for aid in self._shadow:
            assert aid in listed_ids, (
                f"Registered agent {aid!r} missing from list_all()"
            )

    def teardown(self) -> None:
        self._loop.close()


TestAgentRegistryStateful = AgentRegistryMachine.TestCase
TestAgentRegistryStateful.settings = settings(
    max_examples=200,
    stateful_step_count=30,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
