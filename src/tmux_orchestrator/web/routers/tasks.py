"""Tasks APIRouter — /tasks/* endpoints.

Design reference: DESIGN.md §10.42 (v1.1.6)
FastAPI "Bigger Applications": https://fastapi.tiangolo.com/tutorial/bigger-applications/
"""

from __future__ import annotations

import uuid as _uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from tmux_orchestrator.web.schemas import (
    BroadcastTaskSubmit,
    TaskBatchSubmit,
    TaskPriorityUpdate,
    TaskSubmit,
)


def build_tasks_router(
    orchestrator: Any,
    auth: Callable,
    *,
    rate_limit: str = "60/minute",
    limiter: Any = None,
) -> APIRouter:
    """Build and return the tasks APIRouter.

    Parameters
    ----------
    orchestrator:
        The :class:`~tmux_orchestrator.orchestrator.Orchestrator` instance.
    auth:
        Authentication dependency callable (combined session + API key).
    rate_limit:
        SlowAPI rate limit string applied to POST /tasks (default ``"60/minute"``).
    limiter:
        SlowAPI Limiter instance.  When provided, POST /tasks is rate-limited.
    """
    router = APIRouter()

    # POST /tasks — rate-limited when a limiter is provided
    # We define two variants and only register the appropriate one.
    if limiter is not None:

        @router.post("/tasks", summary="Submit a new task", dependencies=[Depends(auth)])
        @limiter.limit(rate_limit)
        async def submit_task_limited(  # noqa: N802 (name differs from non-limited variant intentionally)
            request: Request,  # noqa: ARG001 (required by SlowAPI)
            body: TaskSubmit,
        ) -> dict:
            from tmux_orchestrator.security import sanitize_prompt  # noqa: PLC0415
            return await _do_submit_task(orchestrator, body)

    else:

        @router.post("/tasks", summary="Submit a new task", dependencies=[Depends(auth)])
        async def submit_task(request: Request, body: TaskSubmit) -> dict:  # noqa: ARG001
            return await _do_submit_task(orchestrator, body)

    @router.post("/tasks/batch", summary="Submit multiple tasks in one request", dependencies=[Depends(auth)])
    async def submit_tasks_batch(body: TaskBatchSubmit) -> dict:
        """Submit a list of tasks atomically.

        All tasks in the batch are validated before any are enqueued.  If the
        request body is malformed, FastAPI returns 422 before this handler runs.

        Design reference:
        - adidas API Guidelines "Batch Operations"
          https://adidas.gitbook.io/api-guidelines/rest-api-guidelines/execution/batch-operations
        - PayPal Batch API (Medium, PayPal Tech Blog)
          https://medium.com/paypal-tech/batch-an-api-to-bundle-multiple-paypal-rest-operations-6af6006e002
        """
        results: list[dict] = []
        local_to_global: dict[str, str] = {}
        # Pre-allocate task IDs for all items that have a local_id
        for item in body.tasks:
            if item.local_id:
                local_to_global[item.local_id] = str(_uuid.uuid4())

        for item in body.tasks:
            resolved_deps: list[str] = []
            for dep in item.depends_on:
                if dep in local_to_global:
                    resolved_deps.append(local_to_global[dep])
                else:
                    resolved_deps.append(dep)

            task = await orchestrator.submit_task(
                item.prompt,
                priority=item.priority,
                metadata=item.metadata,
                depends_on=resolved_deps or None,
                reply_to=item.reply_to,
                target_agent=item.target_agent,
                required_tags=item.required_tags or None,
                target_group=item.target_group,
                max_retries=item.max_retries,
                ttl=item.ttl,
                _task_id=local_to_global.get(item.local_id) if item.local_id else None,
            )
            record: dict = {
                "task_id": task.id,
                "prompt": task.prompt,
                "priority": task.priority,
                "max_retries": task.max_retries,
                "retry_count": task.retry_count,
                "submitted_at": task.submitted_at,
                "ttl": task.ttl,
                "expires_at": task.expires_at,
            }
            if item.local_id:
                record["local_id"] = item.local_id
            if task.depends_on:
                record["depends_on"] = task.depends_on
            if task.reply_to is not None:
                record["reply_to"] = task.reply_to
            if task.target_agent is not None:
                record["target_agent"] = task.target_agent
            if task.required_tags:
                record["required_tags"] = task.required_tags
            if task.target_group is not None:
                record["target_group"] = task.target_group
            results.append(record)
        return {"tasks": results}

    @router.post(
        "/tasks/broadcast",
        summary="Submit same task to multiple agents (broadcast/fan-out)",
        dependencies=[Depends(auth)],
    )
    async def broadcast_task(body: BroadcastTaskSubmit) -> dict:
        """Submit the same task to multiple agents simultaneously.

        Target resolution (first match wins):
        1. ``agent_ids`` — explicit list of agent IDs.
        2. ``target_group`` — all agents in this named group.
        3. ``target_tags`` — all agents whose tags include ALL listed tags.

        Returns immediately with a ``broadcast_id``; poll
        ``GET /tasks/broadcast/{broadcast_id}`` for status and results.

        Design references:
        - Fan-out / Fan-in concurrency pattern (Go Concurrency Patterns, DEV 2025)
        - Sakana AI ALE-Agent AHC058 — parallel best-of-N trial-and-error
        - LAMaS latency-aware orchestration (arXiv:2601.10560, 2025)
        - DESIGN.md §10.91 (v1.2.15)
        """
        from tmux_orchestrator.security import sanitize_prompt  # noqa: PLC0415

        # Resolve target agent IDs.
        resolved_ids: list[str] = []

        if body.agent_ids:
            # Explicit IDs — validate they exist.
            all_agents = {a["id"] for a in orchestrator.list_agents()}
            resolved_ids = [aid for aid in body.agent_ids if aid in all_agents]
            missing = [aid for aid in body.agent_ids if aid not in all_agents]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown agent IDs: {missing}",
                )
        elif body.target_group is not None:
            gm = getattr(orchestrator, "_group_manager", None)
            if gm is None:
                raise HTTPException(status_code=400, detail="No group manager available")
            members = gm.get(body.target_group)
            if members is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Group {body.target_group!r} not found",
                )
            resolved_ids = sorted(members)
        elif body.target_tags:
            all_agents_list = orchestrator.list_agents()
            needed = set(body.target_tags)
            resolved_ids = [
                a["id"]
                for a in all_agents_list
                if needed.issubset(set(a.get("tags", [])))
            ]
        else:
            raise HTTPException(
                status_code=400,
                detail="Must specify at least one of: agent_ids, target_group, target_tags",
            )

        if not resolved_ids:
            raise HTTPException(
                status_code=400,
                detail="No agents matched the given target criteria",
            )

        result = await orchestrator.broadcast_task(
            sanitize_prompt(body.prompt),
            resolved_ids,
            mode=body.mode,
            priority=body.priority,
            timeout=body.timeout,
        )
        return {
            "broadcast_id": result.broadcast_id,
            "mode": result.mode,
            "task_ids": result.task_ids,
            "agent_ids": result.agent_ids,
            "status": "pending",
        }

    @router.get(
        "/tasks/broadcast/{broadcast_id}",
        summary="Get broadcast task status and results",
        dependencies=[Depends(auth)],
    )
    async def get_broadcast_status(broadcast_id: str) -> dict:
        """Poll the status of a broadcast submitted via POST /tasks/broadcast.

        Returns:
        - ``status``: ``"pending"`` | ``"running"`` | ``"complete"`` | ``"failed"``
        - ``winner``: (race mode) task and result for the winning agent, or ``null``
        - ``results``: (gather mode) list of ``{task_id, agent_id, result}`` objects
        - ``failed_tasks``: list of task IDs that failed

        Design reference: DESIGN.md §10.91 (v1.2.15)
        """
        bc = orchestrator.get_broadcast(broadcast_id)
        if bc is None:
            raise HTTPException(
                status_code=404,
                detail=f"Broadcast {broadcast_id!r} not found",
            )

        # Build agent_id → task_id reverse lookup for result presentation.
        task_to_agent: dict[str, str] = {
            tid: aid
            for tid, aid in zip(bc.task_ids, bc.agent_ids)
        }

        winner: dict | None = None
        if bc.winner_task_id is not None:
            winner = {
                "task_id": bc.winner_task_id,
                "agent_id": task_to_agent.get(bc.winner_task_id),
                "result": bc.completed_tasks.get(bc.winner_task_id, ""),
            }

        results = [
            {
                "task_id": tid,
                "agent_id": task_to_agent.get(tid),
                "result": bc.completed_tasks[tid],
            }
            for tid in bc.task_ids
            if tid in bc.completed_tasks
        ]

        return {
            "broadcast_id": bc.broadcast_id,
            "mode": bc.mode,
            "status": bc.status,
            "task_ids": bc.task_ids,
            "agent_ids": bc.agent_ids,
            "winner": winner,
            "results": results,
            "failed_tasks": sorted(bc.failed_tasks),
        }

    @router.get("/tasks", summary="List all tasks (active + completed)", dependencies=[Depends(auth)])
    async def list_tasks(skip: int = 0, limit: int = 100) -> list[dict]:
        """Return all tasks: currently queued, in-progress, and completed/failed.

        Combines the pending queue, currently dispatched (in-progress) tasks,
        and per-agent history into a single flat list.  Use ``skip`` and
        ``limit`` query params for pagination.

        Design reference:
        - AWS SQS message visibility / dead-letter queue listing
        - DESIGN.md §10.21 (v0.26.0)
        """
        all_tasks: list[dict] = []

        # 1. Pending (queued) and waiting tasks
        for item in orchestrator.list_tasks():
            task_status = item.get("status", "queued")
            record: dict = {
                "task_id": item["task_id"],
                "prompt": item["prompt"],
                "priority": item["priority"],
                "status": task_status,
                "max_retries": 0,
                "retry_count": 0,
                "submitted_at": item.get("submitted_at"),
                "ttl": item.get("ttl"),
                "expires_at": item.get("expires_at"),
            }
            if item.get("depends_on"):
                record["depends_on"] = item["depends_on"]
            if item.get("required_tags"):
                record["required_tags"] = item["required_tags"]
            if item.get("target_agent"):
                record["target_agent"] = item["target_agent"]
            all_tasks.append(record)

        queued_ids = {t["task_id"] for t in all_tasks}

        # 2. In-progress tasks
        for agent in orchestrator.list_agents():
            agent_obj = orchestrator.get_agent(agent["id"])
            if agent_obj is not None and agent_obj._current_task is not None:
                ct = agent_obj._current_task
                if ct.id not in queued_ids:
                    all_tasks.append({
                        "task_id": ct.id,
                        "prompt": ct.prompt,
                        "priority": ct.priority,
                        "status": "in_progress",
                        "agent_id": agent["id"],
                        "max_retries": ct.max_retries,
                        "retry_count": ct.retry_count,
                        **({"required_tags": ct.required_tags} if ct.required_tags else {}),
                        **({"target_agent": ct.target_agent} if ct.target_agent else {}),
                    })

        # 3. Completed / failed tasks from per-agent history
        seen_task_ids = {t["task_id"] for t in all_tasks}
        for agent in orchestrator.list_agents():
            history = orchestrator.get_agent_history(agent["id"], limit=200) or []
            for record in history:
                tid = record.get("task_id")
                if tid and tid not in seen_task_ids:
                    seen_task_ids.add(tid)
                    active_task = orchestrator._active_tasks.get(tid)
                    all_tasks.append({
                        "task_id": tid,
                        "prompt": record.get("prompt", ""),
                        "priority": 0,
                        "status": record.get("status", "unknown"),
                        "started_at": record.get("started_at"),
                        "finished_at": record.get("finished_at"),
                        "duration_s": record.get("duration_s"),
                        "error": record.get("error"),
                        "agent_id": agent["id"],
                        "max_retries": active_task.max_retries if active_task else 0,
                        "retry_count": active_task.retry_count if active_task else 0,
                    })

        return all_tasks[skip : skip + limit]

    @router.get(
        "/tasks/{task_id}",
        summary="Get a specific task by ID",
        dependencies=[Depends(auth)],
    )
    async def get_task(task_id: str) -> dict:
        """Return the status and details of a specific task by its ID.

        Design reference: DESIGN.md §10.21 (v0.26.0); DESIGN.md §10.24 (v0.29.0)
        """
        # 0. Check _waiting_tasks first
        waiting_task = orchestrator.get_waiting_task(task_id)
        if waiting_task is not None:
            blocking = orchestrator._task_blocking(task_id)
            resp: dict = {
                "task_id": task_id,
                "prompt": waiting_task.prompt,
                "priority": waiting_task.priority,
                "status": "waiting",
                "depends_on": waiting_task.depends_on,
                "max_retries": waiting_task.max_retries,
                "retry_count": waiting_task.retry_count,
                "inherit_priority": waiting_task.inherit_priority,
                "submitted_at": waiting_task.submitted_at,
                "ttl": waiting_task.ttl,
                "expires_at": waiting_task.expires_at,
                "timeout": waiting_task.timeout,
                "escalation_count": getattr(waiting_task, "escalation_count", 0),
                "excluded_agents": getattr(waiting_task, "excluded_agents", []),
            }
            if blocking:
                resp["blocking"] = blocking
            if waiting_task.required_tags:
                resp["required_tags"] = waiting_task.required_tags
            if waiting_task.target_agent:
                resp["target_agent"] = waiting_task.target_agent
            return resp

        # 1. Check pending queue
        for item in orchestrator.list_tasks():
            if item["task_id"] == task_id:
                active = orchestrator._active_tasks.get(task_id)
                blocking = orchestrator._task_blocking(task_id)
                resp = {
                    "task_id": task_id,
                    "prompt": item["prompt"],
                    "priority": item["priority"],
                    "status": item.get("status", "queued"),
                    "depends_on": item.get("depends_on", []),
                    "max_retries": active.max_retries if active else 0,
                    "retry_count": active.retry_count if active else 0,
                    "inherit_priority": active.inherit_priority if active else True,
                    "submitted_at": item.get("submitted_at"),
                    "ttl": item.get("ttl"),
                    "expires_at": item.get("expires_at"),
                    "timeout": active.timeout if active else item.get("timeout"),
                    "escalation_count": getattr(active, "escalation_count", 0) if active else 0,
                    "excluded_agents": getattr(active, "excluded_agents", []) if active else [],
                }
                if blocking:
                    resp["blocking"] = blocking
                if item.get("required_tags"):
                    resp["required_tags"] = item["required_tags"]
                if item.get("target_agent"):
                    resp["target_agent"] = item["target_agent"]
                return resp

        # 2. Check in-progress tasks
        for agent in orchestrator.list_agents():
            agent_obj = orchestrator.get_agent(agent["id"])
            if agent_obj is not None and agent_obj._current_task is not None:
                ct = agent_obj._current_task
                if ct.id == task_id:
                    blocking = orchestrator._task_blocking(task_id)
                    resp = {
                        "task_id": ct.id,
                        "prompt": ct.prompt,
                        "priority": ct.priority,
                        "status": "in_progress",
                        "depends_on": ct.depends_on,
                        "agent_id": agent["id"],
                        "max_retries": ct.max_retries,
                        "retry_count": ct.retry_count,
                        "inherit_priority": ct.inherit_priority,
                        "submitted_at": ct.submitted_at,
                        "ttl": ct.ttl,
                        "expires_at": ct.expires_at,
                        "timeout": ct.timeout,
                        "escalation_count": getattr(ct, "escalation_count", 0),
                        "excluded_agents": getattr(ct, "excluded_agents", []),
                    }
                    if blocking:
                        resp["blocking"] = blocking
                    return resp

        # 3. Check per-agent history
        for agent in orchestrator.list_agents():
            history = orchestrator.get_agent_history(agent["id"], limit=200) or []
            for record in history:
                if record.get("task_id") == task_id:
                    active = orchestrator._active_tasks.get(task_id)
                    blocking = orchestrator._task_blocking(task_id)
                    hist_resp: dict = {
                        "task_id": task_id,
                        "prompt": record.get("prompt", ""),
                        "priority": 0,
                        "status": record.get("status", "unknown"),
                        "agent_id": agent["id"],
                        "started_at": record.get("started_at"),
                        "finished_at": record.get("finished_at"),
                        "duration_s": record.get("duration_s"),
                        "error": record.get("error"),
                        "max_retries": active.max_retries if active else 0,
                        "retry_count": active.retry_count if active else 0,
                        "timeout": record.get("timeout"),
                    }
                    if active and active.depends_on:
                        hist_resp["depends_on"] = active.depends_on
                    if blocking:
                        hist_resp["blocking"] = blocking
                    return hist_resp

        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    @router.patch(
        "/tasks/{task_id}",
        summary="Update a pending task's priority",
        dependencies=[Depends(auth)],
    )
    async def update_task_priority(task_id: str, body: TaskPriorityUpdate) -> dict:
        """Update the priority of a task that is still in the pending queue."""
        updated = await orchestrator.update_task_priority(task_id, body.priority)
        if updated:
            return {"updated": True, "task_id": task_id, "priority": body.priority}
        return {"updated": False, "task_id": task_id}

    @router.patch(
        "/tasks/{task_id}/priority",
        summary="Update a queued task's priority (returns 404 if not found)",
        dependencies=[Depends(auth)],
    )
    async def update_task_priority_strict(task_id: str, body: TaskPriorityUpdate) -> dict:
        """Update the priority of a task that is still in the pending queue.

        Unlike ``PATCH /tasks/{task_id}``, this endpoint returns HTTP 404 when
        the task is not found (already dispatched, completed, or never submitted).

        Design reference: DESIGN.md §10.82 — v1.2.6 Dynamic Task Priority Update.
        Reference: Postman Blog "HTTP PATCH Method: Partial Updates for RESTful APIs"
          https://blog.postman.com/http-patch-method/
        """
        updated = await orchestrator.update_task_priority(task_id, body.priority)
        if not updated:
            raise HTTPException(
                status_code=404,
                detail=f"Task {task_id!r} not found or already dispatched",
            )
        return {"task_id": task_id, "priority": body.priority, "updated": True}

    @router.delete(
        "/tasks/{task_id}",
        summary="Cancel a task by ID (queued or in-progress)",
        dependencies=[Depends(auth)],
    )
    async def delete_task(task_id: str) -> dict:
        """Cancel *task_id* whether it is queued or currently in-progress.

        Delegates to ``CancelTaskUseCase`` so the handler is a thin HTTP
        adapter (Martin "Clean Architecture" Ch. 22).

        Design references:
        - Kubernetes ``kubectl delete pod`` — REST DELETE on a resource URI
        - POSIX SIGTERM/SIGKILL model; Go context.Context cancellation
        - DESIGN.md §10.22 (v0.27.0)
        """
        from tmux_orchestrator.application.use_cases import CancelTaskDTO, CancelTaskUseCase  # noqa: PLC0415

        use_case = CancelTaskUseCase(orchestrator)
        result = await use_case.execute(CancelTaskDTO(task_id=task_id))
        if not result.cancelled:
            raise HTTPException(
                status_code=404,
                detail=f"Task {task_id!r} not found (already completed, unknown, or dead-lettered)",
            )
        return result.to_dict()

    @router.post(
        "/tasks/{task_id}/cancel",
        summary="Cancel a pending task",
        dependencies=[Depends(auth)],
    )
    async def cancel_task(task_id: str) -> dict:
        """Remove *task_id* from the pending queue and discard it.

        Returns:
        - ``{"cancelled": true, "task_id": ..., "status": "cancelled"}``
          if the task was successfully removed from the queue.
        - ``{"cancelled": false, "task_id": ..., "status": "already_dispatched"}``
          if the task was not in the pending queue (already dispatched or
          currently in-flight).
        - ``404`` if the task ID has never been submitted or tracked.

        Design reference: DESIGN.md §11 (v0.17.0) — task cancellation.
        """
        queued_ids = {t["task_id"] for t in orchestrator.list_tasks()}
        was_queued = task_id in queued_ids

        cancelled = await orchestrator.cancel_task(task_id)

        if cancelled:
            return {"cancelled": True, "task_id": task_id, "status": "cancelled"}

        if was_queued:
            return {"cancelled": False, "task_id": task_id, "status": "already_dispatched"}

        in_flight = getattr(orchestrator, "_task_started_at", {})
        if task_id in in_flight:
            return {"cancelled": False, "task_id": task_id, "status": "already_dispatched"}

        completed = getattr(orchestrator, "_completed_tasks", set())
        if task_id in completed:
            return {"cancelled": False, "task_id": task_id, "status": "already_dispatched"}

        dlq_ids = {e.get("task_id") for e in orchestrator.list_dlq()}
        if task_id in dlq_ids:
            return {"cancelled": False, "task_id": task_id, "status": "already_dispatched"}

        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")


    return router


async def _do_submit_task(orchestrator: Any, body: TaskSubmit) -> dict:
    """Shared implementation for rate-limited and non-rate-limited submit_task variants.

    Delegates to ``SubmitTaskUseCase`` so that the HTTP handler remains a thin
    adapter with no business logic (Martin "Clean Architecture" Ch. 22).
    """
    from tmux_orchestrator.application.use_cases import SubmitTaskDTO, SubmitTaskUseCase  # noqa: PLC0415
    from tmux_orchestrator.security import sanitize_prompt  # noqa: PLC0415

    dto = SubmitTaskDTO(
        prompt=sanitize_prompt(body.prompt),
        priority=body.priority,
        metadata=body.metadata or {},
        depends_on=list(body.depends_on) if body.depends_on else [],
        idempotency_key=None,
        reply_to=body.reply_to,
        target_agent=body.target_agent,
        required_tags=list(body.required_tags) if body.required_tags else [],
        target_group=body.target_group,
        max_retries=body.max_retries,
        inherit_priority=body.inherit_priority,
        ttl=body.ttl,
    )
    use_case = SubmitTaskUseCase(orchestrator)
    result_dto = await use_case.execute(dto)
    return result_dto.to_dict()
