Request a dynamic strategy change for your current task phase. When you determine that working alone is insufficient, use this command to escalate to parallel or competitive execution.

Usage:
- `/change-strategy parallel:<count>` — spawn N parallel workers that each tackle the same task; results are returned to you via mailbox
- `/change-strategy parallel:<count> <context>` — spawn workers with a specific task description
- `/change-strategy competitive:<count>` — same as parallel, but agents compete; you pick the best result
- `/change-strategy single` — no-op; continue alone (default)

Examples:
- `/change-strategy parallel:3` — spawn 3 workers using your current task as context
- `/change-strategy parallel:2 "Implement a binary search tree in Python with insert/search/delete"` — spawn 2 workers with explicit context
- `/change-strategy competitive:3` — 3 agents compete on the same problem

Execute this Python snippet:

```python
import json, os, os, shlex, urllib.request, urllib.error
from pathlib import Path

raw_args = """$ARGUMENTS""".strip()

# Parse: "parallel:2 optional context text" or "single"
parts = raw_args.split(None, 1)
if not parts:
    print("Usage: /change-strategy <pattern>[:<count>] [context]")
    print("  patterns: parallel, competitive, single")
    print("  example:  /change-strategy parallel:3 Implement a binary search tree")
    raise SystemExit(1)

pattern_part = parts[0]
extra_context = parts[1] if len(parts) > 1 else None

# Parse pattern:count
if ":" in pattern_part:
    pattern, count_str = pattern_part.split(":", 1)
    try:
        count = int(count_str)
    except ValueError:
        print(f"Invalid count in {pattern_part!r}. Use format: parallel:3")
        raise SystemExit(1)
else:
    pattern = pattern_part
    count = 2

# Validate pattern
valid_patterns = {"single", "parallel", "competitive"}
if pattern not in valid_patterns:
    print(f"Unknown pattern {pattern!r}. Valid: {sorted(valid_patterns)}")
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
url    = f"{ctx['web_base_url'].rstrip('/')}/agents/{my_id}/change-strategy"

# Read API key
api_key = os.environ.get("TMUX_ORCHESTRATOR_API_KEY", "")

headers = {"Content-Type": "application/json"}
if api_key:
    headers["X-API-Key"] = api_key

payload = {
    "pattern": pattern,
    "count": count,
    "reply_to": my_id,
}
if extra_context:
    payload["context"] = extra_context

body = json.dumps(payload).encode()

req = urllib.request.Request(url, data=body, headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
        print(f"✓ Strategy change accepted")
        print(f"  Agent:    {my_id}")
        print(f"  Pattern:  {result['pattern']}")
        print(f"  Count:    {result.get('count', 1)}")
        spawned = result.get("spawned_task_ids", [])
        if spawned:
            print(f"  Spawned tasks ({len(spawned)}):")
            for tid in spawned:
                print(f"    - {tid}")
            print()
            print(f"Workers will deliver results to your mailbox when done.")
            print(f"Run /check-inbox to receive results as they arrive.")
        else:
            print()
            print("Strategy preference recorded. No tasks spawned (no context provided).")
            print("To spawn workers, include a context: /change-strategy parallel:2 <task>")
except urllib.error.HTTPError as e:
    err = e.read().decode()
    print(f"HTTP {e.code} error: {err}")
    raise SystemExit(1)
except OSError as e:
    print(f"Connection failed — is the web server running at {ctx['web_base_url']}?")
    print(f"Error: {e}")
    raise SystemExit(1)
```

After spawning workers, monitor your mailbox with `/check-inbox` to collect results as they arrive. Use the scratchpad (`GET {web_base_url}/scratchpad/`) to share large artifacts between workers and yourself.

Design reference: §12「ワークフロー設計の層構造」層3 実行方式の自律切り替え
