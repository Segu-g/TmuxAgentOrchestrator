"""Tests for GroupManager and agent group dispatch (v0.31.0)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.group_manager import GroupManager
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app
from tmux_orchestrator.web.ws import WebSocketHub


# ---------------------------------------------------------------------------
# Helpers shared with test_orchestrator.py
# ---------------------------------------------------------------------------


class DummyAgent(Agent):
    """Minimal agent that records dispatched tasks."""

    def __init__(self, agent_id: str, bus: Bus, tags: list[str] | None = None) -> None:
        super().__init__(agent_id, bus)
        self.dispatched: list[Task] = []
        self.dispatched_event: asyncio.Event = asyncio.Event()
        self.tags: list[str] = tags or []

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


def make_config(**kwargs) -> OrchestratorConfig:
    defaults = dict(
        session_name="test",
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
# GroupManager unit tests
# ---------------------------------------------------------------------------


def test_create_group_returns_true():
    gm = GroupManager()
    assert gm.create("workers") is True


def test_create_group_duplicate_returns_false():
    gm = GroupManager()
    gm.create("workers")
    assert gm.create("workers") is False


def test_create_group_with_initial_agents():
    gm = GroupManager()
    gm.create("gpu", ["a1", "a2"])
    assert gm.get("gpu") == {"a1", "a2"}


def test_get_returns_none_for_unknown_group():
    gm = GroupManager()
    assert gm.get("nonexistent") is None


def test_get_returns_copy_not_reference():
    gm = GroupManager()
    gm.create("workers", ["a1"])
    members = gm.get("workers")
    members.add("a99")  # modifying the copy should not affect the group
    assert gm.get("workers") == {"a1"}


def test_delete_group_returns_true():
    gm = GroupManager()
    gm.create("workers")
    assert gm.delete("workers") is True


def test_delete_group_returns_false_for_unknown():
    gm = GroupManager()
    assert gm.delete("nonexistent") is False


def test_list_all_empty():
    gm = GroupManager()
    assert gm.list_all() == []


def test_list_all_returns_sorted_by_name():
    gm = GroupManager()
    gm.create("z-group", ["a1"])
    gm.create("a-group", ["a2"])
    names = [e["name"] for e in gm.list_all()]
    assert names == ["a-group", "z-group"]


def test_list_all_agent_ids_sorted():
    gm = GroupManager()
    gm.create("workers", ["b2", "a1", "c3"])
    entry = gm.list_all()[0]
    assert entry["agent_ids"] == ["a1", "b2", "c3"]


def test_add_agent_to_group_returns_true():
    gm = GroupManager()
    gm.create("workers")
    assert gm.add_agent("workers", "a1") is True


def test_add_agent_to_nonexistent_group_returns_false():
    gm = GroupManager()
    assert gm.add_agent("nonexistent", "a1") is False


def test_remove_agent_from_group_returns_true():
    gm = GroupManager()
    gm.create("workers", ["a1"])
    assert gm.remove_agent("workers", "a1") is True
    assert gm.get("workers") == set()


def test_remove_agent_nonmember_returns_false():
    gm = GroupManager()
    gm.create("workers", ["a1"])
    assert gm.remove_agent("workers", "a99") is False


def test_remove_agent_from_nonexistent_group_returns_false():
    gm = GroupManager()
    assert gm.remove_agent("nonexistent", "a1") is False


def test_agent_in_multiple_groups():
    gm = GroupManager()
    gm.create("python-workers", ["a1"])
    gm.create("fast-workers", ["a1", "a2"])
    assert "a1" in gm.get("python-workers")
    assert "a1" in gm.get("fast-workers")


def test_get_agent_groups():
    gm = GroupManager()
    gm.create("python-workers", ["a1"])
    gm.create("fast-workers", ["a1", "a2"])
    assert gm.get_agent_groups("a1") == ["fast-workers", "python-workers"]


def test_contains_operator():
    gm = GroupManager()
    gm.create("workers")
    assert "workers" in gm
    assert "nonexistent" not in gm


def test_len_operator():
    gm = GroupManager()
    assert len(gm) == 0
    gm.create("g1")
    gm.create("g2")
    assert len(gm) == 2


# ---------------------------------------------------------------------------
# Orchestrator + GroupManager integration tests
# ---------------------------------------------------------------------------


async def test_find_idle_worker_group_filter_dispatches_only_to_group():
    """A task with target_group only goes to agents in that group."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    a1 = DummyAgent("a1", bus)
    a2 = DummyAgent("a2", bus)
    orch.register_agent(a1)
    orch.register_agent(a2)
    orch.get_group_manager().create("python-workers", ["a1"])

    await orch.start()
    try:
        task = await orch.submit_task("hello", target_group="python-workers")
        await asyncio.wait_for(a1.dispatched_event.wait(), timeout=2.0)
        assert any(t.id == task.id for t in a1.dispatched)
        assert len(a2.dispatched) == 0
    finally:
        await orch.stop()


async def test_find_idle_worker_group_and_tags_both_required():
    """target_group + required_tags both act as AND-filters."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    # a1: in group, has tag; a2: in group, no tag; a3: not in group, has tag
    a1 = DummyAgent("a1", bus, tags=["gpu"])
    a2 = DummyAgent("a2", bus, tags=[])
    a3 = DummyAgent("a3", bus, tags=["gpu"])
    orch.register_agent(a1)
    orch.register_agent(a2)
    orch.register_agent(a3)
    orch.get_group_manager().create("gpu-group", ["a1", "a2"])

    await orch.start()
    try:
        task = await orch.submit_task("hello", target_group="gpu-group", required_tags=["gpu"])
        await asyncio.wait_for(a1.dispatched_event.wait(), timeout=2.0)
        assert any(t.id == task.id for t in a1.dispatched)
        assert len(a2.dispatched) == 0
        assert len(a3.dispatched) == 0
    finally:
        await orch.stop()


async def test_target_group_no_idle_members_requeues():
    """Task is NOT dispatched to out-of-group agent when all group members are busy."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(dlq_max_retries=3)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    a_in_group = DummyAgent("in-group", bus)
    a_out_of_group = DummyAgent("out-of-group", bus)
    orch.register_agent(a_in_group)
    orch.register_agent(a_out_of_group)
    gm = orch.get_group_manager()
    gm.create("special", ["in-group"])

    await orch.start()
    try:
        # Make the in-group agent busy
        a_in_group.status = AgentStatus.BUSY
        # Submit a task targeting the group
        await orch.submit_task("test task", target_group="special")
        # Give dispatch loop a chance to run
        await asyncio.sleep(0.5)
        # The out-of-group agent must NOT have received the task
        assert len(a_out_of_group.dispatched) == 0
    finally:
        await orch.stop()


async def test_config_groups_loaded_into_group_manager():
    """OrchestratorConfig.groups is loaded into GroupManager in __init__."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(groups=[
        {"name": "fast-workers", "agent_ids": ["w1", "w2"]},
        {"name": "docs-workers", "agent_ids": ["d1"]},
    ])
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    gm = orch.get_group_manager()
    assert gm.get("fast-workers") == {"w1", "w2"}
    assert gm.get("docs-workers") == {"d1"}


async def test_task_with_unknown_group_dead_lettered():
    """Task targeting a nonexistent group goes to DLQ immediately."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    a1 = DummyAgent("a1", bus)
    orch.register_agent(a1)

    await orch.start()
    try:
        await orch.submit_task("test", target_group="nonexistent-group")
        # Give dispatch loop time to process
        await asyncio.sleep(0.5)
        # Task should land in DLQ
        dlq = orch.list_dlq()
        assert any("nonexistent-group" in e.get("reason", "") for e in dlq)
        # a1 must not have received the task
        assert len(a1.dispatched) == 0
    finally:
        await orch.stop()


async def test_get_group_manager_accessor():
    """get_group_manager() returns the GroupManager instance."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    gm = orch.get_group_manager()
    assert isinstance(gm, GroupManager)


# ---------------------------------------------------------------------------
# REST endpoint tests
# ---------------------------------------------------------------------------


class _MockOrch:
    """Minimal orchestrator mock for REST endpoint tests."""
    _dispatch_task = None

    def __init__(self):
        self._gm = GroupManager()
        self._gm.create("existing-group", ["a1", "a2"])

    def get_group_manager(self):
        return self._gm

    def list_agents(self):
        return [
            {"id": "a1", "status": "IDLE"},
            {"id": "a2", "status": "BUSY"},
        ]

    def get_agent(self, agent_id):
        return None

    def list_tasks(self):
        return []

    def get_director(self):
        return None

    def flush_director_pending(self):
        return []

    def list_dlq(self):
        return []

    @property
    def is_paused(self):
        return False

    def get_rate_limiter_status(self):
        return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}

    def reconfigure_rate_limiter(self, *, rate, burst):
        return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}

    def get_agent_context_stats(self, agent_id):
        return None

    def all_agent_context_stats(self):
        return []

    def get_agent_history(self, agent_id, limit=50):
        return None

    def get_workflow_manager(self):
        from tmux_orchestrator.workflow_manager import WorkflowManager
        return WorkflowManager()

    @property
    def _webhook_manager(self):
        from tmux_orchestrator.webhook_manager import WebhookManager
        return WebhookManager()


class _MockHub:
    async def start(self):
        pass

    async def stop(self):
        pass

    async def handle(self, ws):
        pass


@pytest.fixture
def rest_app():
    return create_app(_MockOrch(), _MockHub(), api_key="test-key")


@pytest.mark.anyio
async def test_rest_post_groups_creates_group(rest_app):
    async with AsyncClient(
        transport=ASGITransport(app=rest_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/groups",
            json={"name": "new-group", "agent_ids": ["a1"]},
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200
    assert resp.json()["name"] == "new-group"


@pytest.mark.anyio
async def test_rest_post_groups_duplicate_returns_409(rest_app):
    async with AsyncClient(
        transport=ASGITransport(app=rest_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/groups",
            json={"name": "existing-group", "agent_ids": []},
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_rest_get_groups_lists_all(rest_app):
    async with AsyncClient(
        transport=ASGITransport(app=rest_app), base_url="http://test"
    ) as client:
        resp = await client.get("/groups", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    names = [e["name"] for e in data]
    assert "existing-group" in names


@pytest.mark.anyio
async def test_rest_get_groups_includes_agent_statuses(rest_app):
    async with AsyncClient(
        transport=ASGITransport(app=rest_app), base_url="http://test"
    ) as client:
        resp = await client.get("/groups", headers={"X-API-Key": "test-key"})
    group = next(e for e in resp.json() if e["name"] == "existing-group")
    agent_ids_in_detail = [a["id"] for a in group["agents"]]
    assert "a1" in agent_ids_in_detail
    assert "a2" in agent_ids_in_detail


@pytest.mark.anyio
async def test_rest_get_group_by_name_detail(rest_app):
    async with AsyncClient(
        transport=ASGITransport(app=rest_app), base_url="http://test"
    ) as client:
        resp = await client.get("/groups/existing-group", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "existing-group"
    assert "a1" in data["agent_ids"]
    assert "a2" in data["agent_ids"]


@pytest.mark.anyio
async def test_rest_get_group_not_found_returns_404(rest_app):
    async with AsyncClient(
        transport=ASGITransport(app=rest_app), base_url="http://test"
    ) as client:
        resp = await client.get("/groups/no-such-group", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_rest_delete_group_success(rest_app):
    async with AsyncClient(
        transport=ASGITransport(app=rest_app), base_url="http://test"
    ) as client:
        resp = await client.delete("/groups/existing-group", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True


@pytest.mark.anyio
async def test_rest_delete_group_not_found_returns_404(rest_app):
    async with AsyncClient(
        transport=ASGITransport(app=rest_app), base_url="http://test"
    ) as client:
        resp = await client.delete("/groups/no-such-group", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_rest_post_group_agents_adds_agent(rest_app):
    async with AsyncClient(
        transport=ASGITransport(app=rest_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/groups/existing-group/agents",
            json={"agent_id": "a3"},
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200
    assert resp.json()["agent_id"] == "a3"
    assert resp.json()["added"] is True


@pytest.mark.anyio
async def test_rest_post_group_agents_group_not_found_returns_404(rest_app):
    async with AsyncClient(
        transport=ASGITransport(app=rest_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/groups/no-such-group/agents",
            json={"agent_id": "a3"},
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_rest_delete_group_agent_success(rest_app):
    async with AsyncClient(
        transport=ASGITransport(app=rest_app), base_url="http://test"
    ) as client:
        resp = await client.delete(
            "/groups/existing-group/agents/a1",
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200
    assert resp.json()["removed"] is True


@pytest.mark.anyio
async def test_rest_delete_group_agent_not_member_returns_404(rest_app):
    async with AsyncClient(
        transport=ASGITransport(app=rest_app), base_url="http://test"
    ) as client:
        resp = await client.delete(
            "/groups/existing-group/agents/no-such-agent",
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_rest_delete_group_agent_group_not_found_returns_404(rest_app):
    async with AsyncClient(
        transport=ASGITransport(app=rest_app), base_url="http://test"
    ) as client:
        resp = await client.delete(
            "/groups/no-such-group/agents/a1",
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 404
