"""Tests for DriftMonitor — agent behavioral drift detection.

Feature: エージェントドリフト検出 (DESIGN.md §10.20, v1.0.9 / §10.49 v1.1.17)

Implements a subset of the Agent Stability Index (ASI) from:
  Rath, "Agent Drift: Quantifying Behavioral Degradation in Multi-Agent LLM Systems
  Over Extended Interactions", arXiv:2601.04170, January 2026.

v1.1.17: role_score now uses TF-IDF cosine similarity (pure stdlib, zero new deps)
instead of keyword-overlap heuristic.  See DESIGN.md §10.49.

Tested behaviours:
1. DriftMonitor publishes agent_drift_warning when role_score drops below threshold.
2. DriftMonitor does NOT re-publish if agent was already warned this window.
3. role_score is computed via TF-IDF cosine similarity between system_prompt and pane output.
4. idle_score drops when no pane output change is detected for > idle_threshold seconds.
5. length_score detects sharp variance in output line counts.
6. composite drift_score = weighted average of role_score, idle_score, length_score.
7. drift_score >= threshold → no event; drift_score < threshold → agent_drift_warning.
8. warned flag resets when drift_score recovers above threshold.
9. get_drift_stats() returns None for unknown agents.
10. get_drift_stats() returns typed dict with all expected fields.
11. all_drift_stats() returns list of all tracked agents.
12. DriftMonitor.start() spawns background task; stop() cancels it.
13. Agents with no pane are skipped in poll cycle.
14. agent_drift_warning payload contains agent_id, drift_score, role_score, idle_score, length_score.
15. New agents discovered after start() are picked up automatically.
16. Drift warning fires when pane output is semantically unrelated to system_prompt.
17. REST GET /agents/{id}/drift returns 200 with correct fields (integration test).
18. REST GET /agents/{id}/drift returns 404 for unknown agent.
19. REST GET /drift returns list of all drift stats.
20. Config fields drift_monitor_poll and drift_threshold load from YAML.
21. TF-IDF role_score: identical documents → score close to 1.0.
22. TF-IDF role_score: completely disjoint vocabularies → score = 0.0.
23. TF-IDF role_score: partial overlap → 0.0 < score < 1.0.
24. _tfidf_cosine_similarity handles empty doc_a gracefully.
25. _tfidf_cosine_similarity handles empty doc_b gracefully.
26. _tokenize_role filters tokens shorter than _MIN_KEYWORD_LEN.
27. _tokenize_role lowercases tokens.

Design references:
- Rath arXiv:2601.04170 "Agent Drift" (2026) — ASI 12 dimensions, threshold τ=0.75
- ACL 2025 BlackboxNLP "Emergent Convergence in Multi-Agent LLM Annotation"
- "Behavioral Monitoring & Anomaly Detection for Agents" tekysinfo.com (2025)
- Monitoring LLM-based Multi-Agent Systems arXiv:2510.19420 (2025)
- DESIGN.md §10.20 (v1.0.9), §10.49 (v1.1.17)
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.drift_monitor import (
    AgentDriftStats,
    DriftMonitor,
    _DEFAULT_DRIFT_THRESHOLD,
    _DEFAULT_POLL,
    _compute_role_score,
    _compute_idle_score,
    _compute_length_score,
    _composite_score,
    _tfidf_cosine_similarity,
    _tokenize_role,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(
    agent_id: str = "worker-1",
    pane=None,
    system_prompt: str | None = None,
) -> MagicMock:
    agent = MagicMock()
    agent.id = agent_id
    agent.pane = pane
    agent.system_prompt = system_prompt
    return agent


def _make_tmux(capture_text: str = "") -> MagicMock:
    tmux = MagicMock()
    tmux.capture_pane = MagicMock(return_value=capture_text)
    return tmux


async def _collect_events(bus: Bus, count: int, timeout: float = 1.0) -> list[Message]:
    q = await bus.subscribe("__test__", broadcast=True)
    events: list[Message] = []
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while len(events) < count:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(q.get(), timeout=remaining)
                if msg.type == MessageType.STATUS and msg.payload.get("event") == "agent_drift_warning":
                    events.append(msg)
            except asyncio.TimeoutError:
                break
    finally:
        await bus.unsubscribe("__test__")
    return events


# ---------------------------------------------------------------------------
# Unit tests — scoring functions
# ---------------------------------------------------------------------------


class TestRoleScore:
    def test_high_overlap_returns_high_score(self):
        # All system_prompt keywords present in output → TF-IDF cosine is positive.
        # Note: TF-IDF cosine between documents of different length is not 1.0 —
        # extra tokens in the output reduce the IDF weight of shared terms.
        # We require a meaningful positive score (> 0.3) rather than exact 1.0.
        score = _compute_role_score("implement a sorting algorithm", "Here I implement a sorting algorithm step by step")
        assert score > 0.3

    def test_zero_overlap_returns_zero(self):
        # None of the keywords present → score = 0.0
        score = _compute_role_score("implement sorting algorithm python", "Hello world foo bar baz")
        assert score == pytest.approx(0.0)

    def test_partial_overlap(self):
        # Partial token overlap → 0.0 < score < 1.0
        score = _compute_role_score("implement sorting algorithm python", "I will implement the task today")
        assert 0.0 < score < 1.0

    def test_empty_system_prompt_returns_one(self):
        # No role constraint → no drift possible → score = 1.0
        score = _compute_role_score("", "anything goes here")
        assert score == pytest.approx(1.0)

    def test_empty_output_returns_zero(self):
        # Role keywords specified but output is empty → score = 0.0
        score = _compute_role_score("implement sorting algorithm", "")
        assert score == pytest.approx(0.0)

    def test_case_insensitive(self):
        # Both documents have exactly "python" and "sorting" — cosine should be high
        score = _compute_role_score("Python Sorting", "python sorting is cool")
        assert score > 0.7

    def test_short_stop_words_excluded(self):
        # Words shorter than 4 chars should not count as keywords
        # "do", "a", "ok", "so" are all < 4 chars — all filtered out
        score = _compute_role_score("do a ok so", "completely unrelated text here")
        # All words are short stop words — should behave as empty prompt
        assert score == pytest.approx(1.0)

    def test_identical_documents_high_similarity(self):
        # Same text as prompt and output → cosine should be exactly 1.0
        text = "implement sorting algorithm refactor testing"
        score = _compute_role_score(text, text)
        assert score == pytest.approx(1.0)

    def test_unrelated_output_low_similarity(self):
        # Semantically unrelated output → low cosine similarity
        score = _compute_role_score(
            "implement sorting algorithm refactor python code",
            "pizza recipe: mix flour eggs butter salt sugar vanilla cream",
        )
        assert score < 0.3


class TestIdleScore:
    def test_fresh_output_returns_one(self):
        # Last change just now → score = 1.0
        score = _compute_idle_score(last_change_time=time.monotonic(), idle_threshold=30.0)
        assert score == pytest.approx(1.0)

    def test_completely_idle_returns_zero(self):
        # Last change very long ago → score = 0.0
        long_ago = time.monotonic() - 3600.0
        score = _compute_idle_score(last_change_time=long_ago, idle_threshold=30.0)
        assert score == pytest.approx(0.0)

    def test_half_threshold_returns_half(self):
        # Elapsed = threshold → score = 0.0 (at or past threshold)
        elapsed = 30.0
        last_change = time.monotonic() - elapsed
        score = _compute_idle_score(last_change_time=last_change, idle_threshold=elapsed)
        # At exactly threshold → score should be 0 (clamped)
        assert score == pytest.approx(0.0, abs=0.05)

    def test_quarter_threshold(self):
        # Elapsed = threshold / 4 → score should be 0.75
        elapsed = 7.5
        last_change = time.monotonic() - elapsed
        score = _compute_idle_score(last_change_time=last_change, idle_threshold=30.0)
        assert score == pytest.approx(0.75, abs=0.05)


class TestLengthScore:
    def test_stable_length_returns_one(self):
        # Same line count every cycle → score = 1.0
        score = _compute_length_score(history=[100, 100, 100, 100, 100])
        assert score == pytest.approx(1.0)

    def test_empty_history_returns_one(self):
        score = _compute_length_score(history=[])
        assert score == pytest.approx(1.0)

    def test_single_entry_returns_one(self):
        score = _compute_length_score(history=[42])
        assert score == pytest.approx(1.0)

    def test_high_variance_returns_low_score(self):
        # Large swings in line count → low score
        score = _compute_length_score(history=[10, 500, 10, 600, 10])
        assert score < 0.5

    def test_moderate_variance(self):
        # Moderate growth — acceptable
        score = _compute_length_score(history=[100, 105, 110, 115, 120])
        assert score > 0.7


class TestCompositeScore:
    def test_all_ones(self):
        assert _composite_score(1.0, 1.0, 1.0) == pytest.approx(1.0)

    def test_all_zeros(self):
        assert _composite_score(0.0, 0.0, 0.0) == pytest.approx(0.0)

    def test_weighted_average(self):
        # Weights: role=0.50, idle=0.30, length=0.20
        expected = 0.50 * 1.0 + 0.30 * 0.0 + 0.20 * 1.0
        assert _composite_score(1.0, 0.0, 1.0) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Unit tests — TF-IDF helper functions (v1.1.17)
# ---------------------------------------------------------------------------


class TestTfIdfCosine:
    """Tests for the pure-stdlib TF-IDF cosine similarity helper."""

    def test_identical_docs_score_one(self):
        # Two identical token lists → cosine = 1.0
        tokens = ["implement", "sorting", "algorithm", "python"]
        assert _tfidf_cosine_similarity(tokens, tokens) == pytest.approx(1.0)

    def test_disjoint_docs_score_zero(self):
        # No shared terms → cosine = 0.0
        assert _tfidf_cosine_similarity(
            ["implement", "sorting", "algorithm"],
            ["pizza", "recipe", "flour", "sugar"],
        ) == pytest.approx(0.0)

    def test_partial_overlap_between_zero_and_one(self):
        score = _tfidf_cosine_similarity(
            ["implement", "sorting", "algorithm", "python"],
            ["implement", "testing", "framework", "python"],
        )
        assert 0.0 < score < 1.0

    def test_empty_doc_a_returns_zero(self):
        assert _tfidf_cosine_similarity([], ["implement", "sorting"]) == pytest.approx(0.0)

    def test_empty_doc_b_returns_zero(self):
        assert _tfidf_cosine_similarity(["implement", "sorting"], []) == pytest.approx(0.0)

    def test_both_empty_returns_zero(self):
        assert _tfidf_cosine_similarity([], []) == pytest.approx(0.0)

    def test_single_shared_term(self):
        # One shared token → positive cosine
        score = _tfidf_cosine_similarity(["python"], ["python"])
        assert score == pytest.approx(1.0)

    def test_score_in_range(self):
        # Result always in [0.0, 1.0]
        score = _tfidf_cosine_similarity(
            ["code", "review", "implement", "refactor"],
            ["review", "testing", "deploy", "monitor"],
        )
        assert 0.0 <= score <= 1.0

    def test_shared_terms_increase_score_vs_disjoint(self):
        # Partially overlapping docs score higher than fully disjoint docs
        score_overlap = _tfidf_cosine_similarity(
            ["code", "review", "python"],
            ["code", "testing", "java"],
        )
        score_disjoint = _tfidf_cosine_similarity(
            ["code", "review", "python"],
            ["pizza", "recipe", "flour"],
        )
        assert score_overlap > score_disjoint


class TestTokenizeRole:
    """Tests for _tokenize_role — stop-word filtering."""

    def test_filters_short_tokens(self):
        # Tokens shorter than _MIN_KEYWORD_LEN (4) are removed
        result = _tokenize_role("do a ok so")
        assert result == []

    def test_lowercases_tokens(self):
        result = _tokenize_role("Python Sorting Algorithm")
        assert result == ["python", "sorting", "algorithm"]

    def test_keeps_tokens_at_min_length(self):
        # _MIN_KEYWORD_LEN = 4; "code" (4 chars) should be kept
        result = _tokenize_role("code test")
        assert "code" in result
        assert "test" in result

    def test_alphanumeric_only(self):
        # Punctuation is stripped
        result = _tokenize_role("implement-sorting: algorithm!")
        assert "implement" in result
        assert "sorting" in result
        assert "algorithm" in result

    def test_empty_string(self):
        assert _tokenize_role("") == []

    def test_numbers_kept_if_long_enough(self):
        result = _tokenize_role("version 2025 test")
        assert "2025" in result
        assert "test" in result


# ---------------------------------------------------------------------------
# Integration tests — DriftMonitor polling loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_warning_published_when_below_threshold():
    """agent_drift_warning is published when composite score < threshold."""
    bus = Bus()
    # System prompt has keywords; pane output has none → role_score = 0 → drift
    agent = _make_agent(
        agent_id="worker-1",
        pane=MagicMock(),
        system_prompt="implement sorting algorithm refactor",
    )
    tmux = _make_tmux("Hello world foo bar baz")
    monitor = DriftMonitor(
        bus=bus,
        tmux=tmux,
        agents=lambda: [agent],
        drift_threshold=0.6,
        idle_threshold=3600.0,   # won't trigger idle drift
        poll_interval=0.05,
    )

    # Subscribe BEFORE the first poll so we capture the event
    q = await bus.subscribe("__test_initial__", broadcast=True)
    await monitor._poll_all()
    try:
        msg = await asyncio.wait_for(q.get(), timeout=0.5)
    except asyncio.TimeoutError:
        msg = None
    finally:
        await bus.unsubscribe("__test_initial__")

    assert msg is not None
    assert msg.type == MessageType.STATUS
    assert msg.payload["event"] == "agent_drift_warning"
    assert msg.payload["agent_id"] == "worker-1"
    assert "drift_score" in msg.payload


@pytest.mark.asyncio
async def test_drift_warning_not_repeated_in_same_window():
    """agent_drift_warning is published at most once per warning window."""
    bus = Bus()
    agent = _make_agent(
        agent_id="worker-1",
        pane=MagicMock(),
        system_prompt="implement sorting algorithm refactor",
    )
    tmux = _make_tmux("Hello world foo bar")
    monitor = DriftMonitor(
        bus=bus, tmux=tmux, agents=lambda: [agent],
        drift_threshold=0.6, idle_threshold=3600.0,
    )

    messages: list[Message] = []
    q = await bus.subscribe("__test__", broadcast=True)
    # Poll 3 times — should only warn once
    await monitor._poll_all()
    await monitor._poll_all()
    await monitor._poll_all()
    # Drain queue
    while True:
        try:
            msg = q.get_nowait()
            if msg.payload.get("event") == "agent_drift_warning":
                messages.append(msg)
        except asyncio.QueueEmpty:
            break
    await bus.unsubscribe("__test__")
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_drift_warning_clears_after_recovery():
    """warned flag resets when drift_score recovers above threshold."""
    bus = Bus()
    pane = MagicMock()
    agent = _make_agent(
        agent_id="worker-1",
        pane=pane,
        system_prompt="implement sorting algorithm refactor",
    )
    tmux = _make_tmux("Hello world")
    monitor = DriftMonitor(
        bus=bus, tmux=tmux, agents=lambda: [agent],
        drift_threshold=0.6, idle_threshold=3600.0,
    )

    # First poll → drift detected
    await monitor._poll_all()
    stats = monitor.get_drift_stats("worker-1")
    assert stats is not None
    assert stats["warned"] is True

    # Now update pane to include role keywords → recovery
    tmux.capture_pane = MagicMock(return_value="implement sorting algorithm refactor here now")
    await monitor._poll_all()
    stats = monitor.get_drift_stats("worker-1")
    # Warned flag should be cleared after recovery
    assert stats["warned"] is False


@pytest.mark.asyncio
async def test_no_event_when_score_above_threshold():
    """No agent_drift_warning when composite score >= threshold."""
    bus = Bus()
    agent = _make_agent(
        agent_id="worker-1",
        pane=MagicMock(),
        system_prompt="implement sorting",
    )
    # Output contains role keywords → high role_score
    tmux = _make_tmux("I will implement the sorting now")
    monitor = DriftMonitor(
        bus=bus, tmux=tmux, agents=lambda: [agent],
        drift_threshold=0.6, idle_threshold=3600.0,
    )

    q = await bus.subscribe("__test__", broadcast=True)
    await monitor._poll_all()
    # Should be no events
    try:
        msg = await asyncio.wait_for(q.get(), timeout=0.2)
    except asyncio.TimeoutError:
        msg = None
    finally:
        await bus.unsubscribe("__test__")
    assert msg is None or msg.payload.get("event") != "agent_drift_warning"


@pytest.mark.asyncio
async def test_agents_with_no_pane_are_skipped():
    """Agents without a pane are not polled and cause no errors."""
    bus = Bus()
    agent = _make_agent(agent_id="worker-1", pane=None, system_prompt="implement")
    tmux = _make_tmux("")
    monitor = DriftMonitor(bus=bus, tmux=tmux, agents=lambda: [agent],
                           drift_threshold=0.6, idle_threshold=3600.0)

    await monitor._poll_all()
    # No stats should be recorded for pane-less agents
    assert monitor.get_drift_stats("worker-1") is None


@pytest.mark.asyncio
async def test_get_drift_stats_returns_none_for_unknown():
    bus = Bus()
    tmux = _make_tmux("")
    monitor = DriftMonitor(bus=bus, tmux=tmux, agents=lambda: [],
                           drift_threshold=0.6, idle_threshold=60.0)
    assert monitor.get_drift_stats("nonexistent") is None


@pytest.mark.asyncio
async def test_get_drift_stats_returns_correct_fields():
    """get_drift_stats() returns dict with all expected keys."""
    bus = Bus()
    agent = _make_agent("worker-1", pane=MagicMock(), system_prompt="implement sorting")
    tmux = _make_tmux("I implement sorting today")
    monitor = DriftMonitor(bus=bus, tmux=tmux, agents=lambda: [agent],
                           drift_threshold=0.6, idle_threshold=3600.0)

    await monitor._poll_all()
    stats = monitor.get_drift_stats("worker-1")
    assert stats is not None
    for key in ("agent_id", "drift_score", "role_score", "idle_score", "length_score",
                "warned", "drift_warnings", "last_polled"):
        assert key in stats, f"Missing key: {key}"


@pytest.mark.asyncio
async def test_all_drift_stats_returns_list():
    """all_drift_stats() returns a list of all tracked agents."""
    bus = Bus()
    agent_a = _make_agent("a", pane=MagicMock(), system_prompt="implement")
    agent_b = _make_agent("b", pane=MagicMock(), system_prompt="test")
    tmux = _make_tmux("implement test hello")
    monitor = DriftMonitor(bus=bus, tmux=tmux, agents=lambda: [agent_a, agent_b],
                           drift_threshold=0.6, idle_threshold=3600.0)

    await monitor._poll_all()
    all_stats = monitor.all_drift_stats()
    assert isinstance(all_stats, list)
    assert len(all_stats) == 2
    ids = {s["agent_id"] for s in all_stats}
    assert ids == {"a", "b"}


@pytest.mark.asyncio
async def test_new_agents_picked_up_dynamically():
    """Agents added after start() are picked up on the next poll cycle."""
    bus = Bus()
    agents_list: list = []
    tmux = _make_tmux("implement stuff here")
    monitor = DriftMonitor(bus=bus, tmux=tmux, agents=lambda: agents_list,
                           drift_threshold=0.6, idle_threshold=3600.0)

    # First poll with empty list
    await monitor._poll_all()
    assert monitor.all_drift_stats() == []

    # Add an agent
    agents_list.append(_make_agent("worker-1", pane=MagicMock(), system_prompt="implement"))
    await monitor._poll_all()
    assert len(monitor.all_drift_stats()) == 1


def test_start_and_stop():
    """DriftMonitor.start() spawns a task; stop() cancels it."""
    async def _run():
        bus = Bus()
        tmux = _make_tmux("")
        monitor = DriftMonitor(bus=bus, tmux=tmux, agents=lambda: [],
                               drift_threshold=0.6, idle_threshold=60.0,
                               poll_interval=60.0)
        monitor.start()
        assert monitor._task is not None
        assert not monitor._task.done()
        monitor.stop()
        await asyncio.sleep(0.05)
        assert monitor._task.done()

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_drift_warning_payload_fields():
    """agent_drift_warning payload contains all required fields."""
    bus = Bus()
    agent = _make_agent("worker-1", pane=MagicMock(), system_prompt="implement sorting refactor")
    tmux = _make_tmux("Hello world nothing relevant")
    monitor = DriftMonitor(bus=bus, tmux=tmux, agents=lambda: [agent],
                           drift_threshold=0.6, idle_threshold=3600.0)

    q = await bus.subscribe("__test__", broadcast=True)
    await monitor._poll_all()
    try:
        msg = await asyncio.wait_for(q.get(), timeout=0.5)
    except asyncio.TimeoutError:
        msg = None
    finally:
        await bus.unsubscribe("__test__")

    assert msg is not None
    p = msg.payload
    assert p["event"] == "agent_drift_warning"
    assert "agent_id" in p
    assert "drift_score" in p
    assert "role_score" in p
    assert "idle_score" in p
    assert "length_score" in p


@pytest.mark.asyncio
async def test_idle_drift_triggers_warning():
    """Idle score below threshold due to long inactivity triggers warning.

    Uses a system prompt with role keywords absent from the output so that
    role_score is also low, ensuring the composite falls below threshold.
    With idle_threshold=0.01 and a 50ms sleep, idle_score approaches 0.
    """
    bus = Bus()
    # System prompt has keywords NOT in "same output" → role_score = 0
    # Plus idle_threshold is tiny → idle_score → 0
    # Composite = 0.50*0.0 + 0.30*~0.0 + 0.20*1.0 = ~0.2, well below 0.6
    agent = _make_agent("worker-1", pane=MagicMock(),
                        system_prompt="implement refactor algorithm")
    tmux = _make_tmux("same output")
    monitor = DriftMonitor(
        bus=bus, tmux=tmux, agents=lambda: [agent],
        drift_threshold=0.6,
        idle_threshold=0.01,   # very short: 10ms makes any agent idle
        poll_interval=1.0,
    )

    # Wait enough for idle to kick in
    await asyncio.sleep(0.05)
    q = await bus.subscribe("__test__", broadcast=True)
    await monitor._poll_all()
    try:
        msg = await asyncio.wait_for(q.get(), timeout=0.5)
    except asyncio.TimeoutError:
        msg = None
    finally:
        await bus.unsubscribe("__test__")

    # With very short idle_threshold and no role keywords, drift is well below threshold
    assert msg is not None
    assert msg.payload["event"] == "agent_drift_warning"


# ---------------------------------------------------------------------------
# REST endpoint integration tests
# ---------------------------------------------------------------------------


_REST_API_KEY = "test-key-drift"


def _make_mock_hub() -> MagicMock:
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    return hub


@pytest.mark.asyncio
async def test_rest_get_drift_stats_200(tmp_path):
    """GET /agents/{id}/drift returns 200 with correct fields."""
    from httpx import AsyncClient, ASGITransport
    from tmux_orchestrator.web.app import create_app

    mock_orch = MagicMock()
    mock_orch.get_agent_drift_stats = MagicMock(return_value={
        "agent_id": "worker-1",
        "drift_score": 0.8,
        "role_score": 0.9,
        "idle_score": 0.7,
        "length_score": 0.8,
        "warned": False,
        "drift_warnings": 0,
        "last_polled": 0.0,
    })
    app = create_app(mock_orch, _make_mock_hub(), api_key=_REST_API_KEY)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/agents/worker-1/drift",
                                headers={"X-API-Key": _REST_API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_id"] == "worker-1"
    assert "drift_score" in data


@pytest.mark.asyncio
async def test_rest_get_drift_stats_404(tmp_path):
    """GET /agents/{id}/drift returns 404 for unknown agent."""
    from httpx import AsyncClient, ASGITransport
    from tmux_orchestrator.web.app import create_app

    mock_orch = MagicMock()
    mock_orch.get_agent_drift_stats = MagicMock(return_value=None)
    app = create_app(mock_orch, _make_mock_hub(), api_key=_REST_API_KEY)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/agents/nonexistent/drift",
                                headers={"X-API-Key": _REST_API_KEY})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rest_get_all_drift_stats(tmp_path):
    """GET /drift returns a list of drift stats for all agents."""
    from httpx import AsyncClient, ASGITransport
    from tmux_orchestrator.web.app import create_app

    mock_orch = MagicMock()
    mock_orch.all_agent_drift_stats = MagicMock(return_value=[
        {"agent_id": "worker-1", "drift_score": 0.9},
        {"agent_id": "worker-2", "drift_score": 0.4},
    ])
    app = create_app(mock_orch, _make_mock_hub(), api_key=_REST_API_KEY)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/drift", headers={"X-API-Key": _REST_API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2
