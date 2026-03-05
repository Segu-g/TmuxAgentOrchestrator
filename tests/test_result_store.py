"""Tests for ResultStore — append-only JSONL result persistence.

Design references:
- Martin Fowler "Event Sourcing" (2005)
- Greg Young "CQRS Documents" (2010)
- Rich Hickey "The Value of Values" (Datomic, 2012)
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from tmux_orchestrator.result_store import ResultStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    return tmp_path / "results"


@pytest.fixture
def store(store_dir: Path) -> ResultStore:
    return ResultStore(store_dir=store_dir, session_name="test-session")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _append_one(
    store: ResultStore,
    *,
    task_id: str = "task-001",
    agent_id: str = "agent-1",
    prompt: str = "Do something",
    result_text: str = "Done.",
    error: str | None = None,
    duration_s: float = 1.5,
) -> None:
    store.append(
        task_id=task_id,
        agent_id=agent_id,
        prompt=prompt,
        result_text=result_text,
        error=error,
        duration_s=duration_s,
    )


# ---------------------------------------------------------------------------
# 1. append() writes a valid JSON line to the correct file
# ---------------------------------------------------------------------------


def test_append_creates_jsonl_file(store: ResultStore, store_dir: Path) -> None:
    """append() must create the session sub-directory and write a JSONL file."""
    _append_one(store)
    session_dir = store_dir / "test-session"
    assert session_dir.exists(), "session directory must be created"
    jsonl_files = list(session_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1, "exactly one JSONL file should exist"


def test_append_writes_valid_json_line(store: ResultStore, store_dir: Path) -> None:
    """Each appended record must be a complete, valid JSON object on a single line."""
    _append_one(store, task_id="t1", agent_id="a1", prompt="Hello", result_text="Hi", duration_s=2.0)
    session_dir = store_dir / "test-session"
    lines = [
        l
        for f in session_dir.glob("*.jsonl")
        for l in f.read_text().splitlines()
        if l.strip()
    ]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["task_id"] == "t1"
    assert record["agent_id"] == "a1"
    assert record["prompt"] == "Hello"
    assert record["result_text"] == "Hi"
    assert record["error"] is None
    assert record["duration_s"] == 2.0
    assert "ts" in record


def test_append_with_error_field(store: ResultStore, store_dir: Path) -> None:
    """Records with error must persist the error string."""
    _append_one(store, task_id="t-err", agent_id="a1", error="timeout", result_text="")
    session_dir = store_dir / "test-session"
    lines = [l for f in session_dir.glob("*.jsonl") for l in f.read_text().splitlines() if l.strip()]
    record = json.loads(lines[0])
    assert record["error"] == "timeout"


def test_append_multiple_records_same_day(store: ResultStore, store_dir: Path) -> None:
    """Multiple append() calls on the same day append multiple lines."""
    for i in range(5):
        _append_one(store, task_id=f"task-{i}", agent_id="a1", result_text=f"result-{i}")
    session_dir = store_dir / "test-session"
    lines = [l for f in session_dir.glob("*.jsonl") for l in f.read_text().splitlines() if l.strip()]
    assert len(lines) == 5


# ---------------------------------------------------------------------------
# 2. query() by agent_id returns matching results
# ---------------------------------------------------------------------------


def test_query_by_agent_id(store: ResultStore) -> None:
    """query(agent_id=...) must return only records from that agent."""
    _append_one(store, task_id="t1", agent_id="agent-A", result_text="A result")
    _append_one(store, task_id="t2", agent_id="agent-B", result_text="B result")
    _append_one(store, task_id="t3", agent_id="agent-A", result_text="A result 2")

    results = store.query(agent_id="agent-A")
    assert len(results) == 2
    assert all(r["agent_id"] == "agent-A" for r in results)
    task_ids = {r["task_id"] for r in results}
    assert task_ids == {"t1", "t3"}


def test_query_by_agent_id_no_match(store: ResultStore) -> None:
    """query(agent_id=...) with no match returns empty list."""
    _append_one(store, agent_id="agent-A")
    results = store.query(agent_id="agent-Z")
    assert results == []


# ---------------------------------------------------------------------------
# 3. query() by task_id returns correct result
# ---------------------------------------------------------------------------


def test_query_by_task_id(store: ResultStore) -> None:
    """query(task_id=...) returns only the record with that task_id."""
    _append_one(store, task_id="target-task", agent_id="a1", result_text="Found it")
    _append_one(store, task_id="other-task", agent_id="a2", result_text="Not this one")

    results = store.query(task_id="target-task")
    assert len(results) == 1
    assert results[0]["task_id"] == "target-task"
    assert results[0]["result_text"] == "Found it"


def test_query_by_task_id_not_found(store: ResultStore) -> None:
    """query(task_id=...) for unknown task returns empty list."""
    _append_one(store)
    results = store.query(task_id="nonexistent")
    assert results == []


# ---------------------------------------------------------------------------
# 4. query() by date filters correctly
# ---------------------------------------------------------------------------


def test_query_by_date_existing(store: ResultStore, store_dir: Path) -> None:
    """query(date=...) with a known date returns records from that file."""
    _append_one(store, task_id="day-task", agent_id="a1")
    # Find the date that was written.
    dates = store.all_dates()
    assert len(dates) == 1

    results = store.query(date=dates[0])
    assert len(results) == 1
    assert results[0]["task_id"] == "day-task"


def test_query_by_date_not_existing(store: ResultStore) -> None:
    """query(date=...) for a date with no file returns empty list."""
    results = store.query(date="1970-01-01")
    assert results == []


# ---------------------------------------------------------------------------
# 5. query() respects limit
# ---------------------------------------------------------------------------


def test_query_limit(store: ResultStore) -> None:
    """query(limit=N) returns at most N records."""
    for i in range(10):
        _append_one(store, task_id=f"t{i}", agent_id="a1")
    results = store.query(limit=3)
    assert len(results) == 3


def test_query_limit_larger_than_total(store: ResultStore) -> None:
    """query(limit=N) when N > total records returns all records."""
    for i in range(4):
        _append_one(store, task_id=f"t{i}", agent_id="a1")
    results = store.query(limit=100)
    assert len(results) == 4


# ---------------------------------------------------------------------------
# 6. all_dates() returns sorted list of date strings
# ---------------------------------------------------------------------------


def test_all_dates_empty_when_no_data(store: ResultStore) -> None:
    """all_dates() on a fresh store returns an empty list."""
    assert store.all_dates() == []


def test_all_dates_returns_today(store: ResultStore) -> None:
    """all_dates() returns the current date after an append."""
    _append_one(store)
    dates = store.all_dates()
    assert len(dates) == 1
    # Should be in YYYY-MM-DD format.
    assert len(dates[0]) == 10
    assert dates[0][4] == "-"
    assert dates[0][7] == "-"


def test_all_dates_sorted(store_dir: Path) -> None:
    """all_dates() always returns dates in ascending sorted order."""
    store = ResultStore(store_dir=store_dir, session_name="s")
    session_dir = store_dir / "s"
    session_dir.mkdir(parents=True)
    # Manually create fake JSONL files for specific dates.
    for date_str in ["2026-03-05", "2026-03-03", "2026-03-04"]:
        (session_dir / f"{date_str}.jsonl").write_text(
            json.dumps({"task_id": "x", "agent_id": "a", "prompt": "", "result_text": "",
                        "error": None, "duration_s": 1.0, "ts": f"{date_str}T00:00:00+00:00"}) + "\n"
        )
    dates = store.all_dates()
    assert dates == ["2026-03-03", "2026-03-04", "2026-03-05"]


# ---------------------------------------------------------------------------
# 7. Thread-safety: concurrent appends produce valid JSONL (no corrupted lines)
# ---------------------------------------------------------------------------


def test_concurrent_appends_produce_valid_jsonl(store: ResultStore, store_dir: Path) -> None:
    """Concurrent append() calls from multiple threads must all produce valid JSON lines."""
    N = 50
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            store.append(
                task_id=f"task-{i}",
                agent_id=f"agent-{i % 4}",
                prompt=f"prompt {i}",
                result_text=f"result {i}",
                error=None,
                duration_s=float(i) * 0.01,
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"thread errors: {errors}"

    # All lines must be valid JSON.
    session_dir = store_dir / "test-session"
    all_lines = [
        l
        for f in session_dir.glob("*.jsonl")
        for l in f.read_text().splitlines()
        if l.strip()
    ]
    assert len(all_lines) == N, f"expected {N} lines, got {len(all_lines)}"
    for line in all_lines:
        record = json.loads(line)  # must not raise
        assert "task_id" in record
        assert "agent_id" in record


# ---------------------------------------------------------------------------
# 8 & 9. REST endpoints
# ---------------------------------------------------------------------------


class _MockResultStore:
    """Minimal stub used by the MockOrchestrator for web endpoint tests."""

    def __init__(self) -> None:
        self._records = [
            {
                "task_id": "t1",
                "agent_id": "a1",
                "prompt": "do A",
                "result_text": "done A",
                "error": None,
                "duration_s": 1.5,
                "ts": "2026-03-05T10:00:00+00:00",
            },
            {
                "task_id": "t2",
                "agent_id": "a2",
                "prompt": "do B",
                "result_text": "done B",
                "error": None,
                "duration_s": 2.0,
                "ts": "2026-03-05T10:01:00+00:00",
            },
        ]
        self._dates = ["2026-03-05"]

    def query(
        self,
        *,
        agent_id: str | None = None,
        task_id: str | None = None,
        date: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        results = [r for r in self._records]
        if agent_id:
            results = [r for r in results if r["agent_id"] == agent_id]
        if task_id:
            results = [r for r in results if r["task_id"] == task_id]
        if date:
            results = [r for r in results if r["ts"].startswith(date)]
        return results[:limit]

    def all_dates(self) -> list[str]:
        return self._dates


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def handle(self, ws) -> None:
        pass


class _MockOrchestratorWithStore:
    _dispatch_task = None

    def __init__(self) -> None:
        self._result_store = _MockResultStore()

    def list_agents(self) -> list:
        return []

    def list_tasks(self) -> list:
        return []

    def get_agent(self, agent_id: str):
        return None

    def get_director(self):
        return None

    def flush_director_pending(self) -> list:
        return []

    def list_dlq(self) -> list:
        return []

    @property
    def is_paused(self) -> bool:
        return False

    def get_rate_limiter_status(self) -> dict:
        return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}

    def reconfigure_rate_limiter(self, *, rate: float, burst: int) -> dict:
        return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}

    def get_autoscaler_status(self) -> dict | None:
        return None

    def reconfigure_autoscaler(self, **kwargs) -> dict:
        return {}

    async def submit_task(self, task) -> str:
        return "task-id"

    async def pause(self) -> None:
        pass

    async def resume(self) -> None:
        pass

    def list_capabilities(self) -> list:
        return []

    def get_agent_history(self, agent_id: str, *, limit: int = 50) -> list | None:
        return None

    def get_agent_context_stats(self, agent_id: str) -> dict | None:
        return None

    def all_agent_context_stats(self) -> list:
        return []

    async def cancel_task(self, task_id: str) -> bool | None:
        return None


@pytest.fixture
async def web_client_with_store():
    """AsyncClient for a web app wired with _MockOrchestratorWithStore."""
    from tmux_orchestrator.web.app import create_app
    import tmux_orchestrator.web.app as web_app_mod
    # Reset module-level auth state.
    web_app_mod._credentials.clear()
    web_app_mod._sessions.clear()
    web_app_mod._pending_challenge = None

    orch = _MockOrchestratorWithStore()
    hub = _MockHub()
    app = create_app(orch, hub, api_key="test-key")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"X-API-Key": "test-key"},
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_rest_get_results_returns_records(web_client_with_store):
    """GET /results should return all persisted results from the store."""
    resp = await web_client_with_store.get("/results")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["task_id"] == "t1"
    assert data[1]["task_id"] == "t2"


@pytest.mark.asyncio
async def test_rest_get_results_filter_agent_id(web_client_with_store):
    """GET /results?agent_id=a1 should return only a1 records."""
    resp = await web_client_with_store.get("/results", params={"agent_id": "a1"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["agent_id"] == "a1"


@pytest.mark.asyncio
async def test_rest_get_results_filter_task_id(web_client_with_store):
    """GET /results?task_id=t2 should return only t2 record."""
    resp = await web_client_with_store.get("/results", params={"task_id": "t2"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["task_id"] == "t2"


@pytest.mark.asyncio
async def test_rest_get_results_dates(web_client_with_store):
    """GET /results/dates should return the list of dates from the store."""
    resp = await web_client_with_store.get("/results/dates")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert "2026-03-05" in data


@pytest.mark.asyncio
async def test_rest_get_results_no_store():
    """GET /results when result_store_enabled=False (no store) should return empty list."""
    from tmux_orchestrator.web.app import create_app
    import tmux_orchestrator.web.app as web_app_mod
    web_app_mod._credentials.clear()
    web_app_mod._sessions.clear()

    class _NoStore:
        _dispatch_task = None
        _result_store = None

        def list_agents(self): return []
        def list_tasks(self): return []
        def get_agent(self, _): return None
        def get_director(self): return None
        def flush_director_pending(self): return []
        def list_dlq(self): return []
        @property
        def is_paused(self): return False
        def get_rate_limiter_status(self): return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}
        def reconfigure_rate_limiter(self, *, rate, burst): return {}
        def get_autoscaler_status(self): return None
        def reconfigure_autoscaler(self, **kwargs): return {}
        async def submit_task(self, task): return "x"
        async def pause(self): pass
        async def resume(self): pass
        def list_capabilities(self): return []
        def get_agent_history(self, agent_id, *, limit=50): return None
        def get_agent_context_stats(self, agent_id): return None
        def all_agent_context_stats(self): return []
        async def cancel_task(self, task_id): return None

    orch = _NoStore()
    hub = _MockHub()
    app = create_app(orch, hub, api_key="test-key")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"X-API-Key": "test-key"},
    ) as client:
        resp = await client.get("/results")
        assert resp.status_code == 200
        assert resp.json() == []

        resp2 = await client.get("/results/dates")
        assert resp2.status_code == 200
        assert resp2.json() == []


# ---------------------------------------------------------------------------
# 10. ResultStore disabled when result_store_enabled=False
# ---------------------------------------------------------------------------


def test_result_store_not_created_when_disabled(tmp_path: Path) -> None:
    """When result_store_enabled=False, no ResultStore should be created in Orchestrator."""
    from unittest.mock import MagicMock
    from tmux_orchestrator.config import OrchestratorConfig
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.bus import Bus

    config = OrchestratorConfig(
        session_name="test",
        result_store_enabled=False,
        result_store_dir=str(tmp_path / "results"),
    )
    bus = Bus()
    tmux_mock = MagicMock()
    orch = Orchestrator(bus=bus, tmux=tmux_mock, config=config)
    assert orch._result_store is None


def test_result_store_created_when_enabled(tmp_path: Path) -> None:
    """When result_store_enabled=True, Orchestrator._result_store should be a ResultStore."""
    from unittest.mock import MagicMock
    from tmux_orchestrator.config import OrchestratorConfig
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.result_store import ResultStore

    config = OrchestratorConfig(
        session_name="test",
        result_store_enabled=True,
        result_store_dir=str(tmp_path / "results"),
    )
    bus = Bus()
    tmux_mock = MagicMock()
    orch = Orchestrator(bus=bus, tmux=tmux_mock, config=config)
    assert isinstance(orch._result_store, ResultStore)
