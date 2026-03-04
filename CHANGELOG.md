# Changelog

All notable changes to TmuxAgentOrchestrator are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [0.3.0] — 2026-03-05

### Added

**Reliability: Circuit Breaker (closes Issue #3)**
- New `src/tmux_orchestrator/circuit_breaker.py` — `CircuitBreaker` class with
  CLOSED → OPEN → HALF_OPEN state machine; implements Martin Fowler's
  "Release It!" stability pattern
- Per-agent circuit breakers (`Orchestrator._breakers`) created at agent registration
- `_find_idle_agent()` skips agents whose circuit is OPEN — prevents repeated
  dispatch to unhealthy agents
- `_route_loop()` calls `record_success()`/`record_failure()` on each RESULT message
- Config: `OrchestratorConfig.circuit_breaker_threshold` (default: 3) and
  `circuit_breaker_recovery` (default: 60.0 s)

**Observability: Bus Drop Count Tracking**
- `Bus._drop_counts: dict[str, int]` records messages dropped per subscriber
  when queue is full (was silent)
- `Bus.get_drop_counts()` returns a snapshot of all drop counts
- `Orchestrator.list_agents()` now includes `"bus_drops"` and `"circuit_breaker"`
  fields per agent

**Health Probes**
- `GET /healthz` — liveness probe: returns 200 + timestamp if event loop is alive
- `GET /readyz` — readiness probe: checks dispatch loop, worker availability, and
  paused state; returns 503 with `checks` dict when not ready

**DDD Ubiquitous Language**
- `AgentRole(str, Enum)` in `config.py` — replaces `role: str` everywhere; values
  `WORKER` and `DIRECTOR`; serialises as plain string for backward compatibility

**Orchestrator Use-Case Methods (Hexagonal boundary)**
- `Orchestrator.get_director() → Agent | None` — encapsulates director lookup
- `Orchestrator.flush_director_pending() → list[str]` — atomic read-and-clear of
  buffered worker results; web layer no longer accesses `_director_pending` directly

**Context Engineering**
- `Task.trace_id: str` — 8-byte hex token auto-generated per task; enables
  correlation across agent boundaries for post-hoc debugging
- `_buffer_director_result()` now extracts the final 40 lines of output
  (tail-based, semantic) instead of hard-cutting at 2 000 characters

**Agent Lifecycle**
- `Agent._set_idle()` now always publishes `agent_idle` STATUS event, regardless
  of which code path triggers the IDLE transition; previously only `_run_loop`
  path emitted this event

### Changed

- `AgentConfig.role` type: `str` → `AgentRole` (backward-compatible via `str` mixin)
- `OrchestratorConfig` gains `circuit_breaker_threshold` and `circuit_breaker_recovery`
- `web/app.py` `director_chat` endpoint uses `get_director()` / `flush_director_pending()`

### Tests

- 85 total (64 → 85), all passing
- New: `tests/test_circuit_breaker.py` — 10 circuit breaker state machine tests
- New: orchestrator tests for AgentRole enum, Task.trace_id, circuit breaker dispatch
  integration, `get_director()`, `flush_director_pending()`, bus drop counts
- New: web app tests for `/healthz` and `/readyz` endpoints

---

## [0.2.0] — 2026-03-04

### Added

**Hierarchical Agent Architecture**
- `TmuxInterface.new_subpane(parent_pane)`: sub-agents now split their parent's
  tmux window (pane) instead of opening a new window, matching the intended
  `session=project / window=agent-group / pane=sub-agent` hierarchy
- `ClaudeCodeAgent(parent_pane=...)`: accepts a parent pane reference so sub-agent
  placement is determined at construction time
- `Orchestrator._agent_parents`: tracks parent→child relationships for all
  dynamically spawned sub-agents
- `list_agents()` now includes `parent_id` field
- `register_agent(parent_id=...)`: optional parameter to record the parent at
  registration time (used for both hierarchy display and P2P routing)

**Hierarchy-Based P2P Routing**
- `Orchestrator._is_hierarchy_permitted()`: automatically allows messaging between
  parent↔child and sibling agents (those sharing the same parent, including all
  root-level agents sharing the implicit "no parent" root)
- `p2p_permissions` in YAML config is now an **escape hatch** for cross-branch
  lateral communication rather than the sole permission mechanism
- P2P route log now reports the reason (`user` / `explicit` / `hierarchy` / `blocked`)

**Context Engineering Support**
- `AgentConfig.system_prompt`: YAML field for per-agent role-specific instructions,
  injected into the agent's `CLAUDE.md` at startup
- `AgentConfig.context_files`: list of files to declare as pre-loaded context
  (actual copying: see issue #1)
- `ClaudeCodeAgent._write_agent_claude_md()`: generates a role-specific `CLAUDE.md`
  in each agent's worktree covering identity, communication protocol, TDD conventions,
  and slash command reference — implements per-agent context localization
- `ClaudeCodeAgent._write_notes_template()`: scaffolds `NOTES.md` as a structured
  external scratchpad (Key Decisions / Progress / Blockers / Completed)

**AgentConfig Extensions**
- `AgentConfig.task_timeout`: per-agent timeout override (takes priority over the
  global `OrchestratorConfig.task_timeout`)
- `AgentConfig.command`: custom launch command per agent (default: claude CLI)

**Slash Commands (10 total, up from 5)**
- `/plan <description>`: writes `PLAN.md` with acceptance criteria and TDD test
  strategy before implementation begins
- `/tdd <feature>`: step-by-step Red→Green→Refactor guide with completion checklist
- `/progress <summary>`: sends a structured progress PEER_MSG to the parent agent,
  enriched with PLAN.md and NOTES.md status
- `/summarize`: compresses current work state into `NOTES.md` (context compaction)
- `/delegate <task>`: guides task decomposition and sub-agent assignment with context
  isolation advice

**Integration Tests**
- New `tests/integration/` suite using `HeadlessAgent` (real Bus + Orchestrator +
  Mailbox, no tmux) to verify cross-component behaviour
- Covers: full dispatch round-trip, parallel multi-agent dispatch, P2P with mailbox
  and stdin notification, Director result buffering, sub-agent spawning

**Documentation**
- `DESIGN.md`: architecture decisions, tmux hierarchy mapping, P2P rules,
  context engineering approach, TDD integration, annotated reference list

### Fixed

- `POST /agents` sent `agent_type`/`command` but orchestrator expected `template_id`;
  now aligned — sub-agent spawning via REST API was silently failing
- `_spawn_subagent` did not propagate `role` or `command` from the template config;
  sub-agents always started as `"worker"` regardless of template
- All `asyncio.get_event_loop()` in coroutines replaced with `asyncio.get_running_loop()`
  (deprecated in Python 3.10+, raises DeprecationWarning in 3.12)
- `TmuxInterface` daemon thread now captures the running event loop at `start_watcher()`
  call time and uses `asyncio.run_coroutine_threadsafe()` with the captured reference
  (eliminates a potential `RuntimeError` in Python 3.12+)
- FastAPI `@app.on_event("startup"/"shutdown")` deprecated since FastAPI 0.93;
  migrated to `lifespan` context manager

### Changed

- `spawn-subagent.md` slash command updated to use `template_id` parameter
- Director startup prompt updated to reference `/plan` command

---

## [0.1.0] — 2026-03-02

Initial release.

### Added

- Async in-process pub/sub `Bus` with directed and broadcast delivery
- `Orchestrator`: priority task queue, agent registry, P2P gating via
  `frozenset` permission pairs, sub-agent spawning via CONTROL messages
- `TmuxInterface`: libtmux wrapper, pane watcher daemon thread
- `ClaudeCodeAgent`: drives `claude --dangerously-skip-permissions` in a tmux pane;
  poll-based completion detection (`❯` / `$` / `>` / `Human:` prompt patterns)
- `WorktreeManager`: per-agent git worktree isolation on branch `worktree/{agent_id}`
- `Mailbox`: file-based persistent message store (inbox/read directories)
- FastAPI web server with embedded single-page UI, WebAuthn passkey auth,
  session cookie, API key auth, WebSocket hub
- Textual TUI with agent panel, task queue panel, log panel
- CLI: `tui`, `web`, `run`, `chat` commands via Typer
- Slash commands: `/check-inbox`, `/read-message`, `/send-message`,
  `/spawn-subagent`, `/list-agents`
- Director agent role with result buffering for chat-based coordination
- 42 unit tests
