#!/usr/bin/env python3
"""Simple echo agent for testing.

Reads newline-delimited JSON task lines from stdin and writes result lines
back to stdout — matching the CustomAgent stdio protocol.

Input:  {"task_id": "…", "prompt": "…"}
Output: {"task_id": "…", "result": "Echo: <prompt>"}
"""

import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        task = json.loads(line)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        continue

    result = {"task_id": task.get("task_id", ""), "result": f"Echo: {task.get('prompt', '')}"}
    print(json.dumps(result), flush=True)
