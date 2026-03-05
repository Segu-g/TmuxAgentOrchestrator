"""FastAPI web application — REST endpoints + WebSocket hub."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Callable

import webauthn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel
from webauthn.helpers.structs import (
    AuthenticationCredential,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    RegistrationCredential,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from tmux_orchestrator.agents.base import Task
from tmux_orchestrator.bus import Message, MessageType
from tmux_orchestrator.config import AgentRole
from tmux_orchestrator.web.ws import WebSocketHub

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class TaskSubmit(BaseModel):
    prompt: str
    priority: int = 0
    metadata: dict[str, Any] = {}
    reply_to: str | None = None  # agent_id that receives the RESULT in its mailbox
    target_agent: str | None = None  # when set, task is only dispatched to this agent
    # Capability tags: ALL tags must be present in the target agent's tags list.
    # Reference: FIPA Directory Facilitator (2002); Kubernetes nodeSelector.
    required_tags: list[str] = []


class TaskBatchSubmit(BaseModel):
    """Request body for POST /tasks/batch."""

    tasks: list[TaskSubmit]


class AgentKillResponse(BaseModel):
    agent_id: str
    stopped: bool


class SendMessage(BaseModel):
    type: str = "PEER_MSG"
    payload: dict[str, Any] = {}


class SpawnAgent(BaseModel):
    parent_id: str
    template_id: str


class DynamicAgentCreate(BaseModel):
    """Request body for POST /agents/new — template-free dynamic agent creation.

    Allows a Director (or operator) to add a new agent at runtime without
    any pre-configured YAML entry.  All fields are optional; sensible defaults
    are applied by the orchestrator.
    """

    agent_id: str | None = None
    tags: list[str] = []
    system_prompt: str | None = None
    isolate: bool = True
    merge_on_stop: bool = False
    merge_target: str | None = None
    command: str | None = None
    role: str = "worker"
    task_timeout: int | None = None
    parent_id: str | None = None


class DirectorChat(BaseModel):
    message: str


class ScratchpadWrite(BaseModel):
    """Request body for PUT /scratchpad/{key}."""

    value: Any


class TaskPriorityUpdate(BaseModel):
    """Request body for PATCH /tasks/{task_id}."""

    priority: int


class RateLimitUpdate(BaseModel):
    """Request body for PUT /rate-limit.

    Set ``rate=0`` to disable rate limiting (unlimited throughput).
    """

    rate: float
    burst: int = 0


class AutoScalerUpdate(BaseModel):
    """Request body for PUT /orchestrator/autoscaler.

    All fields are optional — only supplied fields are updated.
    """

    min: int | None = None
    max: int | None = None
    threshold: int | None = None
    cooldown: float | None = None


class WorkflowTaskSpec(BaseModel):
    """A single task node in a workflow DAG submission.

    ``local_id`` is a caller-defined name used to express dependencies
    within this submission.  It is translated to a global orchestrator
    task ID by ``POST /workflows`` before the tasks are enqueued.

    Design reference:
    - Apache Airflow: DAG nodes identified by ``task_id`` strings
    - AWS Step Functions: states referenced by name within a state machine
    - Tomasulo's algorithm: register renaming == local_id → global_task_id
    - DESIGN.md §10.20 (v0.25.0)
    """

    local_id: str
    prompt: str
    depends_on: list[str] = []
    target_agent: str | None = None
    required_tags: list[str] = []
    priority: int = 0


class WorkflowSubmit(BaseModel):
    """Request body for POST /workflows."""

    name: str = "workflow"
    tasks: list[WorkflowTaskSpec]


# ---------------------------------------------------------------------------
# Module-level auth state
# ---------------------------------------------------------------------------

_credentials: dict[str, bytes] = {}    # b64url(cred_id) → public_key bytes
_sign_counts: dict[str, int] = {}      # b64url(cred_id) → sign_count
_sessions: dict[str, float] = {}       # session_token  → expiry (unix ts)
_pending_challenge: bytes | None = None
_SESSION_TTL = 86_400  # 24 h

# ---------------------------------------------------------------------------
# Shared scratchpad — in-process key/value store (cleared on restart)
# ---------------------------------------------------------------------------

_scratchpad: dict[str, Any] = {}  # key → arbitrary JSON-serialisable value


def _new_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + _SESSION_TTL
    return token


def _valid_session(token: str | None) -> bool:
    if not token:
        return False
    expiry = _sessions.get(token)
    return expiry is not None and expiry > time.time()


def _request_origin(request: Request) -> str:
    """Derive the WebAuthn expected_origin, respecting X-Forwarded-Proto from proxies."""
    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    return f"{scheme}://{request.url.netloc}"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------


def _make_session_auth():
    async def _check(request: Request) -> None:
        if not _valid_session(request.cookies.get("session")):
            raise HTTPException(401, "Authentication required")
    return _check


def _make_combined_auth(api_key: str):
    """Session cookie OR X-API-Key/query param; both accepted."""
    async def _check(request: Request) -> None:
        if _valid_session(request.cookies.get("session")):
            return
        if api_key:
            provided = (
                request.headers.get("X-API-Key", "")
                or request.query_params.get("key", "")
            )
            if provided == api_key:
                return
        raise HTTPException(401, "Authentication required")
    return _check


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _build_agent_tree(agents: list[dict]) -> list[dict]:
    """Convert a flat list of agent dicts into a nested tree.

    Each dict must have an ``id`` and optional ``parent_id`` key.  Returns a
    list of root nodes (``parent_id is None``), each with a ``children`` key
    that recursively holds child nodes.

    The resulting structure is compatible with d3-hierarchy's ``d3.hierarchy()``
    function (the library expects a tree rooted at a single node, but we expose
    multiple roots as a list for the REST caller to use freely).
    """
    by_id: dict[str, dict] = {}
    for a in agents:
        node = {**a, "children": []}
        by_id[a["id"]] = node

    roots: list[dict] = []
    for node in by_id.values():
        parent_id = node.get("parent_id")
        if parent_id and parent_id in by_id:
            by_id[parent_id]["children"].append(node)
        else:
            roots.append(node)

    return roots


def create_app(
    orchestrator: Any,
    hub: WebSocketHub,
    *,
    api_key: str = "",
    on_startup: Callable[[], Any] | None = None,
    on_shutdown: Callable[[], Any] | None = None,
) -> FastAPI:
    """Create and wire up the FastAPI application.

    Parameters
    ----------
    orchestrator:
        The :class:`~tmux_orchestrator.orchestrator.Orchestrator` instance.
    hub:
        A :class:`WebSocketHub` already connected to the bus.
    on_startup:
        Optional async callable invoked during lifespan startup (after hub).
        Use this to start the orchestrator when using the web server.
        ``router.on_startup`` hooks are NOT called when a ``lifespan`` context
        manager is provided (FastAPI ≥ 0.93 behaviour), so callers must use
        this parameter instead.
    on_shutdown:
        Optional async callable invoked during lifespan shutdown (before hub).
    """
    auth = _make_combined_auth(api_key)

    @asynccontextmanager
    async def _lifespan(application: FastAPI):  # noqa: ARG001
        await hub.start()
        logger.info("WebSocket hub started")
        if on_startup is not None:
            await on_startup()
        yield
        if on_shutdown is not None:
            await on_shutdown()
        await hub.stop()
        logger.info("WebSocket hub stopped")

    app = FastAPI(
        title="TmuxAgentOrchestrator",
        description="REST + WebSocket API for the tmux agent orchestrator",
        version="0.1.0",
        lifespan=_lifespan,
    )

    # ------------------------------------------------------------------
    # Auth endpoints (no auth dependency — public)
    # ------------------------------------------------------------------

    @app.get("/auth/status", include_in_schema=False)
    async def auth_status(request: Request) -> dict:
        return {
            "registered": bool(_credentials),
            "authenticated": _valid_session(request.cookies.get("session")),
        }

    @app.post("/auth/register-options", include_in_schema=False)
    async def auth_register_options(request: Request) -> JSONResponse:
        global _pending_challenge
        rp_id = request.url.hostname
        options = webauthn.generate_registration_options(
            rp_id=rp_id,
            rp_name="TmuxAgentOrchestrator",
            user_id=b"admin",
            user_name="admin",
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        _pending_challenge = options.challenge
        return JSONResponse(json.loads(webauthn.options_to_json(options)))

    @app.post("/auth/register", include_in_schema=False)
    async def auth_register(request: Request) -> JSONResponse:
        global _pending_challenge
        if _pending_challenge is None:
            raise HTTPException(400, "No pending challenge")
        rp_id = request.url.hostname
        origin = _request_origin(request)
        try:
            body = await request.body()
            credential = RegistrationCredential.parse_raw(body)
            verification = webauthn.verify_registration_response(
                credential=credential,
                expected_challenge=_pending_challenge,
                expected_rp_id=rp_id,
                expected_origin=origin,
            )
        except Exception as exc:
            logger.warning("Registration failed: %s", exc)
            raise HTTPException(400, f"Registration failed: {exc}")
        cred_key = _b64url_encode(verification.credential_id)
        _credentials[cred_key] = verification.credential_public_key
        _sign_counts[cred_key] = verification.sign_count
        _pending_challenge = None
        token = _new_session()
        resp = JSONResponse({"status": "ok"})
        resp.set_cookie("session", token, httponly=True, samesite="lax", path="/")
        return resp

    @app.post("/auth/authenticate-options", include_in_schema=False)
    async def auth_authenticate_options(request: Request) -> JSONResponse:
        global _pending_challenge
        rp_id = request.url.hostname
        allow_credentials = [
            PublicKeyCredentialDescriptor(id=_b64url_decode(k))
            for k in _credentials
        ]
        options = webauthn.generate_authentication_options(
            rp_id=rp_id,
            allow_credentials=allow_credentials,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        _pending_challenge = options.challenge
        return JSONResponse(json.loads(webauthn.options_to_json(options)))

    @app.post("/auth/authenticate", include_in_schema=False)
    async def auth_authenticate(request: Request) -> JSONResponse:
        global _pending_challenge
        if _pending_challenge is None:
            raise HTTPException(400, "No pending challenge")
        rp_id = request.url.hostname
        origin = _request_origin(request)
        try:
            body = await request.body()
            credential = AuthenticationCredential.parse_raw(body)
            cred_key = _b64url_encode(credential.raw_id)
            if cred_key not in _credentials:
                raise HTTPException(401, "Unknown credential")
            verification = webauthn.verify_authentication_response(
                credential=credential,
                expected_challenge=_pending_challenge,
                expected_rp_id=rp_id,
                expected_origin=origin,
                credential_public_key=_credentials[cred_key],
                credential_current_sign_count=_sign_counts[cred_key],
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Authentication failed: %s", exc)
            raise HTTPException(400, f"Authentication failed: {exc}")
        _sign_counts[cred_key] = verification.new_sign_count
        _pending_challenge = None
        token = _new_session()
        resp = JSONResponse({"status": "ok"})
        resp.set_cookie("session", token, httponly=True, samesite="lax", path="/")
        return resp

    @app.post("/auth/logout", include_in_schema=False)
    async def auth_logout(request: Request) -> JSONResponse:
        token = request.cookies.get("session")
        if token:
            _sessions.pop(token, None)
        resp = JSONResponse({"status": "ok"})
        resp.delete_cookie("session", path="/")
        return resp

    # ------------------------------------------------------------------
    # REST endpoints
    # ------------------------------------------------------------------

    @app.post("/tasks", summary="Submit a new task", dependencies=[Depends(auth)])
    async def submit_task(body: TaskSubmit) -> dict:
        task = await orchestrator.submit_task(
            body.prompt,
            priority=body.priority,
            metadata=body.metadata,
            reply_to=body.reply_to,
            target_agent=body.target_agent,
            required_tags=body.required_tags or None,
        )
        result: dict = {"task_id": task.id, "prompt": task.prompt, "priority": task.priority}
        if task.reply_to is not None:
            result["reply_to"] = task.reply_to
        if task.target_agent is not None:
            result["target_agent"] = task.target_agent
        if task.required_tags:
            result["required_tags"] = task.required_tags
        return result

    @app.post("/tasks/batch", summary="Submit multiple tasks in one request", dependencies=[Depends(auth)])
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
        for item in body.tasks:
            task = await orchestrator.submit_task(
                item.prompt,
                priority=item.priority,
                metadata=item.metadata,
                reply_to=item.reply_to,
                target_agent=item.target_agent,
                required_tags=item.required_tags or None,
            )
            record: dict = {"task_id": task.id, "prompt": task.prompt, "priority": task.priority}
            if task.reply_to is not None:
                record["reply_to"] = task.reply_to
            if task.target_agent is not None:
                record["target_agent"] = task.target_agent
            if task.required_tags:
                record["required_tags"] = task.required_tags
            results.append(record)
        return {"tasks": results}

    @app.get("/tasks", summary="List pending tasks", dependencies=[Depends(auth)])
    async def list_tasks() -> list[dict]:
        return orchestrator.list_tasks()

    @app.get("/agents", summary="List agents and their status", dependencies=[Depends(auth)])
    async def list_agents() -> list[dict]:
        return orchestrator.list_agents()

    @app.get("/agents/tree", summary="Agent hierarchy as nested tree", dependencies=[Depends(auth)])
    async def agents_tree() -> list[dict]:
        """Return the agent list as a nested JSON tree (d3-hierarchy compatible).

        Each node has: ``id``, ``status``, ``role``, ``parent_id``,
        ``current_task``, ``bus_drops``, ``circuit_breaker``, ``children``.

        The top level of the returned list contains root-level agents
        (``parent_id == None``); each node's ``children`` list recursively
        contains its sub-agents.
        """
        agents = orchestrator.list_agents()
        return _build_agent_tree(agents)

    @app.delete("/agents/{agent_id}", summary="Stop an agent", dependencies=[Depends(auth)])
    async def stop_agent(agent_id: str) -> AgentKillResponse:
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        await agent.stop()
        return AgentKillResponse(agent_id=agent_id, stopped=True)

    @app.post("/agents/{agent_id}/reset", summary="Manually reset an agent from ERROR state", dependencies=[Depends(auth)])
    async def reset_agent(agent_id: str) -> dict:
        """Stop and restart *agent_id*, clearing ERROR and permanently-failed state.

        Use this endpoint when an agent exhausted automatic recovery attempts
        and needs a manual restart.  Returns 404 if the agent is not registered.

        Design note: ``POST /agents/{id}/reset`` follows the action sub-resource
        pattern — an imperative verb endpoint rather than a PUT state replacement.
        Reference: DESIGN.md §11; Nordic APIs "Designing a True REST State Machine".
        """
        try:
            await orchestrator.reset_agent(agent_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        return {"agent_id": agent_id, "reset": True}

    @app.get(
        "/agents/{agent_id}/stats",
        summary="Per-agent context usage stats",
        dependencies=[Depends(auth)],
    )
    async def agent_context_stats(agent_id: str) -> dict:
        """Return context window usage statistics for *agent_id*.

        Fields:
        - ``pane_chars``: character count of the last captured pane output.
        - ``estimated_tokens``: estimated token count (pane_chars / 4).
        - ``context_window_tokens``: configured total context window size.
        - ``context_pct``: percentage of context window used (0-100+).
        - ``warn_threshold_pct``: threshold at which context_warning is emitted.
        - ``notes_mtime``: mtime of NOTES.md at last check (Unix timestamp).
        - ``notes_updates``: number of NOTES.md changes detected.
        - ``context_warnings``: number of context_warning events emitted.
        - ``summarize_triggers``: number of /summarize auto-injections.
        - ``last_polled``: monotonic timestamp of the last poll cycle.

        Returns 404 if the agent is unknown or not yet tracked by the monitor.

        Design reference: Liu et al. "Lost in the Middle" TACL 2024
        (https://arxiv.org/abs/2307.03172) — context saturation degrades recall;
        monitoring context size enables proactive compression. DESIGN.md §11 (v0.21.0).
        """
        stats = orchestrator.get_agent_context_stats(agent_id)
        if stats is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} context stats not yet available")
        return stats

    @app.get(
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

    @app.get(
        "/agents/{agent_id}/history",
        summary="Per-agent task history",
        dependencies=[Depends(auth)],
    )
    async def agent_history(agent_id: str, limit: int = 50) -> list:
        """Return the last *limit* completed task records for *agent_id*.

        Each record contains:
        - ``task_id``: unique task identifier
        - ``prompt``: the task prompt text
        - ``started_at``: ISO timestamp when the task was dispatched
        - ``finished_at``: ISO timestamp when the RESULT arrived
        - ``duration_s``: wall-clock seconds from dispatch to RESULT
        - ``status``: ``"success"`` or ``"error"``
        - ``error``: error message string, or null on success

        Results are ordered most-recent-first.  Pass ``?limit=N`` to control
        how many records are returned (default 50, capped at 200).

        Design reference: TAMAS (IBM, 2025) "Beyond Black-Box Benchmarking:
        Observability, Analytics, and Optimization of Agentic Systems"
        arXiv:2503.06745 — per-agent task history enables bottleneck analysis.
        Langfuse "AI Agent Observability" (2024): tracing decision paths.
        """
        history = orchestrator.get_agent_history(agent_id, limit=min(limit, 200))
        if history is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        return history

    # ------------------------------------------------------------------
    # Task result persistence (Event Sourcing / CQRS read side)
    # ------------------------------------------------------------------

    @app.get(
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

    @app.get(
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

    @app.post(
        "/workflows",
        summary="Submit a multi-step workflow DAG",
        dependencies=[Depends(auth)],
    )
    async def submit_workflow(body: WorkflowSubmit) -> dict:
        """Submit a named workflow as a directed acyclic graph of tasks.

        Each task in ``tasks`` may reference other tasks in the same submission
        via ``depends_on`` (a list of ``local_id`` strings).  The handler:

        1. Validates the DAG for unknown ``local_id`` references and cycles.
        2. Assigns a global orchestrator task ID to each local node.
        3. Submits tasks to the orchestrator in topological order, translating
           ``depends_on`` local IDs to global task IDs.
        4. Registers all task IDs with the ``WorkflowManager`` for status
           tracking.
        5. Returns the workflow ID and a ``local_id → global_task_id`` mapping.

        Returns 400 on invalid DAG (unknown dependency or cycle).

        Design references:
        - Apache Airflow DAG model — directed acyclic graph of tasks
        - AWS Step Functions — state machine workflow definition
        - Tomasulo's algorithm — register renaming == local_id → task_id mapping
        - Prefect "Modern Data Stack" — submit pipeline as a unit
        - DESIGN.md §10.20 (v0.25.0)
        """
        from tmux_orchestrator.workflow_manager import validate_dag  # noqa: PLC0415

        # Validate and topologically sort
        task_specs = [t.model_dump() for t in body.tasks]
        try:
            ordered = validate_dag(task_specs, local_id_key="local_id", deps_key="depends_on")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # Assign global IDs and submit in dependency order
        local_to_global: dict[str, str] = {}
        global_task_ids: list[str] = []

        for spec in ordered:
            global_deps = [local_to_global[lid] for lid in spec.get("depends_on", [])]
            task = await orchestrator.submit_task(
                spec["prompt"],
                priority=spec.get("priority", 0),
                depends_on=global_deps or None,
                target_agent=spec.get("target_agent"),
                required_tags=spec.get("required_tags") or None,
            )
            local_to_global[spec["local_id"]] = task.id
            global_task_ids.append(task.id)

        # Register with WorkflowManager for status tracking
        wm = orchestrator.get_workflow_manager()
        run = wm.submit(name=body.name, task_ids=global_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": local_to_global,
        }

    @app.get(
        "/workflows",
        summary="List all workflow runs",
        dependencies=[Depends(auth)],
    )
    async def list_workflows() -> list:
        """Return a list of all submitted workflow runs and their current status.

        Each entry contains:
        - ``id``: workflow run UUID
        - ``name``: name given at submission
        - ``task_ids``: ordered list of global orchestrator task IDs
        - ``status``: ``"pending"`` | ``"running"`` | ``"complete"`` | ``"failed"``
        - ``created_at``: Unix timestamp of submission
        - ``completed_at``: Unix timestamp when all tasks finished, or ``null``
        - ``tasks_total``: total number of tasks in the workflow
        - ``tasks_done``: tasks that have finished (succeeded + failed)
        - ``tasks_failed``: tasks that failed

        Design reference: DESIGN.md §10.20 (v0.25.0).
        """
        return orchestrator.get_workflow_manager().list_all()

    @app.get(
        "/workflows/{workflow_id}",
        summary="Get a specific workflow run status",
        dependencies=[Depends(auth)],
    )
    async def get_workflow(workflow_id: str) -> dict:
        """Return the status and task list for *workflow_id*.

        Returns 404 if the workflow ID is unknown.

        Design reference: DESIGN.md §10.20 (v0.25.0).
        """
        result = orchestrator.get_workflow_manager().status(workflow_id)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"Workflow {workflow_id!r} not found",
            )
        return result

    @app.post(
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

        Design reference: Microsoft Azure "Asynchronous Request-Reply pattern"
        (2024): "A client can send an HTTP DELETE request on the URL provided
        by Location header when the task is submitted." We use POST on a verb
        sub-resource (action endpoint) since DELETE on /tasks/{id} could be
        ambiguous with resource deletion semantics.
        DESIGN.md §11 (v0.17.0) — task cancellation.
        """
        # Snapshot the pending queue before attempting cancellation.
        queued_ids = {t["task_id"] for t in orchestrator.list_tasks()}
        was_queued = task_id in queued_ids

        cancelled = await orchestrator.cancel_task(task_id)

        if cancelled:
            return {"cancelled": True, "task_id": task_id, "status": "cancelled"}

        if was_queued:
            # Was in queue but got dispatched between our snapshot and cancel_task().
            # This is a race — treat as already dispatched, not as 404.
            return {"cancelled": False, "task_id": task_id, "status": "already_dispatched"}

        # Task was not in the queue — determine if it was ever tracked.
        # Check in-flight tasks (dispatched but result not yet received).
        in_flight = getattr(orchestrator, "_task_started_at", {})
        if task_id in in_flight:
            return {"cancelled": False, "task_id": task_id, "status": "already_dispatched"}

        # Check completed tasks.
        completed = getattr(orchestrator, "_completed_tasks", set())
        if task_id in completed:
            return {"cancelled": False, "task_id": task_id, "status": "already_dispatched"}

        # Check DLQ — dead-lettered tasks were also "dispatched" in the broad sense.
        dlq_ids = {e.get("task_id") for e in orchestrator.list_dlq()}
        if task_id in dlq_ids:
            return {"cancelled": False, "task_id": task_id, "status": "already_dispatched"}

        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    @app.patch(
        "/tasks/{task_id}",
        summary="Update a pending task's priority",
        dependencies=[Depends(auth)],
    )
    async def update_task_priority(task_id: str, body: TaskPriorityUpdate) -> dict:
        """Update the priority of a task that is still in the pending queue.

        Rebuilds the internal heap after the in-place mutation so the new
        priority is respected on the next dispatch cycle.

        Returns:
        - ``{"updated": true, "task_id": ..., "priority": N}``
          if the task was found in the pending queue and its priority was changed.
        - ``{"updated": false, "task_id": ...}``
          if the task is not in the pending queue (already dispatched, completed,
          or never submitted).

        Design reference: Python heapq "Priority Queue Implementation Notes"
        (https://docs.python.org/3/library/heapq.html); Liu & Layland (1973)
        "Scheduling Algorithms for Multiprogramming in a Hard Real-Time
        Environment", JACM 20(1) — live priority adjustment prevents priority
        inversion and lets operators promote urgent work without re-submitting.
        """
        updated = await orchestrator.update_task_priority(task_id, body.priority)
        if updated:
            return {"updated": True, "task_id": task_id, "priority": body.priority}
        return {"updated": False, "task_id": task_id}

    # ------------------------------------------------------------------
    # Orchestrator dispatch control (pause / resume)
    # ------------------------------------------------------------------

    @app.post(
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

    @app.post(
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

    @app.get(
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

    @app.get(
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

    @app.put(
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

    @app.get(
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

    @app.put(
        "/orchestrator/autoscaler",
        summary="Reconfigure autoscaler parameters",
        dependencies=[Depends(auth)],
    )
    async def put_autoscaler(body: AutoScalerUpdate) -> dict:
        """Update autoscaling parameters at runtime.

        Only supplied fields are changed; omit a field to leave it unchanged.
        Returns 409 when autoscaling is not enabled (``autoscale_max=0``).
        """
        if orchestrator._autoscaler is None:
            raise HTTPException(
                status_code=409,
                detail="Autoscaling is not enabled (autoscale_max=0 in config)",
            )
        result = orchestrator._autoscaler.reconfigure(
            min=body.min,
            max=body.max,
            threshold=body.threshold,
            cooldown=body.cooldown,
        )
        return result

    @app.post("/agents/{agent_id}/message", summary="Send a message to an agent", dependencies=[Depends(auth)])
    async def send_message(agent_id: str, body: SendMessage) -> dict:
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        try:
            msg_type = MessageType[body.type]
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Unknown message type: {body.type!r}")
        msg = Message(
            type=msg_type,
            from_id="__user__",
            to_id=agent_id,
            payload=body.payload,
        )
        await orchestrator.bus.publish(msg)
        return {"message_id": msg.id, "to_id": agent_id}

    @app.post("/agents/new", summary="Create a new agent dynamically (no template required)", dependencies=[Depends(auth)])
    async def create_dynamic_agent(body: DynamicAgentCreate) -> dict:
        """Create and start a new ClaudeCodeAgent with the given parameters.

        Unlike ``POST /agents`` (which requires a pre-configured template_id),
        this endpoint accepts the full agent specification inline so a Director
        agent can spawn specialist workers at runtime.

        Returns the assigned agent ID and a ``"created"`` status.  Returns 409
        if an agent with the requested *agent_id* already exists.
        """
        try:
            agent = await orchestrator.create_agent(
                agent_id=body.agent_id,
                tags=body.tags or [],
                system_prompt=body.system_prompt,
                isolate=body.isolate,
                merge_on_stop=body.merge_on_stop,
                merge_target=body.merge_target,
                command=body.command,
                role=body.role,
                task_timeout=body.task_timeout,
                parent_id=body.parent_id,
            )
            return {"status": "created", "agent_id": agent.id}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/agents", summary="Spawn a sub-agent under a parent agent", dependencies=[Depends(auth)])
    async def spawn_agent(body: SpawnAgent) -> dict:
        parent = orchestrator.get_agent(body.parent_id)
        if parent is None:
            raise HTTPException(
                status_code=404, detail=f"Agent {body.parent_id!r} not found"
            )
        msg = Message(
            type=MessageType.CONTROL,
            from_id=body.parent_id,
            to_id="__orchestrator__",
            payload={
                "action": "spawn_subagent",
                "template_id": body.template_id,
            },
        )
        await orchestrator.bus.publish(msg)
        return {"status": "spawning", "parent_id": body.parent_id, "template_id": body.template_id}

    @app.post("/director/chat", summary="Send a message to the Director agent", dependencies=[Depends(auth)])
    async def director_chat(body: DirectorChat, wait: bool = False) -> dict:
        director = orchestrator.get_director()
        if director is None:
            raise HTTPException(status_code=404, detail="No director agent in this session")

        task_id = str(uuid.uuid4())

        # Prepend any buffered worker results so the Director sees them as context
        pending = orchestrator.flush_director_pending()
        if pending:
            notifications = "\n".join(f"  - {p}" for p in pending)
            prompt = f"[Completed worker tasks since last message]\n{notifications}\n\n{body.message}"
        else:
            prompt = body.message

        task = Task(id=task_id, prompt=prompt, priority=0)

        if wait:
            sub_id = f"__chat_{task_id[:8]}__"
            q = await orchestrator.bus.subscribe(sub_id, broadcast=True)
            await director.send_task(task)
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=300.0)
                    except asyncio.TimeoutError:
                        raise HTTPException(status_code=504, detail="Director response timed out")
                    q.task_done()
                    if msg.type == MessageType.RESULT and msg.payload.get("task_id") == task_id:
                        return {"task_id": task_id, "response": msg.payload.get("output", "")}
            finally:
                await orchestrator.bus.unsubscribe(sub_id)
        else:
            await director.send_task(task)
            return {"task_id": task_id}

    # ------------------------------------------------------------------
    # Shared scratchpad — key/value store for inter-agent data sharing
    # ------------------------------------------------------------------

    @app.get("/scratchpad/", summary="List all scratchpad entries", dependencies=[Depends(auth)])
    async def scratchpad_list() -> dict:
        """Return all scratchpad key-value pairs.

        The shared scratchpad implements the Blackboard architectural pattern
        (Buschmann et al., 1996): a shared working memory that multiple agents
        can read and write independently.  It is especially useful for pipeline
        workflows where one agent writes results that a downstream agent reads.

        Reference: DESIGN.md §11 (architecture) — shared scratchpad (v0.16.0)
        """
        return dict(_scratchpad)

    @app.put(
        "/scratchpad/{key}",
        summary="Write a value to the scratchpad",
        dependencies=[Depends(auth)],
    )
    async def scratchpad_put(key: str, body: ScratchpadWrite) -> dict:
        """Write *value* under *key*.  Creates or overwrites the entry."""
        _scratchpad[key] = body.value
        return {"key": key, "updated": True}

    @app.get(
        "/scratchpad/{key}",
        summary="Read a value from the scratchpad",
        dependencies=[Depends(auth)],
    )
    async def scratchpad_get(key: str) -> dict:
        """Return the value stored under *key*, or 404 if not found."""
        if key not in _scratchpad:
            raise HTTPException(status_code=404, detail=f"Scratchpad key {key!r} not found")
        return {"key": key, "value": _scratchpad[key]}

    @app.delete(
        "/scratchpad/{key}",
        summary="Delete a scratchpad entry",
        dependencies=[Depends(auth)],
    )
    async def scratchpad_delete(key: str) -> dict:
        """Remove *key* from the scratchpad.  Returns 404 if not found."""
        if key not in _scratchpad:
            raise HTTPException(status_code=404, detail=f"Scratchpad key {key!r} not found")
        del _scratchpad[key]
        return {"key": key, "deleted": True}

    # ------------------------------------------------------------------
    # Health probes (no auth required for infrastructure compatibility)
    # ------------------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    async def liveness() -> dict:
        """Liveness probe: returns 200 if the event loop is responsive."""
        return {"status": "ok", "ts": time.time()}

    @app.get("/readyz", include_in_schema=False)
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

    @app.get("/dlq", summary="Dead letter queue", dependencies=[Depends(auth)])
    async def dead_letter_queue() -> list:
        """Return tasks that could not be dispatched after exhausting retries."""
        return orchestrator.list_dlq()

    # ------------------------------------------------------------------
    # Prometheus metrics (no auth — Prometheus scraper compatibility)
    # ------------------------------------------------------------------

    @app.get("/metrics", include_in_schema=False)
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
    # WebSocket — session cookie OR API key query param
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket, key: str = "") -> None:
        session_ok = _valid_session(websocket.cookies.get("session"))
        key_ok = bool(api_key) and key == api_key
        if not session_ok and not key_ok:
            await websocket.close(code=1008)  # Policy Violation
            return
        await hub.handle(websocket)

    # ------------------------------------------------------------------
    # SSE push endpoint — real-time bus event stream
    # ------------------------------------------------------------------

    @app.get(
        "/events",
        summary="Real-time bus event stream (Server-Sent Events)",
        response_class=EventSourceResponse,
        dependencies=[Depends(auth)],
    )
    async def sse_events(request: Request):  # type: ignore[return]
        """Stream all bus events to the client as Server-Sent Events.

        Each event is a JSON object with ``type``, ``from_id``, ``to_id``,
        and ``payload`` fields.  The client can listen with the browser's
        native ``EventSource`` API.

        Authentication: session cookie OR ``X-API-Key`` header / ``?key=`` query parameter.

        Reference:
        - FastAPI SSE (v0.135+): https://fastapi.tiangolo.com/tutorial/server-sent-events/
        - DESIGN.md §10.8 — SSE push notifications (v0.12.0, 2026-03-05)
        """
        sub_id = f"__sse_{id(request)}__"
        q = await orchestrator.bus.subscribe(sub_id, broadcast=True)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Keep-alive comment every 15s to prevent proxy disconnections
                    yield ServerSentEvent(comment="keep-alive")
                    continue
                except asyncio.CancelledError:
                    break
                try:
                    q.task_done()
                    yield ServerSentEvent(
                        data={
                            "type": msg.type.value,
                            "from_id": msg.from_id,
                            "to_id": msg.to_id,
                            "payload": msg.payload,
                        },
                        event=msg.type.value.lower(),
                        id=msg.id,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("SSE: error serialising message %s", msg.id)
        finally:
            await orchestrator.bus.unsubscribe(sub_id)
            logger.debug("SSE: client disconnected, unsubscribed %s", sub_id)

    # ------------------------------------------------------------------
    # Browser UI — unconditional; JS handles auth gate
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def web_ui() -> HTMLResponse:
        return HTMLResponse(_HTML_UI)

    return app


# ---------------------------------------------------------------------------
# Embedded single-page browser UI
# ---------------------------------------------------------------------------

_HTML_UI = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>TmuxAgentOrchestrator</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0d1117;
    color: #c9d1d9;
    height: 100vh;
    display: flex;
    flex-direction: column;
  }
  header {
    background: #161b22;
    border-bottom: 1px solid #30363d;
    padding: 12px 20px;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
  }
  header h1 { font-size: 1.1rem; color: #58a6ff; font-weight: 600; }
  #status-dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: #3fb950; transition: background 0.3s;
  }
  #status-dot.disconnected { background: #f85149; }
  main {
    flex: 1;
    display: grid;
    grid-template-columns: 1fr 1fr;
    grid-template-rows: 1fr 1.6fr;
    gap: 1px;
    background: #30363d;
    overflow: hidden;
    min-height: 0;
  }
  section {
    background: #0d1117;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    min-height: 0;
  }
  .full-width { grid-column: 1 / -1; }
  .section-header {
    background: #161b22;
    padding: 8px 14px;
    font-size: 0.8rem;
    font-weight: 600;
    color: #8b949e;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    border-bottom: 1px solid #30363d;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-shrink: 0;
  }
  .badge {
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 1px 7px;
    font-size: 0.7rem;
    color: #8b949e;
  }
  .badge.director { background: #1f3447; border-color: #58a6ff; color: #58a6ff; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  thead th {
    background: #161b22;
    padding: 6px 12px;
    text-align: left;
    font-weight: 500;
    color: #8b949e;
    font-size: 0.75rem;
    position: sticky;
    top: 0;
  }
  tbody tr:hover { background: #161b22; }
  tbody td { padding: 6px 12px; border-bottom: 1px solid #21262d; }
  .tbl-wrap { overflow-y: auto; flex: 1; min-height: 0; }
  .status-idle    { color: #3fb950; }
  .status-busy    { color: #e3b341; }
  .status-error   { color: #f85149; }
  .status-stopped { color: #6e7681; }
  .role-director  { color: #58a6ff; font-size: 0.7rem; margin-left: 4px; }

  /* ── View toggle (table / tree) ── */
  .view-toggle {
    display: flex;
    gap: 4px;
  }
  .view-btn {
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 4px;
    padding: 2px 10px;
    font-size: 0.72rem;
    color: #8b949e;
    cursor: pointer;
  }
  .view-btn.active {
    background: #1f6feb;
    border-color: #388bfd;
    color: #fff;
  }

  /* ── Agent tree view ── */
  #agents-tree {
    overflow-y: auto;
    flex: 1;
    padding: 8px 14px;
    min-height: 0;
    display: none; /* hidden by default; shown when tree view is active */
  }
  .tree-node {
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .tree-node li {
    position: relative;
    padding-left: 18px;
    margin: 2px 0;
  }
  .tree-node li::before {
    content: '';
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    border-left: 1px solid #30363d;
  }
  .tree-node li:last-child::before {
    height: 0.8em;
  }
  .tree-node li::after {
    content: '';
    position: absolute;
    left: 0;
    top: 0.8em;
    width: 14px;
    border-top: 1px solid #30363d;
  }
  .tree-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 3px 6px;
    border-radius: 4px;
    font-size: 0.82rem;
    cursor: default;
  }
  .tree-item:hover { background: #161b22; }
  .tree-item-id { font-weight: 600; font-family: monospace; }
  .tree-item-role { font-size: 0.7rem; color: #8b949e; padding: 1px 5px; border-radius: 3px; background: #21262d; }
  .tree-item-role.director { color: #58a6ff; background: #1f3447; }

  /* ── Director Chat ── */
  #chat-section { display: none; }
  #chat-history {
    flex: 1;
    overflow-y: auto;
    padding: 10px 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-height: 0;
  }
  .chat-bubble {
    max-width: 80%;
    padding: 8px 12px;
    border-radius: 10px;
    font-size: 0.85rem;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .bubble-user {
    align-self: flex-end;
    background: #1f6feb;
    color: #fff;
    border-bottom-right-radius: 2px;
  }
  .bubble-director {
    align-self: flex-start;
    background: #21262d;
    border: 1px solid #30363d;
    color: #c9d1d9;
    border-bottom-left-radius: 2px;
  }
  .bubble-thinking {
    align-self: flex-start;
    background: #161b22;
    border: 1px dashed #30363d;
    color: #6e7681;
    font-style: italic;
    font-size: 0.8rem;
  }
  #chat-input-row {
    display: flex;
    gap: 8px;
    padding: 8px 10px;
    border-top: 1px solid #30363d;
    flex-shrink: 0;
  }
  #chat-input {
    flex: 1;
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 7px 11px;
    color: #c9d1d9;
    font-size: 0.9rem;
    outline: none;
  }
  #chat-input:focus { border-color: #58a6ff; }
  #chat-send-btn {
    background: #1f6feb;
    color: #fff;
    border: none;
    border-radius: 6px;
    padding: 7px 16px;
    cursor: pointer;
    font-size: 0.85rem;
    font-weight: 500;
  }
  #chat-send-btn:hover { background: #388bfd; }
  #chat-send-btn:disabled { background: #30363d; cursor: default; color: #6e7681; }

  /* ── Event Log ── */
  #log-list {
    overflow-y: auto;
    flex: 1;
    padding: 8px 14px;
    font-size: 0.8rem;
    font-family: 'Consolas', monospace;
    min-height: 0;
  }
  .log-entry {
    display: flex;
    gap: 10px;
    padding: 2px 0;
    border-bottom: 1px solid #21262d11;
  }
  .log-ts   { color: #6e7681; flex-shrink: 0; }
  .log-type { font-weight: 600; flex-shrink: 0; min-width: 60px; }
  .type-RESULT   { color: #3fb950; }
  .type-STATUS   { color: #58a6ff; }
  .type-PEER_MSG { color: #bc8cff; }
  .type-TASK     { color: #e3b341; }
  .type-CONTROL  { color: #f0883e; }

  /* ── Footer ── */
  footer {
    background: #161b22;
    border-top: 1px solid #30363d;
    padding: 10px 20px;
    display: flex;
    gap: 8px;
    align-items: center;
    flex-shrink: 0;
  }
  #task-input {
    flex: 1;
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 8px 12px;
    color: #c9d1d9;
    font-size: 0.9rem;
    outline: none;
  }
  #task-input:focus { border-color: #58a6ff; }
  button {
    background: #238636;
    color: #fff;
    border: none;
    border-radius: 6px;
    padding: 8px 16px;
    cursor: pointer;
    font-size: 0.85rem;
    font-weight: 500;
    transition: background 0.2s;
  }
  button:hover { background: #2ea043; }
  #priority-input {
    width: 70px;
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 8px 10px;
    color: #c9d1d9;
    font-size: 0.9rem;
    outline: none;
  }
  .empty-hint { color: #6e7681; font-size: 0.8rem; padding: 12px; text-align: center; }

  /* ── Agent Conversations ── */
  #conv-list {
    overflow-y: auto;
    flex: 1;
    padding: 6px 14px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-height: 0;
  }
  .conv-entry {
    display: flex;
    gap: 8px;
    align-items: baseline;
    font-size: 0.82rem;
    padding: 4px 0;
    border-bottom: 1px solid #21262d33;
    flex-wrap: wrap;
  }
  .conv-ts   { color: #6e7681; flex-shrink: 0; font-size: 0.72rem; font-family: monospace; }
  .conv-from { font-weight: 700; flex-shrink: 0; }
  .conv-arrow{ color: #6e7681; flex-shrink: 0; }
  .conv-to   { font-weight: 700; flex-shrink: 0; }
  .conv-sep  { color: #6e7681; flex-shrink: 0; }
  .conv-content { color: #c9d1d9; word-break: break-word; flex: 1; min-width: 0; }

  /* ── Auth Overlay ── */
  #auth-overlay {
    position: fixed;
    inset: 0;
    background: rgba(13, 17, 23, 0.97);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }
  #auth-box {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 12px;
    padding: 40px 48px;
    text-align: center;
    max-width: 360px;
    width: 100%;
  }
  #auth-box h2 { color: #58a6ff; margin-bottom: 12px; font-size: 1.3rem; }
  #auth-box > p { color: #8b949e; font-size: 0.9rem; margin-bottom: 24px; }
  #auth-error { color: #f85149; font-size: 0.85rem; margin-top: 12px; min-height: 1.2em; }
  .auth-btn { display: block; width: 100%; margin-bottom: 12px; padding: 10px 20px; font-size: 0.95rem; }
</style>
</head>
<body>

<!-- Auth overlay (shown when unauthenticated) -->
<div id="auth-overlay" style="display:none">
  <div id="auth-box">
    <h2>TmuxAgentOrchestrator</h2>
    <p id="auth-msg">Authenticating…</p>
    <button id="btn-register" class="auth-btn" onclick="registerPasskey()" style="display:none">Register Passkey</button>
    <button id="btn-authenticate" class="auth-btn" onclick="authenticatePasskey()" style="display:none">Sign in with Passkey</button>
    <p id="auth-error"></p>
  </div>
</div>

<header>
  <div id="status-dot" class="disconnected"></div>
  <h1>TmuxAgentOrchestrator</h1>
  <span id="conn-label" style="font-size:0.8rem;color:#8b949e">Connecting…</span>
  <button id="btn-signout" onclick="signOut()" style="margin-left:auto;padding:4px 12px;font-size:0.8rem;background:#21262d;border:1px solid #30363d;color:#c9d1d9;display:none">Sign Out</button>
</header>

<main>
  <!-- Agents panel -->
  <section>
    <div class="section-header">
      Agents <span id="agent-count" class="badge">0</span>
      <div class="view-toggle">
        <button class="view-btn active" id="btn-table-view" onclick="setAgentView('table')">List</button>
        <button class="view-btn" id="btn-tree-view" onclick="setAgentView('tree')">Tree</button>
      </div>
    </div>
    <div class="tbl-wrap" id="agents-table-wrap">
      <table>
        <thead><tr><th>ID</th><th>Role</th><th>Status</th><th>Task</th></tr></thead>
        <tbody id="agents-body"><tr><td colspan="4" class="empty-hint">Loading…</td></tr></tbody>
      </table>
    </div>
    <div id="agents-tree"></div>
  </section>

  <!-- Task queue panel -->
  <section>
    <div class="section-header">
      Task Queue <span id="task-count" class="badge">0</span>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr><th>Pri</th><th>ID</th><th>Prompt</th></tr></thead>
        <tbody id="tasks-body"><tr><td colspan="3" class="empty-hint">Loading…</td></tr></tbody>
      </table>
    </div>
  </section>

  <!-- Director Chat panel (shown only when a director agent exists) -->
  <section id="chat-section" class="full-width">
    <div class="section-header">
      Director Chat
      <span id="director-id-badge" class="badge director">—</span>
    </div>
    <div id="chat-history"></div>
    <div id="chat-input-row">
      <input id="chat-input" type="text" placeholder="Message the Director… (Enter to send)" autocomplete="off" />
      <button id="chat-send-btn" onclick="sendChat()">Send</button>
    </div>
  </section>

  <!-- Agent Conversations panel -->
  <section>
    <div class="section-header">
      Agent Conversations
      <span id="conv-count" class="badge">0</span>
    </div>
    <div id="conv-list"><div class="empty-hint">No P2P messages yet</div></div>
  </section>

  <!-- Event Log panel -->
  <section>
    <div class="section-header">
      Event Log
      <button onclick="clearLog()" style="padding:2px 10px;font-size:0.75rem;background:#21262d;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;">Clear</button>
    </div>
    <div id="log-list"></div>
  </section>
</main>

<footer>
  <input id="task-input" type="text" placeholder="Submit worker task…" />
  <input id="priority-input" type="number" value="0" min="0" title="Priority" />
  <button onclick="submitTask()">Submit Task</button>
</footer>

<script>
const API_BASE = '';

// ── Base64url helpers ──
function b64urlToBuffer(s) {
  const b = s.replace(/-/g, '+').replace(/_/g, '/')
    .padEnd(s.length + (4 - s.length % 4) % 4, '=');
  return Uint8Array.from(atob(b), c => c.charCodeAt(0)).buffer;
}
function bufferToB64url(buf) {
  return btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}

// ── SSE push notifications ──
// Replaces 3-second polling for agent/task state changes.
// Reference: DESIGN.md §10.8; FastAPI SSE (v0.135+).
let _sseSource = null;

function connectSSE() {
  if (_sseSource && _sseSource.readyState !== EventSource.CLOSED) return;
  _sseSource = new EventSource('/events');
  _sseSource.onopen = () => {
    document.getElementById('status-dot').classList.remove('disconnected');
    document.getElementById('conn-label').textContent = 'Connected (SSE)';
  };
  _sseSource.onerror = () => {
    document.getElementById('status-dot').classList.add('disconnected');
    document.getElementById('conn-label').textContent = 'SSE reconnecting…';
    // EventSource auto-reconnects; we keep a fallback poll in case it stays broken
  };
  // On any STATUS or RESULT event, refresh agent/task tables immediately
  _sseSource.addEventListener('status', (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    if (typeof data === 'string') { try { data = JSON.parse(data); } catch { return; } }
    logEntry({type: 'STATUS', from_id: data.from_id, payload: data.payload, timestamp: new Date().toISOString()});
    refreshAgents();
    refreshTasks();
    refreshAgentTree();
  });
  _sseSource.addEventListener('result', (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    if (typeof data === 'string') { try { data = JSON.parse(data); } catch { return; } }
    logEntry({type: 'RESULT', from_id: data.from_id, payload: data.payload, timestamp: new Date().toISOString()});
    refreshAgents();
    // Director response via SSE
    if (pendingChats.has(data.payload?.task_id)) {
      const bubble = pendingChats.get(data.payload.task_id);
      pendingChats.delete(data.payload.task_id);
      const output = (data.payload && data.payload.output) || '';
      bubble.className = 'chat-bubble bubble-director';
      bubble.textContent = output;
      scrollChat();
      document.getElementById('chat-send-btn').disabled = false;
      document.getElementById('chat-input').disabled = false;
    }
  });
  _sseSource.addEventListener('peer_msg', (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    if (typeof data === 'string') { try { data = JSON.parse(data); } catch { return; } }
    if (data.payload && data.payload._forwarded) {
      addConversationEntry({type: 'PEER_MSG', from_id: data.from_id, to_id: data.to_id, payload: data.payload});
    }
  });
}

function disconnectSSE() {
  if (_sseSource) { _sseSource.close(); _sseSource = null; }
}

// ── Auth ──
let _pollInterval = null;

async function checkAuth() {
  const status = await fetch('/auth/status').then(r => r.json());
  const overlay = document.getElementById('auth-overlay');
  const btnReg = document.getElementById('btn-register');
  const btnAuth = document.getElementById('btn-authenticate');
  const btnSignout = document.getElementById('btn-signout');
  document.getElementById('auth-error').textContent = '';

  if (status.authenticated) {
    overlay.style.display = 'none';
    btnSignout.style.display = 'inline-block';
    if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
      connectWS();
    }
    // SSE replaces polling for real-time agent/task updates
    connectSSE();
    refreshAgents();
    refreshTasks();
    refreshAgentTree();
    // Keep a light 30s fallback poll in case SSE misses an event
    if (!_pollInterval) {
      _pollInterval = setInterval(() => { refreshAgents(); refreshTasks(); }, 30000);
    }
  } else {
    overlay.style.display = 'flex';
    btnSignout.style.display = 'none';
    disconnectSSE();
    if (_pollInterval) { clearInterval(_pollInterval); _pollInterval = null; }
    if (!status.registered) {
      document.getElementById('auth-msg').textContent = 'No passkey registered yet.';
      btnReg.style.display = 'block';
      btnAuth.style.display = 'none';
    } else {
      document.getElementById('auth-msg').textContent = 'Sign in with your passkey.';
      btnReg.style.display = 'none';
      btnAuth.style.display = 'block';
    }
  }
}

async function registerPasskey() {
  document.getElementById('auth-error').textContent = '';
  try {
    const opts = await fetch('/auth/register-options', {method: 'POST'}).then(r => r.json());
    opts.challenge = b64urlToBuffer(opts.challenge);
    opts.user.id = b64urlToBuffer(opts.user.id);
    const cred = await navigator.credentials.create({publicKey: opts});
    const body = JSON.stringify({
      id: cred.id,
      rawId: bufferToB64url(cred.rawId),
      type: cred.type,
      response: {
        clientDataJSON: bufferToB64url(cred.response.clientDataJSON),
        attestationObject: bufferToB64url(cred.response.attestationObject),
      },
    });
    const resp = await fetch('/auth/register', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({detail: resp.statusText}));
      document.getElementById('auth-error').textContent = err.detail || 'Registration failed';
      return;
    }
    await checkAuth();
  } catch (e) {
    document.getElementById('auth-error').textContent = e.message || String(e);
  }
}

async function authenticatePasskey() {
  document.getElementById('auth-error').textContent = '';
  try {
    const opts = await fetch('/auth/authenticate-options', {method: 'POST'}).then(r => r.json());
    opts.challenge = b64urlToBuffer(opts.challenge);
    if (opts.allowCredentials) {
      opts.allowCredentials = opts.allowCredentials.map(c => ({...c, id: b64urlToBuffer(c.id)}));
    }
    const cred = await navigator.credentials.get({publicKey: opts});
    const body = JSON.stringify({
      id: cred.id,
      rawId: bufferToB64url(cred.rawId),
      type: cred.type,
      response: {
        clientDataJSON: bufferToB64url(cred.response.clientDataJSON),
        authenticatorData: bufferToB64url(cred.response.authenticatorData),
        signature: bufferToB64url(cred.response.signature),
        userHandle: cred.response.userHandle ? bufferToB64url(cred.response.userHandle) : null,
      },
    });
    const resp = await fetch('/auth/authenticate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({detail: resp.statusText}));
      document.getElementById('auth-error').textContent = err.detail || 'Authentication failed';
      return;
    }
    await checkAuth();
  } catch (e) {
    document.getElementById('auth-error').textContent = e.message || String(e);
  }
}

async function signOut() {
  await fetch('/auth/logout', {method: 'POST'});
  if (ws) { ws.close(); ws = null; }
  disconnectSSE();
  if (_pollInterval) { clearInterval(_pollInterval); _pollInterval = null; }
  await checkAuth();
}

// pending chat task_ids waiting for RESULT
const pendingChats = new Map(); // task_id -> bubble element

// ── WebSocket ──
let ws;
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const wsUrl = `${proto}://${location.host}/ws`;
  ws = new WebSocket(wsUrl);
  ws.onopen = () => {
    document.getElementById('status-dot').classList.remove('disconnected');
    document.getElementById('conn-label').textContent = 'Connected';
    logEntry({type:'CONTROL', from_id:'system', payload:{msg:'WebSocket connected'}, timestamp: new Date().toISOString()});
  };
  ws.onclose = () => {
    document.getElementById('status-dot').classList.add('disconnected');
    document.getElementById('conn-label').textContent = 'Disconnected — retrying…';
    if (_pollInterval) {  // only retry while authenticated (polling is active)
      setTimeout(connectWS, 3000);
    }
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    logEntry(msg);
    if (['RESULT','STATUS','CONTROL'].includes(msg.type)) {
      refreshAgents();
      refreshTasks();
      refreshAgentTree();
    }
    // Director response
    if (msg.type === 'RESULT' && pendingChats.has(msg.payload?.task_id)) {
      const bubble = pendingChats.get(msg.payload.task_id);
      pendingChats.delete(msg.payload.task_id);
      const output = msg.payload.output || '';
      bubble.className = 'chat-bubble bubble-director';
      bubble.textContent = output;
      scrollChat();
      document.getElementById('chat-send-btn').disabled = false;
      document.getElementById('chat-input').disabled = false;
    }
    // Agent P2P conversation (only show forwarded = actually delivered)
    if (msg.type === 'PEER_MSG' && msg.payload?._forwarded) {
      addConversationEntry(msg);
    }
  };
}

// ── Agent view toggle (list / tree) ──
let _agentView = 'table'; // 'table' or 'tree'

function setAgentView(mode) {
  _agentView = mode;
  const tableWrap = document.getElementById('agents-table-wrap');
  const treeWrap = document.getElementById('agents-tree');
  const btnTable = document.getElementById('btn-table-view');
  const btnTree = document.getElementById('btn-tree-view');
  if (mode === 'tree') {
    tableWrap.style.display = 'none';
    treeWrap.style.display = 'block';
    btnTable.classList.remove('active');
    btnTree.classList.add('active');
    refreshAgentTree();
  } else {
    tableWrap.style.display = '';
    treeWrap.style.display = 'none';
    btnTable.classList.add('active');
    btnTree.classList.remove('active');
  }
}

function statusClass(s) { return 'status-' + (s || 'stopped').toLowerCase(); }

function renderTreeNodes(nodes, depth) {
  if (!nodes || nodes.length === 0) return '';
  const items = nodes.map(node => {
    const sc = statusClass(node.status);
    const roleClass = node.role === 'director' ? 'director' : '';
    const taskHint = node.current_task
      ? `<span style="color:#6e7681;font-size:0.72rem">task:${esc(node.current_task.slice(0,8))}</span>` : '';
    const childHtml = node.children && node.children.length > 0
      ? `<ul class="tree-node">${renderTreeNodes(node.children, depth + 1)}</ul>`
      : '';
    return `<li>
      <div class="tree-item">
        <span class="tree-item-id">${esc(node.id)}</span>
        <span class="tree-item-role ${roleClass}">${esc(node.role || 'worker')}</span>
        <span class="${sc}">${esc(node.status)}</span>
        ${taskHint}
      </div>
      ${childHtml}
    </li>`;
  });
  return items.join('');
}

function refreshAgentTree() {
  if (_agentView !== 'tree') return;
  fetch(`${API_BASE}/agents/tree`)
    .then(r => {
      if (r.status === 401) { checkAuth(); return null; }
      return r.json();
    })
    .then(roots => {
      if (!roots) return;
      const wrap = document.getElementById('agents-tree');
      if (roots.length === 0) {
        wrap.innerHTML = '<div class="empty-hint">No agents</div>';
        return;
      }
      wrap.innerHTML = `<ul class="tree-node" style="padding-left:8px;margin-top:6px">${renderTreeNodes(roots, 0)}</ul>`;
    }).catch(console.error);
}

// ── Polling ──
let directorId = null;

function refreshAgents() {
  fetch(`${API_BASE}/agents`)
    .then(r => {
      if (r.status === 401) { checkAuth(); return null; }
      return r.json();
    })
    .then(agents => {
      if (!agents) return;
      document.getElementById('agent-count').textContent = agents.length;
      const body = document.getElementById('agents-body');
      if (agents.length === 0) {
        body.innerHTML = '<tr><td colspan="4" class="empty-hint">No agents</td></tr>';
        return;
      }
      body.innerHTML = agents.map(a => {
        const sc = 'status-' + a.status.toLowerCase();
        const roleLabel = a.role === 'director'
          ? '<span class="role-director">[director]</span>' : '';
        return `<tr>
          <td>${esc(a.id)}${roleLabel}</td>
          <td>${esc(a.role || 'worker')}</td>
          <td class="${sc}">${esc(a.status)}</td>
          <td>${a.current_task ? esc(a.current_task.slice(0,8)) : '—'}</td>
        </tr>`;
      }).join('');

      // Show/hide chat panel based on whether a director exists
      const director = agents.find(a => a.role === 'director');
      const chatSection = document.getElementById('chat-section');
      if (director && !directorId) {
        directorId = director.id;
        document.getElementById('director-id-badge').textContent = director.id;
        chatSection.style.display = 'flex';
        document.querySelector('main').style.gridTemplateRows = '1fr 280px 1.6fr';
      } else if (!director && directorId) {
        directorId = null;
        chatSection.style.display = 'none';
        document.querySelector('main').style.gridTemplateRows = '1fr 1.6fr';
      }
    }).catch(console.error);
}

function refreshTasks() {
  fetch(`${API_BASE}/tasks`)
    .then(r => {
      if (r.status === 401) { checkAuth(); return null; }
      return r.json();
    })
    .then(tasks => {
      if (!tasks) return;
      document.getElementById('task-count').textContent = tasks.length;
      const body = document.getElementById('tasks-body');
      if (tasks.length === 0) {
        body.innerHTML = '<tr><td colspan="3" class="empty-hint">Queue empty</td></tr>';
        return;
      }
      body.innerHTML = tasks.map(t => `<tr>
        <td>${esc(String(t.priority))}</td>
        <td>${esc(t.task_id.slice(0,8))}</td>
        <td>${esc(t.prompt.slice(0,60))}${t.prompt.length > 60 ? '…' : ''}</td>
      </tr>`).join('');
    }).catch(console.error);
}

// ── Agent Conversations ──
let convTotal = 0;

function agentColor(id) {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) & 0xffff;
  return `hsl(${h % 360}, 65%, 65%)`;
}

function addConversationEntry(msg) {
  const list = document.getElementById('conv-list');
  if (convTotal === 0) list.innerHTML = ''; // clear placeholder
  convTotal++;
  document.getElementById('conv-count').textContent = convTotal;

  const content = msg.payload?.content
    || (msg.payload ? JSON.stringify(msg.payload).replace(/"_forwarded":true,?\s*/g, '') : '');
  const ts = new Date(msg.timestamp).toLocaleTimeString();

  const div = document.createElement('div');
  div.className = 'conv-entry';
  div.innerHTML =
    `<span class="conv-ts">${ts}</span>` +
    `<span class="conv-from" style="color:${agentColor(msg.from_id)}">${esc(msg.from_id)}</span>` +
    `<span class="conv-arrow">→</span>` +
    `<span class="conv-to" style="color:${agentColor(msg.to_id || '')}">${esc(msg.to_id || '*')}</span>` +
    `<span class="conv-sep">│</span>` +
    `<span class="conv-content">${esc(String(content).slice(0, 300))}</span>`;
  list.appendChild(div);
  list.scrollTop = list.scrollHeight;
}

// ── Director Chat ──
function scrollChat() {
  const h = document.getElementById('chat-history');
  h.scrollTop = h.scrollHeight;
}

function addBubble(text, role) {
  const div = document.createElement('div');
  div.className = 'chat-bubble bubble-' + role;
  div.textContent = text;
  document.getElementById('chat-history').appendChild(div);
  scrollChat();
  return div;
}

async function sendChat() {
  if (!directorId) return;
  const inp = document.getElementById('chat-input');
  const btn = document.getElementById('chat-send-btn');
  const message = inp.value.trim();
  if (!message) { inp.focus(); return; }

  inp.value = '';
  inp.disabled = true;
  btn.disabled = true;

  addBubble(message, 'user');
  const thinkingBubble = addBubble('Thinking…', 'thinking');

  try {
    const resp = await fetch(`${API_BASE}/director/chat`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message})
    });
    if (!resp.ok) {
      thinkingBubble.className = 'chat-bubble bubble-director';
      thinkingBubble.textContent = 'Error: ' + resp.statusText;
      btn.disabled = false;
      inp.disabled = false;
      return;
    }
    const data = await resp.json();
    // Register for WebSocket result
    pendingChats.set(data.task_id, thinkingBubble);
  } catch (e) {
    thinkingBubble.className = 'chat-bubble bubble-director';
    thinkingBubble.textContent = 'Error: ' + e.message;
    btn.disabled = false;
    inp.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('chat-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
  document.getElementById('task-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') submitTask();
  });
  checkAuth();
});

// ── Worker Task submit ──
async function submitTask() {
  const inp = document.getElementById('task-input');
  const pri = document.getElementById('priority-input');
  const prompt = inp.value.trim();
  if (!prompt) { inp.focus(); return; }
  const resp = await fetch(`${API_BASE}/tasks`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({prompt, priority: parseInt(pri.value || '0', 10)})
  });
  if (resp.ok) { inp.value = ''; refreshTasks(); }
  else alert('Failed: ' + resp.statusText);
}

// ── Log ──
const MAX_LOG = 200;
function logEntry(msg) {
  const list = document.getElementById('log-list');
  const ts = new Date(msg.timestamp).toLocaleTimeString();
  const div = document.createElement('div');
  div.className = 'log-entry';
  const payload = JSON.stringify(msg.payload || {});
  div.innerHTML = `<span class="log-ts">${ts}</span>
    <span class="log-type type-${msg.type}">${msg.type}</span>
    <span>${esc(msg.from_id)} → ${esc(msg.to_id || '*')}: ${esc(payload.slice(0,120))}</span>`;
  list.prepend(div);
  while (list.children.length > MAX_LOG) list.removeChild(list.lastChild);
}
function clearLog() { document.getElementById('log-list').innerHTML = ''; }

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
</script>
</body>
</html>
"""
