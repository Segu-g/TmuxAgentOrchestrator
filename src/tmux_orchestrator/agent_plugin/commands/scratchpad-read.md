Read a value from the shared scratchpad via the orchestrator REST API.

Usage: `/scratchpad-read <key>`

Prints the stored value for the given key, or a 404 error if the key does not exist.
Use `/scratchpad-read` with no arguments to list all keys.

Examples:
  `/scratchpad-read result`
  `/scratchpad-read score`
  `/scratchpad-read`   (lists all keys)

Execute this Python snippet:

```python
import json, os, urllib.request, urllib.error
from pathlib import Path

key = """$ARGUMENTS""".strip()

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

headers = {}
if api_key:
    headers["X-API-Key"] = api_key

if not key:
    # List all keys
    url = f"{api}/scratchpad/"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if not result:
                print("Scratchpad is empty.")
            else:
                print(f"Scratchpad keys ({len(result)}):")
                for k, v in sorted(result.items()):
                    v_preview = json.dumps(v)
                    if len(v_preview) > 80:
                        v_preview = v_preview[:77] + "..."
                    print(f"  {k}: {v_preview}")
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"HTTP {e.code} error: {err}")
        raise SystemExit(1)
    except OSError as e:
        print(f"Connection failed — is the web server running at {api}?")
        print(f"Error: {e}")
        raise SystemExit(1)
else:
    # Read specific key
    url = f"{api}/scratchpad/{key}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            value = result.get("value")
            print(f"Scratchpad[{key!r}] = {json.dumps(value)}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"Key {key!r} not found in scratchpad.")
        else:
            err = e.read().decode()
            print(f"HTTP {e.code} error: {err}")
        raise SystemExit(1)
    except OSError as e:
        print(f"Connection failed — is the web server running at {api}?")
        print(f"Error: {e}")
        raise SystemExit(1)
```

**Note:** Values written via `/scratchpad-write` are persisted to `.orchestrator/scratchpad/{key}`
on the server filesystem and survive server restarts (v1.2.1+).
