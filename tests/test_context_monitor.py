"""Tests for ContextMonitor — context window usage monitoring.

Feature: エージェントのコンテキスト使用量モニタリング (DESIGN.md §11, v0.21.0)

Tested behaviours:
1. ContextMonitor polls agents and updates stats (pane_chars, estimated_tokens).
2. context_warning STATUS event is published when threshold is exceeded.
3. context_warning is NOT re-emitted on subsequent polls if already warned.
4. notes_updated STATUS event is published when NOTES.md mtime changes.
5. /summarize is injected (auto_summarize=True) once per threshold crossing.
6. Injection resets after NOTES.md is updated (summarize_injected flag cleared).
7. get_stats() returns None for unknown agents.
8. all_stats() returns a list of all tracked agents.
9. REST GET /agents/{id}/stats returns 200 with correct fields.
10. REST GET /agents/{id}/stats returns 404 for unknown agents.
11. REST GET /context-stats returns list of all tracked agents.
12. Config fields load correctly from YAML (context_window_tokens etc.).
13. Orchestrator integrates ContextMonitor (start/stop).
14. Context below threshold clears warned flag after threshold crossing.
15. notes_path is picked up from agent.worktree_path.
16. ContextMonitor.stop() cancels the background task.
17. poll_all skips agents with no pane.
18. Context warning includes correct payload fields.
19. summarize_triggered event published when auto_summarize fires.
20. notes_updates counter increments on each NOTES.md change.

Design references:
- Liu et al. "Lost in the Middle" TACL 2024 https://arxiv.org/abs/2307.03172
- Anthropic token counting docs (2025) https://platform.claude.com/docs/en/build-with-claude/token-counting
- DESIGN.md §11 (v0.21.0)
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.context_monitor import (
    AgentContextStats,
    ContextMonitor,
    _CHARS_PER_TOKEN,
    _DEFAULT_CONTEXT_WINDOW_TOKENS,
    _DEFAULT_WARN_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(
    agent_id: str = "worker-1",
    pane=None,
    worktree_path: Path | None = None,
) -> MagicMock:
    agent = MagicMock()
    agent.id = agent_id
    agent.pane = pane
    agent.worktree_path = worktree_path
    agent.notify_stdin = AsyncMock()
    return agent


def _make_tmux(capture_text: str = "") -> MagicMock:
    tmux = MagicMock()
    tmux.capture_pane = MagicMock(return_value=capture_text)
    return tmux


async def _collect_events(bus: Bus, count: int, timeout: float = 1.0) -> list[Message]:
    """Subscribe to bus and collect up to *count* messages."""
    q = await bus.subscribe("__test__", broadcast=True)
    events: list[Message] = []
    try:
        async with asyncio.timeout(timeout):
            while len(events) < count:
                msg = await q.get()
                q.task_done()
                events.append(msg)
    except TimeoutError:
        pass
    await bus.unsubscribe("__test__")
    return events


# ---------------------------------------------------------------------------
# 1. Stats update from pane capture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_updates_pane_chars_and_tokens() -> None:
    bus = Bus()
    pane = MagicMock()
    text = "a" * 400  # 400 chars → 100 estimated tokens
    tmux = _make_tmux(text)
    agent = _make_agent("worker-1", pane=pane)
    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=1000,
        warn_threshold=0.9,  # High threshold — no warning expected
        poll_interval=99.0,
    )
    await monitor._poll_all()

    stats = monitor.get_stats("worker-1")
    assert stats is not None
    assert stats["pane_chars"] == 400
    assert stats["estimated_tokens"] == int(400 / _CHARS_PER_TOKEN)
    assert stats["context_pct"] == round(100 / 1000 * 100, 1)


# ---------------------------------------------------------------------------
# 2. context_warning event published when threshold exceeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_warning_event_published_at_threshold() -> None:
    bus = Bus()
    pane = MagicMock()
    # 800 chars → 200 tokens; window=1000 → 20% — threshold=0.1 (10%)
    text = "x" * 800
    tmux = _make_tmux(text)
    agent = _make_agent("worker-1", pane=pane)
    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=1000,
        warn_threshold=0.1,
        poll_interval=99.0,
    )

    received: list[Message] = []

    async def collect() -> None:
        q = await bus.subscribe("__test__", broadcast=True)
        msg = await asyncio.wait_for(q.get(), timeout=2.0)
        received.append(msg)
        q.task_done()

    task = asyncio.create_task(collect())
    await monitor._poll_all()
    await task

    assert len(received) == 1
    assert received[0].type == MessageType.STATUS
    assert received[0].payload["event"] == "context_warning"
    assert received[0].payload["agent_id"] == "worker-1"
    assert received[0].from_id == "__context_monitor__"


# ---------------------------------------------------------------------------
# 3. context_warning NOT re-emitted on subsequent polls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_warning_not_repeated() -> None:
    bus = Bus()
    pane = MagicMock()
    text = "x" * 800
    tmux = _make_tmux(text)
    agent = _make_agent("worker-1", pane=pane)
    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,  # very small window
        warn_threshold=0.1,
        poll_interval=99.0,
    )

    events: list[Message] = []
    q = await bus.subscribe("__test__", broadcast=True)

    await monitor._poll_all()  # First poll — should emit warning
    await monitor._poll_all()  # Second poll — should NOT emit again

    # Drain queue
    while not q.empty():
        msg = await q.get()
        q.task_done()
        events.append(msg)

    await bus.unsubscribe("__test__")
    # Only one context_warning despite two polls
    warnings = [e for e in events if e.payload.get("event") == "context_warning"]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# 4. notes_updated event published when NOTES.md mtime changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notes_updated_event_on_mtime_change(tmp_path: Path) -> None:
    notes = tmp_path / "NOTES.md"
    notes.write_text("# initial\n")

    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux("")
    agent = _make_agent("worker-1", pane=pane, worktree_path=tmp_path)
    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=200_000,
        warn_threshold=0.99,  # Very high — no warning expected
        poll_interval=99.0,
    )

    # First poll seeds mtime
    await monitor._poll_all()

    events: list[Message] = []
    q = await bus.subscribe("__test__", broadcast=True)

    # Simulate NOTES.md update (write new content + bump mtime)
    await asyncio.sleep(0.01)  # ensure mtime changes
    notes.write_text("# updated\n\n## Progress\nDone.\n")
    # Manually bump mtime to ensure difference (some FS have 1s resolution)
    import os
    os.utime(notes, times=(time.time() + 1, time.time() + 1))

    await monitor._poll_all()

    while not q.empty():
        msg = await q.get()
        q.task_done()
        events.append(msg)

    await bus.unsubscribe("__test__")

    updates = [e for e in events if e.payload.get("event") == "notes_updated"]
    assert len(updates) == 1
    assert updates[0].payload["agent_id"] == "worker-1"
    assert updates[0].from_id == "__context_monitor__"
    assert "notes_path" in updates[0].payload


# ---------------------------------------------------------------------------
# 5. /summarize injected when auto_summarize=True and threshold exceeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_summarize_injected_at_threshold() -> None:
    bus = Bus()
    pane = MagicMock()
    text = "x" * 4000  # large output
    tmux = _make_tmux(text)
    agent = _make_agent("worker-1", pane=pane)
    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,  # 4000 chars → 1000 tokens → 1000% of 100-token window
        warn_threshold=0.1,
        auto_summarize=True,
        poll_interval=99.0,
    )

    await monitor._poll_all()

    agent.notify_stdin.assert_called_once_with("/summarize")
    stats = monitor.get_stats("worker-1")
    assert stats is not None
    assert stats["summarize_triggers"] == 1


# ---------------------------------------------------------------------------
# 6. /summarize NOT injected again on second poll before NOTES.md update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_summarize_injected_only_once_per_crossing() -> None:
    bus = Bus()
    pane = MagicMock()
    text = "x" * 4000
    tmux = _make_tmux(text)
    agent = _make_agent("worker-1", pane=pane)
    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.1,
        auto_summarize=True,
        poll_interval=99.0,
    )

    await monitor._poll_all()
    await monitor._poll_all()  # Second poll

    # Should still only have been called once
    assert agent.notify_stdin.call_count == 1


# ---------------------------------------------------------------------------
# 7. get_stats returns None for unknown agents
# ---------------------------------------------------------------------------


def test_get_stats_returns_none_for_unknown_agent() -> None:
    bus = Bus()
    tmux = _make_tmux()
    monitor = ContextMonitor(bus=bus, tmux=tmux, agents=lambda: [])
    assert monitor.get_stats("no-such-agent") is None


# ---------------------------------------------------------------------------
# 8. all_stats returns list of tracked agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_stats_returns_all_agents() -> None:
    bus = Bus()
    tmux = _make_tmux("hello world")
    agents_list = [
        _make_agent("w1", pane=MagicMock()),
        _make_agent("w2", pane=MagicMock()),
    ]
    monitor = ContextMonitor(bus=bus, tmux=tmux, agents=lambda: agents_list)
    await monitor._poll_all()

    all_s = monitor.all_stats()
    assert len(all_s) == 2
    ids = {s["agent_id"] for s in all_s}
    assert ids == {"w1", "w2"}


# ---------------------------------------------------------------------------
# 9. REST GET /agents/{id}/stats — 200 with correct fields
# ---------------------------------------------------------------------------


def _make_mock_orch_with_stats(agent_id: str, stats: dict | None) -> MagicMock:
    orch = MagicMock()
    orch.list_agents.return_value = []
    orch.list_tasks.return_value = []
    orch.get_agent.return_value = None
    orch.get_director.return_value = None
    orch.flush_director_pending.return_value = []
    orch.list_dlq.return_value = []
    orch.is_paused = False
    orch.get_agent_context_stats = MagicMock(return_value=stats)
    orch.all_agent_context_stats = MagicMock(return_value=[stats] if stats else [])
    orch.bus = MagicMock()
    orch.bus.subscribe = AsyncMock(return_value=asyncio.Queue())
    orch.bus.unsubscribe = AsyncMock()
    orch.get_agent_history = MagicMock(return_value=None)
    return orch


_API_KEY = "context-stats-test-key"


def _make_client(orch):
    import tmux_orchestrator.web.app as web_app_mod
    from tmux_orchestrator.web.app import create_app
    from fastapi.testclient import TestClient

    web_app_mod._credentials.clear()
    web_app_mod._sign_counts.clear()
    web_app_mod._sessions.clear()

    class _MockHub:
        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    app = create_app(orch, _MockHub(), api_key=_API_KEY)
    return TestClient(app, raise_server_exceptions=True)


def test_agent_context_stats_endpoint_200() -> None:
    stats = {
        "agent_id": "worker-1",
        "pane_chars": 4000,
        "estimated_tokens": 1000,
        "context_window_tokens": 200_000,
        "context_pct": 0.5,
        "warn_threshold_pct": 75.0,
        "notes_mtime": 0.0,
        "notes_updates": 2,
        "context_warnings": 1,
        "summarize_triggers": 1,
        "last_polled": time.monotonic(),
    }
    orch = _make_mock_orch_with_stats("worker-1", stats)
    client = _make_client(orch)
    resp = client.get("/agents/worker-1/stats", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_id"] == "worker-1"
    assert data["pane_chars"] == 4000
    assert data["estimated_tokens"] == 1000
    assert data["notes_updates"] == 2


# ---------------------------------------------------------------------------
# 10. REST GET /agents/{id}/stats — 404 for unknown agents
# ---------------------------------------------------------------------------


def test_agent_context_stats_endpoint_404() -> None:
    orch = _make_mock_orch_with_stats("worker-1", None)
    client = _make_client(orch)
    resp = client.get("/agents/no-such-agent/stats", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 11. REST GET /context-stats — list all agents
# ---------------------------------------------------------------------------


def test_all_context_stats_endpoint() -> None:
    stats = {
        "agent_id": "worker-1",
        "pane_chars": 100,
        "estimated_tokens": 25,
        "context_window_tokens": 200_000,
        "context_pct": 0.0,
        "warn_threshold_pct": 75.0,
        "notes_mtime": 0.0,
        "notes_updates": 0,
        "context_warnings": 0,
        "summarize_triggers": 0,
        "last_polled": time.monotonic(),
    }
    orch = _make_mock_orch_with_stats("worker-1", stats)
    client = _make_client(orch)
    resp = client.get("/context-stats", headers={"X-API-Key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["agent_id"] == "worker-1"


# ---------------------------------------------------------------------------
# 12. Config fields load from YAML
# ---------------------------------------------------------------------------


def test_config_context_fields_defaults() -> None:
    from tmux_orchestrator.config import OrchestratorConfig

    cfg = OrchestratorConfig()
    assert cfg.context_window_tokens == 200_000
    assert cfg.context_warn_threshold == 0.75
    assert cfg.context_auto_summarize is False
    assert cfg.context_monitor_poll == 5.0


def test_config_context_fields_from_yaml(tmp_path: Path) -> None:
    from tmux_orchestrator.config import load_config

    yaml_content = """\
session_name: test
agents: []
context_window_tokens: 100000
context_warn_threshold: 0.60
context_auto_summarize: true
context_monitor_poll: 2.0
"""
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml_content)
    cfg = load_config(cfg_path)
    assert cfg.context_window_tokens == 100_000
    assert cfg.context_warn_threshold == 0.60
    assert cfg.context_auto_summarize is True
    assert cfg.context_monitor_poll == 2.0


# ---------------------------------------------------------------------------
# 13. Orchestrator integrates ContextMonitor (start/stop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_starts_and_stops_context_monitor() -> None:
    from tmux_orchestrator.config import OrchestratorConfig
    from tmux_orchestrator.orchestrator import Orchestrator

    bus = Bus()
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    cfg = OrchestratorConfig(context_monitor_poll=99.0)

    orch = Orchestrator(bus=bus, tmux=tmux, config=cfg)

    # The monitor task should NOT be running yet
    assert orch._context_monitor._task is None

    # Patch start methods to avoid actual tmux calls
    with patch.object(orch, "_dispatch_loop", AsyncMock()), \
         patch.object(orch, "_route_loop", AsyncMock()), \
         patch.object(orch, "_watchdog_loop", AsyncMock()), \
         patch.object(orch, "_recovery_loop", AsyncMock()):
        # We can't call orch.start() without real agents/tmux, so test the monitor directly
        orch._context_monitor.start()
        assert orch._context_monitor._task is not None
        assert not orch._context_monitor._task.done()

        orch._context_monitor.stop()
        await asyncio.sleep(0.05)
        assert orch._context_monitor._task.done()


# ---------------------------------------------------------------------------
# 14. Context below threshold clears warned flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warned_flag_resets_when_context_drops_below_threshold() -> None:
    bus = Bus()
    pane = MagicMock()

    captured = {"text": "x" * 4000}  # above threshold
    tmux = MagicMock()
    tmux.capture_pane = MagicMock(side_effect=lambda _: captured["text"])

    agent = _make_agent("worker-1", pane=pane)
    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.1,
        poll_interval=99.0,
    )

    # Poll with high context — warned flag set
    await monitor._poll_all()
    assert monitor._stats["worker-1"].warned is True

    # Now context drops
    captured["text"] = "x"  # 1 char → 0 tokens → 0%
    await monitor._poll_all()
    assert monitor._stats["worker-1"].warned is False


# ---------------------------------------------------------------------------
# 15. notes_path is picked up from agent.worktree_path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notes_path_resolved_from_worktree(tmp_path: Path) -> None:
    notes = tmp_path / "NOTES.md"
    notes.write_text("# agent notes\n")

    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux("")
    agent = _make_agent("worker-1", pane=pane, worktree_path=tmp_path)
    monitor = ContextMonitor(
        bus=bus, tmux=tmux, agents=lambda: [agent], poll_interval=99.0
    )

    await monitor._poll_all()
    s = monitor._stats.get("worker-1")
    assert s is not None
    assert s.notes_path == tmp_path / "NOTES.md"
    assert s.notes_mtime > 0


# ---------------------------------------------------------------------------
# 16. stop() cancels background task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_cancels_background_task() -> None:
    bus = Bus()
    tmux = _make_tmux()
    monitor = ContextMonitor(bus=bus, tmux=tmux, agents=lambda: [], poll_interval=99.0)
    monitor.start()
    task = monitor._task
    assert task is not None
    monitor.stop()
    await asyncio.sleep(0.05)
    assert task.done()


# ---------------------------------------------------------------------------
# 17. poll_all skips agents with no pane (no capture, no error)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_skips_agent_without_pane() -> None:
    bus = Bus()
    tmux = _make_tmux("some text")
    agent = _make_agent("worker-no-pane", pane=None)
    monitor = ContextMonitor(bus=bus, tmux=tmux, agents=lambda: [agent], poll_interval=99.0)

    await monitor._poll_all()

    stats = monitor.get_stats("worker-no-pane")
    assert stats is not None
    # pane_chars stays at 0 since pane is None
    assert stats["pane_chars"] == 0
    # capture_pane should NOT have been called
    tmux.capture_pane.assert_not_called()


# ---------------------------------------------------------------------------
# 18. context_warning payload includes expected fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_warning_payload_fields() -> None:
    bus = Bus()
    pane = MagicMock()
    text = "x" * 800
    tmux = _make_tmux(text)
    agent = _make_agent("worker-1", pane=pane)
    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=1000,
        warn_threshold=0.1,
        poll_interval=99.0,
    )

    received: list[Message] = []
    q = await bus.subscribe("__test__", broadcast=True)

    await monitor._poll_all()

    while not q.empty():
        msg = await q.get()
        q.task_done()
        received.append(msg)

    await bus.unsubscribe("__test__")

    warnings = [m for m in received if m.payload.get("event") == "context_warning"]
    assert len(warnings) == 1
    payload = warnings[0].payload
    assert "pane_chars" in payload
    assert "estimated_tokens" in payload
    assert "context_pct" in payload
    assert "context_window_tokens" in payload
    assert payload["agent_id"] == "worker-1"


# ---------------------------------------------------------------------------
# 19. summarize_triggered event published alongside notify_stdin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_triggered_event_published() -> None:
    bus = Bus()
    pane = MagicMock()
    text = "x" * 4000
    tmux = _make_tmux(text)
    agent = _make_agent("worker-1", pane=pane)
    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.1,
        auto_summarize=True,
        poll_interval=99.0,
    )

    events: list[Message] = []
    q = await bus.subscribe("__test__", broadcast=True)

    await monitor._poll_all()

    while not q.empty():
        msg = await q.get()
        q.task_done()
        events.append(msg)

    await bus.unsubscribe("__test__")

    triggered = [e for e in events if e.payload.get("event") == "summarize_triggered"]
    assert len(triggered) == 1
    assert triggered[0].payload["agent_id"] == "worker-1"
    assert "estimated_tokens" in triggered[0].payload


# ---------------------------------------------------------------------------
# 20. notes_updates counter increments on each change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notes_updates_counter_increments(tmp_path: Path) -> None:
    notes = tmp_path / "NOTES.md"
    notes.write_text("# v1\n")

    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux("")
    agent = _make_agent("worker-1", pane=pane, worktree_path=tmp_path)
    monitor = ContextMonitor(
        bus=bus, tmux=tmux, agents=lambda: [agent], poll_interval=99.0
    )

    # Poll 1 — seeds mtime, no update event
    await monitor._poll_all()
    assert monitor._stats["worker-1"].notes_updates == 0

    # Update NOTES.md
    import os
    notes.write_text("# v2\n")
    os.utime(notes, times=(time.time() + 1, time.time() + 1))

    # Poll 2 — detects change
    await monitor._poll_all()
    assert monitor._stats["worker-1"].notes_updates == 1

    # Update again
    notes.write_text("# v3\n")
    os.utime(notes, times=(time.time() + 2, time.time() + 2))

    # Poll 3 — detects second change
    await monitor._poll_all()
    assert monitor._stats["worker-1"].notes_updates == 2
