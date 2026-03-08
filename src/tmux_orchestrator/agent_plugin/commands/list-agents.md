List all registered agents and their current status (IDLE, BUSY, ERROR, STOPPED).

Execute this Python snippet:

```python
import json, os, urllib.request
from pathlib import Path

ctx      = json.loads(Path("__orchestrator_context__.json").read_text())
my_id    = ctx["agent_id"]
base_url = ctx["web_base_url"].rstrip("/")

# Read API key securely: env var takes priority, then __orchestrator_api_key__ file
api_key = os.environ.get("TMUX_ORCHESTRATOR_API_KEY", "")
if not api_key:
    key_file = Path("__orchestrator_api_key__")
    if key_file.exists():
        api_key = key_file.read_text().strip()

headers = {}
if api_key:
    headers["X-API-Key"] = api_key

try:
    req = urllib.request.Request(f"{base_url}/agents", headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        agents = json.loads(resp.read())
except OSError as e:
    print(f"Connection failed — is the web server running at {base_url}?")
    print(f"Error: {e}")
    raise SystemExit(1)

if not agents:
    print("No agents are currently registered.")
    raise SystemExit(0)

STATUS_ICON = {"IDLE": "●", "BUSY": "◑", "ERROR": "✗", "STOPPED": "○"}
print(f"{'AGENT ID':<32} {'STATUS':<8} {'CURRENT TASK'}")
print("─" * 65)
for a in agents:
    icon    = STATUS_ICON.get(a["status"], "?")
    status  = f"{icon} {a['status']}"
    task    = a.get("current_task") or "—"
    if task != "—":
        task = task[:8] + "…"   # show UUID prefix
    marker  = "  ← you" if a["id"] == my_id else ""
    print(f"{a['id']:<32} {status:<12} {task}{marker}")

print()
print(f"Total: {len(agents)} agent(s).  Your ID: {my_id}")
```

Status meanings:
- `● IDLE` — ready to receive tasks or messages
- `◑ BUSY` — currently executing a task
- `✗ ERROR` — encountered a failure during task execution
- `○ STOPPED` — agent has been shut down

To send a message to an IDLE agent, use `/send-message <agent_id> <text>`.
To spawn a helper agent under your supervision, use `/spawn-subagent`.
