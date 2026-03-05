# Changelog

All notable changes to TmuxAgentOrchestrator are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [0.26.0] — 2026-03-05

### Added

**Per-task retry semantics — `Task.max_retries`, `task_retrying` STATUS events**

Tasks can now be submitted with a `max_retries` parameter. When the
orchestrator receives a RESULT with `error != None`, it checks whether the
task has retries remaining. If `retry_count < max_retries`, the task is
re-enqueued with the same priority and a `task_retrying` STATUS event is
published. Only after all retries are exhausted does the task become
permanently failed (dead-lettered) and the workflow (if any) transitions to
`"failed"`.

Design references:
- AWS SQS `maxReceiveCount` / Redrive policy — re-enqueue before DLQ
- Netflix Hystrix retry — transient-failure tolerance
- Polly .NET resilience library — retry policies
- Erlang OTP supervisor restart strategies — `restart_one_for_one`
- DESIGN.md §10.21 (v0.26.0)

Changes:
- `Task` dataclass (`agents/base.py`): new `max_retries: int = 0` and
  `retry_count: int = 0` fields. New `to_dict()` method for consistent
  serialisation including retry fields.
- `Orchestrator._route_loop()` (`orchestrator.py`): on RESULT with error,
  checks `task.retry_count < task.max_retries`. If true: increments
  `retry_count`, re-enqueues task at same priority, calls
  `WorkflowManager.on_task_retrying()`, publishes `task_retrying` STATUS event.
  Only after retries exhausted: calls `on_task_failed()` as before.
- `Orchestrator._active_tasks: dict[str, Task]`: new mapping populated at
  dispatch time; used by `_route_loop` to retrieve the Task object for retry.
  Cleaned up on success or final failure.
- `Orchestrator.submit_task()`: new `max_retries: int = 0` keyword argument.
- `WorkflowManager.on_task_retrying()` (`workflow_manager.py`): new method.
  Removes `task_id` from `_failed` set and re-runs `_update_status()` so the
  workflow is not prematurely marked `"failed"` during retries.

New/updated REST endpoints:
- `POST /tasks` — accepts `max_retries: int = 0`; response includes
  `max_retries` and `retry_count`.
- `POST /tasks/batch` — each task item accepts `max_retries`; response per
  task includes `max_retries` and `retry_count`.
- `POST /workflows` — `WorkflowTaskSpec` accepts `max_retries: int = 0` per
  task node; passed through to `submit_task()`.
- `GET /tasks` — redesigned: returns all tasks (queued + in-progress +
  completed/failed history) with `skip: int = 0` and `limit: int = 100`
  pagination query params. Each entry includes `status`, `max_retries`, and
  `retry_count`.
- `GET /tasks/{task_id}` — **new endpoint**: returns the status and details
  of a specific task including `retry_count` and `max_retries`. Returns 404
  if the task ID is unknown.

Tests: 30 new tests in `tests/test_task_retry.py` (468 total).

---

## [0.25.0] — 2026-03-05

### Added

**Workflow DAG API — `WorkflowManager` + `POST /workflows` + `GET /workflows`**

Implements a REST-level Workflow DAG submission API that lets users submit an
entire multi-step pipeline as a single atomic request. Previously, tasks had
to be submitted one by one with manually-managed `depends_on` IDs. Now a DAG
of tasks with local cross-references is submitted in one call; the server
translates local IDs to global task IDs and enforces topological ordering.

Design references:
- Apache Airflow DAG model — task dependencies as directed acyclic graph
- Prefect "Modern Data Stack" workflow orchestration
- Tomasulo's algorithm (IBM 1967) — register renaming == local_id → global_task_id
- AWS Step Functions — state machine for workflow orchestration
- Kahn's algorithm (1962) — topological sort in O(V+E)

New components:
- `WorkflowManager` (`workflow_manager.py`): lightweight observer that tracks
  workflow runs and their completion state. Always enabled (zero overhead when
  no workflows are submitted). `on_task_complete()` / `on_task_failed()` called
  from `Orchestrator._route_loop` when RESULT messages arrive.
- `WorkflowRun` dataclass: stores `id`, `name`, `task_ids`, `status`, timestamps.
- `validate_dag()`: validates and topologically sorts a list of task spec dicts
  using Kahn's algorithm; raises `ValueError` on unknown dependencies or cycles.
- `WorkflowTaskSpec`, `WorkflowSubmit` Pydantic models for the REST body.

New REST endpoints:
- `POST /workflows` — submit a named workflow DAG. Validates DAG, assigns
  global task IDs, submits tasks with correct `depends_on` translation,
  registers with WorkflowManager. Returns `{workflow_id, name, task_ids}`.
  Returns 400 on cycle or unknown `depends_on` reference.
- `GET /workflows` — list all submitted workflow runs and their status.
- `GET /workflows/{workflow_id}` — status of a specific workflow run.

Orchestrator integration:
- `Orchestrator._workflow_manager: WorkflowManager` — always instantiated.
- `Orchestrator._route_loop`: calls `on_task_complete()` / `on_task_failed()`
  when processing RESULT messages.
- `Orchestrator.get_workflow_manager()` — accessor for tests and REST layer.

### Tests

- 29 new tests in `tests/test_workflow_manager.py`

Total: **438 tests** (was 409).

---

## [0.24.0] — 2026-03-05

### Added

**Task Result Persistence — `ResultStore` + `GET /results` + `GET /results/dates`**

Implements an append-only JSONL result store following the Event Sourcing
pattern (Fowler 2005) with CQRS-style separation of write (append) and read
(query) paths (Greg Young 2010). Every task completion is recorded as an
immutable, time-stamped fact on disk. Results survive orchestrator restarts,
enabling post-mortem analysis, workflow resumption, and audit trails.

References:
- Martin Fowler "Event Sourcing" (2005) https://martinfowler.com/eaa.html
- Greg Young "CQRS Documents" (2010)
- Rich Hickey "The Value of Values" (Datomic, 2012)

- `ResultStore` (`result_store.py`): new module. Thread-safe append-only JSONL
  store. File layout: `{store_dir}/{session_name}/{YYYY-MM-DD}.jsonl`. Each
  line is a JSON object: `{task_id, agent_id, prompt, result_text, error,
  duration_s, ts}`. `query()` supports filtering by `agent_id`, `task_id`,
  `date` with a `limit` cap. `all_dates()` returns sorted list of dates with
  data.
- `OrchestratorConfig` new fields: `result_store_enabled` (default `False`),
  `result_store_dir` (default `~/.tmux_orchestrator/results`).
- `Orchestrator._result_store` — created when `result_store_enabled=True`; wired
  into `_record_agent_history()` so every RESULT message is persisted.
- REST `GET /results` — query persisted results with optional `agent_id`,
  `task_id`, `date`, `limit` query params. Returns `[]` when store disabled.
- REST `GET /results/dates` — list dates with persisted data.

### Tests

- 23 new tests in `tests/test_result_store.py`

Total: **409 tests** (was 386).

---

## [0.23.0] — 2026-03-05

### Added

**Queue-Depth Autoscaling — `AutoScaler` + `POST /orchestrator/autoscaler`**

Implements elastic agent pool management using the MAPE-K autonomic computing
loop (Thijssen 2009). Agents are created/stopped automatically based on queue
depth, without any manual intervention.

References:
- Kubernetes HPA AverageValue metric (queue-depth-based scaling)
- Thijssen "Autonomic Computing" MIT Press 2009 — MAPE-K loop
- AWS Auto Scaling Groups cooldown periods

- `AutoScaler` (`autoscaler.py`): new module. Scale-up when
  `queue_depth > threshold × idle_agents`; scale-down after queue drains
  for `autoscale_cooldown` seconds. Tracks only its own created agents so
  pre-registered YAML agents are never accidentally stopped.
- `OrchestratorConfig` new fields: `autoscale_min`, `autoscale_max`,
  `autoscale_threshold`, `autoscale_cooldown`, `autoscale_poll`,
  `autoscale_agent_tags`, `autoscale_system_prompt`.
- `Orchestrator.queue_depth()` — returns current queue size.
- `Orchestrator.get_autoscaler_status()` — disabled-safe status dict.
- REST `GET /orchestrator/autoscaler` — current scaling state.
- REST `PUT /orchestrator/autoscaler` — live reconfiguration (min/max/threshold/cooldown).
- AutoScaler integrated into `Orchestrator.start()`/`stop()` lifecycle.

### Also

- `merge_target` parameter added to `WorktreeManager.teardown()`, `AgentConfig`,
  `ClaudeCodeAgent`, `Orchestrator.create_agent()`, and `POST /agents/new`.
  When `merge_on_stop=True`, the squash merge now targets the specified branch
  instead of always merging into current HEAD. The main repo is restored to its
  original branch after the merge completes.

### Tests

- 23 new tests in `tests/test_autoscaler.py`
- 2 new tests in `tests/test_worktree.py` (merge_target)

Total: **386 tests** (was 361).

---

## [0.22.0] — 2026-03-05

### Added

**Dynamic Agent Creation — `Orchestrator.create_agent()` + `POST /agents/new`**

Resolves GitHub Issue #5. Previously, spawning sub-agents required a
pre-configured YAML template (`template_id`). A Director running a complex
task could not spin up specialist workers on the fly.

Motivated by: Hewitt et al. "A Universal Modular Actor Formalism for Artificial
Intelligence" (IJCAI 1973) — actor model (dynamic spawning of actors at
runtime); Varela & Malenfant "Messages are the Medium" (1990) — on-demand
actor instantiation; AWS ECS dynamic task scaling pattern.

- `Orchestrator.create_agent(**kwargs)` — create, register, and start a new
  `ClaudeCodeAgent` at runtime. Accepts: `agent_id`, `tags`, `system_prompt`,
  `isolate`, `merge_on_stop`, `command`, `role`, `task_timeout`, `parent_id`.
  Auto-generates IDs (`dyn-{hex6}` or `{parent_id}-dyn-{hex6}`).
  Publishes `STATUS agent_created` after start.
- `POST /agents/new` — REST endpoint exposing `create_agent()` directly.
  Returns 409 on duplicate `agent_id`.
- CONTROL `{action: "create_agent"}` — bus-based path so Director agents
  can create workers without a REST call. Same parameters as `create_agent()`.

**Worktree merge-on-stop — contribute agent commits to original branch**

Agents running in isolated git worktrees now have lifecycle options to
preserve their commits after the agent stops, without using `isolate=False`.

- `WorktreeManager.teardown(merge_to_base=True)` — squash-merges the agent's
  worktree branch (`worktree/{agent_id}`) into the main repo HEAD before
  removing the worktree and branch. A no-op when there are no new commits.
- `WorktreeManager.keep_branch(agent_id)` — removes the worktree directory
  but preserves the branch for manual inspection and merging.
- `AgentConfig.merge_on_stop: bool = False` — YAML/config flag that
  automatically sets `teardown(merge_to_base=True)` for that agent.
- Exposed in `ClaudeCodeAgent(merge_on_stop=)`, `POST /agents/new`, and the
  CONTROL `create_agent` handler.

### Tests

- 11 new tests in `tests/test_dynamic_agent.py` — covers create_agent() unit
  behaviour, auto-ID generation, P2P grant, STATUS event, CONTROL dispatch,
  REST 200/409 paths.
- 3 new tests in `tests/test_worktree.py` — covers squash merge, keep_branch,
  and no-op when no new commits.

Total: **361 tests** (was 347).

---

## [0.21.0] — 2026-03-05

### Added

**Context Window Usage Monitoring + NOTES.md Update Notification**

Closes two open §11 items: (a) agent context usage monitoring and
(b) NOTES.md update notification when `/summarize` is run.

Motivated by Liu et al. "Lost in the Middle" (TACL 2024): LLM accuracy
degrades significantly when the context window is more than 75% full.
Proactive compression via `/summarize` extends effective working time.

- `ContextMonitor` — new `context_monitor.py` module:
  - Polls every agent's tmux pane (configurable interval, default 5 s).
  - Tracks `pane_chars` and estimates token count (`chars / 4`).
  - Publishes `context_warning` STATUS event when estimated tokens
    exceed `warn_threshold` fraction of `context_window_tokens`.
  - Detects `NOTES.md` `mtime` changes; publishes `notes_updated`
    STATUS event so parent/orchestrator agents can react.
  - Optionally injects `/summarize` into the agent pane when threshold
    is exceeded (`auto_summarize=True`). Injection is debounced: fires
    at most once per threshold crossing; resets after NOTES.md update.
  - Publishes `summarize_triggered` STATUS event when injection occurs.
  - `get_stats(agent_id)` / `all_stats()` for REST consumption.
- `AgentContextStats` dataclass: per-agent snapshot with `pane_chars`,
  `estimated_tokens`, `context_pct`, `notes_mtime`, and counters
  (`notes_updates`, `context_warnings`, `summarize_triggers`).
- `Orchestrator._context_monitor` — created at `__init__` time;
  `start()` calls `context_monitor.start()`, `stop()` calls `context_monitor.stop()`.
- `Orchestrator.get_agent_context_stats(agent_id)` and
  `all_agent_context_stats()` — delegate to `ContextMonitor`.
- `GET /agents/{id}/stats` — per-agent context usage snapshot (404 when
  agent not yet tracked).
- `GET /context-stats` — context usage for all tracked agents.
- `OrchestratorConfig` — four new fields:
  - `context_window_tokens` (default 200 000 — Claude Sonnet/Opus)
  - `context_warn_threshold` (default 0.75 — 75%)
  - `context_auto_summarize` (default False)
  - `context_monitor_poll` (default 5.0 seconds)
- YAML config keys `context_window_tokens`, `context_warn_threshold`,
  `context_auto_summarize`, `context_monitor_poll` loaded by `load_config`.
- 21 new unit tests (347 total, all passing).
- OpenAPI schema snapshot updated.

### References

- Liu et al. "Lost in the Middle: How Language Models Use Long Contexts"
  TACL 2024 — https://arxiv.org/abs/2307.03172
- Anthropic token counting docs (2025) —
  https://platform.claude.com/docs/en/build-with-claude/token-counting
- Anthropic context windows docs (2025) —
  https://platform.claude.com/docs/en/build-with-claude/context-windows

---

## [0.20.0] — 2026-03-05

### Added

**Token-Bucket Rate Limiter — Task Submission Backpressure**

Prevents runaway Director agents from flooding the task queue by applying
a token-bucket algorithm (Tanenbaum §5.3) to `Orchestrator.submit_task()`.
The limiter is async-safe, live-reconfigurable via REST, and observable
through the existing bus STATUS events.

- `TokenBucketRateLimiter(rate, burst)` — new `rate_limiter.py` module.
  - `try_acquire()` — non-blocking; returns `False` when bucket is empty.
  - `acquire(timeout=N)` — async wait; raises `RateLimitExceeded` on timeout.
  - `reconfigure(rate, burst)` — live update without resetting the bucket.
  - `status()` — returns `{"enabled", "rate", "burst", "available_tokens"}`.
- `RateLimitExceeded` exception with `rate`, `burst`, `available` attributes.
- `Orchestrator.submit_task(..., wait_for_token=True)` — new `wait_for_token`
  parameter. When `False`, raises `RateLimitExceeded` immediately; publishes
  `rate_limit_exceeded` STATUS event to the bus for observability.
- `Orchestrator.set_rate_limiter(rl)` — attach or detach a limiter at runtime.
- `Orchestrator.get_rate_limiter_status()` — returns status dict (safe when no limiter).
- `Orchestrator.reconfigure_rate_limiter(rate, burst)` — create or update limiter.
- `OrchestratorConfig.rate_limit_rps` / `rate_limit_burst` — YAML config fields.
  Auto-creates limiter at startup when `rate_limit_rps > 0`. Burst defaults to
  `max(1, int(rps * 2))` when not specified.
- `GET /rate-limit` — current rate limiter status snapshot.
- `PUT /rate-limit` body `{"rate": N, "burst": M}` — live reconfiguration.
  Setting `rate=0` disables rate limiting (unlimited throughput).
- `RateLimitUpdate` Pydantic schema for PUT body validation.
- 24 new unit and REST tests (326 total, all passing).
- OpenAPI schema snapshot updated.

**Design references:**
- Tanenbaum, A.S. "Computer Networks" 5th ed. §5.3 — Token Bucket (2011).
- RFC 4115 "A Differentiated Service Two-Rate, Three-Color Marker", IETF (2005).
- aiolimiter v1.2.1: async-native leaky bucket for Python (2024).
  https://aiolimiter.readthedocs.io/
- NGINX `limit_req_zone` / `limit_req` HTTP rate limiting (2025).
  https://nginx.org/en/docs/http/ngx_http_limit_req_module.html
- DESIGN.md §10.16.

**Demo: Graph Coloring + Rate Limiting**
- Problem: Graph Coloring (15 nodes, 22 edges, K=4 colors), chromatic number=3.
- 3 ClaudeCodeAgent instances: greedy (degree-descending), backtracking (AC-3),
  local search (simulated annealing).
- `rate_limit_rps=3.0 burst=3` in config; `PUT /rate-limit` demonstrates live
  reconfiguration during the demo run.
- Demo folder: `~/Demonstration/v0.20.0-rate-limit-graph-coloring/`

---

## [0.19.0] — 2026-03-05

### Added

**Queue Pause/Resume + Task Priority Live Update**

Enables maintenance-mode queue control and live task priority adjustment,
combining Google Cloud Tasks queue-pause semantics with Python `heapq`
in-place priority mutation.

- `POST /orchestrator/pause` — halt the dispatch loop without killing in-flight
  tasks. Idempotent. Returns `{"paused": true}`.
- `POST /orchestrator/resume` — re-enable dispatch; queue drains immediately in
  priority order. Idempotent. Returns `{"paused": false}`.
- `GET /orchestrator/status` — operational snapshot: `paused`, `queue_depth`,
  `agent_count`, `dlq_depth`.
- `PATCH /tasks/{task_id}` body `{"priority": N}` — live priority update.
  Mutates the task in the heap, calls `heapq.heapify()` for O(n) rebuild.
  Returns `{"updated": bool, "task_id": ..., "priority": N}`.
- `Orchestrator.update_task_priority(task_id, new_priority)` — core method.
  Publishes `task_priority_updated` STATUS event on success.
- `TaskPriorityUpdate` Pydantic schema for PATCH body validation.
- 18 new unit and REST tests (302 total, all passing).
- OpenAPI schema snapshot updated.

**Design references:**
- Google Cloud Tasks `queues.pause` REST API (2024).
- Oracle WebLogic "Pause queue message operations at runtime" (2024).
- Python `heapq` docs "Priority Queue Implementation Notes".
- Liu, C.L.; Layland, J.W. (1973). "Scheduling Algorithms for
  Multiprogramming in a Hard Real-Time Environment". JACM 20(1).
- Sedgewick & Wayne "Algorithms" 4th ed. §2.4 — Priority Queues.

**E2E Demo (v0.19.0 — Weighted Interval Scheduling, pause/resume):**
- 3 ClaudeCodeAgents (`solver-greedy`, `solver-dp`, `solver-random`)
- WIS problem (N=12 intervals, optimal=80)
- Round 1: 3 tasks dispatched via `target_agent` routing
- Paused: 3 round-2 tasks enqueued with priorities 5, 3, 7
- PATCH: solver-random task promoted from priority 7→0 (heap rebuilt)
- Resumed: tasks dispatched in updated priority order (C→B→A)
- All 6 solutions valid, score=68 (85% of optimal)
- Demo folder: `~/Demonstration/v0.19.0-pause-resume-priority/`

---

## [0.18.0] — 2026-03-05

### Added

**Agent Capability Tags + Smart Dispatch**

Enables capability-based task routing: tasks with `required_tags` are only
dispatched to agents whose `tags` list is a superset of the required tags.
Inspired by the FIPA Directory Facilitator capability advertisement model
(2002) and Kubernetes Node Affinity label matching.

- `AgentConfig.tags: list[str]` — capability advertisement per agent in YAML config.
- `Task.required_tags: list[str]` — ALL listed tags must be present in the
  target agent's `tags` for the task to be dispatched there.
- `AgentRegistry.find_idle_worker(required_tags)` — updated with set subset
  matching: `set(required_tags) <= set(agent.tags)`. Backwards-compatible:
  empty `required_tags` (default) matches any idle worker.
- `Orchestrator.submit_task(required_tags=...)` — passes through to Task creation.
- `ClaudeCodeAgent` and factory accept `tags` from `AgentConfig`.
- `_spawn_subagent` propagates `template_cfg.tags` to sub-agents.
- `AgentRegistry.list_all()` includes `tags` field in agent snapshots.
- `Orchestrator.list_tasks()` includes `required_tags` in task snapshots.
- Dead-letter message clarified: `"no idle agent with required_tags=... after N retries"`.
- REST API: `POST /tasks` and `POST /tasks/batch` accept `required_tags: list[str]`.
  Response includes `required_tags` when non-empty.
- YAML config: agents accept `tags` list.
- 23 new tests in `tests/test_capability_tags.py`.

**Research references:**
- FIPA Agent Communication Language — Directory Facilitator (2002)
- Kubernetes nodeSelector / Node Affinity (2024)
- COLA: Collaborative Multi-Agent Framework (EMNLP 2025)
- Agent-Oriented Planning arXiv:2410.02189 (2024)

**E2E Demo (v0.18.0-capability-tags):**
- 2 real ClaudeCodeAgent instances: `python-expert` (tags: python, testing)
  and `docs-writer` (tags: markdown, documentation).
- Task A (`required_tags: [python, testing]`) dispatched exclusively to `python-expert`.
- Task B (`required_tags: [markdown, documentation]`) dispatched exclusively to `docs-writer`.
- All 4 correctness checks passed; DLQ empty; both tasks succeeded.
- Demo folder: `~/Demonstration/v0.18.0-capability-tags/`

---

## [0.17.0] — 2026-03-05

### Added

**Task Cancellation — `POST /tasks/{id}/cancel`**

Allows operators to remove a pending task from the priority queue before it
is dispatched to an agent.  Follows the async request-reply pattern described
in Microsoft Azure Architecture Center "Asynchronous Request-Reply pattern"
(2024).

- `Orchestrator.cancel_task(task_id)` — removes the task from the heap by
  rebuilding it without the cancelled entry; adjusts `_unfinished_tasks`
  counter; returns `True` if found and removed, `False` otherwise.
- Publishes a `task_cancelled` STATUS event on successful cancellation.
- `POST /tasks/{id}/cancel` REST endpoint — returns:
  - `{cancelled: true, status: "cancelled"}` if removed from queue.
  - `{cancelled: false, status: "already_dispatched"}` if not in queue but was tracked.
  - `404` if the task ID is completely unknown.
- Distinguishes pending vs already-dispatched vs unknown via `_task_started_at`,
  `_completed_tasks`, and DLQ lookup.
- 9 new tests in `tests/test_task_cancel.py`.

**Per-Agent Task History — `GET /agents/{id}/history`**

Enables per-agent observability: track every completed task with timing and
outcome.  Follows the TAMAS (IBM, 2025) "Beyond Black-Box Benchmarking"
observability model (arXiv:2503.06745).

- `Orchestrator.get_agent_history(agent_id, limit=50)` — returns the last N
  completed task records for an agent, most-recent-first; capped at 200.
  Each record: `task_id`, `prompt`, `started_at`, `finished_at`,
  `duration_s`, `status` ("success"|"error"), `error`.
- Dispatch loop records `_task_started_at` and `_task_started_prompt` on
  each dispatch; `_route_loop` calls `_record_agent_history()` on RESULT.
- `GET /agents/{id}/history?limit=N` REST endpoint — 404 for unknown agents.
- 12 new tests in `tests/test_agent_history.py`.

**Director → Workers Demo — `~/Demonstration/v0.17.0-director-workers/`**

Demonstrates the Orchestrator-Worker pattern (Guo et al. arXiv:2511.08475,
2024) with 4 real `ClaudeCodeAgent` instances:

- `agent-director` — receives coordination task, reads worker outputs,
  writes `integration_report.md` summarising the CRUD service
- `agent-w1` — implements POST /items endpoint (`endpoint_post_items.py`)
- `agent-w2` — implements GET /items/{id} endpoint (`endpoint_get_items.py`)
- `agent-w3` — implements DELETE /items/{id} endpoint (`endpoint_delete_items.py`)
- 3 workers run in parallel via `target_agent` routing (`POST /tasks`)
- `reply_to=agent-director` on each task routes results to director's mailbox
- Task cancellation demonstrated live: a dummy task queued while workers are
  busy, then cancelled before dispatch
- Per-agent history used to poll for task completion (replaces BUSY→IDLE polling)
- All 4 artifacts verified: 3 endpoint .py files + integration_report.md
- 0 DLQ entries, 0 errors
- Total elapsed: ~70 seconds

## [0.16.0] — 2026-03-05

### Added

**Shared Scratchpad REST API — `GET/PUT/DELETE /scratchpad/{key}`**

Implements the Blackboard architectural pattern (Buschmann et al., 1996):
a simple in-process key-value store that agents in pipeline workflows can
use to share intermediate results without file I/O or direct P2P messaging.

- `GET  /scratchpad/`          — list all key-value pairs
- `PUT  /scratchpad/{key}`     — write arbitrary JSON value; body: `{"value": ...}`; returns `{"key", "updated": true}`
- `GET  /scratchpad/{key}`     — read a value (404 if not found)
- `DELETE /scratchpad/{key}`   — delete an entry (404 if not found)
- State is in-process; cleared on server restart
- 17 new tests in `tests/test_scratchpad.py`

**`target_agent` task routing — dispatch a task to a specific agent**

Implements the Message Router pattern (Hohpe & Woolf "Enterprise Integration
Patterns", 2003): when a task is submitted with `target_agent` set, the
dispatch loop routes it exclusively to the named agent.

- `Task.target_agent: str | None` — new field in the `Task` dataclass
- `POST /tasks` and `POST /tasks/batch` accept optional `target_agent` parameter
- Dispatch loop: if `target_agent` is set and the agent exists but is busy,
  the task is re-queued and retried (up to `dlq_max_retries` times)
- If `target_agent` names an unknown agent, the task is dead-lettered immediately
- Response body includes `target_agent` when set
- 8 new tests in `tests/test_target_agent.py`

**Bug fix: non-isolated agents must not overwrite existing CLAUDE.md** (v0.15.1)

- `ClaudeCodeAgent._write_agent_claude_md()` is now only called when
  `isolate=True`; non-isolated agents share an existing directory that
  may already have a project-level `CLAUDE.md` — overwriting it would
  destroy project context (hotfix 9635538)
- 3 new tests in `tests/test_context_files.py`

**Peer Review Pipeline Demo — `~/Demonstration/v0.16.0-peer-review-pipeline/`**

- Orchestrates 2 real `ClaudeCodeAgent` instances in a 3-phase sequential pipeline:
  1. `agent-author` writes `data_processor.py` (CSV parsing, filtering, aggregation)
  2. `agent-reviewer` reads the code, writes `review.md` (MODERATE severity, 5 edge cases)
     and `test_data_processor.py` (12 tests covering edge cases)
  3. `agent-author` reads the review and refines `data_processor.py` (RFC 4180 compliance,
     proper error handling, empty-input fix)
- Uses `target_agent` routing to guarantee each task goes to the correct agent
- Uses shared scratchpad to pass the review summary (`SEVERITY=MODERATE EDGE_CASES=5
  TESTS=12`) and author revision confirmation back to the orchestrator
- Both agents use `isolate=false` and share the workspace directory
- Demo files: `peer_review_config.yaml`, `run_demo.py`, `workspace/`

Design references:
- Blackboard pattern: Buschmann et al. "Pattern-Oriented Software Architecture
  Vol 1: A System of Patterns" (1996)
- Message Router: Hohpe & Woolf "Enterprise Integration Patterns" (2003)
- AgentReview: Jin et al. "Exploring Peer Review Dynamics with LLM Agents"
  (EMNLP 2024) — arXiv:2406.12708

---

## [0.15.0] — 2026-03-05

### Added

**`POST /tasks/batch` — submit multiple tasks in one request**

- New REST endpoint `POST /tasks/batch` accepts `{"tasks": [...]}` body where
  each item is a full `TaskSubmit` (prompt, priority, metadata, reply_to)
- Returns `{"tasks": [...]}` with task_id, prompt, priority, and optional
  reply_to for each submitted task
- All tasks validated before any are enqueued (all-or-none semantics)
- New `TaskBatchSubmit` Pydantic model alongside existing request models
- 8 new tests in `tests/test_batch_tasks.py` covering: response structure,
  unique IDs, priorities, reply_to propagation, empty batch, auth, invalid
  body, and queue visibility

**AHC Best-of-N Demo — `~/Demonstration/v0.15.0-ahc-best-of-n/`**

- Orchestrates 3 real `ClaudeCodeAgent` instances solving the same Weighted
  Knapsack problem (N=15, C=50) with different strategies in parallel:
  - `agent-greedy`: greedy by value/weight ratio
  - `agent-random`: Monte Carlo sampling (10 000 trials)
  - `agent-dp`: exact 0-1 dynamic programming (optimal score=154)
- Each agent independently writes a solver, runs it, and writes a solution file
- `score.py` validates solutions and outputs `SCORE=N`; orchestrator selects winner
- Uses `POST /tasks/batch` to submit all 3 tasks simultaneously
- Demo files: `problem.txt`, `score.py`, `ahc_config.yaml`, `run_demo.py`

**Bug fix: `TaskResultPayload.output` type coercion**

- `@field_validator("output", "error", mode="before")` — previously only
  `error` was coerced; `output` with non-string values (e.g. int from
  hypothesis-generated payloads) raised `ValidationError`
- Fixes pre-existing property-test failure in `tests/test_properties.py`

Design references:
- REST batch: adidas API Guidelines "Batch Operations"; PayPal Tech Blog
  "Batch: An API to bundle multiple REST operations"
- Best-of-N: Inference Scaling Laws (ICLR 2025); OpenAI arXiv:2502.06807 (2025)
- Multi-agent failures: Cemri et al. arXiv:2503.13657 (2025)

---

## [0.14.0] — 2026-03-05

### Added

**Task result routing — `reply_to` field**

- New `Task.reply_to: str | None` field: when set, the orchestrator delivers
  the completed RESULT directly to the named agent's mailbox (file) and calls
  `agent.notify_stdin("__MSG__:<id>")` — closing the feedback loop for
  multi-level hierarchies without requiring the parent to poll the bus
- `Orchestrator.submit_task()` accepts new `reply_to: str | None` keyword arg
- New `Orchestrator._route_result_reply()` coroutine handles per-task delivery
  after RESULT messages are received in `_route_loop`
- `_task_reply_to: dict[str, str]` routing table tracks task→agent mapping
  (cleaned up on delivery to prevent unbounded growth)
- `_mailbox: Mailbox | None` injectable attribute; callers (main.py / tests)
  inject a configured `Mailbox` instance for file-based delivery
- REST `POST /tasks` now accepts `reply_to` field and echoes it in the response
- 6 new tests in `tests/test_result_routing.py`

Design references:
- Request-reply with correlation IDs: "Learning Notes #15 – Request Reply
  Pattern | RabbitMQ" (parottasalna.com, 2024)
- Hierarchical information flow: Moore, D.J. "A Taxonomy of Hierarchical
  Multi-Agent Systems" arXiv:2508.12683 (2025)

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
