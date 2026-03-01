"""WebSocket hub that fans out bus events to all connected browser clients."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from tmux_orchestrator.bus import Bus

logger = logging.getLogger(__name__)


class WebSocketHub:
    """Subscribes to the bus and broadcasts every message to all WS clients."""

    def __init__(self, bus: "Bus") -> None:
        self.bus = bus
        self._clients: set[WebSocket] = set()
        self._pump_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Client management
    # ------------------------------------------------------------------

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        logger.info("WS client connected (%d total)", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        logger.info("WS client disconnected (%d total)", len(self._clients))

    async def handle(self, ws: WebSocket) -> None:
        """Full lifecycle for one WebSocket connection."""
        await self.connect(ws)
        try:
            while True:
                # Keep the connection alive; clients may send pings
                data = await ws.receive_text()
                # Optionally handle inbound commands from browser
                try:
                    msg = json.loads(data)
                    logger.debug("WS inbound: %s", msg)
                except json.JSONDecodeError:
                    pass
        except WebSocketDisconnect:
            self.disconnect(ws)

    async def broadcast(self, payload: dict) -> None:
        text = json.dumps(payload)
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001
                dead.add(ws)
        self._clients -= dead

    # ------------------------------------------------------------------
    # Bus pump
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start pumping bus messages to all connected WS clients."""
        self._pump_task = asyncio.create_task(self._pump(), name="ws-hub-pump")

    async def stop(self) -> None:
        if self._pump_task:
            self._pump_task.cancel()
        await self.bus.unsubscribe("__ws_hub__")

    async def _pump(self) -> None:
        q = await self.bus.subscribe("__ws_hub__", broadcast=True)
        async for msg in self.bus.iter_messages(q):
            if self._clients:
                await self.broadcast(msg.to_dict())
