# Workflow YAML Template Library

Self-contained YAML templates for every `POST /workflows/*` endpoint in TmuxAgentOrchestrator.

Each template documents the required and optional fields, the agent pipeline, scratchpad keys,
and output files — plus a ready-to-use `curl` example.

## Quick Start

1. Start the orchestrator web server:
   ```bash
   tmux-orchestrator web --config examples/basic_config.yaml --port 8000
   ```

2. Pick a template and submit it as JSON:
   ```bash
   # Using Python to POST from YAML:
   python - <<'EOF'
   import yaml, json, urllib.request
   with open("examples/workflows/tdd.yaml") as f:
       data = yaml.safe_load(f)
   body = {k: v for k, v in data.items() if k != "workflow"}
   req = urllib.request.Request(
       "http://localhost:8000/workflows/tdd",
       data=json.dumps(body).encode(),
       headers={"Content-Type": "application/json",
                "X-API-Key": "YOUR_KEY"},
       method="POST",
   )
   with urllib.request.urlopen(req) as resp:
       print(json.loads(resp.read()))
   EOF
   ```

3. Monitor progress:
   ```bash
   curl http://localhost:8000/workflows/<workflow_id>
   ```

## Available Templates

| Template | Endpoint | Agents | Pipeline |
|----------|----------|--------|----------|
| [tdd.yaml](tdd.yaml) | `POST /workflows/tdd` | 3 | test-writer → implementer → refactorer |
| [pair.yaml](pair.yaml) | `POST /workflows/pair` | 2 | navigator → driver |
| [debate.yaml](debate.yaml) | `POST /workflows/debate` | 3 | advocate + critic (N rounds) → judge |
| [adr.yaml](adr.yaml) | `POST /workflows/adr` | 3 | proposer → reviewer → synthesizer |
| [delphi.yaml](delphi.yaml) | `POST /workflows/delphi` | N+2 | experts (parallel) → moderator → consensus |
| [redblue.yaml](redblue.yaml) | `POST /workflows/redblue` | 3 | blue-team → red-team → arbiter |
| [socratic.yaml](socratic.yaml) | `POST /workflows/socratic` | 3 | questioner → responder → synthesizer |
| [spec-first.yaml](spec-first.yaml) | `POST /workflows/spec-first` | 2 | spec-writer → implementer |
| [clean-arch.yaml](clean-arch.yaml) | `POST /workflows/clean-arch` | 4 | domain → usecase → adapter → framework |
| [ddd.yaml](ddd.yaml) | `POST /workflows/ddd` | N+2 | context-mapper → domain-experts (parallel) → integration-designer |
| [competition.yaml](competition.yaml) | `POST /workflows/competition` | N+1 | solvers (parallel) → judge |

## Template Structure

Every YAML file has the same structure:

```yaml
workflow:
  endpoint: /workflows/<name>   # informational — tells you which endpoint to POST to

# Required fields (varies by workflow)
feature: "..."          # or topic:, task:, problem:, etc.

# Optional fields with defaults
language: python
reply_to: null
*_tags: []              # agent capability routing tags
```

The `workflow.endpoint` key is metadata only — it is not sent in the HTTP request body.
Strip it before POSTing, or use the Python snippet above.

## Field Reference

### Common Optional Fields

| Field | Default | Description |
|-------|---------|-------------|
| `reply_to` | `null` | Agent ID that receives the final RESULT in its mailbox |
| `*_tags` | `[]` | Required capability tags for agent routing (e.g. `["gpu", "fast"]`) |

### Workflow-Specific Required Fields

| Template | Required Fields |
|----------|----------------|
| tdd | `feature` |
| pair | `task` |
| debate | `topic` |
| adr | `topic` |
| delphi | `topic` |
| redblue | `topic` |
| socratic | `topic` |
| spec-first | `topic`, `requirements` |
| clean-arch | `feature` |
| ddd | `topic` |
| competition | `problem`, `strategies` (2-10 items) |

## Agent Capability Routing

Use `required_tags` to route specific workflow phases to specialised agents.
For example, if you have GPU-enabled agents tagged `["gpu"]`:

```yaml
# competition.yaml excerpt
solver_tags: ["gpu"]   # only GPU agents run as solvers
judge_tags: []         # any agent can judge
```

Tags must be defined in your orchestrator config YAML:
```yaml
agents:
  - id: gpu-worker-1
    type: claude_code
    tags: [gpu]
```

## Blackboard Pattern

Agents communicate via the shared scratchpad (Blackboard pattern).
Each workflow uses namespaced keys — see the individual template files for the exact key names.

Read a scratchpad value:
```bash
curl http://localhost:8000/scratchpad/<key> -H "X-API-Key: $TMUX_ORCHESTRATOR_API_KEY"
```

List all scratchpad entries:
```bash
curl http://localhost:8000/scratchpad/ -H "X-API-Key: $TMUX_ORCHESTRATOR_API_KEY"
```
