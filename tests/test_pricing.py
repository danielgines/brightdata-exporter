"""Tests for the shared pricing module — the single source of truth for
the rate_display + billing_model that both /metrics and /api/zones expose.

Before extraction, this logic lived twice (collector + service) and could
silently drift. These tests pin every billing scheme so a fix in one
surface reaches both.
"""

from __future__ import annotations

import pytest

from brightdata_exporter.client import ZoneCost
from brightdata_exporter.pricing import billing_model, pricing_display, pricing_pair


def _cost(**overrides) -> ZoneCost:
    """Construct a ZoneCost with sensible zero defaults."""
    base = {
        "name": "z",
        "cost_usd": 0.0,
        "bw_bytes": 0,
        "gbs": 0.0,
        "vips": 0,
        "gbs_ipbw": 0.0,
        "dedicated_ips": 0,
        "serp_billable_requests": 0,
        "period_from": "",
        "period_to": "",
    }
    base.update(overrides)
    return ZoneCost(**base)


@pytest.mark.parametrize(
    "cost,expected_display,expected_model",
    [
        # Per-GB: $5 cost / 1 GB = $5/GB
        (_cost(cost_usd=5.0, gbs=1.0), "$5.00/GB", "per_gb"),
        # SERP CPM: $1.13 / 1132 reqs * 1000 = $1.00/CPM (rounding)
        (_cost(cost_usd=1.132, serp_billable_requests=1132), "$1.00/CPM", "per_kreq"),
        # Subscription with dedicated IPs: cost rendered as monthly fee
        (_cost(cost_usd=240.0, dedicated_ips=7700), "$240.00/month", "subscription"),
        # Per-VIP: cost / vips
        (_cost(cost_usd=200.0, vips=4), "$50.00/VIP", "per_vip"),
        # Idle — no usage and no fixed fee
        (_cost(), "—", "idle"),
        # Cost present but no recognized scheme — fallback
        (_cost(cost_usd=42.0), "$42.00/period", "unknown"),
    ],
)
def test_pricing_pair_covers_all_billing_schemes(cost, expected_display, expected_model):
    display, model = pricing_pair(cost)
    assert display == expected_display
    assert model == expected_model


def test_per_gb_takes_precedence_over_other_schemes():
    """A zone with both gbs > 0 and a SERP signal renders as per_gb —
    pins the priority order so a future schema change doesn't silently
    flip a zone's billing model."""
    cost = _cost(cost_usd=5.0, gbs=1.0, serp_billable_requests=999)
    display, model = pricing_pair(cost)
    assert model == "per_gb"
    assert display.endswith("/GB")


def test_pricing_display_and_billing_model_are_consistent():
    """The two helpers must always agree on which scheme applies."""
    cases = [
        _cost(cost_usd=10.0, gbs=2.0),
        _cost(cost_usd=300.0, dedicated_ips=100),
        _cost(),
    ]
    for c in cases:
        display, model = pricing_pair(c)
        assert pricing_display(c) == display
        assert billing_model(c) == model
