"""Tests for Orchestrator dependency injection of ContextMonitor and DriftMonitor.

Verifies that:
1. ``ContextMonitorProtocol`` and ``DriftMonitorProtocol`` are satisfied by
   their real implementations (structural subtyping via Protocol).
2. ``NullContextMonitor`` and ``NullDriftMonitor`` satisfy their respective Protocols.
3. ``Orchestrator.__init__`` accepts injected monitors and uses them.
4. When no monitor is injected, the default real implementation is used.
5. Injected monitors have ``start()`` called during ``Orchestrator.start()`` and
   ``stop()`` called during ``Orchestrator.stop()``.
6. A task can be dispatched when fake monitors are injected (no tmux required
   for the orchestrator's own logic).

Reference: DESIGN.md §10.N (v1.0.14 — orchestrator full DI).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.context_monitor import ContextMonitor
from tmux_orchestrator.drift_monitor import DriftMonitor
from tmux_orchestrator.orchestrator import (
    ContextMonitorProtocol,
    DriftMonitorProtocol,
    NullContextMonitor,
    NullDriftMonitor,
    Orchestrator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(**kwargs) -> OrchestratorConfig:
    defaults = dict(
        session_name="test-di",
        agents=[],
        p2p_permissions=[],
        task_timeout=10,
    )
    defaults.update(kwargs)
    return OrchestratorConfig(**defaults)


def make_tmux_mock():
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    return tmux


class DummyAgent(Agent):
    """Minimal agent for dispatch tests."""

    def __init__(self, agent_id: str, bus: Bus) -> None:
        super().__init__(agent_id, bus)
        self.dispatched: list[Task] = []
        self.dispatched_event: asyncio.Event = asyncio.Event()

    async def start(self) -> None:
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(
            self._run_loop(), name=f"{self.id}-loop"
        )

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()

    async def _dispatch_task(self, task: Task) -> None:
        self.dispatched.append(task)
        self.dispatched_event.set()
        await asyncio.sleep(0)
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


class SpyMonitor:
    """Test spy for Monitor protocol compliance — records calls to start/stop."""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def get_stats(self, agent_id: str) -> "dict[str, Any] | None":
        return None

    def all_stats(self) -> "list[dict[str, Any]]":
        return []


class SpyDriftMonitor:
    """Test spy for DriftMonitorProtocol — records calls to start/stop."""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def get_drift_stats(self, agent_id: str) -> "dict[str, Any] | None":
        return None

    def all_drift_stats(self) -> "list[dict[str, Any]]":
        return []


# ---------------------------------------------------------------------------
# Protocol conformance tests
# ---------------------------------------------------------------------------


def test_real_context_monitor_satisfies_protocol() -> None:
    """ContextMonitor satisfies ContextMonitorProtocol via structural subtyping."""
    # Use isinstance() with @runtime_checkable Protocol to verify structural conformance.
    # We patch the constructor to avoid needing real bus/tmux objects.
    cm = object.__new__(ContextMonitor)
    assert isinstance(cm, ContextMonitorProtocol)


def test_real_drift_monitor_satisfies_protocol() -> None:
    """DriftMonitor satisfies DriftMonitorProtocol via structural subtyping."""
    dm = object.__new__(DriftMonitor)
    assert isinstance(dm, DriftMonitorProtocol)


def test_null_context_monitor_satisfies_protocol() -> None:
    """NullContextMonitor satisfies ContextMonitorProtocol."""
    assert isinstance(NullContextMonitor(), ContextMonitorProtocol)


def test_null_drift_monitor_satisfies_protocol() -> None:
    """NullDriftMonitor satisfies DriftMonitorProtocol."""
    assert isinstance(NullDriftMonitor(), DriftMonitorProtocol)


def test_spy_monitor_satisfies_context_protocol() -> None:
    """SpyMonitor satisfies ContextMonitorProtocol."""
    assert isinstance(SpyMonitor(), ContextMonitorProtocol)


def test_spy_drift_monitor_satisfies_drift_protocol() -> None:
    """SpyDriftMonitor satisfies DriftMonitorProtocol."""
    assert isinstance(SpyDriftMonitor(), DriftMonitorProtocol)


# ---------------------------------------------------------------------------
# Null object behaviour
# ---------------------------------------------------------------------------


def test_null_context_monitor_noop() -> None:
    """NullContextMonitor start/stop are no-ops; queries return empty results."""
    ncm = NullContextMonitor()
    ncm.start()  # must not raise
    ncm.stop()   # must not raise
    assert ncm.get_stats("any-agent") is None
    assert ncm.all_stats() == []


def test_null_drift_monitor_noop() -> None:
    """NullDriftMonitor start/stop are no-ops; queries return empty results."""
    ndm = NullDriftMonitor()
    ndm.start()  # must not raise
    ndm.stop()   # must not raise
    assert ndm.get_drift_stats("any-agent") is None
    assert ndm.all_drift_stats() == []


# ---------------------------------------------------------------------------
# Default (no injection) — real monitors created internally
# ---------------------------------------------------------------------------


def test_default_monitors_created_when_none_injected() -> None:
    """When context_monitor=None, Orchestrator creates a real ContextMonitor internally."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    # Real ContextMonitor and DriftMonitor should be present
    assert isinstance(orch._context_monitor, ContextMonitor)
    assert isinstance(orch._drift_monitor, DriftMonitor)


# ---------------------------------------------------------------------------
# Injection tests
# ---------------------------------------------------------------------------


def test_injected_context_monitor_is_stored() -> None:
    """An injected ContextMonitorProtocol is stored, not overridden."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    spy = SpyMonitor()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config, context_monitor=spy)
    assert orch._context_monitor is spy


def test_injected_drift_monitor_is_stored() -> None:
    """An injected DriftMonitorProtocol is stored, not overridden."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    spy = SpyDriftMonitor()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config, drift_monitor=spy)
    assert orch._drift_monitor is spy


async def test_injected_context_monitor_start_called() -> None:
    """start() is called on the injected context monitor when the orchestrator starts."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    spy = SpyMonitor()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config, context_monitor=spy)
    await orch.start()
    try:
        assert spy.started == 1
    finally:
        await orch.stop()


async def test_injected_drift_monitor_start_called() -> None:
    """start() is called on the injected drift monitor when the orchestrator starts."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    spy = SpyDriftMonitor()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config, drift_monitor=spy)
    await orch.start()
    try:
        assert spy.started == 1
    finally:
        await orch.stop()


async def test_injected_monitors_stop_called_on_orchestrator_stop() -> None:
    """stop() is called on both injected monitors when the orchestrator stops."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    cm_spy = SpyMonitor()
    dm_spy = SpyDriftMonitor()
    orch = Orchestrator(
        bus=bus, tmux=tmux, config=config,
        context_monitor=cm_spy,
        drift_monitor=dm_spy,
    )
    await orch.start()
    await orch.stop()
    assert cm_spy.stopped == 1
    assert dm_spy.stopped == 1


# ---------------------------------------------------------------------------
# Full DI with task dispatch
# ---------------------------------------------------------------------------


async def test_task_dispatch_with_null_monitors() -> None:
    """Task dispatch works correctly even when null monitors are injected."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(
        bus=bus, tmux=tmux, config=config,
        context_monitor=NullContextMonitor(),
        drift_monitor=NullDriftMonitor(),
    )
    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()
    try:
        task = await orch.submit_task("hello from DI test")
        await asyncio.wait_for(agent.dispatched_event.wait(), timeout=2.0)
        assert any(t.id == task.id for t in agent.dispatched)
    finally:
        await orch.stop()


async def test_orchestrator_stat_queries_with_null_monitors() -> None:
    """Context/drift stat queries return sensible values when null monitors injected."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(
        bus=bus, tmux=tmux, config=config,
        context_monitor=NullContextMonitor(),
        drift_monitor=NullDriftMonitor(),
    )
    # These should not raise even with null monitors
    assert orch.get_agent_context_stats("nonexistent") is None
    assert orch.all_agent_context_stats() == []
    assert orch.get_agent_drift_stats("nonexistent") is None
    assert orch.all_agent_drift_stats() == []


async def test_injected_null_context_monitor_both_monitors() -> None:
    """Both null monitors can be injected simultaneously for a fully hermetic test."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(
        bus=bus, tmux=tmux, config=config,
        context_monitor=NullContextMonitor(),
        drift_monitor=NullDriftMonitor(),
    )
    # Orchestrator must not contain real ContextMonitor or DriftMonitor
    assert not isinstance(orch._context_monitor, ContextMonitor)
    assert not isinstance(orch._drift_monitor, DriftMonitor)
    # Must be the Null variants
    assert isinstance(orch._context_monitor, NullContextMonitor)
    assert isinstance(orch._drift_monitor, NullDriftMonitor)
    # Start/stop should complete without any tmux pane access
    await orch.start()
    await orch.stop()
