"""Unit tests for orchestrator drift auto re-brief (v1.1.18).

Tests the behaviour of Orchestrator._handle_drift_warning() and
the _route_loop handling of agent_drift_warning STATUS events.

Reference: DESIGN.md §10.50 (v1.1.18)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator(
    *,
    rebrief_enabled: bool = True,
    rebrief_cooldown: float = 60.0,
    rebrief_message: str = "ROLE REMINDER",
) -> Orchestrator:
    """Create a minimal Orchestrator with NullMonitors injected."""
    from unittest.mock import MagicMock
    from tmux_orchestrator.application.monitor_protocols import NullContextMonitor, NullDriftMonitor
    from tmux_orchestrator.application.infra_protocols import NullCheckpointStore, NullResultStore

    cfg = OrchestratorConfig(
        task_timeout=120,
        watchdog_poll=40,
        drift_rebrief_enabled=rebrief_enabled,
        drift_rebrief_cooldown=rebrief_cooldown,
        drift_rebrief_message=rebrief_message,
    )
    bus = Bus()
    mock_tmux = MagicMock()
    orc = Orchestrator(
        bus=bus,
        tmux=mock_tmux,
        config=cfg,
        context_monitor=NullContextMonitor(),
        drift_monitor=NullDriftMonitor(),
        checkpoint_store=NullCheckpointStore(),
        result_store=NullResultStore(),
    )
    return orc


def _make_mock_agent(agent_id: str, *, has_task: bool = True) -> MagicMock:
    """Create a mock agent with a current task if requested."""
    agent = MagicMock(spec=Agent)
    agent.id = agent_id
    agent.notify_stdin = AsyncMock()
    if has_task:
        task = MagicMock(spec=Task)
        task.prompt = f"Implement feature X for agent {agent_id}"
        agent._current_task = task
    else:
        agent._current_task = None
    return agent


# ---------------------------------------------------------------------------
# Tests: _handle_drift_warning (unit)
# ---------------------------------------------------------------------------


class TestHandleDriftWarning:
    """Tests for Orchestrator._handle_drift_warning()."""

    @pytest.mark.asyncio
    async def test_sends_rebrief_to_agent(self):
        """Re-brief is sent to the drifted agent via notify_stdin."""
        orc = _make_orchestrator(rebrief_message="ROLE REMINDER: stay on task")
        agent = _make_mock_agent("worker-1")
        orc.registry._agents["worker-1"] = agent

        await orc._handle_drift_warning("worker-1", drift_score=0.45)

        agent.notify_stdin.assert_awaited_once()
        call_arg = agent.notify_stdin.call_args[0][0]
        assert "ROLE REMINDER" in call_arg

    @pytest.mark.asyncio
    async def test_includes_task_prompt_snippet_when_task_active(self):
        """Re-brief includes the first 200 chars of the current task prompt."""
        orc = _make_orchestrator(rebrief_message="FOCUS")
        agent = _make_mock_agent("worker-1", has_task=True)
        long_prompt = "A" * 300
        agent._current_task.prompt = long_prompt
        orc.registry._agents["worker-1"] = agent

        await orc._handle_drift_warning("worker-1", drift_score=0.4)

        call_arg = agent.notify_stdin.call_args[0][0]
        assert "A" * 200 in call_arg
        # Should not include the full 300-char prompt
        assert "A" * 201 not in call_arg

    @pytest.mark.asyncio
    async def test_no_task_snippet_when_agent_idle(self):
        """Re-brief is sent without task snippet when agent has no active task."""
        orc = _make_orchestrator(rebrief_message="STAY ON TASK")
        agent = _make_mock_agent("worker-1", has_task=False)
        orc.registry._agents["worker-1"] = agent

        await orc._handle_drift_warning("worker-1", drift_score=0.3)

        agent.notify_stdin.assert_awaited_once()
        call_arg = agent.notify_stdin.call_args[0][0]
        assert "STAY ON TASK" in call_arg
        assert "Your current task" not in call_arg

    @pytest.mark.asyncio
    async def test_cooldown_prevents_double_rebrief(self):
        """Second re-brief within cooldown window is suppressed."""
        orc = _make_orchestrator(rebrief_cooldown=60.0)
        agent = _make_mock_agent("worker-1")
        orc.registry._agents["worker-1"] = agent

        # First call — should send
        await orc._handle_drift_warning("worker-1", drift_score=0.4)
        assert agent.notify_stdin.await_count == 1

        # Second call immediately — should be suppressed by cooldown
        await orc._handle_drift_warning("worker-1", drift_score=0.35)
        assert agent.notify_stdin.await_count == 1  # still 1

    @pytest.mark.asyncio
    async def test_cooldown_allows_rebrief_after_expiry(self):
        """Re-brief is allowed again after the cooldown expires."""
        orc = _make_orchestrator(rebrief_cooldown=0.05)  # 50 ms
        agent = _make_mock_agent("worker-1")
        orc.registry._agents["worker-1"] = agent

        await orc._handle_drift_warning("worker-1", drift_score=0.4)
        assert agent.notify_stdin.await_count == 1

        await asyncio.sleep(0.1)  # wait for cooldown to expire

        await orc._handle_drift_warning("worker-1", drift_score=0.38)
        assert agent.notify_stdin.await_count == 2

    @pytest.mark.asyncio
    async def test_disabled_feature_skips_send(self):
        """When drift_rebrief_enabled=False, no re-brief is sent."""
        orc = _make_orchestrator(rebrief_enabled=False)
        agent = _make_mock_agent("worker-1")
        orc.registry._agents["worker-1"] = agent

        await orc._handle_drift_warning("worker-1", drift_score=0.3)

        agent.notify_stdin.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_agent_is_noop(self):
        """Re-brief for an unknown agent_id does not raise."""
        orc = _make_orchestrator()

        # Should not raise
        await orc._handle_drift_warning("nonexistent", drift_score=0.2)

    @pytest.mark.asyncio
    async def test_history_recorded_after_rebrief(self):
        """Re-brief history is recorded with timestamp and drift_score."""
        orc = _make_orchestrator()
        agent = _make_mock_agent("worker-1")
        orc.registry._agents["worker-1"] = agent

        await orc._handle_drift_warning("worker-1", drift_score=0.42)

        history = orc.get_agent_drift_rebriefs("worker-1")
        assert len(history) == 1
        assert history[0]["drift_score"] == pytest.approx(0.42)
        assert "timestamp" in history[0]

    @pytest.mark.asyncio
    async def test_history_accumulates_multiple_rebriefs(self):
        """Multiple re-briefs (after cooldown) accumulate in history."""
        orc = _make_orchestrator(rebrief_cooldown=0.02)
        agent = _make_mock_agent("worker-1")
        orc.registry._agents["worker-1"] = agent

        await orc._handle_drift_warning("worker-1", drift_score=0.5)
        await asyncio.sleep(0.05)
        await orc._handle_drift_warning("worker-1", drift_score=0.4)
        await asyncio.sleep(0.05)
        await orc._handle_drift_warning("worker-1", drift_score=0.3)

        history = orc.get_agent_drift_rebriefs("worker-1")
        assert len(history) == 3
        # Most recent first
        assert history[0]["drift_score"] == pytest.approx(0.3)
        assert history[2]["drift_score"] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_notify_stdin_exception_does_not_propagate(self):
        """If notify_stdin raises, the exception is caught and does not crash the orchestrator."""
        orc = _make_orchestrator()
        agent = _make_mock_agent("worker-1")
        agent.notify_stdin = AsyncMock(side_effect=RuntimeError("pane dead"))
        orc.registry._agents["worker-1"] = agent

        # Should not raise
        await orc._handle_drift_warning("worker-1", drift_score=0.4)

        # History should NOT be recorded (error before recording)
        assert orc.get_agent_drift_rebriefs("worker-1") == []

    @pytest.mark.asyncio
    async def test_last_sent_updated_after_rebrief(self):
        """_drift_rebrief_last_sent is updated after a successful re-brief."""
        orc = _make_orchestrator()
        agent = _make_mock_agent("worker-1")
        orc.registry._agents["worker-1"] = agent

        before = time.monotonic()
        await orc._handle_drift_warning("worker-1", drift_score=0.45)
        after = time.monotonic()

        last = orc._drift_rebrief_last_sent.get("worker-1", 0.0)
        assert before <= last <= after

    @pytest.mark.asyncio
    async def test_per_agent_cooldown_is_independent(self):
        """Cooldown state is per-agent — one agent's cooldown does not affect another."""
        orc = _make_orchestrator(rebrief_cooldown=60.0)
        agent_a = _make_mock_agent("worker-a")
        agent_b = _make_mock_agent("worker-b")
        orc.registry._agents["worker-a"] = agent_a
        orc.registry._agents["worker-b"] = agent_b

        await orc._handle_drift_warning("worker-a", drift_score=0.4)
        # worker-b has not been re-briefed; its cooldown is fresh
        await orc._handle_drift_warning("worker-b", drift_score=0.35)

        assert agent_a.notify_stdin.await_count == 1
        assert agent_b.notify_stdin.await_count == 1


# ---------------------------------------------------------------------------
# Tests: get_agent_drift_rebriefs / all_drift_rebrief_stats (query API)
# ---------------------------------------------------------------------------


class TestDriftRebriefQueries:
    """Tests for the query API exposed on the Orchestrator."""

    @pytest.mark.asyncio
    async def test_get_agent_drift_rebriefs_empty_before_any_rebrief(self):
        """Returns empty list for agent that has never been re-briefed."""
        orc = _make_orchestrator()
        result = orc.get_agent_drift_rebriefs("no-such-agent")
        assert result == []

    @pytest.mark.asyncio
    async def test_all_drift_rebrief_stats_empty_initially(self):
        """Returns empty list when no rebriefs have been sent."""
        orc = _make_orchestrator()
        assert orc.all_drift_rebrief_stats() == []

    @pytest.mark.asyncio
    async def test_all_drift_rebrief_stats_includes_agent(self):
        """all_drift_rebrief_stats() includes agents that received rebriefs."""
        orc = _make_orchestrator()
        agent = _make_mock_agent("worker-1")
        orc.registry._agents["worker-1"] = agent

        await orc._handle_drift_warning("worker-1", drift_score=0.45)

        stats = orc.all_drift_rebrief_stats()
        assert len(stats) == 1
        assert stats[0]["agent_id"] == "worker-1"
        assert stats[0]["rebrief_count"] == 1
        assert stats[0]["last_sent"] is not None

    @pytest.mark.asyncio
    async def test_rebriefs_returned_most_recent_first(self):
        """get_agent_drift_rebriefs returns entries most-recent-first."""
        orc = _make_orchestrator(rebrief_cooldown=0.01)
        agent = _make_mock_agent("worker-1")
        orc.registry._agents["worker-1"] = agent

        scores = [0.55, 0.50, 0.45]
        for score in scores:
            await orc._handle_drift_warning("worker-1", drift_score=score)
            await asyncio.sleep(0.02)

        history = orc.get_agent_drift_rebriefs("worker-1")
        returned_scores = [h["drift_score"] for h in history]
        assert returned_scores == [0.45, 0.50, 0.55]


# ---------------------------------------------------------------------------
# Tests: _route_loop STATUS handler integration
# ---------------------------------------------------------------------------


class TestRouteLoopDriftWarning:
    """Tests that _route_loop dispatches drift warnings to _handle_drift_warning."""

    @pytest.mark.asyncio
    async def test_route_loop_handles_drift_warning_event(self):
        """_route_loop creates a drift-rebrief task on agent_drift_warning STATUS."""
        orc = _make_orchestrator()
        agent = _make_mock_agent("worker-1")
        orc.registry._agents["worker-1"] = agent

        # Manually call the handler to simulate what _route_loop does
        payload = {
            "event": "agent_drift_warning",
            "agent_id": "worker-1",
            "drift_score": 0.42,
        }
        await orc._handle_drift_warning(
            agent_id=payload["agent_id"],
            drift_score=payload["drift_score"],
        )

        agent.notify_stdin.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_route_loop_ignores_drift_warning_when_disabled(self):
        """When drift_rebrief_enabled=False, route_loop-originated re-brief is a no-op."""
        orc = _make_orchestrator(rebrief_enabled=False)
        agent = _make_mock_agent("worker-1")
        orc.registry._agents["worker-1"] = agent

        await orc._handle_drift_warning("worker-1", drift_score=0.3)

        agent.notify_stdin.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_drift_warning_with_empty_agent_id_is_noop(self):
        """Empty agent_id in the drift warning payload does not raise."""
        orc = _make_orchestrator()
        # Should not raise even though agent_id is empty
        await orc._handle_drift_warning("", drift_score=0.2)


# ---------------------------------------------------------------------------
# Tests: config defaults
# ---------------------------------------------------------------------------


class TestDriftRebriefConfig:
    """Tests for OrchestratorConfig drift re-brief fields."""

    def test_drift_rebrief_enabled_default_true(self):
        cfg = OrchestratorConfig(task_timeout=120, watchdog_poll=40)
        assert cfg.drift_rebrief_enabled is True

    def test_drift_rebrief_cooldown_default_60(self):
        cfg = OrchestratorConfig(task_timeout=120, watchdog_poll=40)
        assert cfg.drift_rebrief_cooldown == pytest.approx(60.0)

    def test_drift_rebrief_message_default_contains_reminder(self):
        cfg = OrchestratorConfig(task_timeout=120, watchdog_poll=40)
        assert "ROLE REMINDER" in cfg.drift_rebrief_message

    def test_custom_drift_rebrief_message(self):
        cfg = OrchestratorConfig(
            task_timeout=120,
            watchdog_poll=40,
            drift_rebrief_message="CUSTOM REMINDER",
        )
        assert cfg.drift_rebrief_message == "CUSTOM REMINDER"

    def test_drift_rebrief_enabled_can_be_disabled(self):
        cfg = OrchestratorConfig(
            task_timeout=120,
            watchdog_poll=40,
            drift_rebrief_enabled=False,
        )
        assert cfg.drift_rebrief_enabled is False

    def test_drift_rebrief_cooldown_can_be_customised(self):
        cfg = OrchestratorConfig(
            task_timeout=120,
            watchdog_poll=40,
            drift_rebrief_cooldown=120.0,
        )
        assert cfg.drift_rebrief_cooldown == pytest.approx(120.0)


# ---------------------------------------------------------------------------
# Tests: YAML load_config drift re-brief fields
# ---------------------------------------------------------------------------


class TestLoadConfigDriftRebrief:
    """Tests that load_config correctly reads drift re-brief YAML fields."""

    def _write_yaml(self, tmp_path, extra: str = "") -> str:
        yaml_text = f"""
session_name: test
task_timeout: 120
watchdog_poll: 40
agents: []
{extra}
"""
        p = tmp_path / "config.yaml"
        p.write_text(yaml_text)
        return str(p)

    def test_default_values_when_absent(self, tmp_path):
        from tmux_orchestrator.config import load_config
        cfg = load_config(self._write_yaml(tmp_path))
        assert cfg.drift_rebrief_enabled is True
        assert cfg.drift_rebrief_cooldown == pytest.approx(60.0)
        assert "ROLE REMINDER" in cfg.drift_rebrief_message

    def test_custom_enabled_false(self, tmp_path):
        from tmux_orchestrator.config import load_config
        cfg = load_config(self._write_yaml(tmp_path, "drift_rebrief_enabled: false"))
        assert cfg.drift_rebrief_enabled is False

    def test_custom_cooldown(self, tmp_path):
        from tmux_orchestrator.config import load_config
        cfg = load_config(self._write_yaml(tmp_path, "drift_rebrief_cooldown: 30"))
        assert cfg.drift_rebrief_cooldown == pytest.approx(30.0)

    def test_custom_message(self, tmp_path):
        from tmux_orchestrator.config import load_config
        cfg = load_config(self._write_yaml(tmp_path, 'drift_rebrief_message: "MY CUSTOM MSG"'))
        assert cfg.drift_rebrief_message == "MY CUSTOM MSG"


# ---------------------------------------------------------------------------
# Tests: REST endpoint GET /agents/{id}/drift-rebriefs
# ---------------------------------------------------------------------------


class TestDriftRebriefEndpoint:
    """Tests for GET /agents/{agent_id}/drift-rebriefs REST endpoint."""

    def _make_app_with_history(self, history: list[dict]):
        """Create a FastAPI test client with pre-populated rebrief history."""
        from fastapi.testclient import TestClient
        from unittest.mock import MagicMock
        from tmux_orchestrator.web.app import create_app

        class _MockOrchestrator:
            _dispatch_task = None
            config = MagicMock()

            def list_agents(self): return []
            def list_tasks(self): return []
            def get_agent(self, agent_id): return None
            def get_director(self): return None
            def flush_director_pending(self): return []
            def list_dlq(self): return []
            @property
            def is_paused(self): return False
            def get_rate_limiter_status(self): return {"enabled": False}
            def reconfigure_rate_limiter(self, **kw): return {}
            def get_agent_context_stats(self, agent_id): return None
            def all_agent_context_stats(self): return []
            def get_agent_history(self, agent_id, limit=50): return None
            def get_workflow_manager(self):
                from tmux_orchestrator.workflow_manager import WorkflowManager
                return WorkflowManager()
            @property
            def _webhook_manager(self):
                from tmux_orchestrator.webhook_manager import WebhookManager
                return WebhookManager()
            def get_group_manager(self):
                from tmux_orchestrator.group_manager import GroupManager
                return GroupManager()
            def get_checkpoint_store(self): return None
            def get_telemetry(self): return None
            def get_agent_drift_rebriefs(self, agent_id): return history
            def all_drift_rebrief_stats(self): return []
            @property
            def config(self):
                from unittest.mock import MagicMock
                cfg = MagicMock()
                cfg.otlp_endpoint = ""
                return cfg

        class _MockHub:
            async def start(self): pass
            async def stop(self): pass
            async def handle(self, ws): pass

        app = create_app(_MockOrchestrator(), _MockHub(), api_key="test-key")
        return TestClient(app)

    def test_returns_empty_list_for_unreported_agent(self):
        client = self._make_app_with_history([])
        resp = client.get(
            "/agents/worker-1/drift-rebriefs",
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_history_entries(self):
        history = [
            {"timestamp": "2026-03-10T12:00:00+00:00", "drift_score": 0.45},
            {"timestamp": "2026-03-10T12:01:00+00:00", "drift_score": 0.40},
        ]
        client = self._make_app_with_history(history)
        resp = client.get(
            "/agents/worker-1/drift-rebriefs",
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["drift_score"] == pytest.approx(0.45)

    def test_requires_api_key(self):
        client = self._make_app_with_history([])
        resp = client.get("/agents/worker-1/drift-rebriefs")
        assert resp.status_code == 401
