"""ProcessPort protocol and adapters — Clean Architecture dependency inversion.

Infrastructure adapter for the process/tmux pane external system.
This module is the canonical home for ProcessPort, TmuxProcessAdapter, and
StdioProcessAdapter; the old path ``tmux_orchestrator.process_port``
re-exports from here (Strangler Fig shim).

Layer: infrastructure (may depend on domain/application; must NOT be imported
by domain/ or application/).

Design references:
- PEP 544 Protocols: Structural subtyping (static duck typing)
  https://peps.python.org/pep-0544/
- Martin "Clean Architecture" (2017) Ch.22 — Port & Adapter pattern
  (Dependency Rule: inner-layer Use Cases depend on abstract Ports, not
  concrete outer-layer Adapters)
- "Hexagonal Architecture: Ports and Adapters in Python"
  https://softwarepatternslexicon.com/python/architectural-patterns/hexagonal-architecture-ports-and-adapters/
- Naoyuki Sakai "AI Agent Architecture: Clean Architecture" (2025)
  https://dev.to/hieutran25/building-maintainable-python-applications-with-hexagonal-architecture-and-domain-driven-design-chp
- DESIGN.md §10.13 (v0.46.0), §10.N (v1.0.17 — infrastructure/ continued)

Module layout:
    ProcessPort         — @runtime_checkable Protocol (the Port)
    TmuxProcessAdapter  — wraps libtmux.Pane + TmuxInterface (tmux Adapter)
    StdioProcessAdapter — in-memory fake for unit tests (test Adapter)

Usage in ClaudeCodeAgent:
    # Before (coupled to libtmux):
    self.pane: libtmux.Pane = tmux.new_pane(agent_id)
    self._tmux.send_keys(self.pane, prompt)
    text = self._tmux.capture_pane(self.pane)

    # After (decoupled via port):
    from tmux_orchestrator.infrastructure.process_port import TmuxProcessAdapter
    raw_pane = tmux.new_pane(agent_id)
    self.process: ProcessPort = TmuxProcessAdapter(pane=raw_pane, tmux=tmux)
    self.process.send_keys(prompt)
    text = self.process.capture_pane()
"""

from __future__ import annotations

from typing import TYPE_CHECKING, runtime_checkable

from typing import Protocol

if TYPE_CHECKING:
    import libtmux

    from tmux_orchestrator.tmux_interface import TmuxInterface


# ---------------------------------------------------------------------------
# Port (abstract interface)
# ---------------------------------------------------------------------------


@runtime_checkable
class ProcessPort(Protocol):
    """Abstract port for interacting with a running agent process.

    This protocol represents the minimal interface that ``ClaudeCodeAgent``
    needs to drive its underlying process (send input, capture output).

    Any class that provides ``send_keys(keys, enter=True)`` and
    ``capture_pane() -> str`` is structurally compatible with this port —
    no explicit inheritance required (PEP 544 structural subtyping).

    Adapters
    --------
    - ``TmuxProcessAdapter`` — wraps ``libtmux.Pane`` for production use
    - ``StdioProcessAdapter`` — in-memory fake for unit tests

    Reference: DESIGN.md §10.13 (v0.46.0).
    """

    def send_keys(self, keys: str, enter: bool = True) -> None:
        """Send *keys* as input to the running process.

        Parameters
        ----------
        keys:
            The string to send as keyboard input.
        enter:
            When ``True`` (default), a newline/Enter is appended after *keys*.
        """
        ...

    def capture_pane(self) -> str:
        """Return the current visible output of the process as a string.

        Returns
        -------
        str
            The captured pane/process output.  May include ANSI escape codes
            depending on the adapter implementation.
        """
        ...


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class TmuxProcessAdapter:
    """Adapter that wraps a ``libtmux.Pane`` and its ``TmuxInterface``.

    Delegates ``send_keys`` and ``capture_pane`` to the ``TmuxInterface``
    helper methods, keeping the pane reference internal.

    Parameters
    ----------
    pane:
        The ``libtmux.Pane`` object created by ``TmuxInterface.new_pane()``.
    tmux:
        The ``TmuxInterface`` instance that owns the session.
    """

    def __init__(self, *, pane: "libtmux.Pane", tmux: "TmuxInterface") -> None:
        self._pane = pane
        self._tmux = tmux

    @property
    def pane(self) -> "libtmux.Pane":
        """The underlying ``libtmux.Pane`` object."""
        return self._pane

    def send_keys(self, keys: str, enter: bool = True) -> None:
        """Send *keys* to the tmux pane via ``TmuxInterface.send_keys``.

        The ``TmuxInterface.send_keys`` method always appends an Enter key
        (the ``enter`` parameter is ignored for pane-based presses since
        libtmux handles Enter automatically).  This matches the existing
        ``ClaudeCodeAgent`` behaviour.
        """
        self._tmux.send_keys(self._pane, keys)

    def capture_pane(self) -> str:
        """Capture the current pane output via ``TmuxInterface.capture_pane``."""
        return self._tmux.capture_pane(self._pane)


class StdioProcessAdapter:
    """In-memory fake process adapter for unit tests.

    Simulates a running agent process without requiring tmux or any external
    process.  ``send_keys()`` appends text to an internal output buffer;
    ``capture_pane()`` returns the current buffer contents.

    Test helpers allow pre-seeding the output buffer (``set_output``,
    ``append_output``) and inspecting what was sent (``sent_keys_history``).

    Example
    -------
    ::

        adapter = StdioProcessAdapter()
        # Seed output so the agent "sees" a ready prompt
        adapter.set_output("❯")

        # Simulate what ClaudeCodeAgent would do
        adapter.send_keys("Write hello.py")
        adapter.append_output("\\nWriting file...\\n❯")

        assert "hello.py" in adapter.sent_keys_history()[0]
        assert adapter.capture_pane().endswith("❯")
    """

    def __init__(self) -> None:
        self._output: str = ""
        self._sent: list[str] = []

    # ------------------------------------------------------------------
    # ProcessPort interface
    # ------------------------------------------------------------------

    def send_keys(self, keys: str, enter: bool = True) -> None:
        """Append *keys* to the output buffer and record in sent history.

        When ``enter=True`` (default), a newline is appended after *keys*
        to simulate pressing Enter.
        """
        self._sent.append(keys)
        if enter:
            self._output += keys + "\n"
        else:
            self._output += keys

    def capture_pane(self) -> str:
        """Return the current output buffer contents."""
        return self._output

    # ------------------------------------------------------------------
    # Test helpers (not part of the ProcessPort interface)
    # ------------------------------------------------------------------

    def set_output(self, text: str) -> None:
        """Replace the entire output buffer with *text*."""
        self._output = text

    def append_output(self, text: str) -> None:
        """Append *text* to the output buffer."""
        self._output += text

    def sent_keys_history(self) -> list[str]:
        """Return a list of all strings that were passed to ``send_keys``."""
        return list(self._sent)

    def clear(self) -> None:
        """Reset output buffer and sent keys history."""
        self._output = ""
        self._sent = []
