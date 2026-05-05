"""Bright Data API client.

Thin wrapper over the Account Management API endpoints. Shapes were verified
empirically 2026-05-05 against api.brightdata.com and cross-checked with
docs.brightdata.com/api-reference/account-management-api/.

Why httpx instead of the official `brightdata-sdk` Python SDK
-------------------------------------------------------------

The official SDK (https://github.com/brightdata/sdk-python) targets the
Scraping / Search / Datasets APIs and exposes only zone provisioning
helpers (`list_zones`, `delete_zone`, `ensure_required_zones`). It covers
**1 of 9** endpoints this exporter relies on — and the one it does cover
(`/zone/get_active_zones`) is strictly less informative than the
`/zone/get_all_zones` we use, which carries the `status` field
(active/disabled/deleted) needed for the Prometheus labels.

None of the Account Management endpoints — `/customer/balance`,
`/zone?zone=NAME`, `/zone/status`, `/zone/cost`, `/zone/bw`,
`/zone/whitelist`, `/zone/permissions` — are exposed by the SDK. Pulling
the SDK in just to wrap one inferior call would add ~1.5 MB of aiohttp +
~300 dataset scrapers we never use, all to lose information. Direct httpx
calls are the right choice for this exporter.

Methods returning typed dataclasses are deliberately kept narrow — only
the fields the Prometheus collector consumes are normalized. Raw payloads
are preserved (`raw` attribute) so downstream code can extend without
re-querying.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Typed shapes (subset of API payload, normalized)
# ---------------------------------------------------------------------------


@dataclass
class Balance:
    balance: float
    credit: float
    prepayment: float
    pending_costs: float
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def spent_this_month(self) -> float:
        """Convention: prepayment minus current balance.

        Matches the value shown in the Bright Data dashboard "Spent this
        month" tile. Equivalent to "how much of the prepaid pool has been
        consumed since the last top-up cycle".
        """
        return max(self.prepayment - self.balance, 0.0)


@dataclass
class ZoneListEntry:
    name: str
    type: str
    status: str  # "active" | "disabled" | "deleted"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ZoneInfo:
    """Configuration of a zone, from /zone?zone=NAME (alias /zone/info)."""

    name: str
    created: str
    description: str
    ips: list[str]
    perm: str

    plan_product: str
    plan_type: str
    plan_country: str
    plan_bandwidth: str
    plan_vips_type: str
    plan_ips_type: str
    plan_ips_count: int | None
    plan_dualip: bool
    plan_vip: bool

    usage_limit_value: float | None
    usage_limit_unit: str
    usage_limit_cycle: str
    usage_limit_action: str

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def display_type(self) -> str:
        """Human-readable Type label matching the Bright Data UI column.

        Maps the API's `plan.product` value to the human label shown in the
        Bright Data control panel. If the product is unknown, returns the
        raw API value so nothing is silently lost.
        """
        return _DISPLAY_TYPE.get(self.plan_product, self.plan_product or "Unknown")

    @property
    def pool_tier(self) -> str:
        """Pool/IPs allocation: "dedicated" or "shared".

        Reflects whether IPs in the zone are reserved exclusively
        ("dedicated") or pooled with other Bright Data customers
        ("shared"). Derived from `plan.vips_type` / `plan.ips_type` /
        `plan.vip` — the only API signals that distinguish the two:

          - `vips_type = "domain"` → dedicated VIPs locked to a domain
          - `ips_type = "dedicated"` → dedicated datacenter/ISP IPs
          - `vip = 1` (residential dedicated VIP plans) → dedicated
          - everything else → shared

        Returns "" only if no allocation field is set (rare).
        """
        if self.plan_vips_type == "domain":
            return "dedicated"
        if self.plan_ips_type == "dedicated":
            return "dedicated"
        if self.plan_vip:
            return "dedicated"
        if self.plan_vips_type or self.plan_ips_type:
            return "shared"
        return ""


_DISPLAY_TYPE: dict[str, str] = {
    "res_rotating": "Residential",
    "res_static": "Residential static",
    "dc": "Data Center",
    "isp": "ISP",
    "mobile": "Mobile",
    "serp": "SERP API",
    "unblocker": "Web Unlocker",
}


@dataclass
class ZoneCost:
    """Cost + traffic summary for a zone over a given period.

    Bright Data /zone/cost returns different keys depending on the zone's
    billing model. Empirically observed (2026-05-05) across an account
    with 14 active zones, 5 distinct schemas exist:

      - bw, cost, gbs                — pay-per-GB residential / datacenter
      - bw, cost, gbs, vips          — pay-per-GB with dedicated VIP IPs
      - bw, cost, gbs_ipbw, ips      — subscription / dedicated-IP plan
                                       (cost is the flat fee; ips = IPs allocated)
      - bw, cost, reqs_serp          — SERP zones billed per 1k requests
      - cost only                    — disabled / inactive zone

    All numeric fields are normalized to 0 when absent so callers can blindly
    expose them as gauges; the collector decides which to publish based on
    which fields are non-zero.
    """

    name: str
    cost_usd: float
    bw_bytes: int
    gbs: float
    gbs_ipbw: float  # subscription / dedicated-IP — IP-bandwidth GB-seconds
    dedicated_ips: int  # subscription — count of allocated IPs
    vips: int  # residential — count of VIP IPs the cost applies to
    serp_billable_requests: int  # SERP — billable request count
    period_from: str
    period_to: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ZoneBandwidth:
    """Bandwidth + request breakdown for a zone over a given period.

    Field availability differs by zone type:
      - residential: bw_sum_res, https_svc_req
      - datacenter:  bw_sum_dc, https_direct_req, http_direct_req
    Missing fields are kept as 0 (collector decides whether to expose).
    """

    name: str
    bw_sum: int
    bw_dn: int
    bw_up: int
    bw_sum_dc: int
    bw_sum_res: int
    bw_api: int
    https_direct_req: int
    http_direct_req: int
    https_svc_req: int
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def requests_total(self) -> int:
        return self.https_direct_req + self.http_direct_req + self.https_svc_req


@dataclass
class AccountStatus:
    """GET /status — account-level liveness + identity.

    `can_make_requests` flips false when the account is suspended, the
    token loses scope, or auth fails for any other reason. Useful as a
    standalone alert: `brightdata_account_can_make_requests == 0`.
    """

    status: str  # "active" expected
    customer: str  # customer_id
    can_make_requests: bool
    auth_fail_reason: str  # populated when can_make_requests is False
    ip: str  # the egress IP Bright Data observed
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class NetworkStatus:
    """GET /network_status/{type} — global Bright Data network health.

    Not account-specific — reports whether Bright Data themselves report
    the network type as operational. Useful to distinguish "my account
    is broken" from "Bright Data is in a degraded state".
    """

    network: str  # "all" | "res" | "dc" | "mobile"
    operational: bool


@dataclass
class ZoneIPsPerCountry:
    """GET /zone/ips?zone=&ip_per_country=true — IP count by country.

    Only valid for plans that expose IP rosters (datacenter/ISP, dedicated
    residential). Pay-per-GB residential rotating zones return HTTP 400
    "Wrong zone plan" — the collector swallows that gracefully.
    """

    name: str
    counts: dict[str, int] = field(default_factory=dict)  # country code → IP count


@dataclass
class ZoneVIPRoutes:
    """GET /zone/route_vips?zone= — VIP IPs (dedicated residential gIPs).

    Returns 422 for ordinary residential rotating, 403 for datacenter,
    200 with an array of VIP IDs for residential dedicated zones.
    """

    name: str
    vip_ids: list[str] = field(default_factory=list)


@dataclass
class DomainConsumption:
    """GET /domains/{bw|req} — per-domain breakdown of bandwidth/requests.

    High cardinality: one entry per (zone, domain) combo. Returns `{}`
    on accounts without domain breakdown enabled. The collector exposes
    these as gauges; if your account has many domains, consider gating
    via `BRIGHTDATA_COLLECT_DOMAIN_CONSUMPTION` (env var) or trimming
    via `metricRelabelings` in your Prometheus config.
    """

    metric: str  # "bw" | "req"
    by_zone: dict[str, dict[str, int]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class BrightDataAPIError(Exception):
    """Raised when the Bright Data API returns an unexpected status / body."""

    def __init__(self, status: int, body: str, endpoint: str):
        self.status = status
        self.body = body
        self.endpoint = endpoint
        super().__init__(f"{endpoint} -> HTTP {status}: {body[:200]}")


class BrightDataClient:
    """Synchronous Bright Data API client.

    Synchronous on purpose: Bright Data's documented rate limit is 1 req/s/token
    so concurrency wins are minimal, and a sync client keeps the collector
    loop trivial to reason about (and to test).
    """

    DEFAULT_BASE_URL = "https://api.brightdata.com"
    DEFAULT_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
        limiter: object | None = None,
    ) -> None:
        if not token:
            raise ValueError("token is required")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # Optional rate limiter (duck-typed: anything with `acquire()`).
        # When set, every outbound HTTP call goes through it, so all
        # callers — periodic collector AND on-demand service handlers —
        # collectively respect Bright Data's 1 req/s/token limit.
        self._limiter = limiter
        self._client = client or httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "brightdata-exporter/0.1",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BrightDataClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -----------------------------------------------------------------
    # Low-level GET
    # -----------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        if self._limiter is not None:
            # Acquire BEFORE the HTTP call so the bucket fills regardless
            # of whether the call succeeds or fails.
            self._limiter.acquire()  # type: ignore[attr-defined]
        try:
            response = self._client.get(path, params=clean)
        except httpx.HTTPError as exc:
            logger.error("brightdata.http_error", endpoint=path, error=str(exc))
            raise BrightDataAPIError(0, str(exc), path) from exc

        if response.status_code != 200:
            raise BrightDataAPIError(response.status_code, response.text, path)

        ctype = response.headers.get("Content-Type", "")
        if "json" in ctype:
            return response.json()
        text = response.text.strip()
        try:
            return response.json()
        except ValueError:
            return text

    # -----------------------------------------------------------------
    # High-level endpoints
    # -----------------------------------------------------------------

    def balance(self) -> Balance:
        """GET /customer/balance — account balance + pending costs."""
        raw = self._get("/customer/balance")
        if not isinstance(raw, dict):
            raise BrightDataAPIError(200, str(raw)[:200], "/customer/balance")
        return Balance(
            balance=float(raw.get("balance") or 0.0),
            credit=float(raw.get("credit") or 0.0),
            prepayment=float(raw.get("prepayment") or 0.0),
            pending_costs=float(raw.get("pending_costs") or 0.0),
            raw=raw,
        )

    def all_zones(self) -> list[ZoneListEntry]:
        """GET /zone/get_all_zones — every zone (including deleted/disabled).

        Preferred over /zone/get_active_zones because it carries the `status`
        field which the exporter exposes both as a label and as a metric.
        """
        raw = self._get("/zone/get_all_zones")
        if not isinstance(raw, list):
            raise BrightDataAPIError(200, str(raw)[:200], "/zone/get_all_zones")
        out: list[ZoneListEntry] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            out.append(
                ZoneListEntry(
                    name=name,
                    type=str(item.get("type") or ""),
                    status=str(item.get("status") or ""),
                    raw=item,
                )
            )
        return out

    def active_zones(self) -> list[ZoneListEntry]:
        """GET /zone/get_active_zones — kept as a separate call for parity."""
        raw = self._get("/zone/get_active_zones")
        if not isinstance(raw, list):
            raise BrightDataAPIError(200, str(raw)[:200], "/zone/get_active_zones")
        out: list[ZoneListEntry] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            out.append(
                ZoneListEntry(
                    name=name,
                    type=str(item.get("type") or ""),
                    status="active",  # implicit; endpoint name asserts it
                    raw=item,
                )
            )
        return out

    def zone_info(self, zone: str) -> ZoneInfo:
        """GET /zone?zone=NAME — full configuration of a zone.

        Plan schema differs by zone type. We surface a normalized superset;
        fields absent in the raw payload return as empty strings / 0 / None.
        """
        raw = self._get("/zone", params={"zone": zone})
        if not isinstance(raw, dict):
            raise BrightDataAPIError(200, str(raw)[:200], "/zone")
        # Intermediate-variable + explicit type pattern is for mypy strict —
        # without it, mypy can't narrow `raw.get("x") if isinstance(...) else {}`
        # back to dict[str, Any] for downstream `.get()` calls.
        _plan = raw.get("plan")
        plan: dict[str, Any] = _plan if isinstance(_plan, dict) else {}
        _usage_limit = raw.get("usage_limit")
        usage_limit: dict[str, Any] = _usage_limit if isinstance(_usage_limit, dict) else {}
        _ips = raw.get("ips")
        ips: list[Any] = _ips if isinstance(_ips, list) else []
        return ZoneInfo(
            name=zone,
            created=str(raw.get("created") or ""),
            description=str(raw.get("description") or ""),
            ips=[str(ip) for ip in ips],
            perm=str(raw.get("perm") or ""),
            plan_product=str(plan.get("product") or ""),
            plan_type=str(plan.get("type") or ""),
            plan_country=str(plan.get("default_country") or plan.get("country") or ""),
            plan_bandwidth=str(plan.get("bandwidth") or ""),
            plan_vips_type=str(plan.get("vips_type") or ""),
            plan_ips_type=str(plan.get("ips_type") or ""),
            plan_ips_count=(int(plan["ips"]) if isinstance(plan.get("ips"), int) else None),
            plan_dualip=bool(plan.get("dualip")),
            plan_vip=bool(plan.get("vip")),
            usage_limit_value=(
                float(usage_limit["value"])
                if isinstance(usage_limit.get("value"), int | float)
                else None
            ),
            usage_limit_unit=str(usage_limit.get("unit") or ""),
            usage_limit_cycle=str(usage_limit.get("cycle") or ""),
            usage_limit_action=str(usage_limit.get("bust_action") or ""),
            raw=raw,
        )

    def zone_cost(self, zone: str, period_from: str, period_to: str) -> ZoneCost:
        """GET /zone/cost?zone=&from=&to= — total cost+traffic in the period.

        Response shape:
            {<user>: {custom: {cost, bw, gbs, range:{from,to}}}}
        """
        raw = self._get(
            "/zone/cost",
            params={"zone": zone, "from": period_from, "to": period_to},
        )
        cost = 0.0
        bw = 0
        gbs = 0.0
        gbs_ipbw = 0.0
        dedicated_ips = 0
        vips = 0
        serp_billable = 0
        if isinstance(raw, dict):
            for user_payload in raw.values():
                if not isinstance(user_payload, dict):
                    continue
                custom = user_payload.get("custom")
                if not isinstance(custom, dict):
                    continue
                if isinstance(custom.get("cost"), int | float):
                    cost += float(custom["cost"])
                if isinstance(custom.get("bw"), int | float):
                    bw += int(custom["bw"])
                if isinstance(custom.get("gbs"), int | float):
                    gbs += float(custom["gbs"])
                if isinstance(custom.get("gbs_ipbw"), int | float):
                    gbs_ipbw += float(custom["gbs_ipbw"])
                if isinstance(custom.get("ips"), int):
                    dedicated_ips += custom["ips"]
                if isinstance(custom.get("vips"), int):
                    vips += custom["vips"]
                if isinstance(custom.get("reqs_serp"), int):
                    serp_billable += custom["reqs_serp"]
        return ZoneCost(
            name=zone,
            cost_usd=cost,
            bw_bytes=bw,
            gbs=gbs,
            gbs_ipbw=gbs_ipbw,
            dedicated_ips=dedicated_ips,
            vips=vips,
            serp_billable_requests=serp_billable,
            period_from=period_from,
            period_to=period_to,
            raw=raw if isinstance(raw, dict) else {},
        )

    def zone_bandwidth(self, zone: str, period_from: str, period_to: str) -> ZoneBandwidth:
        """GET /zone/bw?zone=&from=&to= — per-zone bandwidth + request totals.

        Kept for completeness / fallback. The collector prefers
        :meth:`customer_bandwidth` (one call returns every zone) for normal
        scrape cycles.
        """
        raw = self._get(
            "/zone/bw",
            params={"zone": zone, "from": period_from, "to": period_to},
        )
        return self._parse_zone_bandwidth(zone, raw)

    def customer_bandwidth(self, period_from: str, period_to: str) -> dict[str, ZoneBandwidth]:
        """GET /customer/bw?from=&to= — bandwidth + requests for ALL zones.

        Single-call replacement for fanning ``/zone/bw`` per zone — at 1
        req/s upstream rate limit, this collapses N*1s into ~1s regardless
        of how many zones the account has.

        Response shape mirrors ``/zone/bw`` but with multiple zones in
        ``sums``:
            {<cust>: {sums: {<zone1>: {custom: {...}},
                             <zone2>: {custom: {...}}, ...}}}

        Returns a dict keyed by zone name. Zones the account has but with
        no traffic in the period appear with all-zero counters.
        """
        raw = self._get(
            "/customer/bw",
            params={"from": period_from, "to": period_to},
        )
        out: dict[str, ZoneBandwidth] = {}
        if not isinstance(raw, dict):
            return out
        for cust in raw.values():
            if not isinstance(cust, dict):
                continue
            sums = cust.get("sums")
            if not isinstance(sums, dict):
                continue
            for zone_name, zsum in sums.items():
                if not isinstance(zsum, dict):
                    continue
                _custom = zsum.get("custom")
                custom: dict[str, Any] = _custom if isinstance(_custom, dict) else {}
                bw = ZoneBandwidth(
                    name=zone_name,
                    bw_sum=int(custom.get("bw_sum") or 0),
                    bw_dn=int(custom.get("bw_dn") or 0),
                    bw_up=int(custom.get("bw_up") or 0),
                    bw_sum_dc=int(custom.get("bw_sum_dc") or 0),
                    bw_sum_res=int(custom.get("bw_sum_res") or 0),
                    bw_api=int(custom.get("bw_api") or 0),
                    https_direct_req=int(custom.get("https_direct_req") or 0),
                    http_direct_req=int(custom.get("http_direct_req") or 0),
                    https_svc_req=int(custom.get("https_svc_req") or 0),
                    raw=custom,
                )
                # Multi-customer payloads can list the same zone twice;
                # accumulate when that happens.
                existing = out.get(zone_name)
                if existing is None:
                    out[zone_name] = bw
                else:
                    out[zone_name] = ZoneBandwidth(
                        name=zone_name,
                        bw_sum=existing.bw_sum + bw.bw_sum,
                        bw_dn=existing.bw_dn + bw.bw_dn,
                        bw_up=existing.bw_up + bw.bw_up,
                        bw_sum_dc=existing.bw_sum_dc + bw.bw_sum_dc,
                        bw_sum_res=existing.bw_sum_res + bw.bw_sum_res,
                        bw_api=existing.bw_api + bw.bw_api,
                        https_direct_req=existing.https_direct_req + bw.https_direct_req,
                        http_direct_req=existing.http_direct_req + bw.http_direct_req,
                        https_svc_req=existing.https_svc_req + bw.https_svc_req,
                    )
        return out

    @staticmethod
    def _parse_zone_bandwidth(zone: str, raw: Any) -> ZoneBandwidth:
        def _sum(field_name: str) -> int:
            total = 0
            if not isinstance(raw, dict):
                return total
            for cust in raw.values():
                if not isinstance(cust, dict):
                    continue
                _sums = cust.get("sums")
                sums: dict[str, Any] = _sums if isinstance(_sums, dict) else {}
                _zsum = sums.get(zone)
                zsum: dict[str, Any] = _zsum if isinstance(_zsum, dict) else {}
                _custom = zsum.get("custom")
                custom: dict[str, Any] = _custom if isinstance(_custom, dict) else {}
                value = custom.get(field_name)
                if isinstance(value, int | float):
                    total += int(value)
            return total

        return ZoneBandwidth(
            name=zone,
            bw_sum=_sum("bw_sum"),
            bw_dn=_sum("bw_dn"),
            bw_up=_sum("bw_up"),
            bw_sum_dc=_sum("bw_sum_dc"),
            bw_sum_res=_sum("bw_sum_res"),
            bw_api=_sum("bw_api"),
            https_direct_req=_sum("https_direct_req"),
            http_direct_req=_sum("http_direct_req"),
            https_svc_req=_sum("https_svc_req"),
            raw=raw if isinstance(raw, dict) else {},
        )

    # -----------------------------------------------------------------
    # Account-level signals
    # -----------------------------------------------------------------

    def status(self) -> AccountStatus:
        """GET /status — account liveness + identity."""
        raw = self._get("/status")
        if not isinstance(raw, dict):
            raise BrightDataAPIError(200, str(raw)[:200], "/status")
        return AccountStatus(
            status=str(raw.get("status") or ""),
            customer=str(raw.get("customer") or ""),
            can_make_requests=bool(raw.get("can_make_requests")),
            auth_fail_reason=str(raw.get("auth_fail_reason") or ""),
            ip=str(raw.get("ip") or ""),
            raw=raw,
        )

    def network_status(self, network: str = "all") -> NetworkStatus:
        """GET /network_status/{type} — global Bright Data network health.

        ``network`` is one of "all", "res", "dc", "mobile". The collector
        defaults to "all" to keep API budget low; callers can iterate the
        four types if per-network granularity is needed.
        """
        if network not in {"all", "res", "dc", "mobile"}:
            raise ValueError(f"network must be all|res|dc|mobile (got {network!r})")
        raw = self._get(f"/network_status/{network}")
        operational = bool(raw.get("status")) if isinstance(raw, dict) else False
        return NetworkStatus(network=network, operational=operational)

    # -----------------------------------------------------------------
    # Operational signals (silent-when-healthy)
    # -----------------------------------------------------------------

    def zone_ips_unavailable(self) -> dict[str, list[str]]:
        """GET /zone/ips/unavailable — IPs with connectivity problems.

        Returns ``{<zone>: [<ip>, ...]}``. Empty dict on healthy account.
        Only meaningful for static (datacenter/ISP) zones.
        """
        raw = self._get("/zone/ips/unavailable")
        return {z: list(ips) for z, ips in (raw or {}).items() if isinstance(ips, list)}

    def zone_proxies_pending_replacement(self) -> dict[str, Any]:
        """GET /zone/proxies_pending_replacement — static IPs queued for refresh.

        Returns a dict keyed by zone. Empty when nothing is pending.
        Only meaningful for static (datacenter/ISP) zones.
        """
        raw = self._get("/zone/proxies_pending_replacement")
        return raw if isinstance(raw, dict) else {}

    def zone_recent_ips(self, zones: list[str] | None = None) -> dict[str, list[str]]:
        """GET /zone/recent_ips — IPs that recently used the zone(s).

        Returns ``{<zone>: [<ip>, ...]}`` for each zone with activity.
        Empty when nothing recent. Pass ``zones`` (list of names) to
        restrict the scope; omit for an account-wide view.
        """
        params: dict[str, Any] = {}
        if zones:
            params["zones"] = ",".join(zones)
        raw = self._get("/zone/recent_ips", params=params)
        if not isinstance(raw, dict):
            return {}
        return {z: list(ips) for z, ips in raw.items() if isinstance(ips, list)}

    # -----------------------------------------------------------------
    # IP rosters per zone (best effort — many zone types reject these)
    # -----------------------------------------------------------------

    def zone_ips_per_country(self, zone: str) -> ZoneIPsPerCountry:
        """GET /zone/ips?zone=&ip_per_country=true — IP count by country.

        Returns counts keyed by ISO country code. Pay-per-GB residential
        rotating zones return HTTP 400 ("Wrong zone plan") — the caller is
        expected to catch :class:`BrightDataAPIError` for that case.
        """
        raw = self._get(
            "/zone/ips",
            params={"zone": zone, "ip_per_country": "true"},
        )
        counts: dict[str, int] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, int):
                    counts[str(k)] = v
        return ZoneIPsPerCountry(name=zone, counts=counts)

    def zone_dedicated_vips(self, zone: str) -> ZoneVIPRoutes:
        """GET /zone/route_vips?zone= — dedicated residential VIP IPs.

        Returns 200 with an array of VIP IDs for residential dedicated
        zones; 422 for residential rotating, 403 for datacenter — caller
        catches :class:`BrightDataAPIError` for the latter two.
        """
        raw = self._get("/zone/route_vips", params={"zone": zone})
        ids: list[str] = []
        if isinstance(raw, list):
            ids = [str(item) for item in raw]
        return ZoneVIPRoutes(name=zone, vip_ids=ids)

    # -----------------------------------------------------------------
    # Domain consumption (high cardinality — collector gates this)
    # -----------------------------------------------------------------

    def domain_consumption(
        self,
        metric: str,
        period_from: str,
        period_to: str,
        zones: list[str] | None = None,
    ) -> DomainConsumption:
        """GET /domains/{bw|req}?from=&to=&zones= — per-domain breakdown.

        ``metric`` is "bw" (bytes) or "req" (request count). Pass ``zones``
        to restrict the breakdown — omitting it returns nothing on most
        accounts (the API treats "no zones filter" as "exclude all").

        Response is an opaque object whose keys depend on the account's
        domain-tracking configuration. The dataclass keeps the raw payload
        so callers can extract whatever shape Bright Data returns today.
        """
        if metric not in {"bw", "req"}:
            raise ValueError(f"metric must be bw|req (got {metric!r})")
        params: dict[str, Any] = {"from": period_from, "to": period_to}
        if zones:
            params["zones"] = ",".join(zones)
        raw = self._get(f"/domains/{metric}", params=params)
        by_zone: dict[str, dict[str, int]] = {}
        if isinstance(raw, dict):
            for zone_name, breakdown in raw.items():
                if not isinstance(breakdown, dict):
                    continue
                by_zone[str(zone_name)] = {
                    str(domain): int(value)
                    for domain, value in breakdown.items()
                    if isinstance(value, int | float)
                }
        return DomainConsumption(
            metric=metric,
            by_zone=by_zone,
            raw=raw if isinstance(raw, dict) else {},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def period_window(days: int, today: date | None = None) -> tuple[str, str]:
    """Return (from, to) date strings for a rolling window ending today.

    Bright Data accepts ``YYYY-MM-DD`` for /zone/cost and /zone/bw.
    """
    end = today or datetime.now(UTC).date()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()
