Read an out-of-band context brief sent to you by the orchestrator or another agent via `POST /agents/{id}/brief`. The brief file is located at `__brief__/{brief_id}.txt` in your worktree.

Usage: `/read-brief <brief_id>`

When you receive `__BRIEF__:<brief_id>` in your pane, run this command to read the brief content and incorporate it into your context.

Execute this Python snippet (replace BRIEF_ID with `$ARGUMENTS` or the actual ID):

```python
from pathlib import Path

brief_id = """$ARGUMENTS""".strip()
if not brief_id:
    print("Usage: /read-brief <brief_id>")
    print("You receive brief_id in the __BRIEF__:<brief_id> notification.")
    raise SystemExit(1)

brief_file = Path("__brief__") / f"{brief_id}.txt"

if not brief_file.exists():
    # Try relative to the worktree path from the context file
    try:
        import json
        ctx = json.loads(Path("__orchestrator_context__.json").read_text())
        worktree = ctx.get("worktree_path")
        if worktree:
            brief_file = Path(worktree) / "__brief__" / f"{brief_id}.txt"
    except Exception:
        pass

if not brief_file.exists():
    print(f"Brief not found: {brief_id!r}")
    print(f"Checked: {Path('__brief__') / f'{brief_id}.txt'}")
    raise SystemExit(1)

content = brief_file.read_text(encoding="utf-8")
print(f"[Brief {brief_id}]")
print("=" * 60)
print(content)
print("=" * 60)
print()
print("Read the above brief content carefully and incorporate it into your current task context.")
print("You may need to adjust your approach based on this new information.")
```

The brief content is important out-of-band context injected while you are working on a task. After reading it, continue your work with this additional information in mind.
