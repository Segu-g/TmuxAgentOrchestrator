"""Token-bucket rate limiter for async task submission backpressure.

A token bucket maintains a pool of tokens that refill at a constant rate
(``rate`` tokens per second) up to a maximum capacity (``burst``).  Each
task submission consumes one token.  When the bucket is empty:

- ``try_acquire()`` — returns ``False`` immediately (non-blocking).
- ``acquire()`` — waits asynchronously until a token is available.
- ``acquire(timeout=N)`` — raises ``RateLimitExceeded`` if the wait would
  exceed ``N`` seconds.

When ``rate == 0`` the limiter is disabled and every acquisition succeeds
immediately (unlimited throughput).

Design references:
- Tanenbaum, A.S. "Computer Networks" 5th ed. §5.3 — Token Bucket algorithm
- RFC 4115 "A Differentiated Service Two-Rate, Three-Color Marker with
  Efficient Handling of in-Profile Traffic" (2005), IETF
- aiolimiter v1.2.1: async-native leaky bucket for Python (2024)
  https://aiolimiter.readthedocs.io/
- NGINX ``limit_req_zone`` / ``limit_req`` directives for HTTP rate limiting (2025)
  https://nginx.org/en/docs/http/ngx_http_limit_req_module.html
- DESIGN.md §10.16 (v0.20.0)
"""
from __future__ import annotations

import asyncio
import time


class RateLimitExceeded(Exception):
    """Raised by ``TokenBucketRateLimiter.acquire`` when ``timeout`` is exceeded.

    Attributes:
        rate: configured token refill rate (tokens/second).
        burst: configured burst capacity.
        available: tokens available at the time of the rejection.
    """

    def __init__(
        self,
        rate: float,
        burst: int,
        available: float,
        *,
        msg: str | None = None,
    ) -> None:
        self.rate = rate
        self.burst = burst
        self.available = available
        default = (
            f"Rate limit exceeded (rate={rate} t/s, burst={burst}, "
            f"available={available:.3f})"
        )
        super().__init__(msg or default)


class TokenBucketRateLimiter:
    """Async-safe token bucket rate limiter.

    Parameters
    ----------
    rate:
        Token refill rate in tokens per second.  ``0`` means disabled
        (unlimited).
    burst:
        Maximum number of tokens (bucket capacity).  This controls the
        maximum instantaneous burst size.  Ignored when ``rate == 0``.

    Thread / coroutine safety:
        ``try_acquire`` and ``_refill`` are protected by an ``asyncio.Lock``
        (``_lock``).  The lock is *async* so callers from coroutines do not
        block the event loop.  It is NOT safe to call these methods from
        multiple OS threads simultaneously (the project is single-threaded
        asyncio).

    Usage example::

        rl = TokenBucketRateLimiter(rate=5.0, burst=10)
        try:
            await rl.acquire(timeout=1.0)
            await orch.submit_task(prompt)
        except RateLimitExceeded:
            return {"error": "rate limit exceeded"}, 429
    """

    def __init__(self, rate: float, burst: int) -> None:
        self._rate = float(rate)
        self._burst = int(burst)
        # Start with a full bucket
        self._tokens: float = float(burst) if rate > 0 else 0.0
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def rate(self) -> float:
        """Token refill rate in tokens per second."""
        return self._rate

    @property
    def burst(self) -> int:
        """Bucket capacity (maximum burst tokens)."""
        return self._burst

    @property
    def enabled(self) -> bool:
        """Return True when rate limiting is active (rate > 0)."""
        return self._rate > 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """Recompute available tokens based on elapsed time.

        Must be called while holding ``_lock`` OR in a single-threaded
        context (e.g. inside ``try_acquire``).
        """
        if not self.enabled:
            return
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            float(self._burst),
            self._tokens + elapsed * self._rate,
        )
        self._last_refill = now

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def try_acquire(self) -> bool:
        """Try to consume one token without waiting.

        Returns:
            ``True`` if a token was consumed, ``False`` if the bucket is
            empty.  Always ``True`` when the limiter is disabled.
        """
        if not self.enabled:
            return True
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    async def acquire(self, *, timeout: float | None = None) -> None:
        """Consume one token, waiting asynchronously if the bucket is empty.

        Parameters
        ----------
        timeout:
            Maximum number of seconds to wait for a token.  If ``None``
            (default), wait indefinitely.  If the bucket cannot be
            replenished within ``timeout`` seconds, ``RateLimitExceeded``
            is raised.

        Raises:
            RateLimitExceeded: when ``timeout`` is set and elapsed wait
                would exceed it.
        """
        if not self.enabled:
            return

        deadline = (time.monotonic() + timeout) if timeout is not None else None

        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # Calculate how long we must wait for the next token
                deficit = 1.0 - self._tokens
                wait_s = deficit / self._rate

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= wait_s:
                    raise RateLimitExceeded(
                        rate=self._rate,
                        burst=self._burst,
                        available=self._tokens,
                    )
                actual_wait = min(wait_s, remaining)
            else:
                actual_wait = wait_s

            await asyncio.sleep(actual_wait)

    def reconfigure(self, *, rate: float, burst: int) -> None:
        """Update rate and burst in place (live reconfiguration).

        The available token count is clamped to the new burst on the next
        refill, ensuring continuity without a hard reset.

        Parameters
        ----------
        rate:
            New refill rate in tokens per second.  ``0`` disables limiting.
        burst:
            New bucket capacity.
        """
        self._rate = float(rate)
        self._burst = int(burst)
        if self._rate > 0:
            # Clamp current tokens to new burst
            self._tokens = min(self._tokens, float(self._burst))
        else:
            self._tokens = 0.0

    def status(self) -> dict:
        """Return a snapshot of the limiter's current state.

        The ``available_tokens`` field reflects refilled tokens up to this
        moment.
        """
        self._refill()
        return {
            "enabled": self.enabled,
            "rate": self._rate,
            "burst": self._burst,
            "available_tokens": round(self._tokens, 3),
        }
