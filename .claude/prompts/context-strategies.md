# Context Engineering Strategies for Orchestrated Agents

This document provides a detailed reference for the four context engineering strategies
available to agents running inside TmuxAgentOrchestrator.

Based on: Anthropic "Effective context engineering for AI agents" (2025-09-29);
LangChain Blog "Context Engineering for Agents" (2025);
JetBrains Research "Cutting Through the Noise" (2025-12).

---

## The Four Strategies

### 1. Write

**Definition**: Save information outside your context window so it persists across turns
and is accessible to other agents or future agent sessions.

**When to use**:
- Your task produces artefacts (code, plans, reports) that another agent needs.
- You are approaching the context limit and want to preserve key decisions.
- You need to pass structured data to a downstream agent in a pipeline.

**How to apply in TmuxAgentOrchestrator**:

```bash
# Write to a file in your worktree (git-tracked, survives /summarize)
cat > PLAN.md << 'EOF'
## Implementation Plan
1. Define the data model
2. Write failing tests
3. Implement
EOF

# Write to the shared scratchpad (accessible cross-agent)
curl -X PUT $TMUX_ORCHESTRATOR_WEB_BASE_URL/scratchpad/my-result \
  -H "X-API-Key: $TMUX_ORCHESTRATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"value": "result data here"}'

# Write structured notes before /summarize
# NOTES.md is preserved by /summarize as the compressed context seed
```

**Anti-patterns**:
- Do NOT leave important decisions only in chat history — they will be lost after `/summarize`.
- Do NOT write everything; write only what a future agent or future turn will need.

---

### 2. Select

**Definition**: Pull only the relevant information into your context window when you need it.
The goal is a focused, high-signal context.

**When to use**:
- You need to read prior work (spec, plan, test results) without loading everything.
- You are a reviewer/judge and only need the artefacts under evaluation.
- Multiple files exist but only a subset is relevant to your current step.

**How to apply in TmuxAgentOrchestrator**:

```yaml
# In your agent config YAML: declare exactly which files to copy into the worktree
agents:
  - id: reviewer
    type: claude_code
    system_prompt_file: .claude/prompts/roles/reviewer.md
    context_files:
      - docs/spec.md        # only the spec
      - src/module.py       # only the file under review
```

```bash
# Or read files explicitly at the start of your task
# Read only what you need for this step — not the entire codebase
```

**Anti-patterns**:
- Do NOT add `context_files: ["**/*.py"]` — this floods context with irrelevant files.
- Do NOT read files speculatively; read them when you need them.

---

### 3. Compress

**Definition**: Reduce the token count of your context by summarising history into a
compact form, then continuing with the compressed version.

**When to use**:
- Your context exceeds ~60% of the context window.
- You receive a `context_warning` bus event from the orchestrator.
- You have completed a major phase and want to free context for the next phase.

**How to apply in TmuxAgentOrchestrator**:

```
/summarize
```

This compresses the conversation into `NOTES.md` and restarts the context window
with the summary as the seed. The orchestrator's `ContextMonitor` can also trigger
`/summarize` automatically when `context_warn_threshold` is exceeded.

**What is preserved after `/summarize`**:
- Contents of `NOTES.md` (reloaded as context seed)
- All files committed to your worktree branch
- Scratchpad entries (server-side, unaffected by local context)
- Your task prompt (re-injected by the orchestrator)

**Anti-patterns**:
- Do NOT run `/summarize` before writing key decisions to `NOTES.md` or a file.
- Do NOT ignore `context_warning` events — degraded recall causes errors.

---

### 4. Isolate

**Definition**: Split the task across multiple agents, each with its own isolated
context window focused on a narrow sub-problem.

**When to use**:
- The task has 3+ major sub-problems that each require deep focus.
- You are a planner/director and want workers to run in parallel.
- You want to prevent cross-contamination between independent workstreams.

**How to apply in TmuxAgentOrchestrator**:

```
/spawn-subagent worker-2
```

After spawning, send the sub-task via `/send-message`:

```
/send-message worker-2-sub-a3f2 Please implement the authentication module. See PLAN.md §3 for the spec.
```

Or use a workflow endpoint that isolates agents automatically:

```bash
curl -X POST $TMUX_ORCHESTRATOR_WEB_BASE_URL/workflows/tdd \
  -H "X-API-Key: $TMUX_ORCHESTRATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"topic": "FizzBuzz", "spec": "Write a function ..."}'
```

**Anti-patterns**:
- Do NOT spawn a sub-agent for a 10-line task — coordination overhead exceeds benefit.
- Do NOT leave sub-agents without a clear completion signal (`/task-complete`).

---

## Role-Based Strategy Matrix

| Role | Write | Select | Compress | Isolate |
|------|-------|--------|----------|---------|
| implementer | HIGH: PLAN.md + commits | LOW: read spec only | MEDIUM: compress between phases | HIGH: delegate sub-features |
| reviewer | LOW: write review report | HIGH: load only the artefact | HIGH: compress before final report | LOW: single-agent task |
| tester | HIGH: write test plan + test files | MEDIUM: read impl under test | MEDIUM | LOW |
| spec-writer | HIGH: write spec file early | LOW | HIGH: compress before synthesis | MEDIUM |
| planner | HIGH: write plan to scratchpad | LOW | LOW | HIGH: delegate phases |
| judge / arbiter | MEDIUM: write verdict | HIGH: load competing artefacts only | HIGH: compress before verdict | LOW |
| advocate / critic | LOW | MEDIUM: load the position document | HIGH | LOW |

---

## Combining Strategies

The highest-performing configurations combine all four strategies
(Anthropic multi-agent researcher benchmark, 2025):

**Pipeline pattern** (implementer → reviewer):
1. implementer: **Write** spec to scratchpad → **Isolate** implementation sub-tasks → **Compress** between phases
2. reviewer: **Select** only the implementation file + spec → review → **Write** report to scratchpad → **Compress** if needed

**Director → Workers pattern**:
1. director: **Write** plan to scratchpad → **Isolate** (spawn 3 workers) → **Select** worker results from scratchpad
2. each worker: **Select** its sub-task from plan → implement → **Write** result to scratchpad → `/task-complete`

**Competition pattern**:
1. N solvers: **Isolate** (each has its own context) → **Write** solution + score to scratchpad
2. judge: **Select** all solutions from scratchpad → **Compress** → pick winner → **Write** verdict
