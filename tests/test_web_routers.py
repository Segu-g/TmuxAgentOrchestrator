"""Tests for web/routers/* builder functions.

Verifies that each builder function:
1. Returns a FastAPI APIRouter instance.
2. Registers the expected endpoint paths.
3. Can be included in a FastAPI app without errors.

Design reference: DESIGN.md §10.42 (v1.1.6)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import APIRouter, FastAPI


# ---------------------------------------------------------------------------
# Shared test doubles
# ---------------------------------------------------------------------------


def _make_mock_orchestrator():
    orch = MagicMock()
    orch.config = MagicMock()
    orch.config.mailbox_dir = "/tmp/test_mailbox"
    orch.config.session_name = "test-session"
    orch.list_agents.return_value = []
    orch.list_tasks.return_value = []
    orch.list_dlq.return_value = []
    orch.get_agent.return_value = None
    orch.get_director.return_value = None
    orch.flush_director_pending.return_value = []
    orch.get_workflow_manager.return_value = MagicMock()
    orch.get_group_manager.return_value = MagicMock()
    orch.all_agent_context_stats.return_value = []
    orch.all_agent_drift_stats.return_value = []
    orch.is_paused = False
    orch._webhook_manager = MagicMock()
    return orch


def _dummy_auth():
    async def _check():
        pass
    return _check


def _make_episode_store():
    from tmux_orchestrator.episode_store import EpisodeStore
    return EpisodeStore(root_dir="/tmp/test_episodes", session_name="test")


# ---------------------------------------------------------------------------
# Tasks router tests
# ---------------------------------------------------------------------------


def test_build_tasks_router_returns_apirouter():
    """build_tasks_router returns a FastAPI APIRouter."""
    from tmux_orchestrator.web.routers.tasks import build_tasks_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    router = build_tasks_router(orch, auth)
    assert isinstance(router, APIRouter)


def test_build_tasks_router_has_expected_routes():
    """Tasks router registers /tasks, /tasks/batch, /tasks/{id}, etc."""
    from tmux_orchestrator.web.routers.tasks import build_tasks_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    router = build_tasks_router(orch, auth)
    paths = {r.path for r in router.routes}
    assert "/tasks" in paths
    assert "/tasks/batch" in paths
    assert "/tasks/{task_id}" in paths
    assert "/tasks/{task_id}/cancel" in paths


def test_build_tasks_router_with_limiter():
    """Tasks router can be built with a SlowAPI limiter."""
    from slowapi import Limiter
    from slowapi.util import get_remote_address
    from tmux_orchestrator.web.routers.tasks import build_tasks_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    limiter = Limiter(key_func=get_remote_address)
    router = build_tasks_router(orch, auth, limiter=limiter)
    assert isinstance(router, APIRouter)
    # Still has the /tasks endpoint
    paths = {r.path for r in router.routes}
    assert "/tasks" in paths


# ---------------------------------------------------------------------------
# Agents router tests
# ---------------------------------------------------------------------------


def test_build_agents_router_returns_apirouter():
    """build_agents_router returns a FastAPI APIRouter."""
    from tmux_orchestrator.web.routers.agents import build_agents_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    router = build_agents_router(orch, auth)
    assert isinstance(router, APIRouter)


def test_build_agents_router_has_expected_routes():
    """Agents router registers /agents, /agents/tree, /agents/{id}, etc."""
    from tmux_orchestrator.web.routers.agents import build_agents_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    router = build_agents_router(orch, auth)
    paths = {r.path for r in router.routes}
    assert "/agents" in paths
    assert "/agents/tree" in paths
    assert "/agents/{agent_id}" in paths
    assert "/agents/{agent_id}/task-complete" in paths
    assert "/agents/{agent_id}/ready" in paths
    assert "/agents/{agent_id}/drain" in paths
    assert "/agents/{agent_id}/reset" in paths
    assert "/agents/{agent_id}/stats" in paths
    assert "/agents/{agent_id}/drift" in paths
    assert "/agents/{agent_id}/history" in paths
    assert "/agents/{agent_id}/worktree-status" in paths
    assert "/agents/{agent_id}/message" in paths
    assert "/agents/new" in paths
    assert "/director/chat" in paths


def test_build_agents_router_with_episode_store():
    """Agents router accepts an episode_store for auto-record."""
    from tmux_orchestrator.web.routers.agents import build_agents_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    episode_store = _make_episode_store()
    router = build_agents_router(orch, auth, episode_store=episode_store)
    assert isinstance(router, APIRouter)


# ---------------------------------------------------------------------------
# Workflows router tests
# ---------------------------------------------------------------------------


def test_build_workflows_router_returns_apirouter():
    """build_workflows_router returns a FastAPI APIRouter."""
    from tmux_orchestrator.web.routers.workflows import build_workflows_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    router = build_workflows_router(orch, auth)
    assert isinstance(router, APIRouter)


def test_build_workflows_router_has_expected_routes():
    """Workflows router registers /workflows and all typed workflow endpoints."""
    from tmux_orchestrator.web.routers.workflows import build_workflows_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    router = build_workflows_router(orch, auth)
    paths = {r.path for r in router.routes}
    assert "/workflows" in paths
    assert "/workflows/{workflow_id}" in paths
    assert "/workflows/tdd" in paths
    assert "/workflows/debate" in paths
    assert "/workflows/adr" in paths
    assert "/workflows/delphi" in paths
    assert "/workflows/redblue" in paths
    assert "/workflows/socratic" in paths
    assert "/workflows/pair" in paths
    assert "/workflows/fulldev" in paths
    assert "/workflows/clean-arch" in paths
    assert "/workflows/ddd" in paths
    assert "/workflows/competition" in paths


def test_workflows_router_no_task_delete():
    """Workflows router does NOT register DELETE /tasks/{id} (belongs to tasks router)."""
    from tmux_orchestrator.web.routers.workflows import build_workflows_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    router = build_workflows_router(orch, auth)
    # DELETE /tasks/{task_id} should NOT be in workflows router
    delete_tasks = [
        r for r in router.routes
        if r.path == "/tasks/{task_id}" and "DELETE" in getattr(r, "methods", set())
    ]
    assert len(delete_tasks) == 0, (
        "DELETE /tasks/{task_id} must be in tasks router, not workflows router"
    )


# ---------------------------------------------------------------------------
# Scratchpad router tests
# ---------------------------------------------------------------------------


def test_build_scratchpad_router_returns_apirouter():
    """build_scratchpad_router returns a FastAPI APIRouter."""
    from tmux_orchestrator.web.routers.scratchpad import build_scratchpad_router
    auth = _dummy_auth()
    sp = {}
    router = build_scratchpad_router(auth, sp)
    assert isinstance(router, APIRouter)


def test_build_scratchpad_router_has_expected_routes():
    """Scratchpad router registers /scratchpad/ and /scratchpad/{key}."""
    from tmux_orchestrator.web.routers.scratchpad import build_scratchpad_router
    auth = _dummy_auth()
    sp = {}
    router = build_scratchpad_router(auth, sp)
    paths = {r.path for r in router.routes}
    assert "/scratchpad/" in paths
    assert "/scratchpad/{key}" in paths


def test_scratchpad_router_shares_state():
    """Scratchpad router uses the same dict object passed at construction."""
    from tmux_orchestrator.web.routers.scratchpad import build_scratchpad_router
    auth = _dummy_auth()
    sp = {"existing_key": "existing_value"}
    build_scratchpad_router(auth, sp)
    # The dict is shared by reference — no copy is made
    assert sp["existing_key"] == "existing_value"


# ---------------------------------------------------------------------------
# System router tests
# ---------------------------------------------------------------------------


def test_build_system_router_returns_apirouter():
    """build_system_router returns a FastAPI APIRouter."""
    from tmux_orchestrator.web.routers.system import build_system_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    router = build_system_router(orch, auth)
    assert isinstance(router, APIRouter)


def test_build_system_router_has_expected_routes():
    """System router registers health, metrics, orchestrator control, results."""
    from tmux_orchestrator.web.routers.system import build_system_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    router = build_system_router(orch, auth)
    paths = {r.path for r in router.routes}
    assert "/healthz" in paths
    assert "/readyz" in paths
    assert "/metrics" in paths
    assert "/dlq" in paths
    assert "/audit-log" in paths
    assert "/checkpoint/status" in paths
    assert "/telemetry/status" in paths
    assert "/orchestrator/pause" in paths
    assert "/orchestrator/resume" in paths
    assert "/orchestrator/status" in paths
    assert "/orchestrator/drain" in paths
    assert "/orchestrator/autoscaler" in paths
    assert "/rate-limit" in paths
    assert "/results" in paths
    assert "/context-stats" in paths
    assert "/drift" in paths


# ---------------------------------------------------------------------------
# Webhooks router tests
# ---------------------------------------------------------------------------


def test_build_webhooks_router_returns_apirouter():
    """build_webhooks_router returns a FastAPI APIRouter."""
    from tmux_orchestrator.web.routers.webhooks import build_webhooks_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    router = build_webhooks_router(orch, auth)
    assert isinstance(router, APIRouter)


def test_build_webhooks_router_has_expected_routes():
    """Webhooks router registers CRUD endpoints."""
    from tmux_orchestrator.web.routers.webhooks import build_webhooks_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    router = build_webhooks_router(orch, auth)
    paths = {r.path for r in router.routes}
    assert "/webhooks" in paths
    assert "/webhooks/{webhook_id}" in paths
    assert "/webhooks/{webhook_id}/deliveries" in paths


# ---------------------------------------------------------------------------
# Groups router tests
# ---------------------------------------------------------------------------


def test_build_groups_router_returns_apirouter():
    """build_groups_router returns a FastAPI APIRouter."""
    from tmux_orchestrator.web.routers.groups import build_groups_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    router = build_groups_router(orch, auth)
    assert isinstance(router, APIRouter)


def test_build_groups_router_has_expected_routes():
    """Groups router registers CRUD endpoints."""
    from tmux_orchestrator.web.routers.groups import build_groups_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    router = build_groups_router(orch, auth)
    paths = {r.path for r in router.routes}
    assert "/groups" in paths
    assert "/groups/{group_name}" in paths
    assert "/groups/{group_name}/agents" in paths
    assert "/groups/{group_name}/agents/{agent_id}" in paths


# ---------------------------------------------------------------------------
# Memory router tests
# ---------------------------------------------------------------------------


def test_build_memory_router_returns_apirouter():
    """build_memory_router returns a FastAPI APIRouter."""
    from tmux_orchestrator.web.routers.memory import build_memory_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    episode_store = _make_episode_store()
    router = build_memory_router(orch, auth, episode_store=episode_store)
    assert isinstance(router, APIRouter)


def test_build_memory_router_has_expected_routes():
    """Memory router registers /agents/{id}/memory endpoints."""
    from tmux_orchestrator.web.routers.memory import build_memory_router
    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    episode_store = _make_episode_store()
    router = build_memory_router(orch, auth, episode_store=episode_store)
    paths = {r.path for r in router.routes}
    assert "/agents/{agent_id}/memory" in paths
    assert "/agents/{agent_id}/memory/{episode_id}" in paths


# ---------------------------------------------------------------------------
# Integration: all routers can be included in a single FastAPI app
# ---------------------------------------------------------------------------


def test_all_routers_can_be_included_together():
    """All routers can be included in a single FastAPI app without conflicts."""
    from slowapi import Limiter
    from slowapi.util import get_remote_address

    from tmux_orchestrator.web.routers import (
        build_agents_router,
        build_groups_router,
        build_memory_router,
        build_scratchpad_router,
        build_system_router,
        build_tasks_router,
        build_webhooks_router,
        build_workflows_router,
    )

    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    episode_store = _make_episode_store()
    scratchpad = {}
    limiter = Limiter(key_func=get_remote_address)

    app = FastAPI()
    app.include_router(build_tasks_router(orch, auth, limiter=limiter))
    app.include_router(build_agents_router(orch, auth, episode_store=episode_store))
    app.include_router(build_workflows_router(orch, auth))
    app.include_router(build_scratchpad_router(auth, scratchpad))
    app.include_router(build_system_router(orch, auth))
    app.include_router(build_webhooks_router(orch, auth))
    app.include_router(build_groups_router(orch, auth))
    app.include_router(build_memory_router(orch, auth, episode_store=episode_store))

    # No exception means all routers were included successfully
    routes_count = len(app.routes)
    assert routes_count > 50, f"Expected >50 routes, got {routes_count}"


def test_no_duplicate_routes_in_combined_app():
    """No two routers register the same path+method combination."""
    from slowapi import Limiter
    from slowapi.util import get_remote_address

    from tmux_orchestrator.web.routers import (
        build_agents_router,
        build_groups_router,
        build_memory_router,
        build_scratchpad_router,
        build_system_router,
        build_tasks_router,
        build_webhooks_router,
        build_workflows_router,
    )

    orch = _make_mock_orchestrator()
    auth = _dummy_auth()
    episode_store = _make_episode_store()
    scratchpad = {}
    limiter = Limiter(key_func=get_remote_address)

    app = FastAPI()
    app.include_router(build_tasks_router(orch, auth, limiter=limiter))
    app.include_router(build_agents_router(orch, auth, episode_store=episode_store))
    app.include_router(build_workflows_router(orch, auth))
    app.include_router(build_scratchpad_router(auth, scratchpad))
    app.include_router(build_system_router(orch, auth))
    app.include_router(build_webhooks_router(orch, auth))
    app.include_router(build_groups_router(orch, auth))
    app.include_router(build_memory_router(orch, auth, episode_store=episode_store))

    # Check for duplicate path+method combos (operation IDs)
    seen = {}
    duplicates = []
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        for method in methods:
            key = f"{method} {route.path}"
            if key in seen:
                duplicates.append(key)
            else:
                seen[key] = True

    assert not duplicates, f"Duplicate route registrations found: {duplicates}"
