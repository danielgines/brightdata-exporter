# syntax=docker/dockerfile:1.7
#
# brightdata-exporter — multi-stage build.
#
#   Stage 1 (builder): use uv to resolve + lock and build the wheel.
#   Stage 2 (runtime): python:3.12-slim with the wheel + non-root user.
#
# Why uv: ~10x faster install than pip during CI builds. uv also produces
# a deterministic uv.lock that the runtime stage consumes.

FROM ghcr.io/astral-sh/uv:0.8-python3.12-trixie-slim AS builder

WORKDIR /build

# Copy only the bits needed to compute deps + build the wheel.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# Build the wheel into /build/dist.
RUN uv build --wheel --out-dir /build/dist

# ---------------------------------------------------------------------------

FROM python:3.12-slim-trixie AS runtime

# OCI image labels — surface the project identity in registry browsers
# (GHCR, Docker Hub, Artifact Registry) without requiring a README pull.
# `BRIGHTDATA_VERSION` and `BRIGHTDATA_REVISION` are typically populated by CI
# (e.g. `--build-arg BRIGHTDATA_VERSION=$(git describe --tags) --build-arg
# BRIGHTDATA_REVISION=$GITHUB_SHA`); they default to "dev" / "" when building
# locally so an unannotated build still produces a valid manifest.
ARG BRIGHTDATA_VERSION=dev
ARG BRIGHTDATA_REVISION=""
LABEL org.opencontainers.image.title="brightdata-exporter" \
      org.opencontainers.image.description="Bright Data observability + FinOps service. Single binary exposing Prometheus /metrics for alerting + REST /api/* for ad-hoc FinOps queries with caller-supplied date ranges." \
      org.opencontainers.image.url="https://github.com/danielgines/brightdata-exporter" \
      org.opencontainers.image.source="https://github.com/danielgines/brightdata-exporter" \
      org.opencontainers.image.documentation="https://github.com/danielgines/brightdata-exporter/blob/main/README.md" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="Daniel Gines" \
      org.opencontainers.image.version="${BRIGHTDATA_VERSION}" \
      org.opencontainers.image.revision="${BRIGHTDATA_REVISION}"

# Pull any security patches the base image hasn't been rebuilt against
# yet, then install runtime deps. Tini handles SIGTERM forwarding (stdlib
# http.server doesn't trap it otherwise); ca-certificates is needed by
# httpx to verify api.brightdata.com.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends tini ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Non-root user — exporter writes nowhere on disk so a read-only fs is fine.
RUN groupadd --system --gid 1000 brightdata \
    && useradd --system --uid 1000 --gid 1000 --create-home --shell /sbin/nologin brightdata

# Install the wheel produced by the builder stage, then REMOVE pip from
# the runtime image. pip is needed exactly once (this command); the
# running exporter never invokes it again. Keeping pip around exposed
# us to its CVEs (e.g. CVE-2025-8869, CVE-2026-1703 — Trivy rates these
# CRITICAL via NVD CVSS even though GHSA tracks them as MEDIUM/LOW). No
# pip → no pip CVE noise + no install-time foot-gun if a future operator
# `docker exec` and tries to `pip install` something into the running
# container.
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl \
    && rm /tmp/*.whl \
    && pip uninstall -y pip setuptools \
    && rm -rf /root/.cache/pip /usr/local/lib/python3.12/site-packages/pip* /usr/local/lib/python3.12/site-packages/setuptools*

USER brightdata
WORKDIR /home/brightdata

EXPOSE 9617

ENV PYTHONUNBUFFERED=1 \
    BRIGHTDATA_LISTEN_HOST=0.0.0.0 \
    BRIGHTDATA_LISTEN_PORT=9617 \
    BRIGHTDATA_LOG_FORMAT=json

# Healthcheck pulls /healthz — works without curl using stdlib urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9617/healthz', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["brightdata-exporter"]
