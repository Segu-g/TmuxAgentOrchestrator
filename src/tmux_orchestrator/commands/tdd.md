Guide yourself through a Test-Driven Development (TDD) cycle for a specific feature.

TDD for AI agents (ref: Takuto Wada / Agile Journey 2025, Kent Beck): The Red→Green→Refactor
cycle serves as a guardrail that prevents AI-generated code from drifting into unmaintainable
implementations. The test is the specification — write it before the implementation.

Usage: `/tdd <feature or acceptance criterion>`

Execute this Python snippet:

```python
import json
from pathlib import Path
from datetime import datetime, timezone

feature = """$ARGUMENTS""".strip()
if not feature:
    print("Usage: /tdd <feature or acceptance criterion>")
    print("  Scaffolds a TDD cycle: Red → Green → Refactor")
    raise SystemExit(1)

ctx_path = Path("__orchestrator_context__.json")
agent_id = json.loads(ctx_path.read_text())["agent_id"] if ctx_path.exists() else "unknown"
now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

print(f"=== TDD Cycle: {feature} ===")
print(f"Agent: {agent_id}  |  {now}")
print()
print("━━━ PHASE 1: RED (Write a failing test) ━━━")
print()
print("Before writing any implementation code:")
print()
print("1. State the behaviour in one sentence:")
print(f'   "Given <context>, when <action>, then <outcome>"')
print(f'   → Feature: {feature}')
print()
print("2. Write the smallest possible failing test:")
print("   - Name it: test_<feature>_<scenario>()")
print("   - Assert the expected output/state")
print("   - Run it: confirm it FAILS (red) before continuing")
print()
print("3. Commit the failing test:")
print("   git add -p && git commit -m 'test: [red] <feature>'")
print()
print("━━━ PHASE 2: GREEN (Make it pass — minimally) ━━━")
print()
print("4. Write the MINIMUM code to make the test pass:")
print("   - No extra features, no premature abstraction")
print("   - It's OK if the code is ugly — correctness first")
print("   - Run the test: confirm it PASSES (green)")
print()
print("5. Commit the passing implementation:")
print("   git add -p && git commit -m 'feat: [green] <feature>'")
print()
print("━━━ PHASE 3: REFACTOR (Improve without changing behaviour) ━━━")
print()
print("6. Identify code smells (duplication, unclear names, tight coupling)")
print("7. Refactor — run tests after EVERY change to stay green")
print("8. Commit the clean version:")
print("   git add -p && git commit -m 'refactor: <what you improved>'")
print()
print("━━━ CHECKLIST ━━━")
print()
print("  [ ] Test written BEFORE implementation")
print("  [ ] Test initially FAILS (red confirmed)")
print("  [ ] Implementation is MINIMAL (no gold-plating)")
print("  [ ] Test PASSES after implementation (green confirmed)")
print("  [ ] Code REFACTORED (clean, no duplication)")
print("  [ ] All existing tests still PASS")
print()
print("━━━ NEXT STEPS ━━━")
print()
print("  • Update NOTES.md with the test name and decision rationale")
print("  • If the feature is large, break it into smaller acceptance criteria")
print("    and run /tdd for each one separately")
print("  • When the full feature is done, run /progress to notify your parent")

# Notify parent agent that a TDD cycle has been scaffolded (best-effort)
try:
    try:
        from tmux_orchestrator.slash_notify import notify_parent
    except ImportError:
        notify_parent = None
    if notify_parent is not None:
        sent = notify_parent(
            event_type="tdd_cycle_started",
            extra={"feature": feature, "phase": "red", "agent_id": agent_id},
        )
        if sent:
            print(f"✓ Parent agent notified (tdd_cycle_started: {feature})")
except Exception:
    pass  # Notification is best-effort — never block TDD scaffolding
```

The TDD cycle produces verifiable, maintainable code. Each micro-cycle (5–15 min) should result
in a committed, passing test + implementation. Use `/plan` first for complex features.
