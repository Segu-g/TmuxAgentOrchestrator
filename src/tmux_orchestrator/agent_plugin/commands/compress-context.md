# Compress Agent Context (TF-IDF)

Compress the current agent's pane context using TF-IDF relevance scoring.
Lines with low relevance to the current task are removed, reducing context
size while preserving semantically important content.

This command implements the extractive compression approach from
Liu et al. "Lost in the Middle" (TACL 2024): by removing low-relevance
content and optionally placing high-relevance lines first, the compressed
context mitigates the LLM's recency/primacy bias.

## Usage

```
/compress-context [query] [--drop N] [--reorder]
```

**Arguments:**
- `query` (optional) — reference text used to score line relevance. Typically
  the current task prompt. Lines most similar to this text are kept.
  If omitted, the compressor uses query-agnostic scoring.
- `--drop N` — drop the bottom N% of lines by relevance (0.0–0.99, default 0.40)
- `--reorder` — reorder surviving lines so highest-relevance lines appear first

## Implementation

Reads `__orchestrator_context__.json` to obtain `web_base_url` and `agent_id`,
then calls:

```
POST {web_base_url}/agents/{agent_id}/compress-context
{
  "query": "<current task description>",
  "drop_percentile": 0.40,
  "reorder": false
}
```

The endpoint returns compression statistics and the compressed text.
Write the result to `COMPRESSED_CONTEXT.md` for reference.

## Example

```python
import json
import os
from pathlib import Path

import httpx

ctx = json.loads(Path("__orchestrator_context__.json").read_text())
agent_id = ctx["agent_id"]
web_base_url = ctx["web_base_url"]

api_key = os.environ.get("TMUX_ORCHESTRATOR_API_KEY", "")
if not api_key:
    kf = Path("__orchestrator_api_key__")
    if kf.exists():
        api_key = kf.read_text().strip()

headers = {"Content-Type": "application/json"}
if api_key:
    headers["X-API-Key"] = api_key

# Describe the current task as the query
query = "implement fizzbuzz in Python using modulo"

resp = httpx.post(
    f"{web_base_url}/agents/{agent_id}/compress-context",
    headers=headers,
    json={
        "query": query,
        "drop_percentile": 0.40,
        "reorder": True,
    },
)
resp.raise_for_status()
data = resp.json()

print(f"Compression: {data['original_lines']} → {data['kept_lines']} lines "
      f"({data['dropped_lines']} dropped, ratio={data['compression_ratio']:.1%})")

# Optionally write compressed context to a file
Path("COMPRESSED_CONTEXT.md").write_text(
    f"# Compressed Context\n\n"
    f"Query: {query}\n"
    f"Lines: {data['kept_lines']}/{data['original_lines']} kept\n\n"
    f"```\n{data['compressed_text']}\n```\n"
)
print("Wrote COMPRESSED_CONTEXT.md")
```

## When to Use

- When context usage exceeds 70–80% (check `GET /agents/{id}/stats`).
- Before starting a new sub-task to prune context from previous subtasks.
- As an alternative to `/summarize` when you want to keep exact original
  lines rather than an LLM-generated summary.

## Stats

Check cumulative compression statistics:
```
GET {web_base_url}/agents/{agent_id}/compression-stats
```
