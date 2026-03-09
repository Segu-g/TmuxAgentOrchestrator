"""Demo client helpers for TmuxAgentOrchestrator demo scripts.

Provides synchronous (no asyncio) helper functions for:

1. ``wait_for_agent_done`` — poll ``GET /agents/{id}/history`` until a specific
   task_id appears with ``finished_at`` set.  This replaces the fragile
   IDLE-status polling pattern which has a race condition when an agent
   transitions to IDLE and then immediately receives another task.

2. ``wait_for_server`` — wait for the REST server to accept connections on
   ``GET /agents`` (200 or any non-5xx status).

3. ``api`` — simple JSON REST helper (GET, POST, PUT, DELETE).

Design rationale:
    Polling ``GET /agents/{id}/history?limit=200`` and filtering by task_id is
    the most reliable completion detection because:

    - The history record is only written after the orchestrator receives the
      RESULT message from the agent (i.e., after ``/task-complete`` fires).
    - task_id is immutable — it uniquely identifies the dispatched task
      regardless of how many subsequent tasks the agent handles.
    - IDLE-status polling has a race: agent goes IDLE → demo checks IDLE →
      agent starts next task → demo incorrectly concludes task N is done.

    This pattern implements the "Asynchronous Request-Reply" pattern from the
    Microsoft Azure Architecture Center (2024) applied to per-agent task
    history.

References:
    - Microsoft Azure Architecture Center, "Asynchronous Request-Reply Pattern"
      https://learn.microsoft.com/en-us/azure/architecture/patterns/asynchronous-request-reply
      Year: 2024
    - adidas API Guidelines — "Polling"
      https://adidas.gitbook.io/api-guidelines/rest-api-guidelines/execution/long-running-tasks/polling
    - Hookdeck, "When to Use Webhooks, WebSocket, Pub/Sub, and Polling" (2025)
      https://hookdeck.com/webhooks/guides/when-to-use-webhooks
    - DESIGN.md §10.N (v1.0.18 — demo stability: history-based IDLE detection)
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


# ---------------------------------------------------------------------------
# wait_for_agent_done
# ---------------------------------------------------------------------------


def wait_for_agent_done(
    base_url: str,
    agent_id: str,
    task_id: str,
    *,
    api_key: str = "",
    timeout: float = 900.0,
    poll_interval: float = 5.0,
) -> dict:
    """Poll ``GET /agents/{agent_id}/history`` until *task_id* is complete.

    Polls the history endpoint at *poll_interval* second intervals (up to
    *timeout* seconds total).  Returns as soon as a history record with
    ``task_id == task_id`` is found AND ``finished_at`` is not ``None``.

    Parameters
    ----------
    base_url:
        Root URL of the orchestrator REST server, e.g. ``http://localhost:8000``.
    agent_id:
        The ID of the agent whose history to poll.
    task_id:
        The task ID returned by ``POST /tasks``.  Must match ``record["task_id"]``
        in the history response.
    api_key:
        Value for the ``X-API-Key`` header.  Pass ``""`` to omit the header.
    timeout:
        Maximum seconds to wait before raising ``TimeoutError``.  Default 900s
        (15 minutes), matching ``task_timeout: 900`` in demo configs.
    poll_interval:
        Seconds between polls.  Default 5.0s — appropriate for long-running
        agent tasks (agents take minutes, not milliseconds).

    Returns
    -------
    dict
        The history record dict containing at minimum:
        ``task_id``, ``prompt``, ``started_at``, ``finished_at``,
        ``duration_s``, ``status``, ``error``.

    Raises
    ------
    TimeoutError
        If *timeout* seconds elapse before the task completes.
    RuntimeError
        If the agent is not found (HTTP 404).
    urllib.error.URLError
        On unrecoverable network errors (not raised on transient failures —
        those are silently retried until timeout).
    """
    if not base_url:
        raise ValueError("base_url must not be empty")
    if not agent_id:
        raise ValueError("agent_id must not be empty")
    if not task_id:
        raise ValueError("task_id must not be empty")

    url = f"{base_url.rstrip('/')}/agents/{agent_id}/history?limit=200"
    headers: dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key

    deadline = time.monotonic() + timeout

    while True:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                records: list[dict] = json.loads(resp.read())

            # Search for this task_id with finished_at set
            for record in records:
                if record.get("task_id") == task_id:
                    if record.get("finished_at") is not None:
                        return record
                    # Record exists but task not yet complete — continue polling
                    break  # No need to scan further records for this task_id

        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RuntimeError(
                    f"Agent {agent_id!r} not found (HTTP 404)"
                ) from exc
            # Other HTTP errors (429, 5xx, etc.) — retry silently
        except urllib.error.URLError:
            # Connection refused / network error — retry silently
            pass
        except Exception:  # noqa: BLE001
            # JSON parse errors, timeouts, etc. — retry silently
            pass

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Timed out after {timeout}s waiting for agent {agent_id!r} "
                f"task {task_id!r} to complete"
            )

        sleep_time = min(poll_interval, remaining)
        time.sleep(sleep_time)


# ---------------------------------------------------------------------------
# wait_for_server
# ---------------------------------------------------------------------------


def wait_for_server(
    base_url: str,
    *,
    api_key: str = "",
    timeout: float = 30.0,
) -> bool:
    """Wait for the REST server to respond on ``GET /agents``.

    Polls ``GET /agents`` until a non-5xx response is received or *timeout*
    seconds elapse.

    Parameters
    ----------
    base_url:
        Root URL of the orchestrator REST server.
    api_key:
        Value for the ``X-API-Key`` header.
    timeout:
        Maximum seconds to wait.  Default 30s.

    Returns
    -------
    bool
        ``True`` if the server responded within *timeout* seconds,
        ``False`` otherwise.
    """
    url = f"{base_url.rstrip('/')}/agents"
    headers: dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status < 500:
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
    return False


# ---------------------------------------------------------------------------
# api
# ---------------------------------------------------------------------------


def api(
    method: str,
    base_url: str,
    path: str,
    data: Any = None,
    *,
    api_key: str = "",
) -> Any:
    """Send a simple JSON REST request and return the parsed response body.

    Parameters
    ----------
    method:
        HTTP method: ``"GET"``, ``"POST"``, ``"PUT"``, ``"DELETE"``, etc.
    base_url:
        Root URL, e.g. ``"http://localhost:8000"``.
    path:
        URL path, e.g. ``"/tasks"``.  Must start with ``"/"``.
    data:
        If provided, serialised as JSON and sent as the request body.
    api_key:
        Value for the ``X-API-Key`` header.

    Returns
    -------
    Any
        Parsed JSON response body (dict, list, str, int, etc.).

    Raises
    ------
    urllib.error.HTTPError
        On HTTP error responses (4xx, 5xx).
    """
    url = f"{base_url.rstrip('/')}{path}"
    body = json.dumps(data).encode() if data is not None else None
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())
