# Changelog

All notable changes to TmuxAgentOrchestrator are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [0.13.0] — 2026-03-05

### Added

**Manual agent reset — `POST /agents/{id}/reset`**
- New `Orchestrator.reset_agent(agent_id)` method: stops the agent, clears
  `_permanently_failed` flag and `_recovery_attempts` counter, restarts the
  agent, and publishes an `agent_reset` STATUS event on the bus
- Raises `KeyError` for unknown agent IDs
- New REST endpoint `POST /agents/{id}/reset` — 200 on success, 404 on
  unknown agent, 401 without authentication
- Response: `{"agent_id": "<id>", "reset": true}`
- Design: action sub-resource pattern (`POST` verb endpoint, not `PUT` state
  replacement) — Nordic APIs "Designing a True REST State Machine"
- 9 new tests in `tests/test_agent_reset.py`

**Prometheus metrics — `GET /metrics`**
- New `GET /metrics` endpoint: exposes Prometheus text-format metrics
- No authentication required (Prometheus scraper compatibility; document recommends
  network-level protection)
- Metrics exposed per request (per-request `CollectorRegistry` for snapshot accuracy):
  - `tmux_agent_status_total{status}` — Gauge: agent count per status value (IDLE/BUSY/ERROR/STOPPED)
  - `tmux_task_queue_size` — Gauge: current pending task queue depth
  - `tmux_bus_drop_total{agent_id}` — Gauge: bus drop count per agent
- New dependency: `prometheus-client>=0.19` (added to main project deps)
- 9 new tests in `tests/test_metrics.py`
- OpenAPI schema snapshot updated

---

## [0.12.0] — 2026-03-05

### Added

**ERROR state auto-recovery with exponential backoff (Issue #3)**
- New `_recovery_loop` in `Orchestrator`: polls every `recovery_poll` seconds
  (default 2s) for agents in ERROR state and attempts to restart them
- Exponential backoff: each retry waits `recovery_backoff_base^attempt` seconds
  (default: 5^1=5s, 5^2=25s, 5^3=125s) to prevent restart storms
- Publishes `agent_recovered` STATUS event on successful restart
- Publishes `agent_recovery_failed` STATUS event when `recovery_attempts`
  exhausted (default 3); agent then excluded from further auto-recovery
- Permanently-failed agents tracked in `_permanently_failed: set[str]`
- New `OrchestratorConfig` fields: `recovery_attempts: int = 3`,
  `recovery_backoff_base: float = 5.0`, `recovery_poll: float = 2.0`
- YAML config keys: `recovery_attempts`, `recovery_backoff_base`, `recovery_poll`
- 5 new tests in `tests/test_error_recovery.py`
- Closes GitHub Issue #3

**SSE push notifications — `GET /events` endpoint**
- New `GET /events` endpoint using FastAPI native SSE (`EventSourceResponse`,
  v0.135+ — zero external dependencies)
- Auth via session cookie or `X-API-Key` header (same as all other API endpoints)
- Each bus message is streamed as a typed SSE event with named `event=` field:
  `status`, `result`, `peer_msg`, `control` — enables selective client listening
- 15-second keep-alive comment prevents proxy/load-balancer timeout disconnection
- Web UI upgraded from 3s polling to real-time SSE:
  - `connectSSE()` subscribes to `/events` after successful authentication
  - STATUS and RESULT events trigger immediate `refreshAgents()` + `refreshTasks()`
  - PEER_MSG events update the conversation panel in real-time
  - Director RESULT events directly update the chat bubble without polling
  - Fallback 30s poll retained as belt-and-suspenders backstop
- OpenAPI schema snapshot updated (`tests/fixtures/openapi_schema.json`)
- 4 new tests in `tests/test_sse.py`

### Fixed

- **`tmux_interface.py`: missing `asyncio` import** — `asyncio.get_running_loop()`
  and `asyncio.run_coroutine_threadsafe()` were called without the module being
  imported, causing `NameError` at runtime during real-agent demo execution.
  Also removed redundant string-quoted type annotation for `_loop`.

### Tests

- Total: **180 tests** (was 171), all passing.

---

## [0.11.0] — 2026-03-05

### Added

**context_files auto-copy to agent worktree (Issue #1)**
- New `ClaudeCodeAgent._copy_context_files(cwd: Path)` method: copies all
  `context_files` paths (relative to `context_files_root`) into the agent's
  worktree before the agent starts, preserving directory structure (`shutil.copy2`)
- New `context_files_root: Path | None` constructor parameter on `ClaudeCodeAgent`;
  `factory.py` passes `Path.cwd()` when `context_files` is non-empty; same in
  `Orchestrator._spawn_subagent()`
- Missing files emit a `logger.warning` rather than raising — partial context is
  better than a crashed agent
- `ClaudeCodeAgent.start()` calls `_copy_context_files` after `_setup_worktree`,
  `_write_context_file`, `_write_agent_claude_md`, and `_write_notes_template`
- 6 new unit tests in `tests/test_context_files.py` covering: copy, missing file
  warning, empty list no-op, nested directory preservation, no-root warning,
  and integration (start() calls copy)

**Agent hierarchy tree view in Web UI (Issue #2)**
- New `GET /agents/tree` REST endpoint returns agents as a nested JSON tree
  (d3-hierarchy compatible: `{id, status, role, parent_id, children: [...]}`)
- New `_build_agent_tree(agents: list[dict]) → list[dict]` helper converts the flat
  `list_agents()` output to a recursive parent→children structure
- Web UI Agents panel now has **List / Tree** toggle buttons
- Tree view rendered with pure CSS indentation + Vanilla JS (no external CDN/D3):
  `refreshAgentTree()` fetches `/agents/tree` and renders `<ul class="tree-node">`
  with per-node ID, role badge, and status colour
- 9 new tests in `tests/test_hierarchy_tree.py`
- OpenAPI schema snapshot updated (`tests/fixtures/openapi_schema.json`)

### Fixed

- GitHub Issue #1 (context_files auto-copy) — **closed**
- GitHub Issue #2 (Web UI hierarchy tree) — **closed**

---

## [0.10.0] — 2026-03-05

### Added

**Task supervision (`supervision.py`)**
- New `src/tmux_orchestrator/supervision.py` — `supervised_task(coro_factory, name, *,
  max_restarts=5, on_permanent_failure=None)`: wraps an async coroutine factory and
  restarts it on unexpected exceptions with pre-defined backoff levels
  `[0.1, 0.5, 1.0, 5.0, 30.0]` seconds
- `CancelledError` is never caught — cancellation propagates immediately
- `Orchestrator._dispatch_loop` and `_route_loop` are now wrapped with
  `supervised_task`; `_on_internal_failure` publishes a STATUS event when retries
  are exhausted

**Watchdog loop for stuck agents**
- `AgentRegistry._busy_since: dict[str, float]` — tracks monotonic timestamp of
  when each agent was dispatched; cleared on `record_result()`
- `AgentRegistry.record_busy(agent_id)` — called by dispatch loop when sending a task
- `AgentRegistry.find_timed_out_agents(task_timeout) → list[str]` — returns agents
  BUSY for more than 1.5× `task_timeout` (the internal `asyncio.wait_for` gets
  first chance; the watchdog is the backstop)
- `Orchestrator._watchdog_loop(poll)` — polls every `config.watchdog_poll` seconds
  (default 10 s); publishes synthetic `RESULT(error="watchdog_timeout")` for stuck
  agents so the existing circuit-breaker path handles recovery
- `OrchestratorConfig.watchdog_poll: float = 10.0`
- `Orchestrator.stop()` now also cancels and awaits `_watchdog_task`

**Idempotency keys on `submit_task`**
- `submit_task(idempotency_key=...)` — if the same key is submitted twice, the
  second call returns a stub pointing at the original `task_id` without enqueueing
- `Orchestrator._idempotency_keys / _ikey_timestamps` — in-process dict with 1-hour
  TTL; lazy expiry on each new keyed submission
- Pattern: Idempotent Receiver (Hohpe & Woolf, EIP 2004, p. 349)

**Stateful property tests — `BusStateMachine`**
- New `tests/test_bus_stateful.py` — `BusStateMachine(RuleBasedStateMachine)` tests
  Bus invariants across arbitrary sequences of subscribe / broadcast-publish /
  directed-publish / unsubscribe operations
- 200 examples × 30 steps per example; invariants checked after every step:
  drop counts non-negative, local mirror == bus internal table, directed messages
  reach only the target

### Test count: 156 (up from 144)

Reference: Erlang OTP supervisor (Ericsson, 1996); Hattingh "Using Asyncio in Python"
           (O'Reilly, 2020) Ch. 4; Hohpe & Woolf EIP (2004) p. 349;
           Claessen & Hughes QuickCheck (ICFP, 2000); DESIGN.md §10.6 (2026-03-05)

---

## [0.9.0] — 2026-03-05

### Fixed

**`Orchestrator.stop()` task leakage**
- `stop()` now awaits cancelled internal tasks with
  `asyncio.gather(*tasks, return_exceptions=True)` instead of fire-and-forget
  `.cancel()`; prevents background tasks from outliving the orchestrator in tests
  and production shutdown sequences

**`TaskResultPayload.error` coercion**
- `schemas.py` `TaskResultPayload.error` was typed `str | None`; malformed
  messages with non-string error values (e.g. `0`) raised `ValidationError`
- Added `@field_validator("error", mode="before")` that coerces any non-None
  non-string value to `str`, keeping the public type `str | None`
- Found and confirmed by Hypothesis (`test_parse_result_payload_never_raises`)

### Added

**Bounded task queue (`task_queue_maxsize`)**
- `OrchestratorConfig.task_queue_maxsize: int = 0` — `0` means unbounded (default,
  backward-compatible); positive value caps `asyncio.PriorityQueue(maxsize=...)`
- `submit_task()` raises `RuntimeError` immediately when the queue is full
  rather than blocking the caller

**OpenAPI schema contract regression test**
- New `tests/test_openapi_schema.py` + `tests/fixtures/openapi_schema.json`
  snapshot; fails on divergence, regenerated with `UPDATE_SNAPSHOTS=1`

**Deterministic test synchronisation**
- `DummyAgent.dispatched_event: asyncio.Event` — set in `_dispatch_task` when
  a task is accepted; replaces `asyncio.sleep(0.3)` barriers in
  `test_orchestrator.py` with `asyncio.wait_for(event.wait(), timeout=2.0)`
- P2P tests: `route_message` is awaited directly → removed all `asyncio.sleep(0.1)`
  stalls in routing tests (message is in subscriber queue after the `await`)
- Net reduction: 10 `asyncio.sleep` barriers eliminated; 2 reduced (0.3 → 0.1 s)

### Test count: 144 (up from 143)

Reference: Martin Fowler "Patterns of Enterprise Application Architecture" (2002);
           asyncio docs § "Synchronisation Primitives"; DESIGN.md §10.5

---

## [0.8.0] — 2026-03-05

### Added

**Task dependency graph (`depends_on`) + Workflow primitive**
- `Task.depends_on: list[str]` — task IDs that must complete successfully before
  this task is dispatched
- `Orchestrator._completed_tasks: set[str]` — set of task IDs completed without error;
  updated by `_route_loop` when a RESULT without `error` is received
- `Orchestrator._dispatch_loop` checks unmet dependencies before dispatching; re-queues
  the task (counted toward `dlq_max_retries`) until all deps are resolved
- `Orchestrator.submit_task()` gains `depends_on: list[str] | None` parameter
- New `src/tmux_orchestrator/workflow.py` — `Workflow` builder with `step()` / `run()`
  API; `_topological_sort()` (Kahn's algorithm) orders steps before submission;
  raises `ValueError` on cycles or foreign dependencies
- New `tests/test_workflow.py` — 10 tests: `Task.depends_on`, topo sort (linear,
  diamond, cycle, foreign dep), submit with deps, dispatch blocking integration test,
  `Workflow.run()` end-to-end

**Agent lifecycle principle documented**
- `CLAUDE.md` updated: workers are ephemeral (spawn per task/phase, not reused);
  system prompt and `CLAUDE.md` are immutable during the agent's lifetime
- `DESIGN.md` §11 updated: Issue #4 (CLAUDE.md dynamic update) closed — not needed
  given the ephemeral-agent principle
- GitHub Issue #4 closed with design rationale

### Test count: 143 (up from 133)

Reference: Richardson "Microservices Patterns" (2018) Ch. 4 (Saga pattern);
           DESIGN.md §10.5 (2026-03-05)

---

## [0.7.0] — 2026-03-05

### Added

**Structured JSON logging with trace_id context**
- New `src/tmux_orchestrator/logging_config.py` — `JsonFormatter`, `bind_trace()`,
  `bind_agent()`, `unbind()`, `current_trace_id()`, `current_agent_id()`,
  `setup_json_logging()`, `setup_text_logging()`
- Uses `contextvars.ContextVar` so every log record produced within a task dispatch
  call tree automatically includes `trace_id` and `agent_id` — no explicit parameter
  passing required
- `agents/base.py._run_loop` binds `task.trace_id` and `self.id` before calling
  `_dispatch_task` and unbinds in the `finally` block
- `main.py` adds `--json-logs` flag to `web` and `run` commands; `_setup_logging()`
  delegates to `setup_json_logging()` or `setup_text_logging()` accordingly
- `main.py` uses `setup_text_logging()` (force=True) instead of bare `basicConfig`
  for idempotent reconfiguration
- New `tests/test_logging_config.py` — 10 tests covering formatter fields,
  context binding/unbinding, nesting, exception serialisation, and handler setup

### Test count: 133 (up from 123)

Reference: Kleppmann "DDIA" Ch. 11; SRE Book Ch. 16; DESIGN.md §10.5

---

## [0.6.0] — 2026-03-05

### Added / Refactored

**SystemFactory extraction (Layered Architecture)**
- New `src/tmux_orchestrator/factory.py` — `build_system()` and `patch_web_url()`
  separated from CLI entry point (`main.py`)
- `build_system(config_path, *, confirm_kill=None)` is now independently importable
  and testable without any `typer` dependency — injectable `confirm_kill` callback
  (default `None`) decouples interactive I/O from wiring logic
- `patch_web_url(orchestrator, host, port)` fixed to use `orchestrator.registry.all_agents()`
  instead of the previously broken `orchestrator._agents` reference
- `main.py` reduced to CLI adapter: thin `_build_system()` wrapper that supplies the
  `typer.confirm` callback and translates `ValueError` → `typer.Exit(1)`
- New `tests/test_factory.py` — 6 unit tests covering wiring, agent registration,
  unknown-type error, callback forwarding, and `patch_web_url` behaviour

### Test count: 123 (up from 117)

---

## [0.5.0] — 2026-03-05

### Added / Refactored

**AgentRegistry extraction (DDD Aggregate pattern)**
- New `src/tmux_orchestrator/registry.py` — `AgentRegistry` class encapsulates all
  agent-related state: `_agents`, `_agent_parents`, `_p2p` permissions, `_breakers`
- `Orchestrator` becomes a thin coordinator: delegates registration, lookup, P2P
  permission checks, and circuit-breaker updates to the registry
- Public API unchanged — `register_agent()`, `get_agent()`, `list_agents()`, etc.
  are preserved as thin delegators on `Orchestrator`
- `AgentRegistry.is_p2p_permitted()` returns `(bool, reason: str)` with explicit
  reason codes: `"user"`, `"explicit"`, `"hierarchy"`, `"blocked"`

**New test module: `tests/test_registry.py`**
- 20 unit tests for `AgentRegistry` in isolation (no `Orchestrator` or tmux)
- Uses `StubAgent` — minimal in-process agent
- Coverage: registration, parent tracking, unregistration, lookup (`get`, `get_director`,
  `find_idle_worker`), P2P permission rules (user bypass, explicit, hierarchy siblings,
  parent↔child, cross-branch blocked, `grant_p2p`), circuit-breaker recording,
  `list_all` with drop counts

### Test count: 117 (up from 97)

---

## [0.4.0] — 2026-03-05

### Added

**Dead Letter Queue**
- `Orchestrator._dlq: list[dict]` — tasks that could not be dispatched after
  `dlq_max_retries` re-queue attempts are moved here instead of looping forever
- `Orchestrator.list_dlq() → list[dict]` — read-only snapshot of dead-lettered tasks
- `OrchestratorConfig.dlq_max_retries: int` (default: 50) — configurable threshold
- Publishes `task_dead_lettered` STATUS event when a task is dead-lettered
- `GET /dlq` REST endpoint exposes the DLQ to operators
- Integration test: `test_task_dead_lettered_when_no_idle_agents`

**Typed Message Payload Schemas (Pydantic)**
- New `src/tmux_orchestrator/schemas.py` — Pydantic v2 models for all bus
  message payload types: `TaskQueuedPayload`, `AgentBusyPayload`, `AgentIdlePayload`,
  `AgentErrorPayload`, `SubagentSpawnedPayload`, `TaskDeadLetteredPayload`,
  `TaskResultPayload`, `PeerMessagePayload`, `SpawnSubagentPayload`
- `parse_status_payload(dict)` / `parse_result_payload(dict)` factory functions
- Unknown events fall back to `_BasePayload` (forward-compatible via `extra="allow"`)

**Property-Based Tests (Hypothesis)**
- New `tests/test_properties.py` — 11 property tests verifying invariants:
  - `Task.trace_id` always 16-char hex
  - Task trace_ids are unique across all instances
  - Task ordering consistent with priority for any int pair
  - Circuit breaker opens exactly at threshold (any threshold 1–5)
  - Successes in CLOSED state never open the breaker
  - `parse_result_payload` never raises for any dict with `task_id`
  - Unknown event schema falls back without raising
  - Known event schema raises `ValidationError` on missing required fields
  - Bus drop counts are monotonically non-decreasing
  - Subscribe/unsubscribe leaves no leaked state

### Tests

- 97 total (85 → 97), all passing
- Hypothesis found and confirmed: known event schemas correctly reject incomplete payloads

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
