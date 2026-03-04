"""Tests for AgentRegistry — independently of Orchestrator."""
from __future__ import annotations

import asyncio
import time

import pytest

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.circuit_breaker import BreakerState
from tmux_orchestrator.config import AgentRole
from tmux_orchestrator.registry import AgentRegistry


# ---------------------------------------------------------------------------
# Minimal test agent (no tmux required)
# ---------------------------------------------------------------------------


class StubAgent(Agent):
    def __init__(self, agent_id: str, bus: Bus, *, role: str = "worker") -> None:
        super().__init__(agent_id, bus)
        self.role = role

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


def make_registry(**kwargs) -> AgentRegistry:
    defaults = dict(p2p_permissions=[], circuit_breaker_threshold=3, circuit_breaker_recovery=60.0)
    defaults.update(kwargs)
    return AgentRegistry(**defaults)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_creates_circuit_breaker():
    bus = Bus()
    reg = make_registry()
    agent = StubAgent("a1", bus)
    reg.register(agent)
    assert reg.get_breaker("a1") is not None


def test_register_records_parent():
    bus = Bus()
    reg = make_registry()
    parent = StubAgent("parent", bus)
    child = StubAgent("child", bus)
    reg.register(parent)
    reg.register(child, parent_id="parent")
    assert reg._agent_parents["child"] == "parent"


def test_unregister_removes_agent_and_breaker():
    bus = Bus()
    reg = make_registry()
    agent = StubAgent("a1", bus)
    reg.register(agent)
    reg.unregister("a1")
    assert reg.get("a1") is None
    assert reg.get_breaker("a1") is None


def test_unregister_also_removes_parent_record():
    bus = Bus()
    reg = make_registry()
    parent = StubAgent("p", bus)
    child = StubAgent("c", bus)
    reg.register(parent)
    reg.register(child, parent_id="p")
    reg.unregister("c")
    assert "c" not in reg._agent_parents


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_get_returns_agent():
    bus = Bus()
    reg = make_registry()
    agent = StubAgent("a1", bus)
    reg.register(agent)
    assert reg.get("a1") is agent


def test_get_director_returns_director():
    bus = Bus()
    reg = make_registry()
    worker = StubAgent("w1", bus, role="worker")
    director = StubAgent("d1", bus, role="director")
    reg.register(worker)
    reg.register(director)
    assert reg.get_director() is director


def test_get_director_returns_none_when_absent():
    bus = Bus()
    reg = make_registry()
    worker = StubAgent("w1", bus)
    reg.register(worker)
    assert reg.get_director() is None


def test_find_idle_worker_returns_idle_worker():
    bus = Bus()
    reg = make_registry()
    agent = StubAgent("w1", bus)
    agent.status = AgentStatus.IDLE
    reg.register(agent)
    assert reg.find_idle_worker() is agent


def test_find_idle_worker_skips_director():
    bus = Bus()
    reg = make_registry()
    director = StubAgent("d1", bus, role="director")
    director.status = AgentStatus.IDLE
    reg.register(director)
    assert reg.find_idle_worker() is None


def test_find_idle_worker_skips_open_circuit():
    bus = Bus()
    reg = make_registry(circuit_breaker_threshold=1)
    agent = StubAgent("w1", bus)
    agent.status = AgentStatus.IDLE
    reg.register(agent)
    reg.get_breaker("w1").record_failure()  # trips to OPEN
    assert reg.find_idle_worker() is None


# ---------------------------------------------------------------------------
# P2P permission
# ---------------------------------------------------------------------------


def test_p2p_user_always_permitted():
    reg = make_registry()
    permitted, reason = reg.is_p2p_permitted("__user__", "any-agent")
    assert permitted and reason == "user"


def test_p2p_explicit_permitted():
    bus = Bus()
    reg = make_registry(p2p_permissions=[("a", "b")])
    a = StubAgent("a", bus)
    b = StubAgent("b", bus)
    reg.register(a)
    reg.register(b)
    permitted, reason = reg.is_p2p_permitted("a", "b")
    assert permitted and reason == "explicit"


def test_p2p_hierarchy_siblings():
    bus = Bus()
    reg = make_registry()
    a = StubAgent("a", bus)
    b = StubAgent("b", bus)
    reg.register(a)
    reg.register(b)
    permitted, reason = reg.is_p2p_permitted("a", "b")
    assert permitted and reason == "hierarchy"


def test_p2p_hierarchy_parent_child():
    bus = Bus()
    reg = make_registry()
    parent = StubAgent("parent", bus)
    child = StubAgent("child", bus)
    reg.register(parent)
    reg.register(child, parent_id="parent")
    ok, r = reg.is_p2p_permitted("parent", "child")
    assert ok and r == "hierarchy"
    ok2, r2 = reg.is_p2p_permitted("child", "parent")
    assert ok2 and r2 == "hierarchy"


def test_p2p_cross_branch_blocked():
    bus = Bus()
    reg = make_registry()
    root = StubAgent("root", bus)
    branch_a = StubAgent("ba", bus)
    branch_b = StubAgent("bb", bus)
    leaf_a = StubAgent("la", bus)
    leaf_b = StubAgent("lb", bus)
    reg.register(root)
    reg.register(branch_a, parent_id="root")
    reg.register(branch_b, parent_id="root")
    reg.register(leaf_a, parent_id="ba")
    reg.register(leaf_b, parent_id="bb")
    ok, r = reg.is_p2p_permitted("la", "lb")
    assert not ok and r == "blocked"


def test_grant_p2p_unlocks_cross_branch():
    bus = Bus()
    reg = make_registry()
    root = StubAgent("root", bus)
    ba = StubAgent("ba", bus)
    bb = StubAgent("bb", bus)
    la = StubAgent("la", bus)
    lb = StubAgent("lb", bus)
    reg.register(root)
    reg.register(ba, parent_id="root")
    reg.register(bb, parent_id="root")
    reg.register(la, parent_id="ba")
    reg.register(lb, parent_id="bb")
    reg.grant_p2p("la", "lb")
    ok, r = reg.is_p2p_permitted("la", "lb")
    assert ok and r == "explicit"


# ---------------------------------------------------------------------------
# Circuit breaker recording
# ---------------------------------------------------------------------------


def test_record_result_failure_trips_breaker():
    bus = Bus()
    reg = make_registry(circuit_breaker_threshold=1)
    agent = StubAgent("a1", bus)
    reg.register(agent)
    reg.record_result("a1", error=True)
    assert reg.get_breaker("a1").state == BreakerState.OPEN


def test_record_result_success_closes_half_open():
    bus = Bus()
    reg = make_registry(circuit_breaker_threshold=1, circuit_breaker_recovery=300.0)
    agent = StubAgent("a1", bus)
    reg.register(agent)
    reg.record_result("a1", error=True)  # OPEN
    cb = reg.get_breaker("a1")
    cb._opened_at = time.monotonic() - 400.0  # past recovery
    cb.is_allowed()  # → HALF_OPEN
    reg.record_result("a1", error=False)  # → CLOSED
    assert cb.state == BreakerState.CLOSED


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------


def test_list_all_includes_drop_counts():
    bus = Bus()
    reg = make_registry()
    agent = StubAgent("a1", bus)
    agent.status = AgentStatus.IDLE
    reg.register(agent)
    result = reg.list_all(drop_counts={"a1": 7})
    assert result[0]["bus_drops"] == 7


def test_list_all_zero_drops_when_not_provided():
    bus = Bus()
    reg = make_registry()
    agent = StubAgent("a1", bus)
    reg.register(agent)
    result = reg.list_all()
    assert result[0]["bus_drops"] == 0
