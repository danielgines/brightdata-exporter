"""Collector — orchestrates one full scrape cycle.

The collector is intentionally synchronous. Concurrency is not useful when
upstream limits to 1 req/s/token, and a sequential loop is far easier to
reason about (and to test).

A scrape cycle does, in order:
  1. /customer/balance                              (1 call, account totals)
  2. /zone/get_all_zones                            (1 call, full zone list)
  3. for each non-deleted zone matching the filter:
       /zone?zone=<name>                            (cached, info_cache_seconds)
       /zone/cost?zone=&from=&to=                   (window stats)
       /zone/bw?zone=&from=&to=                     (window stats)

A token-bucket rate limiter (api_rate_limit_rps) paces the requests so we
respect Bright Data's 1 req/s policy without bursts.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import structlog

from .client import (
    AccountStatus,
    Balance,
    BrightDataAPIError,
    BrightDataClient,
    DomainConsumption,
    NetworkStatus,
    ZoneBandwidth,
    ZoneCost,
    ZoneInfo,
    ZoneIPsPerCountry,
    ZoneListEntry,
    ZoneVIPRoutes,
    period_window,
)
from .config import Settings
from .metrics import Metrics
from .pricing import billing_model, pricing_display
from .ratelimit import RateLimiter

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Plan-aware endpoint gates
# ---------------------------------------------------------------------------
#
# Bright Data's per-zone enrichment endpoints (/zone/ips, /zone/route_vips)
# reject zones whose plan doesn't support that signal — and they reject
# *deterministically*, not transiently. Calling them anyway means burning a
# rate-limited request slot per zone per cycle, then incrementing
# `brightdata_exporter_scrape_errors_total` for what is, semantically, "this
# plan doesn't expose this data" rather than a real failure.
#
# The gates below short-circuit those calls based on `/zone?zone=NAME`,
# which we already fetch (and cache) on every scrape. Empirically verified
# against api.brightdata.com on 2026-05-05.

# Plan products that reject /zone/ips?ip_per_country=true with HTTP 400
# "Wrong zone plan". Rotating-pool plans don't have stable IP rosters to
# expose; SERP / mobile / unblocker similarly don't surface per-IP data.
_PRODUCTS_WITHOUT_IP_ROSTER = frozenset({"res_rotating", "serp", "mobile", "unblocker"})


def _zone_supports_ips_per_country(info: ZoneInfo) -> bool:
    """Whether /zone/ips?ip_per_country=true will return a roster.

    Verified: datacenter (`dc`) and ISP/static plans return 200. Rotating
    residential, SERP, mobile, and unblocker plans return 400 "Wrong zone
    plan" regardless of vips_type — even VIP-equipped rotating residential
    (e.g. `vips_type=domain`, `vip=1`) is rejected here.
    """
    return info.plan_product not in _PRODUCTS_WITHOUT_IP_ROSTER


def _zone_supports_dedicated_vips(info: ZoneInfo) -> bool:
    """Whether /zone/route_vips will return gIP routes.

    Verified: only dedicated-VIP residential zones return 200. The positive
    signal is `plan.vips_type == "domain"` AND `plan.vip == True`. Datacenter
    zones return 403 "Vip routes not found"; rotating residential without
    VIPs returns 422 "endpoint not available with the chosen zone".
    """
    return info.plan_vips_type == "domain" and info.plan_vip


# ---------------------------------------------------------------------------
# Zone info cache
# ---------------------------------------------------------------------------


@dataclass
class _CachedInfo:
    info: ZoneInfo
    fetched_at: float


class _InfoCache:
    """TTL cache for /zone?zone=NAME results.

    Plan / usage_limit change rarely; refreshing them every cycle wastes API
    budget when most of it should go to /zone/cost + /zone/bw.
    """

    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._store: dict[str, _CachedInfo] = {}

    def get(self, zone: str) -> ZoneInfo | None:
        entry = self._store.get(zone)
        if entry is None:
            return None
        if self._ttl <= 0:
            return None
        if time.monotonic() - entry.fetched_at > self._ttl:
            return None
        return entry.info

    def put(self, zone: str, info: ZoneInfo) -> None:
        self._store[zone] = _CachedInfo(info=info, fetched_at=time.monotonic())

    def evict(self, zones: set[str]) -> None:
        """Drop entries whose names are not in `zones` — i.e. zones deleted."""
        for k in list(self._store):
            if k not in zones:
                self._store.pop(k, None)


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class Collector:
    """Orchestrates a single scrape cycle and updates Prometheus metrics."""

    def __init__(
        self,
        client: BrightDataClient,
        metrics: Metrics,
        settings: Settings,
        limiter: RateLimiter,
    ):
        self._client = client
        self._metrics = metrics
        self._settings = settings
        self._limiter = limiter
        self._cache = _InfoCache(settings.info_cache_seconds)
        self._filter: re.Pattern[str] | None = settings.filter_pattern()

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def collect_once(self) -> None:
        """Run one full scrape cycle.

        Order is chosen to fail-fast on auth/zone-list problems and to
        amortize cheap account-wide calls before per-zone fan-out:

          1. /customer/balance + /status         (account identity)
          2. /zone/get_all_zones                  (drives the per-zone loop)
          3. /customer/bw  (bulk — replaces per-zone /zone/bw)
          4. /network_status/all                  (provider health)
          5. /zone/ips/unavailable + /zone/proxies_pending_replacement
             + /zone/recent_ips                   (silent-when-healthy)
          6. per-zone:
               /zone?zone=                        (info, TTL-cached)
               /zone/cost?zone=                   (no bulk equivalent)
               (bandwidth comes from the bulk call above)
               /zone/ips?ip_per_country=true     (opt-in; rejected by
                 rotating-residential)
               /zone/route_vips                   (opt-in; rejected by DC)
          7. /domains/bw + /domains/req           (opt-in, high cardinality)

        Errors on individual zones / endpoints are caught and counted but
        do not abort the cycle — partial data beats nothing.
        """
        scrape_start = time.monotonic()
        cycle_ok = True
        with self._metrics.scrape_duration_seconds.time():
            self._metrics.reset_per_zone_metrics()

            # ---- 1) account-level signals -----------------------------
            try:
                balance = self._fetch_balance()
                self._publish_balance(balance)
            except BrightDataAPIError as exc:
                cycle_ok = False
                self._record_error("/customer/balance", exc)

            try:
                acct = self._fetch_status()
                self._publish_account_status(acct)
            except BrightDataAPIError as exc:
                cycle_ok = False
                self._record_error("/status", exc)

            # ---- 2) zone roster ---------------------------------------
            try:
                zones = self._fetch_all_zones()
            except BrightDataAPIError as exc:
                cycle_ok = False
                self._record_error("/zone/get_all_zones", exc)
                zones = []

            self._publish_zone_counts(zones)

            scrapeable = self._select_zones(zones)
            self._cache.evict({z.name for z in zones})

            period_from, period_to = period_window(self._settings.period_days)

            # ---- 3) bulk bandwidth (substitutes per-zone /zone/bw) ----
            bw_by_zone: dict[str, ZoneBandwidth] = {}
            try:
                bw_by_zone = self._fetch_customer_bandwidth(period_from, period_to)
            except BrightDataAPIError as exc:
                cycle_ok = False
                self._record_error("/customer/bw", exc)

            # ---- 4) global network status -----------------------------
            if self._settings.collect_network_status:
                try:
                    self._publish_network_status(self._fetch_network_status("all"))
                except BrightDataAPIError as exc:
                    cycle_ok = False
                    self._record_error("/network_status/all", exc)

            # ---- 5) silent-when-healthy operational signals -----------
            zone_label_lookup = {z.name: (z.type, z.status) for z in zones}

            if self._settings.collect_ip_health:
                try:
                    unavail = self._fetch_zone_ips_unavailable()
                    self._publish_zone_ips_unavailable(unavail, zone_label_lookup)
                except BrightDataAPIError as exc:
                    cycle_ok = False
                    self._record_error("/zone/ips/unavailable", exc)
                try:
                    pending = self._fetch_proxies_pending_replacement()
                    self._publish_proxies_pending_replacement(pending, zone_label_lookup)
                except BrightDataAPIError as exc:
                    cycle_ok = False
                    self._record_error("/zone/proxies_pending_replacement", exc)

            if self._settings.collect_recent_ips:
                try:
                    recent = self._fetch_zone_recent_ips()
                    self._publish_zone_recent_ips(recent, zone_label_lookup)
                except BrightDataAPIError as exc:
                    cycle_ok = False
                    self._record_error("/zone/recent_ips", exc)

            # ---- 6) per-zone scrape -----------------------------------
            for zone in scrapeable:
                if not self._scrape_one_zone(zone, period_from, period_to, bw_by_zone):
                    cycle_ok = False

            # ---- 7) domain consumption (opt-in) -----------------------
            if self._settings.collect_domain_consumption:
                zone_names = [z.name for z in scrapeable]
                for metric in ("bw", "req"):
                    try:
                        dc = self._fetch_domain_consumption(
                            metric, period_from, period_to, zone_names
                        )
                        self._publish_domain_consumption(dc, zone_label_lookup)
                    except BrightDataAPIError as exc:
                        cycle_ok = False
                        self._record_error(f"/domains/{metric}", exc)

        self._metrics.last_scrape_timestamp_seconds.set(time.time())
        self._metrics.up.set(1 if cycle_ok else 0)
        elapsed = time.monotonic() - scrape_start
        logger.info(
            "scrape.complete",
            ok=cycle_ok,
            zones_total=len(zones),
            zones_scraped=len(scrapeable),
            elapsed_seconds=round(elapsed, 2),
        )

    # ------------------------------------------------------------------
    # Helpers — each wrapped in rate-limit + counter bookkeeping
    # ------------------------------------------------------------------

    def _fetch_balance(self) -> Balance:
        balance = self._client.balance()
        self._metrics.api_requests_total.labels(endpoint="/customer/balance", code="200").inc()
        return balance

    def _fetch_all_zones(self) -> list[ZoneListEntry]:
        zones = self._client.all_zones()
        self._metrics.api_requests_total.labels(endpoint="/zone/get_all_zones", code="200").inc()
        return zones

    def _fetch_zone_info(self, zone: str) -> ZoneInfo:
        cached = self._cache.get(zone)
        if cached is not None:
            return cached
        info = self._client.zone_info(zone)
        self._metrics.api_requests_total.labels(endpoint="/zone", code="200").inc()
        self._cache.put(zone, info)
        return info

    def _fetch_zone_cost(self, zone: str, pf: str, pt: str) -> ZoneCost:
        cost = self._client.zone_cost(zone, pf, pt)
        self._metrics.api_requests_total.labels(endpoint="/zone/cost", code="200").inc()
        return cost

    def _fetch_zone_bw(self, zone: str, pf: str, pt: str) -> ZoneBandwidth:
        bw = self._client.zone_bandwidth(zone, pf, pt)
        self._metrics.api_requests_total.labels(endpoint="/zone/bw", code="200").inc()
        return bw

    def _fetch_customer_bandwidth(self, pf: str, pt: str) -> dict[str, ZoneBandwidth]:
        result = self._client.customer_bandwidth(pf, pt)
        self._metrics.api_requests_total.labels(endpoint="/customer/bw", code="200").inc()
        return result

    def _fetch_status(self) -> AccountStatus:
        s = self._client.status()
        self._metrics.api_requests_total.labels(endpoint="/status", code="200").inc()
        return s

    def _fetch_network_status(self, network: str) -> NetworkStatus:
        ns = self._client.network_status(network)
        self._metrics.api_requests_total.labels(
            endpoint=f"/network_status/{network}", code="200"
        ).inc()
        return ns

    def _fetch_zone_ips_unavailable(self) -> dict[str, list[str]]:
        out = self._client.zone_ips_unavailable()
        self._metrics.api_requests_total.labels(endpoint="/zone/ips/unavailable", code="200").inc()
        return out

    def _fetch_proxies_pending_replacement(self) -> dict[str, Any]:
        out = self._client.zone_proxies_pending_replacement()
        self._metrics.api_requests_total.labels(
            endpoint="/zone/proxies_pending_replacement", code="200"
        ).inc()
        return out

    def _fetch_zone_recent_ips(self) -> dict[str, list[str]]:
        out = self._client.zone_recent_ips()
        self._metrics.api_requests_total.labels(endpoint="/zone/recent_ips", code="200").inc()
        return out

    def _fetch_zone_ips_per_country(self, zone: str) -> ZoneIPsPerCountry:
        out = self._client.zone_ips_per_country(zone)
        self._metrics.api_requests_total.labels(endpoint="/zone/ips", code="200").inc()
        return out

    def _fetch_zone_dedicated_vips(self, zone: str) -> ZoneVIPRoutes:
        out = self._client.zone_dedicated_vips(zone)
        self._metrics.api_requests_total.labels(endpoint="/zone/route_vips", code="200").inc()
        return out

    def _fetch_domain_consumption(
        self, metric: str, pf: str, pt: str, zones: list[str]
    ) -> DomainConsumption:
        out = self._client.domain_consumption(metric, pf, pt, zones)
        self._metrics.api_requests_total.labels(endpoint=f"/domains/{metric}", code="200").inc()
        return out

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _select_zones(self, zones: list[ZoneListEntry]) -> list[ZoneListEntry]:
        out: list[ZoneListEntry] = []
        for z in zones:
            if z.status == "deleted":
                continue
            if z.status == "disabled" and not self._settings.include_disabled:
                continue
            if self._filter and not self._filter.search(z.name):
                continue
            out.append(z)
        return out

    # ------------------------------------------------------------------
    # Per-zone scrape (info + cost + bw + opt-in IP rosters)
    # ------------------------------------------------------------------

    def _scrape_one_zone(
        self,
        zone: ZoneListEntry,
        pf: str,
        pt: str,
        bw_by_zone: dict[str, ZoneBandwidth],
    ) -> bool:
        ok = True
        # Label dict matches ZONE_LABELS = ("zone", "type", "status"); kept
        # as kwargs because prometheus_client.Metric.labels() refuses to mix
        # positional args with extra named ones (which we need for `cycle`,
        # `direction`, `proto`).
        labels = {"zone": zone.name, "type": zone.type, "status": zone.status}

        info: ZoneInfo | None = None
        try:
            info = self._fetch_zone_info(zone.name)
        except BrightDataAPIError as exc:
            ok = False
            self._record_error("/zone", exc)

        if info is not None:
            self._publish_zone_info(zone, info, labels)

        cost: ZoneCost | None = None
        try:
            cost = self._fetch_zone_cost(zone.name, pf, pt)
        except BrightDataAPIError as exc:
            ok = False
            self._record_error("/zone/cost", exc)

        if cost is not None:
            self._publish_zone_cost(cost, labels)

        # Bandwidth comes from the bulk /customer/bw call (one HTTP per
        # cycle for the whole account). Fall back to per-zone /zone/bw
        # only when the bulk call yielded no entry for this zone.
        bw = bw_by_zone.get(zone.name)
        if bw is None:
            try:
                bw = self._fetch_zone_bw(zone.name, pf, pt)
            except BrightDataAPIError as exc:
                ok = False
                self._record_error("/zone/bw", exc)

        if bw is not None:
            self._publish_zone_bandwidth(bw, labels)

        # Opt-in: per-zone IP rosters. Gated by plan capability — see the
        # _zone_supports_* helpers at the top of this module. Skipping
        # plan-incompatible calls saves rate-limit budget AND keeps
        # `brightdata_exporter_scrape_errors_total` an honest signal
        # (errors there now mean a real upstream failure, not "this plan
        # doesn't expose this endpoint").
        if self._settings.collect_ip_rosters and info is not None:
            if _zone_supports_ips_per_country(info):
                try:
                    ipc = self._fetch_zone_ips_per_country(zone.name)
                    self._publish_zone_ips_per_country(ipc, labels)
                except BrightDataAPIError as exc:
                    self._record_error("/zone/ips", exc)
            if _zone_supports_dedicated_vips(info):
                try:
                    vips = self._fetch_zone_dedicated_vips(zone.name)
                    self._publish_zone_dedicated_vips(vips, labels)
                except BrightDataAPIError as exc:
                    self._record_error("/zone/route_vips", exc)

        return ok

    # ------------------------------------------------------------------
    # Publish — translate dataclass -> metric writes
    # ------------------------------------------------------------------

    def _publish_balance(self, b: Balance) -> None:
        self._metrics.account_balance_usd.set(b.balance)
        self._metrics.account_credit_usd.set(b.credit)
        self._metrics.account_prepayment_usd.set(b.prepayment)
        self._metrics.account_pending_costs_usd.set(b.pending_costs)
        self._metrics.account_spent_this_month_usd.set(b.spent_this_month)

    def _publish_zone_counts(self, zones: list[ZoneListEntry]) -> None:
        counts: dict[str, int] = {}
        for z in zones:
            counts[z.status] = counts.get(z.status, 0) + 1
        self._metrics.zones_total.clear()
        for status, n in counts.items():
            self._metrics.zones_total.labels(status=status or "unknown").set(n)

    def _publish_zone_info(
        self, zone: ZoneListEntry, info: ZoneInfo, labels: dict[str, str]
    ) -> None:
        # `_info` series — one per zone — encodes static configuration as
        # labels (Prometheus `Info` metric pattern). Two derived labels —
        # `display_type` and `pool_tier` — are computed from raw plan fields
        # so the dashboard can render Bright Data UI's "Type" and "Security"
        # columns without re-implementing the heuristic in PromQL/transforms.
        self._metrics.zone_info.labels(zone=zone.name).info(
            {
                "type": zone.type,
                "status": zone.status,
                "display_type": info.display_type,
                "pool_tier": info.pool_tier,
                "product": info.plan_product,
                "plan_type": info.plan_type,
                "vips_type": info.plan_vips_type,
                "ips_type": info.plan_ips_type,
                "country": info.plan_country,
                "bandwidth": info.plan_bandwidth,
                "dualip": str(info.plan_dualip).lower(),
                "perm": info.perm,
                "description": info.description,
                "created": info.created,
            }
        )

        if info.usage_limit_value is not None and info.usage_limit_unit == "$":
            cycle = info.usage_limit_cycle or "m"
            self._metrics.zone_usage_limit_usd.labels(**labels, cycle=cycle).set(
                info.usage_limit_value
            )

    def _publish_zone_cost(self, cost: ZoneCost, labels: dict[str, str]) -> None:
        self._metrics.zone_cost_usd.labels(**labels).set(cost.cost_usd)
        self._metrics.zone_traffic_gb.labels(**labels).set(cost.gbs)
        if cost.gbs > 0:
            self._metrics.zone_rate_usd_per_gb.labels(**labels).set(cost.cost_usd / cost.gbs)

        # Plan-specific fields — emit only when present so dashboards can
        # branch on the metric's existence to know which billing model is in
        # play. (Pay-per-GB has gbs > 0; subscription has dedicated_ips > 0;
        # SERP has serp_billable_requests > 0; etc.)
        if cost.gbs_ipbw > 0:
            self._metrics.zone_ip_bandwidth_gbs.labels(**labels).set(cost.gbs_ipbw)
        if cost.dedicated_ips > 0:
            self._metrics.zone_dedicated_ip_count.labels(**labels).set(cost.dedicated_ips)
            if cost.cost_usd > 0:
                self._metrics.zone_rate_usd_per_ip.labels(**labels).set(
                    cost.cost_usd / cost.dedicated_ips
                )
        if cost.vips > 0:
            self._metrics.zone_vips_billed.labels(**labels).set(cost.vips)
        if cost.serp_billable_requests > 0:
            self._metrics.zone_serp_billable_requests.labels(**labels).set(
                cost.serp_billable_requests
            )
            if cost.cost_usd > 0:
                self._metrics.zone_rate_usd_per_kreq.labels(**labels).set(
                    cost.cost_usd / cost.serp_billable_requests * 1000
                )

        # Human-readable rate display matching Bright Data UI "Cost" column.
        # Branches on which auxiliary field is present in /zone/cost — this
        # is what tells us the billing model (pay-per-GB / SERP / subscription).
        # When NO usage in the period (cost == 0 and no aux field) the
        # display is "—" so the column never shows an unhelpful "$0.00".
        # Logic lives in pricing.py — shared with the REST service so the
        # /metrics rate_display and /api/zones rate_display can never drift.
        self._metrics.zone_pricing.labels(zone=cost.name).info(
            {
                "rate_display": pricing_display(cost),
                "model": billing_model(cost),
            }
        )

    def _publish_zone_bandwidth(self, bw: ZoneBandwidth, labels: dict[str, str]) -> None:
        self._metrics.zone_traffic_bytes.labels(**labels, direction="total").set(bw.bw_sum)
        self._metrics.zone_traffic_bytes.labels(**labels, direction="dn").set(bw.bw_dn)
        self._metrics.zone_traffic_bytes.labels(**labels, direction="up").set(bw.bw_up)
        if bw.bw_sum_dc > 0:
            self._metrics.zone_traffic_bytes.labels(**labels, direction="dc").set(bw.bw_sum_dc)
        if bw.bw_sum_res > 0:
            self._metrics.zone_traffic_bytes.labels(**labels, direction="res").set(bw.bw_sum_res)
        if bw.bw_api > 0:
            self._metrics.zone_traffic_bytes.labels(**labels, direction="api").set(bw.bw_api)

        self._metrics.zone_requests.labels(**labels, proto="https_direct").set(bw.https_direct_req)
        self._metrics.zone_requests.labels(**labels, proto="http_direct").set(bw.http_direct_req)
        self._metrics.zone_requests.labels(**labels, proto="https_svc").set(bw.https_svc_req)
        self._metrics.zone_requests.labels(**labels, proto="total").set(bw.requests_total)

    # ------------------------------------------------------------------
    # Account-level + global publishers
    # ------------------------------------------------------------------

    def _publish_account_status(self, s: AccountStatus) -> None:
        self._metrics.account_can_make_requests.set(1 if s.can_make_requests else 0)
        self._metrics.account_info.info(
            {
                "status": s.status,
                "customer": s.customer,
                "ip": s.ip,
                "auth_fail_reason": s.auth_fail_reason,
            }
        )

    def _publish_network_status(self, ns: NetworkStatus) -> None:
        self._metrics.network_status.labels(network=ns.network).set(1 if ns.operational else 0)

    # ------------------------------------------------------------------
    # Per-zone "silent when healthy" publishers — keys come from API
    # responses, not the zone roster, so we look up labels by name.
    # ------------------------------------------------------------------

    def _zone_labels_for(
        self, zone: str, lookup: dict[str, tuple[str, str]]
    ) -> dict[str, str] | None:
        meta = lookup.get(zone)
        if meta is None:
            # Zone reported by an operational endpoint but absent from
            # /zone/get_all_zones — likely race or transient. Skip silently
            # so we don't emit unlabeled series.
            return None
        return {"zone": zone, "type": meta[0], "status": meta[1]}

    def _publish_zone_ips_unavailable(
        self,
        unavail: dict[str, list[str]],
        zone_label_lookup: dict[str, tuple[str, str]],
    ) -> None:
        for zone_name, ips in unavail.items():
            labels = self._zone_labels_for(zone_name, zone_label_lookup)
            if labels is None:
                continue
            self._metrics.zone_ips_unavailable.labels(**labels).set(len(ips))

    def _publish_proxies_pending_replacement(
        self,
        pending: dict[str, Any],
        zone_label_lookup: dict[str, tuple[str, str]],
    ) -> None:
        for zone_name, payload in pending.items():
            labels = self._zone_labels_for(zone_name, zone_label_lookup)
            if labels is None:
                continue
            # Payload shape varies (count vs list vs dict). Pick the most
            # informative cardinality we can extract.
            if isinstance(payload, list):
                count = len(payload)
            elif isinstance(payload, dict):
                count = int(payload.get("count") or len(payload))
            elif isinstance(payload, int | float):
                count = int(payload)
            else:
                count = 0
            self._metrics.zone_proxies_pending_replacement.labels(**labels).set(count)

    def _publish_zone_recent_ips(
        self,
        recent: dict[str, list[str]],
        zone_label_lookup: dict[str, tuple[str, str]],
    ) -> None:
        for zone_name, ips in recent.items():
            labels = self._zone_labels_for(zone_name, zone_label_lookup)
            if labels is None:
                continue
            self._metrics.zone_recent_ips.labels(**labels).set(len(ips))

    def _publish_zone_ips_per_country(self, ipc: ZoneIPsPerCountry, labels: dict[str, str]) -> None:
        for country, count in ipc.counts.items():
            self._metrics.zone_ips_per_country.labels(**labels, country=country).set(count)

    def _publish_zone_dedicated_vips(self, vips: ZoneVIPRoutes, labels: dict[str, str]) -> None:
        if vips.vip_ids:
            self._metrics.zone_dedicated_vips.labels(**labels).set(len(vips.vip_ids))

    def _publish_domain_consumption(
        self,
        dc: DomainConsumption,
        zone_label_lookup: dict[str, tuple[str, str]],
    ) -> None:
        target = (
            self._metrics.zone_domain_traffic_bytes
            if dc.metric == "bw"
            else self._metrics.zone_domain_requests
        )
        for zone_name, by_domain in dc.by_zone.items():
            labels = self._zone_labels_for(zone_name, zone_label_lookup)
            if labels is None:
                continue
            for domain, value in by_domain.items():
                target.labels(**labels, domain=domain).set(value)

    # ------------------------------------------------------------------
    # Error bookkeeping
    # ------------------------------------------------------------------

    def _record_error(self, endpoint: str, exc: BrightDataAPIError) -> None:
        self._metrics.scrape_errors_total.labels(endpoint=endpoint).inc()
        self._metrics.api_requests_total.labels(endpoint=endpoint, code=str(exc.status or 0)).inc()
        logger.warning(
            "scrape.endpoint_error",
            endpoint=endpoint,
            status=exc.status,
            body=exc.body[:200] if exc.body else "",
        )
