Design a multi-phase workflow for a task and submit it to the orchestrator via `POST /workflows`.

This command implements the Planner Agent pattern (§12 自律モード):
1. You (the Planner) analyse the task description.
2. You design a `phases` JSON array using the `/plan-workflow` schema.
3. The command submits the workflow to `POST /workflows` and returns the workflow ID.

**Usage**: `/plan-workflow <task description>`

Execute this Python snippet:

```python
import json, os, sys, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone

description = """$ARGUMENTS""".strip()
if not description:
    print("Usage: /plan-workflow <task description>")
    print("  Designs a multi-phase workflow for the given task and submits it via POST /workflows.")
    print()
    print("Example:")
    print("  /plan-workflow Build a Python async priority queue with tests")
    raise SystemExit(1)

ctx_path = Path("__orchestrator_context__.json")
if not ctx_path.exists():
    print("ERROR: Not in an orchestrated environment (__orchestrator_context__.json not found).")
    raise SystemExit(1)

ctx   = json.loads(ctx_path.read_text())
my_id = ctx["agent_id"]
api   = ctx["web_base_url"].rstrip("/")

api_key = os.environ.get("TMUX_ORCHESTRATOR_API_KEY", "")
if not api_key:
    key_file = Path("__orchestrator_api_key__")
    if key_file.exists():
        api_key = key_file.read_text().strip()

headers = {"Content-Type": "application/json"}
if api_key:
    headers["X-API-Key"] = api_key

now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

print("=== /plan-workflow ===")
print(f"Task: {description}")
print(f"Planner: {my_id}")
print(f"Time: {now}")
print()
print("━━━ STEP 1: DESIGN PHASES ━━━")
print()
print("Reading planner role template…")
role_path = Path(".claude/prompts/roles/planner.md")
if not role_path.exists():
    # Try relative to project root via __file__ or common paths
    for candidate in [
        Path("/home") / os.environ.get("USER", "user") / "Projects" / "TmuxAgentOrchestrator" / ".claude" / "prompts" / "roles" / "planner.md",
    ]:
        if candidate.exists():
            role_path = candidate
            break

if role_path.exists():
    print(f"Planner role: {role_path}")
    print()
    print("Your task is to design a `phases` JSON array for:")
    print(f"  '{description}'")
    print()
    print("Refer to your planner.md role template for instructions.")
    print("Output format: a JSON object with 'name', 'context', and 'phases' fields.")
else:
    print("Note: planner.md not found; proceeding without role template.")

print()
print("━━━ PHASE DESIGN PROMPT ━━━")
print()
print("Design the workflow phases now. Use this JSON schema:")
print()
print(json.dumps({
    "name": "<workflow name>",
    "context": description,
    "phases": [
        {
            "name": "<phase name>",
            "pattern": "single|parallel|competitive|debate",
            "agents": {"tags": ["<relevant-tag>"], "count": 1},
            "context": "<optional phase-specific instruction>"
        }
    ]
}, indent=2))
print()
print("Patterns:")
print("  single      — one agent, focused deliverable")
print("  parallel    — N agents, independent tasks, broad coverage")
print("  competitive — N agents solve same problem; best wins")
print("  debate      — advocate + critic + judge (design decisions)")
print()
print("After designing phases, save to WORKFLOW_PLAN.json in your working directory,")
print("then re-run /plan-workflow with --submit to submit it:")
print()
print("  /plan-workflow --submit")
print()
print("Or use the REST API directly:")
print(f"  curl -X POST {api}/workflows \\")
print(f"       -H 'X-API-Key: $TMUX_ORCHESTRATOR_API_KEY' \\")
print(f"       -H 'Content-Type: application/json' \\")
print(f"       -d @WORKFLOW_PLAN.json")

# Check if --submit was requested (description == "--submit")
if description.strip() == "--submit":
    plan_file = Path("WORKFLOW_PLAN.json")
    if not plan_file.exists():
        print()
        print("ERROR: WORKFLOW_PLAN.json not found.")
        print("First run /plan-workflow <description> to design the phases,")
        print("save to WORKFLOW_PLAN.json, then run /plan-workflow --submit.")
        raise SystemExit(1)

    plan = json.loads(plan_file.read_text())
    print()
    print("━━━ STEP 2: SUBMITTING WORKFLOW ━━━")
    print()
    print(f"Submitting: {plan.get('name', 'workflow')}")
    print(f"Phases: {len(plan.get('phases', []))}")
    print()

    payload = json.dumps(plan).encode()
    req = urllib.request.Request(
        f"{api}/workflows",
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        print("Workflow submitted successfully!")
        print(f"  workflow_id: {result['workflow_id']}")
        print(f"  name: {result['name']}")
        print(f"  tasks: {len(result.get('task_ids', {}))}")
        if "phases" in result:
            print(f"  phases: {len(result['phases'])}")
            for p in result["phases"]:
                print(f"    - {p['name']} ({p['pattern']}): {len(p['task_ids'])} task(s)")
        print()
        print(f"Monitor via: GET {api}/workflows/{result['workflow_id']}")
        # Optionally write result to WORKFLOW_SUBMITTED.json
        Path("WORKFLOW_SUBMITTED.json").write_text(json.dumps(result, indent=2))
        print("Result saved to WORKFLOW_SUBMITTED.json")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"ERROR: HTTP {e.code} — {body}")
        raise SystemExit(1)
```

After running `/plan-workflow <description>`:
1. Design the phases JSON and save to `WORKFLOW_PLAN.json`.
2. Run `/plan-workflow --submit` to submit the workflow.
3. The workflow ID is saved to `WORKFLOW_SUBMITTED.json`.
4. Monitor the workflow with `GET /workflows/{workflow_id}`.
