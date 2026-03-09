"""Queue-depth-triggered agent pool autoscaler.

Monitors the orchestrator's task queue depth and dynamically creates or stops
agents to maintain throughput.  Scaling decisions are based on:

- **Scale-up**: queue_depth > autoscale_threshold * idle_agent_count
- **Scale-down**: queue is empty and all autoscaled agents have been idle for
  at least autoscale_cooldown seconds.

Design references
-----------------
- Kubernetes Horizontal Pod Autoscaler (HPA):
  https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/
  Queue-depth-based scaling maps to HPA ``AverageValue`` metric target on a
  custom ``queue_depth`` metric.  HPA uses a ``cooldownPeriod`` (default 5 min)
  to prevent rapid scale-up/down oscillations — this autoscaler replicates that
  via ``autoscale_cooldown`` and ``autoscale_poll``.

- Thijssen "Autonomic Computing" (MIT Press, 2009):
  The MAPE-K loop (Monitor–Analyze–Plan–Execute over a shared Knowledge base)
  is the canonical autonomic computing reference.  ``_scale_loop()`` implements
  a simplified MAPE cycle: Monitor (queue_depth, idle count) → Analyze
  (threshold comparisons) → Plan (scale up/down decision) → Execute
  (create_agent / stop_agent).

- AWS Auto Scaling Groups — cooldown periods:
  https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-cooldowns.html
  AWS recommends a default 300-second cooldown after a scale-out to prevent
  consecutive launch storms.  This autoscaler uses a configurable
  ``autoscale_cooldown`` (default 30 s) for scale-down only, since scale-up
  is triggered only when the queue is actively growing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tmux_orchestrator.config import OrchestratorConfig
    from tmux_orchestrator.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class AutoScaler:
    """Queue-depth-triggered agent pool autoscaler.

    Creates new agents via ``Orchestrator.create_agent()`` when queue depth
    exceeds ``autoscale_threshold * idle_agent_count``, and stops idle agents
    back to ``autoscale_min`` when the queue drains.

    A cooldown period (``autoscale_cooldown`` seconds) prevents thrashing
    (rapid create/destroy cycles).

    Parameters
    ----------
    orchestrator:
        The running ``Orchestrator`` instance.
    config:
        ``OrchestratorConfig`` — reads ``autoscale_*`` fields.
    """

    def __init__(self, orchestrator: "Orchestrator", config: "OrchestratorConfig") -> None:
        self._orch = orchestrator
        self._min: int = config.autoscale_min
        self._max: int = config.autoscale_max
        self._threshold: int = config.autoscale_threshold
        self._cooldown: float = config.autoscale_cooldown
        self._poll: float = config.autoscale_poll
        self._agent_tags: list[str] = list(config.autoscale_agent_tags)
        self._system_prompt: str | None = config.autoscale_system_prompt

        # IDs of agents created by *this* autoscaler (not pre-registered ones)
        self._autoscaled_ids: set[str] = set()

        self._last_scale_up: float | None = None
        self._last_scale_down: float | None = None
        # Timestamp when the queue first became empty (for cooldown tracking)
        self._queue_empty_since: float | None = None

        self._task: asyncio.Task | None = None
        self._enabled: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background autoscale loop."""
        if self._task is not None:
            return
        self._enabled = True
        self._task = asyncio.create_task(self._scale_loop(), name="autoscaler")
        logger.info(
            "AutoScaler started (min=%d, max=%d, threshold=%d, cooldown=%.1f)",
            self._min, self._max, self._threshold, self._cooldown,
        )

    def stop(self) -> None:
        """Stop the autoscale loop (does not stop autoscaled agents)."""
        self._enabled = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        logger.info("AutoScaler stopped")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def status(self) -> dict:
        """Return current autoscaler state as a JSON-serialisable dict."""
        return {
            "enabled": self._enabled,
            "agent_count": len(self._autoscaled_ids),
            "queue_depth": self._orch.queue_depth(),
            "last_scale_up": self._last_scale_up,
            "last_scale_down": self._last_scale_down,
            "autoscaled_ids": sorted(self._autoscaled_ids),
            "min": self._min,
            "max": self._max,
            "threshold": self._threshold,
            "cooldown": self._cooldown,
        }

    # ------------------------------------------------------------------
    # Reconfiguration
    # ------------------------------------------------------------------

    def reconfigure(
        self,
        *,
        min: int | None = None,
        max: int | None = None,
        threshold: int | None = None,
        cooldown: float | None = None,
    ) -> dict:
        """Update scaling parameters at runtime (no restart needed)."""
        if min is not None:
            self._min = min
        if max is not None:
            self._max = max
        if threshold is not None:
            self._threshold = threshold
        if cooldown is not None:
            self._cooldown = cooldown
        logger.info(
            "AutoScaler reconfigured (min=%d, max=%d, threshold=%d, cooldown=%.1f)",
            self._min, self._max, self._threshold, self._cooldown,
        )
        return {
            "min": self._min,
            "max": self._max,
            "threshold": self._threshold,
            "cooldown": self._cooldown,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _idle_autoscaled_count(self) -> int:
        """Return the number of autoscaled agents that are currently IDLE."""
        from tmux_orchestrator.agents.base import AgentStatus
        count = 0
        for aid in list(self._autoscaled_ids):
            agent = self._orch.registry.get(aid)
            if agent is not None and agent.status == AgentStatus.IDLE:
                count += 1
        return count

    def _total_idle_count(self) -> int:
        """Return the total number of IDLE agents (including pre-registered)."""
        from tmux_orchestrator.agents.base import AgentStatus
        return sum(
            1 for a in self._orch.registry.all_agents().values()
            if a.status == AgentStatus.IDLE
        )

    def _active_autoscaled_count(self) -> int:
        """Return the number of autoscaled agents still registered."""
        return sum(
            1 for aid in self._autoscaled_ids
            if self._orch.registry.get(aid) is not None
        )

    async def _maybe_scale_up(self, queue_depth: int) -> None:
        """Create a new agent if queue depth exceeds threshold * idle count."""
        current_count = self._active_autoscaled_count()
        if current_count >= self._max:
            logger.debug(
                "AutoScaler: at max (%d), skipping scale-up (queue=%d)",
                self._max, queue_depth,
            )
            return

        idle_count = self._total_idle_count()
        # Effective threshold: queue must exceed threshold * idle agents
        # (at least 1 to handle the zero-idle cold-start case)
        effective_threshold = max(1, self._threshold * max(1, idle_count))
        if queue_depth <= effective_threshold:
            return

        logger.info(
            "AutoScaler: scaling up (queue=%d > threshold=%d, autoscaled=%d/%d)",
            queue_depth, effective_threshold, current_count, self._max,
        )
        try:
            agent = await self._orch.create_agent(
                tags=self._agent_tags or None,
                system_prompt=self._system_prompt,
                isolate=False,  # autoscaled agents share workspace by default
            )
            self._autoscaled_ids.add(agent.id)
            self._last_scale_up = time.time()
            logger.info("AutoScaler: created agent %s", agent.id)
        except Exception:
            logger.exception("AutoScaler: create_agent failed")

    async def _maybe_scale_down(self, queue_depth: int) -> None:
        """Stop idle autoscaled agents when queue drains and cooldown expires."""
        current_count = self._active_autoscaled_count()
        if current_count <= self._min:
            return

        if queue_depth > 0:
            # Queue not empty — reset cooldown timer
            self._queue_empty_since = None
            return

        now = time.time()
        if self._queue_empty_since is None:
            self._queue_empty_since = now
            return

        elapsed = now - self._queue_empty_since
        if elapsed < self._cooldown:
            logger.debug(
                "AutoScaler: cooldown in progress (%.1fs / %.1fs)",
                elapsed, self._cooldown,
            )
            return

        # Cooldown has expired — stop one idle autoscaled agent per cycle
        idle_autoscaled = [
            aid for aid in list(self._autoscaled_ids)
            if self._orch.registry.get(aid) is not None
            and self._is_idle(aid)
        ]
        if not idle_autoscaled:
            return

        # Stop agents until we reach the minimum, one per scale-down cycle
        to_stop_id = idle_autoscaled[0]
        agent = self._orch.registry.get(to_stop_id)
        if agent is None:
            self._autoscaled_ids.discard(to_stop_id)
            return

        logger.info(
            "AutoScaler: scaling down — stopping agent %s (autoscaled=%d, min=%d)",
            to_stop_id, current_count, self._min,
        )
        try:
            await agent.stop()
            self._orch.registry.unregister(to_stop_id)
        except Exception:
            logger.exception("AutoScaler: stop agent %s failed", to_stop_id)
        finally:
            self._autoscaled_ids.discard(to_stop_id)
            self._last_scale_down = time.time()
            # Reset timer after each stop so we wait a full cooldown between
            # consecutive scale-down steps (prevents rapid multi-stop)
            self._queue_empty_since = None

    def _is_idle(self, agent_id: str) -> bool:
        from tmux_orchestrator.agents.base import AgentStatus
        agent = self._orch.registry.get(agent_id)
        return agent is not None and agent.status == AgentStatus.IDLE

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _scale_loop(self) -> None:
        """Periodically evaluate scaling decisions."""
        while True:
            try:
                queue_depth = self._orch.queue_depth()
                await self._maybe_scale_up(queue_depth)
                await self._maybe_scale_down(queue_depth)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AutoScaler: unexpected error in scale loop")
            await asyncio.sleep(self._poll)
