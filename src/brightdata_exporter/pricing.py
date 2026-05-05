"""Shared pricing-display helpers for collector + service.

Both the periodic collector (which writes ``brightdata_zone_pricing`` Info
metrics) and the on-demand REST service (which renders the Cost column in
``/api/zones`` responses) need to translate a ``ZoneCost`` into the same
human-readable rate string and billing model. Keeping the logic in one
module ensures the two surfaces never drift — a new SERP/CPM threshold or
a new ``per_vip`` rule has exactly one place to land.

The two outputs are paired: ``model`` is the machine-readable category and
``display`` is its human-readable form. They derive from the same set of
``ZoneCost`` fields, so deciding one without the other is a code smell.
"""

from __future__ import annotations

from .client import ZoneCost


def billing_model(cost: ZoneCost) -> str:
    """Categorize the billing scheme based on which /zone/cost fields are populated.

    Mirrors the five distinct schemas Bright Data emits per plan family
    (verified empirically 2026-05-05). When no field signals a model, returns
    ``unknown`` so callers can fall back gracefully.
    """
    if cost.gbs > 0:
        return "per_gb"
    if cost.serp_billable_requests > 0:
        return "per_kreq"
    if cost.dedicated_ips > 0:
        return "subscription"
    if cost.vips > 0:
        return "per_vip"
    if cost.cost_usd > 0:
        return "unknown"
    return "idle"


def pricing_display(cost: ZoneCost) -> str:
    """Render the human-readable rate string Bright Data shows in its UI.

    ``$X/GB``      — pay-per-GB plans (cost / gbs)
    ``$X/CPM``     — SERP API plans (cost / billable_requests * 1000)
    ``$X/month``   — subscription / dedicated-IP plans
    ``$X/VIP``     — residential dedicated VIP plans
    ``$X/period``  — fallback when cost > 0 but no scheme fits
    ``—``          — no usage and no fixed fee in the period
    """
    if cost.gbs > 0:
        return f"${cost.cost_usd / cost.gbs:.2f}/GB"
    if cost.serp_billable_requests > 0:
        return f"${cost.cost_usd / cost.serp_billable_requests * 1000:.2f}/CPM"
    if cost.dedicated_ips > 0:
        return f"${cost.cost_usd:.2f}/month"
    if cost.vips > 0 and cost.cost_usd > 0:
        return f"${cost.cost_usd / cost.vips:.2f}/VIP"
    if cost.cost_usd > 0:
        return f"${cost.cost_usd:.2f}/period"
    return "—"


def pricing_pair(cost: ZoneCost) -> tuple[str, str]:
    """Convenience: ``(display, model)`` together — both surfaces use this."""
    return pricing_display(cost), billing_model(cost)
