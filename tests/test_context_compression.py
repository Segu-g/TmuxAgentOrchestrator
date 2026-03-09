"""Tests for TfIdfContextCompressor (application/context_compression.py).

Covers:
- Pure TF-IDF / cosine-similarity helpers
- CompressionResult fields and invariants
- Drop percentile behaviour
- Structural-line preservation (short lines kept unconditionally)
- Reorder mode (highest-scoring lines first)
- Edge cases: empty text, single line, all-short lines, zero drop_percentile
- REST endpoint POST /agents/{id}/compress-context
- REST endpoint GET /agents/{id}/compression-stats

DESIGN.md §10.36 v1.1.11
"""

from __future__ import annotations

import math
import pytest

from tmux_orchestrator.application.context_compression import (
    CompressionResult,
    TfIdfContextCompressor,
    _cosine_similarity,
    _build_tfidf_matrix,
    _percentile_threshold,
    _tokenize,
    _DEFAULT_DROP_PERCENTILE,
)


# ---------------------------------------------------------------------------
# Unit tests — pure helpers
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_basic(self):
        assert _tokenize("Hello World!") == ["hello", "world"]

    def test_numbers_included(self):
        assert "123" in _tokenize("task 123 running")

    def test_empty_string(self):
        assert _tokenize("") == []

    def test_only_punctuation(self):
        assert _tokenize("!!! --- ~~~") == []

    def test_mixed_case_lowered(self):
        tokens = _tokenize("FizzBuzz fIZZbUZZ")
        assert all(t == t.lower() for t in tokens)


class TestBuildTfidfMatrix:
    def test_empty_documents(self):
        vocab, vectors = _build_tfidf_matrix([])
        assert vocab == []
        assert vectors == []

    def test_single_doc(self):
        _, vectors = _build_tfidf_matrix([["hello", "world"]])
        assert len(vectors) == 1
        assert "hello" in vectors[0]
        assert "world" in vectors[0]

    def test_weights_positive(self):
        _, vectors = _build_tfidf_matrix([["foo", "bar"], ["baz", "foo"]])
        for vec in vectors:
            for w in vec.values():
                assert w > 0

    def test_common_term_lower_weight(self):
        """A term appearing in both docs should have lower IDF than a unique term."""
        docs = [["shared", "unique_a"], ["shared", "unique_b"]]
        _, vectors = _build_tfidf_matrix(docs)
        # 'shared' appears in both; idf is lower → weight is lower than unique term
        assert vectors[0]["shared"] < vectors[0]["unique_a"]
        assert vectors[1]["shared"] < vectors[1]["unique_b"]

    def test_returns_parallel_lists(self):
        docs = [["a", "b"], ["c", "d"], ["e"]]
        vocab, vectors = _build_tfidf_matrix(docs)
        assert len(vectors) == len(docs)


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = {"a": 1.0, "b": 2.0}
        assert math.isclose(_cosine_similarity(v, v), 1.0, abs_tol=1e-9)

    def test_orthogonal_vectors(self):
        v1 = {"a": 1.0}
        v2 = {"b": 1.0}
        assert _cosine_similarity(v1, v2) == 0.0

    def test_zero_vector_a(self):
        assert _cosine_similarity({}, {"a": 1.0}) == 0.0

    def test_zero_vector_b(self):
        assert _cosine_similarity({"a": 1.0}, {}) == 0.0

    def test_partial_overlap(self):
        v1 = {"a": 1.0, "b": 1.0}
        v2 = {"a": 1.0, "c": 1.0}
        sim = _cosine_similarity(v1, v2)
        assert 0.0 < sim < 1.0

    def test_result_in_01(self):
        v1 = {"x": 0.5, "y": 0.3}
        v2 = {"x": 0.2, "z": 0.9}
        sim = _cosine_similarity(v1, v2)
        assert 0.0 <= sim <= 1.0


class TestPercentileThreshold:
    def test_empty(self):
        assert _percentile_threshold([], 0.4) == 0.0

    def test_single_element(self):
        assert _percentile_threshold([0.5], 0.4) == 0.5

    def test_p0(self):
        assert _percentile_threshold([1.0, 2.0, 3.0], 0.0) == 1.0

    def test_p1(self):
        assert _percentile_threshold([1.0, 2.0, 3.0], 1.0) == 3.0

    def test_p50(self):
        result = _percentile_threshold([0.0, 1.0, 2.0], 0.5)
        assert math.isclose(result, 1.0, abs_tol=1e-9)

    def test_interpolation(self):
        # scores [0, 4], p=0.25 → 1.0 by linear interp
        result = _percentile_threshold([0.0, 4.0], 0.25)
        assert math.isclose(result, 1.0, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# Unit tests — TfIdfContextCompressor
# ---------------------------------------------------------------------------


class TestTfIdfContextCompressor:
    # --- constructor validation ---

    def test_invalid_drop_percentile_negative(self):
        with pytest.raises(ValueError):
            TfIdfContextCompressor(drop_percentile=-0.1)

    def test_invalid_drop_percentile_one(self):
        with pytest.raises(ValueError):
            TfIdfContextCompressor(drop_percentile=1.0)

    def test_valid_drop_percentile_zero(self):
        c = TfIdfContextCompressor(drop_percentile=0.0)
        assert c._drop_percentile == 0.0

    # --- zero drop_percentile (no-op) ---

    def test_zero_drop_percentile_returns_original(self):
        c = TfIdfContextCompressor(drop_percentile=0.0)
        text = "line one\nline two\nline three"
        result = c.compress(text, query="line one")
        assert result.compressed_text == text
        assert result.dropped_lines == 0
        assert result.kept_lines == result.original_lines

    # --- empty / trivial input ---

    def test_empty_text(self):
        c = TfIdfContextCompressor()
        result = c.compress("", query="python")
        assert result.original_lines == 0
        assert result.compressed_text == ""

    def test_single_line_kept(self):
        c = TfIdfContextCompressor(drop_percentile=0.5)
        result = c.compress("only one meaningful line here", query="meaningful")
        # Single content line — must not be dropped (would leave nothing)
        assert result.kept_lines >= 1

    def test_all_short_lines_kept(self):
        """All-structural lines must be returned intact."""
        c = TfIdfContextCompressor(drop_percentile=0.5)
        text = "❯\n\n❯\n\n"
        result = c.compress(text, query="python fizzbuzz")
        assert result.dropped_lines == 0

    # --- basic compression ---

    def test_drops_irrelevant_lines(self):
        """Lines unrelated to the query should be preferred for removal."""
        c = TfIdfContextCompressor(drop_percentile=0.40)
        text = (
            "implement fizzbuzz in python using a loop\n"
            "the quick brown fox jumps over the lazy dog\n"
            "fizzbuzz returns fizz for multiples of three\n"
            "random unrelated chatter about unrelated things\n"
            "fizzbuzz returns buzz for multiples of five\n"
            "another off topic line about something else entirely"
        )
        result = c.compress(text, query="fizzbuzz python loop")
        assert result.dropped_lines > 0
        assert result.kept_lines < result.original_lines
        # fizzbuzz-relevant lines should be in the output more often than not
        assert "fizzbuzz" in result.compressed_text

    def test_result_invariants(self):
        c = TfIdfContextCompressor(drop_percentile=0.30)
        text = "\n".join(f"line {i} content words tokens" for i in range(20))
        result = c.compress(text, query="content tokens")
        assert result.original_lines == 20
        assert result.kept_lines + result.dropped_lines == result.original_lines
        assert result.compressed_chars <= result.original_chars
        assert result.drop_percentile == 0.30

    def test_compression_ratio_helper(self):
        c = TfIdfContextCompressor(drop_percentile=0.30)
        text = "\n".join(f"irrelevant words line number {i}" for i in range(20))
        result = c.compress(text, query="python code implementation")
        ratio = c.compression_ratio(result)
        assert 0.0 <= ratio <= 1.0

    def test_compression_ratio_zero_drop(self):
        c = TfIdfContextCompressor(drop_percentile=0.0)
        result = c.compress("hello world", query="hello")
        assert c.compression_ratio(result) == 0.0

    def test_no_duplicate_lines(self):
        """No line should appear twice in the output."""
        c = TfIdfContextCompressor(drop_percentile=0.40)
        text = "\n".join([
            "implement a function for fizzbuzz",
            "some random unrelated line about cats",
            "fizzbuzz should return fizz for three",
            "dogs are nice but unrelated to the task",
            "return buzz for multiples of five",
        ])
        result = c.compress(text, query="fizzbuzz")
        output_lines = result.compressed_text.splitlines()
        assert len(output_lines) == len(set(output_lines))

    # --- structural line preservation ---

    def test_blank_lines_always_kept(self):
        c = TfIdfContextCompressor(drop_percentile=0.80, min_line_tokens=3)
        text = "\n".join([
            "fizzbuzz line about python code implementation",
            "",
            "another fizzbuzz python line here",
            "",
            "third meaningful line about fizzbuzz python",
        ])
        result = c.compress(text, query="fizzbuzz python code")
        # Blank lines are structural (<3 tokens) and must be in output
        assert "" in result.compressed_text.splitlines()

    def test_short_prompt_lines_kept(self):
        """Shell prompts and dividers with <3 tokens must survive."""
        c = TfIdfContextCompressor(drop_percentile=0.90)
        text = "❯\nsome important line about python fizzbuzz implementation\n❯"
        result = c.compress(text, query="fizzbuzz implementation")
        output_lines = result.compressed_text.splitlines()
        assert "❯" in output_lines

    # --- reorder mode ---

    def test_reorder_false_preserves_order(self):
        c = TfIdfContextCompressor(drop_percentile=0.20, reorder=False)
        lines = [
            "alpha beta gamma delta epsilon important content",
            "zeta eta theta iota kappa significant result",
            "lambda mu nu xi omicron another important thing",
        ]
        text = "\n".join(lines)
        result = c.compress(text, query="important content result")
        output_lines = result.compressed_text.splitlines()
        # Relative order of kept lines must match original order
        indices = [lines.index(l) for l in output_lines if l in lines]
        assert indices == sorted(indices)
        assert result.reordered is False

    def test_reorder_true_sets_flag(self):
        c = TfIdfContextCompressor(drop_percentile=0.20, reorder=True)
        text = "\n".join([
            "fizzbuzz python implementation important line",
            "unrelated random words dog cat",
            "fizzbuzz returns fizz python loop",
        ])
        result = c.compress(text, query="fizzbuzz python implementation")
        assert result.reordered is True

    def test_reorder_true_highest_score_first(self):
        """After reorder, scores should be in non-increasing order."""
        c = TfIdfContextCompressor(drop_percentile=0.0, reorder=True)
        text = "\n".join([
            "completely unrelated words banana apple orange mango",
            "fizzbuzz python code implementation function loop",
            "another unrelated sentence about weather clouds",
            "fizzbuzz returns fizz for multiples of three python",
        ])
        result = c.compress(text, query="fizzbuzz python code implementation")
        # Scores for kept content lines should be in descending order
        scores = result.scores
        assert scores == sorted(scores, reverse=True)

    # --- no-query mode ---

    def test_no_query_still_compresses(self):
        c = TfIdfContextCompressor(drop_percentile=0.40)
        text = "\n".join(f"line {i} word word word word word" for i in range(15))
        result = c.compress(text, query="")
        # With no query, all lines have score 0 against empty query;
        # no content line is "above the cut" so compressor should keep all
        # (fallback to keep-all when nothing passes the cut)
        assert result.kept_lines >= 1

    # --- large input (performance smoke test) ---

    def test_large_input_completes(self):
        c = TfIdfContextCompressor(drop_percentile=0.40)
        text = "\n".join(
            f"line {i}: implement fizzbuzz or unrelated content here {i * 3}"
            for i in range(500)
        )
        result = c.compress(text, query="fizzbuzz implementation")
        assert result.original_lines == 500
        assert result.kept_lines < result.original_lines

    # --- edge: all lines identical ---

    def test_all_identical_lines_preserved(self):
        c = TfIdfContextCompressor(drop_percentile=0.40)
        text = "\n".join(["same content word word"] * 10)
        result = c.compress(text, query="same content")
        # All lines equal score → cut=max score → nothing above cut → keep all
        assert result.kept_lines >= 1


# ---------------------------------------------------------------------------
# REST endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Build a minimal FastAPI test app with the agents router."""
    from fastapi import FastAPI
    from unittest.mock import MagicMock
    from tmux_orchestrator.web.routers.agents import build_agents_router

    mock_orchestrator = MagicMock()
    mock_auth = lambda: None  # noqa: E731

    # Default: agent not found
    mock_orchestrator.get_agent.return_value = None

    application = FastAPI()
    router = build_agents_router(mock_orchestrator, mock_auth)
    application.include_router(router)
    return application, mock_orchestrator


@pytest.fixture
def client(app):
    from httpx import AsyncClient, ASGITransport
    app_obj, _ = app
    return AsyncClient(transport=ASGITransport(app=app_obj), base_url="http://test")


@pytest.fixture
def mock_agent():
    from unittest.mock import MagicMock
    from tmux_orchestrator.domain.agent import AgentStatus

    agent = MagicMock()
    agent.id = "agent-1"
    agent.status = AgentStatus.IDLE
    agent.worktree_path = None
    agent.started_at = None
    agent.uptime_s = 0.0
    agent.system_prompt = None
    agent.pane = None
    return agent


class TestCompressContextEndpoint:
    @pytest.mark.asyncio
    async def test_compress_context_agent_not_found_returns_404(self, client, app):
        _, mock_orch = app
        mock_orch.get_agent.return_value = None
        async with client as c:
            resp = await c.post(
                "/agents/nonexistent/compress-context",
                json={},  # valid body with defaults
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_compress_context_no_pane_returns_400(self, client, app, mock_agent):
        _, mock_orch = app
        mock_agent.pane = None
        mock_orch.get_agent.return_value = mock_agent
        async with client as c:
            resp = await c.post(
                "/agents/agent-1/compress-context",
                json={"query": "python"},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_compress_context_with_pane_returns_200(self, client, app, mock_agent):
        from unittest.mock import MagicMock, patch
        _, mock_orch = app
        mock_pane = MagicMock()
        mock_agent.pane = mock_pane
        mock_orch.get_agent.return_value = mock_agent

        sample_text = (
            "fizzbuzz python implementation function\n"
            "unrelated random words dog cat mouse\n"
            "fizzbuzz returns fizz for three python loop\n"
        )

        with patch(
            "tmux_orchestrator.web.routers.agents._capture_pane_text",
            return_value=sample_text,
        ) as _mock:
            async with client as c:
                resp = await c.post(
                    "/agents/agent-1/compress-context",
                    json={"query": "fizzbuzz python", "drop_percentile": 0.3},
                )

        assert resp.status_code == 200
        data = resp.json()
        assert "original_lines" in data
        assert "kept_lines" in data
        assert "dropped_lines" in data
        assert "original_chars" in data
        assert "compressed_chars" in data
        assert "drop_percentile" in data
        assert "reordered" in data

    @pytest.mark.asyncio
    async def test_compress_context_invalid_drop_percentile(self, client, app, mock_agent):
        from unittest.mock import MagicMock
        _, mock_orch = app
        mock_agent.pane = MagicMock()
        mock_orch.get_agent.return_value = mock_agent
        async with client as c:
            resp = await c.post(
                "/agents/agent-1/compress-context",
                json={"query": "python", "drop_percentile": 1.5},
            )
        assert resp.status_code == 422  # validation error

    @pytest.mark.asyncio
    async def test_compress_context_default_query(self, client, app, mock_agent):
        from unittest.mock import MagicMock, patch
        _, mock_orch = app
        mock_pane = MagicMock()
        mock_agent.pane = mock_pane
        mock_orch.get_agent.return_value = mock_agent

        sample_text = "\n".join(
            f"line {i} content words tokens here" for i in range(10)
        )
        with patch(
            "tmux_orchestrator.web.routers.agents._capture_pane_text",
            return_value=sample_text,
        ) as _mock:
            async with client as c:
                resp = await c.post(
                    "/agents/agent-1/compress-context",
                    json={},  # no query — uses default ""
                )
        assert resp.status_code == 200


class TestCompressionStatsEndpoint:
    @pytest.mark.asyncio
    async def test_compression_stats_agent_not_found_returns_404(self, client, app):
        _, mock_orch = app
        mock_orch.get_agent.return_value = None
        async with client as c:
            resp = await c.get("/agents/nonexistent/compression-stats")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_compression_stats_no_history_returns_empty(self, client, app, mock_agent):
        _, mock_orch = app
        mock_orch.get_agent.return_value = mock_agent
        # No compression stats stored yet
        if not hasattr(mock_orch, "_compression_stats"):
            mock_orch._compression_stats = {}
        async with client as c:
            resp = await c.get("/agents/agent-1/compression-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "agent_id" in data
        assert data["agent_id"] == "agent-1"
        assert "total_compressions" in data

    @pytest.mark.asyncio
    async def test_compression_stats_fields(self, client, app, mock_agent):
        _, mock_orch = app
        mock_orch.get_agent.return_value = mock_agent
        async with client as c:
            resp = await c.get("/agents/agent-1/compression-stats")
        assert resp.status_code == 200
        data = resp.json()
        expected_fields = {
            "agent_id",
            "total_compressions",
            "total_lines_dropped",
            "total_chars_saved",
            "avg_compression_ratio",
        }
        assert expected_fields.issubset(data.keys())
