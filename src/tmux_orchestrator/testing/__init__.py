"""Testing utilities for TmuxAgentOrchestrator demos and integration tests.

This package provides helper functions for:
- Demo scripts: waiting for agent task completion via REST API
- Integration tests: HTTP client helpers

These helpers are intentionally synchronous (no asyncio) so they can be used
from plain demo.py scripts without an event loop.

References:
    - Microsoft Azure Architecture Center, "Asynchronous Request-Reply Pattern"
      https://learn.microsoft.com/en-us/azure/architecture/patterns/asynchronous-request-reply
    - Hookdeck, "When to Use Webhooks, WebSocket, Pub/Sub, and Polling" (2025)
      https://hookdeck.com/webhooks/guides/when-to-use-webhooks
"""
