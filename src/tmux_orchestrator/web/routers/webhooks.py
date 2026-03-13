"""Webhooks APIRouter — /webhooks/* endpoints.

Design reference:
- GitHub Webhooks; Stripe Webhooks; RFC 2104 HMAC
- Zalando RESTful API Guidelines §webhook
- DESIGN.md §10.25 (v0.30.0)
- DESIGN.md §10.42 (v1.1.6)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from tmux_orchestrator.webhook_manager import WebhookManager

from fastapi import APIRouter, Depends, HTTPException

from tmux_orchestrator.web.schemas import WebhookCreate


def build_webhooks_router(
    orchestrator: Any,
    auth: Callable,
) -> APIRouter:
    """Build and return the webhooks APIRouter."""
    router = APIRouter()

    @router.post(
        "/webhooks",
        summary="Register a new webhook",
        dependencies=[Depends(auth)],
    )
    async def create_webhook(body: WebhookCreate) -> dict:
        """Register a new outbound webhook.
    
        When a subscribed event fires, the orchestrator POSTs a JSON payload to
        the registered URL.  An optional HMAC-SHA256 signature is included in
        the ``X-Signature-SHA256`` header when ``secret`` is supplied.
    
        Valid event names:
        ``task_complete``, ``task_failed``, ``task_retrying``, ``task_cancelled``,
        ``task_dependency_failed``, ``task_waiting``, ``agent_status``,
        ``workflow_complete``, ``workflow_failed``, ``workflow_cancelled``,
        ``phase_complete``, ``phase_failed``, ``phase_skipped``, ``*``
        (wildcard — receive all events).

        Phase events (``phase_complete``, ``phase_failed``, ``phase_skipped``) are
        fired when a workflow phase transitions to a terminal state.  Each payload
        contains ``workflow_id``, ``workflow_name``, ``phase_name``, ``task_ids``,
        and ``timestamp``.  Design reference: DESIGN.md §10.85 (v1.2.9).
    
        Returns: ``{id, url, events, created_at}``
    
        Design reference: GitHub Webhooks; Stripe Webhooks; RFC 2104 HMAC;
        Zalando RESTful API Guidelines §webhook; DESIGN.md §10.25 (v0.30.0).
        """
        from tmux_orchestrator.webhook_manager import KNOWN_EVENTS  # noqa: PLC0415
    
        invalid = [e for e in body.events if e not in KNOWN_EVENTS]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown event name(s): {invalid!r}. "
                       f"Valid events: {sorted(KNOWN_EVENTS)!r}",
            )
        wm: "WebhookManager" = orchestrator._webhook_manager
        wh = wm.register(url=body.url, events=body.events, secret=body.secret)
        return {
            "id": wh.id,
            "url": wh.url,
            "events": wh.events,
            "created_at": wh.created_at,
        }
    
    @router.get(
        "/webhooks",
        summary="List all registered webhooks",
        dependencies=[Depends(auth)],
    )
    async def list_webhooks() -> list:
        """Return all registered webhooks with delivery statistics.
    
        Each entry contains:
        - ``id``: webhook UUID
        - ``url``: target URL
        - ``events``: subscribed event names
        - ``created_at``: Unix timestamp of registration
        - ``delivery_count``: total delivery attempts
        - ``failure_count``: total failed attempts
    
        Design reference: DESIGN.md §10.25 (v0.30.0).
        """
        wm: "WebhookManager" = orchestrator._webhook_manager
        return [wh.to_dict() for wh in wm.list_all()]
    
    @router.delete(
        "/webhooks/{webhook_id}",
        summary="Delete a webhook",
        dependencies=[Depends(auth)],
    )
    async def delete_webhook(webhook_id: str) -> dict:
        """Remove a registered webhook by ID.
    
        Returns 404 if the webhook ID is unknown.
    
        Design reference: DESIGN.md §10.25 (v0.30.0).
        """
        wm: "WebhookManager" = orchestrator._webhook_manager
        removed = wm.unregister(webhook_id)
        if not removed:
            raise HTTPException(
                status_code=404,
                detail=f"Webhook {webhook_id!r} not found",
            )
        return {"deleted": True, "id": webhook_id}
    
    @router.get(
        "/webhooks/{webhook_id}/deliveries",
        summary="Get recent delivery attempts for a webhook",
        dependencies=[Depends(auth)],
    )
    async def get_webhook_deliveries(webhook_id: str) -> list:
        """Return the last 20 delivery attempts for *webhook_id*.
    
        Each entry contains:
        - ``id``: delivery attempt UUID
        - ``webhook_id``: the webhook this delivery belongs to
        - ``event``: the event name that triggered the delivery
        - ``timestamp``: Unix timestamp of the attempt
        - ``success``: whether the delivery succeeded (HTTP 2xx)
        - ``status_code``: HTTP response status code, or null on connection error
        - ``error``: error message string, or null on success
        - ``duration_ms``: request duration in milliseconds
    
        Returns 404 if the webhook ID is unknown.
    
        Design reference: DESIGN.md §10.25 (v0.30.0).
        """
        from dataclasses import asdict  # noqa: PLC0415
    
        wm: "WebhookManager" = orchestrator._webhook_manager
        webhook = wm.get(webhook_id)
        if webhook is None:
            raise HTTPException(
                status_code=404,
                detail=f"Webhook {webhook_id!r} not found",
            )
        deliveries = wm.last_deliveries(webhook_id, n=20)
        return [asdict(d) for d in deliveries]

    return router
