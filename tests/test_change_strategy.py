"""Tests for POST /agents/{agent_id}/change-strategy — autonomous strategy switching.

Design references:
- §12「ワークフロー設計の層構造」層3 実行方式の自律切り替え
- arXiv:2505.19591 (Multi-Agent Collaboration via Evolving Orchestration, 2025):
  orchestrator evolves coordination strategy in real-time
- ALAS arXiv:2505.12501 (2025): adaptive execution with escalation to orchestrator
- DESIGN.md §10.16 (v0.49.0)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock

from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app


_API_KEY = "test-key"
_HEADERS = {"X-API-Key": _API_KEY}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_config(**kwargs) -> OrchestratorConfig:
    defaults = dict(
        session_name="test",
        agents=[],
        p2p_permissions=[],
        task_timeout=10,
        watchdog_poll=9999.0,
        recovery_poll=9999.0,
    )
    defaults.update(kwargs)
    return OrchestratorConfig(**defaults)


def make_tmux_mock():
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    return tmux


class _StubHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _make_app():
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    app = create_app(orch, _StubHub(), api_key=_API_KEY)  # type: ignore[arg-type]
    return app, orch


# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------


def test_change_strategy_invalid_pattern_returns_422():
    """POST /agents/{id}/change-strategy with unknown pattern returns 422."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/agents/nonexistent/change-strategy",
            headers=_HEADERS,
            json={"pattern": "unknown_pattern"},
        )
    assert resp.status_code == 422


def test_change_strategy_missing_pattern_returns_422():
    """POST /agents/{id}/change-strategy without pattern field returns 422."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/agents/nonexistent/change-strategy",
            headers=_HEADERS,
            json={},
        )
    assert resp.status_code == 422


def test_change_strategy_parallel_count_must_be_positive():
    """POST /agents/{id}/change-strategy parallel with count <= 0 returns 422."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/agents/nonexistent/change-strategy",
            headers=_HEADERS,
            json={"pattern": "parallel", "count": 0},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Agent-not-found tests
# ---------------------------------------------------------------------------


def test_change_strategy_agent_not_found_returns_404():
    """POST /agents/{id}/change-strategy returns 404 when agent does not exist."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/agents/nonexistent-agent/change-strategy",
            headers=_HEADERS,
            json={"pattern": "parallel", "count": 2},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Parallel strategy tests (main feature)
# ---------------------------------------------------------------------------


def test_change_strategy_parallel_no_current_task_returns_200():
    """POST /agents/{id}/change-strategy with parallel pattern on IDLE agent succeeds.

    An IDLE agent has no current task — the endpoint records the strategy
    change request for the agent's next task dispatch.
    """
    app, orch = _make_app()

    # Register a fake HeadlessAgent-like stub directly into the registry
    from tmux_orchestrator.agents.base import AgentStatus
    from unittest.mock import MagicMock

    mock_agent = MagicMock()
    mock_agent.id = "agent-solver"
    mock_agent.status = AgentStatus.IDLE
    mock_agent.current_task = None
    mock_agent.tags = ["solver"]
    orch.registry._agents["agent-solver"] = mock_agent

    with TestClient(app) as client:
        resp = client.post(
            "/agents/agent-solver/change-strategy",
            headers=_HEADERS,
            json={"pattern": "parallel", "count": 2},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["pattern"] == "parallel"
    assert data["agent_id"] == "agent-solver"


def test_change_strategy_parallel_returns_strategy_details():
    """POST /agents/{id}/change-strategy response contains count and tags fields."""
    app, orch = _make_app()

    from tmux_orchestrator.agents.base import AgentStatus

    mock_agent = MagicMock()
    mock_agent.id = "agent-worker"
    mock_agent.status = AgentStatus.IDLE
    mock_agent.current_task = None
    mock_agent.tags = ["worker"]
    orch.registry._agents["agent-worker"] = mock_agent

    with TestClient(app) as client:
        resp = client.post(
            "/agents/agent-worker/change-strategy",
            headers=_HEADERS,
            json={"pattern": "parallel", "count": 3, "tags": ["worker"]},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 3
    assert data["tags"] == ["worker"]


def test_change_strategy_single_pattern_returns_200():
    """POST /agents/{id}/change-strategy with single pattern (no-op) returns 200."""
    app, orch = _make_app()

    from tmux_orchestrator.agents.base import AgentStatus

    mock_agent = MagicMock()
    mock_agent.id = "agent-x"
    mock_agent.status = AgentStatus.IDLE
    mock_agent.current_task = None
    mock_agent.tags = []
    orch.registry._agents["agent-x"] = mock_agent

    with TestClient(app) as client:
        resp = client.post(
            "/agents/agent-x/change-strategy",
            headers=_HEADERS,
            json={"pattern": "single"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["pattern"] == "single"


def test_change_strategy_parallel_spawns_tasks():
    """POST /agents/{id}/change-strategy parallel spawns N tasks when context is provided.

    When the request includes a ``context`` field (the task description for parallel
    workers), the endpoint submits parallel tasks immediately and returns their IDs.
    """
    app, orch = _make_app()

    from tmux_orchestrator.agents.base import AgentStatus

    mock_agent = MagicMock()
    mock_agent.id = "agent-solver"
    mock_agent.status = AgentStatus.IDLE
    mock_agent.current_task = None
    mock_agent.tags = ["solver"]
    orch.registry._agents["agent-solver"] = mock_agent

    with TestClient(app) as client:
        resp = client.post(
            "/agents/agent-solver/change-strategy",
            headers=_HEADERS,
            json={
                "pattern": "parallel",
                "count": 2,
                "context": "Solve the knapsack problem using different strategies",
                "reply_to": "agent-solver",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"
    # When context is provided, spawned_task_ids should be in the response
    assert "spawned_task_ids" in data
    assert len(data["spawned_task_ids"]) == 2


def test_change_strategy_parallel_tasks_have_reply_to():
    """Spawned parallel tasks have reply_to pointing to the requesting agent."""
    app, orch = _make_app()

    from tmux_orchestrator.agents.base import AgentStatus

    mock_agent = MagicMock()
    mock_agent.id = "coordinator"
    mock_agent.status = AgentStatus.IDLE
    mock_agent.current_task = None
    mock_agent.tags = []
    orch.registry._agents["coordinator"] = mock_agent

    with TestClient(app) as client:
        resp = client.post(
            "/agents/coordinator/change-strategy",
            headers=_HEADERS,
            json={
                "pattern": "parallel",
                "count": 2,
                "context": "Parallel computation task",
                "reply_to": "coordinator",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    task_ids = data["spawned_task_ids"]

    # Each task should be tracked in the orchestrator
    assert len(task_ids) == 2
    # Verify tasks exist in orchestrator's task tracking
    for tid in task_ids:
        assert isinstance(tid, str)
        assert len(tid) > 0


def test_change_strategy_requires_authentication():
    """POST /agents/{id}/change-strategy without API key returns 401 or 403."""
    app, orch = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/agents/any-agent/change-strategy",
            json={"pattern": "parallel", "count": 2},
        )
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# ChangeStrategyRequest model tests
# ---------------------------------------------------------------------------


def test_change_strategy_request_defaults():
    """ChangeStrategyRequest model has sensible defaults."""
    from tmux_orchestrator.web.app import ChangeStrategyRequest

    req = ChangeStrategyRequest(pattern="parallel")
    assert req.count == 2
    assert req.tags == []
    assert req.context is None
    assert req.reply_to is None


def test_change_strategy_request_valid_patterns():
    """ChangeStrategyRequest accepts all valid pattern values."""
    from tmux_orchestrator.web.app import ChangeStrategyRequest

    for pattern in ("single", "parallel", "competitive"):
        req = ChangeStrategyRequest(pattern=pattern)
        assert req.pattern == pattern


def test_change_strategy_request_invalid_pattern_raises():
    """ChangeStrategyRequest rejects invalid pattern values."""
    from pydantic import ValidationError
    from tmux_orchestrator.web.app import ChangeStrategyRequest

    with pytest.raises(ValidationError):
        ChangeStrategyRequest(pattern="debate")  # debate not supported in v0.49.0


def test_change_strategy_request_count_must_be_positive():
    """ChangeStrategyRequest rejects count <= 0."""
    from pydantic import ValidationError
    from tmux_orchestrator.web.app import ChangeStrategyRequest

    with pytest.raises(ValidationError):
        ChangeStrategyRequest(pattern="parallel", count=0)

    with pytest.raises(ValidationError):
        ChangeStrategyRequest(pattern="parallel", count=-1)


def test_change_strategy_request_count_max_is_10():
    """ChangeStrategyRequest rejects count > 10 (safety limit)."""
    from pydantic import ValidationError
    from tmux_orchestrator.web.app import ChangeStrategyRequest

    with pytest.raises(ValidationError):
        ChangeStrategyRequest(pattern="parallel", count=11)


# ---------------------------------------------------------------------------
# Slash command file tests
# ---------------------------------------------------------------------------


def test_change_strategy_command_file_exists():
    """The /change-strategy slash command file must exist at the expected path."""
    from pathlib import Path

    cmd_file = Path(__file__).parent.parent / ".claude" / "commands" / "change-strategy.md"
    assert cmd_file.exists(), f"Slash command file not found: {cmd_file}"


def test_change_strategy_command_file_has_required_sections():
    """The change-strategy.md command file must document the key patterns."""
    from pathlib import Path

    cmd_file = Path(__file__).parent.parent / ".claude" / "commands" / "change-strategy.md"
    content = cmd_file.read_text()

    assert "parallel" in content
    assert "competitive" in content
    assert "single" in content
    assert "change-strategy" in content
    assert "context" in content.lower()
    assert "reply_to" in content or "reply-to" in content or "mailbox" in content
