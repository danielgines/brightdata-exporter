"""TTL cache with single-flight semantics.

Two responsibilities, both unavoidable for an in-process service that proxies
a rate-limited upstream:

  1. **TTL** — cache responses for a configurable window (default 5 min) so
     repeated dashboard queries with the same parameters don't redo upstream
     calls every page load.

  2. **Single-flight** — when N concurrent requests miss the cache for the
     SAME key, only ONE upstream call is made; the others wait for that
     call to complete and share its result. Without this, a fleet of
     viewers refreshing the dashboard simultaneously would each issue
     their own upstream call, swamp the 1 req/s rate limit, and produce
     duplicated work.

Bounded LRU eviction keeps memory predictable in the face of dashboards
with very wide variable space (e.g. a `$zone` template variable that
multiplies key cardinality).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import structlog

logger = structlog.get_logger(__name__)


T = TypeVar("T")


@dataclass
class _Entry:
    expires_at: float
    value: object


class _Promise:
    """Single-flight promise — leader fills it, followers wait + read."""

    __slots__ = ("error", "event", "value")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: object | None = None
        self.error: BaseException | None = None

    def fulfill(self, value: object) -> None:
        self.value = value
        self.event.set()

    def reject(self, error: BaseException) -> None:
        self.error = error
        self.event.set()

    def wait(self, timeout: float | None = None) -> object:
        """Block until the leader fulfills/rejects this promise.

        ``timeout`` (seconds) caps the wait. Without it, a leader thread
        that hangs (or dies via SIGKILL mid-compute) would stall every
        follower forever — eventually exhausting the HTTP server's
        thread pool. Raises ``TimeoutError`` when the deadline passes.
        """
        if not self.event.wait(timeout=timeout):
            raise TimeoutError(f"single-flight compute() did not complete within {timeout}s")
        if self.error is not None:
            raise self.error
        return self.value


class TTLCache:
    """Thread-safe TTL cache with single-flight + LRU bound.

    Keys are arbitrary strings — callers compose them from
    (path, sorted query params).
    """

    def __init__(
        self,
        ttl_seconds: float,
        max_size: int = 1000,
        single_flight_timeout: float = 60.0,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be >= 0")
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        if single_flight_timeout <= 0:
            raise ValueError("single_flight_timeout must be > 0")
        self._ttl = ttl_seconds
        self._max = max_size
        # Hard cap on how long a follower will wait on the leader's
        # compute() — protects the HTTP thread pool when an upstream
        # call hangs or the leader thread dies mid-flight.
        self._single_flight_timeout = single_flight_timeout
        self._store: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()
        # Per-key promises — a Promise holds the in-progress compute() and
        # collects the result for followers. Replaces the previous
        # event-with-side-channel-dicts approach which had cleanup races.
        self._inflight: dict[str, _Promise] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_compute(self, key: str, compute: Callable[[], T]) -> T:
        """Return cached value for ``key`` or compute via ``compute()``.

        Concurrent calls for the same missing key collapse: only the first
        thread invokes ``compute``; the rest block on a per-key promise
        and share the result (or the same exception, re-raised).
        """
        # Phase 1 — claim leadership or identify as follower.
        leader_promise: _Promise | None = None
        with self._lock:
            entry = self._store.get(key)
            if entry is not None and entry.expires_at > time.monotonic():
                self._store.move_to_end(key)
                return entry.value  # type: ignore[return-value]

            self._store.pop(key, None)  # drop stale entry
            promise = self._inflight.get(key)
            if promise is None:
                promise = _Promise()
                self._inflight[key] = promise
                leader_promise = promise

        # Phase 2 — leader computes; followers wait on the same promise.
        if leader_promise is None:
            return promise.wait(timeout=self._single_flight_timeout)  # type: ignore[return-value]

        # Leader path — compute outside the lock.
        try:
            value = compute()
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(key, None)
            leader_promise.reject(exc)
            raise

        with self._lock:
            self._store[key] = _Entry(
                expires_at=time.monotonic() + self._ttl,
                value=value,
            )
            self._store.move_to_end(key)
            self._evict_if_needed_locked()
            self._inflight.pop(key, None)
        leader_promise.fulfill(value)
        return value

    def get(self, key: str) -> object | None:
        """Return cached value or None if missing/expired. Does not compute."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None or entry.expires_at <= time.monotonic():
                return None
            self._store.move_to_end(key)
            return entry.value

    def invalidate(self, key: str) -> None:
        """Drop a single key. No-op if absent."""
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        """Drop everything."""
        with self._lock:
            self._store.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "size": len(self._store),
                "max_size": self._max,
                "inflight": len(self._inflight),
            }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _evict_if_needed_locked(self) -> None:
        while len(self._store) > self._max:
            oldest_key, _ = self._store.popitem(last=False)
            logger.debug("cache.evict", key=oldest_key)


def make_key(path: str, params: dict[str, str] | None = None) -> str:
    """Compose a stable cache key from path + sorted params.

    ``GET /api/zones?from=2026-04-05&to=2026-05-05`` and
    ``GET /api/zones?to=2026-05-05&from=2026-04-05`` collapse to the same key.

    Empty/None params are dropped, but the resulting key still encodes
    that *some* params were provided — ``make_key("/x")`` and
    ``make_key("/x", {"a": ""})`` produce different keys to prevent
    accidental cross-route cache hits when a caller passes an empty
    param dict that happens to filter to nothing.
    """
    if params is None:
        return path
    sorted_pairs = sorted((k, v) for k, v in params.items() if v)
    if not sorted_pairs:
        # Distinguish "I passed params (all empty)" from "I passed no
        # params" — same path but different intent → different keys.
        return f"{path}?"
    qs = "&".join(f"{k}={v}" for k, v in sorted_pairs)
    return f"{path}?{qs}"
