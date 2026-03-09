"""Memory APIRouter — /agents/{id}/memory/* endpoints.

MIRIX-inspired per-agent episodic memory log.

Design reference:
- Wang & Chen "MIRIX" arXiv:2507.07957 (2025)
- DESIGN.md §10.28 (v1.0.28)
- DESIGN.md §10.42 (v1.1.6)
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException

from tmux_orchestrator.episode_store import EpisodeNotFoundError, EpisodeStore
from tmux_orchestrator.schemas import Episode, EpisodeCreate


def build_memory_router(
    orchestrator: Any,
    auth: Callable,
    *,
    episode_store: EpisodeStore,
) -> APIRouter:
    """Build and return the memory APIRouter.

    Parameters
    ----------
    orchestrator:
        The :class:`~tmux_orchestrator.orchestrator.Orchestrator` instance.
    auth:
        Authentication dependency callable.
    episode_store:
        Shared :class:`~tmux_orchestrator.episode_store.EpisodeStore` instance.
    """
    router = APIRouter()
    _episode_store = episode_store

    @router.get(
        "/agents/{agent_id}/memory",
        summary="List episodic memory entries for an agent",
        dependencies=[Depends(auth)],
    )
    async def list_episodes(agent_id: str, limit: int = 20) -> list[Episode]:
        """Return the most recent *limit* episodic memory entries for *agent_id*.
    
        Episodic memory records past task completions with a human-readable
        summary, outcome classification (success/failure/partial), and lessons
        learned.  Entries are returned newest-first.
    
        Design reference:
        - Wang & Chen, "MIRIX: Multi-Agent Memory System for LLM-Based Agents",
          arXiv:2507.07957, 2025 — episodic memory achieves 35% higher accuracy
          than RAG baseline while reducing storage by 99.9%.
        - "Position: Episodic Memory is the Missing Piece for Long-Term LLM
          Agents", arXiv:2502.06975, 2025.
    
        The 404 is only raised when the agent is completely unknown to the
        registry.  A known agent with *no* episodes returns an empty list.
        Pass ``?limit=N`` to control how many entries are returned (default 20).
        """
        if orchestrator.get_agent(agent_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f"Agent {agent_id!r} not found",
            )
        records = _episode_store.list(agent_id, limit=min(limit, 200))
        return [Episode(**r) for r in records]
    
    @router.post(
        "/agents/{agent_id}/memory",
        summary="Add an episodic memory entry for an agent",
        status_code=201,
        dependencies=[Depends(auth)],
    )
    async def add_episode(agent_id: str, body: EpisodeCreate) -> Episode:
        """Append a new episodic memory entry for *agent_id*.
    
        Agents call this endpoint after completing a task to record what they
        accomplished, whether it succeeded, and any lessons learned.
    
        The ``task_id`` field is optional but recommended for correlation
        with task history (``GET /agents/{id}/history``).
    
        Design reference: MIRIX §4.2 — "each completed task episode is
        appended to the agent's episodic log; the log is never overwritten,
        preserving full recall history."  Contrasts with NOTES.md (single
        summary file that is overwritten on ``/summarize``).
        """
        if orchestrator.get_agent(agent_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f"Agent {agent_id!r} not found",
            )
        record = _episode_store.append(
            agent_id,
            summary=body.summary,
            outcome=body.outcome,
            lessons=body.lessons,
            task_id=body.task_id,
        )
        return Episode(**record)
    
    @router.delete(
        "/agents/{agent_id}/memory/{episode_id}",
        summary="Delete a specific episodic memory entry",
        dependencies=[Depends(auth)],
    )
    async def delete_episode(agent_id: str, episode_id: str) -> dict:
        """Delete the episode identified by *episode_id* from *agent_id*'s log.
    
        Rewrites the JSONL file atomically — all other episodes are preserved.
        Returns 404 when the agent or episode is not found.
    
        Use case: agents can prune obsolete or incorrect episodes to keep the
        memory store relevant.  Unlike NOTES.md, individual episodes can be
        removed without losing the entire history.
        """
        if orchestrator.get_agent(agent_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f"Agent {agent_id!r} not found",
            )
        try:
            _episode_store.delete(agent_id, episode_id)
        except EpisodeNotFoundError:
            raise HTTPException(
                status_code=404,
                detail=f"Episode {episode_id!r} not found for agent {agent_id!r}",
            )
        return {"agent_id": agent_id, "episode_id": episode_id, "deleted": True}

    return router
