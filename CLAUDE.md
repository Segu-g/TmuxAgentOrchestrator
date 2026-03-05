# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**TmuxAgentOrchestrator** — orchestrates Claude Code agents inside tmux panes. An orchestrator process manages a pool of worker agents, dispatches tasks from a priority queue, and optionally permits peer-to-peer messaging between workers. The primary interface is a Textual TUI with a FastAPI/WebSocket web UI layer on top.

## Directory Layout

```
TmuxAgentOrchestrator/
├── pyproject.toml                  # build config + dependencies
├── CLAUDE.md
├── src/
│   └── tmux_orchestrator/
│       ├── __init__.py
│       ├── main.py                 # CLI entry point (typer): tui / web / run
│       ├── config.py               # YAML config loader + dataclasses
│       ├── tmux_interface.py       # libtmux wrapper (sessions, panes, watcher thread)
│       ├── bus.py                  # Async in-process pub/sub message bus
│       ├── orchestrator.py         # Task queue, agent registry, dispatch, P2P gating
│       ├── agents/
│       │   ├── base.py             # Abstract Agent (lifecycle, status, run loop)
│       │   └── claude_code.py      # Drives `claude` CLI in a tmux pane
│       ├── tui/
│       │   ├── app.py              # Textual App root (keybindings: n/k/p/q)
│       │   └── widgets.py          # AgentPanel, TaskQueuePanel, LogPanel, StatusBar
│       └── web/
│           ├── app.py              # FastAPI app (REST + embedded browser UI)
│           └── ws.py               # WebSocket hub (fans out bus events to browsers)
├── examples/
│   └── basic_config.yaml           # Two-worker example config
└── tests/
    ├── test_bus.py
    ├── test_orchestrator.py
    ├── test_tmux_interface.py
    └── test_worktree.py
```

## Installation

```bash
pip install -e ".[dev]"
```

## Running

```bash
# Launch Textual TUI
tmux-orchestrator tui --config examples/basic_config.yaml

# Launch web server (http://localhost:8000)
tmux-orchestrator web --config examples/basic_config.yaml --port 8000

# Headless: submit one task, print result
tmux-orchestrator run --config examples/basic_config.yaml --prompt "hello"

# Module invocation also works
python -m tmux_orchestrator.main tui
```

## Running Tests

```bash
pytest
```

## Architecture Notes

- **Bus** (`bus.py`): async pub/sub; `to_id="*"` = broadcast; `broadcast=True` subscriber receives all messages regardless of `to_id`. Used by web hub and TUI.
- **Orchestrator** (`orchestrator.py`): `asyncio.PriorityQueue` for tasks; polls for idle agents every 0.2 s; P2P permission table is a `Set[frozenset[str]]`.
- **ClaudeCodeAgent**: launches `claude --dangerously-skip-permissions` (with `CLAUDECODE` stripped so it works inside a Claude Code session); waits for the initial `❯` prompt (`_wait_for_ready`) before marking IDLE; sends task via `send_keys`; polls pane output every 500 ms; declares completion when output settles for 3 consecutive cycles and matches a prompt pattern (`❯`, `$`, `>`, or `Human:`).
- **Web UI**: single-page HTML served from `GET /`; auto-reconnecting WebSocket at `ws://host/ws`; polls REST endpoints every 3 s for agent/task table refresh.

## Autonomous Development Loop

**This entire process runs without asking the user for approval.**
Ask the user (via GitHub Issues) only when a fundamental design decision cannot
be resolved with research alone.

### The Mandatory Cycle

```
Research → Implement → Unit Tests → E2E Demo → Feedback → Research → …
```

Each step is **required**. Never skip to the next step until the current one is complete.

#### Step 1 — Research first (before every iteration)
- Search for academic papers, blog posts, and SE books relevant to the planned improvement.
- Use `WebSearch` and `WebFetch` to collect references. Record findings in `DESIGN.md`
  with citations (author, title, URL, year).
- Write a brief justification: *why* this improvement, *what* the literature says, *how*
  it maps to the codebase. Then and only then begin implementation.

#### Step 2 — Implement + unit tests
- Follow the normal TDD cycle (Red → Green → Refactor).
- All existing tests must remain green. `uv run pytest tests/ -x -q`
- Commit in logical units. Push to origin.

#### Step 3 — E2E demonstration with REAL agents
- After each feature set, create a demonstration under `~/Demonstration/v<version>-<topic>/demo.py`.
  The folder name MUST include the version number, e.g. `v0.13.0-reset-and-metrics`.
  Format: `v{major}.{minor}.{patch}-{kebab-case-topic}`
- **Demonstrations MUST use real `ClaudeCodeAgent` instances** running actual `claude`
  processes in real tmux panes. They must produce real artefacts (files, test results).
- Mocks and `HeadlessAgent` variants (`FastAgent`, `SlowAgent`, etc.) are useful and
  appropriate in unit tests and infrastructure benchmarks (e.g. `complex_pipeline`).
  They must NOT be used in demonstrations — the point of a demo is to validate that the
  real system works end-to-end, which mocks cannot do.
- Document demo results in `~/Demonstration/<topic>/build-log.md`:
  - What passed / what failed
  - Root cause of every failure
  - Fix applied (or GitHub Issue opened if it needs user input)

#### Step 4 — Feedback loop
- Every bug or quality gap found in the demo **must** become either:
  a. A fix in the current iteration (if the root cause is clear), or
  b. A GitHub Issue if user input is required, or
  c. A candidate in `DESIGN.md §10.5` for the next iteration.
- After fixing demo bugs, re-run the demo to confirm all checks pass.
- Then begin the next Research step for the next iteration.

### When to Open a GitHub Issue
Open an issue (not a user question) when:
- A design decision requires user preference (e.g., API shape, UX trade-off).
- A bug cannot be diagnosed without information only the user has.
- An architectural change is large enough that user sign-off is prudent.

**Never block the autonomous loop waiting for a response.** After opening the issue,
continue with whatever iteration candidates remain in `DESIGN.md §10.5`.

### Permanent Rules
- `DESIGN.md` is the source of truth for design decisions, references, and iteration history.
- `DESIGN.md §10.5` always lists the next candidates in priority order.
- Every demo's `build-log.md` must be written before moving to the next iteration.
- Do not use `--no-verify` or skip tests to meet a deadline.

## Key Decisions

- `libtmux` is the sole tmux binding; pane watcher runs in a daemon thread, uses `asyncio.run_coroutine_threadsafe` to publish to the async bus.
- `TmuxInterface.ensure_session()` always creates a **fresh** tmux session. If a session with the same name exists, the user is prompted via a `confirm_kill: Callable[[str], bool] | None` callback (wired to `typer.confirm` in `main.py`); declining raises `RuntimeError`. `kill_session()` is called on orchestrator shutdown to clean up.
- Textual TUI and FastAPI web server are two separate CLI commands (`tui` vs `web`) that share the same core components.
- P2P routing is bidirectional per permission entry — stored as `frozenset` pairs.
- Sub-agent spawning via CONTROL message requires a `template_id` matching an agent defined in the YAML config — arbitrary commands cannot be injected at runtime.

---

## Running as an Orchestrated Agent

If you are a Claude Code instance launched by TmuxAgentOrchestrator, this section explains your environment and how to use inter-agent communication.

### Your Identity

At startup the orchestrator writes a context file to your working directory:

```
__orchestrator_context__.json
```

Contents:

```json
{
  "agent_id": "worker-1",
  "session_name": "orchestrator",
  "mailbox_dir": "/home/user/.tmux_orchestrator",
  "worktree_path": "/path/to/repo/.worktrees/worker-1",
  "web_base_url": "http://localhost:8000"
}
```

Read this file to know your `agent_id`, where your mailbox is, and the REST API base URL.

### Receiving Messages

When a message is sent to you, the orchestrator types `__MSG__:{msg_id}` into your pane as a notification. **Do not respond to this text literally** — it is a trigger to check your inbox.

Typical workflow on receiving `__MSG__:{id}`:
1. Run `/check-inbox` — lists all unread messages with from/type/payload preview.
2. Run `/read-message <msg_id>` — shows full JSON, marks message as read.

Message schema (JSON file in `{mailbox_dir}/{session_name}/{agent_id}/inbox/`):

```json
{
  "id": "uuid",
  "type": "PEER_MSG",
  "from_id": "worker-2",
  "to_id": "worker-1",
  "payload": { "text": "…" },
  "timestamp": "2026-03-02T08:00:00+00:00"
}
```

### Sending Messages

Use `/send-message <target_agent_id> <message text>`.

The orchestrator enforces P2P permissions. Your message is silently dropped if the pair `{your_id, target_id}` is not in the config's `p2p_permissions` table. Permissions are automatically granted for sub-agents you spawn.

### Slash Command Reference

| Command | Usage | What it does |
|---|---|---|
| `/check-inbox` | `/check-inbox` | List unread messages (ID, from, type, payload preview) |
| `/read-message` | `/read-message <msg_id>` | Read a message in full, mark it as read |
| `/send-message` | `/send-message <agent_id> <text>` | Send a PEER_MSG to another agent |
| `/spawn-subagent` | `/spawn-subagent <template_id>` | Spawn a pre-configured sub-agent; P2P auto-granted |
| `/list-agents` | `/list-agents` | Show all agents and their IDLE/BUSY/ERROR status |

All commands require `__orchestrator_context__.json` in your cwd.
Commands that use REST (`/send-message`, `/spawn-subagent`, `/list-agents`) require the orchestrator to have been started with `tmux-orchestrator web`.

### Agent Lifecycle Principle

**Workers are ephemeral** — spawn one per task or phase, not reused across different task types.
Each agent is created with a specific role (via `system_prompt`) and should complete its purpose then stop.
The orchestrator context (`CLAUDE.md`, `__orchestrator_context__.json`) is written at startup and
is intentionally immutable during the agent's lifetime. If your task scope changes, spawn a new sub-agent.

### Spawning Sub-Agents

Use `/spawn-subagent <template_id>` to create a helper agent, where `template_id` is the `id` of an agent already defined in the YAML config:

```
/spawn-subagent worker-2
```

The orchestrator instantiates a new `ClaudeCodeAgent` based on that config entry (inheriting its `isolate` setting), assigns it a unique ID like `worker-1-sub-a3f2c1`, and auto-grants P2P between you and the sub-agent.

After spawning, the orchestrator sends you a STATUS message:
```json
{ "event": "subagent_spawned", "sub_agent_id": "worker-1-sub-a3f2c1", "parent_id": "worker-1" }
```
Retrieve it with `/check-inbox` → `/read-message`, then delegate with `/send-message`.

### Task Completion

The orchestrator detects task completion by polling your pane output. It declares you done when the output **has not changed for 3 consecutive 500 ms polls** and the last line matches one of:

- `❯` — Claude interactive prompt (current default)
- `$` or `$ ` — shell prompt
- `>` — bare prompt (older Claude versions)
- `Human:` — Claude conversation prompt

Ensure your final output settles at a recognisable prompt. Do not leave the pane in the middle of streaming output when you are finished.

### Worktree Isolation

By default you run in an isolated git worktree at `{repo_root}/.worktrees/{agent_id}/` on branch `worktree/{agent_id}`. This means:

- Your filesystem changes do not affect other agents.
- Commit freely on your branch.
- On agent stop, your worktree and branch are automatically deleted.

If the config sets `isolate: false` for your agent, you share the main repo working tree.
