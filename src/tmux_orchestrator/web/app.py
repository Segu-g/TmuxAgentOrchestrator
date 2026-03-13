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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from tmux_orchestrator.webhook_manager import WebhookManager

import webauthn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
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
from tmux_orchestrator.episode_store import EpisodeNotFoundError, EpisodeStore
from tmux_orchestrator.schemas import Episode, EpisodeCreate
from tmux_orchestrator.web.ws import WebSocketHub

# ---------------------------------------------------------------------------
# Pydantic schemas — extracted to web/schemas.py in v1.1.5 for modularity.
# Re-exported here for backward compatibility.
# Reference: DESIGN.md §10.41 (v1.1.5); FastAPI "Bigger Applications" guide.
# ---------------------------------------------------------------------------
from tmux_orchestrator.web.schemas import (  # noqa: E402
    AgentBriefRequest,
    AgentKillResponse,
    AgentSelectorModel,
    AdrWorkflowSubmit,
    AutoScalerUpdate,
    ChangeStrategyRequest,
    CleanArchWorkflowSubmit,
    CompetitionWorkflowSubmit,
    DDDWorkflowSubmit,
    DebateWorkflowSubmit,
    DelphiWorkflowSubmit,
    DirectorChat,
    DynamicAgentCreate,
    FulldevWorkflowSubmit,
    GroupAddAgent,
    GroupCreate,
    PairWorkflowSubmit,
    PhaseSpecModel,
    RateLimitUpdate,
    RedBlueWorkflowSubmit,
    ScratchpadWrite,
    SendMessage,
    SocraticWorkflowSubmit,
    SpawnAgent,
    TaskBatchItem,
    TaskBatchSubmit,
    TaskCompleteBody,
    TaskPriorityUpdate,
    TaskSubmit,
    TddWorkflowSubmit,
    WebhookCreate,
    WorkflowSubmit,
    WorkflowTaskSpec,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level auth state
# ---------------------------------------------------------------------------

_credentials: dict[str, bytes] = {}    # b64url(cred_id) → public_key bytes
_sign_counts: dict[str, int] = {}      # b64url(cred_id) → sign_count
_sessions: dict[str, float] = {}       # session_token  → expiry (unix ts)
_pending_challenge: bytes | None = None
_SESSION_TTL = 86_400  # 24 h

# ---------------------------------------------------------------------------
# Shared scratchpad — write-through persistent key/value store (v1.2.1+)
#
# Replaced bare dict with ScratchpadStore for file persistence (write-through).
# The store is module-level so it is shared across all create_app() calls in
# the same process (matches the original dict semantics for tests).
#
# The persist_dir is configured lazily in create_app() via _init_scratchpad().
# This avoids importing ScratchpadStore at module level (keeps import cost low).
#
# Reference: DESIGN.md §10.77 (v1.2.1)
# ---------------------------------------------------------------------------

from tmux_orchestrator.application.scratchpad_store import ScratchpadStore  # noqa: E402

_scratchpad: ScratchpadStore = ScratchpadStore()  # in-memory until create_app() wires persist_dir


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
    cors_origins: list[str] | None = None,
    rate_limit: str = "60/minute",
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
    cors_origins:
        List of allowed CORS origins.  When ``None`` (default), defaults to
        loopback-only: ``["http://localhost", "http://localhost:8000",
        "http://127.0.0.1", "http://127.0.0.1:8000"]``.
        Reference: OWASP CORS cheat sheet; DESIGN.md §10.18 (v0.44.0).
    rate_limit:
        Global rate limit string for SlowAPI (default ``"60/minute"``).
        Applied to all ``POST /tasks`` submissions.
        Reference: SlowAPI docs; DESIGN.md §10.18 (v0.44.0).
    """
    from fastapi.middleware.cors import CORSMiddleware
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    from tmux_orchestrator.security import AuditLogMiddleware

    auth = _make_combined_auth(api_key)

    # Rate limiter (SlowAPI / token bucket)
    # Reference: SlowAPI docs https://slowapi.readthedocs.io/ (2025)
    _limiter = Limiter(key_func=get_remote_address)

    # Effective CORS origins — loopback-only by default
    _cors_origins: list[str] = cors_origins if cors_origins is not None else [
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:8000",
    ]

    @asynccontextmanager
    async def _lifespan(application: FastAPI):  # noqa: ARG001
        await hub.start()
        logger.info("WebSocket hub started")
        # Signal the orchestrator to defer agent process startup so that
        # start_agents() can be called AFTER the server begins accepting
        # requests.  This prevents the SessionStart hook deadlock: agents call
        # POST /agents/{id}/ready via curl, which requires the HTTP server to
        # be up first.
        if hasattr(orchestrator, "_defer_agent_start"):
            orchestrator._defer_agent_start = True
        if on_startup is not None:
            await on_startup()
        # Server is now accepting requests.  Start agent processes in the
        # background so their SessionStart hooks can reach this server.
        _agents_task: asyncio.Task | None = None
        if hasattr(orchestrator, "start_agents"):
            _agents_task = asyncio.create_task(
                orchestrator.start_agents(),
                name="orchestrator-agent-startup",
            )
        yield
        # Cancel in-flight agent startups on shutdown.
        if _agents_task and not _agents_task.done():
            _agents_task.cancel()
            await asyncio.gather(_agents_task, return_exceptions=True)
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
    # Security middleware (CORS + Audit log)
    # Reference: DESIGN.md §10.18 (v0.44.0)
    # ------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(AuditLogMiddleware)

    # Rate limiter state + exception handler
    app.state.limiter = _limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

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
    # APIRouter registration — endpoints are defined in web/routers/
    # Reference: DESIGN.md §10.42 (v1.1.6)
    # FastAPI "Bigger Applications": https://fastapi.tiangolo.com/tutorial/bigger-applications/
    # ------------------------------------------------------------------

    from tmux_orchestrator.web.routers import (  # noqa: PLC0415
        build_agents_router,
        build_groups_router,
        build_memory_router,
        build_scratchpad_router,
        build_system_router,
        build_tasks_router,
        build_webhooks_router,
        build_workflows_router,
    )

    # ------------------------------------------------------------------
    # Scratchpad store — write-through persistent KV store (v1.2.1+)
    # Reinitialise module-level _scratchpad with persist_dir from config.
    # When config is absent (e.g. unit tests with a mock orchestrator that
    # has no config attribute), fall back to in-memory store (no files).
    # Reference: DESIGN.md §10.77 (v1.2.1)
    # ------------------------------------------------------------------
    global _scratchpad
    _orch_config_pre = getattr(orchestrator, "config", None)
    _scratchpad_dir_raw: str | None = getattr(_orch_config_pre, "scratchpad_dir", None)
    if _scratchpad_dir_raw is not None:
        _scratchpad = ScratchpadStore(persist_dir=Path(_scratchpad_dir_raw))
    # If no config, leave the existing module-level ScratchpadStore() in place.

    # Wire scratchpad + cancel function into WorkflowManager for loop-until
    # runtime evaluation (v1.2.7).
    # The WorkflowManager evaluates the until condition against the live
    # scratchpad after each loop-iteration completes; when the condition is
    # met, it calls the cancel function to remove remaining tasks.
    # Reference: DESIGN.md §10.83 (v1.2.7)
    if hasattr(orchestrator, "get_workflow_manager"):
        _wm = orchestrator.get_workflow_manager()
        if _wm is not None and hasattr(_wm, "set_scratchpad"):
            _wm.set_scratchpad(_scratchpad)
            if hasattr(orchestrator, "cancel_task"):
                import asyncio as _asyncio  # noqa: PLC0415

                def _sync_cancel(task_id: str) -> None:
                    """Schedule async cancellation from the sync WorkflowManager callback."""
                    try:
                        loop = _asyncio.get_event_loop()
                        if loop.is_running():
                            _asyncio.ensure_future(orchestrator.cancel_task(task_id))
                    except RuntimeError:
                        pass  # No event loop — ignore (e.g. during unit tests)

                _wm.set_cancel_task_fn(_sync_cancel)

    # Wire phase webhook callback into WorkflowManager (v1.2.9).
    # Delivers phase_complete / phase_failed / phase_skipped events to all
    # registered webhooks when a workflow phase transitions to a terminal state.
    # Reference: DESIGN.md §10.85 (v1.2.9)
    if hasattr(orchestrator, "get_workflow_manager"):
        _wm2 = orchestrator.get_workflow_manager()
        if _wm2 is not None and hasattr(_wm2, "set_webhook_fn"):
            _webhook_mgr = getattr(orchestrator, "_webhook_manager", None)
            if _webhook_mgr is not None and hasattr(_webhook_mgr, "deliver"):
                async def _fire_phase_webhook(event_type: str, payload: dict) -> None:
                    """Deliver a phase lifecycle event to all matching webhooks."""
                    await _webhook_mgr.deliver(event_type, payload)

                _wm2.set_webhook_fn(_fire_phase_webhook)

    # Episodic memory store (shared between agents router and memory router)
    # Reference: DESIGN.md §10.28 (v1.0.28); DESIGN.md §10.29 (v1.0.29)
    _orch_config = getattr(orchestrator, "config", None)
    _episode_store = EpisodeStore(
        root_dir=getattr(_orch_config, "mailbox_dir", ".orchestrator/mailbox"),
        session_name=getattr(_orch_config, "session_name", "orchestrator"),
    )
    # Share with orchestrator dispatch loop for episode auto-inject
    orchestrator._episode_store = _episode_store  # type: ignore[attr-defined]

    app.include_router(
        build_tasks_router(orchestrator, auth, rate_limit=rate_limit, limiter=_limiter),
    )
    app.include_router(
        build_agents_router(orchestrator, auth, episode_store=_episode_store),
    )
    app.include_router(
        build_workflows_router(orchestrator, auth, scratchpad=_scratchpad),
    )
    app.include_router(
        build_scratchpad_router(auth, _scratchpad),
    )
    app.include_router(
        build_system_router(orchestrator, auth),
    )
    app.include_router(
        build_webhooks_router(orchestrator, auth),
    )
    app.include_router(
        build_groups_router(orchestrator, auth),
    )
    app.include_router(
        build_memory_router(orchestrator, auth, episode_store=_episode_store),
    )

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
