# Agent Plugin Docs — Role Documentation

This directory contains role-specific documentation files loaded by
`ClaudeCodeAgent` at startup via the `TMUX_ORCHESTRATOR_AGENT_ROLE` and
`TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR` environment variables.

Each file is a standalone instruction set for one agent role. Claude reads
the appropriate file for its role when it starts, so different roles get
different behavioural guidelines without modifying `CLAUDE.md`.

---

## Available Role Docs

| File | Role value | Workflow usage | Primary responsibility |
|------|-----------|----------------|------------------------|
| `worker.md` | `worker` | General-purpose workers | Accept a task, implement it, call `/task-complete` |
| `director.md` | `director` | Director in any workflow | Coordinate, delegate, aggregate, call REST task-complete |
| `planner.md` | `planner` | Planning phases in AgentMesh, custom | Decompose goal → `PLAN.md`, write to scratchpad |
| `architect.md` | `architect` | Architecture phases | Design → `ARCHITECTURE.md`, store path in scratchpad |
| `spec-writer.md` | `spec-writer` | Spec-first, FullDev | Write `SPEC.md` with acceptance criteria |
| `tester.md` | `tester` | TDD Red phase | Write **failing** tests only, commit, handoff |
| `coder.md` | `coder` | TDD Green phase, PeerReview impl-a/b | Make tests pass, YAGNI, minimal code |
| `reviewer.md` | `reviewer` | TDD Refactor, PeerReview, MobReview | Structured REVIEW.md, no behaviour changes |

---

## Loading Mechanism

At agent startup `ClaudeCodeAgent._write_agent_claude_md()` adds the
following snippet to the agent's `CLAUDE.md`:

```bash
# Read your role-specific rules
cat "$TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR/$TMUX_ORCHESTRATOR_AGENT_ROLE.md"
```

The orchestrator sets two environment variables in the agent's tmux pane:

| Variable | Value |
|----------|-------|
| `TMUX_ORCHESTRATOR_AGENT_ROLE` | Role name, e.g. `tester` |
| `TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR` | Absolute path to this directory |

If no file exists for the agent's role, the agent falls back to `worker.md`.

---

## Document Format Convention

Every role doc **must** follow this structure (sections in this order):

```markdown
# <Role Name> Role — <One-Line Description>

You are a **<Role>** agent. <One-sentence role definition>

## Core Responsibilities

1. <Numbered list of primary duties>
2. ...

## <Role-Specific Section>
<Detailed rules unique to this role>

## Handoff Protocol

After completing your work:
1. Write artefacts to scratchpad: `/scratchpad-write <key> <value>`
2. Report progress: `/progress "<what was done>"`
3. Call `/task-complete "<one-line summary>"`

## Context Engineering

- **Write**: ...
- **Select**: ...
- **Compress**: ...
- **Isolate**: ...

## Anti-Patterns to Avoid

- <Explicit list of what NOT to do>
```

**Required sections** (must appear in every doc):
- `## Core Responsibilities`
- `## Handoff Protocol` (with explicit `/task-complete` call)
- `## Anti-Patterns to Avoid`

**Optional sections** (include when relevant):
- `## Context Engineering`
- Role-specific sections (e.g. `## TDD Rules`, `## Review Axes`)

---

## Workflow ↔ Role Mapping

The table below shows which workflow endpoints use which roles and how
to configure agents in your YAML config file.

| Workflow endpoint | Agent roles used | Notes |
|-------------------|-----------------|-------|
| `POST /workflows/tdd` | `tester` → `coder` → `reviewer` | Sequential Red→Green→Refactor |
| `POST /workflows/peer-review` | `coder` (×2, parallel) → `reviewer` | impl-a∥impl-b then review |
| `POST /workflows/fulldev` | `spec-writer` → `architect` → `tester` → `coder` → `reviewer` | 5-agent SDLC pipeline |
| `POST /workflows/agentmesh` | `planner` → `coder` → (debugger) → `reviewer` | 4-role development |
| `POST /workflows/spec-first` | `spec-writer` → `coder` | 2-agent spec+impl |
| `POST /workflows/spec-first-tdd` | `spec-writer` → `tester` → `coder` → `reviewer` | Spec+TDD combined |
| `POST /workflows/pair` | `planner` (navigator) → `coder` (driver) | Navigator+Driver |
| `POST /workflows/paircoder` | `coder` (writer) → `reviewer` (reviewer) | Writer→Reviewer loop |
| `POST /workflows/mob-review` | `reviewer` (×N parallel) → synthesizer | N-perspective review |
| `POST /workflows/iterative-review` | `coder` → `reviewer` → loop | Iterative improvement |
| `POST /workflows/debate` | advocate → critic → judge | 3-agent debate |
| `POST /workflows/adr` | proposer → reviewer → synthesizer | Architecture Decision Record |
| `POST /workflows/delphi` | experts (×N parallel) → moderator → loop | Multi-round consensus |
| `POST /workflows/redblue` | `coder` (blue) → attacker (red) → arbiter | Security review |
| `POST /workflows/clean-arch` | domain → usecase → adapter → framework | 4-layer design |
| `POST /workflows/ddd` | domain-expert → bounded-context → integration | DDD decomposition |

---

## Adding a New Role Doc

1. Create `agent_plugin/docs/<role-name>.md` following the format convention.
2. Add an entry to the table above with the role value, filename, and workflow.
3. Update `CLAUDE.md` (main project) role table in the "Running as an Orchestrated
   Agent" section if the role is surfaced to operators.
4. Add the role to `AgentRole` enum in `domain/agent.py` if it needs a typed value.
5. Write at least one test in `tests/test_role_rules.py` verifying:
   - The `.md` file exists at the expected path.
   - The doc contains `## Core Responsibilities` and `## Handoff Protocol`.
   - The doc mentions `/task-complete`.

---

## File Naming Convention

- File name = role value (e.g. `tester.md` for `role: tester` in YAML config).
- Use lowercase, hyphens only (no underscores, no spaces).
- One file per role; do not combine multiple roles in one file.

---

## Testing Role Docs

Tests for role docs live in:

- `tests/test_role_rules.py` — verifies env var injection and CLAUDE.md content
- `tests/test_tdd_role_docs.py` — verifies tester/coder/reviewer docs exist and are well-formed

Run with:

```bash
uv run pytest tests/test_role_rules.py tests/test_tdd_role_docs.py -v
```
