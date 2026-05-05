"""Unit tests for the Bright Data API client.

Real API responses (recorded against api.brightdata.com on 2026-05-05) are
replayed via pytest-httpx so the tests don't make outbound calls.
"""

from __future__ import annotations

import httpx
import pytest

from brightdata_exporter.client import (
    BrightDataAPIError,
    BrightDataClient,
    period_window,
)

TOKEN = "test-token-deadbeef"


@pytest.fixture
def client(httpx_mock):
    # httpx_mock is requested so pytest-httpx intercepts outbound requests;
    # the parameter itself is not referenced inside the fixture body.
    _ = httpx_mock
    c = BrightDataClient(token=TOKEN)
    yield c
    c.close()


def test_token_required():
    with pytest.raises(ValueError, match="token is required"):
        BrightDataClient(token="")


def test_balance_normalizes_lowercase_fields(client, httpx_mock):
    httpx_mock.add_response(
        url="https://api.brightdata.com/customer/balance",
        json={"balance": 1948.36, "credit": 0, "prepayment": 1999, "pending_costs": 50.64},
    )
    b = client.balance()
    assert b.balance == 1948.36
    assert b.credit == 0
    assert b.prepayment == 1999
    assert b.pending_costs == 50.64
    assert b.spent_this_month == pytest.approx(50.64, rel=1e-6)


def test_balance_spent_clamps_negative(client, httpx_mock):
    # When balance > prepayment (rare, but possible after a refund) the
    # "spent this month" tile in the UI shows 0, not a negative.
    httpx_mock.add_response(
        url="https://api.brightdata.com/customer/balance",
        json={"balance": 2100, "credit": 0, "prepayment": 1999, "pending_costs": 0},
    )
    assert client.balance().spent_this_month == 0.0


def test_all_zones_carries_status(client, httpx_mock):
    httpx_mock.add_response(
        url="https://api.brightdata.com/zone/get_all_zones",
        json=[
            {"name": "z1", "type": "dc", "status": "active"},
            {"name": "z2", "type": "res_rotating", "status": "disabled"},
            {"name": "z3", "type": "dc", "status": "deleted"},
        ],
    )
    zones = client.all_zones()
    assert [z.name for z in zones] == ["z1", "z2", "z3"]
    assert zones[1].status == "disabled"
    assert zones[2].status == "deleted"


def test_zone_info_residential_schema(client, httpx_mock):
    httpx_mock.add_response(
        url="https://api.brightdata.com/zone?zone=res_x",
        json={
            "created": "2026-05-04T20:07:07.154Z",
            "ips": ["1.1.1.1", "2.2.2.2"],
            "description": "scrapers",
            "perm": "country",
            "plan": {
                "product": "res_rotating",
                "type": "resident",
                "vips_type": "shared",
                "default_country": "br",
            },
            "usage_limit": {
                "value": 100,
                "unit": "$",
                "cycle": "m",
                "bust_action": "disable_notify",
            },
        },
    )
    info = client.zone_info("res_x")
    assert info.plan_product == "res_rotating"
    assert info.plan_country == "br"
    assert info.plan_vips_type == "shared"
    assert info.plan_ips_type == ""  # residential omits ips_type
    assert info.usage_limit_value == 100.0
    assert info.usage_limit_cycle == "m"
    assert info.ips == ["1.1.1.1", "2.2.2.2"]


def test_zone_info_datacenter_schema(client, httpx_mock):
    httpx_mock.add_response(
        url="https://api.brightdata.com/zone?zone=dc_x",
        json={
            "created": "2026-05-04T21:15:25.855Z",
            "ips": ["1.1.1.1"],
            "perm": "country",
            "plan": {
                "product": "dc",
                "type": "static",
                "ips_type": "shared",
                "bandwidth": "payperusage",
            },
            "usage_limit": {"value": 100, "unit": "$", "cycle": "m"},
        },
    )
    info = client.zone_info("dc_x")
    assert info.plan_product == "dc"
    assert info.plan_country == ""  # datacenter has no default_country
    assert info.plan_bandwidth == "payperusage"
    assert info.plan_vips_type == ""
    assert info.plan_ips_type == "shared"
    assert info.description == ""  # datacenter zone in this test has no description


def test_zone_cost_sums_custom_bucket(client, httpx_mock):
    httpx_mock.add_response(
        url="https://api.brightdata.com/zone/cost?zone=z&from=2026-04-05&to=2026-05-05",
        json={
            "alice": {
                "custom": {
                    "cost": 0.6772,
                    "bw": 1612531816,
                    "gbs": 1.6125,
                    "range": {"from": "5-Apr-2026", "to": "5-May-2026"},
                }
            }
        },
    )
    c = client.zone_cost("z", "2026-04-05", "2026-05-05")
    assert c.cost_usd == pytest.approx(0.6772)
    assert c.bw_bytes == 1612531816
    assert c.gbs == pytest.approx(1.6125)


def test_zone_bandwidth_extracts_zone_specific_sums(client, httpx_mock):
    httpx_mock.add_response(
        url="https://api.brightdata.com/zone/bw?zone=zdc&from=2026-04-05&to=2026-05-05",
        json={
            "c_xx": {
                "sums": {
                    "zdc": {
                        "custom": {
                            "bw_sum": 1881717961,
                            "bw_dn": 1868171709,
                            "bw_up": 13546252,
                            "bw_sum_dc": 1845028969,
                            "bw_api": 1844938026,
                            "https_direct_req": 6959,
                            "http_direct_req": 544,
                        }
                    }
                }
            }
        },
    )
    bw = client.zone_bandwidth("zdc", "2026-04-05", "2026-05-05")
    assert bw.bw_sum == 1881717961
    assert bw.bw_dn == 1868171709
    assert bw.bw_up == 13546252
    assert bw.bw_sum_dc == 1845028969
    assert bw.bw_sum_res == 0  # not in datacenter response
    assert bw.https_direct_req == 6959
    assert bw.http_direct_req == 544
    assert bw.https_svc_req == 0  # not in datacenter response
    assert bw.requests_total == 6959 + 544


def test_api_error_includes_status_and_body(client, httpx_mock):
    httpx_mock.add_response(
        url="https://api.brightdata.com/customer/balance",
        status_code=401,
        text="Unauthorized",
    )
    with pytest.raises(BrightDataAPIError) as exc_info:
        client.balance()
    assert exc_info.value.status == 401
    assert "Unauthorized" in exc_info.value.body


def test_network_error_wrapped(httpx_mock):
    # Simulate a complete network failure (httpx raises before any response).
    httpx_mock.add_exception(httpx.ConnectError("dns failure"))
    with BrightDataClient(token=TOKEN) as c:
        with pytest.raises(BrightDataAPIError) as exc_info:
            c.balance()
        assert exc_info.value.status == 0


def test_period_window_returns_iso_dates():
    from datetime import date

    pf, pt = period_window(7, today=date(2026, 5, 5))
    assert pf == "2026-04-28"
    assert pt == "2026-05-05"


# ---------------------------------------------------------------------------
# Upstream-failure contracts
# ---------------------------------------------------------------------------
#
# The audit found these were unverified — pinning the contract so a future
# "let's add retries" PR has a regression net.


def test_upstream_429_raises_brightdata_error_no_retry(client, httpx_mock):
    """Bright Data's 1 req/s limit makes 429 the canonical 'slow down'
    signal. Current contract: surface to caller, no automatic retry.
    Pacing is the rate limiter's job; recovery is the caller's job."""
    httpx_mock.add_response(
        url="https://api.brightdata.com/customer/balance",
        status_code=429,
        text="Too Many Requests",
    )
    with pytest.raises(BrightDataAPIError) as exc_info:
        client.balance()
    assert exc_info.value.status == 429
    assert "Too Many Requests" in exc_info.value.body


def test_upstream_500_raises_brightdata_error(client, httpx_mock):
    """5xx surfaces unmodified — the collector / service decides whether
    to retry, log, or bubble up."""
    httpx_mock.add_response(
        url="https://api.brightdata.com/customer/balance",
        status_code=503,
        text="Service Unavailable",
    )
    with pytest.raises(BrightDataAPIError) as exc_info:
        client.balance()
    assert exc_info.value.status == 503


def test_upstream_timeout_wrapped_with_status_zero(httpx_mock):
    """ReadTimeout has different semantics from ConnectError (request was
    sent, response is missing). Both wrap as status=0 to keep callers
    simple."""
    httpx_mock.add_exception(httpx.ReadTimeout("read timeout after 30s"))
    with BrightDataClient(token=TOKEN) as c:
        with pytest.raises(BrightDataAPIError) as exc_info:
            c.balance()
        assert exc_info.value.status == 0
        assert "timeout" in exc_info.value.body.lower()
