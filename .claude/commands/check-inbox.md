List all unread messages waiting in your mailbox inbox. Run this whenever you receive a `__MSG__:...` notification or simply want to check for pending messages.

Read `__orchestrator_context__.json` in your current working directory, then execute this Python snippet to list your unread messages:

```python
import json
from pathlib import Path

ctx_path = Path("__orchestrator_context__.json")
if not ctx_path.exists():
    print("Not running in an orchestrated environment (__orchestrator_context__.json not found).")
    raise SystemExit(0)

ctx = json.loads(ctx_path.read_text())
agent_id = ctx["agent_id"]
session  = ctx["session_name"]
inbox    = Path(ctx["mailbox_dir"]).expanduser() / session / agent_id / "inbox"

if not inbox.exists() or not list(inbox.glob("*.json")):
    print(f"Inbox is empty — no unread messages for agent '{agent_id}'.")
    raise SystemExit(0)

msgs = sorted(inbox.glob("*.json"))
print(f"{len(msgs)} unread message(s) for '{agent_id}':\n")
for p in msgs:
    d = json.loads(p.read_text())
    ts = d.get("timestamp", "")[:19].replace("T", " ")
    print(f"  ID:      {p.stem}")
    print(f"  From:    {d.get('from_id', '?')}")
    print(f"  Type:    {d.get('type', '?')}")
    print(f"  At:      {ts}")
    payload = d.get("payload", {})
    preview = str(payload)[:100]
    print(f"  Payload: {preview}")
    print()
```

To read a specific message in full and mark it as read, use `/read-message <msg_id>`.
