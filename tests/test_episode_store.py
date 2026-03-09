"""Unit tests for EpisodeStore — MIRIX-inspired per-agent episodic memory.

Tests cover:
- append() writes a valid JSONL record and returns the episode dict
- list() returns newest-first, respects limit
- get() retrieves by ID; raises EpisodeNotFoundError for unknown IDs
- delete() removes exactly one episode, preserves others
- delete() raises EpisodeNotFoundError for unknown IDs
- Concurrent appends (thread safety)
- Empty-agent scenarios (no episodes file yet)
- has_agent() utility

Design reference: DESIGN.md §10.28 (v1.0.28); arXiv:2507.07957.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from tmux_orchestrator.episode_store import EpisodeNotFoundError, EpisodeStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_store(tmp_path: Path) -> EpisodeStore:
    return EpisodeStore(root_dir=tmp_path, session_name="test-session")


AGENT = "agent-1"


# ---------------------------------------------------------------------------
# append()
# ---------------------------------------------------------------------------


def test_append_returns_valid_record(tmp_path):
    store = make_store(tmp_path)
    ep = store.append(AGENT, summary="Did the thing", outcome="success")
    assert ep["id"]
    assert ep["agent_id"] == AGENT
    assert ep["summary"] == "Did the thing"
    assert ep["outcome"] == "success"
    assert ep["lessons"] == ""
    assert ep["task_id"] is None
    assert ep["created_at"]


def test_append_with_all_fields(tmp_path):
    store = make_store(tmp_path)
    ep = store.append(
        AGENT,
        summary="Solved it",
        outcome="partial",
        lessons="Next time use recursion",
        task_id="task-123",
    )
    assert ep["outcome"] == "partial"
    assert ep["lessons"] == "Next time use recursion"
    assert ep["task_id"] == "task-123"


def test_append_creates_jsonl_file(tmp_path):
    store = make_store(tmp_path)
    store.append(AGENT, summary="x", outcome="success")
    path = tmp_path / "test-session" / AGENT / "episodes.jsonl"
    assert path.exists()
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["summary"] == "x"


def test_append_multiple_accumulates_lines(tmp_path):
    store = make_store(tmp_path)
    for i in range(3):
        store.append(AGENT, summary=f"ep{i}", outcome="success")
    path = tmp_path / "test-session" / AGENT / "episodes.jsonl"
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 3


def test_append_episode_id_override(tmp_path):
    store = make_store(tmp_path)
    ep = store.append(AGENT, summary="x", outcome="success", episode_id="custom-id")
    assert ep["id"] == "custom-id"


# ---------------------------------------------------------------------------
# list()
# ---------------------------------------------------------------------------


def test_list_empty_when_no_file(tmp_path):
    store = make_store(tmp_path)
    result = store.list(AGENT)
    assert result == []


def test_list_returns_newest_first(tmp_path):
    store = make_store(tmp_path)
    ids = []
    for i in range(3):
        ep = store.append(AGENT, summary=f"ep{i}", outcome="success")
        ids.append(ep["id"])
    result = store.list(AGENT)
    # newest-first: last appended should be first in list
    assert result[0]["id"] == ids[-1]
    assert result[-1]["id"] == ids[0]


def test_list_respects_limit(tmp_path):
    store = make_store(tmp_path)
    for i in range(10):
        store.append(AGENT, summary=f"ep{i}", outcome="success")
    result = store.list(AGENT, limit=3)
    assert len(result) == 3


def test_list_limit_larger_than_count(tmp_path):
    store = make_store(tmp_path)
    for i in range(2):
        store.append(AGENT, summary=f"ep{i}", outcome="success")
    result = store.list(AGENT, limit=100)
    assert len(result) == 2


def test_list_is_agent_scoped(tmp_path):
    store = make_store(tmp_path)
    store.append("agent-a", summary="for a", outcome="success")
    store.append("agent-b", summary="for b", outcome="failure")
    a_list = store.list("agent-a")
    b_list = store.list("agent-b")
    assert len(a_list) == 1
    assert a_list[0]["summary"] == "for a"
    assert len(b_list) == 1
    assert b_list[0]["summary"] == "for b"


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------


def test_get_returns_episode_by_id(tmp_path):
    store = make_store(tmp_path)
    ep = store.append(AGENT, summary="find me", outcome="success")
    found = store.get(AGENT, ep["id"])
    assert found["id"] == ep["id"]
    assert found["summary"] == "find me"


def test_get_raises_for_unknown_id(tmp_path):
    store = make_store(tmp_path)
    store.append(AGENT, summary="x", outcome="success")
    with pytest.raises(EpisodeNotFoundError):
        store.get(AGENT, "nonexistent-id")


def test_get_raises_for_empty_store(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(EpisodeNotFoundError):
        store.get(AGENT, "any-id")


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------


def test_delete_removes_episode(tmp_path):
    store = make_store(tmp_path)
    ep1 = store.append(AGENT, summary="keep", outcome="success")
    ep2 = store.append(AGENT, summary="remove", outcome="failure")
    store.delete(AGENT, ep2["id"])
    remaining = store.list(AGENT)
    assert len(remaining) == 1
    assert remaining[0]["id"] == ep1["id"]


def test_delete_raises_for_unknown_id(tmp_path):
    store = make_store(tmp_path)
    store.append(AGENT, summary="x", outcome="success")
    with pytest.raises(EpisodeNotFoundError):
        store.delete(AGENT, "nonexistent-id")


def test_delete_raises_for_empty_store(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(EpisodeNotFoundError):
        store.delete(AGENT, "any-id")


def test_delete_preserves_other_episodes(tmp_path):
    store = make_store(tmp_path)
    eps = [store.append(AGENT, summary=f"ep{i}", outcome="success") for i in range(5)]
    store.delete(AGENT, eps[2]["id"])
    remaining_ids = {e["id"] for e in store.list(AGENT)}
    assert eps[2]["id"] not in remaining_ids
    assert len(remaining_ids) == 4


def test_delete_only_episode_empties_log(tmp_path):
    store = make_store(tmp_path)
    ep = store.append(AGENT, summary="sole", outcome="success")
    store.delete(AGENT, ep["id"])
    assert store.list(AGENT) == []


# ---------------------------------------------------------------------------
# has_agent()
# ---------------------------------------------------------------------------


def test_has_agent_false_before_any_append(tmp_path):
    store = make_store(tmp_path)
    assert not store.has_agent(AGENT)


def test_has_agent_true_after_append(tmp_path):
    store = make_store(tmp_path)
    store.append(AGENT, summary="x", outcome="success")
    assert store.has_agent(AGENT)


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_appends_produce_valid_lines(tmp_path):
    store = make_store(tmp_path)
    n = 20
    errors: list[Exception] = []

    def _worker(i: int) -> None:
        try:
            store.append(AGENT, summary=f"concurrent-{i}", outcome="success")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    episodes = store.list(AGENT, limit=n + 10)
    assert len(episodes) == n
    # Each episode should be a valid dict with required fields.
    for ep in episodes:
        assert "id" in ep
        assert "summary" in ep


# ---------------------------------------------------------------------------
# Resilience: corrupt lines
# ---------------------------------------------------------------------------


def test_corrupt_lines_are_skipped(tmp_path):
    store = make_store(tmp_path)
    path = tmp_path / "test-session" / AGENT / "episodes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write one valid + one corrupt line
    valid_ep = store.append(AGENT, summary="valid", outcome="success")
    # Inject corrupt line
    with path.open("a") as f:
        f.write("NOT_JSON\n")
    episodes = store.list(AGENT)
    # Only the valid episode should appear.
    assert len(episodes) == 1
    assert episodes[0]["id"] == valid_ep["id"]
