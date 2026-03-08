"""Thin libtmux wrapper with background pane-watching."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

import libtmux

if TYPE_CHECKING:
    from tmux_orchestrator.bus import Bus

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.1  # seconds


@dataclass
class PaneOutputEvent:
    pane_id: str
    agent_id: str
    text: str


class TmuxInterface:
    """Manages a tmux session and its panes.

    Creates the session on first use (or attaches to an existing one).
    A single background thread polls all registered panes for new output
    and emits :class:`PaneOutputEvent` messages onto the bus.
    """

    def __init__(
        self,
        session_name: str,
        bus: "Bus | None" = None,
        confirm_kill: "Callable[[str], bool] | None" = None,
    ) -> None:
        self.session_name = session_name
        self.bus = bus
        self._confirm_kill = confirm_kill
        self._server = libtmux.Server()
        self._session: libtmux.Session | None = None
        # pane_id → (agent_id, last_hash)
        self._watched: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._watcher_thread: threading.Thread | None = None
        # Event loop reference captured at construction time for thread-safe scheduling
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def ensure_session(self) -> libtmux.Session:
        """Return the managed session, always creating a fresh one.

        If a session with the same name already exists and *confirm_kill* was
        provided at construction time, the user is prompted for confirmation
        before the existing session is killed.  If the user declines, a
        ``RuntimeError`` is raised and the orchestrator does not start.
        """
        if self._session is not None:
            return self._session
        existing = self._server.sessions.get(session_name=self.session_name, default=None)
        if existing:
            if self._confirm_kill is not None and not self._confirm_kill(self.session_name):
                raise RuntimeError(
                    f"tmux session '{self.session_name}' already exists and was not replaced; aborting"
                )
            existing.kill()
        self._session = self._server.new_session(session_name=self.session_name)
        # Disable assume-paste-time so long prompts are never treated as
        # bracket-paste by tmux (the default of 1ms causes Claude CLI to show
        # "[Pasted text #N]" and swallow the subsequent Enter keypress).
        self._session.set_option("assume-paste-time", "0")
        return self._session

    def kill_session(self) -> None:
        """Kill the managed tmux session (call during orchestrator shutdown)."""
        if self._session is not None:
            try:
                self._session.kill()
            except Exception:  # noqa: BLE001
                pass
            self._session = None

    def new_pane(
        self,
        agent_id: str = "",
        environment: dict[str, str] | None = None,
    ) -> libtmux.Pane:
        """Spawn a new window for a top-level agent and return its first pane.

        In the session/window/pane hierarchy:
        - session  → project
        - window   → top-level agent (or agent group)
        - pane     → individual agent process

        ``environment`` is forwarded to libtmux's ``new_window(environment=...)``
        which maps to ``tmux new-window -e KEY=VALUE`` — pane-local, no race.
        """
        session = self.ensure_session()
        window = session.new_window(
            window_name=agent_id or None,
            attach=False,
            environment=environment,
        )
        return window.panes[0]

    def new_subpane(
        self,
        parent_pane: libtmux.Pane,
        agent_id: str = "",
        environment: dict[str, str] | None = None,
    ) -> libtmux.Pane:
        """Split *parent_pane*'s window to create a pane for a sub-agent.

        Sub-agents are co-located in the same tmux window as their parent,
        maintaining the visual hierarchy: window = agent group, pane = agent.
        Returns the newly created pane.

        ``environment`` is forwarded to libtmux's ``split(environment=...)``
        which maps to ``tmux split-window -e KEY=VALUE`` — pane-local, no race.
        """
        window = parent_pane.window
        new_pane = window.split(attach=False, environment=environment)
        logger.debug("Created sub-pane %s for agent %s in window %s", new_pane.id, agent_id, window.id)
        return new_pane

    def get_first_pane(self) -> libtmux.Pane:
        """Return the first (pre-existing) pane of the session."""
        session = self.ensure_session()
        return session.windows[0].panes[0]

    # ------------------------------------------------------------------
    # Pane I/O
    # ------------------------------------------------------------------

    def send_keys(self, pane: libtmux.Pane, text: str, enter: bool = True) -> None:
        """Send *text* to *pane*, optionally followed by Enter.

        Long or multi-line text can trigger Claude CLI's paste-preview mode,
        which absorbs the trailing newline that ``send_keys(enter=True)``
        appends.  We therefore always send Enter as a *separate* keypress
        after a brief pause to let any paste-preview settle.
        """
        pane.send_keys(text, enter=False)
        if enter:
            time.sleep(0.15)  # let paste-preview settle before Enter
            pane.send_keys("", enter=True)

    def capture_pane(self, pane: libtmux.Pane) -> str:
        """Return the current visible text of *pane*."""
        return "\n".join(pane.capture_pane())

    def create_process_adapter(self, pane: libtmux.Pane) -> "TmuxProcessAdapter":
        """Create a ``TmuxProcessAdapter`` wrapping *pane*.

        Factory method for constructing a :class:`ProcessPort`-compatible
        adapter from an existing tmux pane.  Use this to decouple
        ``ClaudeCodeAgent`` from ``libtmux.Pane`` for unit-testability.

        Parameters
        ----------
        pane:
            The ``libtmux.Pane`` returned by ``new_pane()`` or
            ``new_subpane()``.

        Returns
        -------
        TmuxProcessAdapter
            A :class:`ProcessPort`-compatible adapter backed by *pane*.

        Reference: DESIGN.md §10.13 (v0.46.0).
        """
        from tmux_orchestrator.process_port import TmuxProcessAdapter  # noqa: PLC0415
        return TmuxProcessAdapter(pane=pane, tmux=self)

    # ------------------------------------------------------------------
    # Background watcher
    # ------------------------------------------------------------------

    def watch_pane(self, pane: libtmux.Pane, agent_id: str) -> None:
        """Register *pane* for output monitoring."""
        content = self.capture_pane(pane)
        h = _hash(content)
        with self._lock:
            self._watched[pane.id] = (agent_id, h)
        logger.debug("Watching pane %s for agent %s", pane.id, agent_id)

    def unwatch_pane(self, pane: libtmux.Pane) -> None:
        """Remove *pane* from monitoring."""
        with self._lock:
            self._watched.pop(pane.id, None)

    def start_watcher(self) -> None:
        """Start the background polling thread.

        Captures the running event loop so the watcher thread can safely
        schedule coroutines via ``asyncio.run_coroutine_threadsafe``.
        """
        if self._watcher_thread and self._watcher_thread.is_alive():
            return
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._stop_event.clear()
        self._watcher_thread = threading.Thread(
            target=self._watch_loop, daemon=True, name="pane-watcher"
        )
        self._watcher_thread.start()
        logger.debug("Pane watcher started")

    def stop_watcher(self) -> None:
        """Stop the background polling thread."""
        self._stop_event.set()
        if self._watcher_thread:
            self._watcher_thread.join(timeout=2)

    def _watch_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                items = list(self._watched.items())
            for pane_id, (agent_id, last_hash) in items:
                try:
                    pane = self._server.panes.get(pane_id=pane_id, default=None)
                    if pane is None:
                        continue
                    content = self.capture_pane(pane)
                    h = _hash(content)
                    if h != last_hash:
                        with self._lock:
                            if pane_id in self._watched:
                                self._watched[pane_id] = (agent_id, h)
                        event = PaneOutputEvent(
                            pane_id=pane_id, agent_id=agent_id, text=content
                        )
                        if self.bus is not None and self._loop is not None:
                            from tmux_orchestrator.bus import Message, MessageType

                            msg = Message(
                                type=MessageType.STATUS,
                                from_id=agent_id,
                                payload={"pane_output": content},
                            )
                            asyncio.run_coroutine_threadsafe(
                                self.bus.publish(msg), self._loop
                            )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Watcher error for pane %s: %s", pane_id, exc)
            time.sleep(POLL_INTERVAL)


def _hash(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()
