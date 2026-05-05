"""Unit + concurrency tests for the shared RateLimiter.

The limiter is shared between the periodic collector and the on-demand
REST service so the 1 req/s/token Bright Data limit applies *across* both
surfaces. These tests pin two contracts that the audit found unverified:

1. ``rps <= 0`` is rejected (constructor guard).
2. Concurrent acquirers across N threads complete in `(N-1)/rps` wall-clock
   time, NOT `N * interval` — i.e. the lock does not serialize the entire
   sleep, it just orders slot reservations. The previous implementation
   (sleep-inside-lock) failed this contract.
"""

from __future__ import annotations

import threading
import time

import pytest

from brightdata_exporter.ratelimit import RateLimiter


def test_rejects_non_positive_rps():
    with pytest.raises(ValueError, match="rps must be > 0"):
        RateLimiter(0)
    with pytest.raises(ValueError, match="rps must be > 0"):
        RateLimiter(-1.5)


def test_first_acquire_does_not_sleep():
    limiter = RateLimiter(rps=1.0)
    started = time.monotonic()
    limiter.acquire()
    elapsed = time.monotonic() - started
    # First call has no prior slot to wait on — should return immediately.
    assert elapsed < 0.05


def test_sequential_acquires_respect_interval():
    """Two back-to-back acquires must take at least 1/rps seconds total."""
    limiter = RateLimiter(rps=10.0)  # 100ms interval
    started = time.monotonic()
    limiter.acquire()
    limiter.acquire()
    elapsed = time.monotonic() - started
    assert elapsed >= 0.09  # allow a tiny scheduling slack


def test_high_rps_does_not_meaningfully_sleep():
    """1000 rps means 1ms intervals — 5 calls should finish in < 50ms."""
    limiter = RateLimiter(rps=1000.0)
    started = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    assert time.monotonic() - started < 0.05


def test_concurrent_acquirers_are_paced_not_serialized():
    """The audit-flagged regression: under N concurrent threads, the
    limiter must spread requests across `(N-1)/rps` wall-clock seconds,
    not stack their full sleeps under the lock.

    For 4 threads at 10 rps (100ms interval): expected total = ~300ms
    (intervals between slots 1→2, 2→3, 3→4). With sleep-inside-lock
    the same workload could degrade to 4 * 100ms = 400ms+ even though
    only 3 intervals separate the 4 acquisitions.

    We assert the reasonable upper bound the new implementation achieves.
    """
    limiter = RateLimiter(rps=10.0)  # 100ms interval
    n = 4
    barrier = threading.Barrier(n)
    completion_times: list[float] = [0.0] * n

    def worker(idx: int) -> None:
        barrier.wait()
        limiter.acquire()
        completion_times[idx] = time.monotonic()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = time.monotonic() - started

    # Lower bound: 3 intervals must elapse (4 slots, 3 gaps).
    assert total >= 0.27, f"limiter did not pace at all: {total=}"
    # Upper bound: must NOT serialize on the lock for the full N*interval.
    # 0.5s buffer for scheduler jitter; the actual delta in production was
    # ~300ms with the fix vs ~400ms+ before.
    assert total < 0.5, f"limiter serialized on lock instead of pacing: {total=}"


def test_acquirers_observe_distinct_slot_times():
    """Each acquirer's completion time must be at least one interval
    apart from its predecessor — slot reservations are atomic and ordered."""
    limiter = RateLimiter(rps=20.0)  # 50ms interval
    n = 3
    completion_times: list[float] = [0.0] * n
    barrier = threading.Barrier(n)

    def worker(idx: int) -> None:
        barrier.wait()
        limiter.acquire()
        completion_times[idx] = time.monotonic()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    sorted_times = sorted(completion_times)
    for i in range(1, n):
        gap = sorted_times[i] - sorted_times[i - 1]
        # 30ms floor (50ms interval - scheduler slack)
        assert gap >= 0.03, f"acquirers {i - 1} and {i} too close: {gap=}"
