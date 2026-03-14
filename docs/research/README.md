# TmuxAgentOrchestrator — Research Log Index

> Migrated from `docs/research-log.md` (v1.2.29, 2026-03-15).
> The original file has been replaced with a redirect header pointing here.
> Each iteration's WebSearch findings, selection rationale, and implementation notes
> are recorded in per-version files.

## Structure

| File | Contents |
|------|----------|
| [pre-v1.2.md](pre-v1.2.md) | All research entries from v0.3.0 through v1.1.44 (bundled) |
| [v1.2.1.md](v1.2.1.md) | v1.2.1 — Scratchpad file persistence + slash commands |
| [v1.2.2.md](v1.2.2.md) | v1.2.2 — parallel: / sequence: workflow structure blocks |
| [v1.2.3.md](v1.2.3.md) | v1.2.3 — Dynamic ephemeral agent spawning (PhaseSpec.agent_template) |
| [v1.2.4.md](v1.2.4.md) | v1.2.4 — Branch-chain workflow execution |
| [v1.2.5.md](v1.2.5.md) | v1.2.5 — Branch-chain router wiring |
| [v1.2.6.md](v1.2.6.md) | v1.2.6 — Task priority dynamic update + branch trail |
| [v1.2.7.md](v1.2.7.md) | v1.2.7 — Loop until condition runtime evaluation |
| [v1.2.8.md](v1.2.8.md) | v1.2.8 — Workflow branch cleanup + merge_to_main_on_complete |
| [v1.2.9.md](v1.2.9.md) | v1.2.9 — Workflow phase webhook events |
| [v1.2.10.md](v1.2.10.md) | v1.2.10 — Codified Context spec file injection + PairCoder loop |
| [v1.2.11.md](v1.2.11.md) | v1.2.11 — Agent stats dashboard improvements |
| [v1.2.12.md](v1.2.12.md) | v1.2.12 — Circuit breaker auto-restart |
| [v1.2.13.md](v1.2.13.md) | v1.2.13 — Task timeout escalation |
| [v1.2.14.md](v1.2.14.md) | v1.2.14 — Workflow DAG visualization |
| [v1.2.15.md](v1.2.15.md) | v1.2.15 — Broadcast task + Best-of-N selection |
| [v1.2.16.md](v1.2.16.md) | v1.2.16 — Agent metrics time series |
| [v1.2.17.md](v1.2.17.md) | v1.2.17 — Task templates + preset library |
| [v1.2.19.md](v1.2.19.md) | v1.2.19 — Context-exhausted agent auto-rotation + CLAUDE.md role rules |
| [v1.2.22.md](v1.2.22.md) | v1.2.22 — TDD role-specific docs (tester/coder/reviewer) |
| [v1.2.23.md](v1.2.23.md) | v1.2.23 — Workflow template parameter inheritance + context packs |
| [v1.2.24.md](v1.2.24.md) | v1.2.24 — Peer review workflow + agent plugin docs/README.md |
| [v1.2.25.md](v1.2.25.md) | v1.2.25 — Mutation test workflow (POST /workflows/mutation-test) |
| [v1.2.26.md](v1.2.26.md) | v1.2.26 — Socratic dialogue + code audit workflows |
| [v1.2.27.md](v1.2.27.md) | v1.2.27 — Code refactoring workflow (POST /workflows/refactor) |
| [v1.2.28.md](v1.2.28.md) | v1.2.28 — YAML-driven workflow template execution (POST /workflows/from-template) |
| [v1.2.29.md](v1.2.29.md) | v1.2.29 — YAML generic workflow template library (7 new templates) + docs restructure |

## How to Add a New Entry

1. Create `docs/research/v{major}.{minor}.{patch}.md`
2. Add an entry to this README table
3. Reference from `DESIGN.md §10.N`

## Migration Notes

The original `docs/research-log.md` (9646 lines) was split on 2026-03-15:
- Pre-v1.2 content (lines 1–7653) → `pre-v1.2.md`
- v1.2.1 through v1.2.27 entries → individual version files
- v1.2.28 entry → extracted from DESIGN.md §10.103
- v1.2.29 entry → new (this iteration)
