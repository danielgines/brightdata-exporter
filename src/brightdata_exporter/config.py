"""Configuration — env-var driven via pydantic-settings.

Env vars are prefixed ``BRIGHTDATA_`` so ``BRIGHTDATA_API_TOKEN`` /
``BRIGHTDATA_SCRAPE_INTERVAL`` etc. are picked up automatically.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BRIGHTDATA_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Auth ---
    api_token: str = Field(
        ...,
        description="Bright Data API token (Bearer). Required. See "
        "https://docs.brightdata.com/api-reference/authentication.",
    )
    api_base: str = Field(
        default="https://api.brightdata.com",
        description="Override the API base URL — useful for testing or "
        "pointing at a corporate proxy that mirrors Bright Data.",
    )

    # --- Collection ---
    scrape_interval: int = Field(
        default=300,
        ge=30,
        description="Seconds between full scrape cycles. Each cycle issues "
        "1 + N*3 requests (account + per-zone). Bright Data rate limit is "
        "1 req/s/token, so a 14-zone account needs ~45s minimum.",
    )
    period_days: int = Field(
        default=30,
        ge=1,
        le=366,
        description="Rolling window size (days) for /zone/cost and /zone/bw. "
        "Period is [now-Nd, now].",
    )
    info_cache_seconds: int = Field(
        default=3600,
        ge=0,
        description="How long to reuse /zone?zone=NAME results before "
        "re-fetching. Plan / usage_limit change rarely; 1h is safe.",
    )
    api_rate_limit_rps: float = Field(
        default=1.0,
        gt=0.0,
        description="Max requests per second to Bright Data. Default 1.0 "
        "matches the documented limit. Tune down if you hit 429s. Shared "
        "between the periodic collector and the on-demand /api/* service.",
    )
    cache_ttl_seconds: float = Field(
        default=300.0,
        ge=0.0,
        description="TTL for cached /api/* responses. Default 5min — "
        "balances 'fresh enough' for dashboards against 'cheap enough' "
        "to not bombard upstream during traffic spikes. Set to 0 to "
        "disable cache (each request fans out fresh upstream calls).",
    )
    cache_max_size: int = Field(
        default=1000,
        ge=1,
        description="Max number of cached /api/* response keys. LRU "
        "eviction kicks in past this. Defensive bound — typical "
        "dashboards use < 100 unique keys.",
    )
    api_enabled: bool = Field(
        default=True,
        description="Mount the on-demand REST endpoints (/api/account, "
        "/api/zones, /api/zones/{name}). Set false to run as a "
        "metrics-only Prometheus exporter.",
    )
    api_timeout_seconds: float = Field(
        default=30.0,
        gt=0.0,
        description="Per-request HTTP timeout.",
    )
    api_auth_token: str = Field(
        default="",
        description="Optional bearer token guarding the /api/* endpoints. "
        "When empty (default), /api/* is unauthenticated — only safe in "
        "trusted networks (cluster-internal Service, locked-down "
        "NetworkPolicy). When set, every /api/* request MUST include a "
        "matching `Authorization: Bearer <token>` header; mismatches "
        "return HTTP 401 with `WWW-Authenticate: Bearer`. /metrics, "
        "/healthz, /readyz, and / are NEVER authenticated regardless — "
        "Prometheus scraping conventions assume unauth /metrics on "
        "trusted networks. Use a long random value (`openssl rand "
        "-hex 32`); the comparison is constant-time.",
    )
    api_min_window_days: int = Field(
        default=1,
        ge=0,
        description="Minimum from/to window (days) accepted by /api/zones "
        "and /api/zones/{name}. Bright Data returns zeroed cost/traffic "
        "when the requested range falls inside the current day (no "
        "billing data has rolled up yet). Below this threshold the "
        "service expands `from` backward to `to - api_min_window_days` "
        "so dashboards never render an all-zero table after a quick "
        "time-picker selection. Set 0 to disable.",
    )

    # --- Zone filtering ---
    zones_filter: str = Field(
        default="",
        description="Optional regex applied to zone NAME — only matching "
        "zones are scraped for cost/bw/info. Empty = all non-deleted zones.",
    )
    include_disabled: bool = Field(
        default=False,
        description="If true, also scrape cost/bw for status=disabled zones.",
    )

    # --- Optional collectors ---
    collect_ip_rosters: bool = Field(
        default=True,
        description="Per-zone /zone/ips?ip_per_country=true and "
        "/zone/route_vips. Adds ~2 calls per active zone but exposes "
        "IP-by-country and dedicated-VIP counts. Plans that don't "
        "support these endpoints (rotating residential, etc) return "
        "400/403/422 and are silently skipped.",
    )
    collect_domain_consumption: bool = Field(
        default=False,
        description="/domains/bw and /domains/req — per-domain breakdown. "
        "High cardinality (one series per zone*domain). Off by default; "
        "enable when you actually want to monitor domain-level usage.",
    )
    collect_recent_ips: bool = Field(
        default=True,
        description="/zone/recent_ips — count of source IPs that recently "
        "used each zone. Single account-wide call; cheap.",
    )
    collect_ip_health: bool = Field(
        default=True,
        description="/zone/ips/unavailable + /zone/proxies_pending_replacement "
        "— silent-when-healthy alerting metrics. Two cheap account-wide calls.",
    )
    collect_network_status: bool = Field(
        default=True,
        description="/network_status/all — Bright Data's own report of the "
        "network's operational state. Single cheap call.",
    )

    # --- Server ---
    listen_host: str = Field(default="0.0.0.0", description="Bind address for /metrics.")
    listen_port: int = Field(default=9617, ge=1, le=65535)

    # --- Logging ---
    log_level: str = Field(default="info")
    log_format: str = Field(default="json", description="json or console")

    @field_validator("zones_filter")
    @classmethod
    def _compile_regex(cls, v: str) -> str:
        if v:
            try:
                re.compile(v)
            except re.error as exc:
                raise ValueError(f"zones_filter is not a valid regex: {exc}") from exc
        return v

    @field_validator("log_level")
    @classmethod
    def _normalize_level(cls, v: str) -> str:
        v = v.lower()
        if v not in {"debug", "info", "warning", "warn", "error"}:
            raise ValueError(f"log_level must be debug/info/warning/error (got {v!r})")
        return "warning" if v == "warn" else v

    def filter_pattern(self) -> re.Pattern[str] | None:
        return re.compile(self.zones_filter) if self.zones_filter else None


def load_settings(**overrides: Any) -> Settings:
    """Load settings, applying optional overrides on top of env."""
    return Settings(**overrides)
