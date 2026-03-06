"""SQLite-backed checkpoint store for fault-tolerant process restart.

Design references:
- LangGraph checkpointer + AsyncSqliteSaver pattern
  (LangChain docs 2025; https://pypi.org/project/langgraph-checkpoint-sqlite/)
- Apache Flink Checkpoints vs Savepoints
  (https://nightlies.apache.org/flink/flink-docs-stable/docs/ops/state/checkpoints_vs_savepoints/)
- Chandy-Lamport distributed snapshots algorithm (1985) — consistent state
  capture across concurrent components without a global pause.
- "Mastering LangGraph Checkpointing: Best Practices for 2025"
  (https://sparkco.ai/blog/mastering-langgraph-checkpointing-best-practices-for-2025)

Schema (3 tables):
  task_checkpoints    — in-queue task snapshots (pending dispatch)
  waiting_checkpoints — tasks blocked on depends_on prerequisites
  workflow_checkpoints — workflow DAG run state snapshots
  orchestrator_meta   — key-value process metadata (session, version, ...)

Thread-safety: all writes use SQLite's WAL (Write-Ahead Logging) mode so
concurrent readers do not block writers.  A single ``threading.Lock``
serialises Python-level writes to prevent race conditions on the connection.

DESIGN.md §10.12 (v0.45.0).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from tmux_orchestrator.agents.base import Task
from tmux_orchestrator.workflow_manager import WorkflowRun


_CREATE_TASK_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS task_checkpoints (
    task_id TEXT PRIMARY KEY,
    queue_priority INTEGER NOT NULL DEFAULT 0,
    task_json TEXT NOT NULL,
    saved_at REAL NOT NULL
)
"""

_CREATE_WAITING_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS waiting_checkpoints (
    task_id TEXT PRIMARY KEY,
    task_json TEXT NOT NULL,
    saved_at REAL NOT NULL
)
"""

_CREATE_WORKFLOW_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS workflow_checkpoints (
    workflow_id TEXT PRIMARY KEY,
    workflow_json TEXT NOT NULL,
    saved_at REAL NOT NULL
)
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS orchestrator_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


def _task_to_json(task: Task) -> str:
    """Serialise a Task to a JSON string."""
    d = {
        "id": task.id,
        "prompt": task.prompt,
        "priority": task.priority,
        "trace_id": task.trace_id,
        "depends_on": task.depends_on,
        "reply_to": task.reply_to,
        "target_agent": task.target_agent,
        "required_tags": task.required_tags,
        "target_group": task.target_group,
        "max_retries": task.max_retries,
        "retry_count": task.retry_count,
        "inherit_priority": task.inherit_priority,
        "ttl": task.ttl,
        "submitted_at": task.submitted_at,
        "expires_at": task.expires_at,
        "metadata": task.metadata,
    }
    return json.dumps(d, ensure_ascii=False)


def _task_from_json(s: str) -> Task:
    """Deserialise a Task from a JSON string."""
    d = json.loads(s)
    return Task(
        id=d["id"],
        prompt=d["prompt"],
        priority=d.get("priority", 0),
        trace_id=d.get("trace_id", ""),
        depends_on=d.get("depends_on", []),
        reply_to=d.get("reply_to"),
        target_agent=d.get("target_agent"),
        required_tags=d.get("required_tags", []),
        target_group=d.get("target_group"),
        max_retries=d.get("max_retries", 0),
        retry_count=d.get("retry_count", 0),
        inherit_priority=d.get("inherit_priority", True),
        ttl=d.get("ttl"),
        submitted_at=d.get("submitted_at", time.time()),
        expires_at=d.get("expires_at"),
        metadata=d.get("metadata", {}),
    )


def _workflow_to_json(run: WorkflowRun) -> str:
    """Serialise a WorkflowRun to a JSON string."""
    d = {
        "id": run.id,
        "name": run.name,
        "task_ids": run.task_ids,
        "status": run.status,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
        "_completed": list(run._completed),
        "_failed": list(run._failed),
    }
    return json.dumps(d, ensure_ascii=False)


def _workflow_from_json(s: str) -> WorkflowRun:
    """Deserialise a WorkflowRun from a JSON string."""
    d = json.loads(s)
    run = WorkflowRun(
        id=d["id"],
        name=d["name"],
        task_ids=d.get("task_ids", []),
        status=d.get("status", "pending"),
        created_at=d.get("created_at", time.time()),
        completed_at=d.get("completed_at"),
    )
    run._completed = set(d.get("_completed", []))
    run._failed = set(d.get("_failed", []))
    return run


class CheckpointStore:
    """SQLite-backed checkpoint store for task queues and workflow state.

    Provides fault-tolerant persistence so that in-flight tasks and active
    workflows can be recovered after a process restart.  The store is
    designed to be called from synchronous (non-async) code paths in the
    orchestrator via lightweight SQLite I/O.

    Usage
    -----
    ::

        store = CheckpointStore(db_path="~/.tmux_orchestrator/checkpoint.db")
        store.initialize()

        # Save a task when it enters the queue
        store.save_task(task, queue_priority=0)

        # Remove when task completes or fails
        store.remove_task(task_id=task.id)

        # On resume, reload the queue
        pending = store.load_pending_tasks()

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  Expanded with ``Path.expanduser()``.
        Use ``":memory:"`` for in-memory operation (tests / ephemeral mode).
    """

    def __init__(self, db_path: str | Path) -> None:
        if str(db_path) == ":memory:":
            self._db_path = ":memory:"
        else:
            self._db_path = str(Path(db_path).expanduser().resolve())
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Create the database file and tables (idempotent).

        Safe to call multiple times — uses ``CREATE TABLE IF NOT EXISTS``.
        """
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        conn = self._get_conn()
        with self._lock:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_CREATE_TASK_CHECKPOINTS)
            conn.execute(_CREATE_WAITING_CHECKPOINTS)
            conn.execute(_CREATE_WORKFLOW_CHECKPOINTS)
            conn.execute(_CREATE_META)
            conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        """Return (and lazily create) the SQLite connection."""
        if self._conn is None:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                timeout=10.0,
            )
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        """Close the SQLite connection (idempotent)."""
        if self._conn is not None:
            with self._lock:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------
    # Task checkpoints (queued tasks)
    # ------------------------------------------------------------------

    def save_task(self, *, task: Task, queue_priority: int) -> None:
        """Persist a task checkpoint (upsert).

        Parameters
        ----------
        task:
            The Task object to checkpoint.
        queue_priority:
            The effective priority used by the asyncio.PriorityQueue
            (may differ from task.priority due to inheritance).
        """
        task_json = _task_to_json(task)
        now = time.time()
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                """
                INSERT INTO task_checkpoints (task_id, queue_priority, task_json, saved_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    queue_priority = excluded.queue_priority,
                    task_json = excluded.task_json,
                    saved_at = excluded.saved_at
                """,
                (task.id, queue_priority, task_json, now),
            )
            conn.commit()

    def remove_task(self, *, task_id: str) -> None:
        """Remove a task checkpoint (no-op if not found)."""
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                "DELETE FROM task_checkpoints WHERE task_id = ?",
                (task_id,),
            )
            conn.commit()

    def load_pending_tasks(self) -> list[Task]:
        """Load all persisted task checkpoints, ordered by queue_priority ASC.

        Returns
        -------
        list[Task]
            Tasks in priority order (lowest queue_priority first), ready to
            be re-inserted into ``asyncio.PriorityQueue``.
        """
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT task_json FROM task_checkpoints ORDER BY queue_priority ASC"
            ).fetchall()
        return [_task_from_json(row["task_json"]) for row in rows]

    def clear_tasks(self) -> None:
        """Delete all task checkpoints."""
        conn = self._get_conn()
        with self._lock:
            conn.execute("DELETE FROM task_checkpoints")
            conn.commit()

    # ------------------------------------------------------------------
    # Waiting-task checkpoints (blocked on depends_on)
    # ------------------------------------------------------------------

    def save_waiting_task(self, *, task: Task) -> None:
        """Persist a waiting (blocked) task checkpoint (upsert)."""
        task_json = _task_to_json(task)
        now = time.time()
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                """
                INSERT INTO waiting_checkpoints (task_id, task_json, saved_at)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    task_json = excluded.task_json,
                    saved_at = excluded.saved_at
                """,
                (task.id, task_json, now),
            )
            conn.commit()

    def remove_waiting_task(self, *, task_id: str) -> None:
        """Remove a waiting task checkpoint (no-op if not found)."""
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                "DELETE FROM waiting_checkpoints WHERE task_id = ?",
                (task_id,),
            )
            conn.commit()

    def load_waiting_tasks(self) -> list[Task]:
        """Load all persisted waiting task checkpoints."""
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT task_json FROM waiting_checkpoints"
            ).fetchall()
        return [_task_from_json(row["task_json"]) for row in rows]

    def clear_waiting_tasks(self) -> None:
        """Delete all waiting task checkpoints."""
        conn = self._get_conn()
        with self._lock:
            conn.execute("DELETE FROM waiting_checkpoints")
            conn.commit()

    # ------------------------------------------------------------------
    # Workflow checkpoints
    # ------------------------------------------------------------------

    def save_workflow(self, *, run: WorkflowRun) -> None:
        """Persist a workflow run snapshot (upsert)."""
        workflow_json = _workflow_to_json(run)
        now = time.time()
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                """
                INSERT INTO workflow_checkpoints (workflow_id, workflow_json, saved_at)
                VALUES (?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    workflow_json = excluded.workflow_json,
                    saved_at = excluded.saved_at
                """,
                (run.id, workflow_json, now),
            )
            conn.commit()

    def remove_workflow(self, *, workflow_id: str) -> None:
        """Remove a workflow checkpoint (no-op if not found)."""
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                "DELETE FROM workflow_checkpoints WHERE workflow_id = ?",
                (workflow_id,),
            )
            conn.commit()

    def load_workflows(self) -> dict[str, WorkflowRun]:
        """Load all persisted workflow run checkpoints.

        Returns
        -------
        dict[str, WorkflowRun]
            Mapping of workflow_id to WorkflowRun.
        """
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT workflow_json FROM workflow_checkpoints"
            ).fetchall()
        return {
            run.id: run
            for run in (_workflow_from_json(row["workflow_json"]) for row in rows)
        }

    def clear_workflows(self) -> None:
        """Delete all workflow checkpoints."""
        conn = self._get_conn()
        with self._lock:
            conn.execute("DELETE FROM workflow_checkpoints")
            conn.commit()

    # ------------------------------------------------------------------
    # Metadata key-value store
    # ------------------------------------------------------------------

    def save_meta(self, key: str, value: str) -> None:
        """Persist a metadata key-value pair (upsert)."""
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                """
                INSERT INTO orchestrator_meta (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            conn.commit()

    def load_meta(self, key: str, *, default: str | None = None) -> str | None:
        """Load a metadata value by key.

        Returns *default* (``None``) if the key does not exist.
        """
        conn = self._get_conn()
        with self._lock:
            row = conn.execute(
                "SELECT value FROM orchestrator_meta WHERE key = ?",
                (key,),
            ).fetchone()
        return row["value"] if row else default

    # ------------------------------------------------------------------
    # Bulk reset
    # ------------------------------------------------------------------

    def clear_all(self) -> None:
        """Wipe all checkpoint data (tasks, waiting tasks, workflows, meta)."""
        conn = self._get_conn()
        with self._lock:
            conn.execute("DELETE FROM task_checkpoints")
            conn.execute("DELETE FROM waiting_checkpoints")
            conn.execute("DELETE FROM workflow_checkpoints")
            conn.execute("DELETE FROM orchestrator_meta")
            conn.commit()
