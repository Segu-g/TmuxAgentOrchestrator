"""Security hardening for TmuxAgentOrchestrator (v0.44.0).

Implements three security layers:

1. ``sanitize_prompt(prompt, max_length)``
   Strip/replace dangerous control characters from task prompts before they
   are sent via ``send_keys`` to a tmux pane.  Prevents shell injection via
   newlines, null bytes, and carriage returns.

   References:
   - OWASP, "LLM Prompt Injection Prevention Cheat Sheet"
     https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html (2025)
   - OWASP, "LLM01:2025 Prompt Injection"
     https://genai.owasp.org/llmrisk/llm01-prompt-injection/ (2025)

2. ``AuditLogMiddleware``
   Starlette ``BaseHTTPMiddleware`` that intercepts every HTTP request and
   records a structured ``AuditLogEntry`` (timestamp, method, path, client_ip,
   api_key_hint, status_code, duration_ms).  Entries are stored in an
   in-process ring buffer (max 1 000 entries).

   References:
   - Microsoft, "Security - Multi-agent Reference Architecture"
     https://microsoft.github.io/multi-agent-reference-architecture/docs/security/Security.html (2025)
   - arXiv:2506.04133v4 "TRiSM for Agentic AI" (2025)

3. CORS origins (``cors_origins`` in ``OrchestratorConfig``)
   ``create_app()`` accepts a ``cors_origins`` parameter that is passed to
   FastAPI's ``CORSMiddleware``.  Default is ``["http://localhost:*",
   "http://127.0.0.1:*"]`` — loopback-only.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import ClassVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MAX_PROMPT_LENGTH: int = 16_384
_AUDIT_LOG_MAX_SIZE: int = 1_000


# ---------------------------------------------------------------------------
# Prompt sanitization
# ---------------------------------------------------------------------------


def sanitize_prompt(prompt: str, *, max_length: int = _DEFAULT_MAX_PROMPT_LENGTH) -> str:
    """Return a sanitized copy of *prompt* safe for use with ``send_keys``.

    Transformations applied (in order):

    1. Remove null bytes (``\\x00``) — can corrupt terminal emulators.
    2. Remove carriage returns (``\\r``) — prevent CR-injection.
    3. Replace newlines (``\\n``) with a single space — prevents multi-line
       command injection when a task prompt is sent as a single ``send_keys``
       call.  The full content is preserved, just flattened.
    4. Truncate to *max_length* characters.

    .. warning::
        This function does **not** attempt to detect or block adversarial LLM
        prompts (indirect injection attacks).  It focuses solely on preventing
        shell-level control character injection via the tmux ``send_keys`` API.

    Args:
        prompt: Raw task prompt string to sanitize.
        max_length: Maximum allowed length (default 16 384 chars).

    Returns:
        Sanitised prompt string.
    """
    if not prompt:
        return prompt

    original_len = len(prompt)

    # Step 1: remove null bytes
    result = prompt.replace("\x00", "")

    # Step 2: remove carriage returns
    result = result.replace("\r", "")

    # Step 3: replace newlines with a space (preserve content, flatten)
    result = result.replace("\n", " ")

    # Step 4: truncate to max_length
    if len(result) > max_length:
        result = result[:max_length]
        logger.warning(
            "sanitize_prompt: prompt truncated from %d to %d characters",
            original_len,
            max_length,
        )

    if result != prompt:
        logger.info(
            "sanitize_prompt: prompt modified (original_len=%d, sanitized_len=%d)",
            original_len,
            len(result),
        )

    return result


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@dataclass
class AuditLogEntry:
    """Structured record of a single HTTP request.

    Stored in the ``AuditLogMiddleware`` ring buffer and serialisable to JSON
    via :meth:`to_dict`.

    Attributes:
        timestamp: UNIX epoch seconds when the request was *received*.
        method: HTTP method (GET, POST, etc.)
        path: Request path (without query string).
        client_ip: Client IP address string, or empty string if unavailable.
        api_key_hint: First 8 characters of the ``X-API-Key`` header, or
            empty string if not present.  Never stores the full key.
        status_code: HTTP response status code.
        duration_ms: Time from request receipt to response send, in ms.
    """

    timestamp: float
    method: str
    path: str
    client_ip: str
    api_key_hint: str
    status_code: int
    duration_ms: float

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dictionary representation."""
        return asdict(self)


class AuditLogMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that records structured audit log entries.

    Entries are stored in a class-level ring buffer (``_log``) of at most
    ``_AUDIT_LOG_MAX_SIZE`` entries.  Use :meth:`get_log` and
    :meth:`clear_log` for test introspection.

    .. note::
        This is an **in-process** ring buffer.  For production deployments
        requiring durability, integrate with a structured logging backend
        (e.g. ``structlog`` → JSON → Loki / Elasticsearch).

    References:
        - Microsoft Multi-Agent Reference Architecture — Security (2025)
        - IBM mcp-context-forge Audit Logging System (2025)
    """

    _log: ClassVar[deque[AuditLogEntry]] = deque(maxlen=_AUDIT_LOG_MAX_SIZE)

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        start = time.monotonic()
        received_at = time.time()

        # Extract API key hint (first 8 chars only — never store the full key)
        raw_key = request.headers.get("X-API-Key", "")
        key_hint = raw_key[:8]

        # Identify client IP
        client_ip: str = ""
        if request.client:
            client_ip = request.client.host or ""

        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            status_code = 500
            raise
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            entry = AuditLogEntry(
                timestamp=received_at,
                method=request.method,
                path=request.url.path,
                client_ip=client_ip,
                api_key_hint=key_hint,
                status_code=status_code,
                duration_ms=duration_ms,
            )
            self._log.append(entry)
            logger.info(
                "audit",
                extra={
                    "audit.method": entry.method,
                    "audit.path": entry.path,
                    "audit.status_code": entry.status_code,
                    "audit.duration_ms": round(entry.duration_ms, 2),
                    "audit.client_ip": entry.client_ip,
                    "audit.api_key_hint": entry.api_key_hint,
                },
            )

        return response

    @classmethod
    def get_log(cls) -> list[AuditLogEntry]:
        """Return a snapshot of the current audit log (most-recent-last)."""
        return list(cls._log)

    @classmethod
    def clear_log(cls) -> None:
        """Clear the audit log (primarily for testing)."""
        cls._log.clear()
