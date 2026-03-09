Report your current task progress to your parent agent in the hierarchy.

This implements the "child → parent" communication pattern. Progress reports are structured
to give the parent agent exactly the information it needs — no more, no less (context engineering
principle: minimum high-signal tokens).

Usage: `/progress <summary of what you've done and what's next>`

Execute this Python snippet:

```python
import json, os, os, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone

summary = """$ARGUMENTS""".strip()
if not summary:
    print("Usage: /progress <summary>")
    print("  Example: /progress 'Completed auth module tests (3/5 passing). Working on token refresh.'")
    raise SystemExit(1)

# Discover context file: per-agent file takes priority (safe for shared cwd).
_aid = os.environ.get("TMUX_ORCHESTRATOR_AGENT_ID", "")
ctx_path = Path(f"__orchestrator_context__{_aid}__.json") if _aid else None
if ctx_path is None or not ctx_path.exists():
    ctx_path = Path("__orchestrator_context__.json")
if not ctx_path.exists():
    print("Not in an orchestrated environment (__orchestrator_context__.json not found).")
    raise SystemExit(1)

ctx    = json.loads(ctx_path.read_text())
my_id  = ctx["agent_id"]
api    = ctx["web_base_url"].rstrip("/")

# Read API key securely: env var takes priority, then per-agent file, then legacy file
api_key = os.environ.get("TMUX_ORCHESTRATOR_API_KEY", "")
if not api_key:
    _aid2 = os.environ.get("TMUX_ORCHESTRATOR_AGENT_ID", "")
    per_agent_key = Path(f"__orchestrator_api_key__{_aid2}__") if _aid2 else None
    if per_agent_key and per_agent_key.exists():
        api_key = per_agent_key.read_text().strip()
    else:
        key_file = Path("__orchestrator_api_key__")
        if key_file.exists():
            api_key = key_file.read_text().strip()

_auth_headers = {"Content-Type": "application/json"}
if api_key:
    _auth_headers["X-API-Key"] = api_key

# Read NOTES.md for additional context if it exists
notes_snippet = ""
notes_path = Path("NOTES.md")
if notes_path.exists():
    notes_text = notes_path.read_text()
    # Extract the "Progress" section if present
    lines = notes_text.splitlines()
    in_progress = False
    progress_lines = []
    for line in lines:
        if line.startswith("## Progress"):
            in_progress = True
            continue
        elif line.startswith("## ") and in_progress:
            break
        elif in_progress:
            progress_lines.append(line)
    if progress_lines:
        notes_snippet = "\nFrom NOTES.md:\n" + "\n".join(progress_lines[:10])

# Read plan status if PLAN.md exists
plan_snippet = ""
plan_path = Path("PLAN.md")
if plan_path.exists():
    plan_text = plan_path.read_text()
    # Count checked vs unchecked items
    checked = plan_text.count("- [x]")
    unchecked = plan_text.count("- [ ]")
    total = checked + unchecked
    if total > 0:
        plan_snippet = f"\nPlan progress: {checked}/{total} subtasks complete"

now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
full_message = (
    f"[PROGRESS from {my_id}] {now}\n"
    f"{summary}"
    f"{plan_snippet}"
    f"{notes_snippet}"
)

# Find parent agent: look for the agent that registered us
# For now, send to the orchestrator which will route to the right parent
# (The orchestrator's hierarchy tracking knows our parent)
# We send to __orchestrator__ as a STATUS message via the bus
# — use REST /tasks or /agents/{id}/message to reach parent if known
# Try to find parent_id from list_agents
parent_id = None
try:
    req = urllib.request.Request(f"{api}/agents", headers=_auth_headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        agents = json.loads(resp.read())
    for a in agents:
        if a.get("id") == my_id:
            parent_id = a.get("parent_id")
            break
except Exception:
    pass

if parent_id:
    # Send directly to parent via POST /agents/{parent_id}/message
    target = parent_id
    target_url = f"{api}/agents/{parent_id}/message"
    body = json.dumps({
        "type": "PEER_MSG",
        "payload": {"text": full_message, "event": "progress_report", "from_id": my_id},
    }).encode()
    try:
        req = urllib.request.Request(target_url, data=body,
                                     headers=_auth_headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        print(f"✓ Progress reported to parent agent: {parent_id}")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}")
        raise SystemExit(1)
    except OSError as e:
        print(f"Connection error: {e}")
        raise SystemExit(1)
else:
    # No parent tracked — print locally for the orchestrator to capture
    print(f"✓ Progress summary (no parent agent registered):")
    print(full_message)

print()
print(f"Summary: {summary}")
if plan_snippet:
    print(plan_snippet)
```

The progress report is sent as a PEER_MSG to your parent agent, which then uses it to
coordinate work or report to the user via the Director pattern.
