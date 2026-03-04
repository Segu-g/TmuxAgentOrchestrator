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
from typing import Any

import webauthn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
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


class AgentKillResponse(BaseModel):
    agent_id: str
    stopped: bool


class SendMessage(BaseModel):
    type: str = "PEER_MSG"
    payload: dict[str, Any] = {}


class SpawnAgent(BaseModel):
    parent_id: str
    template_id: str


class DirectorChat(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Module-level auth state
# ---------------------------------------------------------------------------

_credentials: dict[str, bytes] = {}    # b64url(cred_id) → public_key bytes
_sign_counts: dict[str, int] = {}      # b64url(cred_id) → sign_count
_sessions: dict[str, float] = {}       # session_token  → expiry (unix ts)
_pending_challenge: bytes | None = None
_SESSION_TTL = 86_400  # 24 h


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


def create_app(orchestrator: Any, hub: WebSocketHub, *, api_key: str = "") -> FastAPI:
    """Create and wire up the FastAPI application.

    Parameters
    ----------
    orchestrator:
        The :class:`~tmux_orchestrator.orchestrator.Orchestrator` instance.
    hub:
        A :class:`WebSocketHub` already connected to the bus.
    """
    auth = _make_combined_auth(api_key)

    @asynccontextmanager
    async def _lifespan(application: FastAPI):  # noqa: ARG001
        await hub.start()
        logger.info("WebSocket hub started")
        yield
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
            body.prompt, priority=body.priority, metadata=body.metadata
        )
        return {"task_id": task.id, "prompt": task.prompt, "priority": task.priority}

    @app.get("/tasks", summary="List pending tasks", dependencies=[Depends(auth)])
    async def list_tasks() -> list[dict]:
        return orchestrator.list_tasks()

    @app.get("/agents", summary="List agents and their status", dependencies=[Depends(auth)])
    async def list_agents() -> list[dict]:
        return orchestrator.list_agents()

    @app.delete("/agents/{agent_id}", summary="Stop an agent", dependencies=[Depends(auth)])
    async def stop_agent(agent_id: str) -> AgentKillResponse:
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        await agent.stop()
        return AgentKillResponse(agent_id=agent_id, stopped=True)

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
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr><th>ID</th><th>Role</th><th>Status</th><th>Task</th></tr></thead>
        <tbody id="agents-body"><tr><td colspan="4" class="empty-hint">Loading…</td></tr></tbody>
      </table>
    </div>
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
    refreshAgents();
    refreshTasks();
    if (!_pollInterval) {
      _pollInterval = setInterval(() => { refreshAgents(); refreshTasks(); }, 3000);
    }
  } else {
    overlay.style.display = 'flex';
    btnSignout.style.display = 'none';
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
