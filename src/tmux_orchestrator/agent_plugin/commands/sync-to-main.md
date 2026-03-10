Sync your isolated worktree branch back into the main branch via the orchestrator.

This command calls `POST /agents/{your_id}/sync` with `strategy=merge` and
`target_branch=master`.  It is only meaningful for agents with `isolate: true`
(those running in a dedicated `worktree/{agent_id}` branch).

Usage: `/sync-to-main [target_branch]`

- With no argument: syncs into `master`.
- With an argument: syncs into the specified branch (e.g. `/sync-to-main develop`).

Execute this Python snippet:

```python
import json, os, urllib.request, urllib.error, sys
from pathlib import Path

# Parse optional target branch from arguments
_args = """$ARGUMENTS""".strip().split()
target_branch = _args[0] if _args else "master"

# Discover context file
_aid = os.environ.get("TMUX_ORCHESTRATOR_AGENT_ID", "")
ctx_path = Path(f"__orchestrator_context__{_aid}__.json") if _aid else None
if ctx_path is None or not ctx_path.exists():
    ctx_path = Path("__orchestrator_context__.json")
if not ctx_path.exists():
    print("Not in an orchestrated environment (__orchestrator_context__.json not found).")
    raise SystemExit(1)

ctx = json.loads(ctx_path.read_text())
agent_id = ctx["agent_id"]
api = ctx["web_base_url"].rstrip("/")
url = f"{api}/agents/{agent_id}/sync"

# Read API key
api_key = os.environ.get("TMUX_ORCHESTRATOR_API_KEY", "")
if not api_key:
    _aid2 = os.environ.get("TMUX_ORCHESTRATOR_AGENT_ID", "")
    per_agent_key = Path(f"__orchestrator_api_key__{_aid2}__") if _aid2 else None
    if per_agent_key and per_agent_key.exists():
        api_key = per_agent_key.read_text().strip()
    else:
        kf = Path("__orchestrator_api_key__")
        if kf.exists():
            api_key = kf.read_text().strip()

headers = {"Content-Type": "application/json"}
if api_key:
    headers["X-API-Key"] = api_key

body = json.dumps({
    "strategy": "merge",
    "target_branch": target_branch,
    "message": "",
}).encode()

print(f"Syncing worktree/{agent_id} → {target_branch} via merge…")
req = urllib.request.Request(url, data=body, headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
        commits = result.get("commits_synced", 0)
        sha = result.get("merge_commit") or "n/a"
        src = result.get("source_branch", f"worktree/{agent_id}")
        tgt = result.get("target_branch", target_branch)
        if commits == 0:
            print(f"✓ Nothing to sync — {src} is already up to date with {tgt}.")
        else:
            print(f"✓ Synced {commits} commit(s) from {src} into {tgt}")
            print(f"  Merge commit: {sha}")
except urllib.error.HTTPError as e:
    err = e.read().decode()
    if e.code == 400:
        print(f"✗ Cannot sync: {err}")
        print("  (Only isolate=true agents with a worktree branch can sync)")
    elif e.code == 409:
        print(f"✗ Merge conflict or git error: {err}")
        print("  Resolve the conflict manually and retry.")
    else:
        print(f"HTTP {e.code} error: {err}")
    raise SystemExit(1)
except OSError as e:
    print(f"Connection failed — is the web server running at {api}?")
    print(f"Error: {e}")
    raise SystemExit(1)
```

**When to use this command:**
- After completing your task, before calling `/task-complete`, when you want your
  branch changes to land in the shared `master` branch.
- Call this only when all your changes are committed in the worktree.
- After a successful sync, your commits are visible on `master` to all other agents.

**Note:** This leaves your worktree branch intact — it is not deleted by this command.
The orchestrator will delete the worktree when the agent stops (standard lifecycle).
