Spawn a new sub-agent that works under your supervision. The orchestrator automatically grants bidirectional P2P messaging between you and the new agent.

Usage:
- `/spawn-subagent custom <shell command>` — runs an arbitrary script via JSON stdio
- `/spawn-subagent claude_code` — starts a new Claude Code instance in a tmux pane

Execute this Python snippet:

```python
import json, urllib.request, urllib.error
from pathlib import Path

args = """$ARGUMENTS""".strip()
if not args:
    print("Usage: /spawn-subagent <claude_code|custom> [command]")
    raise SystemExit(1)

parts      = args.split(None, 1)
agent_type = parts[0]
command    = parts[1] if len(parts) > 1 else ""

if agent_type not in ("claude_code", "custom"):
    print(f"agent_type must be 'claude_code' or 'custom', got: {agent_type!r}")
    raise SystemExit(1)
if agent_type == "custom" and not command:
    print("A 'custom' agent requires a command string, e.g.:")
    print("  /spawn-subagent custom python3 scripts/worker.py")
    raise SystemExit(1)

ctx    = json.loads(Path("__orchestrator_context__.json").read_text())
my_id  = ctx["agent_id"]
url    = f"{ctx['web_base_url'].rstrip('/')}/agents"

body = json.dumps({
    "parent_id":  my_id,
    "agent_type": agent_type,
    "command":    command,
}).encode()

req = urllib.request.Request(
    url, data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
        print(f"✓ Sub-agent spawn initiated")
        print(f"  Parent:     {my_id}")
        print(f"  Type:       {agent_type}")
        if command:
            print(f"  Command:    {command}")
        print()
        print("The orchestrator will send you a STATUS message once the agent is ready.")
        print("Run /check-inbox to retrieve it. The payload will contain:")
        print('  "event": "subagent_spawned"')
        print('  "sub_agent_id": "<new-agent-id>"')
        print()
        print("P2P messaging between you and the sub-agent is automatically enabled.")
except urllib.error.HTTPError as e:
    err = e.read().decode()
    print(f"HTTP {e.code} error: {err}")
    raise SystemExit(1)
except OSError as e:
    print(f"Connection failed — is the web server running at {ctx['web_base_url']}?")
    print(f"Error: {e}")
    raise SystemExit(1)
```

After the sub-agent starts, use `/send-message <sub_agent_id> <task>` to delegate work. The sub-agent runs in its own isolated git worktree by default.
