# TmuxAgentOrchestrator — Test Suite Reference

## Table of Contents

1. [Overview](#1-overview)
2. [Running the Tests](#2-running-the-tests)
3. [Test Architecture](#3-test-architecture)
4. [test_bus.py — Message Bus (6 tests)](#4-test_buspy--message-bus-6-tests)
5. [test_messaging.py — Mailbox (10 tests)](#5-test_messagingpy--mailbox-10-tests)
6. [test_orchestrator.py — Task Dispatch, P2P & Timeouts (11 tests)](#6-test_orchestratorpy--task-dispatch-p2p--timeouts-11-tests)
7. [test_tmux_interface.py — tmux Wrapper (8 tests)](#7-test_tmux_interfacepy--tmux-wrapper-8-tests)
8. [test_worktree.py — Git Worktree Manager (9 tests)](#8-test_worktreepy--git-worktree-manager-9-tests)
9. [Test Infrastructure](#9-test-infrastructure)
10. [Coverage & Gaps](#10-coverage--gaps)
11. [Adding New Tests](#11-adding-new-tests)

---

## 1. Overview

The suite has **43 tests** across 5 files. Every public behaviour of the core library modules is covered. Integration with real external processes (tmux server, `claude` CLI) is avoided entirely — those are replaced with mocks or temporary real git repositories.

```
tests/
├── test_bus.py             6 tests  — async pub/sub message bus
├── test_messaging.py      10 tests  — file-based mailbox (4 classes)
├── test_orchestrator.py   11 tests  — task queue, dispatch, P2P routing, timeouts, events
├── test_tmux_interface.py  8 tests  — libtmux wrapper
└── test_worktree.py        9 tests  — git worktree lifecycle (real git)
```

**Run result:**

```
43 passed in ~3.4 s
```

---

## 2. Running the Tests

```bash
# All tests, verbose
uv run pytest tests/ -v

# Single file
uv run pytest tests/test_bus.py -v

# Single test by name
uv run pytest tests/test_orchestrator.py::test_p2p_allowed -v

# Show print output (useful for debugging)
uv run pytest tests/ -v -s

# Stop on first failure
uv run pytest tests/ -x
```

**Requirements:** `uv sync --extra dev` installs `pytest>=8` and `pytest-asyncio>=0.23`. All async tests run automatically with `asyncio_mode = "auto"` (set in `pyproject.toml`).

---

## 3. Test Architecture

### Isolation strategy

| Module under test | Isolation method |
|---|---|
| `bus.py` | Pure in-process asyncio, no I/O |
| `messaging.py` | `tmp_path` fixture (pytest provides a temp dir) |
| `orchestrator.py` | `DummyAgent` replaces real agents; `MagicMock` replaces tmux |
| `tmux_interface.py` | `@patch("…libtmux.Server")` mocks the tmux server |
| `worktree.py` | Real `git init` in `tmp_path`; no mocking |

### Async test runner

All async test functions are discovered and run automatically. The `pyproject.toml` setting:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

This removes the need for `@pytest.mark.asyncio` decorators.

### Key shared helpers

**`DummyAgent`** (in `test_orchestrator.py`):

A minimal `Agent` subclass used instead of `ClaudeCodeAgent`. It:
- Sets status to `IDLE` on `start()` and launches `_run_loop`.
- Appends each received task to `self.dispatched` in `_dispatch_task`, then calls `_set_idle()`.
- Implements no-op `stop()`, `handle_output()`, `notify_stdin()`.

This lets orchestrator tests verify dispatch logic without requiring a live tmux pane or subprocess.

---

## 4. `test_bus.py` — Message Bus (6 tests)

Tests `Bus`, `Message`, and `MessageType` from `tmux_orchestrator.bus`.

### Fixture

```python
@pytest.fixture
def bus() -> Bus:
    return Bus()
```

A fresh `Bus` instance per test; no shared state.

---

### `test_broadcast_delivery`

**What it tests:** A `Message` published with the default `to_id="*"` (broadcast sentinel) is delivered to every subscriber, regardless of their agent ID.

**Setup:** Two subscribers (`agent-a`, `agent-b`). One STATUS broadcast message.

**Assertions:**
- Both queues have exactly 1 item.
- The item in `q_a` has the same `id` as the published message.

**Why this matters:** The broadcast channel is used by the TUI, web hub, and orchestrator router — all of which need to see every message without registering for specific IDs.

---

### `test_directed_delivery`

**What it tests:** A directed message (`to_id="agent-a"`) reaches only the intended recipient.

**Setup:** Two subscribers. One TASK message addressed to `agent-a`.

**Assertions:**
- `q_a.qsize() == 1`
- `q_b.qsize() == 0`

**Why this matters:** Prevents message leakage between agents. A RESULT or PEER_MSG intended for one agent must not appear in another agent's queue.

---

### `test_broadcast_subscriber_receives_all`

**What it tests:** A subscriber registered with `broadcast=True` receives directed messages even when they are addressed to a different agent ID.

**Setup:** `hub` subscribes with `broadcast=True`; `agent-x` subscribes normally. A RESULT message is directed to `agent-x`.

**Assertions:**
- Both `q_hub` and `q_agent` each contain 1 message.

**Why this matters:** The TUI and WebSocket hub use `broadcast=True` to observe all traffic for display. The orchestrator's internal router also uses this to intercept PEER_MSG and CONTROL messages.

---

### `test_unsubscribe`

**What it tests:** After `bus.unsubscribe("gone")`, publishing a broadcast message does not add anything to the unsubscribed agent's queue.

**Assertions:** Queue size remains 0 after a publish.

**Why this matters:** Ensures no memory leak from accumulating messages for stopped agents.

---

### `test_queue_full_drops_message`

**What it tests:** When a subscriber's queue is at capacity (`maxsize=1`), publishing a second message silently drops it rather than raising an exception.

**Setup:** Subscribe with `maxsize=1`. Fill queue with first message. Publish a second.

**Assertions:** Queue still at size 1 (second message discarded).

**Why this matters:** The bus must be non-blocking and resilient to slow consumers. A slow TUI or web hub should not stall the orchestrator's dispatch loop.

---

### `test_message_iter`

**What it tests:** `Bus.iter_messages()` yields messages in FIFO order and calls `task_done()` correctly.

**Setup:** Subscribe, then publish 3 STATUS messages with payloads `{"n": 0}`, `{"n": 1}`, `{"n": 2}`.

**Assertions:** `received == [0, 1, 2]`.

**Why this matters:** `iter_messages` is used in the `run` CLI command to wait for a task result. Correct ordering and `task_done()` semantics are required for the `asyncio.Queue.join()` mechanism.

---

## 5. `test_messaging.py` — Mailbox (10 tests)

Tests the `Mailbox` class from `tmux_orchestrator.messaging`.

### Fixture

```python
@pytest.fixture
def mailbox(tmp_path: Path) -> Mailbox:
    return Mailbox(root_dir=tmp_path, session_name="test-session")
```

Each test gets a fresh temporary directory. No real `~/.tmux_orchestrator` is touched.

### Helper

```python
def _make_msg(to_id="agent-b", text="hello") -> Message:
    return Message(
        type=MessageType.PEER_MSG,
        from_id="agent-a",
        to_id=to_id,
        payload={"text": text},
    )
```

---

### `TestMailboxWrite`

#### `test_write_creates_file`

Calls `mailbox.write("agent-b", msg)` and verifies:
- The returned path exists on disk.
- The file parses as valid JSON with correct `id` and `payload.text`.

#### `test_write_in_inbox`

Verifies the file path returned by `write()` contains the string `"inbox"`, confirming it is placed in the `inbox/` subdirectory and not `read/`.

---

### `TestMailboxRead`

#### `test_read_from_inbox`

Writes a message, then reads it back with `mailbox.read("agent-b", msg.id)`. Asserts `data["id"] == msg.id`.

#### `test_read_missing_raises`

Calls `mailbox.read("agent-b", "nonexistent-id")` without writing anything first. Asserts `FileNotFoundError` is raised.

#### `test_read_after_mark_read`

Writes a message, calls `mark_read()`, then calls `read()` again. Asserts the message is still readable (from the `read/` directory). This verifies that marking a message as read does not destroy it.

---

### `TestMailboxListInbox`

#### `test_empty_inbox`

Calls `list_inbox()` on an agent with no messages. Asserts an empty list is returned (no error even though the directory doesn't exist).

#### `test_lists_messages`

Writes two messages with different IDs. Asserts `list_inbox()` returns both IDs (as a set comparison, since order is filesystem-dependent).

#### `test_mark_read_removes_from_inbox`

Writes a message, marks it as read, then calls `list_inbox()`. Asserts the list is empty — the message is no longer in `inbox/`.

---

### `TestMailboxMarkRead`

#### `test_mark_read_moves_file`

Writes a message, calls `mark_read()`, then directly checks that the file exists at the expected `read/` path:
```
{tmp_path}/test-session/agent-b/read/{msg.id}.json
```

This verifies the exact filesystem layout, which the `/read-message` slash command depends on.

#### `test_mark_read_nonexistent_noop`

Calls `mark_read("agent-b", "nonexistent-id")` on an empty mailbox. Asserts no exception is raised. Guards against crashes when the orchestrator tries to acknowledge a message that was already processed.

---

## 6. `test_orchestrator.py` — Task Dispatch, P2P & Timeouts (11 tests)

Tests `Orchestrator` from `tmux_orchestrator.orchestrator` using `DummyAgent` and `SlowDummyAgent`.

### Helpers

**`make_config(**kwargs)`** builds an `OrchestratorConfig` with sensible defaults (empty agents/permissions, 10 s timeout).

**`make_tmux_mock()`** returns a `MagicMock` satisfying the `TmuxInterface` interface.

**`SlowDummyAgent`** — an `Agent` subclass whose `_dispatch_task` sleeps for 9 999 seconds (effectively never completes). Used to trigger `task_timeout`.

---

### `test_submit_and_dispatch`

**What it tests:** A task submitted via `orch.submit_task()` is delivered to a registered idle agent.

**Setup:** One `DummyAgent("a1")`, registered and started. One task submitted.

**Timing:** Waits 300 ms for the dispatch loop (polling every 200 ms).

**Assertions:** `agent.dispatched` contains the task with the correct ID.

**Why this matters:** Core dispatch path — if this breaks, no work gets done.

---

### `test_no_idle_agent_requeues`

**What it tests:** When all agents are BUSY, a submitted task stays in the queue and is not dispatched.

**Setup:** Agent started (status reset to IDLE by `start()`), then manually set to BUSY after `orch.start()`. One task submitted.

**Assertions:** `agent.dispatched` is empty after 300 ms.

**Note:** The status must be set BUSY *after* calling `orch.start()` because `start()` calls `agent.start()` which resets the status to IDLE.

**Why this matters:** Verifies the orchestrator does not erroneously dispatch to a busy agent, and that the task survives in the queue.

---

### `test_p2p_allowed`

**What it tests:** A PEER_MSG between two agents in the permission table is forwarded and arrives at the recipient.

**Setup:** Config with `p2p_permissions=[("a1", "a2")]`. Subscribe `q_a2`. Call `orch.route_message()` directly.

**Assertions:**
- `q_a2.qsize() == 1` after 100 ms.
- The received message has the correct payload.

---

### `test_p2p_blocked`

**What it tests:** A PEER_MSG between agents without a permission entry is silently dropped.

**Setup:** Config with empty `p2p_permissions`. Subscribe `q_b`. Attempt to route a message from `"a"` to `"b"`.

**Assertions:** `q_b.qsize() == 0`.

---

### `test_pause_and_resume`

**What it tests:** `orch.pause()` prevents dispatch; `orch.resume()` re-enables it.

**Sequence:**
1. Pause → `is_paused == True`.
2. Submit task → wait 300 ms → `agent.dispatched` is empty.
3. Resume → wait 500 ms → `agent.dispatched` has 1 item.

**Why this matters:** The TUI `p` keybinding uses pause/resume to let users inspect queue state without agents consuming tasks.

---

### `test_list_agents`

**What it tests:** `orch.list_agents()` returns the correct IDs for all registered agents.

**Setup:** Two DummyAgents registered.

**Assertions:** The set of returned IDs equals `{"agent-1", "agent-2"}`.

---

### `test_task_timeout_publishes_result`

**What it tests:** When a task runs for longer than `task_timeout`, a `RESULT` message with `error: "timeout"` is published to the bus.

**Setup:** `SlowDummyAgent` with `task_timeout=0.1` s. A broadcast subscriber captures all messages. A task is sent and the test sleeps 500 ms.

**Assertions:** At least one captured `RESULT` message has `payload["task_id"] == "t-timeout"` and `payload["error"] == "timeout"`.

**Why this matters:** The `run` CLI command and any result-listening clients rely on this RESULT to know the task failed. Without it they would wait forever.

---

### `test_task_timeout_agent_returns_to_idle`

**What it tests:** After a timeout the agent's status is `IDLE`, not `BUSY` or `ERROR`.

**Setup:** Same `SlowDummyAgent`, `task_timeout=0.1` s, wait 500 ms.

**Assertions:** `agent.status == AgentStatus.IDLE`.

**Why this matters:** An agent stuck in BUSY after a timeout would never receive further tasks. Returning to IDLE is required for the agent pool to keep functioning.

---

### `test_agent_busy_event_published`

**What it tests:** When a task starts executing, an `agent_busy` STATUS event is published on the bus.

**Setup:** A regular `DummyAgent`, a broadcast subscriber, one task submitted.

**Assertions:** At least one `STATUS` message with `event == "agent_busy"` and `agent_id == "ev-1"` is received within 300 ms.

**Why this matters:** The TUI and WebSocket clients use these events to update the UI in real time without polling. Missing the event would leave the agent shown as IDLE while it is working.

---

### `test_agent_idle_event_published`

**What it tests:** After a task completes successfully, an `agent_idle` STATUS event is published.

**Setup:** Same as above; after task completion is expected, the subscriber queue is drained.

**Assertions:** At least one `STATUS` message with `event == "agent_idle"` and `agent_id == "ev-2"`.

**Why this matters:** Completes the lifecycle notification pair. Without `agent_idle`, the UI would show the agent as BUSY indefinitely after the task finishes.

---

## 7. `test_tmux_interface.py` — tmux Wrapper (8 tests)

Tests `TmuxInterface` and the `_hash` helper from `tmux_orchestrator.tmux_interface`. All tests that touch the tmux server mock `libtmux.Server`.

---

### `test_hash_deterministic`

Asserts `_hash("hello") == _hash("hello")` and `_hash("hello") != _hash("world")`. Basic sanity for the pane-ID hashing used to track watched panes.

### `test_hash_uses_md5`

Computes the expected MD5 hex digest of `"test content"` using the stdlib and asserts `_hash()` returns the same value. Locks in the exact algorithm so behavioural changes are caught.

---

### `test_ensure_session_creates_new`

**Scenario:** No existing session matches the name.

**Mock setup:** `mock_server.find_where.return_value = None`.

**Assertions:** `mock_server.new_session` is called once with `session_name="test-session"`, and the returned session object is forwarded.

---

### `test_ensure_session_kills_existing_and_creates_fresh`

**Scenario:** A session with the given name already exists and the user confirms the kill.

**Mock setup:** `mock_server.find_where.return_value = existing_mock`; `TmuxInterface` constructed with `confirm_kill=lambda _: True`.

**Assertions:** `existing.kill_session()` is called once; `new_session` is then called to create a fresh session; the new session is returned.

---

### `test_ensure_session_aborts_when_user_declines`

**Scenario:** A session with the given name already exists but the user declines the confirmation prompt.

**Mock setup:** `mock_server.find_where.return_value = existing_mock`; `TmuxInterface` constructed with `confirm_kill=lambda _: False`.

**Assertions:** `pytest.raises(RuntimeError, match="already exists")` — a `RuntimeError` is raised. `existing.kill_session()` is **not** called.

**Why this matters:** Prevents accidental destruction of an existing session. The `confirm_kill` callback is how `main.py` wires in `typer.confirm` with `default=False`, making "abort" the safe default.

---

### `test_watch_and_unwatch_pane`

Creates a `TmuxInterface`, calls `watch_pane(pane, "agent-1")`, then `unwatch_pane(pane)`.

**Assertions:**
- After `watch_pane`: `iface._watched` contains the pane's ID (`"%42"`).
- After `unwatch_pane`: `iface._watched` no longer contains it.

Verifies the watch registry is correctly maintained — used by the background watcher thread to know which panes to poll.

---

### `test_send_keys_delegates`

Calls `iface.send_keys(mock_pane, "echo hello")`.

**Assertions:** `mock_pane.send_keys` was called with `("echo hello", enter=True)`.

Confirms the `enter=True` flag is always passed (the default), which is required for commands to execute.

---

### `test_capture_pane_joins_lines`

`mock_pane.capture_pane()` returns `["line 1", "line 2", "line 3"]`.

**Assertions:** `iface.capture_pane(pane)` returns `"line 1\nline 2\nline 3"`.

Verifies the newline-join logic that converts libtmux's list-of-lines format into the single string expected by `_looks_done()` and `handle_output()`.

---

## 8. `test_worktree.py` — Git Worktree Manager (9 tests)

Tests `WorktreeManager` from `tmux_orchestrator.worktree`. All tests use a **real git repository** created in pytest's `tmp_path`.

### Fixture

```python
@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("hello\n")
    _git("add", "README.md", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)
    return tmp_path
```

A real git repo with one commit is required because `git worktree add` needs at least one commit to check out from.

---

### `test_setup_creates_worktree`

Calls `wm.setup("agent-1")`. Asserts:
- Returned path exists on disk.
- Path equals `{repo}/.worktrees/agent-1`.

**Why this matters:** The most basic contract of the manager.

---

### `test_setup_isolate_false_returns_repo_root`

Calls `wm.setup("agent-2", isolate=False)`. Asserts:
- Returned path equals the repo root (no worktree directory created).
- `.worktrees/agent-2/` does not exist.

**Why this matters:** The `isolate: false` opt-out in the config must not create an unwanted worktree directory.

---

### `test_teardown_removes_worktree_and_branch`

Sets up `"agent-3"`, asserts the path exists, calls `teardown("agent-3")`.

**Assertions:**
- Path no longer exists.
- `git branch --list worktree/agent-3` returns empty output.

Uses a direct `git` subprocess call to verify both filesystem and git state are cleaned up.

---

### `test_teardown_shared_is_noop`

Sets up `"agent-4"` with `isolate=False`, records the git branch list, calls `teardown("agent-4")`, compares branch list.

**Assertion:** Branch list is unchanged — no git operations were performed.

**Why this matters:** `teardown` for a shared agent must not accidentally delete branches.

---

### `test_gitignore_entry_added`

Asserts `.gitignore` does not exist before manager initialisation. After `WorktreeManager(git_repo)`:
- `.gitignore` exists.
- `".worktrees/"` is a line in it.

---

### `test_gitignore_not_duplicated`

Initialises `WorktreeManager` twice on the same repo. Reads the `.gitignore`.

**Assertion:** `lines.count(".worktrees/") == 1` — idempotent.

**Why this matters:** Prevents `.gitignore` bloat when the orchestrator is restarted repeatedly.

---

### `test_not_in_git_repo_raises`

Passes a `tmp_path` with no `.git` directory.

**Assertion:** `RuntimeError` is raised with message matching `"Not inside a git repository"`.

**Why this matters:** The `main.py` fallback path depends on this exact exception type to disable worktree isolation gracefully.

---

### `test_worktree_path_before_setup_returns_none`

Initialises a manager, calls `wm.worktree_path("nonexistent-agent")` without calling `setup()`.

**Assertion:** Returns `None`.

**Why this matters:** The orchestrator calls `parent_agent.worktree_path` in `_spawn_subagent` for `share_parent_worktree`. A `None` result must be handled safely.

---

### `test_duplicate_setup_cleaned_and_recreated`

Calls `wm.setup("agent-5")` twice.

**Assertions:**
- `path1.exists()` after first setup.
- `path2 == path1` (same location).
- `path2.exists()` after second setup (cleaned and recreated).
- `path2` does not exist after `teardown("agent-5")`.

**Why this matters:** Crash recovery — if an agent crashes without calling `stop()`, the next time it starts, the stale worktree from the previous run must be cleaned up without error.

---

## 9. Test Infrastructure

### `pyproject.toml` configuration

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- `asyncio_mode = "auto"` — all `async def test_*` functions run with `asyncio.run` automatically. No explicit `@pytest.mark.asyncio` needed.
- `testpaths` — restricts discovery to `tests/`, preventing accidental collection of example scripts.

### `conftest.py`

Not present. All fixtures are defined locally in each test module, keeping each file self-contained.

### Async timing

Several orchestrator tests use `await asyncio.sleep(N)` to wait for the background dispatch loop (200 ms cycle) or routing loop. The sleeps are:

| Test | Sleep | Why |
|---|---|---|
| `test_submit_and_dispatch` | 300 ms | Dispatch loop polls every 200 ms |
| `test_no_idle_agent_requeues` | 300 ms | Confirm task not dispatched |
| `test_p2p_allowed` | 100 ms | Route loop processes messages |
| `test_p2p_blocked` | 100 ms | Confirm no delivery |
| `test_pause_and_resume` | 300 ms + 500 ms | Pause check, then resume check |

---

## 10. Coverage & Gaps

### What is covered

| Module | Coverage |
|---|---|
| `bus.py` | All public methods; queue-full edge case |
| `messaging.py` | All CRUD operations; missing-file edge cases |
| `orchestrator.py` | Dispatch, requeue, P2P allow/block, pause/resume, list, task timeout, status events |
| `tmux_interface.py` | Session create/kill-existing/create-fresh/abort-on-decline, watch/unwatch, send_keys, capture, hash |
| `worktree.py` | Setup, teardown, isolate=false, gitignore, non-git, duplicate setup |
| `agents/base.py` | Timeout enforcement, status events |

### Known gaps (not tested)

| Area | Reason |
|---|---|
| `ClaudeCodeAgent` end-to-end | Requires a live tmux session and `claude` binary |
| `_patch_web_url` (main.py) | Tests would mock ClaudeCodeAgent attributes; not yet added |
| TUI widgets (`tui/`) | Textual requires a display; tested manually |
| Web server (`web/`) | FastAPI TestClient could cover this; not yet added |
| Sub-agent spawn integration | Covered by manual testing; unit test uses DummyAgent only |
| `_message_loop` concurrency | Basic path covered; edge cases (queue cancellation) not |

---

## 11. Adding New Tests

### Async test

```python
# tests/test_my_feature.py
import asyncio
import pytest
from tmux_orchestrator.bus import Bus, Message, MessageType

async def test_my_feature() -> None:
    bus = Bus()
    q = await bus.subscribe("agent-x")
    await bus.publish(Message(type=MessageType.STATUS, from_id="src", payload={}))
    msg = q.get_nowait()
    assert msg.from_id == "src"
```

No decorator needed — `asyncio_mode = "auto"` handles it.

### Test with real git repo

```python
import subprocess
from pathlib import Path
import pytest
from tmux_orchestrator.worktree import WorktreeManager

@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "add", "f"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path

def test_my_worktree_feature(git_repo: Path) -> None:
    wm = WorktreeManager(git_repo)
    path = wm.setup("my-agent")
    assert path.exists()
    wm.teardown("my-agent")
    assert not path.exists()
```

### Test with DummyAgent

```python
from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from unittest.mock import MagicMock
import asyncio

class DummyAgent(Agent):
    def __init__(self, agent_id, bus):
        super().__init__(agent_id, bus)
        self.dispatched = []

    async def start(self):
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(self._run_loop())

    async def stop(self):
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()

    async def _dispatch_task(self, task):
        self.dispatched.append(task)
        self._set_idle()

    async def handle_output(self, text): pass
    async def notify_stdin(self, n): pass

async def test_my_orchestrator_feature() -> None:
    bus = Bus()
    tmux = MagicMock()
    config = OrchestratorConfig(session_name="test", agents=[], p2p_permissions=[])
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()
    try:
        # ... your test logic
        pass
    finally:
        await orch.stop()
```
