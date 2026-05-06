# brightdata-exporter

Helm chart for [brightdata-exporter](https://github.com/danielgines/brightdata-exporter) —
Bright Data observability + FinOps service. Deploys a single
container that exposes:

- **`/metrics`** — Prometheus exporter for account balance, per-zone
  cost / traffic / requests, and exporter-introspection signals
- **`/api/account` + `/api/zones?from=&to=`** — REST endpoints
  that take a caller-supplied date range, designed to drive Grafana's
  Infinity datasource so the dashboard time picker controls the
  upstream window
- **`/healthz`, `/readyz`** — kubelet probes (HTTP server liveness)

Runs single-replica by design — see the "Single replica" note below.

## TL;DR

```bash
helm install bd \
  oci://ghcr.io/danielgines/charts/brightdata-exporter \
  --version 0.2.14 \
  --namespace brightdata --create-namespace \
  --set auth.apiToken=$YOUR_BRIGHTDATA_TOKEN \
  --set auth.apiAuthToken=$(openssl rand -hex 32)
```

After install, the exporter reaches Bright Data on its own (1 req/s
pacing) and serves `/metrics` immediately. The first per-zone scrape
takes ~45s with 14 zones at the documented rate limit.

## Required configuration

| Parameter | Required? | Description |
|---|---|---|
| `auth.apiToken` OR `auth.existingSecret` | **yes** | Bright Data API token. Inline via `apiToken` for dev; reference an existing Secret for production |
| `auth.apiAuthToken` OR `auth.existingAuthSecret` | recommended | Bearer token guarding `/api/*`. Without it those endpoints are open to anyone with network access |

## Common configurations

### Production with ExternalSecret + ServiceMonitor

```yaml
auth:
  existingSecret: brightdata-credentials   # contains BRIGHTDATA_API_TOKEN + BRIGHTDATA_API_AUTH_TOKEN

serviceMonitor:
  enabled: true     # requires prometheus-operator

resources:
  requests: { cpu: 50m, memory: 128Mi }
  limits:   { cpu: 500m, memory: 256Mi }

priorityClassName: system-cluster-critical
```

### Locked-down ingress + egress (NetworkPolicy)

```yaml
networkPolicy:
  enabled: true
  ingress:
    from:
      - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: monitoring } }
        podSelector:       { matchLabels: { app.kubernetes.io/name: prometheus } }
      - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: grafana } }
        podSelector:       { matchLabels: { app.kubernetes.io/name: grafana } }
  # egress.allowDNS + egress.allowBrightDataAPI default to true
```

Requires a CNI that enforces NetworkPolicy (Calico, Cilium, Antrea).
`flannel` / `kindnet` / vanilla AWS VPC CNI silently admit but don't
filter — see the `templates/networkpolicy.yaml` comment for the
expected matrix.

### Strict Pod Security / Kyverno-policy environments

The chart ships compliant-by-default for the `restricted` Pod Security
Standard:

- `runAsNonRoot: true`, UID 1000
- `readOnlyRootFilesystem: true`
- `capabilities: drop: [ALL]`
- `allowPrivilegeEscalation: false`
- `seccompProfile: RuntimeDefault`
- `automountServiceAccountToken: false` (the exporter never calls
  the k8s API)

If your cluster runs Kyverno's `restrict-sa-token-mounts` policy,
this chart passes out of the box.

## Single replica — by design

`replicaCount: 1` is a contract, not a default. Increasing it WILL
break the exporter at four levels (rate limiter, cache, scrape
loop, metric labels) — see
[issue #8](https://github.com/danielgines/brightdata-exporter/issues/8)
for the full architectural picture.

The chart still ships compliance-friendly defaults for clusters that
enforce "every workload must have a PDB / HPA":

### PodDisruptionBudget — default ON

```yaml
podDisruptionBudget:
  enabled: true                              # default
  maxUnavailable: 1                          # default
  unhealthyPodEvictionPolicy: AlwaysAllow    # default (k8s 1.27+)
```

Why `maxUnavailable: 1` and not `minAvailable: 1`?

- With `replicas: 1` (today), `minAvailable: 1` BLOCKS node drains —
  a [documented anti-pattern](https://kubernetes.io/docs/tasks/run-application/configure-pdb/).
  `maxUnavailable: 1` lets drains succeed normally; the pod
  reschedules in ~10-60s. The PDB is effectively a no-op while
  single-replica, but ticks the compliance box without hurting ops.
- After [issue #8](https://github.com/danielgines/brightdata-exporter/issues/8)
  lands and you run `replicas >= 2`, the SAME spec becomes real
  protection — at most one replica unavailable during voluntary
  disruption — without any manifest change.
- `unhealthyPodEvictionPolicy: AlwaysAllow` prevents the "stuck
  NotReady/Terminating pod blocks all drains" failure mode.

To disable entirely (e.g. if your cluster has a `generate-pdb`
mutator policy and you want to defer to it):

```yaml
podDisruptionBudget:
  enabled: false
```

### HorizontalPodAutoscaler — default OFF, opt-in for compliance only

```yaml
autoscaling:
  enabled: true                              # opt-in
  minReplicas: 1
  maxReplicas: 1                             # hard contract — see issue #8
  targetCPUUtilizationPercentage: 80
```

This template exists ONLY to satisfy "every Deployment must have an
HPA" policies (Kyverno `check-hpa-exists`, equivalents). The default
shape pins replicas at 1 with `min == max` — Kubernetes accepts this,
the HPA controller simply keeps the deployment at a fixed count.

**Do NOT raise `maxReplicas` above 1** until issue #8 is resolved —
the app will 429 on Bright Data, duplicate scrape cycles, and emit
overlapping Prometheus series. The chart annotates the rendered HPA
with `brightdata-exporter.io/hpa-purpose: policy-compliance-only` so
operators auditing the manifest see the intent.

When `autoscaling.enabled=true`, the chart also OMITS `spec.replicas`
on the Deployment so the HPA owns the field — avoids GitOps drift
loops with Argo CD / Flux.

If your governance allows it, the cleaner alternative is a
[Kyverno `PolicyException`](https://kyverno.io/docs/guides/exceptions/)
exempting this workload from the HPA-required policy, with a
reference to issue #8 in the description.

## Values reference

See [`values.yaml`](./values.yaml) for the full annotated reference.
Key sections, in the same order:

- `image.*` — repository, tag, pullPolicy, pullSecrets
- `auth.*` — token plumbing (inline / existingSecret / external)
- `config.*` — env vars passed to the exporter (`BRIGHTDATA_*`)
- `service.*` — k8s Service for the HTTP server
- `serviceMonitor.*` — prometheus-operator integration (opt-in)
- `resources.*` — pod resources requests + limits
- `podSecurityContext` / `containerSecurityContext` — Pod Security
- `probes.liveness` / `probes.readiness` — kubelet health checks
- `nodeSelector`, `tolerations`, `affinity` — scheduling
- `priorityClassName` — opt-in for production preemption protection
- `terminationGracePeriodSeconds` — SIGTERM-to-SIGKILL window (default 30)
- `serviceAccount.*` — SA + projected-token settings
- `strategy.*` — Deployment update strategy (default `Recreate`)
- `podDisruptionBudget.*` — PDB (default ON, drain-friendly shape)
- `autoscaling.*` — HPA (default OFF; compliance-only `min=max=1` when ON)
- `networkPolicy.*` — opt-in ingress/egress restriction

## Container image

The chart deploys `ghcr.io/danielgines/brightdata-exporter` (the same
account as the chart). The image is:

- **Multi-arch** (linux/amd64 + linux/arm64)
- **Cosign keyless signed** by GitHub OIDC
- **SBOM + SLSA provenance** attestations attached
- **Trivy CRITICAL gate** on every release (no unfixed CRITICAL CVEs at publish time)

Verify the image:

```bash
cosign verify ghcr.io/danielgines/brightdata-exporter:0.2.14 \
  --certificate-identity-regexp 'https://github\.com/danielgines/brightdata-exporter/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Source + support

- **Repo**: https://github.com/danielgines/brightdata-exporter
- **Issues**: https://github.com/danielgines/brightdata-exporter/issues
- **Container image**: https://github.com/danielgines/brightdata-exporter/pkgs/container/brightdata-exporter
- **CHANGELOG**: https://github.com/danielgines/brightdata-exporter/blob/main/CHANGELOG.md
- **License**: [MIT](https://github.com/danielgines/brightdata-exporter/blob/main/LICENSE)
