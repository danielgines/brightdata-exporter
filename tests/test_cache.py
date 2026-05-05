"""Tests for the TTL cache + single-flight semantics."""

from __future__ import annotations

import threading
import time

import pytest

from brightdata_exporter.cache import TTLCache, make_key


def test_cache_returns_value_on_first_compute():
    cache = TTLCache(ttl_seconds=60)
    calls = []

    def compute():
        calls.append(1)
        return "v1"

    assert cache.get_or_compute("k", compute) == "v1"
    assert len(calls) == 1


def test_cache_returns_cached_on_subsequent_calls():
    cache = TTLCache(ttl_seconds=60)
    calls = []
    cache.get_or_compute("k", lambda: calls.append(1) or "v1")
    cache.get_or_compute("k", lambda: calls.append(1) or "v2")
    cache.get_or_compute("k", lambda: calls.append(1) or "v3")
    assert len(calls) == 1  # only first call ran


def test_cache_recomputes_after_ttl():
    cache = TTLCache(ttl_seconds=0.05)
    calls = []
    cache.get_or_compute("k", lambda: calls.append(1) or "v1")
    time.sleep(0.1)
    cache.get_or_compute("k", lambda: calls.append(1) or "v2")
    assert len(calls) == 2


def test_cache_ttl_zero_recomputes_every_time():
    cache = TTLCache(ttl_seconds=0)
    calls = []
    cache.get_or_compute("k", lambda: calls.append(1) or "v")
    cache.get_or_compute("k", lambda: calls.append(1) or "v")
    assert len(calls) == 2


def test_cache_invalidate():
    cache = TTLCache(ttl_seconds=60)
    calls = []
    cache.get_or_compute("k", lambda: calls.append(1) or "v")
    cache.invalidate("k")
    cache.get_or_compute("k", lambda: calls.append(1) or "v")
    assert len(calls) == 2


def test_cache_clear():
    cache = TTLCache(ttl_seconds=60)
    cache.get_or_compute("a", lambda: 1)
    cache.get_or_compute("b", lambda: 2)
    cache.clear()
    assert cache.stats()["size"] == 0


def test_cache_lru_evicts_oldest():
    cache = TTLCache(ttl_seconds=60, max_size=2)
    cache.get_or_compute("a", lambda: 1)
    cache.get_or_compute("b", lambda: 2)
    cache.get_or_compute("c", lambda: 3)
    # `a` should have been evicted
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3


def test_cache_single_flight_collapses_concurrent_misses():
    """N concurrent threads asking for the same missing key → 1 compute call."""
    cache = TTLCache(ttl_seconds=60)
    call_count = 0
    started = threading.Event()
    proceed = threading.Event()
    lock = threading.Lock()

    def slow_compute():
        nonlocal call_count
        with lock:
            call_count += 1
        started.set()
        # Block until test releases — gives followers time to enter the wait state.
        proceed.wait(timeout=2)
        return "shared-value"

    n_threads = 10
    results: list[object] = []
    threads = [
        threading.Thread(target=lambda: results.append(cache.get_or_compute("hot", slow_compute)))
        for _ in range(n_threads)
    ]
    for t in threads:
        t.start()
    started.wait(timeout=2)  # wait until leader is inside compute()
    time.sleep(0.05)  # give followers a beat to queue up
    proceed.set()  # release the leader
    for t in threads:
        t.join(timeout=2)
        assert not t.is_alive()

    assert call_count == 1, f"expected 1 compute, got {call_count}"
    assert results == ["shared-value"] * n_threads


def test_cache_single_flight_propagates_exception():
    cache = TTLCache(ttl_seconds=60)
    exc = RuntimeError("boom")
    captured: list[BaseException] = []

    def failing_compute():
        time.sleep(0.05)
        raise exc

    def runner():
        try:
            cache.get_or_compute("err", failing_compute)
        except BaseException as e:
            captured.append(e)

    threads = [threading.Thread(target=runner) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)

    assert len(captured) == 5
    # All threads should see the same exception instance.
    for e in captured:
        assert e is exc


def test_make_key_sorts_params():
    assert make_key("/a", {"b": "1", "a": "2"}) == "/a?a=2&b=1"
    assert make_key("/a", {"a": "2", "b": "1"}) == "/a?a=2&b=1"


def test_make_key_drops_none_params():
    assert make_key("/a", {"a": "1", "b": None}) == "/a?a=1"  # type: ignore[dict-item]


def test_make_key_no_params():
    # No params dict at all — bare path.
    assert make_key("/a") is not None
    assert make_key("/a") == "/a"


def test_make_key_distinguishes_bare_path_from_empty_param_dict():
    """Critical correctness — different intents must produce different keys.

    A caller passing ``{}`` or ``{"x": ""}`` is signalling "this is the
    parametrized variant of /a", not "the bare /a route". If both collapsed
    to the same key, ``/api/account`` (bare) and ``/api/zones`` (with all
    blank params) could share a cache entry across routes.
    """
    assert make_key("/a") == "/a"
    assert make_key("/a", {}) == "/a?"
    assert make_key("/a", {"x": ""}) == "/a?"
    assert make_key("/a") != make_key("/a", {})


def test_make_key_distinguishes_paths_under_same_params():
    """Cross-path collision check — keys must encode the path."""
    assert make_key("/api/zones", {"from": "x"}) != make_key("/api/account", {"from": "x"})
    assert make_key("/api/zones") != make_key("/api/zones/active_dc")


def test_invalid_ttl_raises():
    with pytest.raises(ValueError, match="ttl_seconds"):
        TTLCache(ttl_seconds=-1)


def test_invalid_max_size_raises():
    with pytest.raises(ValueError, match="max_size"):
        TTLCache(ttl_seconds=60, max_size=0)


def test_invalid_single_flight_timeout_raises():
    with pytest.raises(ValueError, match="single_flight_timeout"):
        TTLCache(ttl_seconds=60, single_flight_timeout=0)
    with pytest.raises(ValueError, match="single_flight_timeout"):
        TTLCache(ttl_seconds=60, single_flight_timeout=-1)


def test_followers_time_out_when_leader_hangs():
    """Pinning the audit-flagged production hazard.

    A leader thread that hangs forever (or dies via SIGKILL mid-compute)
    must not stall every follower indefinitely — the cache enforces a
    bounded wait via ``single_flight_timeout``. Without this, a Bright
    Data API hang would slowly exhaust the HTTP server's thread pool.
    """
    cache = TTLCache(ttl_seconds=60, single_flight_timeout=0.3)
    leader_started = threading.Event()
    leader_blocked = threading.Event()

    def hanging_compute():
        leader_started.set()
        leader_blocked.wait()  # never set — simulate the hang
        return "should-never-return"

    leader_results: list[Exception | str] = []
    follower_results: list[Exception | str] = []

    def leader():
        try:
            leader_results.append(cache.get_or_compute("k", hanging_compute))
        except Exception as exc:
            leader_results.append(exc)

    def follower():
        leader_started.wait()
        try:
            follower_results.append(cache.get_or_compute("k", hanging_compute))
        except Exception as exc:
            follower_results.append(exc)

    leader_thread = threading.Thread(target=leader, daemon=True)
    follower_thread = threading.Thread(target=follower, daemon=True)
    leader_thread.start()
    follower_thread.start()

    # Follower's wait deadline is 300ms — give it 600ms total slack.
    follower_thread.join(timeout=1.0)
    assert not follower_thread.is_alive(), "follower should have timed out"
    assert len(follower_results) == 1
    assert isinstance(follower_results[0], TimeoutError)

    # Cleanup: release the leader so daemon thread doesn't linger.
    leader_blocked.set()
