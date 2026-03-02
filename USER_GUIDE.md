# TmuxAgentOrchestrator — User Guide

## Table of Contents

1. [Overview](#1-overview)
2. [Requirements](#2-requirements)
3. [Installation](#3-installation)
4. [Quick Start](#4-quick-start)
5. [Configuration Reference](#5-configuration-reference)
6. [Running Modes](#6-running-modes)
7. [Agent Types](#7-agent-types)
8. [Task Timeouts](#8-task-timeouts)
9. [Agent Status Events](#9-agent-status-events)
10. [Task Submission](#10-task-submission)
11. [P2P Messaging](#11-p2p-messaging)
12. [Sub-Agent Spawning](#12-sub-agent-spawning)
13. [Git Worktree Isolation](#13-git-worktree-isolation)
14. [Web UI & REST API](#14-web-ui--rest-api)
15. [Slash Commands (for Claude Code Agents)](#15-slash-commands-for-claude-code-agents)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. Overview

TmuxAgentOrchestrator runs a pool of AI agents inside tmux panes and dispatches work to them from a priority queue. Each agent is either:

- A **Claude Code agent** — a `claude --no-pager` process in a dedicated tmux pane, driven by keyboard input and pane-output polling.
- A **Custom agent** — an arbitrary script communicating over newline-delimited JSON on stdin/stdout.

A central **orchestrator** process manages the queue, routes peer-to-peer (P2P) messages between permitted agent pairs, and optionally isolates each agent in its own git worktree.

Two interfaces are available:

| Interface | Command | Purpose |
|---|---|---|
| Textual TUI | `tmux-orchestrator tui` | Interactive terminal dashboard |
| Web UI + REST | `tmux-orchestrator web` | Browser dashboard + HTTP API |
| Headless | `tmux-orchestrator run` | One-shot task, print result and exit |

---

## 2. Requirements

| Dependency | Minimum | Notes |
|---|---|---|
| Python | 3.11 | Uses `X \| Y` union syntax and `tomllib` |
| tmux | any recent | Must be running and `$TMUX` or a server accessible |
| git | any recent | Only needed when worktree isolation is enabled |
| `claude` CLI | latest | Only needed for `claude_code` agent type |

Python package dependencies (installed automatically):

```
libtmux>=0.28   textual>=0.60   fastapi>=0.110   uvicorn[standard]
pyyaml>=6       typer>=0.12     rich>=13          websockets>=12
```

---

## 3. Installation

```bash
# From the project directory
pip install -e ".[dev]"

# Or with uv (recommended)
uv sync --extra dev
```

Verify:

```bash
tmux-orchestrator --help
```

---

## 4. Quick Start

### Step 1 — Start a tmux session

The orchestrator requires a running tmux server. Start one if you don't have one:

```bash
tmux new-session -s mywork -d
```

### Step 2 — Write a config file

```yaml
# myproject.yaml
session_name: mywork
mailbox_dir: ~/.tmux_orchestrator

agents:
  - id: worker-1
    type: claude_code   # drives `claude --no-pager`

  - id: worker-2
    type: custom
    command: "python3 examples/echo_agent.py"

p2p_permissions:
  - [worker-1, worker-2]   # allow these two to message each other
```

### Step 3 — Launch

```bash
# TUI (recommended for interactive use)
tmux-orchestrator tui --config myproject.yaml

# Web server (REST API + browser dashboard)
tmux-orchestrator web --config myproject.yaml --port 8000

# One-shot headless run
tmux-orchestrator run --config myproject.yaml --prompt "Summarise the CLAUDE.md file"
```

---

## 5. Configuration Reference

### Top-level fields

| Field | Type | Default | Description |
|---|---|---|---|
| `session_name` | string | `"orchestrator"` | tmux session name to attach to or create |
| `mailbox_dir` | string | `"~/.tmux_orchestrator"` | Root directory for file-based message storage |
| `web_base_url` | string | `"http://localhost:8000"` | REST API base URL injected into agent context files |
| `task_timeout` | integer | `120` | Seconds before a running task is forcibly cancelled (0 = no limit) |
| `p2p_permissions` | list of pairs | `[]` | Agent ID pairs that are allowed to message each other |
| `agents` | list | `[]` | Agent definitions (see below) |

### Agent fields

| Field | Type | Default | Description |
|---|---|---|---|
| `id` | string | required | Unique agent identifier |
| `type` | `claude_code` \| `custom` | required | Agent implementation to use |
| `command` | string | `null` | Shell command for `custom` agents (required); ignored for `claude_code` |
| `isolate` | boolean | `true` | Whether to create a dedicated git worktree for this agent |

### Complete example

```yaml
session_name: dev-swarm
mailbox_dir: ~/.tmux_orchestrator
web_base_url: http://localhost:9000
task_timeout: 300   # cancel task after 5 minutes; 0 = unlimited

agents:
  - id: planner
    type: claude_code
    isolate: true          # gets .worktrees/planner/ on branch worktree/planner

  - id: coder
    type: claude_code
    isolate: true

  - id: tester
    type: custom
    command: "python3 scripts/test_runner_agent.py"
    isolate: false         # shares the main repo working tree

p2p_permissions:
  - [planner, coder]
  - [coder, tester]
```

---

## 6. Running Modes

### `tui` — Textual TUI

```bash
tmux-orchestrator tui --config myproject.yaml [--verbose]
```

Launches a full-screen terminal UI with:

| Panel | Content |
|---|---|
| Agents | Agent ID, status (IDLE/BUSY/ERROR/STOPPED), current task |
| Task Queue | Pending tasks with priority and prompt preview |
| Log | Structured log stream |
| Status Bar | Keybindings reminder |

**Keybindings:**

| Key | Action |
|---|---|
| `n` | Submit a new task (opens prompt dialog) |
| `k` | Kill (stop) the selected agent |
| `p` | Pause / resume task dispatch |
| `q` | Quit and stop all agents |

### `web` — FastAPI Web Server

```bash
tmux-orchestrator web --config myproject.yaml [--host 0.0.0.0] [--port 8000] [--verbose]
```

Starts a web server at `http://{host}:{port}`. Open it in a browser for a live dashboard.
Also exposes the REST API (see [Section 12](#12-web-ui--rest-api)) needed by agent slash commands.

### `run` — Headless One-Shot

```bash
tmux-orchestrator run --config myproject.yaml --prompt "Write unit tests for src/foo.py"
```

Starts all agents, submits the prompt as a task, waits for the result, prints it to stdout, then shuts everything down. Useful in scripts and CI pipelines.

---

## 7. Agent Types

### `claude_code`

Drives the `claude --no-pager` CLI in a dedicated tmux pane.

**How tasks are delivered:** The orchestrator calls `send_keys` to type the task prompt into the pane, just as a human would.

**How completion is detected:** The orchestrator polls the pane output every 500 ms. When the output has not changed for 3 consecutive polls *and* the last visible line matches a prompt pattern (`$`, `>`, or `Human:`), the task is considered complete and the captured text is published as the result.

**Lifecycle:**

```
start()  →  create pane  →  write context file  →  cd {worktree} && claude --no-pager
stop()   →  send "q"  →  unwatch pane  →  remove worktree
```

**Inbox notifications:** When a message arrives for a `claude_code` agent, the orchestrator types `__MSG__:{msg_id}` into the pane. The agent can then use `/check-inbox` to read it.

### `custom`

Runs an arbitrary script as a subprocess communicating over newline-delimited JSON.

**Protocol:**

```
stdin  ← {"task_id": "<uuid>", "prompt": "<task text>"}
stdout → {"task_id": "<uuid>", "result": "<result text>"}
```

Each line is one complete JSON object. The script must flush stdout after each response.

**Example — `examples/echo_agent.py`:**

```python
import json, sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    task = json.loads(line)
    print(json.dumps({
        "task_id": task["task_id"],
        "result": f"Echo: {task['prompt']}"
    }), flush=True)
```

**Working directory:** If `isolate: true`, the subprocess `cwd` is set to the agent's git worktree. If `isolate: false`, `cwd` is the main repo root.

**Context file:** On startup the agent writes `__orchestrator_context__.json` to its working directory — the same format as `claude_code` agents — so any subprocess that needs to call back into the REST API or mailbox can discover the orchestrator's coordinates automatically.

---

## 8. Task Timeouts

### Enforcement

The `task_timeout` config value (default `120` seconds) is enforced per-task for every agent type. When a task exceeds the limit:

1. The `_dispatch_task` coroutine is cancelled via `asyncio.wait_for`.
2. A `RESULT` message with `error: "timeout"` is published to the bus.
3. The agent returns to `IDLE` and is immediately eligible for the next task.

```json
{
  "type": "RESULT",
  "from_id": "worker-1",
  "payload": {
    "task_id": "3f8a2b…",
    "error": "timeout",
    "output": null
  }
}
```

### Setting a custom timeout

```yaml
task_timeout: 60     # 60 seconds
task_timeout: 0      # no timeout (run forever)
```

### Limitation — CustomAgent

For `custom` agents, `_dispatch_task` only writes the JSON line to stdin and returns immediately — actual processing happens asynchronously in `_read_loop`. A timeout therefore cancels the *delivery*, not the subprocess computation. If your script can run arbitrarily long, implement a timeout inside the script itself.

---

## 9. Agent Status Events

The orchestrator publishes `STATUS` bus messages at every lifecycle transition. Clients connected via WebSocket receive these in real time.

| `event` | When | Payload keys |
|---|---|---|
| `task_queued` | Task added to queue | `task_id`, `prompt` |
| `agent_busy` | Agent starts executing a task | `agent_id`, `status`, `task_id` |
| `agent_idle` | Agent finishes a task (success) | `agent_id`, `status`, `task_id` |
| `agent_error` | Agent task threw an exception | `agent_id`, `status`, `task_id` |
| `subagent_spawned` | Sub-agent created | `sub_agent_id`, `parent_id` |

Example WebSocket message:

```json
{
  "type": "STATUS",
  "from_id": "worker-1",
  "payload": {
    "event": "agent_busy",
    "agent_id": "worker-1",
    "status": "BUSY",
    "task_id": "3f8a2b…"
  }
}
```

Timeout results in an `agent_idle` event (the agent returns to IDLE); unhandled exceptions produce `agent_error`.

---

## 10. Task Submission

### Via TUI

Press `n` in the TUI, type your prompt, and press Enter. An optional priority field (integer, lower = higher priority, default 0) is available.

### Via REST API

```bash
curl -s -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write a Python hello world", "priority": 0}'
```

Response:
```json
{"task_id": "3f8a2b…", "prompt": "Write a Python hello world", "priority": 0}
```

### Via Python (programmatic)

```python
import asyncio
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import load_config
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.tmux_interface import TmuxInterface

async def main():
    config = load_config("myproject.yaml")
    bus = Bus()
    tmux = TmuxInterface(session_name=config.session_name, bus=bus)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    await orch.start()
    task = await orch.submit_task("hello", priority=0)
    print(task.id)

asyncio.run(main())
```

### Priority

Tasks use an `asyncio.PriorityQueue`. Lower integer = dispatched first.

```bash
# High priority
curl -X POST http://localhost:8000/tasks \
  -d '{"prompt": "urgent fix", "priority": -10}'

# Normal
curl -X POST http://localhost:8000/tasks \
  -d '{"prompt": "backlog item", "priority": 100}'
```

---

## 11. P2P Messaging

Agents can send messages directly to each other, gated by a permission table.

### Enabling permissions

In the YAML config:

```yaml
p2p_permissions:
  - [agent-a, agent-b]   # both directions: a→b and b→a
  - [agent-b, agent-c]
```

Each entry is bidirectional. If the pair is not listed, messages between those agents are silently dropped by the orchestrator.

**Exception:** Messages sent via `POST /agents/{id}/message` (the REST API) have `from_id = "__user__"`, which always bypasses the permission check.

### Sending a message via REST

```bash
curl -s -X POST http://localhost:8000/agents/worker-2/message \
  -H "Content-Type: application/json" \
  -d '{"type": "PEER_MSG", "payload": {"text": "Can you run the tests?"}}'
```

### Sending a message from a Claude Code agent

Use the `/send-message` slash command inside the agent's pane:

```
/send-message worker-2 Can you run the tests?
```

### Message schema

Messages are JSON files stored in the mailbox:

```
~/.tmux_orchestrator/{session_name}/{agent_id}/inbox/{msg_id}.json
```

```json
{
  "id": "uuid",
  "type": "PEER_MSG",
  "from_id": "worker-1",
  "to_id": "worker-2",
  "payload": {"text": "Can you run the tests?"},
  "timestamp": "2026-03-02T09:00:00+00:00"
}
```

### Message types

| Type | Purpose |
|---|---|
| `TASK` | Carry task dispatch information |
| `RESULT` | Agent publishes completion results |
| `STATUS` | Orchestrator lifecycle events (task_queued, subagent_spawned) |
| `PEER_MSG` | Agent-to-agent communication |
| `CONTROL` | Commands to the orchestrator (e.g., spawn_subagent) |

---

## 12. Sub-Agent Spawning

An agent can request the orchestrator to spawn a new worker under its supervision. The orchestrator automatically grants P2P permission between parent and child.

### Via REST API

```bash
curl -s -X POST http://localhost:8000/agents \
  -H "Content-Type: application/json" \
  -d '{
    "parent_id": "worker-1",
    "agent_type": "custom",
    "command": "python3 scripts/helper.py"
  }'
```

Response:
```json
{"status": "spawning", "parent_id": "worker-1"}
```

The spawned agent's ID is sent back to the parent as a STATUS bus message with:

```json
{
  "event": "subagent_spawned",
  "sub_agent_id": "worker-1-sub-a3f2c1",
  "parent_id": "worker-1"
}
```

### Via CONTROL message (programmatic)

```python
from tmux_orchestrator.bus import Message, MessageType

await bus.publish(Message(
    type=MessageType.CONTROL,
    from_id="worker-1",
    to_id="__orchestrator__",
    payload={
        "action": "spawn_subagent",
        "agent_type": "custom",
        "command": "python3 scripts/helper.py",
        "isolate": True,              # give it its own worktree (default)
        "share_parent_worktree": False,  # or True to reuse parent's cwd
    }
))
```

### Sub-agent ID format

Sub-agent IDs are auto-generated: `{parent_id}-sub-{6 hex chars}`, e.g., `worker-1-sub-a3f2c1`.

### From a Claude Code agent

```
/spawn-subagent custom python3 scripts/helper.py
```

Then watch for the STATUS message in your inbox, read the `sub_agent_id`, and delegate tasks:

```
/check-inbox
/read-message <id from inbox>
/send-message worker-1-sub-a3f2c1 Analyse the test failures in the latest CI run
```

---

## 13. Git Worktree Isolation

When running inside a git repository, each agent can be given its own isolated working tree. This prevents file conflicts when multiple agents work in parallel on the same codebase.

### How it works

1. On `start()`, the orchestrator calls `git worktree add .worktrees/{agent_id} -b worktree/{agent_id}`.
2. The agent's process (`claude` or custom script) runs with its `cwd` set to `.worktrees/{agent_id}/`.
3. On `stop()`, the orchestrator removes the worktree and deletes the branch.

```
repo/
├── .git/
├── .worktrees/          ← auto-created, gitignored
│   ├── worker-1/        ← worker-1's isolated checkout, branch worktree/worker-1
│   └── worker-2/        ← worker-2's isolated checkout, branch worktree/worker-2
├── src/
└── ...
```

### Opt-out with `isolate: false`

```yaml
agents:
  - id: read-only-agent
    type: custom
    command: "python3 scripts/reader.py"
    isolate: false   # runs directly in the main working tree
```

Use this for agents that only read files or for simple scripts that don't need isolation.

### Non-git directories

If the orchestrator is started outside a git repository, worktree isolation is automatically disabled for all agents. A warning is logged:

```
WARNING Not inside a git repository; worktree isolation disabled
```

Agents still start and run normally, just without worktree isolation.

### `.gitignore`

`WorktreeManager` automatically adds `.worktrees/` to the repo's `.gitignore` on first use. If no `.gitignore` exists, one is created.

### Sharing the parent worktree (sub-agents only)

When spawning a sub-agent, pass `share_parent_worktree: true` to have the sub-agent run in the same directory as its parent instead of getting a new worktree:

```python
payload={
    "action": "spawn_subagent",
    "agent_type": "custom",
    "command": "python3 scripts/validator.py",
    "share_parent_worktree": True,
}
```

---

## 14. Web UI & REST API

Start the web server:

```bash
tmux-orchestrator web --config myproject.yaml --port 8000
```

Open `http://localhost:8000` for the live browser dashboard.

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Browser dashboard (HTML) |
| `GET` | `/agents` | List all agents and their status |
| `POST` | `/agents` | Spawn a sub-agent |
| `DELETE` | `/agents/{id}` | Stop and remove an agent |
| `POST` | `/agents/{id}/message` | Send a message to an agent |
| `GET` | `/tasks` | List queued (pending) tasks |
| `POST` | `/tasks` | Submit a new task |
| `WS` | `/ws` | WebSocket stream of all bus events |

### POST /tasks

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Hello!", "priority": 0, "metadata": {}}'
```

### POST /agents/{id}/message

```bash
curl -X POST http://localhost:8000/agents/worker-1/message \
  -H "Content-Type: application/json" \
  -d '{"type": "PEER_MSG", "payload": {"text": "Hello from the UI"}}'
```

Note: REST messages are sent with `from_id = "__user__"` and always bypass P2P permission checks.

### POST /agents (spawn sub-agent)

```bash
curl -X POST http://localhost:8000/agents \
  -H "Content-Type: application/json" \
  -d '{"parent_id": "worker-1", "agent_type": "custom", "command": "python3 scripts/worker.py"}'
```

### WebSocket

Connect to `ws://localhost:8000/ws` to receive a real-time stream of all bus events as JSON:

```json
{
  "id": "uuid",
  "type": "STATUS",
  "from_id": "__orchestrator__",
  "to_id": "*",
  "payload": {"event": "task_queued", "task_id": "…"},
  "timestamp": "2026-03-02T09:00:00+00:00"
}
```

---

## 15. Slash Commands (for Claude Code Agents)

When a Claude Code agent starts, the orchestrator writes `__orchestrator_context__.json` to its working directory. This file enables the following slash commands, available in every agent's Claude session:

| Command | Usage | Description |
|---|---|---|
| `/check-inbox` | `/check-inbox` | List unread messages (ID, from, type, payload preview) |
| `/read-message` | `/read-message <msg_id>` | Read full message content, mark as read |
| `/send-message` | `/send-message <agent_id> <text>` | Send a PEER_MSG to another agent |
| `/spawn-subagent` | `/spawn-subagent <type> [cmd]` | Spawn a sub-agent with auto P2P |
| `/list-agents` | `/list-agents` | Show all agents with status |

Commands that call the REST API (`/send-message`, `/spawn-subagent`, `/list-agents`) require the orchestrator to be running in `web` mode. Commands that use the mailbox files directly (`/check-inbox`, `/read-message`) work in all modes.

---

## 16. Troubleshooting

### Agent doesn't start

- Verify tmux is running: `tmux list-sessions`
- Check the session name in your config matches an existing session, or let the orchestrator create it.
- Run with `--verbose` for debug logs.

### Task never completes (ClaudeCodeAgent stuck)

The orchestrator waits for pane output to settle and end with `$`, `>`, or `Human:`. If `claude` is still streaming or the prompt is not recognised:

- Increase `_SETTLE_CYCLES` in `claude_code.py` (default 3 × 500 ms = 1.5 s).
- Check the `_DONE_PATTERNS` list matches your version of the `claude` CLI.
- Attach to the tmux pane to observe the state: `tmux attach -t {session_name}`.

### P2P message not delivered

- Confirm the sender/receiver pair is in `p2p_permissions` in the config.
- Check the orchestrator log for: `P2P {from} → {to} blocked (not in permission table)`.
- Use `POST /agents/{id}/message` via the REST API as a workaround — it always delivers.

### Worktree error on start

```
fatal: '<path>' is already checked out at '...'
```

A previous run left a stale worktree. Clean it up:

```bash
git worktree list              # see all registered worktrees
git worktree remove --force .worktrees/worker-1
git branch -D worktree/worker-1
```

Or simply delete the `.worktrees/` directory:

```bash
rm -rf .worktrees/
git worktree prune
```

### `WorktreeManager: Not inside a git repository`

You're running the orchestrator outside a git repo. Worktree isolation is automatically disabled. This is not an error — agents will run in the current directory. Add `isolate: false` explicitly to silence the warning or run from inside a git repo.

### Slash commands fail with "connection refused"

The `/send-message`, `/spawn-subagent`, and `/list-agents` commands call the REST API. Ensure:

1. The orchestrator was started with `tmux-orchestrator web` (not `tui`).
2. The `web_base_url` in your config matches the actual host:port.
3. No firewall blocks the port.

### Mailbox directory not found

The mailbox defaults to `~/.tmux_orchestrator`. If you changed `mailbox_dir` in the config, make sure the path is writable. The orchestrator creates subdirectories automatically.
