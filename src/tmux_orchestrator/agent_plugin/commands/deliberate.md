Trigger an internal 2-agent debate to resolve a design question. An advocate sub-agent argues for the proposal, a critic sub-agent argues against it, and you synthesize their arguments into `DELIBERATION.md`.

Based on: DEBATE framework (ACL 2024, arXiv:2405.09935) — 2-agent, 2-round Devil's Advocate pattern substantially reduces single-agent bias. CONSENSAGENT (ACL 2025) — sycophancy suppression via explicit critic role.

Usage: `/deliberate <question or design decision>`

Execute this Python snippet:

```python
import json, os, urllib.request, urllib.error, time
from pathlib import Path
from datetime import datetime, timezone

question = """$ARGUMENTS""".strip()
if not question:
    print("Usage: /deliberate <question or design decision>")
    print("  Example: /deliberate 'Should we use SQLite or PostgreSQL for task storage?'")
    raise SystemExit(1)

# Discover context file: per-agent file takes priority (safe for shared cwd).
_aid = os.environ.get("TMUX_ORCHESTRATOR_AGENT_ID", "")
_ctx_path = Path(f"__orchestrator_context__{_aid}__.json") if _aid else None
if _ctx_path is None or not _ctx_path.exists():
    _ctx_path = Path("__orchestrator_context__.json")
if not _ctx_path.exists():
    print("Not running in an orchestrated environment (__orchestrator_context__.json not found).")
    raise SystemExit(1)

ctx    = json.loads(_ctx_path.read_text())
my_id  = ctx["agent_id"]
api    = ctx["web_base_url"].rstrip("/")
now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# Read API key securely: env var takes priority, then per-agent file, then legacy file
api_key = os.environ.get("TMUX_ORCHESTRATOR_API_KEY", "")

headers = {"Content-Type": "application/json"}
if api_key:
    headers["X-API-Key"] = api_key

print(f"=== /deliberate — Internal Debate ===")
print(f"Question : {question}")
print(f"Agent    : {my_id}")
print(f"Time     : {now}")
print()

# ── STEP 1: Discover available worker templates ──────────────────────────────
try:
    req = urllib.request.Request(f"{api}/agents", headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        agents = json.loads(resp.read())
    worker_templates = [a["id"] for a in agents if a.get("id") != my_id]
except Exception as e:
    worker_templates = []
    print(f"Note: Could not fetch agent list ({e}).")

if not worker_templates:
    print("ERROR: No worker templates available for spawning debate sub-agents.")
    print("       Ensure at least one other agent is defined in the YAML config.")
    raise SystemExit(1)

template_id = worker_templates[0]
print(f"Using template '{template_id}' for debate sub-agents.")
print()

# ── STEP 2: Spawn advocate sub-agent ────────────────────────────────────────
print("Spawning advocate sub-agent (argues FOR / pro position)...")

def spawn_subagent(template_id: str, parent_id: str) -> str:
    body = json.dumps({"parent_id": parent_id, "template_id": template_id}).encode()
    req  = urllib.request.Request(f"{api}/agents", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

spawn_subagent(template_id, my_id)

# Wait for the spawn STATUS message in inbox
print("Waiting for advocate spawn confirmation (up to 60 s)...")
inbox = Path(ctx["mailbox_dir"]).expanduser() / ctx["session_name"] / my_id / "inbox"
inbox.mkdir(parents=True, exist_ok=True)

def wait_for_spawn_msg(inbox: Path, timeout: int = 60) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for p in sorted(inbox.glob("*.json")):
            d = json.loads(p.read_text())
            payload = d.get("payload", {})
            if payload.get("event") == "subagent_spawned":
                p.unlink()
                return payload
        time.sleep(1)
    return None

adv_payload = wait_for_spawn_msg(inbox, timeout=60)
if not adv_payload:
    print("ERROR: Did not receive spawn confirmation for advocate within 60 s.")
    print("       Check orchestrator logs and retry.")
    raise SystemExit(1)

advocate_id = adv_payload["sub_agent_id"]
print(f"  Advocate: {advocate_id}")
print()

# ── STEP 3: Spawn critic sub-agent ──────────────────────────────────────────
print("Spawning critic sub-agent (argues AGAINST / con position)...")
spawn_subagent(template_id, my_id)

print("Waiting for critic spawn confirmation (up to 60 s)...")
crit_payload = wait_for_spawn_msg(inbox, timeout=60)
if not crit_payload:
    print("ERROR: Did not receive spawn confirmation for critic within 60 s.")
    raise SystemExit(1)

critic_id = crit_payload["sub_agent_id"]
print(f"  Critic  : {critic_id}")
print()

# ── STEP 4: Brief advocate and critic ───────────────────────────────────────
def send_msg(to_id: str, text: str) -> None:
    body = json.dumps({"type": "PEER_MSG", "payload": {"from_display": my_id, "text": text}}).encode()
    req  = urllib.request.Request(
        f"{api}/agents/{to_id}/message", data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        json.loads(resp.read())

advocate_brief = (
    f"You are the ADVOCATE in a structured design debate.\n\n"
    f"Question: {question}\n\n"
    f"Your role: Construct the strongest possible argument IN FAVOUR of the proposal. "
    f"List 3-5 concrete benefits with evidence or reasoning. "
    f"Be direct and persuasive. Write your argument to the file ADVOCATE_ARGUMENT.md, "
    f"then call /task-complete with a one-line summary. "
    f"Do NOT hedge — take the pro position firmly."
)

critic_brief = (
    f"You are the CRITIC in a structured design debate.\n\n"
    f"Question: {question}\n\n"
    f"Your role: Construct the strongest possible argument AGAINST the proposal. "
    f"List 3-5 concrete risks, costs, or weaknesses with evidence or reasoning. "
    f"Be direct and rigorous. Write your argument to the file CRITIC_ARGUMENT.md, "
    f"then call /task-complete with a one-line summary. "
    f"Do NOT hedge — take the con position firmly."
)

print("Briefing advocate and critic...")
send_msg(advocate_id, advocate_brief)
send_msg(critic_id, critic_brief)
print("  Briefs sent.")
print()

# ── STEP 5: Output deliberation template ────────────────────────────────────
print("━━━ DELIBERATION TEMPLATE ━━━")
print()
print("While the sub-agents work, prepare DELIBERATION.md with this structure:")
print()
_fence = chr(96) * 3
print(f"{_fence}markdown")
print(f"# Deliberation: {question}")
print(f"Date: {now}")
print(f"Participants: {advocate_id} (advocate), {critic_id} (critic)")
print()
print("## Pro Arguments")
print("(paste ADVOCATE_ARGUMENT.md content here)")
print()
print("## Con Arguments")
print("(paste CRITIC_ARGUMENT.md content here)")
print()
print("## Synthesis")
print("(your balanced conclusion based on both positions)")
print()
print("## Decision")
print("(the recommended course of action)")
print(f"{_fence}")
print()
print("━━━ NEXT STEPS ━━━")
print()
print("1. Wait for both sub-agents to complete (monitor with /list-agents)")
print("   They will signal via __MSG__ notifications when done.")
print("   Use /check-inbox to receive their completion notifications.")
print()
print("2. Read both argument files:")
print("   cat ADVOCATE_ARGUMENT.md")
print("   cat CRITIC_ARGUMENT.md")
print()
print("3. Write DELIBERATION.md synthesizing both positions with your judgment.")
print()
print("4. (Optional — Round 2) Send CRITIC_ARGUMENT.md to advocate for rebuttal,")
print("   then send ADVOCATE_ARGUMENT.md to critic for counter-rebuttal.")
print("   Repeat /check-inbox after each send.")
print()
print("5. Signal completion:")
print(f"   /task-complete Deliberation complete — see DELIBERATION.md")
print()
print(f"Sub-agents: advocate={advocate_id}  critic={critic_id}")
print()
print("Deliberation underway. Monitor progress with /list-agents.")
```

After sub-agents complete, synthesize their arguments into `DELIBERATION.md` and call `/task-complete`.

**Pattern reference (DEBATE, ACL 2024):** 2-agent Devil's Advocate design substantially reduces single-agent bias in evaluation tasks. The advocate/critic asymmetry (not both "balanced") is intentional — forced polarisation produces stronger arguments for synthesis.
