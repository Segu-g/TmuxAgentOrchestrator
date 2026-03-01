# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**TmuxAgentOrchestrator** — orchestrates AI agents (Claude Code instances and custom scripts) inside tmux panes. An orchestrator process manages a pool of worker agents, dispatches tasks from a priority queue, and optionally permits peer-to-peer messaging between workers. The primary interface is a Textual TUI with a FastAPI/WebSocket web UI layer on top.

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
│       │   ├── claude_code.py      # Drives `claude` CLI in a tmux pane
│       │   └── custom.py           # Arbitrary script via newline-delimited JSON stdio
│       ├── tui/
│       │   ├── app.py              # Textual App root (keybindings: n/k/p/q)
│       │   └── widgets.py          # AgentPanel, TaskQueuePanel, LogPanel, StatusBar
│       └── web/
│           ├── app.py              # FastAPI app (REST + embedded browser UI)
│           └── ws.py               # WebSocket hub (fans out bus events to browsers)
├── examples/
│   ├── basic_config.yaml           # Two-worker example config
│   └── echo_agent.py               # Minimal custom agent for testing
└── tests/
    ├── test_bus.py
    ├── test_orchestrator.py
    └── test_tmux_interface.py
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
- **ClaudeCodeAgent**: sends task via `send_keys`; polls pane output every 500 ms; declares completion when output settles for 3 consecutive cycles and matches a prompt pattern.
- **CustomAgent**: newline-delimited JSON over subprocess stdin/stdout. Input: `{"task_id": "…", "prompt": "…"}`. Output: `{"task_id": "…", "result": "…"}`.
- **Web UI**: single-page HTML served from `GET /`; auto-reconnecting WebSocket at `ws://host/ws`; polls REST endpoints every 3 s for agent/task table refresh.

## Key Decisions

- `libtmux` is the sole tmux binding; pane watcher runs in a daemon thread, uses `asyncio.run_coroutine_threadsafe` to publish to the async bus.
- Textual TUI and FastAPI web server are two separate CLI commands (`tui` vs `web`) that share the same core components.
- P2P routing is bidirectional per permission entry — stored as `frozenset` pairs.
