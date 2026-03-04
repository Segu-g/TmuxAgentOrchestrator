Write a structured PLAN.md in your working directory before starting implementation.

Based on Context Engineering principles (Anthropic, 2025): a plan acts as a persistent external memory
that prevents context rot. Writing it before coding ensures the acceptance criteria are clear and
testable — the foundation of TDD.

Usage: `/plan <task description>`

Execute this Python snippet:

```python
import json, sys
from pathlib import Path
from datetime import datetime, timezone

description = """$ARGUMENTS""".strip()
if not description:
    print("Usage: /plan <task description>")
    print("  Writes PLAN.md with steps, acceptance criteria, and TDD test strategy.")
    raise SystemExit(1)

ctx_path = Path("__orchestrator_context__.json")
agent_id = json.loads(ctx_path.read_text())["agent_id"] if ctx_path.exists() else "unknown"

plan_path = Path("PLAN.md")
now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# Read existing NOTES.md for context if available
notes_context = ""
notes_path = Path("NOTES.md")
if notes_path.exists():
    notes_context = f"\n<!-- Existing notes loaded from NOTES.md -->\n"

content = f"""\
# Plan — {description}

**Agent**: `{agent_id}`
**Created**: {now}
**Status**: 🔴 In progress

{notes_context}
## Objective

{description}

## Acceptance Criteria

List specific, testable conditions that define "done":

- [ ] AC1: _define measurable outcome_
- [ ] AC2: _define measurable outcome_
- [ ] AC3: _edge cases handled_

## TDD Test Strategy

### Test cases to write FIRST (Red phase)
```
test_<feature>_<scenario>  →  expected outcome
test_<feature>_<edge_case> →  expected outcome
```

### Implementation steps (Green phase)
1. _minimal code to pass test 1_
2. _minimal code to pass test 2_

### Refactor targets
- _identify coupling, duplication, or clarity issues after Green_

## Subtasks

Break the work into independently completable units:

- [ ] 1. _subtask (can be delegated to sub-agent if needed)_
- [ ] 2. _subtask_
- [ ] 3. _subtask_

## Dependencies

- _list any blockers or prerequisite work_

## Notes

_Record decisions and open questions here as you work._
"""

plan_path.write_text(content)
print(f"✓ PLAN.md written for: {description}")
print()
print("Next steps:")
print("  1. Fill in Acceptance Criteria before writing any code")
print("  2. Write failing tests first (Red) — use `/tdd` for guidance")
print("  3. Implement minimally (Green), then refactor")
print("  4. Update NOTES.md as you progress")
print("  5. Run /progress when done to notify your parent agent")
```

After writing PLAN.md, fill in the acceptance criteria manually, then use `/tdd` to start the TDD cycle.
