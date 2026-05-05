"""Prometheus metric definitions.

All gauges + counters are registered against an explicit CollectorRegistry so
the server can render exactly what the exporter publishes (no Python process
defaults like `process_*` and `python_*` unless we opt in — we do, since they
are useful for ops).
"""

from __future__ import annotations

from prometheus_client import (
    GC_COLLECTOR,
    PLATFORM_COLLECTOR,
    PROCESS_COLLECTOR,
    CollectorRegistry,
    Counter,
    Gauge,
    Info,
    Summary,
)

NAMESPACE = "brightdata"

ZONE_LABELS = ("zone", "type", "status")
"""Common label set for per-zone metrics. Matches /zone/get_all_zones output."""


class Metrics:
    """Holds all Prometheus metric handles + the registry that contains them.

    The collector mutates these gauges; the server renders the registry.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)

        # Re-register the standard process / GC / platform collectors so the
        # exporter exposes process_resident_memory_bytes etc. against THIS
        # registry (the default ones live on the global registry which we
        # don't render).
        self.registry.register(PROCESS_COLLECTOR)
        self.registry.register(PLATFORM_COLLECTOR)
        self.registry.register(GC_COLLECTOR)

        # ---- Account-level ----
        self.account_balance_usd = Gauge(
            f"{NAMESPACE}_account_balance_usd",
            "Current account balance (USD). From /customer/balance.balance.",
            registry=self.registry,
        )
        self.account_credit_usd = Gauge(
            f"{NAMESPACE}_account_credit_usd",
            "Account credit (USD). From /customer/balance.credit.",
            registry=self.registry,
        )
        self.account_prepayment_usd = Gauge(
            f"{NAMESPACE}_account_prepayment_usd",
            "Total prepayment deposited on the account (USD). From /customer/balance.prepayment.",
            registry=self.registry,
        )
        self.account_pending_costs_usd = Gauge(
            f"{NAMESPACE}_account_pending_costs_usd",
            "Costs accrued but not yet billed (USD). From /customer/balance.pending_costs.",
            registry=self.registry,
        )
        self.account_spent_this_month_usd = Gauge(
            f"{NAMESPACE}_account_spent_this_month_usd",
            "Convention: prepayment - balance. Matches the 'Spent this "
            "month' tile in the Bright Data dashboard.",
            registry=self.registry,
        )

        # ---- Zone counts (by status) ----
        self.zones_total = Gauge(
            f"{NAMESPACE}_zones_total",
            "Total number of zones on the account, broken down by status. "
            "From /zone/get_all_zones.",
            labelnames=("status",),
            registry=self.registry,
        )

        # ---- Per-zone metrics ----
        self.zone_info = Info(
            f"{NAMESPACE}_zone",
            "Static configuration of a zone (one info series per zone). "
            "Values come from /zone?zone=NAME and /zone/get_all_zones.",
            labelnames=("zone",),
            registry=self.registry,
        )
        self.zone_cost_usd = Gauge(
            f"{NAMESPACE}_zone_cost_usd",
            "Total cost (USD) for the zone over the configured period. From /zone/cost.cost.",
            labelnames=ZONE_LABELS,
            registry=self.registry,
        )
        self.zone_traffic_bytes = Gauge(
            f"{NAMESPACE}_zone_traffic_bytes",
            "Total traffic for the zone over the configured period (bytes). "
            "Direction = total | dn | up | dc | res | api. "
            "Sourced from /zone/bw .sums.<zone>.custom.bw_*.",
            labelnames=(*ZONE_LABELS, "direction"),
            registry=self.registry,
        )
        self.zone_traffic_gb = Gauge(
            f"{NAMESPACE}_zone_traffic_gb",
            "Total traffic in GB-seconds for the zone. From /zone/cost.gbs.",
            labelnames=ZONE_LABELS,
            registry=self.registry,
        )
        self.zone_requests = Gauge(
            f"{NAMESPACE}_zone_requests",
            "Number of requests for the zone over the configured period, "
            "broken down by protocol. proto = https_direct | http_direct | "
            "https_svc | total. From /zone/bw .sums.<zone>.custom.*_req.",
            labelnames=(*ZONE_LABELS, "proto"),
            registry=self.registry,
        )
        self.zone_usage_limit_usd = Gauge(
            f"{NAMESPACE}_zone_usage_limit_usd",
            "Spend cap configured on the zone (USD). From /zone.usage_limit. "
            "Cycle label is 'm' (month) | 'd' (day) | 'h' (hour). "
            "When the zone has no limit, the metric is absent.",
            labelnames=(*ZONE_LABELS, "cycle"),
            registry=self.registry,
        )
        self.zone_rate_usd_per_gb = Gauge(
            f"{NAMESPACE}_zone_rate_usd_per_gb",
            "Derived rate: cost / gbs over the configured period. "
            "Only emitted when gbs > 0. Bright Data does not expose an "
            "explicit per-GB rate, so this is the best available signal.",
            labelnames=ZONE_LABELS,
            registry=self.registry,
        )

        # ---- Per-zone — billing-model-specific (sparse) ----
        # Bright Data /zone/cost returns different keys per plan. These five
        # gauges expose the variants so dashboards can pick the right rate
        # for each zone (per-GB / per-IP / per-1k-requests).
        self.zone_ip_bandwidth_gbs = Gauge(
            f"{NAMESPACE}_zone_ip_bandwidth_gbs",
            "IP-bandwidth GB-seconds for the zone over the configured period. "
            "Only present for subscription / dedicated-IP plans (Bright Data "
            "key `gbs_ipbw`).",
            labelnames=ZONE_LABELS,
            registry=self.registry,
        )
        self.zone_dedicated_ip_count = Gauge(
            f"{NAMESPACE}_zone_dedicated_ip_count",
            "Number of dedicated IPs allocated to the zone (key `ips` in "
            "/zone/cost). Subscription / dedicated-IP plans only.",
            labelnames=ZONE_LABELS,
            registry=self.registry,
        )
        self.zone_vips_billed = Gauge(
            f"{NAMESPACE}_zone_vips_billed",
            "Number of VIP IPs the cost applies to (key `vips` in "
            "/zone/cost). Residential dedicated VIP plans only.",
            labelnames=ZONE_LABELS,
            registry=self.registry,
        )
        self.zone_serp_billable_requests = Gauge(
            f"{NAMESPACE}_zone_serp_billable_requests",
            "Number of billable SERP requests in the period (key "
            "`reqs_serp` in /zone/cost). SERP-API zones only.",
            labelnames=ZONE_LABELS,
            registry=self.registry,
        )
        self.zone_rate_usd_per_kreq = Gauge(
            f"{NAMESPACE}_zone_rate_usd_per_kreq",
            "Derived rate: cost / reqs_serp * 1000 = USD per 1000 requests "
            "(CPM). Only emitted for SERP zones with billable requests > 0.",
            labelnames=ZONE_LABELS,
            registry=self.registry,
        )
        self.zone_rate_usd_per_ip = Gauge(
            f"{NAMESPACE}_zone_rate_usd_per_ip",
            "Derived rate: cost / dedicated_ip_count = USD per IP per "
            "period. Only emitted for subscription / dedicated-IP plans.",
            labelnames=ZONE_LABELS,
            registry=self.registry,
        )
        self.zone_pricing = Info(
            f"{NAMESPACE}_zone_pricing",
            "Human-readable rate string for the zone matching the Bright "
            "Data UI 'Cost' column. Composed from /zone/cost: '$X/GB' for "
            "pay-per-GB plans, '$X/CPM' for SERP, '$X/month' for "
            "subscription / dedicated-IP plans, or '$X/period' as fallback.",
            labelnames=("zone",),
            registry=self.registry,
        )

        # ---- Exporter introspection ----
        self.scrape_duration_seconds = Summary(
            f"{NAMESPACE}_exporter_scrape_duration_seconds",
            "Duration of a full scrape cycle.",
            registry=self.registry,
        )
        self.last_scrape_timestamp_seconds = Gauge(
            f"{NAMESPACE}_exporter_last_scrape_timestamp_seconds",
            "Unix timestamp of the most recent successful scrape.",
            registry=self.registry,
        )
        self.scrape_errors_total = Counter(
            f"{NAMESPACE}_exporter_scrape_errors_total",
            "Number of API errors encountered during scrape, by endpoint.",
            labelnames=("endpoint",),
            registry=self.registry,
        )
        self.api_requests_total = Counter(
            f"{NAMESPACE}_exporter_api_requests_total",
            "Number of HTTP requests issued to Bright Data, by endpoint and response code.",
            labelnames=("endpoint", "code"),
            registry=self.registry,
        )
        self.up = Gauge(
            f"{NAMESPACE}_up",
            "1 when the most recent scrape succeeded, 0 otherwise.",
            registry=self.registry,
        )
        self.build_info = Info(
            f"{NAMESPACE}_exporter_build",
            "Build metadata for the exporter (version, python).",
            registry=self.registry,
        )

        # ---- Account status (/status) ----
        self.account_can_make_requests = Gauge(
            f"{NAMESPACE}_account_can_make_requests",
            "1 if Bright Data's /status endpoint reports the proxy network "
            "can be reached with the auth supplied to the call, 0 otherwise. "
            "WARNING: this does NOT measure API-token validity — it tests "
            "*proxy-network credentials* (zone username + proxy password) "
            "and returns auth_fail_reason='wrong_password' / 'zone_not_found' "
            "when called without those. For exporters that only consume the "
            "REST API (no proxy traffic) this gauge is expected to read 0 "
            "even on a perfectly healthy account. Use `brightdata_up` for "
            "API-side health.",
            registry=self.registry,
        )
        self.account_info = Info(
            f"{NAMESPACE}_account",
            "Account identity + last-seen status. Labels carry the customer "
            "name, the account state, the egress IP Bright Data observed, "
            "and (when present) auth_fail_reason.",
            registry=self.registry,
        )

        # ---- Global Bright Data network status (/network_status/{type}) ----
        self.network_status = Gauge(
            f"{NAMESPACE}_network_status",
            "1 if the named Bright Data network type is reported "
            "operational by Bright Data, 0 otherwise. Not account-scoped — "
            "this is the upstream provider's own health signal.",
            labelnames=("network",),
            registry=self.registry,
        )

        # ---- Operational signals (silent-when-healthy) ----
        self.zone_ips_unavailable = Gauge(
            f"{NAMESPACE}_zone_ips_unavailable",
            "Number of IPs in the zone that are currently flagged as "
            "having connectivity problems. From /zone/ips/unavailable. "
            "Series only emitted for zones with unavailable IPs — absent "
            "when healthy.",
            labelnames=ZONE_LABELS,
            registry=self.registry,
        )
        self.zone_proxies_pending_replacement = Gauge(
            f"{NAMESPACE}_zone_proxies_pending_replacement",
            "Number of static IPs in the zone awaiting replacement. "
            "From /zone/proxies_pending_replacement. Only relevant for "
            "static (datacenter/ISP) zones; absent when nothing pending.",
            labelnames=ZONE_LABELS,
            registry=self.registry,
        )
        self.zone_recent_ips = Gauge(
            f"{NAMESPACE}_zone_recent_ips",
            "Number of distinct source IPs that recently used the zone. "
            "From /zone/recent_ips. Absent for zones with no recent "
            "activity (or accounts where the endpoint returns empty).",
            labelnames=ZONE_LABELS,
            registry=self.registry,
        )

        # ---- IP rosters (per zone, plan-dependent — best effort) ----
        self.zone_ips_per_country = Gauge(
            f"{NAMESPACE}_zone_ips_per_country",
            "IP count for the zone broken down by country. From "
            "/zone/ips?ip_per_country=true. Only emitted for plans that "
            "expose IP rosters (datacenter/ISP, dedicated residential); "
            "rotating-residential plans are silently skipped.",
            labelnames=(*ZONE_LABELS, "country"),
            registry=self.registry,
        )
        self.zone_dedicated_vips = Gauge(
            f"{NAMESPACE}_zone_dedicated_vips",
            "Number of dedicated residential VIP gIPs allocated to the "
            "zone. From /zone/route_vips. Emitted only for residential "
            "dedicated zones; rotating residential and datacenter zones "
            "are skipped (they reject this endpoint).",
            labelnames=ZONE_LABELS,
            registry=self.registry,
        )

        # ---- Domain consumption (high-cardinality — gated by config) ----
        self.zone_domain_traffic_bytes = Gauge(
            f"{NAMESPACE}_zone_domain_traffic_bytes",
            "Bandwidth consumed against the named domain via this zone. "
            "From /domains/bw. High cardinality (one series per "
            "zone*domain). Disabled by default — opt in via "
            "BRIGHTDATA_COLLECT_DOMAIN_CONSUMPTION=true. Consider "
            "Prometheus metricRelabelings to drop low-volume domains.",
            labelnames=(*ZONE_LABELS, "domain"),
            registry=self.registry,
        )
        self.zone_domain_requests = Gauge(
            f"{NAMESPACE}_zone_domain_requests",
            "Request count against the named domain via this zone. "
            "From /domains/req. Same cardinality caveats as "
            "zone_domain_traffic_bytes.",
            labelnames=(*ZONE_LABELS, "domain"),
            registry=self.registry,
        )

    # ------------------------------------------------------------------
    # Convenience accessors used by the collector
    # ------------------------------------------------------------------

    def reset_per_zone_metrics(self) -> None:
        """Clear per-zone series before each scrape so deleted zones drop out.

        Without this, a zone deleted in Bright Data lingers in Mimir at its
        last value forever (we never write a new sample so prom-client
        doesn't tombstone it).
        """
        self.zone_cost_usd.clear()
        self.zone_traffic_bytes.clear()
        self.zone_traffic_gb.clear()
        self.zone_requests.clear()
        self.zone_usage_limit_usd.clear()
        self.zone_rate_usd_per_gb.clear()
        self.zone_ip_bandwidth_gbs.clear()
        self.zone_dedicated_ip_count.clear()
        self.zone_vips_billed.clear()
        self.zone_serp_billable_requests.clear()
        self.zone_rate_usd_per_kreq.clear()
        self.zone_rate_usd_per_ip.clear()
        self.zone_info._metrics.clear()
        self.zone_pricing._metrics.clear()
        self.zone_ips_unavailable.clear()
        self.zone_proxies_pending_replacement.clear()
        self.zone_recent_ips.clear()
        self.zone_ips_per_country.clear()
        self.zone_dedicated_vips.clear()
        self.zone_domain_traffic_bytes.clear()
        self.zone_domain_requests.clear()
