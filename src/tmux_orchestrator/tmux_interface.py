"""Thin libtmux wrapper with background pane-watching."""

from __future__ import annotations

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
        return self._session

    def kill_session(self) -> None:
        """Kill the managed tmux session (call during orchestrator shutdown)."""
        if self._session is not None:
            try:
                self._session.kill()
            except Exception:  # noqa: BLE001
                pass
            self._session = None

    def new_pane(self, window_index: int = 0) -> libtmux.Pane:
        """Spawn a new pane in the managed session's first window."""
        session = self.ensure_session()
        window = session.windows[window_index]
        pane: libtmux.Pane = window.split(attach=False)
        return pane

    def get_first_pane(self) -> libtmux.Pane:
        """Return the first (pre-existing) pane of the session."""
        session = self.ensure_session()
        return session.windows[0].panes[0]

    # ------------------------------------------------------------------
    # Pane I/O
    # ------------------------------------------------------------------

    def send_keys(self, pane: libtmux.Pane, text: str, enter: bool = True) -> None:
        """Send *text* to *pane*, optionally followed by Enter."""
        pane.send_keys(text, enter=enter)

    def capture_pane(self, pane: libtmux.Pane) -> str:
        """Return the current visible text of *pane*."""
        return "\n".join(pane.capture_pane())

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
        """Start the background polling thread."""
        if self._watcher_thread and self._watcher_thread.is_alive():
            return
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
                        if self.bus is not None:
                            import asyncio

                            from tmux_orchestrator.bus import Message, MessageType

                            msg = Message(
                                type=MessageType.STATUS,
                                from_id=agent_id,
                                payload={"pane_output": content},
                            )
                            try:
                                loop = asyncio.get_event_loop()
                                if loop.is_running():
                                    asyncio.run_coroutine_threadsafe(
                                        self.bus.publish(msg), loop
                                    )
                            except RuntimeError:
                                pass
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Watcher error for pane %s: %s", pane_id, exc)
            time.sleep(POLL_INTERVAL)


def _hash(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()
