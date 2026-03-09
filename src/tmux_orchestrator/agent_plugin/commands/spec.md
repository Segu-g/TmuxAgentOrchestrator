Write a formal specification document (SPEC.md) for a feature or module.

The Spec-First pattern (ref: Vasilopoulos arXiv:2602.20478 "Codified Context" 2026;
Hou et al. 2025 "Trustworthy AI Requires Formal Methods") establishes an unambiguous
contract before any implementation begins. Specification documents reduce
misunderstandings between agents in multi-agent pipelines (spec-writer → implementer).

Usage: `/spec <feature or module description>`

Execute this Python snippet:

```python
import json, os
from pathlib import Path
from datetime import datetime, timezone

description = """$ARGUMENTS""".strip()
if not description:
    print("Usage: /spec <feature or module description>")
    print("  Writes a formal SPEC.md with preconditions, postconditions,")
    print("  invariants, type signatures, and edge cases.")
    raise SystemExit(1)

_aid = os.environ.get("TMUX_ORCHESTRATOR_AGENT_ID", "")
ctx_path = Path(f"__orchestrator_context__{_aid}__.json") if _aid else None
if ctx_path is None or not ctx_path.exists():
    ctx_path = Path("__orchestrator_context__.json")
agent_id = json.loads(ctx_path.read_text())["agent_id"] if ctx_path.exists() else "unknown"
now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

print(f"=== Spec: {description} ===")
print(f"Agent: {agent_id}  |  {now}")
print()
print("Write SPEC.md with these mandatory sections:")
print()
print("━━━ SPEC.md TEMPLATE ━━━")
print()
print("# Specification: <Feature Name>")
print()
print("## Context")
print("<Why this feature is needed; what problem it solves>")
print()
print("## Scope")
print("- IN SCOPE: ...")
print("- OUT OF SCOPE: ...")
print()
print("## Type Signatures")
print("```python")
print("def function_name(param: Type) -> ReturnType:")
print('    """One-line summary of what this does."""')
print("    ...")
print("```")
print()
print("## Preconditions")
print("- PRE-1: <condition that must hold BEFORE the function is called>")
print("- PRE-2: ...")
print()
print("## Postconditions")
print("- POST-1: <condition that must hold AFTER the function returns successfully>")
print("- POST-2: ...")
print()
print("## Invariants")
print("- INV-1: <property that must ALWAYS hold, regardless of inputs>")
print("- INV-2: ...")
print()
print("## Functional Requirements")
print("1. <FR-1> ...")
print("2. <FR-2> ...")
print()
print("## Acceptance Criteria")
print("- AC-1: Given <context>, when <action>, then <outcome>")
print("- AC-2: ...")
print()
print("## Edge Cases")
print("- EDGE-1: <unusual input> → <expected behaviour>")
print("- EDGE-2: ...")
print()
print("## Glossary")
print("- **Term**: definition")
print()
print("## References")
print("- ...")
print()
print("━━━ INSTRUCTIONS ━━━")
print()
print(f"Feature to specify: {description}")
print()
print("Steps:")
print("1. Think through all preconditions, postconditions, and invariants.")
print("2. Write SPEC.md in your working directory (NOT as a code block — as a file).")
print("3. Each acceptance criterion must be a verifiable, unambiguous statement.")
print("4. Glossary: define every domain-specific term used in the spec.")
print("5. Commit SPEC.md:")
print("   git add SPEC.md && git commit -m 'spec: <feature>'")
print()
print("━━━ CHECKLIST ━━━")
print()
print("  [ ] SPEC.md written to disk")
print("  [ ] Preconditions listed (what must be true before calling)")
print("  [ ] Postconditions listed (what is true after successful return)")
print("  [ ] Invariants listed (properties that always hold)")
print("  [ ] Acceptance criteria numbered and testable")
print("  [ ] Edge cases enumerated")
print("  [ ] Glossary covers all domain terms")
print("  [ ] SPEC.md committed to git")
print()
print("━━━ NEXT STEPS ━━━")
print()
print("  • Share SPEC.md with the implementer: store it in the scratchpad")
print("    or use context_files in the next task.")
print("  • Run /tdd to drive implementation from acceptance criteria.")
print("  • Once implementation is complete, run /progress to notify your parent.")

# Notify parent agent (best-effort)
try:
    try:
        from tmux_orchestrator.slash_notify import notify_parent
    except ImportError:
        notify_parent = None
    if notify_parent is not None:
        sent = notify_parent(
            event_type="spec_started",
            extra={"description": description, "agent_id": agent_id},
        )
        if sent:
            print(f"\u2713 Parent agent notified (spec_started: {description})")
except Exception:
    pass  # Notification is best-effort — never block spec scaffolding
```

A good specification is the contract between agents. The implementer must be able to
produce a correct implementation from SPEC.md alone, without consulting the spec-writer.
Use `/plan` first if the feature scope is unclear.
