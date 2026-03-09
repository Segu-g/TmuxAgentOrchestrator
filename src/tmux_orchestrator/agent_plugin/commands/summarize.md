Compress your current task state into NOTES.md to prevent context rot.

Context Engineering principle (Anthropic Engineering Blog, 2025): as context windows grow large,
recall degrades ('context rot'). Structured compaction — summarising key decisions and state into
an external file — preserves high-signal information while discarding redundant detail.

Run this when: your conversation is getting long, you're switching focus, or before reporting
results to your parent agent.

Usage: `/summarize`

Execute this Python snippet:

```python
import json, os
from pathlib import Path
from datetime import datetime, timezone

_aid = os.environ.get("TMUX_ORCHESTRATOR_AGENT_ID", "")
ctx_path = Path(f"__orchestrator_context__{_aid}__.json") if _aid else None
if ctx_path is None or not ctx_path.exists():
    ctx_path = Path("__orchestrator_context__.json")
agent_id = json.loads(ctx_path.read_text())["agent_id"] if ctx_path.exists() else "unknown"
now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

notes_path = Path("NOTES.md")
plan_path  = Path("PLAN.md")

# Read existing notes
existing = notes_path.read_text() if notes_path.exists() else ""
# Read plan for completed/pending items
plan_summary = ""
if plan_path.exists():
    plan_text = plan_path.read_text()
    checked   = plan_text.count("- [x]")
    unchecked = plan_text.count("- [ ]")
    plan_summary = f"Plan: {checked} done, {unchecked} remaining"

print(f"=== Context Summarization — {agent_id} ===")
print(f"Time: {now}")
print()
print("Fill in the sections below, then Claude will write them to NOTES.md.")
print("Keep each section to 3–5 bullet points maximum (high-signal, low-noise).")
print()
print("━━━ PROMPT FOR MANUAL COMPLETION ━━━")
print()
print("Please answer these questions concisely, then this command will update NOTES.md:")
print()
print("1. CURRENT TASK (one sentence):")
print("   What am I working on right now?")
print()
print("2. KEY DECISIONS (bullet points):")
print("   What architectural or design choices have I made and why?")
print()
print("3. PROGRESS (checklist):")
print("   What is done? What is still to do?")
print(f"   {plan_summary}")
print()
print("4. BLOCKERS (bullet points or 'None'):")
print("   What is preventing me from proceeding?")
print()
print("5. COMPLETED (bullet points):")
print("   What has been finished and can be removed from working memory?")
print()
print("━━━ NOTES.md TEMPLATE ━━━")
print()

template = f"""\
# Agent Notes — {agent_id}

*Last updated: {now}*

## Current Task

<!-- One sentence: what am I doing right now? -->
_Replace this with your current focus._

## Key Decisions

<!-- Keep to 3–5 bullets. Record WHY, not just what. -->
- Decision: _<what>_ — Rationale: _<why>_

## Progress

<!-- Compact checklist. Move completed items to 'Completed' section. -->
- [ ] _next step_
- [ ] _next step_

## Blockers

_None._

## Completed

<!-- Move done items here to free context, don't delete — they may matter for retrospectives. -->
- _completed item_
"""

# Preserve any existing "Completed" section content
completed_section = ""
if existing:
    in_completed = False
    completed_lines = []
    for line in existing.splitlines():
        if line.startswith("## Completed"):
            in_completed = True
            continue
        elif line.startswith("## ") and in_completed:
            break
        elif in_completed and line.strip() and not line.startswith("_"):
            completed_lines.append(line)
    if completed_lines:
        completed_section = "\n".join(completed_lines)
        print(f"Preserved from existing NOTES.md — Completed section:")
        print(completed_section)
        print()

notes_path.write_text(template)
print(f"✓ NOTES.md template written. Edit it now to fill in your current state.")
print()
print("Tip: After filling in NOTES.md, your context window can be cleared.")
print("     The next task or conversation turn will load NOTES.md as fresh context.")
```

After running `/summarize`, manually edit NOTES.md with your current state. This creates a
'checkpoint' you can restore from in a new conversation turn.
