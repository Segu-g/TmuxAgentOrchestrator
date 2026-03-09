"""Context window usage monitoring for ClaudeCodeAgent instances.

Each agent accumulates pane output over its lifetime.  When that output
grows large the effective context window of the underlying LLM may be
approaching saturation, leading to *context rot* — degraded recall and
accuracy for information positioned in the middle of the context window
(Liu et al., "Lost in the Middle", TACL 2024).

This module provides a lightweight monitor that:

1. **Tracks pane output size** by periodically capturing each agent's tmux
   pane and measuring character count.
2. **Estimates token count** via the common 4-chars-per-token heuristic
   (Anthropic token counting docs 2025; exact counting via the API is
   impractical in a polling loop).
3. **Detects NOTES.md updates** by watching the file's ``mtime``.  When
   the file changes (e.g. after the agent runs ``/summarize``), a
   ``notes_updated`` STATUS event is published on the bus so that the
   parent/orchestrator can react — closes the open §11 architecture item.
4. **Publishes ``context_warning``** STATUS events when the estimated
   token count exceeds a configurable threshold fraction of the total
   context window.
5. **Auto-triggers ``/summarize``** by injecting the command into the
   agent pane when the threshold is exceeded and ``auto_summarize=True``
   is configured.  This closes the feedback loop: the agent compresses
   its context, writes NOTES.md, the monitor detects the change, and
   publishes ``notes_updated``.

Design references:
- Liu et al. "Lost in the Middle: How Language Models Use Long Contexts"
  TACL 2024 — context window saturation degrades mid-context recall.
  https://arxiv.org/abs/2307.03172
- Anthropic token counting docs (2025):
  https://platform.claude.com/docs/en/build-with-claude/token-counting
- Anthropic context windows docs (2025):
  https://platform.claude.com/docs/en/build-with-claude/context-windows
- DESIGN.md §11 — "エージェントのコンテキスト使用量モニタリング"
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tmux_orchestrator.application.context_compression import TfIdfContextCompressor
from tmux_orchestrator.bus import Message, MessageType

if TYPE_CHECKING:
    from tmux_orchestrator.agents.base import Agent
    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.tmux_interface import TmuxInterface

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Approximate characters per token for Claude models (conservative estimate).
# Exact counting via `messages.countTokens` is impractical in a polling loop.
# Reference: Anthropic token counting docs (2025); common rule-of-thumb.
_CHARS_PER_TOKEN: float = 4.0

# Default poll interval in seconds.  Higher values reduce tmux overhead.
_DEFAULT_POLL: float = 5.0

# Default total context window for Claude Sonnet/Opus models (200 k tokens).
_DEFAULT_CONTEXT_WINDOW_TOKENS: int = 200_000

# Default threshold fraction at which a warning is emitted.
_DEFAULT_WARN_THRESHOLD: float = 0.75


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class AgentContextStats:
    """Snapshot of one agent's estimated context usage."""

    agent_id: str
    # Raw character count of the last captured pane output.
    pane_chars: int = 0
    # Estimated token count (pane_chars / _CHARS_PER_TOKEN).
    estimated_tokens: int = 0
    # Fraction of the configured context window (0.0 – 1.0+).
    context_pct: float = 0.0
    # Whether the agent has been warned (to avoid duplicate events).
    warned: bool = False
    # Whether /summarize was already injected (reset after NOTES.md update).
    summarize_injected: bool = False
    # Monotonic timestamp of the last NOTES.md mtime seen.
    notes_mtime: float = 0.0
    # Path to the agent's NOTES.md (set when worktree_path is known).
    notes_path: Path | None = None
    # Accumulated stats for observability.
    notes_updates: int = 0
    context_warnings: int = 0
    summarize_triggers: int = 0
    # TF-IDF auto-compress tracking (v1.1.12).
    # Whether auto-compression has been injected for the current threshold crossing.
    compress_injected: bool = False
    # Total number of times auto-compression was triggered.
    compress_triggers: int = 0
    # Monotonic timestamp of last poll.
    last_polled: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# ContextMonitor
# ---------------------------------------------------------------------------


class ContextMonitor:
    """Polls agent panes and publishes context / NOTES.md events.

    Parameters
    ----------
    bus:
        The shared message bus.  Events are published as ``MessageType.STATUS``
        messages from ``__context_monitor__``.
    tmux:
        TmuxInterface instance used to capture pane text.
    agents:
        Callable that returns the current list of live agents.  Called on
        every poll cycle so newly spawned agents are picked up automatically.
    context_window_tokens:
        Total context window size in tokens (used to compute
        ``context_pct``).  Defaults to 200 000 (Claude Sonnet/Opus).
    warn_threshold:
        Fraction of ``context_window_tokens`` that triggers a
        ``context_warning`` event and (optionally) auto-inject.  Default
        0.75 (75 % full).
    auto_summarize:
        When ``True``, inject ``/summarize`` into the agent pane once the
        threshold is exceeded.  The injection is done at most once per
        threshold crossing (reset after the monitor detects a NOTES.md
        update).
    auto_compress:
        When ``True``, automatically run TF-IDF extractive compression on the
        agent's pane output at threshold and inject the compressed context via
        ``notify_stdin``.  This is injected at most once per threshold crossing
        (the ``compress_injected`` flag resets when the context drops below the
        threshold again).  Can be combined with ``auto_summarize`` — both will
        run independently.
    compress_drop_percentile:
        Fraction of low-relevance lines to discard during auto-compression.
        Default 0.40 — removes the bottom 40 % of lines by TF-IDF relevance.
        Reference: ACON arXiv:2510.00615 (Kang et al. 2025).
    poll_interval:
        Seconds between polling cycles.
    """

    def __init__(
        self,
        bus: "Bus",
        tmux: "TmuxInterface",
        agents: "Any",  # Callable[[], list[Agent]]
        *,
        context_window_tokens: int = _DEFAULT_CONTEXT_WINDOW_TOKENS,
        warn_threshold: float = _DEFAULT_WARN_THRESHOLD,
        auto_summarize: bool = False,
        auto_compress: bool = False,
        compress_drop_percentile: float = 0.40,
        poll_interval: float = _DEFAULT_POLL,
    ) -> None:
        self._bus = bus
        self._tmux = tmux
        self._get_agents = agents  # Callable[[], list[Agent]]
        self._context_window_tokens = context_window_tokens
        self._warn_threshold = warn_threshold
        self._auto_summarize = auto_summarize
        self._auto_compress = auto_compress
        self._compress_drop_percentile = compress_drop_percentile
        self._poll_interval = poll_interval
        # Pre-create the TF-IDF compressor (stateless, reusable).
        self._compressor: TfIdfContextCompressor | None = (
            TfIdfContextCompressor(drop_percentile=compress_drop_percentile)
            if auto_compress
            else None
        )
        # Per-agent stats, keyed by agent_id
        self._stats: dict[str, AgentContextStats] = {}
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the background polling coroutine."""
        self._task = asyncio.create_task(self._poll_loop(), name="context-monitor")
        logger.info("ContextMonitor started (poll=%.1fs, window=%d tokens, threshold=%.0f%%)",
                    self._poll_interval, self._context_window_tokens,
                    self._warn_threshold * 100)

    def stop(self) -> None:
        """Cancel the background polling coroutine."""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("ContextMonitor stopped")

    # ------------------------------------------------------------------
    # Public stats access
    # ------------------------------------------------------------------

    def get_stats(self, agent_id: str) -> dict[str, Any] | None:
        """Return a JSON-serialisable stats dict for *agent_id*, or None."""
        s = self._stats.get(agent_id)
        if s is None:
            return None
        return {
            "agent_id": s.agent_id,
            "pane_chars": s.pane_chars,
            "estimated_tokens": s.estimated_tokens,
            "context_window_tokens": self._context_window_tokens,
            "context_pct": round(s.context_pct * 100, 1),
            "warn_threshold_pct": round(self._warn_threshold * 100, 1),
            "notes_mtime": s.notes_mtime,
            "notes_updates": s.notes_updates,
            "context_warnings": s.context_warnings,
            "summarize_triggers": s.summarize_triggers,
            "compress_triggers": s.compress_triggers,
            "last_polled": s.last_polled,
        }

    def all_stats(self) -> list[dict[str, Any]]:
        """Return stats for all tracked agents."""
        return [self.get_stats(aid) for aid in self._stats]  # type: ignore[misc]

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
                logger.exception("ContextMonitor poll error")

    async def _poll_all(self) -> None:
        """Poll every live agent once."""
        loop = asyncio.get_running_loop()
        agents: list[Agent] = self._get_agents()
        for agent in agents:
            try:
                await self._poll_agent(agent, loop)
            except Exception:  # noqa: BLE001
                logger.exception("ContextMonitor: error polling agent %s", agent.id)

    async def _poll_agent(self, agent: "Agent", loop: asyncio.AbstractEventLoop) -> None:
        """Poll a single agent's pane and NOTES.md."""
        agent_id = agent.id
        if agent_id not in self._stats:
            self._stats[agent_id] = AgentContextStats(agent_id=agent_id)

        s = self._stats[agent_id]
        s.last_polled = time.monotonic()

        # -- Refresh notes_path from agent's worktree_path (may change after start) --
        if s.notes_path is None and agent.worktree_path is not None:
            s.notes_path = agent.worktree_path / "NOTES.md"
            # Seed mtime so the first check doesn't false-positive
            if s.notes_path.exists():
                s.notes_mtime = s.notes_path.stat().st_mtime

        # -- Capture pane output (runs in executor to avoid blocking event loop) --
        pane = agent.pane
        if pane is not None:
            text: str = await loop.run_in_executor(
                None, self._tmux.capture_pane, pane
            )
            s.pane_chars = len(text)
            s.estimated_tokens = int(s.pane_chars / _CHARS_PER_TOKEN)
            s.context_pct = s.estimated_tokens / self._context_window_tokens

            await self._check_context_threshold(agent, s)

        # -- Check NOTES.md mtime --
        if s.notes_path is not None:
            await self._check_notes_updated(agent, s, loop)

    async def _check_context_threshold(
        self, agent: "Agent", s: AgentContextStats
    ) -> None:
        """Publish context_warning and optionally inject /summarize and/or TF-IDF compression."""
        if s.context_pct < self._warn_threshold:
            # Threshold not exceeded — reset flags for next crossing
            if s.warned:
                logger.debug(
                    "ContextMonitor: agent %s context below threshold (%.1f%%)",
                    agent.id, s.context_pct * 100,
                )
            s.warned = False
            s.summarize_injected = False
            s.compress_injected = False
            return

        if not s.warned:
            s.warned = True
            s.context_warnings += 1
            logger.warning(
                "ContextMonitor: agent %s context at %.1f%% (est. %d tokens / %d)",
                agent.id, s.context_pct * 100,
                s.estimated_tokens, self._context_window_tokens,
            )
            await self._publish(
                "context_warning",
                agent_id=agent.id,
                pane_chars=s.pane_chars,
                estimated_tokens=s.estimated_tokens,
                context_pct=round(s.context_pct * 100, 1),
                context_window_tokens=self._context_window_tokens,
            )

        if self._auto_summarize and not s.summarize_injected and agent.pane is not None:
            s.summarize_injected = True
            s.summarize_triggers += 1
            logger.info(
                "ContextMonitor: injecting /summarize into agent %s pane", agent.id
            )
            await agent.notify_stdin("/summarize")
            await self._publish(
                "summarize_triggered",
                agent_id=agent.id,
                estimated_tokens=s.estimated_tokens,
                context_pct=round(s.context_pct * 100, 1),
            )

        if (
            self._auto_compress
            and self._compressor is not None
            and not s.compress_injected
            and agent.pane is not None
        ):
            await self._run_auto_compress(agent, s)

    async def _check_notes_updated(
        self,
        agent: "Agent",
        s: AgentContextStats,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Detect NOTES.md changes and publish notes_updated event."""
        notes_path = s.notes_path
        assert notes_path is not None  # checked by caller

        try:
            mtime: float = await loop.run_in_executor(
                None, lambda: notes_path.stat().st_mtime if notes_path.exists() else 0.0
            )
        except OSError:
            return

        if mtime <= s.notes_mtime:
            return  # unchanged

        # File was created or updated
        prev_mtime = s.notes_mtime
        s.notes_mtime = mtime
        s.notes_updates += 1

        # Reset summarize injection flag so we can inject again if needed
        s.summarize_injected = False

        logger.info(
            "ContextMonitor: NOTES.md updated for agent %s (mtime %.3f → %.3f)",
            agent.id, prev_mtime, mtime,
        )

        # Read a short preview of the notes for the event payload
        try:
            preview: str = await loop.run_in_executor(
                None,
                lambda: (notes_path.read_text(encoding="utf-8", errors="replace")[:500]
                          if notes_path.exists() else ""),
            )
        except OSError:
            preview = ""

        await self._publish(
            "notes_updated",
            agent_id=agent.id,
            notes_path=str(notes_path),
            notes_mtime=mtime,
            preview=preview,
        )

    # ------------------------------------------------------------------
    # Auto-compress (TF-IDF extractive compression)
    # ------------------------------------------------------------------

    async def _run_auto_compress(
        self, agent: "Agent", s: AgentContextStats
    ) -> None:
        """Run TF-IDF compression on the agent's pane and inject the result.

        The compressed text is sent to the agent via ``notify_stdin`` using the
        ``__COMPRESS_CONTEXT__`` protocol token.  The agent plugin's
        UserPromptSubmit hook (or a future dedicated hook) can intercept this
        token and inject the compressed text as additional context.

        Injection is skipped if the compressor is unavailable or pane capture
        fails.  The ``compress_injected`` flag prevents re-compression within
        the same threshold crossing — it is reset when the context drops below
        ``warn_threshold``.

        References
        ----------
        - ACON arXiv:2510.00615 (Kang et al. 2025): threshold-based auto-compress.
        - Focus Agent arXiv:2601.07190 (Verma 2026): intra-trajectory compression.
        """
        assert self._compressor is not None  # checked by caller

        # Capture current pane text (already done in _poll_agent, but we need
        # it here for the compressor; do a fresh capture for accuracy).
        loop = asyncio.get_running_loop()
        try:
            pane_text: str = await loop.run_in_executor(
                None, self._tmux.capture_pane, agent.pane
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "ContextMonitor: auto-compress pane capture failed for %s", agent.id
            )
            return

        if not pane_text.strip():
            return

        # Run TF-IDF compression (CPU-bound but typically < 10ms for typical pane sizes).
        try:
            result = await loop.run_in_executor(
                None, self._compressor.compress, pane_text, ""
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "ContextMonitor: TF-IDF compression failed for agent %s", agent.id
            )
            return

        # Mark injected *before* await to prevent concurrent duplicate injections.
        s.compress_injected = True
        s.compress_triggers += 1

        ratio = self._compressor.compression_ratio(result)
        logger.info(
            "ContextMonitor: auto-compressing agent %s pane: %d → %d lines "
            "(%.0f%% char reduction, drop_percentile=%.2f)",
            agent.id,
            result.original_lines,
            result.kept_lines,
            ratio * 100,
            self._compress_drop_percentile,
        )

        # Inject the compressed text via the __COMPRESS_CONTEXT__ protocol token.
        #
        # File-based delivery (preferred — same consume-once pattern as __task_prompt__):
        #   1. Write compressed text to __compress_context__{agent.id}__.txt in the
        #      agent's cwd (worktree_path).
        #   2. Send only the short trigger "__COMPRESS_CONTEXT__" via send_keys.
        #   3. The UserPromptSubmit hook (user-prompt-submit.py) intercepts the trigger,
        #      reads the file, deletes it (consume-once), and returns the compressed text
        #      as additionalContext so Claude receives the context summary.
        #
        # Fallback (no worktree): send "__COMPRESS_CONTEXT__\n{text}" inline.  Claude
        # sees the raw text as a user message — less clean but functional.
        #
        # References:
        # - DESIGN.md §10.38 (v1.1.13 — __COMPRESS_CONTEXT__ UserPromptSubmit hook)
        # - DESIGN.md §10.38 (v1.1.2 — __TASK__ / __task_prompt__ file pattern)
        # - ACON arXiv:2510.00615: threshold-based context compression delivery.
        worktree = agent.worktree_path
        if worktree is not None:
            compress_file = worktree / f"__compress_context__{agent.id}__.txt"
            try:
                await loop.run_in_executor(
                    None,
                    lambda p=compress_file, t=result.compressed_text: p.write_text(
                        t, encoding="utf-8"
                    ),
                )
                notification = "__COMPRESS_CONTEXT__"
            except OSError:
                logger.warning(
                    "ContextMonitor: could not write compress file for %s — "
                    "falling back to inline delivery",
                    agent.id,
                )
                notification = f"__COMPRESS_CONTEXT__\n{result.compressed_text}"
        else:
            # No worktree: fall back to inline delivery.
            notification = f"__COMPRESS_CONTEXT__\n{result.compressed_text}"
        await agent.notify_stdin(notification)

        await self._publish(
            "compress_triggered",
            agent_id=agent.id,
            estimated_tokens=s.estimated_tokens,
            context_pct=round(s.context_pct * 100, 1),
            original_lines=result.original_lines,
            kept_lines=result.kept_lines,
            original_chars=result.original_chars,
            compressed_chars=result.compressed_chars,
            compression_ratio=round(ratio, 3),
            drop_percentile=self._compress_drop_percentile,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _publish(self, event: str, **kwargs: Any) -> None:
        """Publish a STATUS message on the bus."""
        await self._bus.publish(
            Message(
                type=MessageType.STATUS,
                from_id="__context_monitor__",
                payload={"event": event, **kwargs},
            )
        )
