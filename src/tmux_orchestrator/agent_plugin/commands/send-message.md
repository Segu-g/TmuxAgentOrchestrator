Send a PEER_MSG to another agent via the orchestrator's REST API.

Usage: `/send-message <target_agent_id> <message text>`

The first word is the target agent's ID. Everything after is the message body.

Execute this Python snippet:

```python
import json, os, urllib.request, urllib.error
from pathlib import Path

args = """$ARGUMENTS""".strip()
if not args:
    print("Usage: /send-message <agent_id> <message text>")
    raise SystemExit(1)

parts = args.split(None, 1)
target_id    = parts[0]
message_text = parts[1] if len(parts) > 1 else ""

ctx    = json.loads(Path("__orchestrator_context__.json").read_text())
my_id  = ctx["agent_id"]
url    = f"{ctx['web_base_url'].rstrip('/')}/agents/{target_id}/message"

# Read API key securely: env var takes priority, then __orchestrator_api_key__ file
api_key = os.environ.get("TMUX_ORCHESTRATOR_API_KEY", "")
if not api_key:
    key_file = Path("__orchestrator_api_key__")
    if key_file.exists():
        api_key = key_file.read_text().strip()

headers = {"Content-Type": "application/json"}
if api_key:
    headers["X-API-Key"] = api_key

body = json.dumps({
    "type": "PEER_MSG",
    "payload": {
        "from_display": my_id,
        "text": message_text,
    },
}).encode()

req = urllib.request.Request(
    url, data=body,
    headers=headers,
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
        print(f"✓ Message sent (ID: {result.get('message_id', '?')})")
        print(f"  From:    {my_id}")
        print(f"  To:      {target_id}")
        print(f"  Content: {message_text}")
except urllib.error.HTTPError as e:
    err = e.read().decode()
    print(f"HTTP {e.code} error: {err}")
    raise SystemExit(1)
except OSError as e:
    print(f"Connection failed — is the web server running at {ctx['web_base_url']}?")
    print(f"Error: {e}")
    raise SystemExit(1)
```

**Important:** P2P messaging requires an explicit permission entry in the orchestrator's `p2p_permissions` config (or the pair was granted via sub-agent spawning). Messages between unpermitted pairs are silently dropped by the orchestrator. Use `/list-agents` to verify the target agent exists and is IDLE or BUSY.
