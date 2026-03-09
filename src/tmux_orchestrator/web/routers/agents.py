"""Agents APIRouter — /agents/* endpoints.

Design reference: DESIGN.md §10.42 (v1.1.6)
FastAPI "Bigger Applications": https://fastapi.tiangolo.com/tutorial/bigger-applications/
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from tmux_orchestrator.agents.base import Task
from tmux_orchestrator.application.context_compression import TfIdfContextCompressor
from tmux_orchestrator.application.use_cases import (
    GetAgentDTO,
    GetAgentUseCase,
    ListAgentsDTO,
    ListAgentsUseCase,
)
from tmux_orchestrator.bus import Message, MessageType
from tmux_orchestrator.config import AgentRole
from tmux_orchestrator.web.schemas import (
    AgentBriefRequest,
    AgentKillResponse,
    ChangeStrategyRequest,
    DirectorChat,
    DynamicAgentCreate,
    SendMessage,
    SpawnAgent,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level schemas for context compression endpoints
# (must be module-level for FastAPI/Pydantic to correctly bind request body)
# ---------------------------------------------------------------------------


class CompressContextRequest(BaseModel):
    """Request body for POST /agents/{agent_id}/compress-context."""

    task_query: str = Field(
        default="",
        alias="query",
        description=(
            "Task prompt used as the relevance reference.  Lines most "
            "similar to this text are retained.  Empty string → "
            "query-agnostic compression."
        ),
    )
    drop_percentile: float = Field(
        default=0.40,
        ge=0.0,
        lt=1.0,
        description=(
            "Fraction of content lines to drop by ascending relevance "
            "score.  Default 0.40 removes the least-relevant 40 %."
        ),
    )
    reorder: bool = Field(
        default=False,
        description=(
            "When True, surviving lines are reordered so the "
            "highest-scoring lines appear first, mitigating the "
            "'Lost in the Middle' effect (Liu et al. TACL 2024)."
        ),
    )

    model_config = {"populate_by_name": True}


def _build_agent_tree(agents: list[dict]) -> list[dict]:
    """Convert a flat list of agent dicts into a nested tree.

    Each dict must have an ``id`` and optional ``parent_id`` key.  Returns a
    list of root nodes (``parent_id is None``), each with a ``children`` key
    that recursively holds child nodes.

    The resulting structure is compatible with d3-hierarchy's ``d3.hierarchy()``
    function (the library expects a tree rooted at a single node, but we expose
    multiple roots as a list for the REST caller to use freely).
    """
    by_id: dict[str, dict] = {}
    for a in agents:
        node = {**a, "children": []}
        by_id[a["id"]] = node

    roots: list[dict] = []
    for node in by_id.values():
        parent_id = node.get("parent_id")
        if parent_id and parent_id in by_id:
            by_id[parent_id]["children"].append(node)
        else:
            roots.append(node)

    return roots


def build_agents_router(
    orchestrator: Any,
    auth: Callable,
    *,
    episode_store: Any = None,
) -> APIRouter:
    """Build and return the agents APIRouter.

    Parameters
    ----------
    orchestrator:
        The :class:`~tmux_orchestrator.orchestrator.Orchestrator` instance.
    auth:
        Authentication dependency callable (combined session + API key).
    episode_store:
        :class:`~tmux_orchestrator.episode_store.EpisodeStore` instance for
        episode auto-record on task-complete (v1.0.29).
    """
    router = APIRouter()
    _orch_config = getattr(orchestrator, "config", None)
    _episode_store = episode_store  # used in agent_task_complete episode auto-record

    @router.get("/agents", summary="List agents and their status", dependencies=[Depends(auth)])
    async def list_agents() -> list[dict]:
        uc = ListAgentsUseCase(orchestrator)
        result = await uc.execute(ListAgentsDTO())
        return result.to_list()
    
    @router.get("/agents/tree", summary="Agent hierarchy as nested tree", dependencies=[Depends(auth)])
    async def agents_tree() -> list[dict]:
        """Return the agent list as a nested JSON tree (d3-hierarchy compatible).
    
        Each node has: ``id``, ``status``, ``role``, ``parent_id``,
        ``current_task``, ``bus_drops``, ``circuit_breaker``, ``children``.
    
        The top level of the returned list contains root-level agents
        (``parent_id == None``); each node's ``children`` list recursively
        contains its sub-agents.
        """
        agents = orchestrator.list_agents()
        return _build_agent_tree(agents)
    
    @router.get(
        "/agents/{agent_id}",
        summary="Get a single agent by ID",
        dependencies=[Depends(auth)],
    )
    async def get_agent(agent_id: str) -> dict:
        """Return the status dict for a single agent identified by *agent_id*.
    
        Returns the same field set as ``GET /agents`` but for one agent only.
        Raises 404 when *agent_id* is not registered.
    
        Design note: ``GET /collections/{id}`` is the canonical REST pattern for
        single-resource retrieval (Microsoft Azure API Design Best Practices;
        REST API Design – Vinay Sahni 2013).  Reusing the same dict shape as
        ``list_agents()`` keeps clients consistent.
    
        Reference: DESIGN.md §10.40 (v1.1.4).
        """
        uc = GetAgentUseCase(orchestrator)
        result = await uc.execute(GetAgentDTO(agent_id=agent_id))
        if not result.found:
            raise HTTPException(
                status_code=404,
                detail=f"Agent {agent_id!r} not found",
            )
        return result.to_dict()
    
    @router.delete("/agents/{agent_id}", summary="Stop an agent", dependencies=[Depends(auth)])
    async def stop_agent(agent_id: str) -> AgentKillResponse:
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        await agent.stop()
        return AgentKillResponse(agent_id=agent_id, stopped=True)
    
    @router.post("/agents/{agent_id}/reset", summary="Manually reset an agent from ERROR state", dependencies=[Depends(auth)])
    async def reset_agent(agent_id: str) -> dict:
        """Stop and restart *agent_id*, clearing ERROR and permanently-failed state.
    
        Use this endpoint when an agent exhausted automatic recovery attempts
        and needs a manual restart.  Returns 404 if the agent is not registered.
    
        Design note: ``POST /agents/{id}/reset`` follows the action sub-resource
        pattern — an imperative verb endpoint rather than a PUT state replacement.
        Reference: DESIGN.md §11; Nordic APIs "Designing a True REST State Machine".
        """
        try:
            await orchestrator.reset_agent(agent_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        return {"agent_id": agent_id, "reset": True}
    
    @router.post(
        "/agents/{agent_id}/change-strategy",
        summary="Request an autonomous strategy change for the agent's current phase",
        dependencies=[Depends(auth)],
    )
    async def change_agent_strategy(agent_id: str, body: ChangeStrategyRequest) -> dict:
        """Allow an agent to autonomously change its execution strategy.
    
        This endpoint implements §12 層3 「実行方式の自律切り替え」: when an agent
        determines that the current ``single`` execution strategy is insufficient
        for its task, it calls this endpoint to escalate to a ``parallel`` or
        ``competitive`` pattern.
    
        Behaviour by ``pattern``:
    
        - **``single``**: No-op; acknowledges the strategy (default, no spawning).
        - **``parallel``**: When ``context`` is provided, submits ``count`` identical
          tasks that will be dispatched to different agents simultaneously.  Each
          spawned task has ``reply_to`` set to the requesting agent so that results
          are delivered back to it.  When ``context`` is omitted, only the strategy
          preference is recorded (no immediate spawning).
        - **``competitive``**: Same as ``parallel`` but task prompts indicate
          competition semantics (agents solve the same problem independently; the
          best result wins).
    
        Returns
        -------
        dict
            ``{"status": "accepted", "agent_id": ..., "pattern": ..., "count": ...,
              "tags": ..., "spawned_task_ids": [...]}``
    
            ``spawned_task_ids`` is present (and non-empty) only when ``context``
            was provided and tasks were actually submitted.
    
        HTTP error codes:
        - 404: agent not found
        - 422: schema validation failure (invalid pattern or count)
    
        Design references:
        - §12「ワークフロー設計の層構造」層3 実行方式の自律切り替え
        - arXiv:2505.19591 (Evolving Orchestration 2025): dynamic orchestration
        - ALAS arXiv:2505.12501 (2025): three-layer adaptive execution framework
        - DESIGN.md §10.16 (v0.49.0)
        """
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
    
        spawned_task_ids: list[str] = []
    
        # When context is provided, immediately spawn the parallel/competitive tasks.
        if body.context is not None and body.pattern in ("parallel", "competitive"):
            count = body.count
            for i in range(count):
                if body.pattern == "competitive":
                    slot_prompt = (
                        f"You are solver #{i + 1} of {count} in a COMPETITIVE phase.\n"
                        f"Solve the following problem independently.  Write your solution "
                        f"to the scratchpad and include a numeric score or quality metric.\n\n"
                        f"## Task\n{body.context}"
                    )
                else:
                    slot_prompt = (
                        f"You are worker #{i + 1} of {count} in a PARALLEL phase.\n"
                        f"Complete the following task.  "
                        f"The requesting agent ({agent_id}) will aggregate all results.\n\n"
                        f"## Task\n{body.context}"
                    )
    
                task = await orchestrator.submit_task(
                    slot_prompt,
                    required_tags=body.tags if body.tags else None,
                    reply_to=body.reply_to,
                )
                spawned_task_ids.append(task.id)
    
            logger.info(
                "change-strategy: agent=%s pattern=%s count=%d spawned=%s",
                agent_id, body.pattern, count, spawned_task_ids,
            )
    
        response: dict = {
            "status": "accepted",
            "agent_id": agent_id,
            "pattern": body.pattern,
            "count": body.count,
            "tags": body.tags,
        }
        if spawned_task_ids:
            response["spawned_task_ids"] = spawned_task_ids
    
        return response
    
    @router.post(
        "/agents/{agent_id}/task-complete",
        summary="Signal task completion (explicit) or nudge agent (Stop hook)",
        dependencies=[Depends(auth)],
    )
    async def agent_task_complete(agent_id: str, request: Request, task_id: str | None = None) -> dict:
        """Handle task-complete signal from agent or Stop hook nudge from Claude Code.
    
        Two call sources are distinguished by the request body:
    
        **Explicit** ``/task-complete`` slash command (body has no ``stop_hook_active`` key):
        - Completes the current task via ``handle_output()``.
        - Returns ``{"status": "ok"}``.
        - Body: ``{"output": "<one-line summary>"}``
    
        **Claude Code Stop hook** (body contains ``stop_hook_active`` key):
        - ``stop_hook_active=True``: Claude is mid-tool-call continuation — skip entirely.
          Returns ``{"status": "skipped", "reason": "stop_hook_active"}``.
        - ``stop_hook_active=False``: Claude finished a response turn but the agent has
          not called ``/task-complete`` → send a nudge via ``notify_stdin``.
          Returns ``{"status": "nudged"}``.
          The task remains open; only an explicit call can complete it.
    
        HTTP error codes:
        - 404: agent not found
        - 409: agent is not in BUSY state (no active task to complete)
    
        Design references:
        - Claude Code Hooks Reference https://code.claude.com/docs/en/hooks (2025)
        - DESIGN.md §10.latest (v1.0.x Stop hook / NudgingStrategy)
        """
        from tmux_orchestrator.agents.base import AgentStatus
    
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        if agent.status != AgentStatus.BUSY:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Agent {agent_id!r} is not BUSY (status={agent.status.value!r}); "
                    "cannot complete a task that is not in progress"
                ),
            )
    
        # If the stop hook URL includes ?task_id=<id>, validate it against the
        # current task.  _update_stop_hook_for_task() writes the task_id into the
        # URL before each dispatch, so a mismatch means the hook is stale (fired
        # from a previous task).  Calls without a task_id (e.g. direct API use
        # or old-style stop hooks) are accepted as before.
        if task_id and agent._current_task and agent._current_task.id != task_id:
            logger.debug(
                "Agent %s task-complete skipped: task_id mismatch (hook=%r, current=%r)",
                agent_id, task_id, agent._current_task.id,
            )
            return {"status": "skipped", "reason": "task_id_mismatch"}
    
        # Parse optional body.
        # Claude Code Stop hook sends: {"stop_hook_active": bool, "last_assistant_message": str, ...}
        # Explicit /task-complete slash command sends: {"output": "<summary>"}
        #
        # The presence of the "stop_hook_active" key distinguishes the two sources:
        #   - Key present  → came from Stop hook → nudge the agent (never complete the task).
        #   - Key absent   → explicit /task-complete call → complete the task.
        #
        # This ensures the Stop hook is purely a nudge trigger, never a task-completion trigger.
        # Reference: DESIGN.md §10.latest (v1.0.x Stop hook / NudgingStrategy)
        nudge_requested = False
        output = ""
        try:
            body = await request.json()
            if "stop_hook_active" in body:
                # Came from Stop hook.
                if body.get("stop_hook_active"):
                    # stop_hook_active=True → Claude is mid-tool-call continuation → skip entirely.
                    return {"status": "skipped", "reason": "stop_hook_active"}
                # stop_hook_active=False → Claude finished a response turn but task still open.
                nudge_requested = True
            else:
                # Explicit /task-complete call.
                output = (
                    body.get("last_assistant_message")
                    or body.get("output")
                    or ""
                )
        except Exception:  # noqa: BLE001
            pass  # body is optional; treat as explicit call with empty output
    
        if nudge_requested:
            task_id_prefix = agent._current_task.id[:8] if agent._current_task else "?"
            nudge = (
                f"__ORCHESTRATOR__: Your task is still open (task_id={task_id_prefix}). "
                "If all work is complete and artefacts are committed, call:\n"
                "    /task-complete <one-line summary>\n"
                "If you still have work to do, please continue."
            )
            await agent.notify_stdin(nudge)
            logger.info(
                "Agent %s: Stop hook fired — nudge sent (task still open)",
                agent_id,
            )
            return {"status": "nudged"}
    
        # Capture task_id before handle_output() clears _current_task.
        completed_task_id = agent._current_task.id if agent._current_task else None
        await agent.handle_output(output)
        logger.info(
            "Agent %s task-complete received via explicit signal (task_id=%s)",
            agent_id,
            completed_task_id or "unknown",
        )
        # --- Episode auto-record (v1.0.29) ---
        # When memory_auto_record is enabled, automatically append an episode to
        # the agent's JSONL store.  The output string becomes the episode summary.
        # Reference: Wang & Chen "MIRIX" arXiv:2507.07957 (2025);
        # DESIGN.md §10.29 (v1.0.29).
        _auto_record = getattr(_orch_config, "memory_auto_record", True)
        if _auto_record and output:
            try:
                _episode_store.append(
                    agent_id,
                    summary=output[:500],  # cap at 500 chars to keep episodes compact
                    outcome="success",
                    lessons="",
                    task_id=completed_task_id,
                )
                logger.debug(
                    "Episode auto-recorded for agent %s task %s",
                    agent_id, completed_task_id,
                )
            except Exception as _ep_err:  # noqa: BLE001
                logger.warning(
                    "Episode auto-record failed for agent %s: %s", agent_id, _ep_err
                )
        return {"status": "ok"}
    
    @router.post(
        "/agents/{agent_id}/ready",
        summary="Signal agent startup readiness (called by SessionStart hook)",
        # No auth: hook fires from claude's process on the same host.
        # The endpoint only sets an asyncio.Event — no sensitive data is exposed.
    )
    async def agent_ready(agent_id: str) -> dict:
        """Set the startup-ready event for *agent_id*.
    
        Called by the ``SessionStart`` hook (via ``curl``) when Claude Code
        starts a new session.  Sets ``agent._startup_ready`` so that
        ``ClaudeCodeAgent._wait_for_ready()`` can return instead of timing out.
    
        - 404 if agent is not found.
        - 200 ``{"status": "ok"}`` on success (even if ``_startup_ready`` is
          already set or absent — idempotent by design).
        """
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        ready_event = getattr(agent, "_startup_ready", None)
        if ready_event is not None:
            ready_event.set()
        return {"status": "ok"}
    
    @router.post(
        "/agents/{agent_id}/drain",
        summary="Drain an agent — stop it after its current task completes",
        dependencies=[Depends(auth)],
    )
    async def drain_agent(agent_id: str) -> dict:
        """Put *agent_id* into graceful drain mode.
    
        - **IDLE**: immediately stops the agent and removes it from the registry.
          Returns ``{status: "stopped_immediately"}``.
        - **BUSY**: marks the agent as ``DRAINING``; it will be auto-stopped and
          removed from the registry once its current task finishes.
          Returns ``{status: "draining"}``.
        - **DRAINING / STOPPED / ERROR**: returns 409 Conflict.
    
        A STATUS event ``agent_draining`` (or ``agent_drained`` for immediate stops)
        is published to the bus.
    
        Design references:
        - Kubernetes Pod ``terminationGracePeriodSeconds``
        - HAProxy graceful restart
        - UNIX ``SO_LINGER`` graceful socket close
        - AWS ECS ``stopTimeout``
        - DESIGN.md §10.23 (v0.28.0)
        """
        try:
            result = await orchestrator.drain_agent(agent_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        status = result.get("status")
        if status in ("already_draining", "already_stopped"):
            raise HTTPException(
                status_code=409,
                detail=f"Agent {agent_id!r} cannot be drained (current status: {status})",
            )
        return result
    
    @router.get(
        "/agents/{agent_id}/drain",
        summary="Check drain status of an agent",
        dependencies=[Depends(auth)],
    )
    async def get_agent_drain_status(agent_id: str) -> dict:
        """Return the drain status of *agent_id*.
    
        Response fields:
        - ``agent_id``: the agent's ID
        - ``draining``: ``true`` if the agent is currently in DRAINING state
        - ``status``: the agent's current status value
    
        Returns 404 if the agent is not registered.
    
        Design reference: DESIGN.md §10.23 (v0.28.0).
        """
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        from tmux_orchestrator.agents.base import AgentStatus  # noqa: PLC0415
        return {
            "agent_id": agent_id,
            "draining": agent.status == AgentStatus.DRAINING,
            "status": agent.status.value,
        }
    
    
    @router.get(
        "/agents/{agent_id}/stats",
        summary="Per-agent context usage stats",
        dependencies=[Depends(auth)],
    )
    async def agent_context_stats(agent_id: str) -> dict:
        """Return context window usage statistics for *agent_id*.
    
        Fields:
        - ``pane_chars``: character count of the last captured pane output.
        - ``estimated_tokens``: estimated token count (pane_chars / 4).
        - ``context_window_tokens``: configured total context window size.
        - ``context_pct``: percentage of context window used (0-100+).
        - ``warn_threshold_pct``: threshold at which context_warning is emitted.
        - ``notes_mtime``: mtime of NOTES.md at last check (Unix timestamp).
        - ``notes_updates``: number of NOTES.md changes detected.
        - ``context_warnings``: number of context_warning events emitted.
        - ``summarize_triggers``: number of /summarize auto-injections.
        - ``last_polled``: monotonic timestamp of the last poll cycle.
        - ``worktree_path``: filesystem path to the agent's worktree (str | null).
        - ``status``: current agent status (IDLE/BUSY/STOPPED/ERROR/DRAINING).
        - ``task_count``: number of completed tasks (success + error).
        - ``error_count``: number of tasks that completed with an error.
    
        Returns 404 if the agent is not registered.
    
        Design reference: Liu et al. "Lost in the Middle" TACL 2024
        (https://arxiv.org/abs/2307.03172) — context saturation degrades recall;
        monitoring context size enables proactive compression. DESIGN.md §11 (v0.21.0).
        Design reference (enrichment): Zalando RESTful API Guidelines §compatibility —
        adding optional fields is a backward-compatible change. DESIGN.md §10 (v1.0.20).
        """
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
    
        # Build enrichment fields from registry/history regardless of context monitor.
        history = orchestrator.get_agent_history(agent_id, limit=200) or []
        task_count = len(history)
        error_count = sum(1 for r in history if r.get("status") == "error")
    
        enrichment: dict = {
            "worktree_path": (
                str(agent.worktree_path) if agent.worktree_path is not None else None
            ),
            "status": agent.status.value,
            "task_count": task_count,
            "error_count": error_count,
            "started_at": (
                agent.started_at.isoformat() if agent.started_at is not None else None
            ),
            "uptime_s": agent.uptime_s,
        }
    
        stats = orchestrator.get_agent_context_stats(agent_id)
        if stats is None:
            # Agent registered but context monitor has not polled yet;
            # return skeleton with enrichment fields.
            return {"agent_id": agent_id, **enrichment}
    
        return {**stats, **enrichment}
    
    
    @router.get(
        "/agents/{agent_id}/drift",
        summary="Per-agent behavioral drift stats",
        dependencies=[Depends(auth)],
    )
    async def agent_drift_stats(agent_id: str) -> dict:
        """Return behavioral drift statistics for *agent_id*.
    
        Fields:
        - ``drift_score``: composite drift score (0–1; lower = more drifted).
        - ``role_score``: keyword overlap between system_prompt and pane output.
        - ``idle_score``: 1 when pane is active; 0 when idle past ``drift_idle_threshold``.
        - ``length_score``: output line-count stability score.
        - ``warned``: whether the agent is currently in a drift-warned state.
        - ``drift_warnings``: cumulative count of agent_drift_warning events emitted.
        - ``drift_threshold``: the configured composite score threshold.
        - ``last_polled``: monotonic timestamp of the most recent poll.
    
        Returns 404 if the agent is unknown or not yet tracked by the drift monitor.
    
        Design reference: Rath arXiv:2601.04170 "Agent Drift" (2026) — ASI framework;
        DESIGN.md §10.20 (v1.0.9).
        """
        stats = orchestrator.get_agent_drift_stats(agent_id)
        if stats is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} drift stats not yet available")
        return stats
    
    
    @router.get(
        "/agents/{agent_id}/drift-rebriefs",
        summary="Per-agent drift auto re-brief history",
        dependencies=[Depends(auth)],
    )
    async def agent_drift_rebriefs(agent_id: str) -> list[dict]:
        """Return the list of automatic role re-briefs sent to *agent_id*.

        Each entry contains:
        - ``timestamp``: ISO-8601 UTC string when the re-brief was sent.
        - ``drift_score``: composite drift score at the time of re-brief.

        Returns an empty list if no re-briefs have been sent for this agent.

        Design reference: Rath arXiv:2601.04170 "drift-aware routing" re-brief;
        arXiv:2603.03258 "goal reminder injection";
        DESIGN.md §10.50 (v1.1.18).
        """
        return orchestrator.get_agent_drift_rebriefs(agent_id)

    @router.get(
        "/agents/{agent_id}/history",
        summary="Per-agent task history",
        dependencies=[Depends(auth)],
    )
    async def agent_history(agent_id: str, limit: int = 50) -> list:
        """Return the last *limit* completed task records for *agent_id*.
    
        Each record contains:
        - ``task_id``: unique task identifier
        - ``prompt``: the task prompt text
        - ``started_at``: ISO timestamp when the task was dispatched
        - ``finished_at``: ISO timestamp when the RESULT arrived
        - ``duration_s``: wall-clock seconds from dispatch to RESULT
        - ``status``: ``"success"`` or ``"error"``
        - ``error``: error message string, or null on success
    
        Results are ordered most-recent-first.  Pass ``?limit=N`` to control
        how many records are returned (default 50, capped at 200).
    
        Design reference: TAMAS (IBM, 2025) "Beyond Black-Box Benchmarking:
        Observability, Analytics, and Optimization of Agentic Systems"
        arXiv:2503.06745 — per-agent task history enables bottleneck analysis.
        Langfuse "AI Agent Observability" (2024): tracing decision paths.
        """
        history = orchestrator.get_agent_history(agent_id, limit=min(limit, 200))
        if history is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        return history

    @router.get(
        "/agents/{agent_id}/worktree-status",
        summary="Worktree integrity status for an agent",
        dependencies=[Depends(auth)],
    )
    async def agent_worktree_status(agent_id: str) -> dict:
        """Return the git worktree integrity status for *agent_id*.

        Checks performed:
        - Path existence (worktree directory must exist on disk)
        - ``index.lock`` presence (indicates a crashed git process)
        - HEAD resolution (``git rev-parse HEAD`` must succeed)
        - Branch name (expected ``worktree/{agent_id}``)
        - Dirty state (uncommitted changes via ``git status --porcelain``)
        - Object-store integrity (``git fsck --no-dangling``)

        Returns 404 if the agent is not registered with the orchestrator.

        Design references:
        - git-fsck(1): https://git-scm.com/docs/git-fsck
        - GitLab "Repository checks": https://docs.gitlab.com/ee/administration/repository_checks.html
        - DESIGN.md §10.17 (v0.43.0)
        """
        from tmux_orchestrator.infrastructure.worktree_integrity import WorktreeIntegrityChecker  # noqa: PLC0415

        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")

        wm = getattr(orchestrator, "_worktree_manager", None)
        worktree_path = None
        if wm is not None:
            worktree_path = wm.worktree_path(agent_id)

        repo_root = getattr(orchestrator, "_repo_root", None)
        if repo_root is None and wm is not None:
            repo_root = getattr(wm, "_repo_root", None)

        if worktree_path is None:
            from tmux_orchestrator.infrastructure.worktree_integrity import WorktreeStatus  # noqa: PLC0415
            status = WorktreeStatus(agent_id=agent_id, path=None)
            return status.to_dict()

        if repo_root is None:
            repo_root = worktree_path

        checker = WorktreeIntegrityChecker(repo_root=repo_root)
        status = await checker.check_path(agent_id, worktree_path)
        return status.to_dict()

    @router.post(
        "/agents/{agent_id}/brief",
        summary="Inject an out-of-band context brief to a running agent",
        dependencies=[Depends(auth)],
    )
    async def brief_agent(agent_id: str, body: AgentBriefRequest) -> dict:
        """Inject a context message into a running agent without interrupting its task.

        Writes ``__brief__/{brief_id}.txt`` into the agent's worktree directory
        and sends ``__BRIEF__:{brief_id}`` to the agent's tmux pane.  The agent
        can retrieve the brief content with the ``/read-brief`` slash command.

        When the agent has no worktree (``isolate: false``), the brief file is
        written to the orchestrator's current working directory instead.

        Returns:
        - ``brief_id``: UUID string identifying the brief file.
        - ``delivered``: ``true`` when ``notify_stdin`` succeeded.
        - ``worktree_path``: absolute path of the directory where the file was written.

        Returns 404 when *agent_id* is not registered.

        Design references:
        - OpenAI Agents SDK "Context Management" (2025):
          https://openai.github.io/openai-agents-python/context/
        - LangChain "Context Engineering in Agents" (2025):
          https://docs.langchain.com/oss/python/langchain/context-engineering
        - DESIGN.md §10.43 (v1.1.7)
        """
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")

        brief_id = body.brief_id or str(uuid.uuid4())

        # Determine the directory to write the brief file into.
        # Priority: agent's worktree_path > orchestrator cwd > process cwd.
        worktree_path: Path | None = getattr(agent, "worktree_path", None)
        if worktree_path is None:
            # isolate: false — fall back to orchestrator working directory
            worktree_path = Path.cwd()

        brief_dir = worktree_path / "__brief__"
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, brief_dir.mkdir, 0o755, True, True)

        brief_file = brief_dir / f"{brief_id}.txt"
        await loop.run_in_executor(None, brief_file.write_text, body.content, "utf-8")

        # Notify the agent via its tmux pane (same mechanism as P2P __MSG__).
        delivered = False
        try:
            await agent.notify_stdin(f"__BRIEF__:{brief_id}")
            delivered = True
        except Exception:  # noqa: BLE001
            logger.warning(
                "brief_agent: notify_stdin failed for agent %s brief %s",
                agent_id,
                brief_id,
            )

        return {
            "brief_id": brief_id,
            "delivered": delivered,
            "worktree_path": str(worktree_path),
        }

    @router.post("/agents/{agent_id}/message", summary="Send a message to an agent", dependencies=[Depends(auth)])
    async def send_message(agent_id: str, body: SendMessage) -> dict:
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        try:
            msg_type = MessageType[body.type]
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Unknown message type: {body.type!r}")
        msg = Message(
            type=msg_type,
            from_id="__user__",
            to_id=agent_id,
            payload=body.payload,
        )
        await orchestrator.bus.publish(msg)
        return {"message_id": msg.id, "to_id": agent_id}
    
    @router.post("/agents/new", summary="Create a new agent dynamically (no template required)", dependencies=[Depends(auth)])
    async def create_dynamic_agent(body: DynamicAgentCreate) -> dict:
        """Create and start a new ClaudeCodeAgent with the given parameters.
    
        Unlike ``POST /agents`` (which requires a pre-configured template_id),
        this endpoint accepts the full agent specification inline so a Director
        agent can spawn specialist workers at runtime.
    
        Returns the assigned agent ID and a ``"created"`` status.  Returns 409
        if an agent with the requested *agent_id* already exists.
        """
        try:
            agent = await orchestrator.create_agent(
                agent_id=body.agent_id,
                tags=body.tags or [],
                system_prompt=body.system_prompt,
                isolate=body.isolate,
                merge_on_stop=body.merge_on_stop,
                merge_target=body.merge_target,
                command=body.command,
                role=body.role,
                task_timeout=body.task_timeout,
                parent_id=body.parent_id,
            )
            return {"status": "created", "agent_id": agent.id}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    
    @router.post("/agents", summary="Spawn a sub-agent under a parent agent", dependencies=[Depends(auth)])
    async def spawn_agent(body: SpawnAgent) -> dict:
        parent = orchestrator.get_agent(body.parent_id)
        if parent is None:
            raise HTTPException(
                status_code=404, detail=f"Agent {body.parent_id!r} not found"
            )
        msg = Message(
            type=MessageType.CONTROL,
            from_id=body.parent_id,
            to_id="__orchestrator__",
            payload={
                "action": "spawn_subagent",
                "template_id": body.template_id,
            },
        )
        await orchestrator.bus.publish(msg)
        return {"status": "spawning", "parent_id": body.parent_id, "template_id": body.template_id}
    
    @router.post("/director/chat", summary="Send a message to the Director agent", dependencies=[Depends(auth)])
    async def director_chat(body: DirectorChat, wait: bool = False) -> dict:
        director = orchestrator.get_director()
        if director is None:
            raise HTTPException(status_code=404, detail="No director agent in this session")
    
        task_id = str(uuid.uuid4())
    
        # Prepend any buffered worker results so the Director sees them as context
        pending = orchestrator.flush_director_pending()
        if pending:
            notifications = "\n".join(f"  - {p}" for p in pending)
            prompt = f"[Completed worker tasks since last message]\n{notifications}\n\n{body.message}"
        else:
            prompt = body.message
    
        task = Task(id=task_id, prompt=prompt, priority=0)
    
        if wait:
            sub_id = f"__chat_{task_id[:8]}__"
            q = await orchestrator.bus.subscribe(sub_id, broadcast=True)
            await director.send_task(task)
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=300.0)
                    except asyncio.TimeoutError:
                        raise HTTPException(status_code=504, detail="Director response timed out")
                    q.task_done()
                    if msg.type == MessageType.RESULT and msg.payload.get("task_id") == task_id:
                        return {"task_id": task_id, "response": msg.payload.get("output", "")}
            finally:
                await orchestrator.bus.unsubscribe(sub_id)
        else:
            await director.send_task(task)
            return {"task_id": task_id}
    
    # ------------------------------------------------------------------
    # Shared scratchpad — key/value store for inter-agent data sharing
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Context compression — TF-IDF extractive compression
    # (DESIGN.md §10.36 v1.1.11)
    # ------------------------------------------------------------------

    # Per-agent cumulative compression stats (in-process, reset on restart).
    _compression_stats: dict[str, dict] = {}

    @router.post(
        "/agents/{agent_id}/compress-context",
        summary="Compress agent pane context via TF-IDF relevance scoring",
        dependencies=[Depends(auth)],
    )
    async def compress_agent_context(
        agent_id: str, body: CompressContextRequest
    ) -> dict:
        """Capture the agent's pane output and compress it with TF-IDF.

        Returns compression statistics and the compressed text.  Does NOT
        alter the agent's running Claude session — the compressed text is
        returned for the caller to use (e.g. to write to NOTES.md).

        Algorithm:
        1. Capture the current tmux pane output.
        2. Tokenise each line and build a TF-IDF matrix (no external deps).
        3. Score each line by cosine similarity to *query*.
        4. Drop lines whose score is in the bottom *drop_percentile*.
        5. Optionally reorder surviving lines (highest score first).

        Design reference: Liu et al. "Lost in the Middle" TACL 2024;
        DESIGN.md §10.36 v1.1.11.
        """
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")

        pane = agent.pane
        if pane is None:
            raise HTTPException(
                status_code=400,
                detail=f"Agent {agent_id!r} has no active tmux pane",
            )

        # Capture pane text in executor to avoid blocking event loop
        loop = asyncio.get_running_loop()
        try:
            tmux = orchestrator.tmux
            pane_text: str = await loop.run_in_executor(
                None, _capture_pane_text, tmux, pane
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("compress-context: pane capture failed for %s: %s", agent_id, exc)
            raise HTTPException(
                status_code=500, detail=f"Failed to capture pane: {exc}"
            ) from exc

        compressor = TfIdfContextCompressor(
            drop_percentile=body.drop_percentile,
            reorder=body.reorder,
        )
        result = compressor.compress(pane_text, query=body.task_query)
        ratio = compressor.compression_ratio(result)

        # Accumulate cumulative stats
        entry = _compression_stats.setdefault(
            agent_id,
            {
                "agent_id": agent_id,
                "total_compressions": 0,
                "total_lines_dropped": 0,
                "total_chars_saved": 0,
                "_ratio_sum": 0.0,
            },
        )
        entry["total_compressions"] += 1
        entry["total_lines_dropped"] += result.dropped_lines
        entry["total_chars_saved"] += result.original_chars - result.compressed_chars
        entry["_ratio_sum"] += ratio

        return {
            "agent_id": agent_id,
            "original_lines": result.original_lines,
            "kept_lines": result.kept_lines,
            "dropped_lines": result.dropped_lines,
            "original_chars": result.original_chars,
            "compressed_chars": result.compressed_chars,
            "compression_ratio": round(ratio, 4),
            "drop_percentile": result.drop_percentile,
            "reordered": result.reordered,
            "query": body.task_query,
            "compressed_text": result.compressed_text,
        }

    @router.get(
        "/agents/{agent_id}/compression-stats",
        summary="Cumulative context compression statistics for an agent",
        dependencies=[Depends(auth)],
    )
    async def agent_compression_stats(agent_id: str) -> dict:
        """Return cumulative context compression statistics for *agent_id*.

        Fields:
        - ``agent_id``: the agent identifier.
        - ``total_compressions``: number of POST /compress-context calls.
        - ``total_lines_dropped``: cumulative lines removed across all calls.
        - ``total_chars_saved``: cumulative characters removed.
        - ``avg_compression_ratio``: average char-level compression ratio.

        Returns 404 if the agent does not exist.  Returns zeroed stats if the
        agent exists but has not had any compression passes yet.

        Design reference: DESIGN.md §10.36 v1.1.11.
        """
        agent = orchestrator.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")

        entry = _compression_stats.get(agent_id)
        if entry is None:
            return {
                "agent_id": agent_id,
                "total_compressions": 0,
                "total_lines_dropped": 0,
                "total_chars_saved": 0,
                "avg_compression_ratio": 0.0,
            }

        n = entry["total_compressions"]
        avg_ratio = (entry["_ratio_sum"] / n) if n > 0 else 0.0
        return {
            "agent_id": agent_id,
            "total_compressions": n,
            "total_lines_dropped": entry["total_lines_dropped"],
            "total_chars_saved": entry["total_chars_saved"],
            "avg_compression_ratio": round(avg_ratio, 4),
        }

    return router


# ---------------------------------------------------------------------------
# Module-level helper (patchable in tests)
# ---------------------------------------------------------------------------


def _capture_pane_text(tmux: Any, pane: Any) -> str:
    """Capture and return text from *pane* via *tmux*.

    Separated from the router closure so tests can monkeypatch it.
    """
    return tmux.capture_pane(pane)
