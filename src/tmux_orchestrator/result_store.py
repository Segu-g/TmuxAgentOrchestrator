"""Append-only JSONL result store for task result persistence.

Design: Event Sourcing pattern (Fowler, 2005) — all task completions are
recorded as immutable facts in an append-only log.  Any derived state
(e.g., per-agent summaries) can be reproduced by replaying the log.

CQRS (Greg Young) separates writes (append) from reads (query by
agent/task/date), ensuring the write path is always a simple, low-latency
file append while reads can be arbitrarily complex queries over the log.

Datomic "value of values" (Hickey, 2012) — each result record is an
immutable, time-stamped fact.  Nothing is ever updated in-place.

Thread safety: A single ``threading.Lock`` serialises all ``append()``
calls so concurrent completions from different agent tasks do not produce
interleaved (corrupted) JSONL lines.

File layout:
    {store_dir}/{session_name}/{YYYY-MM-DD}.jsonl

Each line is a JSON object:
    {"task_id": ..., "agent_id": ..., "prompt": ..., "result_text": ...,
     "error": null, "duration_s": 1.23, "ts": "2026-03-05T12:00:00+00:00"}

References:
- Martin Fowler "Event Sourcing" (2005) https://martinfowler.com/eaa.html
- Greg Young "CQRS Documents" (2010) https://cqrs.files.wordpress.com/2010/11/cqrs_documents.pdf
- Rich Hickey "The Value of Values" (Datomic, 2012)
  https://www.infoq.com/presentations/Value-Values/
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class ResultStore:
    """Append-only JSONL result store.

    Results are written to: ``{store_dir}/{session_name}/{YYYY-MM-DD}.jsonl``
    Each line is a JSON object with fields: task_id, agent_id, prompt,
    result_text, error, duration_s, ts.

    Thread-safe: concurrent ``append()`` calls are serialised with a
    ``threading.Lock`` so no two threads interleave partial JSON writes.
    """

    def __init__(self, store_dir: str | Path, session_name: str) -> None:
        self._store_dir = Path(store_dir).expanduser().resolve()
        self._session_name = session_name
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def _day_file(self, date_str: str) -> Path:
        """Return the JSONL file path for *date_str* (``YYYY-MM-DD``)."""
        session_dir = self._store_dir / self._session_name
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir / f"{date_str}.jsonl"

    def append(
        self,
        *,
        task_id: str,
        agent_id: str,
        prompt: str,
        result_text: str,
        error: Optional[str],
        duration_s: float,
    ) -> None:
        """Append one result record to the current-day JSONL file.

        This method is thread-safe: all writes are serialised with an
        internal ``threading.Lock`` so concurrent callers never produce
        corrupted/interleaved JSON lines.

        Parameters
        ----------
        task_id:
            Unique task identifier (UUID string).
        agent_id:
            ID of the agent that completed the task.
        prompt:
            Short preview of the task prompt (will be stored verbatim).
        result_text:
            Task output text (callers should truncate long outputs before
            passing; this method stores the value as-is).
        error:
            Error message string, or ``None`` on success.
        duration_s:
            Wall-clock seconds from dispatch to result receipt.
        """
        ts = datetime.now(tz=timezone.utc).isoformat()
        date_str = ts[:10]  # "YYYY-MM-DD"
        record = {
            "task_id": task_id,
            "agent_id": agent_id,
            "prompt": prompt,
            "result_text": result_text,
            "error": error,
            "duration_s": duration_s,
            "ts": ts,
        }
        line = json.dumps(record, ensure_ascii=False)
        file_path = self._day_file(date_str)

        with self._lock:
            with file_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    # ------------------------------------------------------------------
    # Read path (CQRS query side)
    # ------------------------------------------------------------------

    def _session_dir(self) -> Path:
        return self._store_dir / self._session_name

    def all_dates(self) -> list[str]:
        """Return a sorted list of date strings that have persisted data.

        Each entry is in ``YYYY-MM-DD`` format.  Dates with empty files are
        excluded.  Returns an empty list when no data has been written yet.
        """
        session_dir = self._session_dir()
        if not session_dir.exists():
            return []
        dates = []
        for f in session_dir.glob("*.jsonl"):
            date_str = f.stem  # filename without extension
            # Only include dates where the file is non-empty.
            if f.stat().st_size > 0:
                dates.append(date_str)
        return sorted(dates)

    def query(
        self,
        *,
        agent_id: str | None = None,
        task_id: str | None = None,
        date: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Query persisted results.

        Filters are applied with AND semantics.  All parameters are optional;
        with no filters the most-recent *limit* results across all dates are
        returned.

        Parameters
        ----------
        agent_id:
            When set, only records from this agent are returned.
        task_id:
            When set, only the record with this task_id is returned.
        date:
            ``YYYY-MM-DD`` string; when set, only records from that date's
            file are scanned.
        limit:
            Maximum number of records to return (oldest-first ordering
            within results, newest dates scanned first so that ``limit``
            captures the most recent results when scanning all dates).

        Returns
        -------
        list[dict]
            List of result records (plain dicts).  At most *limit* entries,
            ordered oldest-first within the returned slice.
        """
        session_dir = self._session_dir()
        if not session_dir.exists():
            return []

        if date is not None:
            dates_to_scan = [date] if (session_dir / f"{date}.jsonl").exists() else []
        else:
            dates_to_scan = sorted(self.all_dates(), reverse=True)

        results: list[dict] = []
        for d in dates_to_scan:
            if len(results) >= limit:
                break
            file_path = session_dir / f"{d}.jsonl"
            if not file_path.exists():
                continue
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Apply filters.
                if agent_id is not None and record.get("agent_id") != agent_id:
                    continue
                if task_id is not None and record.get("task_id") != task_id:
                    continue
                results.append(record)
                if len(results) >= limit:
                    break

        # Dates were scanned newest-first so results are newest-first.
        # Reverse to present oldest-first (chronological order).
        results.reverse()
        return results
