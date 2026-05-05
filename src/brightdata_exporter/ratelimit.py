"""Shared rate limiter — paces all upstream calls (collector + service).

Single instance, shared via the dependency-injected wiring in __main__,
so periodic scrapes and on-demand /api/* requests respect Bright Data's
documented 1 req/s/token limit together. Without sharing, the two could
issue overlapping requests and trip rate-limit responses.

Token-bucket-ish (a leaky bucket): each acquire() blocks until at least
1/rps seconds have passed since the previous acquire. Good enough for a
sync caller against a sync rate limit.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Thread-safe leaky-bucket pacing helper."""

    def __init__(self, rps: float) -> None:
        if rps <= 0:
            raise ValueError("rps must be > 0")
        self._min_interval = 1.0 / rps
        self._last = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until the next request slot is allowed.

        The lock guards the slot reservation (mutating ``_last``); the
        wait happens *outside* the lock so concurrent acquirers form an
        ordered queue rather than serializing on the lock for the full
        sleep duration. Without this split, a 1 req/s limiter would
        force every concurrent caller to wait `n * min_interval` even
        when slots reserved earlier had already been served.
        """
        with self._lock:
            now = time.monotonic()
            # Reserve the next slot at max(now, _last + interval). Update
            # _last immediately so the next acquirer reserves a slot
            # *after* this one, even if its sleep hasn't started yet.
            scheduled_at = max(now, self._last + self._min_interval)
            self._last = scheduled_at
        wait = scheduled_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
