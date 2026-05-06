# brightdata-exporter

[![PyPI](https://img.shields.io/pypi/v/brightdata-exporter)](https://pypi.org/project/brightdata-exporter/)
[![Container](https://img.shields.io/badge/ghcr.io-brightdata--exporter-blue)](https://github.com/danielgines/brightdata-exporter/pkgs/container/brightdata-exporter)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Observability + FinOps service for [Bright Data](https://brightdata.com).
Single binary, two interfaces:

  - **Prometheus exporter** (`/metrics`) — periodic snapshots of account
    balance and per-zone cost / traffic / request totals over a fixed
    rolling window. Drives alerts, time-series trends, and long-term
    retention via Mimir / Thanos / VictoriaMetrics.

  - **REST service** (`/api/zones?from=&to=`, `/api/account`, …) — on-demand
    JSON queries against the Bright Data account-management API, with the
    `from`/`to` range driven by the dashboard's time picker. Pairs with
    Grafana's [Infinity datasource](https://grafana.com/grafana/plugins/yesoreyeram-infinity-datasource/)
    so users pick "last 7 days", "MTD", or any custom range and the
    numbers reflect THAT range — not whichever Prometheus sample is
    closest to the right edge.

## Why

The Bright Data UI shows the right cost / traffic / request data but it's
UI-only — no exposed API endpoint returns "total cost across all zones in
the last 7 days" in one call, and the per-zone endpoints require
authentication and date-range fan-out per zone. This service does the
fan-out for you, formats the result the way the UI does
(`$5/GB` vs `$1/CPM` vs `$300/month` per plan type), and ships it via two
familiar shapes (Prometheus metrics + REST JSON) so you can plug it into
your existing Grafana + Alertmanager stack with zero glue code.

The two interfaces serve complementary use cases:

| Use case | Interface |
|---|---|
| "Alert me if balance drops below $100" | Prometheus exporter — `brightdata_account_balance_usd < 100` rule in Mimir |
| "Show how spending evolved daily over the last quarter" | Prometheus exporter — timeseries panel against `brightdata_zone_cost_usd` |
| "How much did I spend on `linkedin_companies` between Mar 14 and Apr 22?" | REST service — `GET /api/zones?from=2026-03-14&to=2026-04-22` |
| "Bring up a Grafana table where the time picker drives the spend numbers" | REST service via Infinity datasource (see `examples/grafana-dashboard.json`) |

## How it works

`brightdata-exporter` is a standard Prometheus pull-model exporter. It
**does not store anything** — it just refreshes a snapshot of metrics in
memory and exposes them on `/metrics`. Persistence and history live in
your Prometheus / Mimir / Thanos / VictoriaMetrics — wherever you already
keep time-series data.

```
┌─────────────────────────┐
│    Bright Data API      │   external (api.brightdata.com)
└────────────▲────────────┘
             │
             │ (1) exporter pulls every BRIGHTDATA_SCRAPE_INTERVAL (300s default)
             │     ~1 + N×3 requests per cycle (account + per-zone endpoints)
             │     paced at BRIGHTDATA_API_RATE_LIMIT_RPS (1 req/s default)
             │
┌────────────┴────────────┐
│   brightdata-exporter   │   STATELESS — last snapshot in memory only
│         (this)          │   restart = momentarily empty, no historical data lost
│                         │   (history is in your TSDB)
│      GET /metrics       │
│      GET /healthz       │
│      GET /readyz        │
└────────────┬────────────┘
             │
             │ (2) Prometheus / Mimir / Thanos / Alloy / VictoriaMetrics
             │     scrapes /metrics every scrape_interval (60s typical)
             │
┌────────────▼────────────┐
│  Prometheus / Mimir     │   PERSISTS to disk (TSDB) → S3 / GCS for long-term
│  (your existing stack)  │   retention is yours to configure (15d local default,
│                         │   months-to-years on Mimir)
└────────────▲────────────┘
             │
             │ (3) Grafana queries via PromQL
             │
┌────────────┴────────────┐
│   Grafana dashboards    │
└─────────────────────────┘
```

Two timers, one consequence:

| Timer | Controlled by | Default | Effect |
|---|---|---|---|
| `BRIGHTDATA_SCRAPE_INTERVAL` | this exporter (env) | **300s** | How often the exporter calls Bright Data |
| `scrape_interval` | Prometheus side | 60s typical | How often Prometheus reads `/metrics` |

If Prometheus scrapes faster than the exporter refreshes, you get
identical samples until the next exporter cycle — TSDB compacts those
efficiently, no harm done. Don't set the exporter interval below 60s
unless you really need it: each cycle costs `1 + N×3` API calls, paced
at 1 req/s.

What this means in practice:

- **The exporter has no database, no disk writes, no queue, no buffer.**
  Just the current value of every gauge/counter in RAM.
- **Restarting the exporter is safe.** Counters reset (`rate()` handles
  this idiomatically), gauges repopulate on the next cycle. The history
  was always in Prometheus/Mimir, not here.
- **Multiple scrapers can coexist** — Prometheus + Mimir + Thanos sidecar
  + Grafana Alloy can all hit the same `/metrics`. The exporter doesn't
  care or know.
- **Without a scraper, no history.** `curl /metrics` gives you the
  current snapshot but nothing else. Pair it with whatever Prometheus-
  compatible TSDB you run.

## Why direct HTTP instead of the official SDK?

A reasonable question — Bright Data ships an official Python SDK
([`brightdata-sdk`](https://github.com/brightdata/sdk-python)). Why not
use it?

Because it doesn't cover what this exporter needs. The SDK targets the
**Scraping / Search / Datasets APIs** plus zone *provisioning*
(`list_zones`, `delete_zone`, `ensure_required_zones`). The Account
Management API — `/customer/balance`, `/zone/cost`, `/zone/bw`,
`/zone?zone=NAME`, `/zone/get_all_zones` (with `status`), `/zone/whitelist`,
`/zone/permissions` — is not exposed.

Concretely, of the 14 endpoints this exporter calls, the SDK covers **one**:
`list_zones()`, which wraps `/zone/get_active_zones`. We use
`/zone/get_all_zones` instead because it carries the `status` field
(`active`/`disabled`/`deleted`) needed for the Prometheus labels and the
`brightdata_zones_total{status}` count breakdown — strictly more
information than the SDK's wrapper provides.

Pulling in the SDK would mean shipping ~1.5 MB of `aiohttp` plus ~300
dataset scrapers we never use, all to *lose* information. So the exporter
calls the Account Management API directly via `httpx` (~150 KB, sync —
which fits the 1 req/s upstream rate limit naturally). When the SDK
eventually adds account-management coverage, this decision is worth
revisiting — see the [empirical schema notes](src/brightdata_exporter/client.py)
in the client docstring.

## Quick start

### Docker

```bash
docker run --rm -p 9617:9617 \
  -e BRIGHTDATA_API_TOKEN=$YOUR_TOKEN \
  ghcr.io/danielgines/brightdata-exporter:latest

curl http://localhost:9617/metrics
```

### Docker Compose (with Prometheus + Grafana)

A complete stack is in [`examples/docker-compose.yml`](examples/docker-compose.yml):

```bash
cd examples
BRIGHTDATA_API_TOKEN=your-token docker compose up
# Grafana on :3000 (admin/admin), prometheus on :9090
```

### pip

```bash
pip install brightdata-exporter
BRIGHTDATA_API_TOKEN=your-token brightdata-exporter
```

### Helm (Kubernetes)

```bash
helm install brightdata-exporter \
  ./helm/brightdata-exporter \
  --set auth.apiToken=your-token \
  --set serviceMonitor.enabled=true     # opt-in if you run prometheus-operator
```

For production, use `auth.existingSecret` and let an ExternalSecret operator
sync the token from your secret manager:

```bash
helm install brightdata-exporter ./helm/brightdata-exporter \
  --set auth.existingSecret=brightdata-credentials \
  --set serviceMonitor.enabled=true
```

## Configuration

All settings are env vars prefixed `BRIGHTDATA_`.

| Variable | Default | Description |
|---|---|---|
| `BRIGHTDATA_API_TOKEN` | _required_ | Bearer token from your Bright Data account |
| `BRIGHTDATA_API_BASE` | `https://api.brightdata.com` | API base URL (override for testing) |
| `BRIGHTDATA_SCRAPE_INTERVAL` | `300` | Seconds between full scrape cycles |
| `BRIGHTDATA_PERIOD_DAYS` | `30` | Rolling window for cost/bandwidth queries (days) |
| `BRIGHTDATA_INFO_CACHE_SECONDS` | `3600` | TTL for `/zone?zone=NAME` results |
| `BRIGHTDATA_API_RATE_LIMIT_RPS` | `1.0` | Max req/s to Bright Data (their docs say 1 req/s/token) |
| `BRIGHTDATA_API_TIMEOUT_SECONDS` | `30` | Per-request HTTP timeout |
| `BRIGHTDATA_ZONES_FILTER` | `""` | Optional regex on zone names; empty = all non-deleted |
| `BRIGHTDATA_INCLUDE_DISABLED` | `false` | If true, also scrape `status=disabled` zones |
| `BRIGHTDATA_COLLECT_IP_ROSTERS` | `true` | Per-zone `/zone/ips` + `/zone/route_vips` (IPs by country, dedicated VIPs). Calls are gated by plan capability — rotating-residential, SERP, mobile, and unblocker zones are skipped for `/zone/ips`; non-VIP zones are skipped for `/zone/route_vips` |
| `BRIGHTDATA_COLLECT_RECENT_IPS` | `true` | Account-wide `/zone/recent_ips` (cheap) |
| `BRIGHTDATA_COLLECT_IP_HEALTH` | `true` | `/zone/ips/unavailable` + `/zone/proxies_pending_replacement` |
| `BRIGHTDATA_COLLECT_NETWORK_STATUS` | `true` | `/network_status/all` (Bright Data's own health signal) |
| `BRIGHTDATA_COLLECT_DOMAIN_CONSUMPTION` | `false` | `/domains/bw` + `/domains/req` — high cardinality, opt-in |
| `BRIGHTDATA_API_ENABLED` | `true` | Mount the REST `/api/*` endpoints. Set false to run as Prometheus-only |
| `BRIGHTDATA_CACHE_TTL_SECONDS` | `300` | TTL for `/api/*` response cache. 0 disables cache (every request fans out) |
| `BRIGHTDATA_CACHE_MAX_SIZE` | `1000` | Max cached entries (LRU eviction beyond this) |
| `BRIGHTDATA_API_MIN_WINDOW_DAYS` | `1` | Minimum from/to window enforced on `/api/zones*`. Bright Data zeroes out billing data for sub-day ranges; this clamp expands `from` backward when needed. Set 0 to disable |
| `BRIGHTDATA_API_AUTH_TOKEN` | `""` | Optional bearer token guarding `/api/*`. When empty (default), endpoints are open. When set, every `/api/*` request must include `Authorization: Bearer <token>`; mismatch returns 401 with `WWW-Authenticate: Bearer`. `/metrics` + `/healthz` + `/readyz` are NEVER auth-gated. Constant-time compare. Generate with `openssl rand -hex 32` |
| `BRIGHTDATA_LISTEN_HOST` | `0.0.0.0` | HTTP bind address |
| `BRIGHTDATA_LISTEN_PORT` | `9617` | HTTP bind port |
| `BRIGHTDATA_LOG_LEVEL` | `info` | `debug` / `info` / `warning` / `error` |
| `BRIGHTDATA_LOG_FORMAT` | `json` | `json` (production) / `console` (dev) |

### Rate limiting note

Bright Data documents a limit of 1 request per second per API token. With
14 active zones and 3 endpoints per zone (`info`, `cost`, `bw`) plus 2
account calls, a full cycle issues ~44 requests. At 1 req/s that's ~44s of
wall time, which is fine for a 5-minute scrape interval. If your account
has hundreds of active zones, raise `BRIGHTDATA_SCRAPE_INTERVAL` or use
`BRIGHTDATA_ZONES_FILTER` to narrow scope.

## Endpoints

### Prometheus exporter
| Path | Purpose |
|---|---|
| `/metrics` | Prometheus exposition (collected every `BRIGHTDATA_SCRAPE_INTERVAL`) |
| `/healthz` | Liveness — 200 while the HTTP server is responding |
| `/readyz` | Readiness — 200 while the HTTP server is responding |

### REST service (FinOps / ad-hoc queries)
All `/api/*` endpoints return JSON, set CORS `*` so Grafana on a different
host can call them, and go through a TTL cache (default 5 min) so concurrent
viewers collapse onto a single upstream fan-out per `(path, params)`.

**Authentication:** `/api/*` is unauthenticated by default for back-compat,
but a bearer-token gate is available via `BRIGHTDATA_API_AUTH_TOKEN`. Set
it in production — these endpoints expose account balance, per-zone
spend, and IP rosters. Once set, every `/api/*` request must include
`Authorization: Bearer <token>` (constant-time compared); missing or
mismatched returns 401 with `WWW-Authenticate: Bearer`. The Grafana
Infinity datasource supports this via the "Custom HTTP Headers" auth
option (Bearer scheme). Note: `/metrics`, `/healthz`, `/readyz`, `/`
are never auth-gated regardless — Prometheus scraping convention is
unauth on a trusted network; isolate via NetworkPolicy if you can't
guarantee that.

| Method + path | Purpose | Cache key |
|---|---|---|
| `GET /api/account` | Balance + status + zone counts (snapshot, no date range) | `/api/account` |
| `GET /api/zones?from=&to=` | List zones with cost/traffic/requests for `[from, to]` (YYYY-MM-DD). Optional: `status=active,disabled,deleted`, `zone_filter=<regex>` | `(path, sorted params)` |
| `GET /api/zones/{name}?from=&to=` | Single-zone detail including IP roster (when supported) | `(path, sorted params)` |

```bash
# Sample — match the Bright Data UI "Zones" page
curl 'http://localhost:9617/api/zones?from=2026-04-05&to=2026-05-05' | jq '.[0]'
{
  "name": "linkedin_companies",
  "type": "Residential",
  "status": "active",
  "pool_tier": "dedicated",
  "rate_display": "$5.64/GB",
  "billing_model": "per_gb",
  "cost_usd": 709.43,
  "traffic_gb": 125.72,
  "traffic": {"total": 125719645731, "down": 114601142905, "up": 11118502826, ...},
  "requests": {"https_direct": 175, "http_direct": 38, "https_svc": 98303, "total": 98516},
  "usage_limit": {"value": 800, "unit": "$", "cycle": "m", "action": "disable"},
  ...
}
```

| Path | Purpose |
|---|---|
| `/` | Friendly index page (lists every endpoint above) |

Both `/healthz` and `/readyz` return 200 as long as the HTTP server is up.
This follows the convention of `blackbox_exporter` and other Prometheus
Foundation exporters: probes do **not** validate upstream connectivity.

Use the `brightdata_up` metric (1 if the most recent scrape succeeded, 0
otherwise) to alert on Bright Data API health, and
`time() - brightdata_exporter_last_scrape_timestamp_seconds > 2 * scrape_interval`
to detect a stuck exporter. Tying readiness to upstream availability would
remove the pod from the Prometheus scrape pool the moment Bright Data has
issues — exactly the moment you need that data the most.

## Metrics

### Account

| Metric | Type | Description |
|---|---|---|
| `brightdata_account_balance_usd` | gauge | Current balance |
| `brightdata_account_credit_usd` | gauge | Credit on the account |
| `brightdata_account_prepayment_usd` | gauge | Prepayment deposited |
| `brightdata_account_pending_costs_usd` | gauge | Costs accrued but not yet billed |
| `brightdata_account_spent_this_month_usd` | gauge | `prepayment - balance` (matches the UI tile) |
| `brightdata_account_can_make_requests` | gauge | 1 when the token is currently authorized to issue proxy requests; 0 when suspended/blocked |
| `brightdata_account_info` | info | Static label set: `customer`, `status`, `ip` (egress IP Bright Data observes), `auth_fail_reason` |
| `brightdata_zones_total{status}` | gauge | Zone counts by status (`active`/`disabled`/`deleted`) |
| `brightdata_network_status{network}` | gauge | 1 if Bright Data reports the named network (`all`/`res`/`dc`/`mobile`) operational |

### Per-zone (labels: `zone`, `type`, `status`)

| Metric | Type | Extra label | Description |
|---|---|---|---|
| `brightdata_zone_cost_usd` | gauge | — | Total cost (USD) over the configured period |
| `brightdata_zone_traffic_bytes` | gauge | `direction` | Traffic split: `total`, `dn`, `up`, `dc`, `res`, `api` |
| `brightdata_zone_traffic_gb` | gauge | — | GB-seconds (Bright Data's `gbs` field) |
| `brightdata_zone_requests` | gauge | `proto` | Requests by protocol: `https_direct`, `http_direct`, `https_svc`, `total` |
| `brightdata_zone_usage_limit_usd` | gauge | `cycle` | Spend cap; `cycle` is `m`/`d`/`h`. Absent when no limit |
| `brightdata_zone_rate_usd_per_gb` | gauge | — | **Derived**: `cost / gbs` over the period |
| `brightdata_zone_info` | info | many | Static config: `product`, `plan_type`, `country`, `bandwidth`, `security`, `perm`, `description`, `created` |
| `brightdata_zone_ips_per_country` | gauge | `country` | IP count by country for plans that expose IP rosters (DC/ISP, dedicated residential) |
| `brightdata_zone_dedicated_vips` | gauge | — | Number of dedicated residential VIP gIPs allocated to the zone |
| `brightdata_zone_ips_unavailable` | gauge | — | IPs flagged with connectivity problems (silent when healthy) |
| `brightdata_zone_proxies_pending_replacement` | gauge | — | Static IPs awaiting refresh (silent when none pending) |
| `brightdata_zone_recent_ips` | gauge | — | Distinct source IPs that recently used the zone |
| `brightdata_zone_domain_traffic_bytes` | gauge | `domain` | Bandwidth per domain (opt-in via `BRIGHTDATA_COLLECT_DOMAIN_CONSUMPTION`) |
| `brightdata_zone_domain_requests` | gauge | `domain` | Request count per domain (same opt-in) |

### Exporter introspection

| Metric | Type | Description |
|---|---|---|
| `brightdata_up` | gauge | 1 if the most recent scrape succeeded |
| `brightdata_exporter_scrape_duration_seconds` | summary | Scrape cycle latency |
| `brightdata_exporter_last_scrape_timestamp_seconds` | gauge | Unix ts of last successful scrape |
| `brightdata_exporter_scrape_errors_total{endpoint}` | counter | API errors by endpoint |
| `brightdata_exporter_api_requests_total{endpoint,code}` | counter | Bright Data calls by endpoint and HTTP code |
| `brightdata_exporter_build_info{version,python_version}` | info | Build metadata |

## Grafana dashboards

The exporter ships **two complementary dashboards** that reflect the
hybrid Prometheus + REST architecture. Each one is the right tool for a
different question — running both side-by-side is encouraged.

### `examples/grafana-dashboard.json` — FinOps investigation

100% **Infinity datasource** (`/api/*`). Picker-driven. Use this when
you want to answer "how much did I spend between dates X and Y, and on
which zones?".

- **Account row** — PrePayment / Credit / Balance / Spent this month / Active Zones (snapshot tiles, time picker is intentionally ignored — these are "now" values from `/api/account`)
- **Zones table** — every active zone with Cost / Traffic / Requests / Spent for the picked range. Calls `/api/zones?from=&to=` so the time picker drives the upstream window. Footer sums; sortable; status color mapping
- **Cost breakdown** — bar charts (top 10 by spend, top 10 by traffic) and donuts (spend by Pool Type, spend by zone Type) all over the picked range

No Prometheus required. Time picker on "Last 5 minutes" automatically
clamps to a 1-day minimum window (`BRIGHTDATA_API_MIN_WINDOW_DAYS`)
because Bright Data's billing data rolls up daily.

### `examples/grafana-dashboard-operations.json` — continuous monitoring

100% **Prometheus**. Always-on, refresh-driven. Use this for alerts,
runway estimation, and detecting silent regressions.

- **Account drawdown** — balance / pending / spent this month over time, plus a derived `$/day burn rate` (negative deriv of balance)
- **Cost evolution** — top 10 zones by cost as line series, $/GB drift to spot Bright Data quietly raising prices
- **Traffic evolution** — top 10 zones by bytes, account-wide requests by protocol
- **Health & alerts** — exporter `up`, time since last scrape, scrape duration, scrape errors, IPs unavailable, proxies pending replacement, Bright Data network status, zone counts by status

Both dashboards are fully dynamic (no hardcoded zone names) and link to
each other via the dashboard header.

## Sample alert rules

```yaml
groups:
  - name: brightdata
    rules:
      - alert: BrightDataAccountLowBalance
        expr: brightdata_account_balance_usd < 100
        for: 10m
        annotations:
          summary: "Bright Data account balance below $100"

      - alert: BrightDataZoneNearUsageLimit
        expr: brightdata_zone_cost_usd / brightdata_zone_usage_limit_usd > 0.8
        for: 15m
        annotations:
          summary: "{{ $labels.zone }} above 80% of its monthly spend limit"

      - alert: BrightDataExporterDown
        expr: brightdata_up == 0
        for: 10m
        annotations:
          summary: "brightdata-exporter scrape failing"
```

## Limitations

- **Event Log is UI-only.** Bright Data's per-request log (last 200 requests,
  shown in the dashboard's "Event Log" tab) has no public API. To capture
  request-level logs, instrument your client at the call site (write to
  Loki / CloudWatch / etc.) — the exporter cannot fill that gap.

- **Cost rates are derived, not authoritative.** Bright Data's API exposes
  the period total (`cost`, `gbs`) but not the explicit `$/GB` rate. The
  exporter computes `cost / gbs` and exposes it as
  `brightdata_zone_rate_usd_per_gb` — this matches the UI for pay-per-GB
  zones but is meaningless for unlimited-bandwidth (subscription) plans.

- **`security` label is a best-effort.** The Bright Data UI shows a
  "Security" column with no documented API field. The exporter uses
  `plan.vips_type` (residential) or `plan.ips_type` (datacenter) — usually
  `shared` or `dedicated`. Open an issue if your account shows a value the
  exporter doesn't surface.

- **`brightdata_account_can_make_requests` is about proxy creds, not the
  API token.** Bright Data's `/status` endpoint reports
  `can_make_requests=true` only when called with valid *proxy-network*
  credentials (zone username + proxy password). Called without proxy
  password (which is how the exporter calls it) it returns
  `can_make_requests=false` with `auth_fail_reason="zone_not_found"` or
  `"wrong_password"` — even on a perfectly healthy account where the API
  token is working fine. For exporters that only consume the REST API
  (no proxy traffic) this gauge is expected to read 0; use
  `brightdata_up` and `time() - brightdata_exporter_last_scrape_timestamp_seconds`
  for actual liveness signals. The `can_make_requests` gauge is kept for
  callers that *do* exercise the proxy plane and pass proxy auth via a
  custom endpoint.

## Development

```bash
git clone https://github.com/danielgines/brightdata-exporter.git
cd brightdata-exporter

# One-shot setup — installs dev deps + pre-commit hooks
just install

# Local CI bundle (ruff + mypy + pytest + helm lint + helm template)
just ci

# Run only the tests
just test

# Local stack (exporter + Prometheus + Grafana, all on localhost)
BRIGHTDATA_API_TOKEN=your-token \
  BRIGHTDATA_API_AUTH_TOKEN=$(openssl rand -hex 32) \
  just compose-up

# Trivy scan of the local image
just trivy

# All recipes
just
```

Pre-commit runs ruff, mypy, yamllint, hadolint, actionlint, gitleaks, and
helm validation on every commit, and the full pytest suite on every push.
The same checks run in CI as `just ci`. See `justfile` for the recipe
inventory and `.pre-commit-config.yaml` for the hook details.

## Releasing

`pyproject.toml` is the **canonical version source**. `__init__.py` and
`Chart.yaml` (both `version` + `appVersion`) are auto-synced from it via
`scripts/sync-version.py`, enforced as a pre-commit hook so drift can't
slip in.

The flow is one command + push:

1. Add a `## [X.Y.Z]` section to `CHANGELOG.md` describing what's changing
2. Run **`just release X.Y.Z`**, which:
   - Validates the working tree is clean and you're on `main`
   - Validates `CHANGELOG.md` has the matching section
   - Bumps `pyproject.toml`, runs `sync-version.py` to propagate
   - Stages all four files + `CHANGELOG.md`
   - Creates the commit `chore(release): vX.Y.Z`
3. `git push origin main`
4. CI runs → on success, `release.yaml` fires via `workflow_run`,
   detects the `chore(release):` subject, validates the four version
   sources still align (defense-in-depth), and runs the full release
   pipeline:
   - Builds linux/amd64 + linux/arm64 image with SBOM + provenance attestations
   - Pushes to `ghcr.io/<owner>/brightdata-exporter:X.Y.Z` (+ semver + `latest`)
   - Trivy-scans the published image, **fails on any unfixed CRITICAL CVE**
   - **Cosign keyless signs** the image by digest via GitHub OIDC
   - Packages and pushes the Helm chart to `oci://ghcr.io/<owner>/charts/brightdata-exporter`
   - Creates the `vX.Y.Z` git tag (after artifacts succeed)
   - Creates a GitHub Release with auto-generated notes

**No PAT required.** Everything happens inside one workflow run, so
`GITHUB_TOKEN` is sufficient (the "GITHUB_TOKEN-pushed events don't
trigger other workflows" limitation only matters when you need a
*second* workflow to fire). The release workflow accepts three triggers:

- `workflow_run` from `ci` succeeding on main (the primary path above)
- `workflow_dispatch` with a `version` input (manual re-release path,
  e.g. retry after a transient failure)
- `push: tags ["v*"]` (someone using GitHub UI's "Draft a new release"
  which creates a tag, or `git push origin v0.2.8` directly)

**Verifying signed images:**

```bash
cosign verify ghcr.io/<owner>/brightdata-exporter@<digest> \
  --certificate-identity-regexp 'https://github\.com/<owner>/brightdata-exporter/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

### ArtifactHub indexing

The Helm chart is also published with verified-publisher metadata on
[ArtifactHub](https://artifacthub.io). ArtifactHub treats each chart
as its own repository (it does **not** enumerate registry namespaces),
so registration is per-chart with the chart's full OCI URL, not the
parent namespace.

One-time setup:

1. Sign in at https://artifacthub.io with GitHub
2. Add a Helm OCI repository pointing to
   **`oci://ghcr.io/<owner>/charts/brightdata-exporter`** (chart
   path — NOT just `…/charts`)
3. Copy the assigned `repositoryID` into `helm/artifacthub-repo.yml`
4. Commit, push, and run **`just artifacthub-publish`** (or trigger
   the `artifacthub-publish` workflow from the GitHub Actions UI)

After that, every chart release is auto-indexed by ArtifactHub on its
next scrape (~30 min cadence). The verified-publisher badge requires
the metadata artifact to be present at
`ghcr.io/<owner>/charts/brightdata-exporter:artifacthub.io` (the same
OCI repo as the chart, dedicated `artifacthub.io` tag) with the right
`repositoryID` — that's what step 4 publishes.

## License

[MIT](LICENSE) — © 2026 Daniel Gines.
