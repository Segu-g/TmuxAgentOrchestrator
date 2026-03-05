"""Parent-notification helper for /plan and /tdd slash commands.

When an orchestrated agent runs /plan or /tdd, this module sends a structured
PEER_MSG to the parent agent so the Director can track sub-agent progress without
polling.

Design reference:
  - Google ADK AgentTool pattern: child captures final response and forwards to parent.
  - Semantic Kernel ResponseCallback: parent observes each agent's output.
  - /progress command: existing child→parent notification (same HTTP pattern).

Usage (from a slash command Python snippet):
    from tmux_orchestrator.slash_notify import notify_parent
    notify_parent(event_type="plan_created", extra={"description": desc})
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Internal helpers (injectable for tests)
# ---------------------------------------------------------------------------


def _cwd() -> Path:
    """Return current working directory (override in tests via mock)."""
    return Path.cwd()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_parent_message(
    agent_id: str,
    event_type: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    """Build the payload dict for a parent notification message.

    Parameters
    ----------
    agent_id:
        The ID of the sending agent (``worker-1``, etc.).
    event_type:
        A short event name, e.g. ``"plan_created"`` or ``"tdd_cycle_started"``.
    extra:
        Additional key/value pairs merged into the payload
        (e.g. ``{"description": "...", "plan_path": "PLAN.md"}``).

    Returns
    -------
    dict
        Payload suitable for ``POST /agents/{parent_id}/message``.
    """
    payload: dict[str, Any] = {
        "event": event_type,
        "from_id": agent_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(extra)
    return payload


def notify_parent(
    event_type: str,
    extra: dict[str, Any],
    *,
    timeout: int = 10,
) -> bool:
    """Notify the parent agent about a slash-command completion event.

    Reads ``__orchestrator_context__.json`` from the current working directory
    to discover the agent's own ID and the REST API base URL.  Then calls
    ``GET /agents`` to resolve the parent agent ID, and finally POSTs a
    ``PEER_MSG`` to ``POST /agents/{parent_id}/message``.

    This is a fire-and-forget call — failures are swallowed so that slash
    commands always succeed even when the orchestrator is unavailable.

    Additionally, if a ``PLAN.md`` exists in the current directory and the
    event_type is ``"plan_created"``, its content is included in the payload
    under the ``"plan_content"`` key.

    Parameters
    ----------
    event_type:
        Short event identifier (``"plan_created"``, ``"tdd_cycle_started"``).
    extra:
        Extra fields to include in the payload.
    timeout:
        HTTP request timeout in seconds (default 10).

    Returns
    -------
    bool
        ``True`` if the notification was sent successfully,
        ``False`` if context is missing, parent is not set, or an error occurred.
    """
    cwd = _cwd()
    ctx_path = cwd / "__orchestrator_context__.json"

    if not ctx_path.exists():
        # Not running inside an orchestrated environment — silent no-op.
        return False

    try:
        ctx = json.loads(ctx_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False

    agent_id: str = ctx.get("agent_id", "unknown")
    api: str = ctx.get("web_base_url", "http://localhost:8000").rstrip("/")
    api_key: str = ctx.get("api_key", "")

    # Enrich extra: attach plan content when available
    enriched = dict(extra)
    if event_type == "plan_created":
        plan_path = cwd / "PLAN.md"
        if plan_path.exists():
            try:
                enriched["plan_content"] = plan_path.read_text()
            except OSError:
                pass

    # Build common HTTP headers (include API key when set)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    # Resolve parent_id via GET /agents
    parent_id: str | None = None
    try:
        req = urllib.request.Request(
            f"{api}/agents",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            agents = json.loads(resp.read())
        for agent in agents:
            if agent.get("id") == agent_id:
                parent_id = agent.get("parent_id")
                break
    except Exception:  # noqa: BLE001
        return False

    if not parent_id:
        # Top-level agent with no parent — nothing to notify.
        return False

    # Build and send the notification
    payload = build_parent_message(agent_id=agent_id, event_type=event_type, extra=enriched)
    body = json.dumps({
        "type": "PEER_MSG",
        "payload": payload,
    }).encode()

    try:
        post_req = urllib.request.Request(
            f"{api}/agents/{parent_id}/message",
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(post_req, timeout=timeout) as resp:
            resp.read()  # consume response
    except urllib.error.HTTPError:
        return False
    except OSError:
        return False

    return True
