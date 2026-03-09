"""Tests for OrchestratorConfig.__post_init__ watchdog_poll validator.

Validates the cross-field constraint:
  When watchdog_poll < task_timeout (watchdog is active),
  watchdog_poll must be <= task_timeout / 3.

When watchdog_poll >= task_timeout the watchdog is effectively disabled
(no task can complete a watchdog cycle before timing out), which is a
valid configuration for testing — no error is raised.

Reference: DESIGN.md §10.33 (v1.0.33 — watchdog_poll validator)
"""

from __future__ import annotations

import pytest

from tmux_orchestrator.config import OrchestratorConfig


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def make_config(**kwargs) -> OrchestratorConfig:
    defaults = dict(session_name="test", agents=[], p2p_permissions=[], task_timeout=120)
    defaults.update(kwargs)
    return OrchestratorConfig(**defaults)


# ---------------------------------------------------------------------------
# Default value tests
# ---------------------------------------------------------------------------


class TestDefaultWatchdogPoll:
    """The default watchdog_poll changed from 10.0 to 30.0 in v1.0.33."""

    def test_default_watchdog_poll_is_30(self):
        cfg = OrchestratorConfig(session_name="s", agents=[], p2p_permissions=[])
        assert cfg.watchdog_poll == 30.0

    def test_default_does_not_raise_with_default_task_timeout(self):
        """Default watchdog_poll=30 with default task_timeout=120: 30 <= 40 ✓"""
        cfg = make_config()  # task_timeout=120, watchdog_poll=30 (default)
        assert cfg.watchdog_poll == 30.0
        assert cfg.task_timeout == 120


# ---------------------------------------------------------------------------
# Valid configurations (no error expected)
# ---------------------------------------------------------------------------


class TestWatchdogPollValid:
    def test_watchdog_poll_exactly_one_third(self):
        """watchdog_poll == task_timeout / 3 is valid (boundary)."""
        cfg = make_config(task_timeout=90, watchdog_poll=30.0)
        assert cfg.watchdog_poll == 30.0

    def test_watchdog_poll_less_than_one_third(self):
        """watchdog_poll < task_timeout / 3 is valid."""
        cfg = make_config(task_timeout=120, watchdog_poll=10.0)
        assert cfg.watchdog_poll == 10.0

    def test_watchdog_poll_equal_to_task_timeout(self):
        """watchdog_poll == task_timeout: watchdog effectively disabled → valid."""
        cfg = make_config(task_timeout=30, watchdog_poll=30.0)
        assert cfg.watchdog_poll == 30.0

    def test_watchdog_poll_greater_than_task_timeout(self):
        """watchdog_poll > task_timeout: watchdog disabled (test mode) → valid."""
        cfg = make_config(task_timeout=10, watchdog_poll=9999.0)
        assert cfg.watchdog_poll == 9999.0

    def test_watchdog_poll_much_greater_than_task_timeout(self):
        """Extreme test-disable value (99999) with small task_timeout."""
        cfg = make_config(task_timeout=10, watchdog_poll=99999.0)
        assert cfg.watchdog_poll == 99999.0

    def test_small_task_timeout_equal_watchdog_poll(self):
        """task_timeout=0.05, watchdog_poll=0.05: equal → not active → valid."""
        cfg = make_config(task_timeout=0.05, watchdog_poll=0.05)
        assert cfg.watchdog_poll == 0.05

    def test_watchdog_poll_small_with_large_task_timeout(self):
        """watchdog_poll=1.0, task_timeout=900: ratio 0.001 — fine."""
        cfg = make_config(task_timeout=900, watchdog_poll=1.0)
        assert cfg.watchdog_poll == 1.0


# ---------------------------------------------------------------------------
# Invalid configurations (ValueError expected)
# ---------------------------------------------------------------------------


class TestWatchdogPollInvalid:
    def test_watchdog_poll_too_high_basic(self):
        """watchdog_poll=50 > task_timeout(90)/3=30 → ValueError."""
        with pytest.raises(ValueError, match="watchdog_poll"):
            make_config(task_timeout=90, watchdog_poll=50.0)

    def test_watchdog_poll_just_over_threshold(self):
        """watchdog_poll just above task_timeout/3 triggers error."""
        with pytest.raises(ValueError, match="watchdog_poll"):
            make_config(task_timeout=90, watchdog_poll=30.001)

    def test_error_message_contains_values(self):
        """Error message includes both watchdog_poll and task_timeout values."""
        with pytest.raises(ValueError) as exc_info:
            make_config(task_timeout=60, watchdog_poll=25.0)
        msg = str(exc_info.value)
        assert "watchdog_poll" in msg
        assert "25.0" in msg
        assert "60" in msg

    def test_error_message_mentions_max(self):
        """Error message includes the allowable maximum."""
        with pytest.raises(ValueError) as exc_info:
            make_config(task_timeout=120, watchdog_poll=50.0)
        msg = str(exc_info.value)
        # task_timeout/3 = 40.0
        assert "40.0" in msg

    def test_task_timeout_30_watchdog_poll_11(self):
        """task_timeout=30, watchdog_poll=11: 11 > 30/3=10 → error."""
        with pytest.raises(ValueError, match="watchdog_poll"):
            make_config(task_timeout=30, watchdog_poll=11.0)

    def test_watchdog_poll_equals_task_timeout_minus_epsilon_but_too_large(self):
        """watchdog_poll=29.99 with task_timeout=30: 29.99 > 10 → error."""
        with pytest.raises(ValueError, match="watchdog_poll"):
            make_config(task_timeout=30, watchdog_poll=29.99)


# ---------------------------------------------------------------------------
# Integration: validator fires at construction time
# ---------------------------------------------------------------------------


class TestTaskTimeoutValidator:
    def test_task_timeout_zero_raises(self):
        """task_timeout=0 is meaningless and raises ValueError."""
        with pytest.raises(ValueError, match="task_timeout"):
            make_config(task_timeout=0)

    def test_task_timeout_negative_raises(self):
        """task_timeout=-1 is invalid and raises ValueError."""
        with pytest.raises(ValueError, match="task_timeout"):
            make_config(task_timeout=-1)

    def test_task_timeout_positive_is_valid(self):
        """task_timeout=1 is valid (very short but positive)."""
        # watchdog_poll=30 >= task_timeout=1 → watchdog effectively disabled → valid
        cfg = make_config(task_timeout=1, watchdog_poll=30.0)
        assert cfg.task_timeout == 1


class TestWatchdogPollFailFast:
    def test_raises_at_construction_not_at_use(self):
        """The ValueError is raised immediately at dataclass creation."""
        with pytest.raises(ValueError):
            OrchestratorConfig(
                session_name="test",
                agents=[],
                p2p_permissions=[],
                task_timeout=60,
                watchdog_poll=25.0,  # 25 > 60/3=20 → invalid
            )

    def test_valid_construction_returns_instance(self):
        """Valid configuration constructs without error and returns OrchestratorConfig."""
        cfg = OrchestratorConfig(
            session_name="test",
            agents=[],
            p2p_permissions=[],
            task_timeout=60,
            watchdog_poll=15.0,  # 15 <= 60/3=20 → valid
        )
        assert isinstance(cfg, OrchestratorConfig)
        assert cfg.watchdog_poll == 15.0
