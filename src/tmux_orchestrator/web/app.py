"""FastAPI web application — REST endpoints + WebSocket hub."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from tmux_orchestrator.bus import Message, MessageType
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
    agent_type: str = "custom"
    command: str = ""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_app(orchestrator: Any, hub: WebSocketHub) -> FastAPI:
    """Create and wire up the FastAPI application.

    Parameters
    ----------
    orchestrator:
        The :class:`~tmux_orchestrator.orchestrator.Orchestrator` instance.
    hub:
        A :class:`WebSocketHub` already connected to the bus.
    """
    app = FastAPI(
        title="TmuxAgentOrchestrator",
        description="REST + WebSocket API for the tmux agent orchestrator",
        version="0.1.0",
    )

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------

    @app.on_event("startup")
    async def _startup() -> None:
        await hub.start()
        logger.info("WebSocket hub started")

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await hub.stop()
        logger.info("WebSocket hub stopped")

    # ------------------------------------------------------------------
    # REST endpoints
    # ------------------------------------------------------------------

    @app.post("/tasks", summary="Submit a new task")
    async def submit_task(body: TaskSubmit) -> dict:
        task = await orchestrator.submit_task(
            body.prompt, priority=body.priority, metadata=body.metadata
        )
        return {"task_id": task.id, "prompt": task.prompt, "priority": task.priority}

    @app.get("/tasks", summary="List pending tasks")
    async def list_tasks() -> list[dict]:
        return orchestrator.list_tasks()

    @app.get("/agents", summary="List agents and their status")
    async def list_agents() -> list[dict]:
        return orchestrator.list_agents()

    @app.delete("/agents/{agent_id}", summary="Stop an agent")
    async def stop_agent(agent_id: str) -> AgentKillResponse:
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        await agent.stop()
        return AgentKillResponse(agent_id=agent_id, stopped=True)

    @app.post("/agents/{agent_id}/message", summary="Send a message to an agent")
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

    @app.post("/agents", summary="Spawn a sub-agent under a parent agent")
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
                "agent_type": body.agent_type,
                "command": body.command,
            },
        )
        await orchestrator.bus.publish(msg)
        return {"status": "spawning", "parent_id": body.parent_id}

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await hub.handle(websocket)

    # ------------------------------------------------------------------
    # Browser UI (single-page)
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def web_ui() -> HTMLResponse:
        return HTMLResponse(_HTML_UI)

    return app


# ---------------------------------------------------------------------------
# Embedded single-page browser UI
# ---------------------------------------------------------------------------

_HTML_UI = """<!DOCTYPE html>
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
    gap: 0;
  }
  header {
    background: #161b22;
    border-bottom: 1px solid #30363d;
    padding: 12px 20px;
    display: flex;
    align-items: center;
    gap: 12px;
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
    grid-template-rows: 1fr auto;
    gap: 1px;
    background: #30363d;
    overflow: hidden;
  }
  section {
    background: #0d1117;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  section.full-width { grid-column: 1 / -1; }
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
  }
  .badge {
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 1px 7px;
    font-size: 0.7rem;
    color: #8b949e;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.85rem;
  }
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
  .tbl-wrap { overflow-y: auto; flex: 1; }
  .status-idle   { color: #3fb950; }
  .status-busy   { color: #e3b341; }
  .status-error  { color: #f85149; }
  .status-stopped{ color: #6e7681; }
  #log-panel { grid-column: 1 / -1; max-height: 200px; }
  #log-list {
    overflow-y: auto;
    flex: 1;
    padding: 8px 14px;
    font-size: 0.8rem;
    font-family: 'Consolas', monospace;
  }
  .log-entry {
    display: flex;
    gap: 10px;
    padding: 2px 0;
    border-bottom: 1px solid #21262d11;
  }
  .log-ts { color: #6e7681; flex-shrink: 0; }
  .log-type { font-weight: 600; flex-shrink: 0; min-width: 60px; }
  .type-RESULT { color: #3fb950; }
  .type-STATUS { color: #58a6ff; }
  .type-PEER_MSG { color: #bc8cff; }
  .type-TASK { color: #e3b341; }
  .type-CONTROL { color: #f0883e; }
  footer {
    background: #161b22;
    border-top: 1px solid #30363d;
    padding: 10px 20px;
    display: flex;
    gap: 8px;
    align-items: center;
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
  button.danger { background: #b62324; }
  button.danger:hover { background: #da3633; }
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
  .empty-hint {
    color: #6e7681;
    font-size: 0.8rem;
    padding: 12px;
    text-align: center;
  }
</style>
</head>
<body>

<header>
  <div id="status-dot" class="disconnected"></div>
  <h1>TmuxAgentOrchestrator</h1>
  <span id="conn-label" style="font-size:0.8rem;color:#8b949e">Connecting…</span>
</header>

<main>
  <!-- Agents panel -->
  <section>
    <div class="section-header">
      Agents
      <span id="agent-count" class="badge">0</span>
    </div>
    <div class="tbl-wrap">
      <table id="agents-table">
        <thead><tr><th>ID</th><th>Status</th><th>Current Task</th></tr></thead>
        <tbody id="agents-body"><tr><td colspan="3" class="empty-hint">Loading…</td></tr></tbody>
      </table>
    </div>
  </section>

  <!-- Task queue panel -->
  <section>
    <div class="section-header">
      Task Queue
      <span id="task-count" class="badge">0</span>
    </div>
    <div class="tbl-wrap">
      <table id="tasks-table">
        <thead><tr><th>Priority</th><th>Task ID</th><th>Prompt</th></tr></thead>
        <tbody id="tasks-body"><tr><td colspan="3" class="empty-hint">Loading…</td></tr></tbody>
      </table>
    </div>
  </section>

  <!-- Log panel -->
  <section id="log-panel">
    <div class="section-header">
      Event Log
      <button onclick="clearLog()" style="padding:2px 10px;font-size:0.75rem;background:#21262d;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;">Clear</button>
    </div>
    <div id="log-list"></div>
  </section>
</main>

<footer>
  <input id="task-input" type="text" placeholder="Enter task prompt…" />
  <input id="priority-input" type="number" value="0" min="0" title="Priority (lower = higher priority)" />
  <button onclick="submitTask()">Submit Task</button>
</footer>

<script>
const API_BASE = '';

// --- WebSocket ---
let ws;
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    document.getElementById('status-dot').classList.remove('disconnected');
    document.getElementById('conn-label').textContent = 'Connected';
    logEntry({type:'CONTROL', from_id:'system', payload:{msg:'WebSocket connected'}, timestamp: new Date().toISOString()});
  };
  ws.onclose = () => {
    document.getElementById('status-dot').classList.add('disconnected');
    document.getElementById('conn-label').textContent = 'Disconnected — retrying…';
    setTimeout(connectWS, 3000);
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    logEntry(msg);
    // Refresh tables on relevant events
    if (['RESULT','STATUS','CONTROL'].includes(msg.type)) {
      refreshAgents();
      refreshTasks();
    }
  };
}

// --- Polling ---
function refreshAgents() {
  fetch(`${API_BASE}/agents`)
    .then(r => r.json())
    .then(agents => {
      document.getElementById('agent-count').textContent = agents.length;
      const body = document.getElementById('agents-body');
      if (agents.length === 0) {
        body.innerHTML = '<tr><td colspan="3" class="empty-hint">No agents registered</td></tr>';
        return;
      }
      body.innerHTML = agents.map(a => {
        const sc = 'status-' + a.status.toLowerCase();
        return `<tr>
          <td>${esc(a.id)}</td>
          <td class="${sc}">${esc(a.status)}</td>
          <td>${a.current_task ? esc(a.current_task.slice(0,8)) : '—'}</td>
        </tr>`;
      }).join('');
    }).catch(console.error);
}

function refreshTasks() {
  fetch(`${API_BASE}/tasks`)
    .then(r => r.json())
    .then(tasks => {
      document.getElementById('task-count').textContent = tasks.length;
      const body = document.getElementById('tasks-body');
      if (tasks.length === 0) {
        body.innerHTML = '<tr><td colspan="3" class="empty-hint">Queue is empty</td></tr>';
        return;
      }
      body.innerHTML = tasks.map(t => `<tr>
        <td>${esc(String(t.priority))}</td>
        <td>${esc(t.task_id.slice(0,8))}</td>
        <td>${esc(t.prompt.slice(0,60))}${t.prompt.length > 60 ? '…' : ''}</td>
      </tr>`).join('');
    }).catch(console.error);
}

// --- Task submit ---
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
  if (resp.ok) {
    inp.value = '';
    refreshTasks();
  } else {
    alert('Failed to submit task: ' + resp.statusText);
  }
}
document.getElementById('task-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') submitTask();
});

// --- Log ---
const MAX_LOG = 200;
function logEntry(msg) {
  const list = document.getElementById('log-list');
  const ts = new Date(msg.timestamp).toLocaleTimeString();
  const div = document.createElement('div');
  div.className = 'log-entry';
  const payload = JSON.stringify(msg.payload || {});
  div.innerHTML = `
    <span class="log-ts">${ts}</span>
    <span class="log-type type-${msg.type}">${msg.type}</span>
    <span>${esc(msg.from_id)} → ${esc(msg.to_id || '*')}: ${esc(payload.slice(0,120))}</span>
  `;
  list.prepend(div);
  // Trim old entries
  while (list.children.length > MAX_LOG) list.removeChild(list.lastChild);
}
function clearLog() { document.getElementById('log-list').innerHTML = ''; }

function esc(str) {
  return String(str)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;');
}

// --- Init ---
connectWS();
refreshAgents();
refreshTasks();
setInterval(() => { refreshAgents(); refreshTasks(); }, 3000);
</script>
</body>
</html>
"""
