"""Tests for application/infra_protocols.py — Protocol conformance and Null Objects.

Verifies:
1. NullResultStore / NullCheckpointStore / NullAutoScaler satisfy their protocols.
2. Real implementations (ResultStore, CheckpointStore, AutoScaler) also satisfy protocols.
3. Null Object methods behave as documented (no-ops / empty returns).

Reference: DESIGN.md §10.35 (v1.0.35 — orchestrator infra DI).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tmux_orchestrator.application.infra_protocols import (
    AutoScalerProtocol,
    CheckpointStoreProtocol,
    NullAutoScaler,
    NullCheckpointStore,
    NullResultStore,
    ResultStoreProtocol,
)


# ---------------------------------------------------------------------------
# NullResultStore
# ---------------------------------------------------------------------------


class TestNullResultStore:
    def test_isinstance_protocol(self):
        store = NullResultStore()
        assert isinstance(store, ResultStoreProtocol)

    def test_append_is_noop(self):
        store = NullResultStore()
        # Should not raise
        store.append(
            task_id="t1",
            agent_id="a1",
            prompt="hello",
            result_text="world",
            error=None,
            duration_s=1.0,
        )

    def test_all_dates_returns_empty(self):
        store = NullResultStore()
        assert store.all_dates() == []

    def test_query_returns_empty(self):
        store = NullResultStore()
        assert store.query() == []
        assert store.query(date="2026-01-01", agent_id="a1", task_id="t1", limit=10) == []

    def test_append_with_error(self):
        store = NullResultStore()
        store.append(
            task_id="t2",
            agent_id="a2",
            prompt="p",
            result_text="",
            error="oops",
            duration_s=0.5,
        )
        # Still empty — no data written
        assert store.all_dates() == []


# ---------------------------------------------------------------------------
# NullCheckpointStore
# ---------------------------------------------------------------------------


class TestNullCheckpointStore:
    def test_isinstance_protocol(self):
        store = NullCheckpointStore()
        assert isinstance(store, CheckpointStoreProtocol)

    def test_initialize_is_noop(self):
        store = NullCheckpointStore()
        store.initialize()  # Should not raise

    def test_close_is_noop(self):
        store = NullCheckpointStore()
        store.close()  # Should not raise

    def test_load_pending_tasks_returns_empty(self):
        store = NullCheckpointStore()
        assert store.load_pending_tasks() == []

    def test_load_waiting_tasks_returns_empty(self):
        store = NullCheckpointStore()
        assert store.load_waiting_tasks() == []

    def test_load_workflows_returns_empty_dict(self):
        store = NullCheckpointStore()
        assert store.load_workflows() == {}

    def test_load_meta_returns_default(self):
        store = NullCheckpointStore()
        assert store.load_meta("key") is None
        assert store.load_meta("key", default="fallback") == "fallback"

    def test_save_task_is_noop(self):
        store = NullCheckpointStore()
        task = MagicMock()
        store.save_task(task=task, queue_priority=0)  # Should not raise

    def test_remove_task_is_noop(self):
        store = NullCheckpointStore()
        store.remove_task(task_id="t1")  # Should not raise

    def test_save_waiting_task_is_noop(self):
        store = NullCheckpointStore()
        task = MagicMock()
        store.save_waiting_task(task=task)  # Should not raise

    def test_remove_waiting_task_is_noop(self):
        store = NullCheckpointStore()
        store.remove_waiting_task(task_id="t1")  # Should not raise

    def test_save_meta_is_noop(self):
        store = NullCheckpointStore()
        store.save_meta("key", "value")  # Should not raise
        # After save, load still returns default (null store does not persist)
        assert store.load_meta("key") is None

    def test_save_workflow_is_noop(self):
        store = NullCheckpointStore()
        run = MagicMock()
        store.save_workflow(run=run)  # Should not raise

    def test_remove_workflow_is_noop(self):
        store = NullCheckpointStore()
        store.remove_workflow(workflow_id="wf-1")  # Should not raise

    def test_clear_tasks_is_noop(self):
        store = NullCheckpointStore()
        store.clear_tasks()  # Should not raise

    def test_clear_waiting_tasks_is_noop(self):
        store = NullCheckpointStore()
        store.clear_waiting_tasks()  # Should not raise

    def test_clear_workflows_is_noop(self):
        store = NullCheckpointStore()
        store.clear_workflows()  # Should not raise

    def test_clear_all_is_noop(self):
        store = NullCheckpointStore()
        store.clear_all()  # Should not raise


# ---------------------------------------------------------------------------
# NullAutoScaler
# ---------------------------------------------------------------------------


class TestNullAutoScaler:
    def test_isinstance_protocol(self):
        scaler = NullAutoScaler()
        assert isinstance(scaler, AutoScalerProtocol)

    def test_start_is_noop(self):
        scaler = NullAutoScaler()
        scaler.start()  # Should not raise

    def test_stop_is_noop(self):
        scaler = NullAutoScaler()
        scaler.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_status_returns_disabled_stub(self):
        scaler = NullAutoScaler()
        result = await scaler.status()
        assert result["enabled"] is False
        assert result["agent_count"] == 0
        assert result["autoscaled_ids"] == []

    def test_reconfigure_returns_dict(self):
        scaler = NullAutoScaler()
        result = scaler.reconfigure(min=1, max=5, threshold=2, cooldown=30.0)
        assert isinstance(result, dict)
        assert result["min"] == 1
        assert result["max"] == 5

    def test_reconfigure_noop_for_none_args(self):
        scaler = NullAutoScaler()
        result = scaler.reconfigure()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Protocol structural conformance with real implementations
# ---------------------------------------------------------------------------


class TestRealImplementationConformance:
    """Verify real infrastructure classes satisfy the Protocols at runtime."""

    def test_result_store_satisfies_protocol(self, tmp_path):
        from tmux_orchestrator.result_store import ResultStore
        store = ResultStore(store_dir=tmp_path, session_name="test")
        assert isinstance(store, ResultStoreProtocol)

    def test_checkpoint_store_satisfies_protocol(self, tmp_path):
        from tmux_orchestrator.checkpoint_store import CheckpointStore
        store = CheckpointStore(db_path=tmp_path / "cp.db")
        assert isinstance(store, CheckpointStoreProtocol)

    def test_autoscaler_satisfies_protocol(self):
        from unittest.mock import MagicMock
        from tmux_orchestrator.autoscaler import AutoScaler
        from tmux_orchestrator.config import OrchestratorConfig
        orch_mock = MagicMock()
        cfg = OrchestratorConfig(
            session_name="test",
            agents=[],
            p2p_permissions=[],
            autoscale_max=3,
            autoscale_min=0,
            autoscale_threshold=2,
        )
        scaler = AutoScaler(orch_mock, cfg)
        assert isinstance(scaler, AutoScalerProtocol)
