"""WebhookManager — outbound HTTP webhook notifications.

When specific orchestrator events occur (task complete, task failed, agent status
change, workflow complete), a JSON payload is POSTed to each registered webhook URL.

Design references:
- GitHub Webhooks: https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks
- Stripe Webhooks: https://docs.stripe.com/webhooks
- RFC 2104 HMAC: https://datatracker.ietf.org/doc/html/rfc2104
- Zalando RESTful API Guidelines §webhook: https://opensource.zalando.com/restful-api-guidelines/#webhook
- Shopify webhook verification: https://shopify.dev/docs/apps/build/webhooks/signature-verification

DESIGN.md §10.25 (v0.30.0).
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# All supported event names (plus "*" wildcard)
KNOWN_EVENTS: frozenset[str] = frozenset({
    "task_complete",
    "task_failed",
    "task_retrying",
    "task_cancelled",
    "task_dependency_failed",
    "task_waiting",
    "agent_status",
    "workflow_complete",
    "workflow_failed",
    "workflow_cancelled",
    "*",
})


@dataclass
class WebhookDelivery:
    """Record of a single delivery attempt (including all retries)."""

    id: str
    webhook_id: str
    event: str
    timestamp: float
    success: bool
    status_code: int | None
    error: str | None
    duration_ms: float
    # Number of retry attempts made before the final outcome (0 = no retries).
    retries: int = 0


@dataclass
class Webhook:
    """A registered webhook endpoint."""

    id: str
    url: str
    events: list[str]
    secret: str | None
    created_at: float
    delivery_count: int = 0
    failure_count: int = 0
    # Per-webhook retry configuration (set at registration time).
    # max_retries=0 means fire-and-forget (no retries).
    # Reference: DESIGN.md §10.N (v1.0.22 — webhook retry backoff)
    max_retries: int = 3
    retry_backoff_base: float = 1.0
    # Circular buffer of the last 50 delivery attempts.
    _deliveries: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=50),
        repr=False,
        compare=False,
    )

    def to_dict(self) -> dict:
        """Return a JSON-serialisable snapshot (no secret, no raw deliveries)."""
        return {
            "id": self.id,
            "url": self.url,
            "events": self.events,
            "created_at": self.created_at,
            "delivery_count": self.delivery_count,
            "failure_count": self.failure_count,
            "max_retries": self.max_retries,
            "retry_backoff_base": self.retry_backoff_base,
        }


class WebhookManager:
    """Register webhooks and deliver events to matching endpoints.

    Each call to :meth:`deliver` is fire-and-forget: individual HTTP POSTs are
    scheduled as background :func:`asyncio.create_task` calls so they never
    block the dispatch or routing loops.

    Thread safety: this class is designed for use inside a single asyncio event
    loop.  The ``_webhooks`` dict is mutated only from async contexts.
    """

    def __init__(
        self,
        timeout: float = 5.0,
        max_retries: int = 3,
        retry_backoff_base: float = 1.0,
    ) -> None:
        """Initialise the manager.

        Parameters
        ----------
        timeout:
            HTTP timeout in seconds for each delivery attempt.
        max_retries:
            Maximum number of retry attempts after the initial failure.
            Set to 0 to disable retries (fire-and-forget behaviour).
        retry_backoff_base:
            Base value (seconds) for exponential backoff.
            Effective sleep = min(60, retry_backoff_base * 2^attempt) with equal jitter.
            Reference: AWS Architecture Blog "Exponential Backoff and Jitter" (2015).
        """
        self._webhooks: dict[str, Webhook] = {}
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff_base = retry_backoff_base

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def register(
        self,
        url: str,
        events: list[str],
        secret: str | None = None,
        max_retries: int | None = None,
        retry_backoff_base: float | None = None,
    ) -> Webhook:
        """Register a new webhook and return it.

        Parameters
        ----------
        url:
            HTTP(S) endpoint for POST delivery.
        events:
            List of event names to subscribe to (use ``["*"]`` for all).
        secret:
            Optional HMAC-SHA256 signing secret.
        max_retries:
            Per-webhook retry limit.  Falls back to manager default when None.
        retry_backoff_base:
            Per-webhook backoff base in seconds.  Falls back to manager default when None.
        """
        wh = Webhook(
            id=str(uuid.uuid4()),
            url=url,
            events=list(events),
            secret=secret,
            created_at=time.time(),
            max_retries=max_retries if max_retries is not None else self._max_retries,
            retry_backoff_base=retry_backoff_base if retry_backoff_base is not None else self._retry_backoff_base,
        )
        self._webhooks[wh.id] = wh
        logger.info(
            "Webhook registered: id=%s url=%s events=%s max_retries=%d retry_backoff_base=%.1f",
            wh.id, url, events, wh.max_retries, wh.retry_backoff_base,
        )
        return wh

    def unregister(self, webhook_id: str) -> bool:
        """Remove a webhook by ID.  Returns True if found, False otherwise."""
        if webhook_id in self._webhooks:
            del self._webhooks[webhook_id]
            logger.info("Webhook unregistered: id=%s", webhook_id)
            return True
        return False

    def list_all(self) -> list[Webhook]:
        """Return all registered webhooks."""
        return list(self._webhooks.values())

    def get(self, webhook_id: str) -> Webhook | None:
        """Return a webhook by ID, or None if not found."""
        return self._webhooks.get(webhook_id)

    def last_deliveries(self, webhook_id: str, n: int = 20) -> list[WebhookDelivery]:
        """Return the last *n* delivery attempts for *webhook_id*.

        Returns the most recent deliveries first (newest → oldest).
        Returns an empty list if the webhook is not found.
        """
        wh = self._webhooks.get(webhook_id)
        if wh is None:
            return []
        deliveries = list(wh._deliveries)  # oldest first from deque
        # Return the last n entries, reversed (newest first)
        return list(reversed(deliveries[-n:]))

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    async def deliver(self, event: str, data: dict) -> None:
        """Fire background delivery tasks for all webhooks matching *event*.

        Each delivery is a non-blocking :func:`asyncio.create_task` — this
        method returns immediately after spawning the tasks.

        Parameters
        ----------
        event:
            The event name (e.g. ``"task_complete"``).
        data:
            Event-specific payload dict.
        """
        matching = [
            wh for wh in self._webhooks.values()
            if "*" in wh.events or event in wh.events
        ]
        if not matching:
            return

        now_iso = datetime.now(tz=timezone.utc).isoformat()
        body: dict[str, Any] = {
            "event": event,
            "timestamp": now_iso,
            "data": data,
        }
        body_bytes = json.dumps(body, separators=(",", ":")).encode()

        for wh in matching:
            asyncio.create_task(
                self._send(wh, event, body_bytes),
                name=f"webhook-{wh.id[:8]}-{event}",
            )

    @staticmethod
    def _backoff_sleep(attempt: int, backoff_base: float) -> float:
        """Compute equal-jitter sleep duration for *attempt* (0-indexed).

        Formula: cap = min(60, backoff_base * 2^attempt)
                 sleep = cap/2 + uniform(0, cap/2)

        Equal jitter avoids very-short sleeps while still providing
        randomisation to prevent thundering-herd on concurrent retries.

        Reference: AWS Architecture Blog "Exponential Backoff and Jitter" (2015)
        https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
        """
        cap = min(60.0, backoff_base * (2 ** attempt))
        half = cap / 2.0
        return half + random.uniform(0.0, half)

    async def _send(self, wh: Webhook, event: str, body_bytes: bytes) -> None:
        """POST *body_bytes* to *wh.url* and record the delivery outcome.

        Retries up to ``self._max_retries`` times on failure with exponential
        backoff + equal jitter.  A single ``WebhookDelivery`` record is written
        after the final attempt, capturing the number of retries made.

        Reference:
            AWS Builders Library "Timeouts, retries, and backoff with jitter"
            https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/
        """
        import httpx  # noqa: PLC0415

        delivery_id = str(uuid.uuid4())
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if wh.secret:
            headers["X-Signature-SHA256"] = self._sign(body_bytes, wh.secret)

        t0 = time.monotonic()
        status_code: int | None = None
        error: str | None = None
        success = False
        retries_made = 0

        # attempt 0 = initial try; attempts 1..max_retries = retries
        for attempt in range(wh.max_retries + 1):
            if attempt > 0:
                sleep_secs = self._backoff_sleep(attempt - 1, wh.retry_backoff_base)
                logger.debug(
                    "Webhook retry %d/%d: id=%s url=%s event=%s sleeping=%.2fs",
                    attempt, wh.max_retries, wh.id, wh.url, event, sleep_secs,
                )
                await asyncio.sleep(sleep_secs)
                retries_made += 1

            status_code = None
            error = None
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(wh.url, content=body_bytes, headers=headers)
                status_code = resp.status_code
                if 200 <= resp.status_code < 300:
                    success = True
                    break
                # Non-2xx: treat as transient failure and retry
                error = f"HTTP {resp.status_code}"
                logger.debug(
                    "Webhook non-2xx: id=%s url=%s event=%s status=%s attempt=%d",
                    wh.id, wh.url, event, resp.status_code, attempt,
                )
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                logger.debug(
                    "Webhook exception: id=%s url=%s event=%s error=%s attempt=%d",
                    wh.id, wh.url, event, error, attempt,
                )

        duration_ms = (time.monotonic() - t0) * 1000.0

        if not success:
            logger.warning(
                "Webhook delivery failed after %d attempt(s): id=%s url=%s event=%s error=%s",
                retries_made + 1, wh.id, wh.url, event, error,
            )

        delivery = WebhookDelivery(
            id=delivery_id,
            webhook_id=wh.id,
            event=event,
            timestamp=time.time(),
            success=success,
            status_code=status_code,
            error=error,
            duration_ms=round(duration_ms, 2),
            retries=retries_made,
        )
        wh._deliveries.append(delivery)
        wh.delivery_count += 1
        if not success:
            wh.failure_count += 1
            logger.debug(
                "Webhook delivery outcome: id=%s success=%s status=%s retries=%d",
                wh.id, success, status_code, retries_made,
            )
        else:
            logger.debug(
                "Webhook delivered: id=%s event=%s status=%s retries=%d duration_ms=%.1f",
                wh.id, event, status_code, retries_made, duration_ms,
            )

    # ------------------------------------------------------------------
    # HMAC signing
    # ------------------------------------------------------------------

    @staticmethod
    def _sign(body: bytes, secret: str) -> str:
        """Compute HMAC-SHA256 signature of *body* using *secret*.

        Returns the signature as ``sha256=<hex_digest>`` — compatible with
        GitHub and Stripe webhook verification conventions.

        Reference: RFC 2104 HMAC (https://datatracker.ietf.org/doc/html/rfc2104)
        """
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return f"sha256={sig}"
