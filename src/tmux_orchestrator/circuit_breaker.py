"""Per-agent circuit breaker for dispatch throttling after repeated failures."""
from __future__ import annotations

import logging
import time
from enum import Enum

logger = logging.getLogger(__name__)


class BreakerState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """Tracks per-agent failure history and blocks dispatch when the circuit is open.

    States:
    - CLOSED (normal): all tasks dispatched.
    - OPEN (tripped): no tasks dispatched until recovery_timeout elapses.
    - HALF_OPEN: one probe task allowed; on success → CLOSED, on failure → OPEN.

    This prevents a repeatedly-failing agent from consuming queue capacity
    and enables graceful recovery without operator intervention.

    Reference: Martin Fowler "Release It!" (2018) Ch. 5 — Stability Patterns.
    """

    def __init__(
        self,
        agent_id: str,
        *,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
    ) -> None:
        self.agent_id = agent_id
        self.state = BreakerState.CLOSED
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._failure_count = 0
        self._opened_at: float | None = None

    def is_allowed(self) -> bool:
        """True if a task may be dispatched to this agent."""
        if self.state == BreakerState.CLOSED:
            return True
        if self.state == BreakerState.OPEN:
            if (
                self._opened_at is not None
                and time.monotonic() - self._opened_at >= self._recovery_timeout
            ):
                self._to_half_open()
                return True
            return False
        # HALF_OPEN: allow one probe only if no prior probe is in flight
        return self._failure_count == 0

    def record_success(self) -> None:
        if self.state == BreakerState.HALF_OPEN:
            self._to_closed()
        elif self.state == BreakerState.CLOSED:
            self._failure_count = 0

    def record_failure(self) -> None:
        self._failure_count += 1
        if self.state == BreakerState.HALF_OPEN:
            self._to_open()
        elif (
            self.state == BreakerState.CLOSED
            and self._failure_count >= self._failure_threshold
        ):
            self._to_open()

    def _to_open(self) -> None:
        self.state = BreakerState.OPEN
        self._opened_at = time.monotonic()
        logger.warning(
            "Circuit breaker OPEN for agent %s (failures=%d)",
            self.agent_id, self._failure_count,
        )

    def _to_half_open(self) -> None:
        self.state = BreakerState.HALF_OPEN
        self._failure_count = 0
        logger.info("Circuit breaker HALF-OPEN for agent %s — sending probe", self.agent_id)

    def _to_closed(self) -> None:
        self.state = BreakerState.CLOSED
        self._failure_count = 0
        self._opened_at = None
        logger.info("Circuit breaker CLOSED for agent %s — resumed", self.agent_id)
