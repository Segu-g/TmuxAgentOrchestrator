# TmuxAgentOrchestrator

Orchestrate multiple [Claude Code](https://claude.ai/code) agents inside tmux panes. A central orchestrator manages a pool of workers, dispatches tasks from a priority queue, and routes peer-to-peer messages between agents. Monitor and control everything from a Textual TUI or a browser-based web UI.

## Features

- **Multi-agent dispatch** — priority task queue fanned out to idle Claude Code workers
- **P2P messaging** — agents can send messages to each other (permission-gated)
- **Sub-agent spawning** — agents can spawn helpers at runtime via slash commands
- **Git worktree isolation** — each agent works in its own branch by default; changes don't interfere
- **Textual TUI** — terminal dashboard with agent status, task queue, and live event log
- **Web UI** — browser dashboard with the same panels plus a Director chat interface
- **Passkey auth** — WebAuthn (FIDO2) for browser login; API key for CLI/agents
- **Director agent** — optional orchestrator-level agent you can chat with directly

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
  - id: worker-2
    type: claude_code
p2p_permissions:
  - [worker-1, worker-2]
task_timeout: 120
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

| Field | Default | Description |
|---|---|---|
| `session_name` | `orchestrator` | tmux session name |
| `mailbox_dir` | `~/.tmux_orchestrator` | Directory for agent inboxes |
| `task_timeout` | `null` | Per-task timeout in seconds |
| `web_base_url` | `http://localhost:8000` | Base URL agents use to reach the REST API |
| `agents` | — | List of agent definitions (see below) |
| `p2p_permissions` | `[]` | Pairs of agent IDs allowed to message each other |

### Agent fields

| Field | Default | Description |
|---|---|---|
| `id` | — | Unique agent identifier |
| `type` | — | `claude_code` (only supported type) |
| `role` | `worker` | `worker` or `director` |
| `isolate` | `true` | Give the agent its own git worktree branch |

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

All endpoints (except `/auth/*` and `GET /`) require either a valid session cookie or `X-API-Key` header.

| Method | Path | Description |
|---|---|---|
| `GET` | `/agents` | List agents and status |
| `DELETE` | `/agents/{id}` | Stop an agent |
| `POST` | `/agents/{id}/message` | Send a message to an agent |
| `POST` | `/agents` | Spawn a sub-agent |
| `GET` | `/tasks` | List queued tasks |
| `POST` | `/tasks` | Submit a new task |
| `POST` | `/director/chat` | Chat with the Director agent |
| `WS` | `/ws` | WebSocket event stream |

## Agent Slash Commands

When running inside the orchestrator, Claude Code agents have access to these slash commands:

| Command | Description |
|---|---|
| `/check-inbox` | List unread messages |
| `/read-message <id>` | Read a message in full |
| `/send-message <agent_id> <text>` | Send a message to another agent |
| `/spawn-subagent <template_id>` | Spawn a helper agent |
| `/list-agents` | Show all agent statuses |

## Architecture

```
┌─────────────────────────────────────────────┐
│                Orchestrator                  │
│  PriorityQueue → dispatch → agent pool       │
│  P2P router (frozenset permission table)     │
└──────┬──────────────────────────┬────────────┘
       │ async pub/sub Bus        │
  ┌────▼─────┐              ┌─────▼──────┐
  │ Textual  │              │  FastAPI   │
  │   TUI    │              │  Web UI    │
  └──────────┘              └─────▼──────┘
                                  │ WebSocket
                            browser clients
       │
  ┌────▼───────────────────────────────────┐
  │  ClaudeCodeAgent × N                   │
  │  tmux pane → claude CLI → poll output  │
  │  git worktree isolation per agent      │
  └────────────────────────────────────────┘
```

- **Bus** — async in-process pub/sub; broadcast subscribers receive all messages
- **Orchestrator** — polls for idle agents every 200 ms; routes P2P messages
- **ClaudeCodeAgent** — drives `claude` via tmux `send-keys`; detects completion by output settling at a prompt pattern (`❯`, `$`, `>`, `Human:`)
- **Worktree isolation** — each agent gets `.worktrees/{agent_id}/` on branch `worktree/{agent_id}`, deleted on agent stop

## Running Tests

```bash
uv run pytest tests/ -v
```

## License

MIT
