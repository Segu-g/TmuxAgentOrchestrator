"""Tests for Orchestrator constructor DI injection of infra components.

Verifies that:
1. NullResultStore / NullCheckpointStore / NullAutoScaler can be injected.
2. When injected, the Orchestrator does not try to inline-import the real impls.
3. WorkflowManager and GroupManager can be injected via constructor.
4. ``reconfigure_autoscaler()`` public method delegates to injected autoscaler.
5. ``get_autoscaler_status()`` delegates to injected autoscaler.

Reference: DESIGN.md §10.35 (v1.0.35 — orchestrator infra DI).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tmux_orchestrator.agents.base import AgentStatus
from tmux_orchestrator.application.infra_protocols import (
    NullAutoScaler,
    NullCheckpointStore,
    NullResultStore,
)
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.group_manager import GroupManager
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.workflow_manager import WorkflowManager


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


# ---------------------------------------------------------------------------
# Constructor injection tests
# ---------------------------------------------------------------------------


def test_null_result_store_injected():
    """Orchestrator accepts NullResultStore via result_store= parameter."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    null_store = NullResultStore()
    orch = Orchestrator(
        bus=bus,
        tmux=tmux,
        config=config,
        result_store=null_store,
    )
    assert orch._result_store is null_store


def test_null_checkpoint_store_injected():
    """Orchestrator accepts NullCheckpointStore via checkpoint_store= parameter."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    null_cp = NullCheckpointStore()
    orch = Orchestrator(
        bus=bus,
        tmux=tmux,
        config=config,
        checkpoint_store=null_cp,
    )
    assert orch._checkpoint_store is null_cp


def test_null_autoscaler_injected():
    """Orchestrator accepts NullAutoScaler via autoscaler= parameter."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    null_scaler = NullAutoScaler()
    orch = Orchestrator(
        bus=bus,
        tmux=tmux,
        config=config,
        autoscaler=null_scaler,
    )
    assert orch._autoscaler is null_scaler


def test_workflow_manager_injected():
    """Orchestrator accepts WorkflowManager via workflow_manager= parameter."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    wm = WorkflowManager()
    orch = Orchestrator(
        bus=bus,
        tmux=tmux,
        config=config,
        workflow_manager=wm,
    )
    assert orch.get_workflow_manager() is wm


def test_group_manager_injected():
    """Orchestrator accepts GroupManager via group_manager= parameter."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    gm = GroupManager()
    orch = Orchestrator(
        bus=bus,
        tmux=tmux,
        config=config,
        group_manager=gm,
    )
    assert orch.get_group_manager() is gm


# ---------------------------------------------------------------------------
# Default (None) behaviour — backwards-compatible
# ---------------------------------------------------------------------------


def test_default_result_store_none_when_disabled():
    """When result_store_enabled=False and no injection, _result_store is None."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(result_store_enabled=False)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    assert orch._result_store is None


def test_default_checkpoint_store_none_when_disabled():
    """When checkpoint_enabled=False and no injection, _checkpoint_store is None."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(checkpoint_enabled=False)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    assert orch._checkpoint_store is None


def test_default_autoscaler_none_when_disabled():
    """When autoscale_max=0 and no injection, _autoscaler is None."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(autoscale_max=0)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    assert orch._autoscaler is None


def test_default_workflow_manager_created():
    """Without injection, Orchestrator creates a default WorkflowManager."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    assert orch.get_workflow_manager() is not None
    assert isinstance(orch.get_workflow_manager(), WorkflowManager)


def test_default_group_manager_created():
    """Without injection, Orchestrator creates a default GroupManager."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    assert orch.get_group_manager() is not None
    assert isinstance(orch.get_group_manager(), GroupManager)


# ---------------------------------------------------------------------------
# reconfigure_autoscaler() public method
# ---------------------------------------------------------------------------


def test_reconfigure_autoscaler_delegates_to_injected():
    """reconfigure_autoscaler() delegates to the injected AutoScalerProtocol."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    null_scaler = NullAutoScaler()
    orch = Orchestrator(
        bus=bus,
        tmux=tmux,
        config=config,
        autoscaler=null_scaler,
    )
    result = orch.reconfigure_autoscaler(min=1, max=5, threshold=2, cooldown=30.0)
    assert isinstance(result, dict)


def test_reconfigure_autoscaler_raises_when_none():
    """reconfigure_autoscaler() raises ValueError when autoscaler is not active."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(autoscale_max=0)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    with pytest.raises(ValueError, match="Autoscaling is not enabled"):
        orch.reconfigure_autoscaler()


# ---------------------------------------------------------------------------
# get_autoscaler_status() with NullAutoScaler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_autoscaler_status_with_null_scaler():
    """get_autoscaler_status() returns disabled-stub when NullAutoScaler injected."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    null_scaler = NullAutoScaler()
    orch = Orchestrator(
        bus=bus,
        tmux=tmux,
        config=config,
        autoscaler=null_scaler,
    )
    result = await orch.get_autoscaler_status()
    assert result["enabled"] is False


# ---------------------------------------------------------------------------
# All components injected together
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_infra_components_injected_together():
    """All 5 infra components can be injected simultaneously."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    null_result = NullResultStore()
    null_cp = NullCheckpointStore()
    null_scaler = NullAutoScaler()
    wm = WorkflowManager()
    gm = GroupManager()
    orch = Orchestrator(
        bus=bus,
        tmux=tmux,
        config=config,
        result_store=null_result,
        checkpoint_store=null_cp,
        autoscaler=null_scaler,
        workflow_manager=wm,
        group_manager=gm,
    )
    assert orch._result_store is null_result
    assert orch._checkpoint_store is null_cp
    assert orch._autoscaler is null_scaler
    assert orch.get_workflow_manager() is wm
    assert orch.get_group_manager() is gm

    # Verify status works correctly
    status = await orch.get_autoscaler_status()
    assert status["enabled"] is False

    # reconfigure_autoscaler should not raise
    result = orch.reconfigure_autoscaler(min=1, max=3)
    assert isinstance(result, dict)
