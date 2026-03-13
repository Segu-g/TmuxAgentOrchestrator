"""System APIRouter — health, metrics, orchestrator control, results, drift, etc.

Design reference: DESIGN.md §10.42 (v1.1.6)
FastAPI "Bigger Applications": https://fastapi.tiangolo.com/tutorial/bigger-applications/
"""

from __future__ import annotations

import time
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from tmux_orchestrator.config import AgentRole
from tmux_orchestrator.web.schemas import AutoScalerUpdate, RateLimitUpdate


def build_system_router(
    orchestrator: Any,
    auth: Callable,
    metrics_collector: Any = None,
) -> APIRouter:
    """Build and return the system APIRouter.

    Parameters
    ----------
    orchestrator:
        The :class:`~tmux_orchestrator.orchestrator.Orchestrator` instance.
    auth:
        Authentication dependency callable (combined session + API key).
    metrics_collector:
        Optional :class:`~tmux_orchestrator.infrastructure.metrics_collector.MetricsCollector`
        instance.  When ``None``, the ``GET /metrics/time-series`` and
        ``GET /metrics/agents/{id}`` endpoints return disabled stubs.
    """
    router = APIRouter()

    @router.post(
        "/orchestrator/drain",
        summary="Drain all agents — graceful orchestrator shutdown",
        dependencies=[Depends(auth)],
    )
    async def drain_orchestrator() -> dict:
        """Drain all registered agents.
    
        Iterates over every registered agent and calls ``drain_agent()``:
        - IDLE agents are stopped immediately.
        - BUSY agents are marked DRAINING and auto-stopped after their current task.
    
        Response fields:
        - ``draining``: agent IDs that are now draining (were BUSY)
        - ``stopped_immediately``: agent IDs stopped immediately (were IDLE)
        - ``already_stopped``: agent IDs skipped (already STOPPED, ERROR, or DRAINING)
    
        Design reference: DESIGN.md §10.23 (v0.28.0).
        """
        return await orchestrator.drain_all()
    @router.get(
        "/context-stats",
        summary="Context usage stats for all agents",
        dependencies=[Depends(auth)],
    )
    async def all_context_stats() -> list:
        """Return context window usage statistics for all tracked agents.
    
        See ``GET /agents/{id}/stats`` for field descriptions.
    
        Design reference: DESIGN.md §11 (v0.21.0) — エージェントのコンテキスト使用量モニタリング.
        """
        return orchestrator.all_agent_context_stats()
    
    @router.get(
        "/drift",
        summary="Behavioral drift stats for all agents",
        dependencies=[Depends(auth)],
    )
    async def all_drift_stats() -> list:
        """Return behavioral drift statistics for all tracked agents.
    
        See ``GET /agents/{id}/drift`` for field descriptions.
    
        Design reference: Rath arXiv:2601.04170 "Agent Drift" (2026);
        DESIGN.md §10.20 (v1.0.9).
        """
        return orchestrator.all_agent_drift_stats()
    # ------------------------------------------------------------------
    # Task result persistence (Event Sourcing / CQRS read side)
    # ------------------------------------------------------------------
    
    @router.get(
        "/results",
        summary="Query persisted task results",
        dependencies=[Depends(auth)],
    )
    async def query_results(
        agent_id: str | None = None,
        task_id: str | None = None,
        date: str | None = None,
        limit: int = 50,
    ) -> list:
        """Return persisted task results from the append-only JSONL store.
    
        Query parameters are AND-combined:
        - ``agent_id``: filter by the agent that completed the task.
        - ``task_id``: filter to a specific task.
        - ``date``: ``YYYY-MM-DD`` — scan only that day's file.
        - ``limit``: maximum records returned (default 50).
    
        Returns an empty list when ``result_store_enabled=False`` or no
        results have been persisted yet.
    
        Design reference:
        - Martin Fowler "Event Sourcing" (2005): append-only log of facts.
        - Greg Young "CQRS Documents" (2010): separate write/read paths.
        - Rich Hickey "The Value of Values" (Datomic, 2012): immutable facts.
        - DESIGN.md §10.19 (v0.24.0).
        """
        result_store = getattr(orchestrator, "_result_store", None)
        if result_store is None:
            return []
        return result_store.query(
            agent_id=agent_id,
            task_id=task_id,
            date=date,
            limit=limit,
        )
    
    @router.get(
        "/results/dates",
        summary="List dates with persisted result data",
        dependencies=[Depends(auth)],
    )
    async def results_dates() -> list:
        """Return a sorted list of ``YYYY-MM-DD`` date strings for which
        result data exists in the JSONL store.
    
        Returns an empty list when ``result_store_enabled=False`` or no
        results have been persisted yet.
    
        Design reference: DESIGN.md §10.19 (v0.24.0).
        """
        result_store = getattr(orchestrator, "_result_store", None)
        if result_store is None:
            return []
        return result_store.all_dates()
    
    # ------------------------------------------------------------------
    # Workflow DAG API
    # ------------------------------------------------------------------
    @router.post(
        "/orchestrator/pause",
        summary="Pause task dispatch",
        dependencies=[Depends(auth)],
    )
    async def pause_dispatch() -> dict:
        """Pause the orchestrator dispatch loop.
    
        While paused, no new tasks are dequeued from the pending queue.
        In-flight tasks (already dispatched to agents) continue to run
        normally.  New tasks can still be submitted to the queue via
        ``POST /tasks`` — they will be dispatched as soon as dispatch is
        resumed.
    
        Idempotent: calling pause on an already-paused orchestrator is safe.
    
        Design reference: Google Cloud Tasks ``queues.pause`` API; Oracle
        WebLogic Server "Pause queue message operations at runtime" — queue
        pause enables maintenance, rolling deploys, and controlled draining
        without dropping in-flight work.
        DESIGN.md §11 (v0.19.0) — queue pause/resume.
        """
        orchestrator.pause()
        return {"paused": True}
    
    @router.post(
        "/orchestrator/resume",
        summary="Resume task dispatch",
        dependencies=[Depends(auth)],
    )
    async def resume_dispatch() -> dict:
        """Resume the orchestrator dispatch loop after a pause.
    
        Idempotent: calling resume on an already-running orchestrator is safe.
    
        After resuming, the dispatch loop immediately checks the pending queue
        and dispatches any queued tasks to idle agents.
        """
        orchestrator.resume()
        return {"paused": False}
    
    @router.get(
        "/orchestrator/status",
        summary="Orchestrator operational status",
        dependencies=[Depends(auth)],
    )
    async def orchestrator_status() -> dict:
        """Return operational status of the orchestrator.
    
        Returns:
        - ``paused``: whether dispatch is currently paused
        - ``queue_depth``: number of tasks waiting in the pending queue
        - ``agent_count``: total number of registered agents
        - ``dlq_depth``: number of tasks in the dead-letter queue
        """
        return {
            "paused": orchestrator.is_paused,
            "queue_depth": len(orchestrator.list_tasks()),
            "agent_count": len(orchestrator.list_agents()),
            "dlq_depth": len(orchestrator.list_dlq()),
        }
    
    @router.get(
        "/rate-limit",
        summary="Get rate limiter status",
        dependencies=[Depends(auth)],
    )
    async def get_rate_limit() -> dict:
        """Return the current rate limiter configuration and token availability.
    
        Fields:
        - ``enabled``: True when rate limiting is active.
        - ``rate``: refill rate in tokens per second.
        - ``burst``: bucket capacity (maximum burst size).
        - ``available_tokens``: tokens currently available (live snapshot).
        """
        return orchestrator.get_rate_limiter_status()
    
    @router.put(
        "/rate-limit",
        summary="Reconfigure rate limiter",
        dependencies=[Depends(auth)],
    )
    async def put_rate_limit(body: RateLimitUpdate) -> dict:
        """Create or update the token-bucket rate limiter.
    
        Set ``rate=0`` to disable rate limiting (unlimited throughput).
        ``burst`` is ignored when ``rate=0``.
    
        Returns the updated rate limiter status.
        """
        return orchestrator.reconfigure_rate_limiter(rate=body.rate, burst=body.burst)
    
    @router.get(
        "/orchestrator/autoscaler",
        summary="Get autoscaler status",
        dependencies=[Depends(auth)],
    )
    async def get_autoscaler_status() -> dict:
        """Return the current autoscaler state.
    
        Returns ``{"enabled": false, ...}`` when autoscaling is not configured
        (``autoscale_max=0`` in config).
        """
        return await orchestrator.get_autoscaler_status()
    
    @router.put(
        "/orchestrator/autoscaler",
        summary="Reconfigure autoscaler parameters",
        dependencies=[Depends(auth)],
    )
    async def put_autoscaler(body: AutoScalerUpdate) -> dict:
        """Update autoscaling parameters at runtime.
    
        Only supplied fields are changed; omit a field to leave it unchanged.
        Returns 409 when autoscaling is not enabled (``autoscale_max=0``).
        """
        try:
            result = orchestrator.reconfigure_autoscaler(
                min=body.min,
                max=body.max,
                threshold=body.threshold,
                cooldown=body.cooldown,
            )
        except ValueError:
            raise HTTPException(
                status_code=409,
                detail="Autoscaling is not enabled (autoscale_max=0 in config)",
            )
        return result
    @router.get("/healthz", include_in_schema=False)
    async def liveness() -> dict:
        """Liveness probe: returns 200 if the event loop is responsive."""
        return {"status": "ok", "ts": time.time()}
    
    @router.get("/readyz", include_in_schema=False)
    async def readiness():
        """Readiness probe: 200 when the system can accept and dispatch tasks."""
        checks: dict = {}
        ready = True
    
        # Dispatch loop running?
        dispatch_alive = (
            orchestrator._dispatch_task is not None
            and not orchestrator._dispatch_task.done()
        )
        checks["dispatch_loop"] = {"ready": dispatch_alive}
        if not dispatch_alive:
            ready = False
    
        # At least one non-error worker?
        agents = orchestrator.list_agents()
        workers = [a for a in agents if a.get("role", AgentRole.WORKER) == AgentRole.WORKER]
        error_workers = [a for a in workers if a["status"] == "ERROR"]
        agent_ready = len(workers) > 0 and len(error_workers) < len(workers)
        checks["agents"] = {
            "ready": agent_ready,
            "total": len(workers),
            "error": len(error_workers),
        }
        if not agent_ready:
            ready = False
    
        # Dispatch not paused?
        if orchestrator.is_paused:
            checks["dispatch_paused"] = {"ready": False}
            ready = False
    
        return JSONResponse(
            content={"ready": ready, "checks": checks},
            status_code=200 if ready else 503,
        )
    
    @router.get("/dlq", summary="Dead letter queue", dependencies=[Depends(auth)])
    async def dead_letter_queue() -> list:
        """Return tasks that could not be dispatched after exhausting retries."""
        return orchestrator.list_dlq()
    
    # ------------------------------------------------------------------
    # Security: Audit log endpoint
    # Reference: DESIGN.md §10.18 (v0.44.0)
    # ------------------------------------------------------------------
    
    @router.get("/audit-log", summary="Recent audit log entries", dependencies=[Depends(auth)])
    async def get_audit_log(limit: int = 100) -> list:
        """Return the most recent audit log entries (up to *limit*).
    
        Each entry records a single HTTP request: timestamp, method, path,
        client_ip, api_key_hint (first 8 chars only), status_code, duration_ms.
    
        Entries are stored in an in-process ring buffer of at most 1 000
        entries.  No sensitive data (full API keys, request bodies) is stored.
    
        Design reference:
        - Microsoft Multi-Agent Reference Architecture — Security (2025)
          https://microsoft.github.io/multi-agent-reference-architecture/docs/security/Security.html
        - DESIGN.md §10.18 (v0.44.0)
        """
        from tmux_orchestrator.security import AuditLogMiddleware
        entries = AuditLogMiddleware.get_log()
        # Return the most recent *limit* entries (newest last)
        return [e.to_dict() for e in entries[-limit:]]
    
    # ------------------------------------------------------------------
    # Checkpoint status (DESIGN.md §10.12 v0.45.0)
    # ------------------------------------------------------------------
    
    @router.get("/checkpoint/status", summary="Checkpoint store status", dependencies=[Depends(auth)])
    async def get_checkpoint_status() -> dict:
        """Return the current state of the checkpoint store.
    
        When ``checkpoint_enabled: true`` is set in the YAML config, this
        endpoint reports how many tasks and workflows are currently persisted
        in the SQLite checkpoint database.  This can be used to verify that
        checkpoints are being written and to diagnose resume issues.
    
        Returns ``{"enabled": false}`` when checkpointing is disabled.
    
        Reference: LangGraph checkpointer pattern (LangChain 2025);
                   DESIGN.md §10.12 (v0.45.0).
        """
        store = orchestrator.get_checkpoint_store()
        if store is None:
            return {"enabled": False}
        pending_tasks = store.load_pending_tasks()
        waiting_tasks = store.load_waiting_tasks()
        workflows = store.load_workflows()
        session_name = store.load_meta("session_name")
        return {
            "enabled": True,
            "pending_tasks": len(pending_tasks),
            "waiting_tasks": len(waiting_tasks),
            "workflows": len(workflows),
            "session_name": session_name,
            "pending_task_ids": [t.id for t in pending_tasks],
            "workflow_ids": list(workflows.keys()),
        }
    
    @router.get(
        "/telemetry/spans",
        summary="Recent OTel spans (ring buffer)",
        dependencies=[Depends(auth)],
    )
    async def get_telemetry_spans(limit: int = 50) -> list:
        """Return recently captured OTel spans from the in-process ring buffer.

        When ``telemetry_enabled: true`` is set and no ``OTEL_EXPORTER_OTLP_ENDPOINT``
        is configured, spans are accumulated in a ``RingBufferSpanExporter`` (capacity
        200).  This endpoint returns up to *limit* of the most recent spans as JSON
        dicts for debugging and testing purposes.

        Returns an empty list when:
        - telemetry is disabled, or
        - an OTLP exporter is active (spans flow to an external collector, not a ring buffer).

        Each span dict contains: ``name``, ``trace_id``, ``span_id``, ``parent_id``,
        ``start_time``, ``end_time``, ``status``, ``attributes``.

        Reference: OTel GenAI Semantic Conventions
                   https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/
                   DESIGN.md §10.20 (v1.1.10).
        """
        telemetry = orchestrator.get_telemetry()
        if telemetry is None:
            return []
        from tmux_orchestrator.telemetry import RingBufferSpanExporter  # noqa: PLC0415
        ring = telemetry.ring_buffer_exporter
        if ring is None:
            return []
        spans = ring.get_spans()
        # Return the most recent *limit* spans (newest-last ordering preserved)
        return spans[-limit:]

    @router.get("/telemetry/status", summary="OpenTelemetry status", dependencies=[Depends(auth)])
    async def get_telemetry_status() -> dict:
        """Return the current telemetry configuration.
    
        When ``telemetry_enabled: true`` is set in the YAML config, this endpoint
        reports whether an OTLP exporter is configured or whether the fallback
        ConsoleSpanExporter is active.
    
        Returns ``{"enabled": false}`` when telemetry is disabled.
    
        Reference: OTel GenAI Semantic Conventions
                   https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/
                   DESIGN.md §10.14 (v0.47.0).
        """
        telemetry = orchestrator.get_telemetry()
        if telemetry is None:
            return {"enabled": False}
        otlp_endpoint = orchestrator.config.otlp_endpoint
        return {
            "enabled": True,
            "otlp_endpoint": otlp_endpoint or None,
            "exporter": "otlp" if otlp_endpoint else "console",
        }
    
    @router.post("/checkpoint/clear", summary="Clear all checkpoint data", dependencies=[Depends(auth)])
    async def clear_checkpoint() -> dict:
        """Wipe all checkpoint data (tasks, workflows, meta).
    
        Use this to reset the checkpoint state when starting fresh after a
        resume, or to discard stale checkpoints from a previous session.
    
        Warning: this is irreversible.  All pending/waiting task snapshots
        and workflow state will be deleted from the SQLite database.
    
        Reference: DESIGN.md §10.12 (v0.45.0).
        """
        store = orchestrator.get_checkpoint_store()
        if store is None:
            raise HTTPException(status_code=400, detail="Checkpointing is not enabled")
        store.clear_all()
        return {"cleared": True}
    
    # ------------------------------------------------------------------
    # Prometheus metrics (no auth — Prometheus scraper compatibility)
    # ------------------------------------------------------------------
    
    @router.get("/metrics", include_in_schema=False)
    async def prometheus_metrics():
        """Expose Prometheus-format metrics for the orchestrator.
    
        No authentication required so that Prometheus (or OpenTelemetry
        collectors) can scrape without managing credentials.  Expose this
        port only on a trusted network or bind it to localhost.
    
        Metrics exposed:
        - ``tmux_agent_status_total{status}`` — gauge: agent count per status
        - ``tmux_task_queue_size`` — gauge: current task queue depth
        - ``tmux_bus_drop_total{agent_id}`` — gauge: per-agent bus drop count
    
        Reference: prometheus_client Python library;
                   DESIGN.md §10.6 (Prometheus metrics, low priority);
                   OneUptime blog (2025-01-06) — python-custom-metrics-prometheus.
        """
        from prometheus_client import (  # noqa: PLC0415
            CONTENT_TYPE_LATEST,
            CollectorRegistry,
            Gauge,
            generate_latest,
        )
        from fastapi.responses import Response  # noqa: PLC0415
    
        registry = CollectorRegistry()
    
        # --- Agent status distribution ---
        agent_status_gauge = Gauge(
            "tmux_agent_status_total",
            "Number of agents per status",
            ["status"],
            registry=registry,
        )
        agents = orchestrator.list_agents()
        status_counts: dict[str, int] = {}
        for a in agents:
            s = a.get("status", "UNKNOWN")
            status_counts[s] = status_counts.get(s, 0) + 1
        for status_val, count in status_counts.items():
            agent_status_gauge.labels(status=status_val).set(count)
    
        # --- Task queue depth ---
        task_queue_gauge = Gauge(
            "tmux_task_queue_size",
            "Current number of tasks waiting in the queue",
            registry=registry,
        )
        task_queue_gauge.set(len(orchestrator.list_tasks()))
    
        # --- Bus drop counts ---
        bus_drop_gauge = Gauge(
            "tmux_bus_drop_total",
            "Total dropped bus messages per agent",
            ["agent_id"],
            registry=registry,
        )
        for a in agents:
            drops = a.get("bus_drops", 0)
            if drops:
                bus_drop_gauge.labels(agent_id=a["id"]).set(drops)
    
        output = generate_latest(registry)
        return Response(content=output, media_type=CONTENT_TYPE_LATEST)

    # ------------------------------------------------------------------
    # Metrics time-series endpoints (JSON, ring buffer)
    # Reference: DESIGN.md §10.92 (v1.2.16)
    # ------------------------------------------------------------------

    @router.get(
        "/metrics/time-series",
        summary="Agent and queue metrics time series (ring buffer)",
        dependencies=[Depends(auth)],
    )
    async def get_metrics_time_series(last_n: int = 60) -> dict:
        """Return the last *last_n* metric snapshots from the ring buffer.

        Each snapshot is collected every ``metrics_interval_s`` seconds
        (default 10 s, configured via ``OrchestratorConfig.metrics_interval_s``).

        The default ``last_n=60`` returns the last 10 minutes of history at
        the default 10-second collection interval.

        Response structure:
        - ``enabled``: ``false`` when ``metrics_enabled: false`` in config.
        - ``interval_s``: collection interval (seconds).
        - ``count``: number of snapshots returned.
        - ``snapshots``: list of ``{timestamp, queue_depth, active_agents, idle_agents,
          tasks_completed_total, tasks_failed_total}``.
        - ``latest``: most-recent snapshot with full ``per_agent`` detail, or ``null``.

        Design reference: DESIGN.md §10.92 (v1.2.16)
        """
        from dataclasses import asdict  # noqa: PLC0415
        if metrics_collector is None:
            return {"enabled": False, "interval_s": 10, "count": 0, "snapshots": [], "latest": None}
        snapshots = metrics_collector.get_snapshots(last_n=last_n)
        latest_snap = metrics_collector.get_latest()
        return {
            "enabled": True,
            "interval_s": metrics_collector.interval_s,
            "count": len(snapshots),
            "snapshots": [
                {
                    "timestamp": s.timestamp,
                    "queue_depth": s.queue_depth,
                    "active_agents": s.active_agents,
                    "idle_agents": s.idle_agents,
                    "tasks_completed_total": s.tasks_completed_total,
                    "tasks_failed_total": s.tasks_failed_total,
                }
                for s in snapshots
            ],
            "latest": asdict(latest_snap) if latest_snap is not None else None,
        }

    @router.get(
        "/metrics/agents/{agent_id}",
        summary="Per-agent metrics time series",
        dependencies=[Depends(auth)],
    )
    async def get_agent_metrics_series(agent_id: str, last_n: int = 60) -> dict:
        """Return per-agent metric time series for *agent_id*.

        Each entry in ``series`` contains ``{timestamp, status}`` plus any
        per-agent stats available at collection time (``tasks_completed``,
        ``tasks_failed``, ``error_rate``).

        Returns a 404 when the agent has never appeared in any snapshot.

        Design reference: DESIGN.md §10.92 (v1.2.16)
        """
        if metrics_collector is None:
            return {"enabled": False, "agent_id": agent_id, "series": []}
        snapshots = metrics_collector.get_snapshots(last_n=last_n)
        series = [
            {
                "timestamp": s.timestamp,
                **s.per_agent.get(agent_id, {"status": "unknown"}),
            }
            for s in snapshots
        ]
        return {"enabled": True, "agent_id": agent_id, "series": series}

    return router
