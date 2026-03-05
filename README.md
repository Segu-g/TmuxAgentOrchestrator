# TmuxAgentOrchestrator

Orchestrate multiple [Claude Code](https://claude.ai/code) agents inside tmux panes. A central orchestrator manages a pool of workers, dispatches tasks from a priority queue, and routes peer-to-peer messages between agents. Monitor and control everything from a Textual TUI or a browser-based web UI.

## Features

### Core dispatch
- **Multi-agent dispatch** — priority task queue fanned out to idle Claude Code workers; lower `priority` value = dispatched first
- **Capability-based dispatch** — `required_tags` on tasks matched against agent `tags` (FIPA Directory Facilitator pattern)
- **Target-agent pinning** — `target_agent` forces a task to wait for a specific worker
- **Task dependencies** — `depends_on` list; topological sort ensures prerequisite tasks complete first
- **Batch task submission** — `POST /tasks/batch` validates all tasks before enqueueing any
- **Task cancellation** — `POST /tasks/{id}/cancel` removes a pending task from the queue
- **Live priority update** — `PATCH /tasks/{id}` changes priority in-place; heap is rebuilt immediately
- **Dead letter queue (DLQ)** — tasks dead-lettered after `dlq_max_retries` failed dispatch attempts; queryable via `GET /dlq`
- **Bounded or unbounded queue** — `task_queue_maxsize`; 0 = unbounded (default)

### Reliability
- **Circuit breaker** — per-agent CLOSED → OPEN → HALF_OPEN; configurable threshold and recovery window
- **Watchdog timeout** — synthetic `RESULT(error="watchdog_timeout")` published at 1.5× task threshold
- **Auto-recovery** — ERROR agents restarted up to `recovery_attempts` times with exponential backoff
- **Idempotency keys** — `submit_task(idempotency_key=)`; 1-hour in-process TTL prevents duplicate execution
- **Token-bucket rate limiter** — configurable `rate_limit_rps` / `rate_limit_burst`; live reconfiguration via `PUT /rate-limit`
- **Dispatch pause / resume** — in-flight tasks continue; queue accumulates while paused

### Agent communication
- **P2P messaging** — agents send messages to each other (permission-gated by `p2p_permissions` config)
- **Sub-agent spawning** — agents spawn helpers at runtime via `/spawn-subagent`; P2P auto-granted
- **`reply_to` routing** — task RESULT delivered directly to a named agent's mailbox + pane notification
- **Shared scratchpad** — Blackboard-pattern key/value store for pipeline data sharing (`GET/PUT/DELETE /scratchpad/{key}`)

### Observability
- **Context window monitoring** — `ContextMonitor` polls each pane, estimates token count, publishes `context_warning` events at a configurable threshold; optional auto-inject of `/summarize`
- **NOTES.md update detection** — publishes `notes_updated` bus event when an agent's `NOTES.md` changes
- **Per-agent task history** — last 200 completed task records per agent; `GET /agents/{id}/history`
- **Prometheus metrics** — `GET /metrics`; gauges for agent status distribution, queue depth, bus drops
- **SSE event stream** — `GET /events`; browser `EventSource` API; keep-alive every 15 s
- **Agent hierarchy tree** — `GET /agents/tree`; nested JSON (d3-hierarchy compatible); Web UI List/Tree toggle
- **Structured JSON logging** — `trace_id` + `agent_id` in every log record via `contextvars`

### Isolation and context
- **Git worktree isolation** — each agent works in its own branch (`.worktrees/{agent_id}/`); changes don't interfere
- **`context_files` auto-copy** — files copied into the agent's worktree at startup (`shutil.copy2`)
- **`system_prompt` injection** — prepended to the auto-generated `CLAUDE.md` in each agent's worktree

### Interfaces
- **Textual TUI** — terminal dashboard with agent status, task queue, and live event log (keybindings: `n` new task, `k` kill agent, `p` P2P message, `q` quit)
- **Web UI** — browser dashboard with the same panels plus a Director chat interface; Web UI List/Tree toggle
- **Passkey auth** — WebAuthn (FIDO2) for browser login; `X-API-Key` header for CLI/agents
- **Director agent** — optional orchestrator-level agent you can chat with directly via `POST /director/chat`
- **Workflow builder** — `Workflow` class with topological sort for defining multi-step pipelines

## Requirements

- Python 3.11+
- [tmux](https://github.com/tmux/tmux)
- [Claude Code CLI](https://claude.ai/code) (`claude` on PATH)
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

## Installation

```bash
git clone https://github.com/Segu-g/TmuxAgentOrchestrator.git
cd TmuxAgentOrchestrator
uv sync
```

For development (includes test dependencies):

```bash
uv sync --extra dev
```

## Quick Start

**1. Create a config file** (or use the example):

```yaml
# examples/basic_config.yaml
session_name: orchestrator
agents:
  - id: worker-1
    type: claude_code
    tags: [python, testing]
  - id: worker-2
    type: claude_code
    system_prompt: "You are a specialist in data analysis."
    context_files:
      - examples/basic_config.yaml
p2p_permissions:
  - [worker-1, worker-2]
task_timeout: 120
rate_limit_rps: 2.0
```

**2. Launch the TUI:**

```bash
uv run tmux-orchestrator tui --config examples/basic_config.yaml
```

**3. Or launch the web server:**

```bash
uv run tmux-orchestrator web --config examples/basic_config.yaml
```

Open `http://localhost:8000/` — register a passkey on first visit, then sign in with your device biometric/PIN.

## CLI Reference

```
tmux-orchestrator tui   --config FILE            # Textual terminal UI
tmux-orchestrator web   --config FILE [--port N] # Web UI + REST API
tmux-orchestrator run   --config FILE [--prompt] # Headless, optional single task
tmux-orchestrator chat  --url URL --api-key KEY  # CLI chat with Director agent
```

## Configuration

### Orchestrator fields

| Field | Type | Default | Description |
|---|---|---|---|
| `session_name` | `str` | `"orchestrator"` | tmux session name |
| `mailbox_dir` | `str` | `"~/.tmux_orchestrator"` | Directory for agent inboxes |
| `web_base_url` | `str` | `"http://localhost:8000"` | Base URL agents use to reach the REST API |
| `task_timeout` | `int` | `120` | Global per-task timeout in seconds |
| `agents` | `list` | `[]` | List of agent definitions (see below) |
| `p2p_permissions` | `list` | `[]` | Pairs of agent IDs allowed to message each other |
| `circuit_breaker_threshold` | `int` | `3` | ERROR count before opening a circuit breaker |
| `circuit_breaker_recovery` | `float` | `60.0` | Seconds before attempting HALF_OPEN recovery |
| `dlq_max_retries` | `int` | `50` | Re-queue attempts before dead-lettering a task |
| `task_queue_maxsize` | `int` | `0` | Queue capacity; `0` = unbounded; `>0` raises when full |
| `watchdog_poll` | `float` | `10.0` | Seconds between watchdog checks |
| `recovery_attempts` | `int` | `3` | Max restart attempts per ERROR agent before giving up |
| `recovery_backoff_base` | `float` | `5.0` | Backoff base in seconds; attempt N waits `base^N` seconds |
| `recovery_poll` | `float` | `2.0` | Seconds between recovery loop checks |
| `rate_limit_rps` | `float` | `0.0` | Token-bucket refill rate (tasks/second); `0` = unlimited |
| `rate_limit_burst` | `int` | `0` | Bucket capacity; `0` defaults to `2 × rps` (or 1 when rps=0) |
| `context_window_tokens` | `int` | `200000` | Total context window size for monitoring (Claude Sonnet/Opus default) |
| `context_warn_threshold` | `float` | `0.75` | Fraction (0–1) at which `context_warning` is emitted |
| `context_auto_summarize` | `bool` | `false` | Auto-inject `/summarize` into agent pane at threshold |
| `context_monitor_poll` | `float` | `5.0` | Context monitor poll interval in seconds |

### Agent fields

| Field | Type | Default | Description |
|---|---|---|---|
| `id` | `str` | — | Unique agent identifier |
| `type` | `str` | — | `claude_code` (only supported type) |
| `role` | `str` | `"worker"` | `"worker"` or `"director"` |
| `isolate` | `bool` | `true` | Give the agent its own git worktree branch |
| `task_timeout` | `int\|null` | `null` | Per-agent override of global `task_timeout`; `null` = use global |
| `command` | `str\|null` | `null` | Custom CLI command; `null` = `claude` CLI |
| `system_prompt` | `str\|null` | `null` | Prepended to the agent's auto-generated `CLAUDE.md` at startup |
| `context_files` | `list[str]` | `[]` | Relative paths (from cwd) copied into worktree at startup |
| `tags` | `list[str]` | `[]` | Capability tags; tasks with `required_tags` only dispatch to matching agents |

## Web UI

The web UI is a single-page app served at `GET /`. On first visit it shows a passkey registration screen. After registering, subsequent visits authenticate via your device biometric or PIN (no passwords, no tokens in URLs).

Agents and CLI tools use the printed API key via `X-API-Key` header — unaffected by the passkey flow.

### Exposing over the internet

WebAuthn requires HTTPS. Use a TLS-terminating reverse proxy and ensure it forwards `X-Forwarded-Proto: https`. The app handles this header automatically.

**Cloudflare Tunnel** is the easiest option:

```bash
cloudflared tunnel run --url http://localhost:8000 my-tunnel
```

## REST API

All endpoints require either a valid session cookie or `X-API-Key` header unless noted otherwise.

### Tasks

| Method | Path | Description |
|---|---|---|
| `POST` | `/tasks` | Submit a new task (`prompt`, `priority`, `metadata`, `reply_to`, `target_agent`, `required_tags`) |
| `POST` | `/tasks/batch` | Submit multiple tasks atomically; all validated before any are enqueued |
| `GET` | `/tasks` | List pending (queued) tasks |
| `POST` | `/tasks/{id}/cancel` | Remove a pending task from the queue; 404 if never submitted |
| `PATCH` | `/tasks/{id}` | Update priority of a pending task in-place; heap is rebuilt |

### Agents

| Method | Path | Description |
|---|---|---|
| `GET` | `/agents` | List all agents and their current status |
| `GET` | `/agents/tree` | Agent hierarchy as nested JSON (d3-hierarchy compatible) |
| `POST` | `/agents` | Spawn a sub-agent under a parent agent |
| `DELETE` | `/agents/{id}` | Stop an agent |
| `POST` | `/agents/{id}/reset` | Clear ERROR / permanently-failed state and restart the agent |
| `POST` | `/agents/{id}/message` | Send a bus message directly to an agent |
| `GET` | `/agents/{id}/history` | Last N completed tasks (`?limit=N`; default 50, max 200) |
| `GET` | `/agents/{id}/stats` | Context window usage stats for one agent |

### Orchestrator control

| Method | Path | Description |
|---|---|---|
| `POST` | `/orchestrator/pause` | Pause dispatch loop (in-flight tasks continue; idempotent) |
| `POST` | `/orchestrator/resume` | Resume dispatch loop (idempotent) |
| `GET` | `/orchestrator/status` | Returns `paused`, `queue_depth`, `agent_count`, `dlq_depth` |

### Rate limiter

| Method | Path | Description |
|---|---|---|
| `GET` | `/rate-limit` | Current limiter config and live token availability |
| `PUT` | `/rate-limit` | Reconfigure live (`rate`, `burst`); `rate=0` disables |

### Shared scratchpad

| Method | Path | Description |
|---|---|---|
| `GET` | `/scratchpad/` | List all key-value pairs |
| `PUT` | `/scratchpad/{key}` | Write (create or overwrite) a value |
| `GET` | `/scratchpad/{key}` | Read a value; 404 if absent |
| `DELETE` | `/scratchpad/{key}` | Delete an entry; 404 if absent |

### Director

| Method | Path | Description |
|---|---|---|
| `POST` | `/director/chat` | Send a message to the Director agent; add `?wait=true` for synchronous response (up to 300 s) |

### Observability

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/context-stats` | Required | Context window stats for all agents |
| `GET` | `/dlq` | Required | Dead-letter queue contents |
| `GET` | `/events` | Required | Server-Sent Events stream (all bus events); `EventSource`-compatible |
| `WS` | `/ws` | Session or `?key=` | WebSocket event stream |
| `GET` | `/metrics` | None | Prometheus-format metrics (agent status, queue depth, bus drops) |
| `GET` | `/healthz` | None | Liveness probe; 200 if event loop is responsive |
| `GET` | `/readyz` | None | Readiness probe; 200 when able to dispatch; 503 with detail when not ready |

### Auth (internal — not in OpenAPI schema)

| Method | Path | Description |
|---|---|---|
| `GET` | `/auth/status` | Whether a passkey is registered and whether the session is authenticated |
| `POST` | `/auth/register-options` | Begin WebAuthn registration handshake |
| `POST` | `/auth/register` | Complete WebAuthn registration |
| `POST` | `/auth/authenticate-options` | Begin WebAuthn login handshake |
| `POST` | `/auth/authenticate` | Complete WebAuthn login |
| `POST` | `/auth/logout` | Invalidate the current session cookie |

## Agent Slash Commands

When running inside the orchestrator, Claude Code agents have access to these slash commands:

| Command | Description |
|---|---|
| `/check-inbox` | List unread messages |
| `/read-message <id>` | Read a message in full |
| `/send-message <agent_id> <text>` | Send a message to another agent |
| `/spawn-subagent <template_id>` | Spawn a helper agent |
| `/list-agents` | Show all agent statuses |
| `/plan <description>` | Write `PLAN.md` before beginning implementation |
| `/tdd <feature>` | Guide a Red → Green → Refactor TDD cycle |
| `/progress <summary>` | Report progress to your parent agent |
| `/summarize` | Compress current context state into `NOTES.md` |
| `/delegate <task>` | Break a task into subtasks and spawn sub-agents |

## Architecture

```
┌─────────────────────────────────────────────┐
│                Orchestrator                  │
│  PriorityQueue → dispatch → agent pool       │
│  P2P router (frozenset permission table)     │
│  Circuit breaker per agent                   │
│  Watchdog + auto-recovery                    │
│  ContextMonitor (pane token estimation)      │
└──────┬──────────────────────────┬────────────┘
       │ async pub/sub Bus        │
  ┌────▼─────┐              ┌─────▼──────┐
  │ Textual  │              │  FastAPI   │
  │   TUI    │              │  Web UI    │
  └──────────┘              └─────▼──────┘
                                  │ WebSocket / SSE
                            browser clients
       │
  ┌────▼───────────────────────────────────┐
  │  ClaudeCodeAgent × N                   │
  │  tmux pane → claude CLI → poll output  │
  │  git worktree isolation per agent      │
  └────────────────────────────────────────┘
```

- **Bus** — async in-process pub/sub; broadcast subscribers receive all messages
- **Orchestrator** — polls for idle agents every 200 ms; routes P2P messages; watchdog at 1.5× timeout
- **ClaudeCodeAgent** — drives `claude` via tmux `send-keys`; detects completion when output settles at a prompt pattern (`❯`, `$`, `>`, or `Human:`, with any trailing whitespace)
- **Worktree isolation** — each agent gets `.worktrees/{agent_id}/` on branch `worktree/{agent_id}`, deleted on agent stop
- **ContextMonitor** — polls each agent's pane every `context_monitor_poll` seconds; estimates token count as `chars / 4`; publishes `context_warning` / `notes_updated` / `summarize_triggered` bus events

## Running Tests

```bash
uv run pytest tests/ -v
```

## License

MIT
