Signal to the orchestrator that your current task is fully complete.

Call this command ONLY ONCE, after all files are committed, tests pass, and all work
for the current task is finished. Do NOT call it mid-task or before your work is done.

Usage: `/task-complete <one-line summary of what was accomplished>`

Execute this Python snippet:

```python
import json, os, urllib.request, urllib.error
from pathlib import Path

summary = """$ARGUMENTS""".strip()
if not summary:
    print("Usage: /task-complete <summary of what was accomplished>")
    print("  Example: /task-complete 'Implemented auth module with 12 passing tests'")
    raise SystemExit(1)

ctx_path = Path("__orchestrator_context__.json")
if not ctx_path.exists():
    print("Not in an orchestrated environment (__orchestrator_context__.json not found).")
    raise SystemExit(1)

ctx      = json.loads(ctx_path.read_text())
agent_id = ctx["agent_id"]
api      = ctx["web_base_url"].rstrip("/")
url      = f"{api}/agents/{agent_id}/task-complete"

# Read API key securely: env var takes priority, then __orchestrator_api_key__ file
api_key = os.environ.get("TMUX_ORCHESTRATOR_API_KEY", "")
if not api_key:
    key_file = Path("__orchestrator_api_key__")
    if key_file.exists():
        api_key = key_file.read_text().strip()

headers = {"Content-Type": "application/json"}
if api_key:
    headers["X-API-Key"] = api_key

body = json.dumps({"output": summary}).encode()

req = urllib.request.Request(url, data=body, headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
        status = result.get("status", "?")
        if status == "ok":
            print(f"✓ Task marked complete (agent: {agent_id})")
            print(f"  Summary: {summary}")
        elif status == "skipped":
            reason = result.get("reason", "unknown")
            print(f"⚠ Completion signal skipped (reason: {reason})")
            print("  The orchestrator did not mark the task done — check agent status.")
        else:
            print(f"Response: {result}")
except urllib.error.HTTPError as e:
    err = e.read().decode()
    print(f"HTTP {e.code} error: {err}")
    raise SystemExit(1)
except OSError as e:
    print(f"Connection failed — is the web server running at {api}?")
    print(f"Error: {e}")
    raise SystemExit(1)
```

**Important:** Call `/task-complete` only when ALL of the following are true:
- All required files have been written and committed
- All tests pass (if applicable)
- You have no remaining subtasks
- You have reported final results to your parent agent (if applicable)

After calling this command the orchestrator will mark you IDLE and you may receive a new task.
