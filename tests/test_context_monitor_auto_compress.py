"""Tests for ContextMonitor auto-compress integration (v1.1.12).

Feature: ContextMonitor TF-IDF 自動統合 (DESIGN.md §10.37 v1.1.12)

When ``auto_compress=True`` is set on the ContextMonitor, the monitor
automatically runs TF-IDF extractive compression on the agent's pane output
when the context threshold is exceeded, and injects the compressed text via
``agent.notify_stdin`` using the ``__COMPRESS_CONTEXT__`` protocol token.

Tested behaviours:
1.  auto_compress=False (default) — no compress_triggered event, no injection.
2.  auto_compress=True — compress_triggered event published at threshold.
3.  auto_compress=True — agent.notify_stdin called with __COMPRESS_CONTEXT__ prefix.
4.  auto_compress injection fires at most once per threshold crossing.
5.  compress_injected flag resets when context drops below threshold.
6.  compress_triggers counter increments on each trigger.
7.  get_stats() includes compress_triggers field.
8.  compress_triggered event payload includes all required fields.
9.  auto_compress + auto_summarize can fire together independently.
10. auto_compress is skipped when agent has no pane.
11. auto_compress is skipped when pane capture returns empty string.
12. compress_drop_percentile is forwarded to TfIdfContextCompressor.
13. Config fields context_auto_compress / context_compress_drop_percentile
    load from YAML correctly.
14. Orchestrator passes new config fields to ContextMonitor.
15. compress_triggered event carries correct compression ratio.
16. Second poll after reset triggers a new compress cycle.
17. auto_compress with empty pane text does not inject or publish.
18. compress_injected is independent of summarize_injected.

Design references:
- ACON arXiv:2510.00615 (Kang et al. 2025): threshold-based auto-compress.
- Focus Agent arXiv:2601.07190 (Verma 2026): intra-trajectory compression.
- DESIGN.md §10.37 (v1.1.12)
"""
from __future__ import annotations

import asyncio
import io
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import load_config
from tmux_orchestrator.context_monitor import (
    AgentContextStats,
    ContextMonitor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(
    agent_id: str = "worker-1",
    pane: Any = None,
) -> MagicMock:
    agent = MagicMock()
    agent.id = agent_id
    agent.pane = pane
    agent.worktree_path = None
    agent.notify_stdin = AsyncMock()
    return agent


def _make_tmux(capture_text: str = "") -> MagicMock:
    tmux = MagicMock()
    tmux.capture_pane = MagicMock(return_value=capture_text)
    return tmux


_LONG_PANE_TEXT = "\n".join(
    [
        "word " * 20 + f"line {i}"
        for i in range(50)
    ]
)
"""50 lines of text that will have meaningful TF-IDF variation."""


async def _collect_events_by_type(
    bus: Bus, event_type: str, count: int, timeout: float = 1.0
) -> list[Message]:
    q = await bus.subscribe("__test__", broadcast=True)
    events: list[Message] = []
    try:
        async with asyncio.timeout(timeout):
            while len(events) < count:
                msg = await q.get()
                q.task_done()
                if msg.payload.get("event") == event_type:
                    events.append(msg)
    except TimeoutError:
        pass
    await bus.unsubscribe("__test__")
    return events


# ---------------------------------------------------------------------------
# 1. auto_compress=False (default) — no injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_compress_disabled_by_default() -> None:
    """With auto_compress=False no compress_triggered event is published."""
    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-1", pane=pane)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,  # very small → pane text exceeds threshold
        warn_threshold=0.01,
        auto_compress=False,
        poll_interval=99.0,
    )

    q = await bus.subscribe("__test__", broadcast=True)
    await monitor._poll_all()

    events: list[Message] = []
    while not q.empty():
        msg = await q.get()
        q.task_done()
        events.append(msg)
    await bus.unsubscribe("__test__")

    compress_events = [e for e in events if e.payload.get("event") == "compress_triggered"]
    assert compress_events == [], "No compress events expected when auto_compress=False"
    agent.notify_stdin.assert_not_called()


# ---------------------------------------------------------------------------
# 2. auto_compress=True — compress_triggered event published
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_compress_publishes_compress_triggered_event() -> None:
    """compress_triggered STATUS event is published when auto_compress=True."""
    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-1", pane=pane)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )

    events = await _collect_events_by_type(
        bus,
        "compress_triggered",
        1,
        timeout=2.0,
    )
    # Run poll concurrently with event collection
    monitor_task = asyncio.create_task(monitor._poll_all())
    events = await _collect_events_by_type(bus, "compress_triggered", 1, timeout=2.0)
    await monitor_task

    assert len(events) >= 1
    assert events[0].payload["event"] == "compress_triggered"
    assert events[0].payload["agent_id"] == "worker-1"
    assert events[0].from_id == "__context_monitor__"


# ---------------------------------------------------------------------------
# 3. notify_stdin called with __COMPRESS_CONTEXT__ prefix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_compress_calls_notify_stdin_with_protocol_token() -> None:
    """agent.notify_stdin receives a string starting with __COMPRESS_CONTEXT__."""
    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-1", pane=pane)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )

    await monitor._poll_all()

    agent.notify_stdin.assert_called_once()
    call_arg: str = agent.notify_stdin.call_args[0][0]
    assert call_arg.startswith("__COMPRESS_CONTEXT__\n"), (
        f"Expected __COMPRESS_CONTEXT__ prefix, got: {call_arg[:80]!r}"
    )


# ---------------------------------------------------------------------------
# 4. Injection fires at most once per threshold crossing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_compress_fires_once_per_threshold_crossing() -> None:
    """compress_triggered is published only once even if threshold stays exceeded."""
    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-1", pane=pane)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )

    await monitor._poll_all()  # First poll — should compress
    await monitor._poll_all()  # Second poll — still above threshold, should NOT re-compress

    assert agent.notify_stdin.call_count == 1, (
        "notify_stdin should be called exactly once per threshold crossing"
    )


# ---------------------------------------------------------------------------
# 5. compress_injected resets when context drops below threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compress_injected_flag_resets_below_threshold() -> None:
    """compress_injected flag resets when context falls below warn_threshold."""
    bus = Bus()
    pane = MagicMock()
    # Start with text that exceeds threshold
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-1", pane=pane)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,  # very low → always exceeded with long text
        auto_compress=True,
        poll_interval=99.0,
    )

    await monitor._poll_all()  # Trigger compress
    s: AgentContextStats = monitor._stats["worker-1"]
    assert s.compress_injected is True, "compress_injected should be set after trigger"

    # Simulate context dropping below threshold by using a very high threshold
    monitor._warn_threshold = 999.0  # impossibly high — never exceeded
    await monitor._poll_all()  # Poll with high threshold → below threshold
    assert s.compress_injected is False, "compress_injected should reset below threshold"


# ---------------------------------------------------------------------------
# 6. compress_triggers counter increments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compress_triggers_counter_increments() -> None:
    """compress_triggers counter increases by 1 on each auto-compress trigger."""
    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-1", pane=pane)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )

    await monitor._poll_all()
    s: AgentContextStats = monitor._stats["worker-1"]
    assert s.compress_triggers == 1

    # Reset flags manually to simulate a new threshold crossing
    s.compress_injected = False
    s.warned = False
    await monitor._poll_all()
    assert s.compress_triggers == 2


# ---------------------------------------------------------------------------
# 7. get_stats includes compress_triggers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_stats_includes_compress_triggers() -> None:
    """get_stats() returns a dict that includes 'compress_triggers' key."""
    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-1", pane=pane)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )

    await monitor._poll_all()
    stats = monitor.get_stats("worker-1")
    assert stats is not None
    assert "compress_triggers" in stats
    assert stats["compress_triggers"] == 1


# ---------------------------------------------------------------------------
# 8. compress_triggered event payload includes all required fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compress_triggered_payload_fields() -> None:
    """compress_triggered event payload contains all documented fields."""
    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-1", pane=pane)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        compress_drop_percentile=0.30,
        poll_interval=99.0,
    )

    q = await bus.subscribe("__test__", broadcast=True)

    monitor_task = asyncio.create_task(monitor._poll_all())
    await monitor_task

    events: list[Message] = []
    while not q.empty():
        msg = await q.get()
        q.task_done()
        events.append(msg)
    await bus.unsubscribe("__test__")

    compress_events = [e for e in events if e.payload.get("event") == "compress_triggered"]
    assert len(compress_events) == 1

    payload = compress_events[0].payload
    required_fields = {
        "event", "agent_id", "estimated_tokens", "context_pct",
        "original_lines", "kept_lines", "original_chars", "compressed_chars",
        "compression_ratio", "drop_percentile",
    }
    missing = required_fields - set(payload.keys())
    assert not missing, f"Missing payload fields: {missing}"

    assert payload["agent_id"] == "worker-1"
    assert payload["drop_percentile"] == pytest.approx(0.30)
    assert 0.0 <= payload["compression_ratio"] <= 1.0
    assert payload["kept_lines"] <= payload["original_lines"]
    assert payload["compressed_chars"] <= payload["original_chars"]


# ---------------------------------------------------------------------------
# 9. auto_compress + auto_summarize fire independently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_compress_and_auto_summarize_fire_together() -> None:
    """Both summarize_triggered and compress_triggered events fire when both flags are set."""
    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-1", pane=pane)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_summarize=True,
        auto_compress=True,
        poll_interval=99.0,
    )

    q = await bus.subscribe("__test__", broadcast=True)
    await monitor._poll_all()

    events: list[Message] = []
    while not q.empty():
        msg = await q.get()
        q.task_done()
        events.append(msg)
    await bus.unsubscribe("__test__")

    event_types = {e.payload.get("event") for e in events}
    assert "summarize_triggered" in event_types, "summarize_triggered should fire"
    assert "compress_triggered" in event_types, "compress_triggered should fire"

    # notify_stdin called twice: once for /summarize, once for __COMPRESS_CONTEXT__
    assert agent.notify_stdin.call_count == 2


# ---------------------------------------------------------------------------
# 10. auto_compress skipped when agent has no pane
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_compress_skipped_when_no_pane() -> None:
    """auto_compress does not fire if agent.pane is None."""
    bus = Bus()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-1", pane=None)  # No pane

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )

    await monitor._poll_all()
    agent.notify_stdin.assert_not_called()


# ---------------------------------------------------------------------------
# 11. auto_compress skipped on empty pane text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_compress_skipped_on_empty_pane_text() -> None:
    """auto_compress does not inject or publish if the pane capture is empty/whitespace."""
    bus = Bus()
    pane = MagicMock()
    # Large count of whitespace chars — exceeds threshold but no content
    tmux = _make_tmux("   \n   \n   " * 1000)
    agent = _make_agent("worker-1", pane=pane)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )

    q = await bus.subscribe("__test__", broadcast=True)
    await monitor._poll_all()

    events: list[Message] = []
    while not q.empty():
        msg = await q.get()
        q.task_done()
        events.append(msg)
    await bus.unsubscribe("__test__")

    compress_events = [e for e in events if e.payload.get("event") == "compress_triggered"]
    assert compress_events == [], "compress_triggered should not fire on whitespace-only pane"
    agent.notify_stdin.assert_not_called()


# ---------------------------------------------------------------------------
# 12. compress_drop_percentile is forwarded to TfIdfContextCompressor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compress_drop_percentile_forwarded() -> None:
    """compress_drop_percentile value from config is used in TF-IDF compressor."""
    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-1", pane=pane)

    PERCENTILE = 0.20
    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        compress_drop_percentile=PERCENTILE,
        poll_interval=99.0,
    )

    assert monitor._compressor is not None
    assert monitor._compressor._drop_percentile == pytest.approx(PERCENTILE)


# ---------------------------------------------------------------------------
# 13. Config fields load from YAML
# ---------------------------------------------------------------------------


def test_config_auto_compress_fields_load_from_yaml(tmp_path: "Path") -> None:
    """context_auto_compress and context_compress_drop_percentile are parsed from YAML."""
    from pathlib import Path
    from tmux_orchestrator.config import load_config

    yaml_content = """\
session_name: test
agents:
  - id: worker-1
    type: claude_code
    command: claude
context_auto_compress: true
context_compress_drop_percentile: 0.35
"""
    cfg_file = tmp_path / "test_config.yaml"
    cfg_file.write_text(yaml_content)
    cfg = load_config(cfg_file)
    assert cfg.context_auto_compress is True
    assert cfg.context_compress_drop_percentile == pytest.approx(0.35)


# ---------------------------------------------------------------------------
# 14. Orchestrator passes new config fields to ContextMonitor
# ---------------------------------------------------------------------------


def test_orchestrator_passes_auto_compress_to_context_monitor() -> None:
    """Orchestrator wires context_auto_compress + context_compress_drop_percentile."""
    from tmux_orchestrator.config import OrchestratorConfig, AgentConfig
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.bus import Bus

    config = OrchestratorConfig(
        session_name="test",
        agents=[AgentConfig(id="w1", type="claude_code", command="echo")],
        context_auto_compress=True,
        context_compress_drop_percentile=0.50,
        context_warn_threshold=0.75,
        context_window_tokens=200_000,
    )
    bus = Bus()
    tmux = MagicMock()

    orch = Orchestrator(config=config, bus=bus, tmux=tmux)
    monitor = orch._context_monitor

    # The monitor should be a ContextMonitor (not Null) and have the right config
    assert hasattr(monitor, "_auto_compress")
    assert monitor._auto_compress is True  # type: ignore[attr-defined]
    assert monitor._compress_drop_percentile == pytest.approx(0.50)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 15. compress_triggered event carries correct compression_ratio
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compress_triggered_compression_ratio_in_range() -> None:
    """compression_ratio in compress_triggered payload is in [0.0, 1.0)."""
    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-1", pane=pane)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )

    q = await bus.subscribe("__test__", broadcast=True)
    await monitor._poll_all()

    events: list[Message] = []
    while not q.empty():
        msg = await q.get()
        q.task_done()
        events.append(msg)
    await bus.unsubscribe("__test__")

    compress_events = [e for e in events if e.payload.get("event") == "compress_triggered"]
    assert compress_events
    ratio = compress_events[0].payload["compression_ratio"]
    assert 0.0 <= ratio < 1.0, f"compression_ratio={ratio} out of expected range [0, 1)"


# ---------------------------------------------------------------------------
# 16. Second threshold crossing after reset triggers a new compress cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_compress_triggers_again_after_reset() -> None:
    """After context drops below threshold and rises again, auto-compress fires again."""
    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-1", pane=pane)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )

    # First crossing
    await monitor._poll_all()
    assert agent.notify_stdin.call_count == 1

    # Simulate context dropping below threshold
    monitor._warn_threshold = 999.0
    await monitor._poll_all()

    # Reset to trigger again
    monitor._warn_threshold = 0.01
    await monitor._poll_all()
    assert agent.notify_stdin.call_count == 2, (
        "Expected a second injection after threshold re-crossed"
    )


# ---------------------------------------------------------------------------
# 17. Empty pane text does not inject or publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_compress_no_inject_on_whitespace_only() -> None:
    """If pane capture is entirely whitespace, no injection or event should occur."""
    bus = Bus()
    pane = MagicMock()
    # Generate enough whitespace characters to exceed the tiny window
    tmux = _make_tmux("  \n" * 500)
    agent = _make_agent("worker-1", pane=pane)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )

    await monitor._poll_all()

    s = monitor._stats.get("worker-1")
    assert s is not None
    assert s.compress_triggers == 0
    agent.notify_stdin.assert_not_called()


# ---------------------------------------------------------------------------
# 18. compress_injected is independent of summarize_injected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compress_injected_independent_of_summarize_injected() -> None:
    """compress_injected and summarize_injected flags are managed independently."""
    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-1", pane=pane)

    # Only auto_compress=True (no auto_summarize)
    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_summarize=False,
        auto_compress=True,
        poll_interval=99.0,
    )

    await monitor._poll_all()
    s = monitor._stats["worker-1"]
    assert s.compress_injected is True
    assert s.summarize_injected is False, "summarize_injected should remain False"


# ---------------------------------------------------------------------------
# 19. _compressor is None when auto_compress=False
# ---------------------------------------------------------------------------


def test_compressor_is_none_when_auto_compress_disabled() -> None:
    """_compressor attribute is None when auto_compress=False (avoids overhead)."""
    bus = Bus()
    tmux = _make_tmux()

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [],
        auto_compress=False,
    )
    assert monitor._compressor is None


# ---------------------------------------------------------------------------
# 20. _compressor is created when auto_compress=True
# ---------------------------------------------------------------------------


def test_compressor_created_when_auto_compress_enabled() -> None:
    """_compressor is a TfIdfContextCompressor instance when auto_compress=True."""
    from tmux_orchestrator.application.context_compression import TfIdfContextCompressor

    bus = Bus()
    tmux = _make_tmux()

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [],
        auto_compress=True,
        compress_drop_percentile=0.45,
    )
    assert isinstance(monitor._compressor, TfIdfContextCompressor)
    assert monitor._compressor._drop_percentile == pytest.approx(0.45)


# ---------------------------------------------------------------------------
# 21–26. File-based __COMPRESS_CONTEXT__ delivery (v1.1.13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_compress_writes_file_when_worktree_present(
    tmp_path: Any,
) -> None:
    """When agent.worktree_path is set, compressed text is written to a file."""
    import pathlib

    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-1", pane=pane)
    agent.worktree_path = pathlib.Path(tmp_path)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )
    await monitor._poll_all()

    # File must have been created in the worktree directory.
    compress_files = list(pathlib.Path(tmp_path).glob("__compress_context__*.txt"))
    assert len(compress_files) == 1, f"Expected 1 compress file, found: {compress_files}"


@pytest.mark.asyncio
async def test_auto_compress_file_named_with_agent_id(
    tmp_path: Any,
) -> None:
    """Compress file is named __compress_context__<agent_id>__.txt."""
    import pathlib

    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("my-special-agent", pane=pane)
    agent.worktree_path = pathlib.Path(tmp_path)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )
    await monitor._poll_all()

    expected = pathlib.Path(tmp_path) / "__compress_context__my-special-agent__.txt"
    assert expected.exists(), f"Expected file {expected} does not exist"


@pytest.mark.asyncio
async def test_auto_compress_file_contains_compressed_text(
    tmp_path: Any,
) -> None:
    """The compress file contains actual compressed text (non-empty)."""
    import pathlib

    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-2", pane=pane)
    agent.worktree_path = pathlib.Path(tmp_path)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )
    await monitor._poll_all()

    compress_file = pathlib.Path(tmp_path) / "__compress_context__worker-2__.txt"
    assert compress_file.exists()
    content = compress_file.read_text(encoding="utf-8")
    assert content.strip(), "Compress file must contain non-empty content"


@pytest.mark.asyncio
async def test_auto_compress_sends_short_trigger_not_inline_text_when_worktree(
    tmp_path: Any,
) -> None:
    """When worktree_path is set, notify_stdin receives only '__COMPRESS_CONTEXT__'."""
    import pathlib

    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-3", pane=pane)
    agent.worktree_path = pathlib.Path(tmp_path)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )
    await monitor._poll_all()

    agent.notify_stdin.assert_called_once()
    call_arg: str = agent.notify_stdin.call_args[0][0]
    assert call_arg == "__COMPRESS_CONTEXT__", (
        f"Expected only '__COMPRESS_CONTEXT__' trigger, got: {call_arg[:80]!r}"
    )


@pytest.mark.asyncio
async def test_auto_compress_fallback_inline_when_no_worktree() -> None:
    """When worktree_path is None, falls back to inline __COMPRESS_CONTEXT__\\n{text}."""
    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-4", pane=pane)
    agent.worktree_path = None  # no worktree

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )
    await monitor._poll_all()

    agent.notify_stdin.assert_called_once()
    call_arg: str = agent.notify_stdin.call_args[0][0]
    assert call_arg.startswith("__COMPRESS_CONTEXT__\n"), (
        f"Expected inline __COMPRESS_CONTEXT__\\n prefix, got: {call_arg[:80]!r}"
    )


@pytest.mark.asyncio
async def test_auto_compress_file_delivery_still_publishes_compress_triggered_event(
    tmp_path: Any,
) -> None:
    """File-based delivery still publishes the compress_triggered event."""
    import pathlib

    bus = Bus()
    pane = MagicMock()
    tmux = _make_tmux(_LONG_PANE_TEXT)
    agent = _make_agent("worker-5", pane=pane)
    agent.worktree_path = pathlib.Path(tmp_path)

    monitor = ContextMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        context_window_tokens=100,
        warn_threshold=0.01,
        auto_compress=True,
        poll_interval=99.0,
    )

    q = await bus.subscribe("__test2__", broadcast=True)
    await monitor._poll_all()

    events: list[Message] = []
    while not q.empty():
        msg = await q.get()
        q.task_done()
        events.append(msg)
    await bus.unsubscribe("__test2__")

    compress_events = [e for e in events if e.payload.get("event") == "compress_triggered"]
    assert len(compress_events) == 1, "compress_triggered event must be published"
    assert compress_events[0].payload["agent_id"] == "worker-5"
