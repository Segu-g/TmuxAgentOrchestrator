"""Behavioral drift detection for ClaudeCodeAgent instances.

Agent drift is the progressive degradation of agent behavior, decision quality,
and inter-agent coherence over extended interaction sequences.  This module
implements a lightweight, dependency-free subset of the Agent Stability Index
(ASI) from:

  Rath, "Agent Drift: Quantifying Behavioral Degradation in Multi-Agent LLM
  Systems Over Extended Interactions," arXiv:2601.04170, January 2026.

Three sub-scores are computed on each poll cycle:

1. **Role score** (weight 0.50)
   Measures keyword overlap between the agent's ``system_prompt`` and the
   current pane output.  When an agent stops using the vocabulary of its
   designated role, this score falls.  Corresponds to the ASI "Role Adherence"
   dimension (Inter-Agent Coordination category, ASI 0.25 weight).

2. **Idle score** (weight 0.30)
   Measures how long the agent's pane output has been unchanged.  A pane that
   has not changed for more than ``idle_threshold`` seconds scores 0.  A freshly
   updated pane scores 1.  Corresponds to the UEBA "response latency" anomaly
   from "Behavioral Monitoring & Anomaly Detection for Agents" (tekysinfo, 2025).

3. **Length score** (weight 0.20)
   Measures variance in the line count of captured pane output over a rolling
   window.  High variance indicates erratic output volume.  Corresponds to the
   ASI "Output Length Stability" dimension (Behavioral Boundaries category).

A composite ``drift_score`` (weighted average, range [0, 1]) is compared to a
configurable ``drift_threshold`` (default 0.6).  When the score drops below
the threshold, an ``agent_drift_warning`` STATUS event is published on the bus.
The warning is published at most once per warning window; the flag resets
automatically when the score recovers above the threshold.

Design references:
- Rath arXiv:2601.04170 "Agent Drift" (2026) — ASI framework
- "Behavioral Monitoring & Anomaly Detection for Agents" tekysinfo.com (2025)
- Monitoring LLM-based Multi-Agent Systems arXiv:2510.19420 (2025)
- DESIGN.md §10.20 (v1.0.9)
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tmux_orchestrator.bus import Message, MessageType

if TYPE_CHECKING:
    from tmux_orchestrator.agents.base import Agent
    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.tmux_interface import TmuxInterface

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default composite drift score threshold below which agent_drift_warning fires.
# Derived from ASI τ=0.75 in arXiv:2601.04170, relaxed slightly to 0.6 to
# reduce false positives in short-lived demo agents.
_DEFAULT_DRIFT_THRESHOLD: float = 0.6

# Default poll interval (seconds).
_DEFAULT_POLL: float = 10.0

# Default idle threshold — pane unchanged for this many seconds → idle_score = 0.
_DEFAULT_IDLE_THRESHOLD: float = 300.0  # 5 minutes

# Rolling window size for length_score variance.
_LENGTH_HISTORY_WINDOW: int = 10

# Minimum keyword length to include in role_score (filters stop words).
_MIN_KEYWORD_LEN: int = 4

# Sub-score weights — must sum to 1.0.
_ROLE_WEIGHT: float = 0.50
_IDLE_WEIGHT: float = 0.30
_LENGTH_WEIGHT: float = 0.20


# ---------------------------------------------------------------------------
# Public scoring functions (importable for tests)
# ---------------------------------------------------------------------------


def _compute_role_score(system_prompt: str, pane_output: str) -> float:
    """Compute keyword overlap between *system_prompt* and *pane_output*.

    Returns 1.0 when there is no role constraint (empty prompt) or when all
    keywords are present.  Returns 0.0 when none of the keywords appear in the
    output.

    Parameters
    ----------
    system_prompt:
        The agent's assigned role description.  Short stop words (< 4 chars)
        are ignored.
    pane_output:
        The current captured pane text to evaluate.
    """
    if not system_prompt.strip():
        return 1.0  # No role → no drift possible

    # Tokenise and filter stop words
    tokens = re.findall(r"[a-zA-Z]+", system_prompt.lower())
    keywords = [t for t in tokens if len(t) >= _MIN_KEYWORD_LEN]
    if not keywords:
        return 1.0  # All words were stop words → no constraint

    if not pane_output.strip():
        return 0.0  # Role specified but pane is empty

    output_lower = pane_output.lower()
    present = sum(1 for kw in keywords if kw in output_lower)
    return present / len(keywords)


def _compute_idle_score(last_change_time: float, idle_threshold: float) -> float:
    """Compute idle score based on time since last pane output change.

    Returns 1.0 if the pane changed recently (within ``idle_threshold``),
    decreasing linearly to 0.0 at ``idle_threshold`` seconds, clamped at 0.

    Parameters
    ----------
    last_change_time:
        Monotonic timestamp of the last detected pane output change.
    idle_threshold:
        Seconds after which idle_score reaches 0.0.
    """
    elapsed = time.monotonic() - last_change_time
    if idle_threshold <= 0:
        return 1.0
    score = 1.0 - (elapsed / idle_threshold)
    return max(0.0, min(1.0, score))


def _compute_length_score(history: list[int]) -> float:
    """Compute stability score from rolling line-count variance.

    Returns 1.0 for perfectly stable (zero variance) output.  High variance
    relative to the mean drives the score toward 0.

    Parameters
    ----------
    history:
        List of line counts from recent poll cycles (oldest first).
    """
    if len(history) < 2:
        return 1.0

    mean = sum(history) / len(history)
    if mean == 0:
        return 1.0

    variance = sum((x - mean) ** 2 for x in history) / len(history)
    std = math.sqrt(variance)
    # Coefficient of variation clamped to [0, 1]; high CV → low score
    cv = std / (mean + 1.0)  # +1 avoids division by zero for tiny means
    return max(0.0, 1.0 - min(1.0, cv))


def _composite_score(role: float, idle: float, length: float) -> float:
    """Weighted composite of the three sub-scores."""
    return _ROLE_WEIGHT * role + _IDLE_WEIGHT * idle + _LENGTH_WEIGHT * length


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class AgentDriftStats:
    """Per-agent drift tracking state."""

    agent_id: str
    # Latest sub-scores
    role_score: float = 1.0
    idle_score: float = 1.0
    length_score: float = 1.0
    drift_score: float = 1.0
    # Whether the agent is currently in a warned state.
    warned: bool = False
    # Cumulative drift warning count.
    drift_warnings: int = 0
    # Rolling line-count history for length_score computation.
    line_count_history: list[int] = field(default_factory=list)
    # Monotonic timestamp of the last detected pane output change.
    last_change_time: float = field(default_factory=time.monotonic)
    # Last captured pane text (to detect changes).
    last_pane_text: str = ""
    # Monotonic timestamp of the most recent poll.
    last_polled: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# DriftMonitor
# ---------------------------------------------------------------------------


class DriftMonitor:
    """Polls agent panes and publishes drift warning events.

    Parameters
    ----------
    bus:
        The shared message bus.  Events are published as ``MessageType.STATUS``
        messages from ``__drift_monitor__``.
    tmux:
        TmuxInterface used to capture pane text.
    agents:
        Callable that returns the current list of live agents.
    drift_threshold:
        Composite score below which ``agent_drift_warning`` fires.
        Default 0.6.
    idle_threshold:
        Seconds of unchanged pane output before idle_score reaches 0.
        Default 300 (5 minutes).
    poll_interval:
        Seconds between polling cycles.  Default 10.
    """

    def __init__(
        self,
        bus: "Bus",
        tmux: "TmuxInterface",
        agents: Any,  # Callable[[], list[Agent]]
        *,
        drift_threshold: float = _DEFAULT_DRIFT_THRESHOLD,
        idle_threshold: float = _DEFAULT_IDLE_THRESHOLD,
        poll_interval: float = _DEFAULT_POLL,
    ) -> None:
        self._bus = bus
        self._tmux = tmux
        self._get_agents = agents
        self._drift_threshold = drift_threshold
        self._idle_threshold = idle_threshold
        self._poll_interval = poll_interval
        self._stats: dict[str, AgentDriftStats] = {}
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the background polling coroutine."""
        self._task = asyncio.create_task(self._poll_loop(), name="drift-monitor")
        logger.info(
            "DriftMonitor started (poll=%.1fs, threshold=%.2f, idle=%.0fs)",
            self._poll_interval, self._drift_threshold, self._idle_threshold,
        )

    def stop(self) -> None:
        """Cancel the background polling coroutine."""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("DriftMonitor stopped")

    # ------------------------------------------------------------------
    # Public stats access
    # ------------------------------------------------------------------

    def get_drift_stats(self, agent_id: str) -> dict[str, Any] | None:
        """Return a JSON-serialisable drift stats dict, or None."""
        s = self._stats.get(agent_id)
        if s is None:
            return None
        return {
            "agent_id": s.agent_id,
            "drift_score": round(s.drift_score, 4),
            "role_score": round(s.role_score, 4),
            "idle_score": round(s.idle_score, 4),
            "length_score": round(s.length_score, 4),
            "warned": s.warned,
            "drift_warnings": s.drift_warnings,
            "drift_threshold": self._drift_threshold,
            "last_polled": s.last_polled,
        }

    def all_drift_stats(self) -> list[dict[str, Any]]:
        """Return drift stats for all tracked agents."""
        result = []
        for aid in self._stats:
            stats = self.get_drift_stats(aid)
            if stats is not None:
                result.append(stats)
        return result

    # ------------------------------------------------------------------
    # Internal — poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Main polling coroutine — runs until cancelled."""
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._poll_all()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception("DriftMonitor poll error")

    async def _poll_all(self) -> None:
        """Poll every live agent once."""
        loop = asyncio.get_running_loop()
        agents: list[Agent] = self._get_agents()
        for agent in agents:
            try:
                await self._poll_agent(agent, loop)
            except Exception:  # noqa: BLE001
                logger.exception("DriftMonitor: error polling agent %s", agent.id)

    async def _poll_agent(self, agent: "Agent", loop: asyncio.AbstractEventLoop) -> None:
        """Poll a single agent and update its drift stats."""
        if agent.pane is None:
            return  # Cannot monitor agents without a tmux pane

        agent_id = agent.id
        if agent_id not in self._stats:
            self._stats[agent_id] = AgentDriftStats(agent_id=agent_id)

        s = self._stats[agent_id]
        s.last_polled = time.monotonic()

        # Capture current pane output
        text: str = await loop.run_in_executor(
            None, self._tmux.capture_pane, agent.pane
        )

        # Update last_change_time if pane content changed
        if text != s.last_pane_text:
            s.last_change_time = time.monotonic()
            s.last_pane_text = text

        # Update rolling line-count history
        line_count = len(text.splitlines())
        s.line_count_history.append(line_count)
        if len(s.line_count_history) > _LENGTH_HISTORY_WINDOW:
            s.line_count_history.pop(0)

        # Compute sub-scores
        system_prompt = getattr(agent, "system_prompt", None) or ""
        s.role_score = _compute_role_score(system_prompt, text)
        s.idle_score = _compute_idle_score(s.last_change_time, self._idle_threshold)
        s.length_score = _compute_length_score(s.line_count_history)
        s.drift_score = _composite_score(s.role_score, s.idle_score, s.length_score)

        await self._check_drift_threshold(agent, s)

    async def _check_drift_threshold(self, agent: "Agent", s: AgentDriftStats) -> None:
        """Publish agent_drift_warning or reset the warned flag."""
        if s.drift_score >= self._drift_threshold:
            # Score recovered above threshold — clear warned flag
            if s.warned:
                logger.debug(
                    "DriftMonitor: agent %s drift score recovered (%.3f >= %.3f)",
                    agent.id, s.drift_score, self._drift_threshold,
                )
                s.warned = False
            return

        # Score below threshold
        if not s.warned:
            s.warned = True
            s.drift_warnings += 1
            logger.warning(
                "DriftMonitor: agent %s drift detected "
                "(score=%.3f role=%.3f idle=%.3f length=%.3f threshold=%.3f)",
                agent.id, s.drift_score,
                s.role_score, s.idle_score, s.length_score,
                self._drift_threshold,
            )
            await self._publish(
                "agent_drift_warning",
                agent_id=agent.id,
                drift_score=round(s.drift_score, 4),
                role_score=round(s.role_score, 4),
                idle_score=round(s.idle_score, 4),
                length_score=round(s.length_score, 4),
                drift_threshold=self._drift_threshold,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _publish(self, event: str, **kwargs: Any) -> None:
        """Publish a STATUS message on the bus."""
        await self._bus.publish(
            Message(
                type=MessageType.STATUS,
                from_id="__drift_monitor__",
                payload={"event": event, **kwargs},
            )
        )
