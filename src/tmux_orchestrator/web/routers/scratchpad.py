"""Scratchpad APIRouter — /scratchpad/* endpoints.

Implements the Blackboard architectural pattern: a shared in-process
key/value store that multiple agents can read and write independently.

Design reference:
- Buschmann et al. "Pattern-Oriented Software Architecture" (1996) — Blackboard
- DESIGN.md §10.42 (v1.1.6)
- FastAPI "Bigger Applications": https://fastapi.tiangolo.com/tutorial/bigger-applications/
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException

from tmux_orchestrator.web.schemas import ScratchpadWrite


def build_scratchpad_router(
    auth: Callable,
    scratchpad: dict,
) -> APIRouter:
    """Build and return the scratchpad APIRouter.

    Parameters
    ----------
    auth:
        Authentication dependency callable (combined session + API key).
    scratchpad:
        Shared in-process dict for the Blackboard store.  Pass the same
        dict object that ``create_app`` created so all routers share state.
    """
    router = APIRouter()

    @router.get("/scratchpad/", summary="List all scratchpad entries", dependencies=[Depends(auth)])
    async def scratchpad_list() -> dict:
        """Return all scratchpad key-value pairs.

        The shared scratchpad implements the Blackboard architectural pattern
        (Buschmann et al., 1996): a shared working memory that multiple agents
        can read and write independently.  It is especially useful for pipeline
        workflows where one agent writes results that a downstream agent reads.

        Reference: DESIGN.md §11 (architecture) — shared scratchpad (v0.16.0)
        """
        return dict(scratchpad)

    @router.put(
        "/scratchpad/{key}",
        summary="Write a value to the scratchpad",
        dependencies=[Depends(auth)],
    )
    async def scratchpad_put(key: str, body: ScratchpadWrite) -> dict:
        """Write *value* under *key*.  Creates or overwrites the entry."""
        scratchpad[key] = body.value
        return {"key": key, "updated": True}

    @router.get(
        "/scratchpad/{key}",
        summary="Read a value from the scratchpad",
        dependencies=[Depends(auth)],
    )
    async def scratchpad_get(key: str) -> dict:
        """Return the value stored under *key*, or 404 if not found."""
        if key not in scratchpad:
            raise HTTPException(status_code=404, detail=f"Scratchpad key {key!r} not found")
        return {"key": key, "value": scratchpad[key]}

    @router.delete(
        "/scratchpad/{key}",
        summary="Delete a scratchpad entry",
        dependencies=[Depends(auth)],
    )
    async def scratchpad_delete(key: str) -> dict:
        """Remove *key* from the scratchpad.  Returns 404 if not found."""
        if key not in scratchpad:
            raise HTTPException(status_code=404, detail=f"Scratchpad key {key!r} not found")
        del scratchpad[key]
        return {"key": key, "deleted": True}

    return router
