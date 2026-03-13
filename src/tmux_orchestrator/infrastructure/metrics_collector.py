"""Lightweight time-series metrics collector for TmuxAgentOrchestrator.

Collects agent and queue metrics at regular intervals into a fixed-capacity
ring buffer (``collections.deque``).  The collector is intentionally minimal:
no external dependencies, pure asyncio, getter-injection for testability.

Design references:
- Circular buffer ring design: https://ssojet.com/data-structures/implement-circular-buffer-in-python
- Time-series dense vs sparse: https://www.enbnt.dev/posts/timeseries-dense-sparse-circle/
- "Beyond Black-Box Benchmarking" arXiv:2503.06745 (Galileo 2025) — agent observability
- Prometheus metrics collection patterns: https://prometheus.io/docs/prometheus/latest/querying/api/
- DESIGN.md §10.92 (v1.2.16)
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class MetricsSnapshot:
    """A point-in-time snapshot of orchestrator metrics.

    Fields
    ------
    timestamp:
        ISO-8601 UTC string when this snapshot was taken.
    queue_depth:
        Number of tasks currently waiting in the pending queue.
    active_agents:
        Number of agents in BUSY state.
    idle_agents:
        Number of agents in IDLE state.
    tasks_completed_total:
        Cumulative count of tasks completed successfully across all agents.
    tasks_failed_total:
        Cumulative count of tasks that completed with an error across all agents.
    per_agent:
        Per-agent detail dict, keyed by agent_id.  Each value is a dict with
        at minimum ``{"status": str}``.  Additional fields (tasks_completed,
        tasks_failed, error_rate) are included when history data is available.
    """

    timestamp: str
    queue_depth: int
    active_agents: int
    idle_agents: int
    tasks_completed_total: int
    tasks_failed_total: int
    per_agent: dict[str, dict[str, Any]] = field(default_factory=dict)


class MetricsCollector:
    """Collects and stores orchestrator metrics as a time-series ring buffer.

    Usage (production)::

        collector = MetricsCollector(
            get_queue_depth=orchestrator.queue_size,
            get_agent_statuses=orchestrator.get_all_agent_statuses,
            get_cumulative_stats=orchestrator.get_cumulative_stats,
            max_snapshots=360,  # 1 hour at 10s interval
        )
        await collector.start(interval_s=10.0)
        # ...
        await collector.stop()

    Usage (tests)::

        collector = MetricsCollector(
            get_queue_depth=lambda: 5,
            get_agent_statuses=lambda: {"w1": "IDLE", "w2": "BUSY"},
            get_cumulative_stats=lambda: {"tasks_completed_total": 3, "tasks_failed_total": 0},
            max_snapshots=10,
        )

    The collector calls the getter functions synchronously during each
    ``_collect()`` pass.  If any getter raises, the exception is logged and
    the snapshot is skipped for that interval.

    Design reference: DESIGN.md §10.92 (v1.2.16)
    """

    def __init__(
        self,
        *,
        get_queue_depth: Callable[[], int],
        get_agent_statuses: Callable[[], dict[str, str]],
        get_cumulative_stats: Callable[[], dict[str, int]],
        max_snapshots: int = 360,
    ) -> None:
        self._get_queue_depth = get_queue_depth
        self._get_agent_statuses = get_agent_statuses
        self._get_cumulative_stats = get_cumulative_stats
        self._snapshots: deque[MetricsSnapshot] = deque(maxlen=max_snapshots)
        self._task: asyncio.Task | None = None
        self._interval_s: float = 10.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, interval_s: float = 10.0) -> None:
        """Start the background collection loop at *interval_s* seconds."""
        self._interval_s = interval_s
        self._task = asyncio.create_task(
            self._collect_loop(interval_s),
            name="metrics-collector",
        )
        logger.info("MetricsCollector started (interval=%.1fs)", interval_s)

    async def stop(self) -> None:
        """Cancel the background collection loop."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MetricsCollector stopped")

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def get_snapshots(self, last_n: int | None = None) -> list[MetricsSnapshot]:
        """Return stored snapshots as a list (oldest first).

        Parameters
        ----------
        last_n:
            When provided, return at most the last *last_n* snapshots.
            When ``None`` (default), return all stored snapshots.
        """
        snaps = list(self._snapshots)
        if last_n is not None and last_n > 0:
            return snaps[-last_n:]
        return snaps

    def get_latest(self) -> MetricsSnapshot | None:
        """Return the most recent snapshot, or ``None`` when the buffer is empty."""
        return self._snapshots[-1] if self._snapshots else None

    @property
    def interval_s(self) -> float:
        """The configured collection interval in seconds."""
        return self._interval_s

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _collect_loop(self, interval_s: float) -> None:
        """Background task: collect a snapshot every *interval_s* seconds."""
        while True:
            await asyncio.sleep(interval_s)
            try:
                snapshot = self._collect()
                self._snapshots.append(snapshot)
            except Exception:
                logger.exception("MetricsCollector: error during snapshot collection")

    def _collect(self) -> MetricsSnapshot:
        """Collect a single snapshot synchronously using the injected getters."""
        timestamp = datetime.now(tz=timezone.utc).isoformat()

        queue_depth = self._get_queue_depth()
        agent_statuses: dict[str, str] = self._get_agent_statuses()
        cumulative: dict[str, int] = self._get_cumulative_stats()

        active_agents = sum(
            1 for s in agent_statuses.values() if s.upper() == "BUSY"
        )
        idle_agents = sum(
            1 for s in agent_statuses.values() if s.upper() == "IDLE"
        )

        tasks_completed_total = cumulative.get("tasks_completed_total", 0)
        tasks_failed_total = cumulative.get("tasks_failed_total", 0)

        # Per-agent detail from per-agent sub-stats if provided
        per_agent_stats: dict[str, dict[str, int]] = cumulative.get(  # type: ignore[assignment]
            "per_agent", {}
        )

        per_agent: dict[str, dict[str, Any]] = {}
        for agent_id, status in agent_statuses.items():
            entry: dict[str, Any] = {"status": status}
            if agent_id in per_agent_stats:
                entry.update(per_agent_stats[agent_id])
            per_agent[agent_id] = entry

        return MetricsSnapshot(
            timestamp=timestamp,
            queue_depth=queue_depth,
            active_agents=active_agents,
            idle_agents=idle_agents,
            tasks_completed_total=tasks_completed_total,
            tasks_failed_total=tasks_failed_total,
            per_agent=per_agent,
        )
