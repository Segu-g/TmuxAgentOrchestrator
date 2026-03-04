Break a task into subtasks and spawn sub-agents to work on them in parallel.

This implements the "cooperative specialization" pattern (Anthropic Context Engineering, 2025):
sub-agents handle focused tasks and return condensed summaries, isolating detailed work context
from the coordinator. Each sub-agent gets its own worktree and CLAUDE.md, so their context
remains independent and focused.

Usage: `/delegate <task description>`

Execute this Python snippet:

```python
import json, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone

task = """$ARGUMENTS""".strip()
if not task:
    print("Usage: /delegate <task description>")
    print("  Guides you through breaking a task into subtasks and spawning sub-agents.")
    raise SystemExit(1)

ctx_path = Path("__orchestrator_context__.json")
if not ctx_path.exists():
    print("Not in an orchestrated environment.")
    raise SystemExit(1)

ctx    = json.loads(ctx_path.read_text())
my_id  = ctx["agent_id"]
api    = ctx["web_base_url"].rstrip("/")
now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# Fetch available templates (registered agents in config)
try:
    req = urllib.request.Request(f"{api}/agents", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        agents = json.loads(resp.read())
    worker_templates = [a["id"] for a in agents if a.get("role") == "worker" and a.get("id") != my_id]
except Exception as e:
    worker_templates = []
    print(f"Note: Could not fetch agent list ({e}). Proceeding without template list.")

print(f"=== Delegation Planning — {my_id} ===")
print(f"Task: {task}")
print(f"Time: {now}")
print()
print("━━━ STEP 1: DECOMPOSE ━━━")
print()
print("Break the task into independently completable subtasks:")
print("  Good subtask: self-contained, has a clear deliverable, doesn't block others")
print("  Bad subtask:  requires output from another subtask to start")
print()
print("Example decomposition:")
print(f"  Task: '{task}'")
print("  → Subtask A: <specific, testable unit of work>")
print("  → Subtask B: <specific, testable unit of work>")
print("  → Subtask C: <integration / synthesis after A and B complete>")
print()
print("━━━ STEP 2: SPAWN SUB-AGENTS ━━━")
print()

if worker_templates:
    print("Available worker templates (from config):")
    for t in worker_templates:
        print(f"  /spawn-subagent {t}")
else:
    print("No worker templates found. Check that worker agents are defined in your config YAML.")

print()
print("For each subtask, run:")
print("  /spawn-subagent <template_id>")
print("  Then wait for STATUS message: /check-inbox")
print("  Then: /send-message <new_agent_id> <subtask description>")
print()
print("━━━ STEP 3: COORDINATE ━━━")
print()
print("After spawning:")
print("  1. Write a PLAN.md tracking which agent owns which subtask")
print("  2. Monitor via /list-agents (check IDLE/BUSY/ERROR)")
print("  3. Receive results as __MSG__ notifications → /check-inbox")
print("  4. When all subtasks complete, synthesize and /progress to your parent")
print()
print("━━━ DELEGATION CHECKLIST ━━━")
print()
print("  [ ] Task decomposed into independent subtasks")
print("  [ ] Each subtask has clear acceptance criteria")
print("  [ ] Sub-agents spawned (one per subtask, or reuse if available)")
print("  [ ] Each sub-agent given a clear, self-contained prompt")
print("  [ ] PLAN.md updated with agent↔subtask mapping")
print("  [ ] Result aggregation plan decided (how to synthesize outputs)")
print()
print("━━━ CONTEXT ISOLATION REMINDER ━━━")
print()
print("Each sub-agent has its own worktree and CLAUDE.md.")
print("Send only the context each sub-agent needs — not everything you know.")
print("This keeps their context windows small and focused (prevents context rot).")
```

After running `/delegate`, use `/spawn-subagent <template_id>` for each subtask, then
`/send-message <sub_agent_id> <focused task prompt>` to assign work.
