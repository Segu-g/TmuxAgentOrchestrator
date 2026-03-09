#!/usr/bin/env python3
"""UserPromptSubmit hook: inject task prompt or compressed context from file.

Handles two protocol triggers dispatched by TmuxAgentOrchestrator:

1. **Task dispatch** (``__TASK__`` trigger):
   - Server writes full prompt to ``__task_prompt__<agent_id>__.txt``.
   - Hook reads file, deletes it (consume-once), returns prompt as
     ``additionalContext``.

2. **Context compression** (``__COMPRESS_CONTEXT__`` trigger, v1.1.13):
   - Server (ContextMonitor) writes TF-IDF compressed pane text to
     ``__compress_context__<agent_id>__.txt`` after auto-compression fires.
   - Hook reads file, deletes it (consume-once), returns the compressed
     context summary as ``additionalContext`` so Claude can use it as a
     concise context refresh.

In both cases, the pattern avoids tmux paste-preview mode (only the short
trigger is sent via send_keys; the full payload stays in a file).

If no matching trigger file is found, the hook exits 0 without output and
the original prompt passes through unchanged.

References:
- DESIGN.md §10.38 (v1.1.2 — UserPromptSubmit hook for prompt injection)
- DESIGN.md §10.38 (v1.1.13 — __COMPRESS_CONTEXT__ file delivery pattern)
- ACON arXiv:2510.00615 (Kang et al. 2025): threshold-based context compression.
- Claude Code hooks reference: https://code.claude.com/docs/en/hooks
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

# Protocol trigger strings — must match constants in claude_code.py / context_monitor.py.
_TASK_TRIGGER = "__TASK__"
_COMPRESS_TRIGGER = "__COMPRESS_CONTEXT__"


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    cwd = Path(data.get("cwd", "."))
    prompt = data.get("prompt", "")

    # ── Dispatch: task prompt delivery ──────────────────────────────────────
    if _TASK_TRIGGER in prompt:
        # Find __task_prompt__*.txt files in cwd — agent_id namespaced to avoid
        # collisions when multiple non-isolated agents share the same directory.
        pattern = str(cwd / "__task_prompt__*.txt")
        matches = sorted(glob.glob(pattern))

        if matches:
            prompt_file = Path(matches[0])
            try:
                task_prompt = prompt_file.read_text(encoding="utf-8")
                prompt_file.unlink()  # consume-once: delete after reading
            except OSError:
                # File disappeared between glob and read (race) — pass-through
                sys.exit(0)

            _emit_additional_context(task_prompt)
            return

    # ── Dispatch: context compression delivery ───────────────────────────────
    if _COMPRESS_TRIGGER in prompt:
        # Find __compress_context__*.txt files in cwd (agent_id namespaced).
        pattern = str(cwd / "__compress_context__*.txt")
        matches = sorted(glob.glob(pattern))

        if matches:
            compress_file = Path(matches[0])
            try:
                compressed_text = compress_file.read_text(encoding="utf-8")
                compress_file.unlink()  # consume-once: delete after reading
            except OSError:
                # File disappeared between glob and read (race) — pass-through
                sys.exit(0)

            # Wrap the compressed text in a clear framing so Claude understands
            # this is a context summary, not a new task instruction.
            framed = (
                "## Context Summary (auto-compressed by TmuxAgentOrchestrator)\n\n"
                "Your context window was approaching its limit. The following is a "
                "TF-IDF extractive summary of recent pane output. Use it as a "
                "concise reference — your original task is unchanged.\n\n"
                f"{compressed_text}"
            )
            _emit_additional_context(framed)
            return

    # No trigger matched — pass-through (human prompt or unknown trigger)
    sys.exit(0)


def _emit_additional_context(text: str) -> None:
    """Print a UserPromptSubmit additionalContext JSON response and exit 0."""
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }
    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
