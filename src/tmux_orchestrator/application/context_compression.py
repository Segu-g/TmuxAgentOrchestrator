"""TF-IDF based context compression for agent pane output.

Implements a lightweight extractive context compressor that uses TF-IDF
cosine similarity to rank each line of an agent's pane output against the
current task prompt, discarding the lowest-scoring lines while preserving
semantically relevant content.

This module has zero external dependencies — TF-IDF and cosine similarity
are implemented using Python stdlib (``math``, ``collections``) only.

Design rationale
----------------
Liu et al. "Lost in the Middle" (TACL 2024) demonstrated that LLMs
systematically ignore information in the middle of long contexts due to the
recency + primacy bias introduced by Rotary Position Embedding.  Removing
low-relevance lines alleviates this effect by shrinking the context to
the most salient content, which the LLM can then attend to reliably.

JetBrains Research "The Complexity Trap" (NeurIPS DL4Code 2025) showed
that simple observation masking (dropping low-relevance observations) can
achieve ~50 % cost reduction without significant accuracy loss — on par
with or exceeding LLM-Summarization approaches.

Our implementation follows the extractive path:
- Lines are treated as independent documents.
- A TF-IDF matrix is built from all lines plus the task query.
- Each line receives a cosine similarity score vs. the query vector.
- Lines with score below a configurable percentile cut are removed.
- Optionally, surviving lines are reordered so high-scoring content
  appears first, further mitigating the "Lost in the Middle" effect.

References
----------
- Liu et al. "Lost in the Middle: How Language Models Use Long Contexts."
  TACL 2024. https://aclanthology.org/2024.tacl-1.9/
- Lindenbauer et al. "The Complexity Trap." NeurIPS DL4Code Workshop 2025.
  https://github.com/JetBrains-Research/the-complexity-trap
- Mishra, Varun. "Mastering Extractive Summarization: TF-IDF and TextRank."
  Medium 2024.
  https://medium.com/@varun_mishra/text-summarization-with-tf-idf-and-textrank-a-deep-dive-into-the-code-and-theory-4cc76c285e28
- DESIGN.md §10.36 v1.1.11
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default fraction of lines to drop (lines whose score is in the bottom
# ``drop_percentile`` are removed).
_DEFAULT_DROP_PERCENTILE: float = 0.40

# Lines shorter than this token count are treated as structural (prompts,
# dividers, blank lines) and kept unconditionally.
_MIN_LINE_TOKENS: int = 3

# Regex for tokenisation — keeps alphanumeric sequences, lowercased.
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class CompressionResult:
    """Outcome of a single compression pass.

    Attributes
    ----------
    original_lines:
        Total number of lines in the input text.
    kept_lines:
        Number of lines retained after compression.
    dropped_lines:
        Number of lines removed.
    original_chars:
        Character count of the input text.
    compressed_chars:
        Character count of the output text.
    compressed_text:
        The compressed output string (lines joined by newline).
    scores:
        Per-line cosine similarity scores (parallel to the *kept* lines
        after filtering, not the original sequence).  Useful for debugging.
    drop_percentile:
        The percentile threshold that was applied.
    reordered:
        Whether lines were reordered by score (highest first).
    """

    original_lines: int
    kept_lines: int
    dropped_lines: int
    original_chars: int
    compressed_chars: int
    compressed_text: str
    scores: list[float] = field(default_factory=list)
    drop_percentile: float = _DEFAULT_DROP_PERCENTILE
    reordered: bool = False


# ---------------------------------------------------------------------------
# Pure-stdlib TF-IDF helpers
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    """Return lowercase alphanumeric tokens from *text*."""
    return _TOKEN_RE.findall(text.lower())


def _build_tfidf_matrix(
    documents: list[list[str]],
) -> tuple[list[str], list[dict[str, float]]]:
    """Compute TF-IDF vectors for a list of tokenised documents.

    Parameters
    ----------
    documents:
        Each element is a list of tokens (one document / line).

    Returns
    -------
    vocab:
        Sorted list of all unique terms.
    vectors:
        One dict per document mapping term → TF-IDF weight.
    """
    n_docs = len(documents)
    if n_docs == 0:
        return [], []

    # Collect all unique terms
    vocab: set[str] = set()
    for doc in documents:
        vocab.update(doc)
    term_list = sorted(vocab)

    # Document frequency: how many documents contain each term
    df: dict[str, int] = Counter()
    for doc in documents:
        for term in set(doc):
            df[term] += 1

    vectors: list[dict[str, float]] = []
    for doc in documents:
        tf = Counter(doc)
        n_tokens = max(len(doc), 1)
        vec: dict[str, float] = {}
        for term in set(doc):
            # TF: normalised term frequency
            tf_score = tf[term] / n_tokens
            # IDF: smoothed log IDF
            idf_score = math.log((1 + n_docs) / (1 + df[term])) + 1.0
            vec[term] = tf_score * idf_score
        vectors.append(vec)

    return term_list, vectors


def _cosine_similarity(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    """Compute cosine similarity between two TF-IDF sparse vectors."""
    dot = sum(vec_a.get(term, 0.0) * val for term, val in vec_b.items())
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _percentile_threshold(scores: list[float], pct: float) -> float:
    """Return the value at the given percentile (0.0–1.0) in *scores*.

    Uses linear interpolation (same as numpy.percentile with
    interpolation='linear').  Returns 0.0 for empty input.
    """
    if not scores:
        return 0.0
    sorted_scores = sorted(scores)
    n = len(sorted_scores)
    # Index in [0, n-1]
    idx = pct * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_scores[lo] * (1 - frac) + sorted_scores[hi] * frac


# ---------------------------------------------------------------------------
# Main compressor
# ---------------------------------------------------------------------------


class TfIdfContextCompressor:
    """Extractive context compressor based on TF-IDF cosine similarity.

    Usage
    -----
    ::

        compressor = TfIdfContextCompressor(drop_percentile=0.40)
        result = compressor.compress(pane_text, query="implement fizzbuzz in Python")
        print(result.compressed_text)

    Parameters
    ----------
    drop_percentile:
        Fraction of lines (by ascending relevance score) to discard.
        Default 0.40 — removes the bottom 40 % of lines by relevance.
        Set to 0.0 to keep all lines (no-op mode).
    reorder:
        When ``True``, surviving lines are reordered so that
        highest-scoring (most relevant) lines appear first.
        This mitigates the "Lost in the Middle" effect described by
        Liu et al. (TACL 2024).  Default ``False`` (preserve order).
    min_line_tokens:
        Lines with fewer tokens than this are kept unconditionally (e.g.
        shell prompts, blank lines, section dividers).  Default 3.
    """

    def __init__(
        self,
        *,
        drop_percentile: float = _DEFAULT_DROP_PERCENTILE,
        reorder: bool = False,
        min_line_tokens: int = _MIN_LINE_TOKENS,
    ) -> None:
        if not 0.0 <= drop_percentile < 1.0:
            raise ValueError(
                f"drop_percentile must be in [0, 1); got {drop_percentile!r}"
            )
        self._drop_percentile = drop_percentile
        self._reorder = reorder
        self._min_line_tokens = min_line_tokens

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compress(self, text: str, query: str = "") -> CompressionResult:
        """Compress *text* by removing lines with low relevance to *query*.

        Parameters
        ----------
        text:
            Multi-line string to compress (typically agent pane output).
        query:
            Task prompt or reference text.  Lines most similar to this
            query are retained.  If empty, all lines are scored equally
            (uniform random selection — compression still occurs but is
            query-agnostic).

        Returns
        -------
        CompressionResult
            Detailed statistics and the compressed text.
        """
        lines = text.splitlines()
        original_lines = len(lines)
        original_chars = len(text)

        if not lines or self._drop_percentile == 0.0:
            return CompressionResult(
                original_lines=original_lines,
                kept_lines=original_lines,
                dropped_lines=0,
                original_chars=original_chars,
                compressed_chars=original_chars,
                compressed_text=text,
                scores=[],
                drop_percentile=self._drop_percentile,
                reordered=False,
            )

        # Separate "structural" lines (kept unconditionally)
        # from "content" lines (subject to scoring).
        structural_indices: set[int] = set()
        content_indices: list[int] = []
        for i, line in enumerate(lines):
            tokens = _tokenize(line)
            if len(tokens) < self._min_line_tokens:
                structural_indices.add(i)
            else:
                content_indices.append(i)

        if not content_indices:
            # Nothing to score — return as-is
            return CompressionResult(
                original_lines=original_lines,
                kept_lines=original_lines,
                dropped_lines=0,
                original_chars=original_chars,
                compressed_chars=original_chars,
                compressed_text=text,
                scores=[],
                drop_percentile=self._drop_percentile,
                reordered=False,
            )

        # Build TF-IDF matrix: content lines + query as last document
        query_tokens = _tokenize(query) if query else []
        content_docs: list[list[str]] = [
            _tokenize(lines[i]) for i in content_indices
        ]
        all_docs = content_docs + [query_tokens]
        _, vectors = _build_tfidf_matrix(all_docs)

        query_vec = vectors[-1]  # last document is the query
        line_scores: list[tuple[int, float]] = []  # (original_idx, score)
        for j, idx in enumerate(content_indices):
            sim = _cosine_similarity(vectors[j], query_vec)
            line_scores.append((idx, sim))

        # Determine cut-off score
        score_values = [s for _, s in line_scores]
        cut = _percentile_threshold(score_values, self._drop_percentile)

        # Keep lines above the cut (or equal, to avoid keeping nothing)
        kept_content: list[tuple[int, float]] = [
            (idx, score) for idx, score in line_scores if score > cut
        ]
        # If all lines would be dropped (all equal score), keep all
        if not kept_content:
            kept_content = list(line_scores)

        kept_content_indices: set[int] = {idx for idx, _ in kept_content}

        # Assemble final line list
        if self._reorder:
            # Structural lines first (preserving order), then content sorted desc
            structural_lines = [lines[i] for i in sorted(structural_indices)]
            content_sorted = sorted(kept_content, key=lambda t: t[1], reverse=True)
            reordered_content = [lines[idx] for idx, _ in content_sorted]
            result_lines = structural_lines + reordered_content
            kept_scores = [score for _, score in content_sorted]
        else:
            # Preserve original order
            result_lines = [
                lines[i]
                for i in range(original_lines)
                if i in structural_indices or i in kept_content_indices
            ]
            kept_scores = [
                score
                for idx, score in line_scores
                if idx in kept_content_indices
            ]

        compressed_text = "\n".join(result_lines)
        kept_count = len(result_lines)
        dropped_count = original_lines - kept_count

        logger.debug(
            "TfIdfContextCompressor: %d → %d lines (dropped %d, cut=%.4f, "
            "query=%r, reorder=%s)",
            original_lines,
            kept_count,
            dropped_count,
            cut,
            (query[:40] + "...") if len(query) > 40 else query,
            self._reorder,
        )

        return CompressionResult(
            original_lines=original_lines,
            kept_lines=kept_count,
            dropped_lines=dropped_count,
            original_chars=original_chars,
            compressed_chars=len(compressed_text),
            compressed_text=compressed_text,
            scores=kept_scores,
            drop_percentile=self._drop_percentile,
            reordered=self._reorder,
        )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def compression_ratio(self, result: CompressionResult) -> float:
        """Return char-level compression ratio (0.0 = no compression, 1.0 = all removed)."""
        if result.original_chars == 0:
            return 0.0
        return 1.0 - result.compressed_chars / result.original_chars
