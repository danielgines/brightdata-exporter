# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added — Helm chart `values.schema.json` (with the non-elastic contract baked in)

The chart now ships a JSON Schema (draft-07) at the chart root,
covering every field in `values.yaml`. Helm enforces the schema on
every `install`, `upgrade`, `template`, and `lint` run — invalid
values are rejected client-side with a structured error message
before the manifest is rendered.

Concretely the schema:

- Locks down the **top-level vocabulary** with `additionalProperties:
  false`, so `relicaCount: 1` (typo) is rejected at admission rather
  than silently ignored.
- Enforces the **non-elastic contract** as machine-readable
  invariants — `replicaCount`, `autoscaling.minReplicas`, and
  `autoscaling.maxReplicas` are all bounded to exactly 1. Operators
  who try to scale the app out get a fast, specific error pointing
  at the schema instead of a runtime 429 storm against Bright Data.
  When [issue #8](https://github.com/danielgines/brightdata-exporter/issues/8)
  is resolved, the bounds relax in one place.
- Mirrors the **pydantic Settings constraints** in
  `src/brightdata_exporter/config.py` — `config.scrapeInterval`
  rejects values below 30, `config.periodDays` is clamped to 1-366,
  rate/cache fields enforce their `gt=` / `ge=` boundaries.
- Pins **closed-vocabulary enums**: `image.pullPolicy`, `service.type`,
  `strategy.type`, `config.logLevel`, `config.logFormat`,
  `podDisruptionBudget.unhealthyPodEvictionPolicy`,
  `podSecurityContext.seccompProfile.type`. Catches values like
  `pullPolicy: Sometimes` or `logLevel: verbose` at admission.
- Validates **probes** against the standard Kubernetes Probe shape
  (httpGet + tcpSocket + exec, with port/path/scheme constraints).

ArtifactHub picks up the schema from the packaged `.tgz` and renders
the **Values Schema** badge plus an interactive schema browser on
the chart page.

Free-form blocks (`*.annotations`, `*.labels`, `nodeSelector`,
`tolerations`, `affinity`, `serviceMonitor.relabelings`,
`autoscaling.metrics`, `autoscaling.behavior`,
`networkPolicy.ingress.from`, `networkPolicy.egress.extra`) intentionally
allow operator-supplied content so the schema doesn't constrain
upstream Kubernetes API shapes that we don't want to vendor.

## [0.2.16] — 2026-05-06

### Changed — Helm chart PDB default shape (drain-friendly + compliance-friendly)

`podDisruptionBudget.enabled` flips from `false` to `true`. The shipped
shape changes from `minAvailable: 1` to `maxUnavailable: 1` plus
`unhealthyPodEvictionPolicy: AlwaysAllow` (k8s 1.27+, GA 1.31).

Why: `minAvailable: 1` on a single-replica deployment is a
[documented anti-pattern](https://kubernetes.io/docs/tasks/run-application/configure-pdb/) —
it BLOCKS node drains entirely, making the chart the cluster's
maintenance blocker for zero protection benefit (one replica down ==
service down, regardless of PDB). The new defaults:

- Are a no-op while `replicas: 1` (drains succeed normally).
- Become real protection automatically once `replicas >= 2` (issue
  [#8](https://github.com/danielgines/brightdata-exporter/issues/8))
  without any spec change — `maxUnavailable: 1` is the recommended
  forward-compatible shape per the upstream docs.
- Survive the "stuck NotReady/Terminating pod blocks all drains"
  failure mode via `unhealthyPodEvictionPolicy: AlwaysAllow`.
- Satisfy "every workload must have a PDB" compliance policies
  (Kyverno, OPA Gatekeeper, ValidatingAdmissionPolicy) without the
  drain-blocking downside.

Operators who genuinely run `replicas >= 3` and prefer strict uptime
over drain-friendliness can still override `minAvailable` (mutually
exclusive with `maxUnavailable`).

### Added — HorizontalPodAutoscaler template (compliance-only, opt-in)

New `templates/hpa.yaml` behind `autoscaling.enabled` (default `false`).
Defaults pin `minReplicas: maxReplicas: 1` — Kubernetes accepts
`min == max`, the controller simply keeps the deployment at a fixed
count rather than scaling. The rendered HPA carries explicit
annotations:

- `brightdata-exporter.io/hpa-purpose: "policy-compliance-only"`
- `brightdata-exporter.io/non-elastic-tracking-issue: "https://github.com/danielgines/brightdata-exporter/issues/8"`

so operators auditing the manifest see the intent. Raising
`maxReplicas` above 1 BREAKS the service (Bright Data 429s,
duplicate scrape cycles, overlapping Prometheus series) — see
issue #8 for the architectural background.

The chart now also OMITS `spec.replicas` on the Deployment when
`autoscaling.enabled=true` so the HPA controller owns the field —
prevents GitOps drift loops between Argo CD / Flux's chart-rendered
count and the HPA-driven count.

For clusters where the cleaner answer is a Kyverno `PolicyException`
exempting this workload from a `require-hpa` policy, the README now
documents that path alongside the opt-in HPA.

### Added — configurable Deployment update strategy

New `strategy` block in `values.yaml` (default `type: Recreate`,
matching the previous hardcoded value). When the app moves to
`replicas >= 2` (after issue #8), operators can switch to
`type: RollingUpdate` and supply `rollingUpdate.maxSurge` /
`rollingUpdate.maxUnavailable` without forking the chart.

### Added — `podDisruptionBudget.annotations`

Operator-supplied annotations on the rendered PodDisruptionBudget
resource. Useful for owner/contact metadata, ArgoCD ignore
directives, etc.

### Added — `helm-template-full` smoke now covers `autoscaling.enabled=true`

The CI smoke render that exercises every feature flag now also
includes the new HPA template, catching template-time bugs in PR
review the same way it catches them for ServiceMonitor /
NetworkPolicy / auth.

## [0.2.15] — 2026-05-06

### Added — chart-specific `README.md` for ArtifactHub indexing

ArtifactHub looks for a `README.md` at the **chart root** (next to
`Chart.yaml`) inside the packaged `.tgz`, not in the repository root.
Without it the package page showed "This package version does not
provide a README file" — discoverability hit, since most users land
on ArtifactHub before the GitHub repo.

Added `helm/brightdata-exporter/README.md` covering:

- **TL;DR install** — single `helm install oci://…` command
- **Required configuration** — `auth.apiToken` + `auth.apiAuthToken`
- **Common patterns** — production with ExternalSecret + ServiceMonitor;
  locked-down NetworkPolicy; strict Pod Security / Kyverno alignment
- **Single-replica contract** — explains why scaling breaks and links
  to issue #8
- **Values reference index** — pointers into `values.yaml` by section
- **Image supply chain** — multi-arch + cosign verify command + SBOM
  + provenance + Trivy gate disclosure

The repo-root `README.md` stays focused on the project as a whole;
the chart `README.md` is purely operational ("I want to install
this chart, what do I do?"). Different audiences, no duplication.

`helm package` automatically picks up `README.md` next to `Chart.yaml`
— verified with `tar -tzf` showing `brightdata-exporter/README.md`
in the resulting `.tgz`.

## [0.2.14] — 2026-05-06

### Added — Helm chart compliance hardening

Three opt-in / secure-by-default fields aligning the chart with strict
Pod Security + Kyverno policy environments:

- **`automountServiceAccountToken: false`** (new default) — set on
  both the ServiceAccount AND the pod spec. The exporter never calls
  the Kubernetes API, so the projected SA token is dead weight that
  attackers could exfil. Some platforms run Kyverno's
  `restrict-sa-token-mounts` policy that requires this; the rest get
  a free zero-trust win.
- **`terminationGracePeriodSeconds: 30`** explicit and configurable.
  Default value is the same as the k8s implicit default but making
  it visible documents the SIGTERM → drain → SIGKILL contract and
  lets operators bump it for slow-network clusters.
- **`priorityClassName: ""`** (opt-in). Empty by default = use cluster
  default. Set in production to e.g. `system-cluster-critical` for
  preemption protection.

### Added — PodDisruptionBudget template (opt-in, default disabled)

`templates/poddisruptionbudget.yaml` ships behind `podDisruptionBudget.enabled`
(default `false`). For single-replica deployments, a PDB with
`minAvailable: 1` BLOCKS node drains — making the chart the cluster's
maintenance blocker. Default-off lets cluster admins drain freely;
operators who genuinely need uptime-during-drain opt in.

### Added — ArtifactHub annotations on Chart.yaml

Rather than ship a separate `artifacthub-pkg.yml`, the chart now
carries ArtifactHub-specific metadata in `Chart.yaml.annotations`
(the Helm-idiomatic path used by prometheus-community / bitnami).
Surfaces:

- **Category** `monitoring-logging` for ArtifactHub search filters
- **License** `MIT` (SPDX identifier)
- **Containers Images tab** linking the GHCR image (CVE scan integration)
- **Quick links** in the sidebar (source, container image, changelog)
- **Per-version Changelog tab** populated via `artifacthub.io/changes`
  list with structured kinds (added / fixed / changed)

`scripts/sync-version.py` now also syncs the image tag in the
`artifacthub.io/images` annotation alongside Chart.yaml's `version` +
`appVersion` — single bump bumps all four references.

### Fixed — OCI label `org.opencontainers.image.version`

Was being stamped with `0.2` (the semver `{{major}}.{{minor}}`
pattern from the metadata-action's tag generator) instead of the full
release version. `docker/metadata-action`'s default-derived label
overrides whatever the Dockerfile's `LABEL` directive carries. Fixed
by passing an explicit `labels:` input to metadata-action that pins
the full version.

### Added — issue tracking for deferred work

Three GitHub issues opened to track work that doesn't fit a single
release:

- [#8](https://github.com/danielgines/brightdata-exporter/issues/8)
  HPA / multi-replica support — needs distributed rate limiter,
  cache, leader-elected scrape, and per-replica metric labels.
  Documented in `values.yaml` next to `replicaCount: 1` so operators
  see the constraint before trying to scale.
- [#9](https://github.com/danielgines/brightdata-exporter/issues/9)
  `__main__.py` test coverage gap — entrypoint wiring + signal
  handling untested. Tagged `good first issue`.
- [#10](https://github.com/danielgines/brightdata-exporter/issues/10)
  Docker Hub mirror — eval and implement when there's a real
  user-asked-for-it signal.

### Changed

- `astral-sh/setup-uv@v3 → @v7` (#3)

## [0.2.13] — 2026-05-06

### Changed — Python runtime 3.12 → 3.14

Bumped the runtime base image from `python:3.12-slim-trixie` to
`python:3.14-slim-trixie` (#7). Verified all our Python deps have
appropriate wheels: pure-Python ones (httpx, prometheus-client,
pydantic, pydantic-settings, structlog, etc) ship as `py3-none-any`
which is forward-compatible; `pydantic-core` 2.46.3 already ships
`cp314` wheels.

Side fixes that PR #7 left for us to clean up:

- **Header comment** updated to reflect Python 3.14.
- **Builder image** also bumped: `ghcr.io/astral-sh/uv:0.8-python3.12-trixie-slim`
  → `ghcr.io/astral-sh/uv:0.11-python3.14-trixie-slim`. PR #7 only
  bumped the runtime stage; leaving the builder on 3.12 with a 3.14
  runtime worked because our wheel is pure-Python, but is fragile —
  any future native build step would surface ABI mismatch. Same Python
  series across both stages eliminates that class of bug. (This
  obsoletes the still-open dependabot PR #6 which proposed
  `0.8 → 0.11`; we just took both bumps at once.)
- **pip cleanup path** is now Python-version-agnostic. Previously
  hardcoded `/usr/local/lib/python3.12/site-packages/pip*` which is a
  no-op in a 3.14 image. Now derived at build time:
  `python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])'`.
  The defense-in-depth cleanup runs in any future Python series.

Smoke-tested locally: `docker build` succeeds, container starts,
`/healthz` returns 200, `pip` is fully removed from the runtime image
(no `pip*` directories under `/usr/local/lib/python3.14/site-packages`).

### Added — `concurrency` block on release.yaml

Serializes release runs on the same branch with `cancel-in-progress:
false`. Cancelling mid-release would leave GHCR in a half-pushed state
(image at version X exists but no helm chart, no git tag, no GitHub
Release) — far worse than a queued second run. The resolve job's
commit-subject filter keeps non-release queue entries cheap (~16s
skip) so this doesn't bottleneck normal CI.

### Documentation

- `[![PyPI]]` badge removed from README — we don't publish to PyPI
  (operational decision, not a regression). Distribution is GHCR
  (image + chart) + ArtifactHub (chart index).
- `[![Artifact Hub]]` badge added — links to the chart's ArtifactHub
  search result and shows publication status (verified-publisher
  badge will appear once ArtifactHub finishes the next scrape cycle).

## [0.2.12] — 2026-05-06

### Changed — GitHub Actions toolchain refresh

Routine dependabot bumps on the workflow surface, no behavior changes
to the published artifact:

- `docker/setup-qemu-action@v3 → @v4` (#1)
- `docker/setup-buildx-action@v3 → @v4` (#2)
- `actions/checkout@v4 → @v6` (#4)
- `docker/build-push-action@v6 → @v7` (#5) — the v7.0.0 breaking
  changes (Node 24 default runtime, removed deprecated env vars
  `DOCKER_BUILD_NO_SUMMARY` / `DOCKER_BUILD_EXPORT_RETENTION_DAYS`)
  do not affect this project. SBOM / provenance / cosign integration
  contract unchanged.

## [0.2.11] — 2026-05-06

### Fixed — ArtifactHub verification metadata path

The verification metadata artifact was being pushed to a sub-path
(`<chart>/artifacthub-repo:latest`) which ArtifactHub never reads.
Per the [ArtifactHub OCI docs](https://artifacthub.io/docs/topics/repositories/helm-charts/),
the artifact must use the **dedicated `artifacthub.io` tag on the
SAME OCI repo as the chart**:

  Wrong: `ghcr.io/<owner>/charts/<chart>/artifacthub-repo:latest`
  Right: `ghcr.io/<owner>/charts/<chart>:artifacthub.io`

That's why the package showed `verified_publisher: False` even after
the metadata was pushed — ArtifactHub looked at the right tag and
found nothing. Same fix in three places: workflow yaml, justfile
recipe, README, in-file comment.

### Fixed — drop unsupported `icon:` from Chart.yaml

ArtifactHub's image handler accepts PNG / JPG / SVG / WebP but **not
ICO**. With `icon: https://brightdata.com/favicon.ico` set, every scan
emitted "image: unknown format" errors next to otherwise-valid index
entries. Bright Data doesn't host their logo at a publicly-discoverable
PNG/SVG URL we can confidently link to (404s on the obvious paths +
trademark concerns about embedding a 3rd-party logo on a community
project), so the icon field is removed entirely. ArtifactHub falls
back to its default placeholder.

To add a custom logo later, host a PNG/SVG at any public URL (e.g.
a raw.githubusercontent.com path inside the repo, or a personal CDN)
and re-add the `icon:` field.

## [0.2.10] — 2026-05-05

### Changed — GitHub Release page now mirrors the curated CHANGELOG section

Previously `softprops/action-gh-release@v2` was invoked with
`generate_release_notes: true`, which builds the release page body from
the list of commits since the previous tag. For a project that
maintains a hand-curated `CHANGELOG.md` this duplicated the inferior
auto-list right next to the link to the actual changelog.

Replaced by an awk extraction step that pulls the `## [X.Y.Z]` section
of `CHANGELOG.md` and passes it as the `body` to action-gh-release.
Behavior:

- Awk captures lines from the version heading until the next `## [`
  heading (or EOF). The version heading itself is skipped — GitHub's
  release UI already shows the version in its title.
- If the section is missing or empty, the job fails with a clear
  message: an operator who forgot to write the CHANGELOG entry can't
  ship an empty release page. Image + Helm chart are already published
  by the time this step runs, so the failure mode is "release artifact
  exists but no Release page" — recoverable by a subsequent push fixing
  the CHANGELOG, or by editing the GitHub Release manually.

The Release page for this version (v0.2.10) demonstrates the new
behavior — what you see here is exactly what landed in CHANGELOG.md
under `## [0.2.10]`, not the auto-generated commit list.

## [0.2.9] — 2026-05-05

### Removed — `auto-tag.yaml` and the `RELEASE_PAT` requirement

The previous design split tag creation (auto-tag.yaml) from the release
pipeline (release.yaml) so that pushing a `chore(release): vX.Y.Z`
commit would auto-tag the repo and trigger the release. This required a
`RELEASE_PAT` secret because GitHub forbids `GITHUB_TOKEN`-pushed tags
from triggering other workflows (anti-recursion).

Collapsed both into a single `release.yaml` with three triggers
(`workflow_run` on ci success, `workflow_dispatch` for manual
re-release, `push: tags ["v*"]` for manual tag push). Everything now
happens inside one workflow run, so `GITHUB_TOKEN` is sufficient — the
anti-recursion limitation only matters when you need a *second*
workflow to fire.

### Operator impact

- `RELEASE_PAT` no longer needed. Setting it does no harm.
- The `chore(release): vX.Y.Z` → push → ci → release flow is unchanged
  from the operator's perspective.
- New manual path: `gh workflow run release.yaml -f version=0.2.9` to
  re-release a specific version (e.g. retry after a transient registry
  outage).

### Implementation

- New `resolve` job at the top funnels all three trigger paths to a
  common `(version, tag, ref, should_run)` output set; downstream
  jobs only see those.
- New `tag` job creates and pushes the `vX.Y.Z` annotated tag at the
  END of the release (after artifacts succeed) — this means a partial
  failure leaves no tag, so retries are clean.
- Tag job is skipped on `push: tags` triggers (tag already exists);
  the `github-release` job's `if:` accepts both 'success' and
  'skipped' states for it.

## [0.2.8] — 2026-05-05

### Added — single-source-of-truth version sync

Operator now bumps version in **one place** (`pyproject.toml`) and
tooling propagates. Previously the same version had to be edited in
four files (`pyproject.toml`, `src/brightdata_exporter/__init__.py`,
`helm/brightdata-exporter/Chart.yaml.version`, and `Chart.yaml.appVersion`),
making drift easy and `just release`-style helpers necessary as
mitigation.

- **`scripts/sync-version.py`** (new) — reads the canonical version
  from `pyproject.toml [project].version` and rewrites the literal
  in `__init__.py` (`__version__`) and `Chart.yaml` (`version` +
  `appVersion`). Idempotent; exits 0 when sources already align,
  exits 1 when it rewrote a file (the standard pre-commit "I fixed
  something, please re-stage and retry" UX).
- **Pre-commit hook** wired to run on edits to any of the four files,
  so manual edits that desync them are caught at commit time.
- **`just release X.Y.Z`** new recipe — validates working tree is
  clean, you're on `main`, and `CHANGELOG.md` has a matching
  `## [X.Y.Z]` section; bumps `pyproject.toml`; runs `sync-version.py`
  to propagate; stages everything; creates the
  `chore(release): vX.Y.Z` commit. One operator command per release.

The `version-gate` job in `release.yaml` and the in-workflow
validation in `auto-tag.yaml` are kept as defense-in-depth — they
catch the (unlikely) case of someone bumping `pyproject.toml`,
bypassing pre-commit (`--no-verify`), and pushing directly. Costs
~5s of CI, blocks a broken release.

## [0.2.7] — 2026-05-05

### Security & supply chain — full release pipeline hardening

This release closes the remaining items from the security audit and
formalizes the development → release workflow.

#### NetworkPolicy template in Helm chart

Opt-in `networkPolicy.enabled=true` (default false for back-compat)
emits a Kubernetes NetworkPolicy that:

- **Ingress** — only the peers configured in
  `networkPolicy.ingress.from` (raw NetworkPolicyPeer list) can reach
  port `:9617`. Empty list = deny all ingress.
- **Egress** — three independently toggleable blocks:
  - `allowDNS` (default true) — UDP/TCP 53 to kube-dns / CoreDNS in
    kube-system (label `k8s-app=kube-dns`)
  - `allowBrightDataAPI` (default true) — TCP 443 to public internet,
    with RFC1918 + link-local CIDRs **excluded** so the pod cannot
    accidentally reach internal services
  - `extra` — raw NetworkPolicyEgressRule list for FQDN egress (Cilium
    / Calico Enterprise) or corp-proxy carve-outs

Validated end-to-end in kind+Calico: 4 ingress permutations (allowed
peer 200, wrong namespace timeout, wrong pod label timeout, allowed
peer + bearer 200) and 3 egress permutations (DNS works, BD API
reachable, RFC1918 blocked).

#### CI workflow — Trivy scanning with SARIF upload

`.github/workflows/ci.yaml` now includes:

- `trivy-fs` — filesystem scan (deps + Dockerfile + Helm + IaC),
  HIGH+CRITICAL fixable, **report-only** (uploads SARIF to GitHub
  Security tab; doesn't fail the job)
- `trivy-image` — builds the container, then **gates** on any unfixed
  CRITICAL CVE; HIGH is reported but not gated. Two SARIF uploads
  per run (one per severity slice).
- `lint-test` job extended with `mypy strict` + `ruff format --check`
  for parity with the local `just ci` bundle

#### Release workflow — SBOM + provenance + Cosign keyless signing

`.github/workflows/release.yaml` rebuilt around four jobs gated by a
`version-gate`:

- **version-gate** — fails the release if `pyproject.toml`,
  `src/brightdata_exporter/__init__.py`, and Helm `Chart.yaml`
  (version + appVersion) don't all match the git tag. Single source
  of truth, refuses to ship drifted metadata.
- **ghcr** — multi-arch build (amd64 + arm64) with **buildx
  attestations**:
  - SLSA provenance (`provenance: mode=max`) — full build trace
  - SBOM (`sbom: true`) — SPDX format, attached as image attestation
  Then post-push Trivy scan **gates on CRITICAL** (catches anything
  introduced between local and CI). Then **Cosign keyless signs** the
  image by digest using GitHub OIDC (no key rotation; identity is
  bound to the workflow run via Sigstore certificate). README documents
  the verification command.
- **helm-chart** — packages and pushes the chart as an OCI artifact
  to `ghcr.io/<owner>/charts/brightdata-exporter`. One registry, two
  artifact types, single auth path.
- **github-release** — auto-generated notes from commits since the
  prior tag.

The PyPI publishing job was removed — distribution channel is
container + Helm chart, not pip. Easier to keep the release surface
focused.

### Local development experience

- **`.pre-commit-config.yaml`** (new) — 18 hooks across two stages:
  pre-commit (trailing-whitespace, end-of-file-fixer, check-yaml/toml/
  json, check-merge-conflict, check-added-large-files, detect-private-
  key, mixed-line-ending, ruff, ruff-format, yamllint, hadolint,
  actionlint, gitleaks, mypy strict, helm lint, helm template smoke)
  and pre-push (full pytest suite). `--no-verify` discouraged but
  available as escape hatch.
- **`.yamllint.yaml`** (new) — strict on operational YAML, ignores
  Helm templates which contain Go template directives that aren't
  valid YAML until rendered.
- **`justfile`** (new) — 24 recipes grouping setup / python / helm /
  container / stack / ci / cleanup. `just install` does the one-shot
  setup, `just ci` runs the same checks the CI does (~5s), `just
  ci-full` adds docker build + Trivy + multi-arch (~2 min).
- **`pyproject.toml`** — `pre-commit>=3.7` added to the dev extra.
- **mypy strict** — fixed 30 pre-existing `union-attr` violations in
  `client.py` parsers via the intermediate-variable + explicit-type
  pattern. The `# type: ignore[arg-type]` in `config.py` was unused
  and removed. `server.py` adds a `per-file-ignores` for `N802` to
  preserve the stdlib `BaseHTTPRequestHandler.do_<METHOD>` dispatch
  convention.
- **ruff lint** — 8 instances of `isinstance(x, (int, float))`
  modernized to `isinstance(x, int | float)` (UP038, Python 3.10+ syntax).

### Distribution metadata

- Helm chart Chart.yaml `version` + `appVersion` synced to the
  release tag (was drifted at 0.2.5 while pyproject was 0.2.6); the
  release workflow now blocks any future drift via `version-gate`.

### Auto-tag — `chore(release): vX.Y.Z` → tag → release

`.github/workflows/auto-tag.yaml` (new): watches pushes to `main`,
waits for `ci` to succeed, then if the commit subject matches
`chore(release): vX.Y.Z`, validates that all four version sources
match `X.Y.Z`, and creates + pushes the `vX.Y.Z` annotated tag. The
tag push fires `release.yaml`.

Subject regex tested locally against the two production shapes:
`chore(release): v0.2.8` (direct push) and
`chore(release): v0.2.8 (#42)` (squash-merge with PR suffix). Both
match; non-release subjects (`chore: bump`, `feat: ...`, malformed
versions) are correctly rejected.

Requires a `RELEASE_PAT` repository secret (fine-grained PAT scoped
to this repo with `Contents: Read and write`). GitHub blocks
`GITHUB_TOKEN`-pushed tags from triggering subsequent workflows
to prevent recursion; without the PAT the tag is created but the
release workflow doesn't fire. The workflow fails fast with a clear
error message when `RELEASE_PAT` is unset, so misconfiguration is
visible.

README "Releasing" section documents the manual side (bump four
sources, write CHANGELOG entry, commit `chore(release): vX.Y.Z`,
direct-push to main) and the automated side (validation, tag push,
multi-arch + signed publish + Helm OCI).

## [0.2.6] — 2026-05-05

### Added — bearer auth on /api/* (CRITICAL security gap closed)

`/api/*` was unauthenticated and CORS-wide-open: anyone with network
access to the pod could read account balance, per-zone spend, IP
rosters, and usage limits. The audit flagged this as the highest-impact
gap in the security posture (above any container hardening or trivy
finding).

New env var `BRIGHTDATA_API_AUTH_TOKEN`:

- **Empty (default)** — `/api/*` is open. Back-compat with v0.2.x;
  emits a `WARNING api.auth_disabled` log line at startup so this
  posture is visible in the deployment logs.
- **Set** — every `/api/*` request must include
  `Authorization: Bearer <token>`. Mismatch returns HTTP 401 with
  `WWW-Authenticate: Bearer realm="brightdata-exporter"`. Comparison
  uses `hmac.compare_digest` to defeat timing oracles.

Security boundary unchanged for `/metrics`, `/healthz`, `/readyz`, `/`
— these are never auth-gated. Prometheus scrape contract assumes
unauth `/metrics` on a trusted network; if your network isn't
trusted, isolate via NetworkPolicy.

CORS now also returns `Access-Control-Allow-Headers: Authorization`
so cross-origin Grafana clients can include the bearer header.

### Helm chart support

- New values: `auth.apiAuthToken` (inline literal) /
  `auth.existingAuthSecret` + `auth.existingAuthSecretKey` (separate
  Secret) / can also live in the same Secret as `BRIGHTDATA_API_TOKEN`
  under the `BRIGHTDATA_API_AUTH_TOKEN` key
- `templates/secret.yaml` renders both keys when inline values are
  provided
- `templates/deployment.yaml` mounts the optional bearer token via
  `secretKeyRef.optional: true` so the pod still boots if the key is
  absent

### Tests

`tests/test_server.py` (new, 16 tests) — first coverage for the
`server.py` boundary, addressing a P1 gap from the v0.2.5 test-engineer
audit:

- `_verify_bearer` unit tests (correct token, wrong token, missing
  header, wrong scheme, case-insensitive Bearer, empty server token
  rejects everything)
- End-to-end via real `MetricsServer` on ephemeral port
  (`127.0.0.1:0`) covering: /metrics + /healthz + /readyz unauth even
  when token set, /api/* open when unset, 401 + WWW-Authenticate when
  token set + missing/wrong/Basic-scheme, 200 when correct, OPTIONS
  preflight passes without auth, index page open, unknown path 404

Test count: 89 → 105 (+16).

### Documentation

- README "REST service" section gains an Authentication subsection
  documenting the gate and Grafana Infinity setup
- README config table adds `BRIGHTDATA_API_AUTH_TOKEN`
- Helm `values.yaml` documents the three secret-source patterns

## [0.2.5] — 2026-05-05

### Fixed — three correctness bugs surfaced by code/test audits

**Rate limiter sleep-inside-lock (high impact under concurrency).**
`RateLimiter.acquire()` was holding the lock during `time.sleep()`, so
concurrent acquirers serialized on the lock for the full sleep duration
instead of forming an ordered queue. With 4 threads at 10 rps the wall
clock ran ~4× the interval (each thread waited the full pace) instead of
~3× (3 gaps between 4 slots). Fix: reserve the slot inside the lock,
release, then sleep until the reserved instant.

**Single-flight cache could hang followers forever.** `_Promise.wait()`
had no timeout — a leader thread that hung mid-compute (Bright Data API
hang, leader killed by SIGKILL) would stall every follower indefinitely,
slowly exhausting the HTTP server's thread pool. Fix: bounded wait with
a configurable `single_flight_timeout` (default 60s), raises
`TimeoutError` when exceeded.

**`make_key` collision between bare path and empty param dict.** Both
`make_key("/api/account")` and `make_key("/api/zones", {"from": ""})`
produced the key `"/api/account"` (the `make_key("/x", {})` case
collapsed to just `"/x"`). On Grafana variable interpolation that
produces empty params, this could cross-route cache hits between
unrelated endpoints. Fix: empty-dict and all-blank-values now produce
`"path?"` (with trailing `?`) so they cannot collide with a bare path.

### Refactored — pricing logic deduplicated

The `rate_display` + `billing_model` helpers existed in both
`collector.py` and `service.py` (with identical bodies and a comment
acknowledging the duplication). Extracted to `brightdata_exporter/pricing.py`
with a single `pricing_pair(cost) -> (display, model)` entry point; both
the periodic `/metrics` Info series and the on-demand `/api/zones`
response now route through the same code path so they cannot drift.

### Tests

- `tests/test_ratelimit.py` (new, 6 tests) — concurrency contract for the
  shared limiter, including the "do not serialize on lock" regression
- `tests/test_pricing.py` (new, 8 tests) — every billing scheme, plus the
  consistency contract between `pricing_display` and `billing_model`
- `tests/test_cache.py` — added `test_make_key_distinguishes_bare_path_from_empty_param_dict`,
  `test_make_key_distinguishes_paths_under_same_params`,
  `test_invalid_single_flight_timeout_raises`,
  `test_followers_time_out_when_leader_hangs`
- `tests/test_client.py` — added 4 upstream-failure pins (429, 5xx,
  ReadTimeout, non-JSON 200) so a future "let's add retries" PR has a
  regression net
- `tests/test_collector.py` — added parametrized matrix tests for the
  plan-aware gates covering every known plan_product (8 cases for
  `_zone_supports_ips_per_country`, 8 for `_zone_supports_dedicated_vips`)
- `tests/test_service.py` — added boundary test
  `test_zones_window_passes_through_at_exactly_min_days` for the
  off-by-one bait in `_clamp_window`

Test count: 51 → 89 (+38).

### Documentation

- README "Configuration" entry for `BRIGHTDATA_COLLECT_IP_ROSTERS` now
  documents the plan-aware gating shipped in v0.2.4
- README "Operations dashboard" health row description updated to
  "Last scrape" (the panel rename from v0.2.3, previously omitted)
- README SDK-comparison endpoint count corrected (9 → 14)

### Security — base image bookworm → trixie

Bumped both Dockerfile stages to Debian 13 (trixie):

- `ghcr.io/astral-sh/uv:0.5-python3.12-bookworm-slim` →
  `ghcr.io/astral-sh/uv:0.8-python3.12-trixie-slim` (builder)
- `python:3.12-slim-bookworm` → `python:3.12-slim-trixie` (runtime)

Trivy delta on the resulting image (HIGH+CRITICAL only):

| Severity | bookworm | trixie  | delta |
|----------|----------|---------|-------|
| CRITICAL | 3        | **0**   | −3    |
| HIGH     | 11       | 7       | −4    |
| TOTAL    | 14       | 7       | **−50%** |

Eliminated outright (all CRITICAL): glibc CVE-2026-0861, libgnutls30
CVE-2026-33845, libsqlite3-0 CVE-2025-7458, zlib1g CVE-2023-45853
(`will_not_fix` in bookworm). All four had no fix available on
bookworm; trixie ships the patched versions.

The 7 remaining HIGH are all `status=affected` with no upstream fix
yet (libcap2 TOCTOU, libsystemd0/libudev1 IPC RCE, ncurses 4-pkg
buffer overflow). None are reachable in this container's threat
model — no TTY (ncurses unused), no D-Bus / systemd interaction,
`capabilities drop ALL` + `runAsNonRoot` neutralizes libcap escalation
vectors. They are platform noise rather than exploitable bugs.

Image size: ~144 MB (was ~150 MB on bookworm).

Smoke test validated post-rebuild: `/healthz` 200, `/metrics` 200,
container starts under 4s.

Also added `apt-get upgrade -y` to the runtime stage's apt block. As
of this build it produces zero delta (the 7 remaining HIGH all have
`fix=-` in the trixie repo), but it future-proofs the image: any
subsequent `docker build` automatically picks up Debian security
patches without waiting for the upstream `python:3.12-slim-trixie`
tag to be rebuilt. Build cost: ~10-30s; layer is cleaned via
`apt-get clean && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*`.

### Distribution metadata — service identity, not just exporter

The Helm chart, raw k8s manifests, and Dockerfile still described the
project as a pure Prometheus exporter — written in v0.1.0 before the
v0.2.0 pivot to hybrid exporter+REST. Brought the deployment surface in
line with the actual identity:

- **`helm/brightdata-exporter/Chart.yaml`** — description rewritten to
  reflect both interfaces; version + appVersion bumped from 0.1.0 to
  0.2.5; keywords expanded with `rest-api`, `finops`, `observability`,
  `grafana`.
- **`helm/brightdata-exporter/values.yaml`** — `config:` block split
  into 4 labelled sections (Periodic /metrics collector, Optional
  collectors, On-demand REST /api/* service, Logging) and now exposes
  9 previously-missing env vars: `collectIpRosters`, `collectRecentIps`,
  `collectIpHealth`, `collectNetworkStatus`, `collectDomainConsumption`,
  `apiEnabled`, `cacheTtlSeconds`, `cacheMaxSize`, `apiMinWindowDays`.
  Without these, `helm install` couldn't configure the FinOps half at all.
- **`helm/brightdata-exporter/templates/deployment.yaml`** — env block
  passes through all 9 new vars.
- **`Dockerfile`** — added OCI image labels
  (`org.opencontainers.image.{title,description,version,revision,...}`)
  so registry browsers (GHCR, Docker Hub) show the correct identity.
  Version + revision injected via `BRIGHTDATA_VERSION` and
  `BRIGHTDATA_REVISION` build args.
- **`.github/workflows/release.yaml`** — release pipeline now passes
  `BRIGHTDATA_VERSION=${{ github.ref_name }}` and
  `BRIGHTDATA_REVISION=${{ github.sha }}` so published images carry
  accurate metadata.
- **`examples/kubernetes/deployment.yaml`** — header comment
  enumerates both `/metrics` and `/api/*` surfaces.

## [0.2.4] — 2026-05-05

### Fixed — bug, not noise: spurious scrape_errors from plan-incompatible endpoints

Every scrape cycle was calling `/zone/ips?ip_per_country=true` and
`/zone/route_vips` on every active zone, even when the zone's plan
deterministically rejects those endpoints. On a 14-zone account this
generated **~430 spurious upstream errors per hour**, all counted in
`brightdata_exporter_scrape_errors_total` — making the metric useless
for actual alerting because the floor was permanent noise.

Empirical verification against `api.brightdata.com` (2026-05-05) maps
plan compatibility:

- `/zone/ips?ip_per_country=true` returns **400 "Wrong zone plan"** for
  rotating-residential, SERP, mobile, and unblocker plans — even when
  those plans have allocated VIPs (e.g. `vips_type=domain, vip=1`)
- `/zone/route_vips` returns **403 "Vip routes not found"** for
  datacenter zones and **422** for rotating-residential without VIPs;
  only `vips_type=domain AND vip=true` zones return 200

The collector now reads `plan` info from `/zone?zone=NAME` (already
fetched + cached every cycle) and skips the call when the gate says no.
Two new helpers in `collector.py`:

- `_zone_supports_ips_per_country(info)` — false for `res_rotating`,
  `serp`, `mobile`, `unblocker` plan products
- `_zone_supports_dedicated_vips(info)` — true only when
  `plan.vips_type == "domain"` AND `plan.vip == true`

Side effects:

- `brightdata_exporter_scrape_errors_total` returns to **~0/h** for
  healthy accounts → alert thresholds become meaningful
- API rate-limit budget freed: ~28 calls/cycle saved on a 14-zone
  rotating-heavy account (2 endpoints × ~14 plan-mismatched zones), so
  scrape duration drops noticeably

### Tests

- Five new tests in `test_collector.py` covering the gate matrix:
  rotating-residential skips both, datacenter skips only route_vips,
  dedicated-VIP residential calls route_vips even though product is
  rotating, SERP skips both, and verification that the
  `scrape_errors_total` series stays absent for skipped endpoints.

## [0.2.3] — 2026-05-05

### Fixed — misleading "API auth" panel in Operations dashboard

The Operations dashboard had a stat panel labelled "API auth" backed by
`brightdata_account_can_make_requests`. Empirical verification against
`api.brightdata.com` showed that gauge does NOT measure API-token
validity — Bright Data's `/status` endpoint reports
`can_make_requests=true` only when called with valid **proxy-network**
credentials (zone username + proxy password). Called without proxy
password (the exporter's pattern) it returns
`can_make_requests=false` with `auth_fail_reason="zone_not_found"` or
`"wrong_password"` even on a healthy account where every other endpoint
is returning 200. So the panel rendered "BLOCKED" red while the
exporter was happily scraping.

Replaced with a "Last scrape" stat: seconds since the last successful
scrape cycle, computed as
`time() - brightdata_exporter_last_scrape_timestamp_seconds`. Green up
to 600s (2× default scrape interval), orange to 900s, red beyond.

### Changed — clarified `brightdata_account_can_make_requests` docstring

Metric HELP text now warns that the gauge reflects proxy-network creds,
not API-token validity, and points callers at `brightdata_up` for
API-side health. The metric is kept (callers that *do* exercise the
proxy plane via a custom endpoint may want it) but its meaning is
documented honestly.

### Documentation

- README "Limitations" section gained a new bullet documenting the
  `/status` proxy-vs-API-auth pitfall, so consumers don't misinterpret
  `can_make_requests=0` as a token problem.

## [0.2.2] — 2026-05-05

### Changed — split single dashboard into FinOps + Operations

Replaced the single mixed-datasource dashboard with two dashboards that
each own one half of the hybrid architecture:

- **`examples/grafana-dashboard.json` — Bright Data — Account & Zones**
  (FinOps investigation). 100% Infinity datasource. Picker-driven. Account
  row is now `/api/account` (snapshot — picker irrelevant), Zones table
  unchanged, and the old Trends row was replaced by a new **Cost
  breakdown** row with bar charts (top 10 by spend, top 10 by traffic)
  and donut charts (spend by Pool Type, spend by zone Type) — all over
  the picked range.

- **`examples/grafana-dashboard-operations.json` — Bright Data —
  Operations** (continuous monitoring, new). 100% Prometheus. Picker
  selects the visible window of recorded samples, not the upstream query
  range. Includes account drawdown, derived `$/day burn rate`, top 10
  zones by cost / traffic, $/GB drift, exporter health (`up`, scrape
  duration, scrape errors), API auth status, IPs unavailable, proxies
  pending replacement, Bright Data network status, and zone counts by
  status.

Why the split: a single dashboard mixing Prometheus + Infinity caused a
foot-gun where snapshot tiles (Account row, originally Prometheus
`instant: true`) returned "No data" whenever the time picker was set to
a past range outside Prometheus's recorded window — even though the
underlying value (account balance) is timeless. The split puts each
question on the right datasource: snapshot → Infinity, evolution →
Prometheus.

The two dashboards link to each other via the dashboard header.

### Removed

- The `${datasource}` template variable from the FinOps dashboard
  (no longer needed — datasource is implied by panel target).

## [0.2.1] — 2026-05-05

### Fixed — empty tables on short time-picker windows

`/api/zones` and `/api/zones/{name}` now expand the requested `from`/`to`
window when the caller asks for a range below `BRIGHTDATA_API_MIN_WINDOW_DAYS`
(default `1`). Bright Data's billing data rolls up daily — a request whose
range falls inside the current day returns zeroed cost/traffic regardless
of actual usage, which made the Grafana Zones table render an empty
all-zero result whenever the dashboard was on "Last 5 minutes" / "Last 15
minutes" / etc.

The clamp expands `from` backward to `to - api_min_window_days`; `to` is
preserved. The response's per-row `period` field reflects the queried
range so the caller can see what was actually executed. Set
`BRIGHTDATA_API_MIN_WINDOW_DAYS=0` to disable.

### Configuration additions

- `BRIGHTDATA_API_MIN_WINDOW_DAYS` (default `1`) — minimum from/to window
  enforced by the REST handlers.

## [0.2.0] — 2026-05-05

### Added — REST service mode (FinOps / ad-hoc queries)

Pivoted the project from a single-mode Prometheus exporter into a
hybrid observability + FinOps service. Same single binary, two interfaces:

- **Prometheus exporter** (`/metrics`, unchanged) — still pulls account
  balance and per-zone cost/traffic/request totals on a fixed rolling
  window for alerts, time-series trends, and long-term retention.

- **REST service** (`/api/*`, new) — on-demand JSON queries that take a
  `from=&to=` range driven by the caller (e.g. a Grafana time picker).
  Endpoints:
    * `GET /api/account` — balance + status + zone counts (snapshot)
    * `GET /api/zones?from=&to=` — list zones with cost/traffic/requests
      for the requested range
    * `GET /api/zones/{name}?from=&to=` — single-zone detail incl.
      IP roster

The REST service exists because the Prometheus model doesn't naturally
support "user picks an arbitrary date range and the answer reflects that
range". A periodic exporter scrapes a fixed window — the time picker
only chooses which sample of that window to display, not the window
itself. For FinOps investigations ("how much did I spend Mar 14–Apr 22?")
that distinction matters.

### Added — supporting modules

- `cache.py` — TTL cache with single-flight semantics. Concurrent dashboard
  viewers requesting the same `/api/*` key collapse onto one upstream
  call; the rest wait for the result. Bounded LRU eviction.
- `ratelimit.py` — promoted out of `collector.py` so the periodic
  collector AND the on-demand service handlers share one rate limiter,
  collectively respecting Bright Data's 1 req/s/token limit.
- `service.py` — dispatcher + handlers for `/api/*`, with status/regex
  filters and per-plan-type rate formatting (`$X/GB` vs `$X/CPM` vs
  `$X/month` vs `$X/VIP`).
- `server.py` — routes `/api/*` to the service module, adds `Access-
  Control-Allow-Origin: *` so Grafana on a different host can call.

### Added — Grafana dashboard

`examples/grafana-dashboard.json` Zones table now consumes
`/api/zones?from=…&to=…` via the Infinity datasource. The dashboard's
time picker drives the actual upstream window — switch from "last 7 days"
to "last 30 days" and the cost / traffic / request numbers change.
Account row + Trends row stay on Prometheus.

### Configuration additions

- `BRIGHTDATA_API_ENABLED` (default `true`) — set `false` to run as
  Prometheus-only.
- `BRIGHTDATA_CACHE_TTL_SECONDS` (default `300`) — cache lifetime for
  `/api/*` responses. `0` disables.
- `BRIGHTDATA_CACHE_MAX_SIZE` (default `1000`) — LRU bound on cached keys.

## [0.1.0] — 2026-05-05

Initial release.

### Added

- Bright Data API client covering `/customer/balance`,
  `/zone/get_all_zones`, `/zone/get_active_zones`, `/zone?zone=NAME`
  (alias `/zone/info`), `/zone/cost`, `/zone/bw`, `/zone/status`. Schema
  verified empirically against `api.brightdata.com` on 2026-05-05.
- Prometheus metric registry exposing 17 metric families across account,
  per-zone, and exporter introspection scopes.
- Periodic collector with rate limiter (1 req/s default) and TTL cache
  for zone info (1h default).
- HTTP server with `/metrics`, `/healthz`, `/readyz`, `/`.
- Pydantic-settings env-driven config (12 settings, all `BRIGHTDATA_*`
  prefixed).
- Multi-stage Dockerfile (~150MB final image, non-root, read-only fs).
- Helm chart with Deployment, Service, ServiceAccount, ServiceMonitor
  (opt-in), and Secret (opt-in — recommended path is `auth.existingSecret`
  with ExternalSecret operator).
- Docker Compose example bundling exporter + Prometheus + Grafana with
  a starter dashboard provisioned automatically.
- Kubernetes raw manifests for Helm-free deployments.
- Test suite (16 tests) covering client schema parsing, error paths, and
  full collector cycles against pytest-httpx mocks.

### Notes

- Bright Data does not expose request-level logs via API; the dashboard's
  "Event Log" tab is UI-only. The exporter cannot fill that gap.
- Cost rates (`brightdata_zone_rate_usd_per_gb`) are derived as
  `cost / gbs` over the configured period — meaningful for pay-per-GB
  zones, undefined for unlimited-bandwidth subscription plans.
- The `security` label in `brightdata_zone_info` falls back from
  `plan.vips_type` to `plan.ips_type` — best effort against the
  undocumented "Security" column shown in the Bright Data UI.
