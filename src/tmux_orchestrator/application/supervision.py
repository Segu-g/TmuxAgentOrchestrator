"""Application-layer supervision service.

Implements the Supervisor Pattern from Erlang OTP applied to asyncio coroutines.
``supervised_task`` wraps a no-argument coroutine factory and restarts it on
unexpected exceptions, giving up after *max_restarts* attempts.

Layer rule: this module imports ONLY asyncio and stdlib — no infrastructure.

Key design choices:
- ``asyncio.CancelledError`` is NEVER caught — cancellation propagates immediately.
- Restart delay uses pre-defined backoff levels (not unbounded exponential growth).
- Caller supplies an optional async ``on_permanent_failure`` callback invoked when
  all restart attempts are exhausted.

Reference:
    - Erlang OTP supervisor behaviour (Ericsson, 1996)
    - Hattingh "Using Asyncio in Python" (O'Reilly, 2020) Ch. 4
    - Martin "Clean Architecture" (2017) — application layer use-cases
    - DESIGN.md §10.6 (2026-03-05 supervision); §10.N (v1.0.15 application/)
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# Backoff schedule (seconds) indexed by attempt number (capped at last entry).
_BACKOFF = [0.1, 0.5, 1.0, 5.0, 30.0]


async def supervised_task(
    coro_factory: Callable[[], Coroutine[Any, Any, None]],
    name: str,
    *,
    max_restarts: int = 5,
    on_permanent_failure: Callable[[str, Exception], Coroutine[Any, Any, None]] | None = None,
) -> None:
    """Run *coro_factory()* and restart it on failure up to *max_restarts* times.

    Parameters
    ----------
    coro_factory:
        A no-argument callable that returns a fresh coroutine on each invocation.
        The supervisor calls it once per attempt.
    name:
        Human-readable task name used in log messages.
    max_restarts:
        Maximum number of restart attempts before giving up and re-raising.
    on_permanent_failure:
        Optional async callback invoked with ``(name, exc)`` when all restarts
        are exhausted.  Called before the exception is re-raised.
    """
    attempt = 0
    while True:
        try:
            await coro_factory()
            return  # clean exit (e.g. CancelledError propagated from inside)
        except asyncio.CancelledError:
            raise  # always propagate — do not restart on cancellation
        except Exception as exc:
            if attempt >= max_restarts:
                logger.critical(
                    "Supervised task %r failed permanently after %d restarts: %s",
                    name, attempt, exc, exc_info=True,
                )
                if on_permanent_failure is not None:
                    await on_permanent_failure(name, exc)
                raise
            backoff = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            logger.error(
                "Supervised task %r crashed (attempt %d/%d), restarting in %.1fs: %s",
                name, attempt + 1, max_restarts, backoff, exc,
            )
            attempt += 1
            await asyncio.sleep(backoff)
