"""Integration test: full Collector cycle against mocked Bright Data."""

from __future__ import annotations

import re

import pytest
from prometheus_client import generate_latest

from brightdata_exporter.client import BrightDataClient, ZoneInfo
from brightdata_exporter.collector import (
    Collector,
    _zone_supports_dedicated_vips,
    _zone_supports_ips_per_country,
)
from brightdata_exporter.config import load_settings
from brightdata_exporter.metrics import Metrics
from brightdata_exporter.ratelimit import RateLimiter


@pytest.fixture
def populated_collector(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r".*?/customer/balance"),
        json={"balance": 1000, "credit": 0, "prepayment": 1500, "pending_costs": 12.5},
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/get_all_zones"),
        json=[
            {"name": "active_dc", "type": "dc", "status": "active"},
            {"name": "old_zone", "type": "dc", "status": "deleted"},
        ],
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone\?zone=active_dc$"),
        json={
            "created": "2026-01-01T00:00:00Z",
            "ips": ["1.1.1.1"],
            "perm": "country",
            "plan": {
                "product": "dc",
                "type": "static",
                "ips_type": "shared",
                "bandwidth": "payperusage",
            },
            "usage_limit": {"value": 50, "unit": "$", "cycle": "m"},
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/cost\?.*"),
        json={"u": {"custom": {"cost": 1.23, "bw": 100000, "gbs": 0.5}}},
        is_reusable=True,
    )
    # Per-zone /zone/bw — only used as fallback when /customer/bw misses
    # a zone. In normal cycles this isn't hit.
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/bw\?.*"),
        json={
            "c": {
                "sums": {
                    "active_dc": {
                        "custom": {
                            "bw_sum": 100000,
                            "bw_dn": 90000,
                            "bw_up": 10000,
                            "bw_sum_dc": 100000,
                            "https_direct_req": 50,
                            "http_direct_req": 5,
                        }
                    }
                }
            }
        },
        is_reusable=True,
        is_optional=True,
    )
    # Bulk bandwidth (replaces per-zone /zone/bw in normal cycles)
    httpx_mock.add_response(
        url=re.compile(r".*?/customer/bw\?.*"),
        json={
            "c": {
                "sums": {
                    "active_dc": {
                        "custom": {
                            "bw_sum": 100000,
                            "bw_dn": 90000,
                            "bw_up": 10000,
                            "bw_sum_dc": 100000,
                            "https_direct_req": 50,
                            "http_direct_req": 5,
                        }
                    }
                }
            }
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/status$"),
        json={
            "status": "active",
            "customer": "tester",
            "can_make_requests": True,
            "auth_fail_reason": "",
            "ip": "10.0.0.1",
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/network_status/all"),
        json={"status": True},
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/ips/unavailable"),
        json={},
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/proxies_pending_replacement"),
        json={},
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/recent_ips"),
        json={},
        is_reusable=True,
    )
    # Per-zone IP roster endpoints — opt-in default ON, plans without
    # IP visibility return 4xx; mock as 200 with empty so the test exercises
    # the publish path.
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/ips\?.*"),
        json={"br": 100, "us": 50},
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/route_vips\?.*"),
        json=[],
        is_reusable=True,
        # plan-aware gate skips this for non-VIP plans; mark optional so the
        # fixture works whether or not the gate decides to call.
        is_optional=True,
    )

    settings = load_settings(
        api_token="test",
        api_rate_limit_rps=1000.0,  # disable pacing for tests
        scrape_interval=60,
        info_cache_seconds=0,  # disable caching for tests
    )
    metrics = Metrics()
    limiter = RateLimiter(settings.api_rate_limit_rps)
    client = BrightDataClient(token="test", limiter=limiter)
    collector = Collector(client=client, metrics=metrics, settings=settings, limiter=limiter)
    yield collector, metrics
    client.close()


def test_collect_publishes_account_metrics(populated_collector):
    collector, metrics = populated_collector
    collector.collect_once()
    assert metrics.account_balance_usd._value.get() == 1000
    assert metrics.account_pending_costs_usd._value.get() == 12.5
    assert metrics.account_spent_this_month_usd._value.get() == pytest.approx(500)


def test_collect_skips_deleted_zones(populated_collector):
    collector, metrics = populated_collector
    collector.collect_once()
    payload = generate_latest(metrics.registry).decode()
    assert "active_dc" in payload
    # The deleted zone should NOT have any per-zone metric series.
    assert 'zone="old_zone"' not in payload


def test_collect_publishes_zone_status_counts(populated_collector):
    collector, metrics = populated_collector
    collector.collect_once()
    payload = generate_latest(metrics.registry).decode()
    assert 'brightdata_zones_total{status="active"} 1.0' in payload
    assert 'brightdata_zones_total{status="deleted"} 1.0' in payload


def test_collect_derives_rate_per_gb(populated_collector):
    collector, metrics = populated_collector
    collector.collect_once()
    payload = generate_latest(metrics.registry).decode()
    # cost=1.23, gbs=0.5  →  rate=2.46
    assert "brightdata_zone_rate_usd_per_gb" in payload
    # The exact float formatting comes out of prometheus_client; verify the
    # value is right by parsing the relevant line.
    line = next(
        ln for ln in payload.splitlines() if ln.startswith("brightdata_zone_rate_usd_per_gb{")
    )
    rate = float(line.rsplit(" ", 1)[1])
    assert rate == pytest.approx(2.46)


def test_collect_records_up_metric(populated_collector):
    collector, metrics = populated_collector
    collector.collect_once()
    assert metrics.up._value.get() == 1


# ---------------------------------------------------------------------------
# Plan-aware endpoint gating
# ---------------------------------------------------------------------------
#
# These tests target the regression where every scrape cycle hammered
# /zone/ips and /zone/route_vips on every active zone, even when the zone's
# plan deterministically rejects those endpoints (rotating-residential,
# datacenter-without-VIPs, SERP, mobile). A 14-zone account was generating
# ~430 spurious upstream errors per hour; the fix reads plan info from
# /zone?zone=NAME and skips the call when the plan can't support it.


def _build_collector(httpx_mock, plan: dict, status: str = "active"):
    """Stand up a collector against a single-zone fixture with custom plan."""
    httpx_mock.add_response(
        url=re.compile(r".*?/customer/balance"),
        json={"balance": 100, "credit": 0, "prepayment": 200, "pending_costs": 0},
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/customer/bw\?.*"),
        json={"c": {"sums": {}}},
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/get_all_zones"),
        json=[{"name": "z1", "type": plan.get("product", "dc"), "status": status}],
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone\?zone=z1$"),
        json={
            "created": "2026-01-01T00:00:00Z",
            "ips": [],
            "perm": "country",
            "plan": plan,
            "usage_limit": None,
        },
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/cost\?.*"),
        json={"u": {"custom": {"cost": 0, "bw": 0, "gbs": 0}}},
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/bw\?.*"),
        json={"c": {"sums": {}}},
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/recent_ips"),
        json={},
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/ips/unavailable"),
        json={},
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/proxies_pending_replacement"),
        json={},
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/network_status/all"),
        json={"status": True},
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/status$"),
        json={
            "status": "active",
            "customer": "t",
            "can_make_requests": True,
            "auth_fail_reason": "",
            "ip": "10.0.0.1",
        },
        is_reusable=True,
        is_optional=True,
    )

    settings = load_settings(
        api_token="test",
        api_rate_limit_rps=1000.0,
        scrape_interval=60,
        info_cache_seconds=0,
    )
    metrics = Metrics()
    limiter = RateLimiter(1000.0)
    client = BrightDataClient(token="test", limiter=limiter)
    collector = Collector(
        client=client,
        metrics=metrics,
        settings=settings,
        limiter=limiter,
    )
    return collector, metrics, client


def _called_paths(httpx_mock) -> list[str]:
    return [r.url.path for r in httpx_mock.get_requests()]


def test_skips_zone_ips_for_rotating_residential(httpx_mock):
    """Rotating-residential plans return 400 Wrong zone plan on /zone/ips —
    the collector should skip the call entirely instead of burning a request
    slot to learn nothing."""
    collector, _m, client = _build_collector(
        httpx_mock,
        plan={
            "product": "res_rotating",
            "type": "resident",
            "vips_type": "shared",
            "vip": False,
        },
    )
    try:
        collector.collect_once()
    finally:
        client.close()
    paths = _called_paths(httpx_mock)
    assert "/zone/ips" not in paths
    assert "/zone/route_vips" not in paths


def test_skips_route_vips_for_datacenter(httpx_mock):
    """Datacenter plans return 403 'Vip routes not found'. With the gate,
    /zone/ips still runs (datacenter exposes IP rosters) but /zone/route_vips
    is skipped."""
    collector, _m, client = _build_collector(
        httpx_mock,
        plan={
            "product": "dc",
            "type": "static",
            "ips_type": "shared",
        },
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/ips\?.*"),
        json={"br": 5},
        is_reusable=True,
        is_optional=True,
    )
    try:
        collector.collect_once()
    finally:
        client.close()
    paths = _called_paths(httpx_mock)
    assert "/zone/ips" in paths
    assert "/zone/route_vips" not in paths


def test_calls_route_vips_for_dedicated_vip_residential(httpx_mock):
    """Dedicated-VIP residential (vips_type=domain, vip=True) is the only
    plan that accepts /zone/route_vips — make sure we still call it there
    even though the zone is res_rotating product."""
    collector, _m, client = _build_collector(
        httpx_mock,
        plan={
            "product": "res_rotating",
            "type": "resident",
            "vips_type": "domain",
            "vip": True,
        },
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/route_vips\?.*"),
        json=[{"vip": "v1"}],
        is_reusable=True,
        is_optional=True,
    )
    try:
        collector.collect_once()
    finally:
        client.close()
    paths = _called_paths(httpx_mock)
    # Even though product is rotating residential, vips_type=domain unlocks
    # the VIP routes endpoint. /zone/ips stays skipped (rotating).
    assert "/zone/ips" not in paths
    assert "/zone/route_vips" in paths


def test_skips_both_for_serp_zone(httpx_mock):
    """SERP / mobile / unblocker plans expose neither endpoint — skip both."""
    collector, _m, client = _build_collector(
        httpx_mock,
        plan={
            "product": "serp",
            "type": "unblocker",
            "vips_type": "shared",
        },
    )
    try:
        collector.collect_once()
    finally:
        client.close()
    paths = _called_paths(httpx_mock)
    assert "/zone/ips" not in paths
    assert "/zone/route_vips" not in paths


def test_no_scrape_errors_recorded_for_skipped_endpoints(httpx_mock):
    """The whole point of the gate: skipped calls don't show up in
    `brightdata_exporter_scrape_errors_total`."""
    collector, metrics, client = _build_collector(
        httpx_mock,
        plan={
            "product": "res_rotating",
            "type": "resident",
            "vips_type": "shared",
        },
    )
    try:
        collector.collect_once()
    finally:
        client.close()
    payload = generate_latest(metrics.registry).decode()
    # No /zone/ips or /zone/route_vips error series should exist for the
    # skipped zone — gate filtered them out before they could fail.
    assert 'brightdata_exporter_scrape_errors_total{endpoint="/zone/ips"}' not in payload
    assert 'brightdata_exporter_scrape_errors_total{endpoint="/zone/route_vips"}' not in payload


# ---------------------------------------------------------------------------
# Plan-gate matrix — pin behavior for every plan we know about
# ---------------------------------------------------------------------------
#
# The audit flagged the gate matrix as incomplete: the integration tests above
# cover 4 plans but the production exporter sees 6+. These pure-function
# tests pin the contract for every known plan_product (verified empirically
# 2026-05-05 against api.brightdata.com) and an unknown future product so a
# new Bright Data plan family can't silently regress us.


def _zone_info_with_plan(
    *,
    product: str,
    vips_type: str = "",
    vip: bool = False,
    ips_type: str = "",
) -> ZoneInfo:
    """Build a minimal ZoneInfo for gate-helper tests."""
    return ZoneInfo(
        name="z",
        created="",
        description="",
        ips=[],
        perm="country",
        plan_product=product,
        plan_type="",
        plan_country="",
        plan_bandwidth="",
        plan_vips_type=vips_type,
        plan_ips_type=ips_type,
        plan_ips_count=None,
        plan_dualip=False,
        plan_vip=vip,
        usage_limit_value=None,
        usage_limit_unit="",
        usage_limit_cycle="",
        usage_limit_action="",
        raw={},
    )


@pytest.mark.parametrize(
    "product,expected",
    [
        ("dc", True),  # datacenter — verified 200
        ("isp", True),  # ISP/static — allow-by-default
        ("res_static", True),  # static residential — allow-by-default
        ("res_rotating", False),  # rotating residential — verified 400
        ("serp", False),  # SERP API — verified 400
        ("mobile", False),  # mobile — verified 400
        ("unblocker", False),  # unblocker — verified 400
        ("future_quantum_proxy", True),  # unknown product — allow until proven
    ],
)
def test_zone_supports_ips_per_country_matrix(product, expected):
    """Pin every product → /zone/ips support decision."""
    info = _zone_info_with_plan(product=product)
    assert _zone_supports_ips_per_country(info) is expected


@pytest.mark.parametrize(
    "product,vips_type,vip,expected",
    [
        # Only the dedicated-VIP combination unlocks /zone/route_vips.
        ("res_rotating", "domain", True, True),  # the LinkedIn-style case
        ("res_rotating", "domain", False, False),  # vip=False short-circuits
        ("res_rotating", "shared", False, False),  # rotating without VIPs
        ("dc", "", False, False),  # datacenter never has VIPs
        ("isp", "shared", False, False),
        ("serp", "", False, False),
        ("mobile", "", False, False),
        # Edge: vips_type set but missing the magic 'domain' value
        ("res_rotating", "exclusive", True, False),
    ],
)
def test_zone_supports_dedicated_vips_matrix(product, vips_type, vip, expected):
    """Pin every (product, vips_type, vip) → /zone/route_vips support decision."""
    info = _zone_info_with_plan(product=product, vips_type=vips_type, vip=vip)
    assert _zone_supports_dedicated_vips(info) is expected
