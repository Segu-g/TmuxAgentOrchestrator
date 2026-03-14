# Documentation Audit — TmuxAgentOrchestrator

Audited files: `README.md`, `CLAUDE.md`, `DESIGN.md` (§10–§11), `CHANGELOG.md`,
`src/tmux_orchestrator/web/app.py`, `src/tmux_orchestrator/orchestrator.py`,
`src/tmux_orchestrator/config.py`, `src/tmux_orchestrator/agents/base.py`,
`src/tmux_orchestrator/rate_limiter.py`, `src/tmux_orchestrator/context_monitor.py`.

---

## 1. Missing from README

Features and endpoints added in v0.11–v0.21 that are entirely absent from `README.md`.

### 1.1 Undocumented REST endpoints

README.md lines 117–126 show only 7 endpoints. The following 20+ endpoints exist in
`web/app.py` but are not mentioned in README at all:

| Method | Path | Introduced | Source |
|--------|------|-----------|--------|
| `GET` | `/agents/tree` | v0.11.0 | `app.py:431` |
| `POST` | `/agents/{id}/reset` | v0.12.0 | `app.py:453` |
| `GET` | `/agents/{id}/stats` | v0.21.0 | `app.py:470` |
| `GET` | `/agents/{id}/history` | v0.17+ | `app.py:515` |
| `POST` | `/tasks/batch` | v0.18+ | `app.py:390` |
| `POST` | `/tasks/{id}/cancel` | v0.17.0 | `app.py:545` |
| `PATCH` | `/tasks/{id}` | v0.18+ | `app.py:600` |
| `POST` | `/orchestrator/pause` | v0.19.0 | `app.py:633` |
| `POST` | `/orchestrator/resume` | v0.19.0 | `app.py:658` |
| `GET` | `/orchestrator/status` | v0.19.0 | `app.py:674` |
| `GET` | `/rate-limit` | v0.20.0 | `app.py:695` |
| `PUT` | `/rate-limit` | v0.20.0 | `app.py:711` |
| `GET` | `/scratchpad/` | v0.16.0 | `app.py:804` |
| `PUT` | `/scratchpad/{key}` | v0.16.0 | `app.py:817` |
| `GET` | `/scratchpad/{key}` | v0.16.0 | `app.py:827` |
| `DELETE` | `/scratchpad/{key}` | v0.16.0 | `app.py:838` |
| `GET` | `/dlq` | v0.4.0 | `app.py:897` |
| `GET` | `/events` | v0.12.0 | `app.py:988` |
| `GET` | `/context-stats` | v0.21.0 | `app.py:501` |
| `GET` | `/healthz` | v0.3.0 | `app.py:854` |
| `GET` | `/readyz` | v0.3.0 | `app.py:859` |
| `GET` | `/metrics` | v0.13.0 | `app.py:906` |

### 1.2 Undocumented features

| Feature | Introduced | Notes |
|---------|-----------|-------|
| Context window monitoring (`ContextMonitor`) | v0.21.0 | Polls pane size, estimates tokens, auto-injects `/summarize` |
| NOTES.md update detection | v0.21.0 | Publishes `notes_updated` STATUS event on bus |
| Token-bucket rate limiter | v0.20.0 | Configurable via `rate_limit_rps` / `rate_limit_burst`; live reconfiguration via `PUT /rate-limit` |
| Dispatch pause / resume | v0.19.0 | In-flight tasks continue; queue accumulates while paused |
| Dead letter queue (DLQ) | v0.4.0 | Tasks dead-lettered after `dlq_max_retries`; queryable via `GET /dlq` |
| Shared scratchpad | v0.16.0 | Blackboard pattern for inter-agent data; agents write/read via REST |
| Task cancellation | v0.17.0 | Removes pending task from queue; returns 404 if never submitted |
| Task priority update | v0.18+ | `PATCH /tasks/{id}` updates priority in-place; heap is rebuilt |
| Per-agent task history | v0.17+ | Last 200 completed tasks per agent; most-recent-first |
| Batch task submission | v0.18+ | `POST /tasks/batch` validates all items before enqueueing |
| `reply_to` task field | v0.14.0 | Delivers RESULT directly to named agent's mailbox |
| `target_agent` task field | v0.14+ | Forces task to wait for a specific agent |
| `required_tags` / agent `tags` | v0.11+ | Capability-based dispatch (FIPA Directory Facilitator pattern) |
| Circuit breaker | v0.3.0 | CLOSED→OPEN→HALF_OPEN per agent; configurable threshold/recovery |
| Auto-recovery with backoff | v0.12.0 | ERROR agents restarted up to `recovery_attempts` times; exponential backoff at `backoff_base^N` seconds |
| SSE event stream | v0.12.0 | `GET /events`; browser `EventSource` API; keep-alive every 15s |
| Prometheus metrics | v0.13.0 | `GET /metrics`; no auth required; gauges for agent status, queue depth, bus drops |
| Agent hierarchy tree | v0.11.0 | `GET /agents/tree`; nested JSON; Web UI List/Tree toggle |
| `context_files` auto-copy | v0.11.0 | Copies files into worktree at agent start (`shutil.copy2`) |
| `system_prompt` injection | v0.10+ | Injected into auto-generated `CLAUDE.md` in worktree |
| Watchdog timeout | v0.10.0 | Publishes synthetic `RESULT(error="watchdog_timeout")` at 1.5× threshold |
| Idempotency keys | v0.10.0 | `submit_task(idempotency_key=)`; 1-hour TTL, in-process only |
| Structured JSON logging | v0.7.0 | `trace_id` + `agent_id` in every log record via `contextvars` |
| Task `depends_on` / Workflow | v0.8.0 | Topological sort; tasks wait for dependencies to complete |
| `POST /agents/{id}/reset` | v0.12.0 | Clears ERROR / permanently-failed state and restarts agent |

---

## 2. Config Fields Not Documented

README.md Configuration table (lines 79–86) and Agent fields table (lines 88–96)
are both significantly out of date.

### 2.1 Missing OrchestratorConfig fields

All fields defined in `config.py:38–71` but absent from README:

| Field | Default | Since | Source |
|-------|---------|-------|--------|
| `circuit_breaker_threshold` | `3` | v0.3.0 | `config.py:46` |
| `circuit_breaker_recovery` | `60.0` | v0.3.0 | `config.py:47` |
| `dlq_max_retries` | `50` | v0.4.0 | `config.py:48` |
| `task_queue_maxsize` | `0` (unbounded) | v0.9.0+ | `config.py:49` |
| `watchdog_poll` | `10.0` | v0.10.0 | `config.py:50` |
| `recovery_attempts` | `3` | v0.12.0 | `config.py:52` |
| `recovery_backoff_base` | `5.0` | v0.12.0 | `config.py:53` |
| `recovery_poll` | `2.0` | v0.12.0 | `config.py:54` |
| `rate_limit_rps` | `0.0` (disabled) | v0.20.0 | `config.py:59` |
| `rate_limit_burst` | `0` (2× rps) | v0.20.0 | `config.py:60` |
| `context_window_tokens` | `200000` | v0.21.0 | `config.py:68` |
| `context_warn_threshold` | `0.75` | v0.21.0 | `config.py:69` |
| `context_auto_summarize` | `false` | v0.21.0 | `config.py:70` |
| `context_monitor_poll` | `5.0` | v0.21.0 | `config.py:71` |

### 2.2 Missing AgentConfig fields

README agent fields table (lines 88–96) omits:

| Field | Default | Notes |
|-------|---------|-------|
| `task_timeout` | `null` | Per-agent override of the global `task_timeout`; `config.py:25` |
| `command` | `null` | Custom CLI command (defaults to `claude` CLI); `config.py:26` |
| `system_prompt` | `null` | Injected into auto-generated `CLAUDE.md`; `config.py:28` |
| `context_files` | `[]` | Relative paths copied into worktree at startup; `config.py:29` |
| `tags` | `[]` | Capability tags for smart dispatch; `config.py:34` |

---

## 3. REST API Gaps (No Public Documentation)

The following endpoints exist in `web/app.py` and have no public documentation in
README, CLAUDE.md, or any USER_GUIDE file.

### 3.1 Auth endpoints (all `include_in_schema=False`)

These are not listed in the README's REST API table or anywhere in the docs:

- `GET /auth/status` — `app.py:257`
- `POST /auth/register-options` — `app.py:264`
- `POST /auth/register` — `app.py:281`
- `POST /auth/authenticate-options` — `app.py:309`
- `POST /auth/authenticate` — `app.py:325`
- `POST /auth/logout` — `app.py:358`

The README mentions passkey authentication (line 13, 66, 99–101) but gives no API
reference for the auth handshake flow.

### 3.2 All undocumented data-plane endpoints

See Section 1.1 above. In addition, the following fields of documented endpoints are
not described:

- `POST /tasks` accepts `reply_to`, `target_agent`, `required_tags` — none documented
  in README (`app.py:42–51`, `TaskSubmit` model).
- `POST /director/chat` accepts optional `?wait=true` query parameter for synchronous
  response — undocumented (`app.py:764`).
- `GET /agents/{id}/history` accepts `?limit=N` (default 50, max 200) — not described.
- `GET /readyz` returns 503 with detail when not ready — not described.

---

## 4. CLAUDE.md Gaps

The "Running as an Orchestrated Agent" section of `CLAUDE.md` is missing information
that an orchestrated agent would need to operate effectively.

### 4.1 Slash Command Reference is incomplete

CLAUDE.md's "Slash Command Reference" table (near end of file) lists only 5 commands:
`/check-inbox`, `/read-message`, `/send-message`, `/spawn-subagent`, `/list-agents`.

The auto-generated `CLAUDE.md` written to each agent's worktree by
`ClaudeCodeAgent._write_agent_claude_md()` (`claude_code.py:138`) includes the full
set. The following commands are described in `DESIGN.md §7` but absent from the
CLAUDE.md reference table:

| Command | Purpose |
|---------|---------|
| `/plan <description>` | Write `PLAN.md` before implementation |
| `/tdd <feature>` | Guide Red→Green→Refactor cycle |
| `/progress <summary>` | Report progress to parent agent |
| `/summarize` | Compress context state into `NOTES.md` |
| `/delegate <task>` | Break task into subtasks for sub-agents |

### 4.2 Shared scratchpad not mentioned

The shared scratchpad (`GET/PUT/DELETE /scratchpad/{key}`) — added v0.16.0 as the
Blackboard pattern for inter-agent data sharing — is entirely absent from CLAUDE.md.
Agents performing pipeline workflows (one writes, another reads) need this to share
structured data without P2P messaging.

### 4.3 Bus events from context monitor not described

Agents receive the following STATUS events from `__context_monitor__` but CLAUDE.md
does not mention them:

| Event | Payload fields | Meaning |
|-------|---------------|---------|
| `context_warning` | `agent_id`, `estimated_tokens`, `context_pct`, `context_window_tokens` | Agent pane output exceeds `warn_threshold` |
| `notes_updated` | `agent_id`, `notes_path`, `notes_mtime`, `preview` | Agent's `NOTES.md` was modified |
| `summarize_triggered` | `agent_id`, `estimated_tokens`, `context_pct` | `/summarize` was auto-injected |

Parent/Director agents that subscribe to the bus can react to these to proactively
rotate or re-brief workers. Agents that receive `context_warning` directed at
themselves know to run `/summarize` voluntarily.

### 4.4 Task submission fields not described for agents

CLAUDE.md describes receiving tasks but not submitting them via REST. Agents that use
`POST /tasks` (a common Director pattern) are not told about:

- `reply_to` — routes the RESULT directly to a named agent's mailbox
- `target_agent` — pins the task to a specific worker
- `required_tags` — restricts dispatch to workers advertising matching capability tags

### 4.5 `reply_to` result delivery not explained

When a task is submitted with `reply_to: "agent-id"`, the orchestrator writes the
RESULT message to the named agent's mailbox AND delivers `__MSG__:{id}` to its pane
(same mechanism as P2P). This is the canonical way for a Director to receive worker
results. It is not mentioned in CLAUDE.md.

### 4.6 `GET /agents/{id}/history` not mentioned

Agents can query their own or sibling agents' task history for self-awareness and
coordination. Not mentioned in CLAUDE.md.

---

## 5. Outdated Content

### 5.1 README.md — `task_timeout` wrong default (line 83)

README states:
```
| `task_timeout` | `null` | Per-task timeout in seconds |
```

The code (`config.py:43`) defines:
```python
task_timeout: int = 120
```

The default is **120 seconds**, not `null`. (`AgentConfig.task_timeout` defaults to
`None`, but that is a *per-agent override*; the global OrchestratorConfig default is 120.)

### 5.2 README.md — REST API table severely outdated (lines 117–126)

The table lists 7 endpoints. The actual API has 30+ endpoints (see Section 1.1 and
Section 3). The table was approximately current at v0.3.0 and has not been updated
since.

### 5.3 README.md — Agent fields table incomplete (lines 88–96)

Lists only `id`, `type`, `role`, `isolate`. Five additional fields are supported:
`task_timeout`, `command`, `system_prompt`, `context_files`, `tags`. The YAML config
example at lines 40–52 does not demonstrate any of these advanced fields.

### 5.4 README.md — Configuration table missing 14 fields (lines 79–86)

The table has 6 rows. `OrchestratorConfig` has 20 fields. See Section 2.1 for the
complete list of undocumented fields.

### 5.5 CLAUDE.md — Slash Command Reference incomplete

See Section 4.1. The table lists 5 commands; 10 are available. The missing 5
(`/plan`, `/tdd`, `/progress`, `/summarize`, `/delegate`) are described in
`DESIGN.md §7` and are written into each agent's auto-generated `CLAUDE.md`.

### 5.6 CLAUDE.md — `__orchestrator_context__.json` example is accurate

The example in CLAUDE.md includes `session_name` and `web_base_url`. These are
injected by `ClaudeCodeAgent._context_extras()` at `claude_code.py:131–132`. This
section is **correct** and does not need updating.

### 5.7 CLAUDE.md — Completion detection patterns

CLAUDE.md lists `❯`, `$`, `$` (with space), `>`, `Human:` as completion signals. The
actual patterns in `claude_code.py:29–34` are anchored regexes:

```python
re.compile(r"^\s*❯\s*$", re.MULTILINE)
re.compile(r"^\s*>\s*$", re.MULTILINE)
re.compile(r"Human:\s*$", re.MULTILINE)
re.compile(r"\$\s*$", re.MULTILINE)
```

The CLAUDE.md listing is functionally accurate, but listing `$` and `$ ` as separate
entries is misleading — they are covered by a single `\$\s*$` regex. Consider
clarifying that any trailing whitespace is tolerated.

### 5.8 DESIGN.md §2 component list is outdated

`DESIGN.md §2` lists these components:
```
├── WorktreeManager (worktree.py)
├── Mailbox (messaging.py)
```
And omits post-v0.5.0 modules:
- `registry.py` (AgentRegistry) — v0.5.0
- `factory.py` (SystemFactory / `build_system()`) — v0.6.0
- `logging_config.py` (structured JSON logging) — v0.7.0
- `workflow.py` (Workflow builder + topological sort) — v0.8.0
- `supervision.py` (`supervised_task()`) — v0.10.0
- `schemas.py` (Pydantic payload models) — v0.4.0
- `rate_limiter.py` (TokenBucketRateLimiter) — v0.20.0
- `context_monitor.py` (ContextMonitor) — v0.21.0

---

## Summary

| Category | Issues found |
|----------|-------------|
| Undocumented REST endpoints | 22 |
| Undocumented features | 22 |
| Missing OrchestratorConfig fields | 14 |
| Missing AgentConfig fields | 5 |
| CLAUDE.md missing commands | 5 |
| Other CLAUDE.md gaps | 4 |
| Outdated/incorrect existing content | 8 |

**Highest priority fixes:**
1. `README.md` REST API table — rebuild from scratch to list all endpoints
2. `README.md` Configuration table — add all 14 missing OrchestratorConfig fields
3. `README.md` Agent fields table — add `system_prompt`, `context_files`, `tags`
4. `README.md` line 83 — correct `task_timeout` default from `null` to `120`
5. `CLAUDE.md` Slash Command table — add `/plan`, `/tdd`, `/progress`, `/summarize`, `/delegate`
6. `CLAUDE.md` — add Scratchpad API section for inter-agent data sharing
7. `CLAUDE.md` — add section on bus events from `ContextMonitor`
