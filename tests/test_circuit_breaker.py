"""Tests for the per-agent CircuitBreaker."""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from tmux_orchestrator.circuit_breaker import BreakerState, CircuitBreaker


def make_breaker(threshold: int = 3, recovery: float = 60.0) -> CircuitBreaker:
    return CircuitBreaker("agent-x", failure_threshold=threshold, recovery_timeout=recovery)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_initial_state_closed():
    cb = make_breaker()
    assert cb.state == BreakerState.CLOSED
    assert cb.is_allowed() is True


# ---------------------------------------------------------------------------
# CLOSED → OPEN
# ---------------------------------------------------------------------------


def test_failures_below_threshold_stay_closed():
    cb = make_breaker(threshold=3)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == BreakerState.CLOSED
    assert cb.is_allowed() is True


def test_failures_at_threshold_open_breaker():
    cb = make_breaker(threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert cb.state == BreakerState.OPEN
    assert cb.is_allowed() is False


def test_success_in_closed_resets_failure_count():
    cb = make_breaker(threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    # Still 2 failures before reset, but success clears counter
    cb.record_failure()
    cb.record_failure()
    assert cb.state == BreakerState.CLOSED  # only 2 failures after reset


# ---------------------------------------------------------------------------
# OPEN → HALF_OPEN after recovery timeout
# ---------------------------------------------------------------------------


def test_open_stays_blocked_before_timeout():
    cb = make_breaker(threshold=1, recovery=60.0)
    cb.record_failure()
    assert cb.state == BreakerState.OPEN
    assert cb.is_allowed() is False


def test_open_transitions_to_half_open_after_timeout():
    cb = make_breaker(threshold=1, recovery=60.0)
    cb.record_failure()
    # Simulate time elapsed
    cb._opened_at = time.monotonic() - 61.0
    assert cb.is_allowed() is True  # triggers transition
    assert cb.state == BreakerState.HALF_OPEN


# ---------------------------------------------------------------------------
# HALF_OPEN → CLOSED or OPEN
# ---------------------------------------------------------------------------


def test_half_open_success_closes_breaker():
    cb = make_breaker(threshold=1, recovery=60.0)
    cb.record_failure()
    cb._opened_at = time.monotonic() - 61.0
    cb.is_allowed()  # → HALF_OPEN
    cb.record_success()
    assert cb.state == BreakerState.CLOSED
    assert cb.is_allowed() is True


def test_half_open_failure_reopens_breaker():
    cb = make_breaker(threshold=1, recovery=60.0)
    cb.record_failure()
    cb._opened_at = time.monotonic() - 61.0
    cb.is_allowed()  # → HALF_OPEN
    cb.record_failure()
    assert cb.state == BreakerState.OPEN
    assert cb.is_allowed() is False
