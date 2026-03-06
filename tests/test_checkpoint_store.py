"""Tests for CheckpointStore — SQLite-backed checkpoint persistence.

Design references:
- LangGraph checkpointer pattern (LangChain docs 2025)
- Apache Flink Checkpoints vs Savepoints (Flink stable docs)
- DESIGN.md §10.12 (v0.45.0)
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from tmux_orchestrator.agents.base import Task
from tmux_orchestrator.checkpoint_store import CheckpointStore
from tmux_orchestrator.workflow_manager import WorkflowRun


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path):
    """Return a temporary SQLite database path."""
    return tmp_path / "test_checkpoint.db"


@pytest.fixture
def store(tmp_db):
    """Return an initialized CheckpointStore."""
    s = CheckpointStore(db_path=tmp_db)
    s.initialize()
    return s


@pytest.fixture
def sample_task():
    return Task(
        id="task-001",
        prompt="Write a hello world program",
        priority=1,
        depends_on=["task-000"],
        reply_to="director",
        target_agent="worker-1",
        required_tags=["python"],
        target_group="workers",
        max_retries=2,
        retry_count=0,
        inherit_priority=True,
        ttl=300.0,
        metadata={"key": "value"},
    )


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------


def test_initialize_creates_tables(tmp_db):
    """CheckpointStore.initialize() creates the required SQLite tables."""
    store = CheckpointStore(db_path=tmp_db)
    store.initialize()
    assert tmp_db.exists()


def test_initialize_idempotent(store, tmp_db):
    """Calling initialize() twice does not raise and leaves tables intact."""
    store2 = CheckpointStore(db_path=tmp_db)
    store2.initialize()  # should not raise


# ---------------------------------------------------------------------------
# Task checkpoint tests
# ---------------------------------------------------------------------------


def test_save_task_checkpoint(store, sample_task):
    """save_task() persists a task to SQLite."""
    store.save_task(task=sample_task, queue_priority=1)
    tasks = store.load_pending_tasks()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.id == "task-001"
    assert t.prompt == "Write a hello world program"
    assert t.priority == 1


def test_save_task_preserves_all_fields(store, sample_task):
    """save_task() round-trips all Task fields faithfully."""
    store.save_task(task=sample_task, queue_priority=1)
    tasks = store.load_pending_tasks()
    t = tasks[0]
    assert t.depends_on == ["task-000"]
    assert t.reply_to == "director"
    assert t.target_agent == "worker-1"
    assert t.required_tags == ["python"]
    assert t.target_group == "workers"
    assert t.max_retries == 2
    assert t.retry_count == 0
    assert t.inherit_priority is True
    assert t.ttl == 300.0
    assert t.metadata == {"key": "value"}


def test_remove_task_checkpoint(store, sample_task):
    """remove_task() deletes a task from the checkpoint store."""
    store.save_task(task=sample_task, queue_priority=1)
    store.remove_task(task_id=sample_task.id)
    tasks = store.load_pending_tasks()
    assert tasks == []


def test_remove_nonexistent_task_is_noop(store):
    """remove_task() on a non-existent task_id does not raise."""
    store.remove_task(task_id="nonexistent")  # should not raise


def test_save_multiple_tasks(store):
    """Multiple tasks can be saved and loaded in priority order."""
    t1 = Task(id="t1", prompt="low priority", priority=10)
    t2 = Task(id="t2", prompt="high priority", priority=0)
    t3 = Task(id="t3", prompt="medium priority", priority=5)

    store.save_task(task=t1, queue_priority=10)
    store.save_task(task=t2, queue_priority=0)
    store.save_task(task=t3, queue_priority=5)

    tasks = store.load_pending_tasks()
    assert len(tasks) == 3
    # Should be ordered by queue_priority ascending
    priorities = [t.priority for t in tasks]
    assert priorities == sorted(priorities)


def test_save_task_upsert(store, sample_task):
    """Saving the same task_id twice updates the record (upsert)."""
    store.save_task(task=sample_task, queue_priority=1)
    updated = Task(
        id=sample_task.id,
        prompt="Updated prompt",
        priority=5,
    )
    store.save_task(task=updated, queue_priority=5)
    tasks = store.load_pending_tasks()
    assert len(tasks) == 1
    assert tasks[0].prompt == "Updated prompt"
    assert tasks[0].priority == 5


def test_load_pending_tasks_empty_store(store):
    """load_pending_tasks() returns empty list when no tasks are stored."""
    assert store.load_pending_tasks() == []


# ---------------------------------------------------------------------------
# Workflow checkpoint tests
# ---------------------------------------------------------------------------


def test_save_workflow_checkpoint(store):
    """save_workflow() persists a WorkflowRun to SQLite."""
    run = WorkflowRun(
        id="wf-001",
        name="My Pipeline",
        task_ids=["t1", "t2", "t3"],
        status="running",
    )
    store.save_workflow(run=run)
    workflows = store.load_workflows()
    assert "wf-001" in workflows
    wf = workflows["wf-001"]
    assert wf.name == "My Pipeline"
    assert wf.task_ids == ["t1", "t2", "t3"]
    assert wf.status == "running"


def test_save_workflow_preserves_timestamps(store):
    """save_workflow() preserves created_at and completed_at."""
    t0 = time.time()
    run = WorkflowRun(
        id="wf-002",
        name="Timed Workflow",
        task_ids=["t1"],
        status="complete",
        created_at=t0 - 100,
        completed_at=t0,
    )
    store.save_workflow(run=run)
    workflows = store.load_workflows()
    wf = workflows["wf-002"]
    assert abs(wf.created_at - (t0 - 100)) < 0.001
    assert wf.completed_at is not None
    assert abs(wf.completed_at - t0) < 0.001


def test_remove_workflow_checkpoint(store):
    """remove_workflow() deletes a workflow from the checkpoint store."""
    run = WorkflowRun(id="wf-003", name="To Delete", task_ids=["t1"])
    store.save_workflow(run=run)
    store.remove_workflow(workflow_id="wf-003")
    workflows = store.load_workflows()
    assert "wf-003" not in workflows


def test_remove_nonexistent_workflow_is_noop(store):
    """remove_workflow() on a non-existent ID does not raise."""
    store.remove_workflow(workflow_id="nonexistent")  # should not raise


def test_save_workflow_upsert(store):
    """Saving the same workflow_id twice updates the record."""
    run = WorkflowRun(id="wf-004", name="Original", task_ids=["t1"], status="pending")
    store.save_workflow(run=run)
    updated = WorkflowRun(id="wf-004", name="Updated", task_ids=["t1", "t2"], status="running")
    store.save_workflow(run=updated)
    workflows = store.load_workflows()
    assert len(workflows) == 1
    wf = workflows["wf-004"]
    assert wf.name == "Updated"
    assert wf.status == "running"
    assert wf.task_ids == ["t1", "t2"]


def test_load_workflows_empty_store(store):
    """load_workflows() returns empty dict when no workflows are stored."""
    assert store.load_workflows() == {}


# ---------------------------------------------------------------------------
# Metadata tests
# ---------------------------------------------------------------------------


def test_save_and_load_meta(store):
    """save_meta() and load_meta() round-trip arbitrary key-value metadata."""
    store.save_meta("session_name", "my-session")
    store.save_meta("version", "0.45.0")
    assert store.load_meta("session_name") == "my-session"
    assert store.load_meta("version") == "0.45.0"


def test_load_meta_missing_key_returns_none(store):
    """load_meta() returns None for unknown keys."""
    assert store.load_meta("nonexistent") is None


def test_load_meta_missing_key_returns_default(store):
    """load_meta() returns default value for unknown keys."""
    assert store.load_meta("nonexistent", default="fallback") == "fallback"


# ---------------------------------------------------------------------------
# Clear / reset tests
# ---------------------------------------------------------------------------


def test_clear_tasks(store, sample_task):
    """clear_tasks() removes all task checkpoints."""
    store.save_task(task=sample_task, queue_priority=1)
    store.clear_tasks()
    assert store.load_pending_tasks() == []


def test_clear_workflows(store):
    """clear_workflows() removes all workflow checkpoints."""
    run = WorkflowRun(id="wf-clear", name="Clear Me", task_ids=["t1"])
    store.save_workflow(run=run)
    store.clear_workflows()
    assert store.load_workflows() == {}


def test_clear_all(store, sample_task):
    """clear_all() wipes tasks, workflows, and meta."""
    store.save_task(task=sample_task, queue_priority=1)
    store.save_workflow(run=WorkflowRun(id="wf-x", name="X", task_ids=["t1"]))
    store.save_meta("k", "v")
    store.clear_all()
    assert store.load_pending_tasks() == []
    assert store.load_workflows() == {}
    assert store.load_meta("k") is None


# ---------------------------------------------------------------------------
# Persistence across instances (process restart simulation)
# ---------------------------------------------------------------------------


def test_persistence_across_instances(tmp_db, sample_task):
    """Data saved by one CheckpointStore instance is readable by another."""
    s1 = CheckpointStore(db_path=tmp_db)
    s1.initialize()
    s1.save_task(task=sample_task, queue_priority=1)
    s1.save_workflow(run=WorkflowRun(id="wf-persist", name="Persist", task_ids=["task-001"]))
    s1.save_meta("key", "value")

    # Simulate process restart — new instance
    s2 = CheckpointStore(db_path=tmp_db)
    s2.initialize()
    tasks = s2.load_pending_tasks()
    assert len(tasks) == 1
    assert tasks[0].id == "task-001"
    workflows = s2.load_workflows()
    assert "wf-persist" in workflows
    assert s2.load_meta("key") == "value"


# ---------------------------------------------------------------------------
# Waiting-task checkpoint tests
# ---------------------------------------------------------------------------


def test_save_and_load_waiting_task(store, sample_task):
    """save_waiting_task() persists a task that is blocked on dependencies."""
    store.save_waiting_task(task=sample_task)
    waiting = store.load_waiting_tasks()
    assert len(waiting) == 1
    assert waiting[0].id == "task-001"


def test_remove_waiting_task(store, sample_task):
    """remove_waiting_task() deletes a waiting task checkpoint."""
    store.save_waiting_task(task=sample_task)
    store.remove_waiting_task(task_id=sample_task.id)
    assert store.load_waiting_tasks() == []


def test_load_waiting_tasks_empty(store):
    """load_waiting_tasks() returns empty list when nothing is stored."""
    assert store.load_waiting_tasks() == []
