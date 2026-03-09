Spawn a new sub-agent that works under your supervision. The orchestrator automatically grants bidirectional P2P messaging between you and the new agent.

Usage:
- `/spawn-subagent <template_id>` — starts a new agent based on a pre-configured template from the YAML config

`template_id` must match an agent `id` defined in the orchestrator's config file (e.g., `worker-1`, `worker-2`).

Execute this Python snippet:

```python
import json, os, os, urllib.request, urllib.error
from pathlib import Path

template_id = """$ARGUMENTS""".strip()
if not template_id:
    print("Usage: /spawn-subagent <template_id>")
    print("  template_id must be an agent id defined in the orchestrator config, e.g.:")
    print("    /spawn-subagent worker-1")
    raise SystemExit(1)

# Discover context file: per-agent file takes priority (safe for shared cwd).
_aid = os.environ.get("TMUX_ORCHESTRATOR_AGENT_ID", "")
_ctx_path = Path(f"__orchestrator_context__{_aid}__.json") if _aid else None
if _ctx_path is None or not _ctx_path.exists():
    _aid = os.environ.get("TMUX_ORCHESTRATOR_AGENT_ID", "")
    _ctx_path = Path(f"__orchestrator_context__{_aid}__.json") if _aid else None
    if _ctx_path is None or not _ctx_path.exists():
        _ctx_path = Path("__orchestrator_context__.json")
ctx    = json.loads(_ctx_path.read_text())
my_id  = ctx["agent_id"]
url    = f"{ctx['web_base_url'].rstrip('/')}/agents"

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

headers = {"Content-Type": "application/json"}
if api_key:
    headers["X-API-Key"] = api_key

body = json.dumps({
    "parent_id":   my_id,
    "template_id": template_id,
}).encode()

req = urllib.request.Request(
    url, data=body,
    headers=headers,
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
        print(f"✓ Sub-agent spawn initiated")
        print(f"  Parent:      {my_id}")
        print(f"  Template:    {template_id}")
        print()
        print("The orchestrator will send you a STATUS message once the agent is ready.")
        print("Run /check-inbox to retrieve it. The payload will contain:")
        print('  "event": "subagent_spawned"')
        print('  "sub_agent_id": "<new-agent-id>"')
        print()
        print("P2P messaging between you and the sub-agent is automatically enabled.")
        print("Use /send-message <sub_agent_id> <task> to delegate work.")
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
