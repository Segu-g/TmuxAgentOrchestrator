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
uv sync --extra dev
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
Choose → Research → Implement → Unit Tests → E2E Demo → Feedback → Choose → …
```

Each step is **required**. Never skip to the next step until the current one is complete.

#### Step 0 — Choose the next feature — MANDATORY, NO EXCEPTIONS
- **The very first action of every iteration is to choose what to build.**
- Read `DESIGN.md §11` (候補一覧) and the most recent `build-log.md` under `~/Demonstration/`.
- Select the highest-priority uncompleted item from `DESIGN.md §11`, taking into account
  any bugs or quality gaps surfaced in the previous demo's `build-log.md`.
- Write a brief selection rationale in `DESIGN.md §10.N` (the new research section for this
  iteration) before proceeding:
  - *What* you chose and *why* (priority, dependency on previous work, demo feedback)
  - *What* you decided NOT to choose and why
- Do not begin WebSearch or any implementation until the choice is written down.

#### Step 1 — Research (before any code) — MANDATORY, NO EXCEPTIONS
- **Search before reading code.**
  Do NOT read any code file, do NOT write any code, until web research is complete.
- Search for academic papers, blog posts, RFC specifications, and SE books relevant
  to the chosen feature. Minimum 3 queries with `WebSearch`; follow up with `WebFetch`
  for relevant pages.
- Record findings in the same `DESIGN.md §10.N` section with full citations (author, title, URL, year).
- **References must come from actual web searches, not from training knowledge alone.**
  Verify URLs are reachable. Then and only then begin implementation.

#### Step 2 — Implement + unit tests
- Follow the normal TDD cycle (Red → Green → Refactor).
- All existing tests must remain green. `uv run pytest tests/ -x -q`
- Commit in logical units. Push to origin.

#### Step 3 — E2E demonstration with REAL agents
- Use `task_timeout: 900` in the demo YAML config. Real claude agents can take time;
  never shrink or simplify tasks to avoid timeouts — increase the timeout instead.
- After each feature set, create `~/Demonstration/v<version>-<topic>/demo.py`.
  Folder name format: `v{major}.{minor}.{patch}-{kebab-case-topic}`
  Example: `v0.13.0-reset-and-metrics`
- **Write `demo.py`, then immediately run it from the project root:**
  ```
  PYTHONPATH=src uv run python ~/Demonstration/v<version>-<topic>/demo.py
  ```
  Running the demo is not optional — writing the script without executing it does not count.
- If the demo fails, debug and fix the root cause before proceeding to Step 4.
  Do not move on while the demo is broken.
- **Demonstrations MUST use real `ClaudeCodeAgent` instances** running actual `claude`
  processes in real tmux panes. They must produce real artefacts (files, test results).
- Mocks, `HeadlessAgent`, and any in-process fake agents (`FastAgent`, `SlowAgent`, etc.)
  are appropriate in unit tests only. They must NOT appear in demonstrations.
  **Inspecting queue state or `_task_priorities` without dispatching real tasks does not count
  as a demonstration.** The demo must show tasks being dispatched to and completed by real agents.

**Demos must justify multi-agent orchestration.**
A demo with 1 agent doing 1 task is not sufficient — it fails to demonstrate the value
of this framework. Every demo MUST involve genuine multi-agent collaboration, chosen
from patterns like:

| Pattern | Example |
|---|---|
| Parallel specialisation | agent-a writes implementation, agent-b writes tests simultaneously; orchestrator merges results |
| Pipeline / dependency chain | agent-a produces an artefact that agent-b consumes (real file dependency) |
| Director → workers | Director agent breaks a task into subtasks via P2P, delegates to 2+ workers, aggregates results |
| Peer review | agent-a writes code, agent-b reviews and requests fixes, agent-a revises |
| Competitive / best-of-N | multiple agents solve the same problem; orchestrator picks the best result |

Minimum bar: **2 agents with meaningful interaction** (one agent's output is an input to
another, OR agents communicate via P2P messages, OR a Director coordinates workers).
A demo where 2 agents write independent files without any coordination does not count.

**Recommended concrete scenario — AtCoder Heuristic Contest (AHC):**
AHC problems are ideal for best-of-N validation because:
- The problem statement and scoring function are fully specified (no ambiguity)
- Multiple agents independently generate solutions using different strategies/seeds
- Scores are objective and comparable — the orchestrator can deterministically pick the winner
- Agents can run in true parallel (each tackles the same input, different approach)

Workflow:
1. Fetch an AHC problem statement (e.g. from atcoder.jp or a local copy)
2. Spawn N agents (N ≥ 3), each given the same problem + a different strategy hint
   (e.g. "greedy", "random restart", "simulated annealing")
3. Each agent writes a solver script and runs it, producing a score
4. Orchestrator collects scores and selects the highest
5. Demo verifies: correct number of solutions produced, scores are numeric, winner is selected

Use past AHC problems (e.g. AHC001–AHC030) which are publicly available and have
well-defined offline scoring tools.

- After the demo runs successfully, record actual results in `~/Demonstration/v<version>-<topic>/build-log.md`:
  - Actual output observed (stdout, files created, REST responses)
  - What passed / what failed
  - Root cause of every failure and the fix applied
  - Open a GitHub Issue only if a fix requires user input

#### Step 4 — Feedback loop
- Every bug or quality gap found in the demo **must** become either:
  a. A fix committed in the current iteration (if the root cause is clear), or
  b. A GitHub Issue if user input is required, or
  c. A prioritised candidate in `DESIGN.md §11` for the next iteration.
- After fixing demo bugs, re-run the demo to confirm all checks pass.
- Summarise findings (what worked, what broke, what was improved) at the top of `build-log.md`.
- Then begin Step 1 (WebSearch) for the next iteration.

### When to Open a GitHub Issue
Open an issue (not a user question) when:
- A design decision requires user preference (e.g., API shape, UX trade-off).
- A bug cannot be diagnosed without information only the user has.
- An architectural change is large enough that user sign-off is prudent.

**Never block the autonomous loop waiting for a response.** After opening the issue,
continue with whatever iteration candidates remain in `DESIGN.md §11`.

### Permanent Rules
- `DESIGN.md` is the source of truth for design decisions, references, and iteration history.
- `DESIGN.md §11` always lists the next candidates in priority order.
- Every demo's `build-log.md` must be written before moving to the next iteration.
- After adding or changing any REST endpoint, regenerate the OpenAPI snapshot:
  `UPDATE_SNAPSHOTS=1 uv run pytest tests/test_openapi_schema.py`
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

### API Key for Authenticated Requests

REST endpoints require an `X-API-Key` header. The key is delivered securely through two channels:

1. **Environment variable** `TMUX_ORCHESTRATOR_API_KEY` — set on the tmux session; your shell
   inherits it automatically. Use `os.environ.get("TMUX_ORCHESTRATOR_API_KEY", "")` in Python
   or `$TMUX_ORCHESTRATOR_API_KEY` in shell scripts.
2. **Key file** `__orchestrator_api_key__` in your working directory — written with `chmod 600`;
   contains the raw key on a single line.  Read it as a fallback when the env var is absent.

Quick pattern for Python slash commands:

```python
import os
from pathlib import Path

api_key = os.environ.get("TMUX_ORCHESTRATOR_API_KEY", "")
if not api_key:
    kf = Path("__orchestrator_api_key__")
    if kf.exists():
        api_key = kf.read_text().strip()

headers = {"Content-Type": "application/json"}
if api_key:
    headers["X-API-Key"] = api_key
```

The key is **not** stored in `__orchestrator_context__.json` (security fix v0.35.0).

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
| `/plan` | `/plan <description>` | Write a structured `PLAN.md` before beginning implementation |
| `/tdd` | `/tdd <feature>` | Guide a Red → Green → Refactor TDD cycle |
| `/progress` | `/progress <summary>` | Report progress to your parent agent in the hierarchy |
| `/summarize` | `/summarize` | Compress current context state into `NOTES.md` to prevent context rot |
| `/delegate` | `/delegate <task>` | Break a task into subtasks and spawn sub-agents to work on them |

All commands require `__orchestrator_context__.json` in your cwd.
Commands that use REST (`/send-message`, `/spawn-subagent`, `/list-agents`, `/progress`, `/delegate`) require the orchestrator to have been started with `tmux-orchestrator web`.

### Shared Scratchpad

The shared scratchpad is a server-side key/value store for passing data between agents without P2P messaging. It implements the Blackboard pattern — one agent writes results, another reads them.

```bash
# Write a value
curl -X PUT {web_base_url}/scratchpad/my-key \
  -H "X-API-Key: $TMUX_ORCHESTRATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"value": "result data here"}'

# Read a value
curl {web_base_url}/scratchpad/my-key -H "X-API-Key: $TMUX_ORCHESTRATOR_API_KEY"

# List all entries
curl {web_base_url}/scratchpad/ -H "X-API-Key: $TMUX_ORCHESTRATOR_API_KEY"

# Delete an entry
curl -X DELETE {web_base_url}/scratchpad/my-key -H "X-API-Key: $TMUX_ORCHESTRATOR_API_KEY"
```

Use the scratchpad for pipeline workflows (agent-a writes an artefact path, agent-b reads it) rather than embedding large payloads in P2P messages.

### Submitting Tasks via REST (Director Pattern)

Director agents and workers with `POST /tasks` access can submit tasks directly. Key fields:

| Field | Type | Description |
|---|---|---|
| `prompt` | `str` | Task instruction text |
| `priority` | `int` | Lower = dispatched first (default `0`) |
| `reply_to` | `str\|null` | Agent ID that receives the RESULT in its mailbox; triggers `__MSG__:{id}` notification |
| `target_agent` | `str\|null` | Force dispatch to a specific agent; task waits until that agent is idle |
| `required_tags` | `list[str]` | Only dispatch to agents whose `tags` include ALL listed tags |

When `reply_to` is set to your `agent_id`, the orchestrator writes the RESULT to your mailbox and types `__MSG__:{id}` into your pane — the same mechanism as P2P. This is the canonical way for a Director to receive worker results without polling.

### Context Monitor Events

The orchestrator's `ContextMonitor` polls each agent's tmux pane and publishes STATUS events on the bus. Director agents that subscribe to the bus can react to these events to proactively rotate or re-brief workers.

| Event | Key payload fields | Meaning |
|---|---|---|
| `context_warning` | `agent_id`, `estimated_tokens`, `context_pct`, `context_window_tokens` | Agent pane output exceeds `context_warn_threshold` |
| `notes_updated` | `agent_id`, `notes_path`, `notes_mtime`, `preview` | Agent's `NOTES.md` was modified (e.g. after `/summarize`) |
| `summarize_triggered` | `agent_id`, `estimated_tokens`, `context_pct` | `/summarize` was auto-injected into the agent pane |

If you receive a `context_warning` directed at your own `agent_id`, run `/summarize` to compress your context before it degrades your recall. Per-agent stats are available at `GET /agents/{id}/stats`.

### Agent Task History

You can query your own or sibling agents' recent task history for coordination and self-awareness:

```
GET {web_base_url}/agents/{agent_id}/history?limit=20
```

Each record contains `task_id`, `prompt`, `started_at`, `finished_at`, `duration_s`, `status` (`"success"` or `"error"`), and `error` (null on success). Results are ordered most-recent-first.

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
- `$` — shell prompt (any trailing whitespace is tolerated)
- `>` — bare prompt (older Claude versions)
- `Human:` — Claude conversation prompt

Ensure your final output settles at a recognisable prompt. Do not leave the pane in the middle of streaming output when you are finished.

### Worktree Isolation

By default you run in an isolated git worktree at `{repo_root}/.worktrees/{agent_id}/` on branch `worktree/{agent_id}`. This means:

- Your filesystem changes do not affect other agents.
- Commit freely on your branch.
- On agent stop, your worktree and branch are automatically deleted.

If the config sets `isolate: false` for your agent, you share the main repo working tree.
