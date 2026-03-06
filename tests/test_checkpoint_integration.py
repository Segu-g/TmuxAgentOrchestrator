"""Integration tests for CheckpointStore ↔ Orchestrator interaction.

Tests that the orchestrator correctly checkpoints tasks on submit and
removes them on completion, and that start(resume=True) re-enqueues
persisted tasks.

DESIGN.md §10.12 (v0.45.0).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from tmux_orchestrator.agents.base import AgentStatus, Task
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.checkpoint_store import CheckpointStore
from tmux_orchestrator.config import OrchestratorConfig


# ---------------------------------------------------------------------------
# Minimal fixtures — no libtmux, no tmux panes
# ---------------------------------------------------------------------------


class _FakeTmux:
    """Minimal stub for TmuxInterface used by Orchestrator."""

    def __init__(self) -> None:
        self.session = None

    def stop_watcher(self) -> None:
        pass

    def kill_session(self) -> None:
        pass


class _FakeWorktreeManager:
    async def create_worktree(self, agent_id: str) -> Path:
        return Path("/tmp")

    async def remove_worktree(self, agent_id: str) -> None:
        pass


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "ckpt.db"


@pytest.fixture
def config(tmp_db):
    cfg = OrchestratorConfig(
        session_name="test-session",
        checkpoint_enabled=True,
        checkpoint_db=str(tmp_db),
    )
    return cfg


@pytest.fixture
def bus():
    return Bus()


@pytest.fixture
def orchestrator(bus, config):
    from tmux_orchestrator.orchestrator import Orchestrator
    return Orchestrator(
        bus=bus,
        tmux=_FakeTmux(),
        config=config,
    )


# ---------------------------------------------------------------------------
# CheckpointStore is created when checkpoint_enabled=True
# ---------------------------------------------------------------------------


def test_checkpoint_store_created_when_enabled(orchestrator, tmp_db):
    """Orchestrator creates CheckpointStore when checkpoint_enabled=True."""
    store = orchestrator.get_checkpoint_store()
    assert store is not None
    assert isinstance(store, CheckpointStore)
    assert tmp_db.exists()


def test_checkpoint_store_none_when_disabled(bus, tmp_db):
    """Orchestrator does not create CheckpointStore when disabled."""
    from tmux_orchestrator.orchestrator import Orchestrator
    cfg = OrchestratorConfig(
        session_name="test",
        checkpoint_enabled=False,
    )
    orc = Orchestrator(bus=bus, tmux=_FakeTmux(), config=cfg)
    assert orc.get_checkpoint_store() is None


# ---------------------------------------------------------------------------
# Task checkpointing on submit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_task_checkpoints(orchestrator, tmp_db):
    """submit_task() persists the task in the checkpoint store."""
    await orchestrator.bus.subscribe("__orchestrator__", broadcast=True)

    task = await orchestrator.submit_task("test prompt", priority=3)

    store = orchestrator.get_checkpoint_store()
    pending = store.load_pending_tasks()
    assert len(pending) == 1
    assert pending[0].id == task.id
    assert pending[0].prompt == "test prompt"
    assert pending[0].priority == 3

    await orchestrator.bus.unsubscribe("__orchestrator__")


@pytest.mark.asyncio
async def test_submit_waiting_task_checkpoints(orchestrator):
    """Tasks blocked on depends_on are saved as waiting checkpoints."""
    await orchestrator.bus.subscribe("__orchestrator__", broadcast=True)

    task = await orchestrator.submit_task(
        "dependent task",
        depends_on=["nonexistent-dep"],
    )

    store = orchestrator.get_checkpoint_store()
    waiting = store.load_waiting_tasks()
    assert len(waiting) == 1
    assert waiting[0].id == task.id
    # Should NOT be in pending queue
    assert store.load_pending_tasks() == []

    await orchestrator.bus.unsubscribe("__orchestrator__")


# ---------------------------------------------------------------------------
# Resume from checkpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_reloads_pending_tasks(bus, config, tmp_db):
    """start(resume=True) re-enqueues tasks from the checkpoint store."""
    from tmux_orchestrator.orchestrator import Orchestrator

    # Phase 1: populate checkpoint store directly
    store = CheckpointStore(db_path=tmp_db)
    store.initialize()
    t1 = Task(id="resume-task-1", prompt="Resume task 1", priority=0)
    t2 = Task(id="resume-task-2", prompt="Resume task 2", priority=5)
    store.save_task(task=t1, queue_priority=0)
    store.save_task(task=t2, queue_priority=5)
    store.save_meta("session_name", config.session_name)

    # Phase 2: new orchestrator instance — simulates process restart
    orc = Orchestrator(bus=bus, tmux=_FakeTmux(), config=config)
    # Manually call bus subscribe to avoid full start
    orc._bus_queue = await bus.subscribe("__orchestrator__", broadcast=True)
    await orc._resume_from_checkpoint()

    assert orc.queue_depth() == 2

    await bus.unsubscribe("__orchestrator__")


@pytest.mark.asyncio
async def test_resume_reloads_waiting_tasks(bus, config, tmp_db):
    """start(resume=True) restores waiting tasks to _waiting_tasks dict."""
    from tmux_orchestrator.orchestrator import Orchestrator

    store = CheckpointStore(db_path=tmp_db)
    store.initialize()
    waiting_task = Task(
        id="waiting-task-1",
        prompt="Blocked task",
        priority=0,
        depends_on=["not-done-yet"],
    )
    store.save_waiting_task(task=waiting_task)

    orc = Orchestrator(bus=bus, tmux=_FakeTmux(), config=config)
    orc._bus_queue = await bus.subscribe("__orchestrator__", broadcast=True)
    await orc._resume_from_checkpoint()

    assert "waiting-task-1" in orc._waiting_tasks
    assert orc.queue_depth() == 0  # not yet in queue

    await bus.unsubscribe("__orchestrator__")


# ---------------------------------------------------------------------------
# REST endpoint: GET /checkpoint/status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_status_endpoint_enabled():
    """GET /checkpoint/status returns pending/waiting/workflow counts."""
    import tempfile
    from httpx import AsyncClient, ASGITransport
    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.web.ws import WebSocketHub

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_ckpt.db"
        cfg = OrchestratorConfig(
            session_name="web-test",
            checkpoint_enabled=True,
            checkpoint_db=str(db_path),
        )
        bus = Bus()
        orc = Orchestrator(bus=bus, tmux=_FakeTmux(), config=cfg)
        hub = WebSocketHub(bus=bus)

        app = create_app(
            orchestrator=orc,
            hub=hub,
            api_key="test-key",
            on_startup=lambda: None,
            on_shutdown=lambda: None,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/checkpoint/status",
                headers={"X-API-Key": "test-key"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["enabled"] is True
            assert data["pending_tasks"] == 0
            assert data["waiting_tasks"] == 0


@pytest.mark.asyncio
async def test_checkpoint_status_endpoint_disabled():
    """GET /checkpoint/status returns {enabled: false} when disabled."""
    from httpx import AsyncClient, ASGITransport
    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.web.ws import WebSocketHub

    cfg = OrchestratorConfig(session_name="web-test", checkpoint_enabled=False)
    bus = Bus()
    orc = Orchestrator(bus=bus, tmux=_FakeTmux(), config=cfg)
    hub = WebSocketHub(bus=bus)

    app = create_app(
        orchestrator=orc,
        hub=hub,
        api_key="test-key",
        on_startup=lambda: None,
        on_shutdown=lambda: None,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/checkpoint/status",
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False


@pytest.mark.asyncio
async def test_checkpoint_clear_endpoint():
    """POST /checkpoint/clear wipes all checkpoint data."""
    import tempfile
    from httpx import AsyncClient, ASGITransport
    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.web.ws import WebSocketHub

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "clear_test.db"
        cfg = OrchestratorConfig(
            session_name="web-test",
            checkpoint_enabled=True,
            checkpoint_db=str(db_path),
        )
        bus = Bus()
        orc = Orchestrator(bus=bus, tmux=_FakeTmux(), config=cfg)
        # Pre-populate checkpoint store
        store = orc.get_checkpoint_store()
        store.save_task(task=Task(id="t1", prompt="hello"), queue_priority=0)

        hub = WebSocketHub(bus=bus)
        app = create_app(
            orchestrator=orc,
            hub=hub,
            api_key="test-key",
            on_startup=lambda: None,
            on_shutdown=lambda: None,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Verify data exists
            resp = await client.get(
                "/checkpoint/status",
                headers={"X-API-Key": "test-key"},
            )
            assert resp.json()["pending_tasks"] == 1

            # Clear
            resp = await client.post(
                "/checkpoint/clear",
                headers={"X-API-Key": "test-key"},
            )
            assert resp.status_code == 200
            assert resp.json()["cleared"] is True

            # Verify cleared
            resp = await client.get(
                "/checkpoint/status",
                headers={"X-API-Key": "test-key"},
            )
            assert resp.json()["pending_tasks"] == 0


@pytest.mark.asyncio
async def test_checkpoint_clear_disabled_returns_400():
    """POST /checkpoint/clear returns 400 when checkpointing is disabled."""
    from httpx import AsyncClient, ASGITransport
    from tmux_orchestrator.web.app import create_app
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.web.ws import WebSocketHub

    cfg = OrchestratorConfig(session_name="web-test", checkpoint_enabled=False)
    bus = Bus()
    orc = Orchestrator(bus=bus, tmux=_FakeTmux(), config=cfg)
    hub = WebSocketHub(bus=bus)
    app = create_app(
        orchestrator=orc,
        hub=hub,
        api_key="test-key",
        on_startup=lambda: None,
        on_shutdown=lambda: None,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/checkpoint/clear",
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 400
