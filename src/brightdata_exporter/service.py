"""Ad-hoc REST service — the FinOps half of the hybrid.

While the periodic collector + /metrics path serves "monitoring" use cases
(alerts, time-series trends, fixed-window snapshots), the REST endpoints
in this module serve the "FinOps / billing investigation" use case where
the user picks an arbitrary date range from a dashboard time picker and
wants the answer reflecting THAT range.

Endpoints
---------

  GET /api/account
      Account snapshot — balance, credit, prepayment, status. No date
      range. Cached briefly because balance updates aren't constant.

  GET /api/zones
      List of zones (default status=active) with cost / traffic / requests
      / pricing / usage_limit for the requested period. The dashboard's
      Zones table consumes this via Grafana Infinity.

      Required query params: from, to (YYYY-MM-DD).
      Optional: status (comma-separated active|disabled|deleted),
                zone_filter (regex), include (comma-separated extras).

  GET /api/zones/<name>
      Same data as /api/zones for a single zone, plus the IP roster and
      VIP details when supported by the plan.

All handlers go through a shared TTL cache so concurrent dashboard viewers
collapse onto a single upstream fan-out per (path, params) combination.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

import structlog

from .cache import TTLCache, make_key
from .client import (
    BrightDataAPIError,
    BrightDataClient,
    ZoneListEntry,
)
from .config import Settings
from .pricing import pricing_pair

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors / response shapes
# ---------------------------------------------------------------------------


class ServiceError(Exception):
    """Raised for client-visible request errors. status -> HTTP code."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def _problem(status: int, message: str) -> tuple[int, bytes]:
    payload = json.dumps({"status": status, "error": message}).encode()
    return status, payload


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class BrightDataService:
    """REST handlers for /api/* paths.

    Stateless aside from the shared cache. All upstream fan-out goes through
    :class:`BrightDataClient`, which itself uses the shared
    :class:`RateLimiter` so concurrent service requests + scheduled scrapes
    respect Bright Data's 1 req/s/token limit together.
    """

    def __init__(
        self,
        client: BrightDataClient,
        cache: TTLCache,
        settings: Settings,
    ):
        self._client = client
        self._cache = cache
        self._settings = settings

    # -----------------------------------------------------------------
    # Public dispatch
    # -----------------------------------------------------------------

    def handle(self, path: str, params: dict[str, str]) -> tuple[int, bytes, str]:
        """Dispatch a GET to the right handler.

        Returns ``(status, body_bytes, content_type)``. Raises nothing — all
        client/upstream errors become structured JSON problem responses.
        """
        try:
            data = self._dispatch(path, params)
            payload = json.dumps(data, default=_json_default).encode()
            return 200, payload, "application/json; charset=utf-8"
        except ServiceError as exc:
            status, body = _problem(exc.status, exc.message)
            return status, body, "application/json; charset=utf-8"
        except BrightDataAPIError as exc:
            logger.warning(
                "service.upstream_error",
                endpoint=exc.endpoint,
                status=exc.status,
            )
            status, body = _problem(
                exc.status if 400 <= exc.status < 600 else 502,
                f"upstream {exc.endpoint}: {exc.body[:200] if exc.body else 'no body'}",
            )
            return status, body, "application/json; charset=utf-8"
        except Exception as exc:
            logger.exception("service.unexpected", error=str(exc))
            status, body = _problem(500, "internal server error")
            return status, body, "application/json; charset=utf-8"

    # -----------------------------------------------------------------
    # Routing
    # -----------------------------------------------------------------

    def _dispatch(self, path: str, params: dict[str, str]) -> Any:
        # Trim trailing slash for ergonomics.
        path = path.rstrip("/") or "/"

        if path == "/api/account":
            return self._cache.get_or_compute(
                make_key(path),
                self._account,
            )

        if path == "/api/zones":
            period_from, period_to = _require_period(params)
            period_from, period_to = _clamp_window(
                period_from, period_to, self._settings.api_min_window_days
            )
            return self._cache.get_or_compute(
                make_key(
                    path,
                    {
                        "from": period_from,
                        "to": period_to,
                        "status": params.get("status", ""),
                        "zone_filter": params.get("zone_filter", ""),
                    },
                ),
                lambda: self._zones(period_from, period_to, params),
            )

        if path.startswith("/api/zones/"):
            zone_name = path[len("/api/zones/") :]
            if not zone_name or "/" in zone_name:
                raise ServiceError(400, "invalid zone name")
            period_from, period_to = _require_period(params)
            period_from, period_to = _clamp_window(
                period_from, period_to, self._settings.api_min_window_days
            )
            return self._cache.get_or_compute(
                make_key(path, {"from": period_from, "to": period_to}),
                lambda: self._zone_detail(zone_name, period_from, period_to),
            )

        raise ServiceError(404, f"not found: {path}")

    # -----------------------------------------------------------------
    # Handlers
    # -----------------------------------------------------------------

    def _account(self) -> dict[str, Any]:
        balance = self._client.balance()
        status = self._client.status()
        all_zones = self._client.all_zones()

        zone_counts = {"active": 0, "disabled": 0, "deleted": 0}
        for z in all_zones:
            zone_counts[z.status] = zone_counts.get(z.status, 0) + 1

        return {
            "balance": {
                "balance_usd": balance.balance,
                "credit_usd": balance.credit,
                "prepayment_usd": balance.prepayment,
                "pending_costs_usd": balance.pending_costs,
                "spent_this_month_usd": balance.spent_this_month,
            },
            "status": {
                "status": status.status,
                "customer": status.customer,
                "can_make_requests": status.can_make_requests,
                "auth_fail_reason": status.auth_fail_reason,
                "ip": status.ip,
            },
            "zones": {
                "active": zone_counts.get("active", 0),
                "disabled": zone_counts.get("disabled", 0),
                "deleted": zone_counts.get("deleted", 0),
                "total": len(all_zones),
            },
        }

    def _zones(
        self,
        period_from: str,
        period_to: str,
        params: dict[str, str],
    ) -> list[dict[str, Any]]:
        all_zones = self._client.all_zones()
        wanted_status = _parse_status_filter(params.get("status"))
        zone_pattern = _compile_filter(params.get("zone_filter"))

        scrapeable: list[ZoneListEntry] = []
        for z in all_zones:
            if z.status not in wanted_status:
                continue
            if zone_pattern is not None and not zone_pattern.search(z.name):
                continue
            scrapeable.append(z)

        # Bulk bandwidth — single upstream call covers every zone.
        bw_by_zone = self._client.customer_bandwidth(period_from, period_to)

        rows: list[dict[str, Any]] = []
        for z in scrapeable:
            row = self._build_zone_row(z, period_from, period_to, bw_by_zone.get(z.name))
            rows.append(row)

        return rows

    def _zone_detail(
        self,
        zone_name: str,
        period_from: str,
        period_to: str,
    ) -> dict[str, Any]:
        all_zones = self._client.all_zones()
        match = next((z for z in all_zones if z.name == zone_name), None)
        if match is None:
            raise ServiceError(404, f"zone not found: {zone_name}")

        bw_map = self._client.customer_bandwidth(period_from, period_to)
        row = self._build_zone_row(match, period_from, period_to, bw_map.get(zone_name))

        # Best-effort enrichment — these endpoints reject some plan types,
        # so failures translate to "unavailable" rather than aborting.
        try:
            ips = self._client.zone_ips_per_country(zone_name)
            row["ips_per_country"] = ips.counts
        except BrightDataAPIError:
            row["ips_per_country"] = None
        try:
            vips = self._client.zone_dedicated_vips(zone_name)
            row["dedicated_vip_ids"] = vips.vip_ids
        except BrightDataAPIError:
            row["dedicated_vip_ids"] = None
        return row

    # -----------------------------------------------------------------
    # Row construction (shared between list + detail)
    # -----------------------------------------------------------------

    def _build_zone_row(
        self,
        zone: ZoneListEntry,
        period_from: str,
        period_to: str,
        bandwidth: Any,
    ) -> dict[str, Any]:
        # Skip per-zone INFO calls for non-scrapeable status to keep the
        # /api/zones response cheap; caller filtered to active+disabled.
        info = self._client.zone_info(zone.name)
        cost = self._client.zone_cost(zone.name, period_from, period_to)

        rate_display, model = pricing_pair(cost)
        usage_limit = (
            {
                "value": info.usage_limit_value,
                "unit": info.usage_limit_unit,
                "cycle": info.usage_limit_cycle,
                "action": info.usage_limit_action,
            }
            if info.usage_limit_value is not None
            else None
        )

        row: dict[str, Any] = {
            "name": zone.name,
            "type": info.display_type,
            "status": zone.status,
            "pool_tier": info.pool_tier,
            "rate_display": rate_display,
            "billing_model": model,
            "cost_usd": cost.cost_usd,
            "traffic_gb": cost.gbs,
            "traffic_bytes": cost.bw_bytes,
            "vips_billed": cost.vips,
            "ip_bandwidth_gbs": cost.gbs_ipbw,
            "dedicated_ips": cost.dedicated_ips,
            "serp_billable_requests": cost.serp_billable_requests,
            "usage_limit": usage_limit,
            "raw_product": info.plan_product,
            "raw_plan_type": info.plan_type,
            "country": info.plan_country,
            "perm": info.perm,
            "created": info.created,
            "description": info.description,
            "ip_count": len(info.ips),
            "period": {"from": period_from, "to": period_to},
        }

        if bandwidth is not None:
            row["traffic"] = {
                "total": bandwidth.bw_sum,
                "down": bandwidth.bw_dn,
                "up": bandwidth.bw_up,
                "datacenter": bandwidth.bw_sum_dc,
                "residential": bandwidth.bw_sum_res,
                "api": bandwidth.bw_api,
            }
            row["requests"] = {
                "https_direct": bandwidth.https_direct_req,
                "http_direct": bandwidth.http_direct_req,
                "https_svc": bandwidth.https_svc_req,
                "total": bandwidth.requests_total,
            }
        else:
            row["traffic"] = None
            row["requests"] = None
        return row


# ---------------------------------------------------------------------------
# Param parsing helpers
# ---------------------------------------------------------------------------


def _require_period(params: dict[str, str]) -> tuple[str, str]:
    pf = params.get("from", "").strip()
    pt = params.get("to", "").strip()
    if not pf or not pt:
        raise ServiceError(400, "from and to query params are required (YYYY-MM-DD)")
    if not _looks_like_date(pf) or not _looks_like_date(pt):
        raise ServiceError(400, "from/to must be YYYY-MM-DD")
    return pf, pt


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _looks_like_date(s: str) -> bool:
    return bool(_ISO_DATE.match(s))


def _clamp_window(period_from: str, period_to: str, min_days: int) -> tuple[str, str]:
    """Expand `from` backward when the requested window is below `min_days`.

    Bright Data's /zone/cost and /customer/bw aggregate billing data on a
    rolling daily cadence — a request whose `from`/`to` both land on the
    current day returns zero cost/traffic until the day rolls over,
    regardless of how much usage actually happened. Without a guard, a
    Grafana dashboard on "Last 5 minutes" renders an all-zero zones table
    that looks like a broken integration.

    Behaviour:
      * `min_days <= 0` — pass through unchanged (escape hatch).
      * Otherwise, if `(to - from).days < min_days`, set
        `from = to - min_days`; `to` is preserved so the picker's
        right edge stays meaningful.
    """
    if min_days <= 0:
        return period_from, period_to
    try:
        d_from = date.fromisoformat(period_from)
        d_to = date.fromisoformat(period_to)
    except ValueError:
        return period_from, period_to
    if (d_to - d_from).days < min_days:
        d_from = d_to - timedelta(days=min_days)
        return d_from.isoformat(), period_to
    return period_from, period_to


def _parse_status_filter(value: str | None) -> set[str]:
    if not value:
        return {"active"}  # sane default — most users want only active zones
    parts = {p.strip() for p in value.split(",") if p.strip()}
    valid = parts & {"active", "disabled", "deleted"}
    return valid or {"active"}


def _compile_filter(pattern: str | None) -> re.Pattern[str] | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ServiceError(400, f"invalid zone_filter regex: {exc}") from exc


def _json_default(obj: Any) -> Any:
    """Fallback JSON encoder for dataclass-like things."""
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    if isinstance(obj, Iterable):
        return list(obj)
    return str(obj)
