Read a specific message from your inbox by its ID, display the full content, and mark it as read (moves it from `inbox/` to `read/`).

Usage: `/read-message <msg_id>`

Execute this Python snippet (replace MSG_ID with `$ARGUMENTS` or the actual ID):

```python
import json, shutil
from pathlib import Path

msg_id = """$ARGUMENTS""".strip()
if not msg_id:
    print("Usage: /read-message <msg_id>")
    print("Run /check-inbox first to see available message IDs.")
    raise SystemExit(1)

ctx = json.loads(Path("__orchestrator_context__.json").read_text())
agent_id = ctx["agent_id"]
session  = ctx["session_name"]
base     = Path(ctx["mailbox_dir"]).expanduser() / session / agent_id
inbox    = base / "inbox"
read_dir = base / "read"
read_dir.mkdir(parents=True, exist_ok=True)

src = inbox / f"{msg_id}.json"
dst = read_dir / f"{msg_id}.json"

if src.exists():
    data = json.loads(src.read_text())
    shutil.move(str(src), str(dst))
    print(f"[Marked as read: {msg_id}]\n")
elif dst.exists():
    data = json.loads(dst.read_text())
    print(f"[Already read: {msg_id}]\n")
else:
    print(f"Message not found: {msg_id!r}")
    print(f"Checked: {src}")
    raise SystemExit(1)

print(json.dumps(data, indent=2))
print()
print("--- Payload ---")
payload = data.get("payload", {})
if isinstance(payload, dict):
    for k, v in payload.items():
        if not k.startswith("_"):
            print(f"  {k}: {v}")
else:
    print(payload)
```

The `payload` field contains the message content. The `from_id` field identifies the sender. After running this, the message moves to `read/` and will no longer appear in `/check-inbox`.
