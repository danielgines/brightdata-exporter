"""Tests for the REST service handlers."""

from __future__ import annotations

import json
import re

import pytest

from brightdata_exporter.cache import TTLCache
from brightdata_exporter.client import BrightDataClient
from brightdata_exporter.config import load_settings
from brightdata_exporter.service import BrightDataService


@pytest.fixture
def populated_service(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r".*?/customer/balance"),
        json={"balance": 100, "credit": 0, "prepayment": 200, "pending_costs": 5},
        is_reusable=True,
        is_optional=True,
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
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/get_all_zones"),
        json=[
            {"name": "active_dc", "type": "dc", "status": "active"},
            {"name": "old_zone", "type": "dc", "status": "deleted"},
        ],
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone\?zone=.*"),
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
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/cost\?.*"),
        json={"u": {"custom": {"cost": 1.23, "bw": 100000, "gbs": 0.5}}},
        is_reusable=True,
        is_optional=True,
    )
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
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/ips\?.*"), json={"br": 100}, is_reusable=True, is_optional=True
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/route_vips\?.*"), json=[], is_reusable=True, is_optional=True
    )

    settings = load_settings(api_token="test", api_rate_limit_rps=1000.0)
    client = BrightDataClient(token="test")
    cache = TTLCache(ttl_seconds=60)
    service = BrightDataService(client=client, cache=cache, settings=settings)
    yield service, cache, client
    client.close()


def test_account_endpoint_returns_balance_status_zones(populated_service):
    service, _cache, _client = populated_service
    status, body, _ct = service.handle("/api/account", {})
    assert status == 200
    payload = json.loads(body)
    assert payload["balance"]["balance_usd"] == 100
    assert payload["balance"]["spent_this_month_usd"] == 100  # prepayment - balance
    assert payload["status"]["status"] == "active"
    assert payload["zones"]["active"] == 1
    assert payload["zones"]["deleted"] == 1


def test_zones_endpoint_requires_from_to(populated_service):
    service, _cache, _client = populated_service
    status, body, _ct = service.handle("/api/zones", {})
    assert status == 400
    assert "from and to" in json.loads(body)["error"]


def test_zones_endpoint_validates_date_format(populated_service):
    service, _cache, _client = populated_service
    status, body, _ct = service.handle("/api/zones", {"from": "yesterday", "to": "today"})
    assert status == 400
    assert "YYYY-MM-DD" in json.loads(body)["error"]


def test_zones_endpoint_default_filters_to_active(populated_service):
    service, _cache, _client = populated_service
    status, body, _ct = service.handle("/api/zones", {"from": "2026-04-05", "to": "2026-05-05"})
    assert status == 200
    rows = json.loads(body)
    assert len(rows) == 1
    assert rows[0]["name"] == "active_dc"
    assert rows[0]["status"] == "active"
    assert rows[0]["cost_usd"] == 1.23
    assert rows[0]["traffic_gb"] == 0.5
    assert rows[0]["rate_display"] == "$2.46/GB"
    assert rows[0]["billing_model"] == "per_gb"


def test_zones_endpoint_status_filter_can_include_deleted(populated_service):
    service, _cache, _client = populated_service
    status, body, _ct = service.handle(
        "/api/zones",
        {"from": "2026-04-05", "to": "2026-05-05", "status": "active,deleted"},
    )
    assert status == 200
    rows = json.loads(body)
    assert {r["name"] for r in rows} == {"active_dc", "old_zone"}


def test_zones_endpoint_zone_filter_regex(populated_service):
    service, _cache, _client = populated_service
    status, body, _ct = service.handle(
        "/api/zones",
        {"from": "2026-04-05", "to": "2026-05-05", "zone_filter": "^old_"},
    )
    # zone_filter narrows to old_zone — but old_zone is `deleted` so default
    # status filter (active) excludes it. Result should be empty.
    assert status == 200
    assert json.loads(body) == []

    # When status includes deleted, zone_filter still applies.
    status, body, _ct = service.handle(
        "/api/zones",
        {"from": "2026-04-05", "to": "2026-05-05", "zone_filter": "^old_", "status": "deleted"},
    )
    rows = json.loads(body)
    assert len(rows) == 1
    assert rows[0]["name"] == "old_zone"


def test_zones_endpoint_invalid_regex(populated_service):
    service, _cache, _client = populated_service
    status, body, _ct = service.handle(
        "/api/zones",
        {"from": "2026-04-05", "to": "2026-05-05", "zone_filter": "[unclosed"},
    )
    assert status == 400
    assert "regex" in json.loads(body)["error"]


def test_zones_endpoint_uses_cache_for_repeat_call(populated_service):
    service, cache, _client = populated_service
    # First call — populates cache.
    service.handle("/api/zones", {"from": "2026-04-05", "to": "2026-05-05"})
    size_after_first = cache.stats()["size"]
    # Second call same params — should hit cache.
    service.handle("/api/zones", {"from": "2026-04-05", "to": "2026-05-05"})
    size_after_second = cache.stats()["size"]
    assert size_after_first == size_after_second == 1


def test_zone_detail_returns_full_payload(populated_service):
    service, _cache, _client = populated_service
    status, body, _ct = service.handle(
        "/api/zones/active_dc", {"from": "2026-04-05", "to": "2026-05-05"}
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["name"] == "active_dc"
    assert payload["traffic"]["total"] == 100000
    assert payload["requests"]["total"] == 55  # 50 https_direct + 5 http_direct
    # Best-effort fields:
    assert "ips_per_country" in payload
    assert "dedicated_vip_ids" in payload


def test_zone_detail_404_for_missing(populated_service):
    service, _cache, _client = populated_service
    status, body, _ct = service.handle(
        "/api/zones/does_not_exist",
        {"from": "2026-04-05", "to": "2026-05-05"},
    )
    assert status == 404
    assert "not found" in json.loads(body)["error"]


def test_unknown_path_returns_404(populated_service):
    service, _cache, _client = populated_service
    status, _body, _ct = service.handle("/api/unknown", {})
    assert status == 404


def test_zones_window_clamps_when_below_minimum(populated_service):
    """Picker on a single day expands `from` so upstream sees a real range."""
    service, _cache, _client = populated_service
    # Default api_min_window_days=1 — same-day picker should clamp.
    status, body, _ct = service.handle("/api/zones", {"from": "2026-05-05", "to": "2026-05-05"})
    assert status == 200
    rows = json.loads(body)
    assert len(rows) == 1
    # Upstream was actually queried for [2026-05-04, 2026-05-05] — but the
    # response echoes the queried period, so verify it shifted.
    assert rows[0]["period"] == {"from": "2026-05-04", "to": "2026-05-05"}


def test_zones_window_passes_through_when_above_minimum(populated_service):
    service, _cache, _client = populated_service
    _status, body, _ct = service.handle("/api/zones", {"from": "2026-04-05", "to": "2026-05-05"})
    rows = json.loads(body)
    assert rows[0]["period"] == {"from": "2026-04-05", "to": "2026-05-05"}


def test_zones_window_disabled_when_min_zero(httpx_mock):
    """min_window_days=0 turns the guard off entirely."""
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/get_all_zones"),
        json=[{"name": "z1", "type": "dc", "status": "active"}],
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone\?zone=.*"),
        json={
            "created": "2026-01-01T00:00:00Z",
            "ips": [],
            "perm": "country",
            "plan": {
                "product": "dc",
                "type": "static",
                "ips_type": "shared",
                "bandwidth": "payperusage",
            },
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
        url=re.compile(r".*?/customer/bw\?.*"),
        json={"c": {"sums": {}}},
        is_reusable=True,
        is_optional=True,
    )

    settings = load_settings(api_token="test", api_rate_limit_rps=1000.0, api_min_window_days=0)
    client = BrightDataClient(token="test")
    cache = TTLCache(ttl_seconds=60)
    service = BrightDataService(client=client, cache=cache, settings=settings)
    try:
        _status, body, _ct = service.handle(
            "/api/zones", {"from": "2026-05-05", "to": "2026-05-05"}
        )
        rows = json.loads(body)
        assert rows[0]["period"] == {"from": "2026-05-05", "to": "2026-05-05"}
    finally:
        client.close()


def test_zones_window_clamps_with_custom_minimum(httpx_mock):
    """api_min_window_days=7 expands a 3-day picker to 7 days."""
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/get_all_zones"),
        json=[{"name": "z1", "type": "dc", "status": "active"}],
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone\?zone=.*"),
        json={
            "created": "2026-01-01T00:00:00Z",
            "ips": [],
            "perm": "country",
            "plan": {
                "product": "dc",
                "type": "static",
                "ips_type": "shared",
                "bandwidth": "payperusage",
            },
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
        url=re.compile(r".*?/customer/bw\?.*"),
        json={"c": {"sums": {}}},
        is_reusable=True,
        is_optional=True,
    )

    settings = load_settings(api_token="test", api_rate_limit_rps=1000.0, api_min_window_days=7)
    client = BrightDataClient(token="test")
    cache = TTLCache(ttl_seconds=60)
    service = BrightDataService(client=client, cache=cache, settings=settings)
    try:
        _status, body, _ct = service.handle(
            "/api/zones", {"from": "2026-05-02", "to": "2026-05-05"}
        )
        rows = json.loads(body)
        # 7-day clamp shifts `from` back: 2026-05-05 minus 7d = 2026-04-28.
        assert rows[0]["period"] == {"from": "2026-04-28", "to": "2026-05-05"}
    finally:
        client.close()


def test_zone_detail_window_clamps(populated_service):
    service, _cache, _client = populated_service
    status, body, _ct = service.handle(
        "/api/zones/active_dc", {"from": "2026-05-05", "to": "2026-05-05"}
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["period"] == {"from": "2026-05-04", "to": "2026-05-05"}


def test_zones_window_passes_through_at_exactly_min_days(populated_service):
    """Boundary check: window EQUAL to api_min_window_days must NOT clamp.

    The audit flagged the strict ``<`` comparison as off-by-one bait.
    Default min_days=1, so a 1-day window (from 2026-05-04 to 2026-05-05)
    passes through unchanged. If the comparison ever flips to ``<=``,
    this test fails immediately.
    """
    service, _cache, _client = populated_service
    _status, body, _ct = service.handle("/api/zones", {"from": "2026-05-04", "to": "2026-05-05"})
    rows = json.loads(body)
    assert rows[0]["period"] == {"from": "2026-05-04", "to": "2026-05-05"}
