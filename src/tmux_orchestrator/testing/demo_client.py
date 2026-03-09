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
import os
import signal
import subprocess
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


# ---------------------------------------------------------------------------
# start_server / stop_server — process-group-aware server lifecycle helpers
# ---------------------------------------------------------------------------


def start_server(
    cmd: list[str],
    cwd: "str | os.PathLike[str]",
    **kwargs: Any,
) -> subprocess.Popen:
    """Start a server process in its own process group.

    Uses ``start_new_session=True`` (equivalent to ``os.setsid()``) so that
    the server and all its child processes form an independent process group.
    This makes ``stop_server()`` able to cleanly terminate the entire group
    via ``os.killpg()``, avoiding zombie child processes left behind by a
    simple ``proc.terminate()`` call.

    Parameters
    ----------
    cmd:
        Command list passed to ``subprocess.Popen``.
    cwd:
        Working directory for the server process.  **Must** be the demo
        folder (not ``PROJECT_ROOT``) so that ``WorktreeManager`` uses the
        demo git repository.
    **kwargs:
        Additional keyword arguments forwarded to ``subprocess.Popen``.
        Note: ``start_new_session`` is always set to ``True``; any caller-
        supplied value is overridden.

    Returns
    -------
    subprocess.Popen
        The running server process handle.  Pass to ``stop_server()`` to
        shut it down.

    Design references:
    - POSIX ``setsid(2)`` — create a new session and process group
    - POSIX ``killpg(2)`` — send signal to a process group
    - GNU libc manual "Process Group Functions"
      https://www.gnu.org/software/libc/manual/html_node/Process-Group-Functions.html
    - DESIGN.md §10.37 (v1.1.1 — server cleanup helper)
    """
    # Force start_new_session=True; ignore any caller-supplied value.
    kwargs.pop("start_new_session", None)
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        **kwargs,
    )
    return proc


def stop_server(proc: subprocess.Popen, *, timeout: float = 5.0) -> None:
    """Terminate a server process and its entire process group.

    Sends ``SIGTERM`` to the process group created by ``start_server()``,
    then waits up to *timeout* seconds for the process to exit.  If the
    process group no longer exists (e.g. it already exited), the error is
    silently ignored.

    Parameters
    ----------
    proc:
        The ``subprocess.Popen`` object returned by ``start_server()``.
    timeout:
        Seconds to wait for the process to exit after sending ``SIGTERM``.
        Default 5.0 s.

    Design references:
    - POSIX ``killpg(2)`` — send signal to process group
    - POSIX SIGTERM/SIGKILL model: graceful shutdown before forced kill
    - DESIGN.md §10.37 (v1.1.1 — server cleanup helper)
    """
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        # Process already exited — nothing to kill.
        pass
    except OSError:
        # Fallback: try direct terminate if getpgid fails.
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Force-kill the process group if graceful shutdown timed out.
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
