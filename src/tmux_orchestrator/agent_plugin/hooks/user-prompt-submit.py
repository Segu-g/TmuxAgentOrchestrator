#!/usr/bin/env python3
"""UserPromptSubmit hook: inject task prompt from __task_prompt__<agent_id>__.txt.

When TmuxAgentOrchestrator dispatches a task, it:
1. Writes the full prompt to ``__task_prompt__<agent_id>__.txt`` in the agent's cwd.
2. Sends a short trigger string (``__TASK__``) via send_keys — no paste-preview risk.

This hook fires when Claude processes the ``__TASK__`` trigger. It reads the prompt
file, deletes it (consume-once), and outputs the actual task as ``additionalContext``
so Claude receives both the trigger and the full task content.

If no prompt file is found (e.g. a human-typed prompt), the hook exits 0 without
output and the original prompt passes through unchanged.

Reference: DESIGN.md §10.38 (v1.1.2 — UserPromptSubmit hook for prompt injection)
Claude Code hooks reference: https://code.claude.com/docs/en/hooks
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    cwd = Path(data.get("cwd", "."))

    # Find __task_prompt__*.txt files in cwd — agent_id namespaced to avoid
    # collisions when multiple non-isolated agents share the same directory.
    pattern = str(cwd / "__task_prompt__*.txt")
    matches = sorted(glob.glob(pattern))

    if not matches:
        # No task prompt file — pass-through (human prompt or no task dispatch)
        sys.exit(0)

    prompt_file = Path(matches[0])
    try:
        task_prompt = prompt_file.read_text(encoding="utf-8")
        prompt_file.unlink()  # consume-once: delete after reading
    except OSError:
        # File disappeared between glob and read (race) — ignore, pass-through
        sys.exit(0)

    # Output additionalContext so Claude receives the actual task prompt
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": task_prompt,
        }
    }
    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
