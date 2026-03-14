Write a value to the shared scratchpad via the orchestrator REST API.

Usage: `/scratchpad-write <key> <value>`

The first word is the key. Everything after is the value (stored as a JSON string).
To store a non-string value, prefix the value with `json:` followed by valid JSON.

Examples:
  `/scratchpad-write result "hello from writer"`
  `/scratchpad-write score 42`
  `/scratchpad-write data json:{"status":"ok","count":3}`

Execute this Python snippet:

```python
import json, os, urllib.request, urllib.error
from pathlib import Path

args = """$ARGUMENTS""".strip()
if not args:
    print("Usage: /scratchpad-write <key> <value>")
    print("  Example: /scratchpad-write result 'hello from writer'")
    raise SystemExit(1)

parts = args.split(None, 1)
if len(parts) < 2:
    print("Usage: /scratchpad-write <key> <value>")
    print("  Both key and value are required.")
    raise SystemExit(1)

key = parts[0]
raw_value = parts[1]

# Parse value: if prefixed with "json:" treat remainder as raw JSON.
# Otherwise treat the entire string as a JSON value (auto-parse numbers
# and booleans), falling back to a plain string.
if raw_value.startswith("json:"):
    try:
        value = json.loads(raw_value[5:])
    except json.JSONDecodeError as e:
        print(f"Invalid JSON after 'json:' prefix: {e}")
        raise SystemExit(1)
else:
    # Try to parse as JSON first (handles numbers, booleans, null).
    # If that fails, treat as plain string — strip surrounding quotes if present.
    stripped = raw_value.strip('"').strip("'")
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError:
        value = stripped

# Discover context file: per-agent file takes priority (safe for shared cwd).
_aid = os.environ.get("TMUX_ORCHESTRATOR_AGENT_ID", "")
ctx_path = Path(f"__orchestrator_context__{_aid}__.json") if _aid else None
if ctx_path is None or not ctx_path.exists():
    ctx_path = Path("__orchestrator_context__.json")
if not ctx_path.exists():
    print("Not in an orchestrated environment (__orchestrator_context__.json not found).")
    raise SystemExit(1)

ctx = json.loads(ctx_path.read_text())
api = ctx["web_base_url"].rstrip("/")
url = f"{api}/scratchpad/{key}"

# Read API key securely: env var takes priority, then per-agent file, then legacy file
api_key = os.environ.get("TMUX_ORCHESTRATOR_API_KEY", "")

headers = {"Content-Type": "application/json"}
if api_key:
    headers["X-API-Key"] = api_key

body = json.dumps({"value": value}).encode()

req = urllib.request.Request(url, data=body, headers=headers, method="PUT")
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
        if result.get("updated"):
            print(f"✓ Scratchpad key {key!r} written")
            print(f"  Value: {json.dumps(value)}")
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

**Note:** The value is persisted to `.orchestrator/scratchpad/{key}` on the server filesystem
and survives server restarts (v1.2.1+). Use `/scratchpad-read` to retrieve the value.
