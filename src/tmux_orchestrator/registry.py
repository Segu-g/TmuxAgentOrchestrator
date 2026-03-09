"""Agent registry: identity, hierarchy, P2P permissions, and circuit breakers.

Extracts the agent-state concern from ``Orchestrator`` following the DDD
Aggregate pattern.  The registry is intentionally synchronous and has no
async I/O — it is a pure in-memory value object that the Orchestrator
delegates all agent queries to.

Design decision: the registry does NOT start/stop agents.  Lifecycle
management (agent.start() / agent.stop()) remains with the Orchestrator,
which coordinates agent lifecycle with the task queue and routing loop.

Reference: Evans "Domain-Driven Design" (2003) Ch. 6 — Aggregates;
           research survey DESIGN.md §10.3 (2026-03-05).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from tmux_orchestrator.agents.base import Agent, AgentStatus
from tmux_orchestrator.circuit_breaker import CircuitBreaker
from tmux_orchestrator.config import AgentRole

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Encapsulates all agent-related state for the orchestrator.

    Responsibilities:
    - Maintain the set of registered agents and their parent relationships.
    - Manage per-agent circuit breakers.
    - Evaluate P2P message permission (explicit table + hierarchy rules).
    - Provide filtered views: idle workers, director agent, serialised list.
    """

    def __init__(
        self,
        *,
        p2p_permissions: list[tuple[str, str]],
        circuit_breaker_threshold: int = 3,
        circuit_breaker_recovery: float = 60.0,
    ) -> None:
        self._agents: dict[str, Agent] = {}
        self._agent_parents: dict[str, str] = {}
        self._p2p: set[frozenset[str]] = {
            frozenset(pair) for pair in p2p_permissions
        }
        self._breakers: dict[str, CircuitBreaker] = {}
        self._cb_threshold = circuit_breaker_threshold
        self._cb_recovery = circuit_breaker_recovery
        # Watchdog tracking: agent_id → monotonic timestamp of when dispatch started
        self._busy_since: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, agent: Agent, *, parent_id: str | None = None) -> None:
        """Register *agent* and create its circuit breaker.

        If *parent_id* is given the agent is recorded as a sub-agent of that
        parent, enabling automatic hierarchy-based P2P routing.
        """
        self._agents[agent.id] = agent
        if parent_id is not None:
            self._agent_parents[agent.id] = parent_id
        self._breakers[agent.id] = CircuitBreaker(
            agent.id,
            failure_threshold=self._cb_threshold,
            recovery_timeout=self._cb_recovery,
        )
        logger.debug("Registry: registered %s (parent=%s)", agent.id, parent_id)

    def unregister(self, agent_id: str) -> None:
        """Remove *agent_id* from the registry entirely."""
        self._agents.pop(agent_id, None)
        self._agent_parents.pop(agent_id, None)
        self._breakers.pop(agent_id, None)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, agent_id: str) -> Agent | None:
        """Return the agent for *agent_id*, or None."""
        return self._agents.get(agent_id)

    def all_agents(self) -> dict[str, Agent]:
        """Return a shallow copy of the current {id: Agent} mapping."""
        return dict(self._agents)

    def get_director(self) -> Agent | None:
        """Return the director agent, or None if none is registered."""
        return next(
            (
                a
                for a in self._agents.values()
                if getattr(a, "role", "") == AgentRole.DIRECTOR
            ),
            None,
        )

    def find_idle_worker(
        self,
        required_tags: list[str] | None = None,
        allowed_agent_ids: set[str] | None = None,
    ) -> Agent | None:
        """Return the first IDLE worker whose circuit is not OPEN, or None.

        When *required_tags* is non-empty, only agents whose ``tags`` attribute
        is a superset of *required_tags* are eligible.

        When *allowed_agent_ids* is non-None, only agents whose ID appears in
        that set are eligible (used to implement named group dispatch).

        Design reference:
        - FIPA Directory Facilitator (2002) — capability-based agent selection.
        - Kubernetes Node Affinity nodeSelector — label-set subset matching.
        - Kubernetes Node Pools / Node Groups — restrict scheduling to a pool.
        - DESIGN.md §10.14 (v0.18.0, 2026-03-05); §10.26 (v0.31.0).
        """
        needed: set[str] = set(required_tags) if required_tags else set()
        for agent in self._agents.values():
            if agent.status != AgentStatus.IDLE:
                continue
            if getattr(agent, "role", AgentRole.WORKER) != AgentRole.WORKER:
                continue
            if not self._breakers.get(agent.id, CircuitBreaker(agent.id)).is_allowed():
                continue
            if needed and not needed.issubset(set(getattr(agent, "tags", []))):
                continue
            if allowed_agent_ids is not None and agent.id not in allowed_agent_ids:
                continue
            return agent
        return None

    def list_all(self, drop_counts: dict[str, int] | None = None) -> list[dict]:
        """Return a JSON-serialisable snapshot of all registered agents.

        *drop_counts* should be ``Bus.get_drop_counts()`` from the caller;
        accepted as a plain dict so this class has no hard dependency on Bus.
        """
        drops = drop_counts or {}
        return [
            {
                "id": a.id,
                "status": a.status.value,
                "current_task": a._current_task.id if a._current_task else None,
                "role": getattr(a, "role", AgentRole.WORKER),
                "parent_id": self._agent_parents.get(a.id),
                "tags": list(getattr(a, "tags", [])),
                "bus_drops": drops.get(a.id, 0),
                "circuit_breaker": (
                    self._breakers[a.id].state.value
                    if a.id in self._breakers
                    else None
                ),
                "worktree_path": (
                    str(a.worktree_path) if a.worktree_path is not None else None
                ),
            }
            for a in self._agents.values()
        ]

    # ------------------------------------------------------------------
    # P2P permission
    # ------------------------------------------------------------------

    def grant_p2p(self, id_a: str, id_b: str) -> None:
        """Explicitly grant bidirectional P2P messaging between two agents."""
        self._p2p.add(frozenset({id_a, id_b}))

    def is_p2p_permitted(self, from_id: str, to_id: str) -> tuple[bool, str]:
        """Evaluate whether a PEER_MSG from *from_id* to *to_id* is allowed.

        Returns ``(permitted, reason)`` where *reason* is one of:
        ``"user"``, ``"explicit"``, ``"hierarchy"``, ``"blocked"``.

        Permission rules (first match wins):
        1. ``from_id == "__user__"`` — always allowed (Web API bypass).
        2. The pair appears in the explicit ``p2p_permissions`` table.
        3. The agents share a natural hierarchy relationship.
        """
        if from_id == "__user__":
            return True, "user"
        if frozenset({from_id, to_id}) in self._p2p:
            return True, "explicit"
        if self._is_hierarchy_permitted(from_id, to_id):
            return True, "hierarchy"
        return False, "blocked"

    def _is_hierarchy_permitted(self, from_id: str, to_id: str) -> bool:
        """True when agents are parent↔child or siblings (same parent / both root-level)."""
        if from_id not in self._agents or to_id not in self._agents:
            return False
        from_parent = self._agent_parents.get(from_id)
        to_parent = self._agent_parents.get(to_id)
        if from_id == to_parent:    # parent → child
            return True
        if to_id == from_parent:    # child → parent
            return True
        if from_parent == to_parent:  # siblings (incl. both root-level None)
            return True
        return False

    # ------------------------------------------------------------------
    # Circuit breaker interface
    # ------------------------------------------------------------------

    def record_busy(self, agent_id: str) -> None:
        """Record that *agent_id* started executing a task (for watchdog tracking)."""
        self._busy_since[agent_id] = time.monotonic()

    def find_timed_out_agents(self, task_timeout: float) -> list[str]:
        """Return agent IDs that have been BUSY for more than 1.5× *task_timeout*.

        The 1.5× multiplier gives the per-agent ``asyncio.wait_for`` timeout a
        chance to fire first; the watchdog is a belt-and-suspenders backstop.
        """
        deadline = task_timeout * 1.5
        now = time.monotonic()
        return [
            aid for aid, t in self._busy_since.items()
            if now - t > deadline
        ]

    def record_result(self, agent_id: str, *, error: bool) -> None:
        """Update the circuit breaker for *agent_id* based on a task outcome."""
        self._busy_since.pop(agent_id, None)  # clear watchdog timestamp
        cb = self._breakers.get(agent_id)
        if cb is None:
            return
        if error:
            cb.record_failure()
        else:
            cb.record_success()

    def get_breaker(self, agent_id: str) -> CircuitBreaker | None:
        """Return the circuit breaker for *agent_id*, or None."""
        return self._breakers.get(agent_id)
