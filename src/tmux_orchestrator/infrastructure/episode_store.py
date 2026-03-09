"""MIRIX-inspired episodic memory store for per-agent task episodes.

Design: Each agent accumulates a JSONL episode log at:
    {mailbox_root}/{session_name}/{agent_id}/episodes.jsonl

Each line is a JSON object representing one completed task episode:
    {"id": "<uuid>", "agent_id": "...", "task_id": "...",
     "summary": "...", "outcome": "success|failure|partial",
     "lessons": "...", "created_at": "<ISO 8601>"}

Motivation (MIRIX, arXiv:2507.07957, Wang & Chen 2025):
    Episodic memory captures time-stamped situational experiences and achieves
    35% higher accuracy than a RAG baseline on the ScreenshotVQA benchmark
    while reducing storage by 99.9%.  The key insight is that *cumulative
    append-only* storage (never overwrite) combined with *recency-first*
    retrieval gives long-lived agents persistent recall across task sessions.

    "Position: Episodic Memory is the Missing Piece for Long-Term LLM Agents"
    (arXiv:2502.06975, 2025) argues that single-shot learning of instances and
    context-aware retrieval are the two capabilities most lacking in current LLM
    agent memory systems.  A lightweight JSONL log satisfies both: individual
    episodes are never aggregated away, and retrieval returns the N most recent
    entries ordered by creation time.

Contrast with NOTES.md:
    NOTES.md (written by /summarize) is a *single-file summary* that is
    overwritten each time.  The episode log is *append-only* so prior episodes
    are never lost — the canonical distinction between episodic and semantic
    memory in cognitive-science terms (Tulving, 1972).

Thread safety:
    A ``threading.Lock`` serialises all ``append()`` calls.  ``list()`` and
    ``delete()`` build a transactional snapshot by reading all lines and then
    atomically rewriting the file (delete case).

References:
- Wang & Chen, "MIRIX: Multi-Agent Memory System for LLM-Based Agents",
  arXiv:2507.07957, July 2025. https://arxiv.org/abs/2507.07957
- "Position: Episodic Memory is the Missing Piece for Long-Term LLM Agents",
  arXiv:2502.06975, 2025. https://arxiv.org/pdf/2502.06975
- Tulving, E. (1972). "Episodic and Semantic Memory" in *Organization of
  Memory*, Academic Press.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class EpisodeNotFoundError(KeyError):
    """Raised when an episode ID does not exist in the store."""


class EpisodeStore:
    """Per-agent JSONL episodic memory store.

    Layout on disk::

        {root_dir}/{session_name}/{agent_id}/episodes.jsonl

    Each line is a complete JSON object (one episode).  Lines are appended
    in creation order; ``list()`` returns episodes newest-first.

    Thread-safe: all writes are serialised with a ``threading.Lock``.
    """

    def __init__(self, root_dir: str | Path, session_name: str) -> None:
        self._root = Path(root_dir).expanduser().resolve() / session_name
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _agent_dir(self, agent_id: str) -> Path:
        return self._root / agent_id

    def _episodes_file(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / "episodes.jsonl"

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def append(
        self,
        agent_id: str,
        *,
        summary: str,
        outcome: str,
        lessons: str = "",
        task_id: Optional[str] = None,
        episode_id: Optional[str] = None,
    ) -> dict:
        """Append a new episode to *agent_id*'s log and return the record.

        Parameters
        ----------
        agent_id:
            The agent whose memory receives this episode.
        summary:
            1–2 sentence description of what was accomplished.
        outcome:
            One of ``"success"``, ``"failure"``, or ``"partial"``.
        lessons:
            Free-text knowledge to carry forward to the next task (optional).
        task_id:
            The task ID that produced this episode (optional, for correlation).
        episode_id:
            Override the auto-generated UUID (useful for tests).
        """
        ep_id = episode_id or str(uuid.uuid4())
        created_at = datetime.now(tz=timezone.utc).isoformat()
        record = {
            "id": ep_id,
            "agent_id": agent_id,
            "task_id": task_id,
            "summary": summary,
            "outcome": outcome,
            "lessons": lessons,
            "created_at": created_at,
        }
        line = json.dumps(record, ensure_ascii=False)
        agent_dir = self._agent_dir(agent_id)

        with self._lock:
            agent_dir.mkdir(parents=True, exist_ok=True)
            with self._episodes_file(agent_id).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

        return record

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def _read_all(self, agent_id: str) -> list[dict]:
        """Return all valid episode records from disk (insertion order)."""
        path = self._episodes_file(agent_id)
        if not path.exists():
            return []
        records: list[dict] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def list(self, agent_id: str, *, limit: int = 20) -> list[dict]:
        """Return the most recent *limit* episodes for *agent_id*, newest-first.

        Parameters
        ----------
        agent_id:
            Agent whose episode log to query.
        limit:
            Maximum number of records to return (default 20, no hard cap here;
            callers may clamp as needed).

        Returns
        -------
        list[dict]
            Episodes ordered newest-first (reverse insertion order).
        """
        records = self._read_all(agent_id)
        # Reverse so newest comes first, then slice.
        return list(reversed(records))[:limit]

    def get(self, agent_id: str, episode_id: str) -> dict:
        """Return a single episode by ID.

        Raises :class:`EpisodeNotFoundError` when the episode does not exist.
        """
        for record in self._read_all(agent_id):
            if record.get("id") == episode_id:
                return record
        raise EpisodeNotFoundError(episode_id)

    # ------------------------------------------------------------------
    # Delete path
    # ------------------------------------------------------------------

    def delete(self, agent_id: str, episode_id: str) -> None:
        """Delete the episode with *episode_id* from *agent_id*'s log.

        Rewrites the JSONL file atomically (read all → filter → write back).

        Raises :class:`EpisodeNotFoundError` when the ID is not found.
        """
        with self._lock:
            path = self._episodes_file(agent_id)
            records = self._read_all(agent_id)
            original_len = len(records)
            filtered = [r for r in records if r.get("id") != episode_id]
            if len(filtered) == original_len:
                raise EpisodeNotFoundError(episode_id)
            # Rewrite
            path.parent.mkdir(parents=True, exist_ok=True)
            content = "\n".join(
                json.dumps(r, ensure_ascii=False) for r in filtered
            )
            if content:
                content += "\n"
            path.write_text(content, encoding="utf-8")

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def has_agent(self, agent_id: str) -> bool:
        """Return True if *agent_id* has an episodes file (possibly empty)."""
        return self._episodes_file(agent_id).exists()
