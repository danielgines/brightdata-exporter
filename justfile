# brightdata-exporter — local development tasks.
#
# Install just (https://just.systems): `cargo install just` or `brew install just`.
# Run `just` (no args) to see the recipe list.

set shell := ["bash", "-cu"]
set dotenv-load := true

# Default: list recipes
default:
    @just --list --unsorted

# ---------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------

# First-time setup: install dev deps + pre-commit hooks
install:
    uv sync --extra dev
    uv run pre-commit install --install-hooks
    @echo "✓ dev deps + pre-commit hooks installed"
    @echo "  next: 'just ci' to run the full local CI bundle"

# Refresh deps after editing pyproject.toml
sync:
    uv sync --extra dev

# ---------------------------------------------------------------------
# Python — tests + lint + types
# ---------------------------------------------------------------------

# Run unit tests (105+ tests, ~2s)
test:
    uv run pytest -q

# Run a single test by name pattern (e.g. `just test-only test_zones_window`)
test-only PATTERN:
    uv run pytest -q -k "{{PATTERN}}"

# Run tests with coverage report (terminal + HTML at htmlcov/)
test-cov:
    uv run pytest \
      --cov=src/brightdata_exporter \
      --cov-report=term-missing \
      --cov-report=html
    @echo "✓ HTML coverage at htmlcov/index.html"

# Lint Python (ruff check)
lint:
    uv run ruff check src tests

# Format Python (ruff format) and auto-fix lint
format:
    uv run ruff format src tests
    uv run ruff check --fix src tests

# Type check (mypy strict, src only — see [tool.mypy] in pyproject)
typecheck:
    uv run mypy src

# ---------------------------------------------------------------------
# Helm chart
# ---------------------------------------------------------------------

# Helm chart structural lint
helm-lint:
    helm lint helm/brightdata-exporter

# Render Helm chart to stdout (use `-- --set foo=bar` for extra flags)
helm-template *FLAGS:
    helm template t helm/brightdata-exporter \
      --set auth.apiToken=fake-token \
      {{FLAGS}}

# Smoke render with every feature flag on (catches template bugs)
helm-template-full:
    helm template t helm/brightdata-exporter \
      --set auth.apiToken=fake \
      --set auth.apiAuthToken=fake \
      --set networkPolicy.enabled=true \
      --set serviceMonitor.enabled=true \
      > /dev/null
    @echo "✓ helm template renders with all features enabled"

# ---------------------------------------------------------------------
# Container image
# ---------------------------------------------------------------------

# Build container image (brightdata-exporter:dev) with version + git rev
docker-build:
    #!/usr/bin/env bash
    set -eu
    VERSION=$(grep '^version' pyproject.toml | head -1 | cut -d'"' -f2)
    REVISION=$(git rev-parse --short HEAD 2>/dev/null || echo dirty)
    docker build \
      -t brightdata-exporter:dev \
      --build-arg BRIGHTDATA_VERSION="${VERSION}" \
      --build-arg BRIGHTDATA_REVISION="${REVISION}" \
      .
    echo "✓ built brightdata-exporter:dev (v${VERSION} @ ${REVISION})"

# Multi-arch build smoke (amd64 + arm64; verifies both build cleanly)
docker-build-multiarch:
    #!/usr/bin/env bash
    set -eu
    if ! docker buildx inspect multiarch >/dev/null 2>&1; then
      docker run --privileged --rm tonistiigi/binfmt --install all
      docker buildx create --name multiarch --use
      docker buildx inspect --bootstrap >/dev/null
    else
      docker buildx use multiarch
    fi
    docker buildx build \
      --platform linux/amd64,linux/arm64 \
      --build-arg BRIGHTDATA_VERSION=multiarch-test \
      --output type=image,push=false \
      .
    docker buildx use default
    echo "✓ multiarch build (amd64 + arm64) clean"

# Refresh trivy's vulnerability DB ahead of a scan. Trivy auto-updates
# on first run of the day, but CI uses `aquasecurity/trivy-action` which
# pulls a fresh DB every run — local can drift. Force-refresh before any
# investigation to match what CI sees.
trivy-update:
    trivy image --download-db-only

# Trivy scan of the local image (HIGH+CRITICAL only). Refreshes the DB
# first so local result matches what the CI's trivy-action sees.
trivy: docker-build trivy-update
    trivy image \
      --severity HIGH,CRITICAL \
      --ignore-unfixed \
      --no-progress \
      --skip-version-check \
      brightdata-exporter:dev

# Trivy filesystem scan (deps + Dockerfile + IaC)
trivy-fs: trivy-update
    trivy fs \
      --severity HIGH,CRITICAL \
      --ignore-unfixed \
      --skip-version-check \
      --skip-dirs .venv,.pytest_cache,.mypy_cache,htmlcov \
      .

# ---------------------------------------------------------------------
# Local stack (docker-compose: exporter + Prometheus + Grafana)
# ---------------------------------------------------------------------

# Bring up the local stack (set BRIGHTDATA_API_TOKEN in env or .env)
compose-up: docker-build
    cd examples && docker compose up -d
    @echo "✓ stack up"
    @echo "    Grafana    → http://localhost:3000 (admin / admin)"
    @echo "    Prometheus → http://localhost:9090"
    @echo "    Exporter   → http://localhost:9617/metrics  /api/account  /api/zones?from=&to="

# Tear down the local stack including volumes
compose-down:
    cd examples && docker compose down -v

# Tail exporter logs
compose-logs:
    cd examples && docker compose logs -f brightdata-exporter

# ---------------------------------------------------------------------
# Pre-commit + CI parity
# ---------------------------------------------------------------------

# Run all pre-commit hooks against all files (CI parity)
pre-commit:
    uv run pre-commit run --all-files

# Push helm/artifacthub-repo.yml to GHCR as an OCI artifact so ArtifactHub
# can verify ownership of oci://ghcr.io/danielgines/charts. One-off after
# initial registration, then re-run only if owners/repositoryID change
# (NOT on every chart release — ArtifactHub auto-scrapes those).
#
# Requires:
#   - oras CLI (auto-installed to ~/.local/bin if missing)
#   - gh auth token (logged in via `gh auth login`)
#   - helm/artifacthub-repo.yml with the real repositoryID from artifacthub.io
artifacthub-publish:
    #!/usr/bin/env bash
    set -euo pipefail
    if grep -q REPLACE_AFTER_ARTIFACTHUB_REGISTRATION helm/artifacthub-repo.yml; then
      echo "::error::helm/artifacthub-repo.yml still has the placeholder repositoryID."
      echo "1. Sign in at https://artifacthub.io with GitHub"
      echo "2. Add a Helm OCI repo pointing to oci://ghcr.io/danielgines/charts"
      echo "3. Copy the assigned repositoryID into helm/artifacthub-repo.yml"
      echo "4. Re-run 'just artifacthub-publish'"
      exit 1
    fi
    if ! command -v oras >/dev/null 2>&1; then
      echo "Installing oras to ~/.local/bin ..."
      mkdir -p ~/.local/bin
      ORAS_VERSION=$(gh api repos/oras-project/oras/releases/latest --jq '.tag_name' | tr -d v)
      curl -sL "https://github.com/oras-project/oras/releases/download/v${ORAS_VERSION}/oras_${ORAS_VERSION}_linux_amd64.tar.gz" \
        | tar xz -C ~/.local/bin oras
      chmod +x ~/.local/bin/oras
      export PATH="$HOME/.local/bin:$PATH"
    fi
    gh auth token | oras login ghcr.io --username "$(gh api user --jq .login)" --password-stdin
    # ArtifactHub fetches verification metadata from the SAME OCI repo
    # as the chart, under the dedicated `artifacthub.io` tag.
    oras push \
      ghcr.io/danielgines/charts/brightdata-exporter:artifacthub.io \
      --config /dev/null:application/vnd.cncf.artifacthub.config.v1+yaml \
      helm/artifacthub-repo.yml:application/vnd.cncf.artifacthub.repository-metadata.layer.v1.yaml
    echo "✓ pushed metadata to ghcr.io/danielgines/charts/brightdata-exporter:artifacthub.io"
    echo "  ArtifactHub will pick it up on its next scan (~30 min)."

# Release flow — bump pyproject + sync derived files + commit `chore(release): vX.Y.Z`
# Usage:  just release 0.2.8
# Then `git push` to fire CI → auto-tag → release pipeline.
release VERSION:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{VERSION}}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      echo "::error::version must be X.Y.Z (got {{VERSION}})"
      exit 1
    fi
    if ! git diff --quiet || ! git diff --cached --quiet; then
      echo "::error::working tree is dirty — commit or stash first"
      exit 1
    fi
    if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then
      echo "::error::release commits go directly on 'main' (you're on $(git rev-parse --abbrev-ref HEAD))"
      exit 1
    fi
    if ! grep -q "^## \[{{VERSION}}\] " CHANGELOG.md; then
      echo "::error::CHANGELOG.md has no '## [{{VERSION}}]' section — add one before releasing"
      exit 1
    fi
    sed -i 's/^version = "[^"]*"/version = "{{VERSION}}"/' pyproject.toml
    python3 scripts/sync-version.py || true   # exits 1 when it rewrites — that's fine
    git add pyproject.toml src/brightdata_exporter/__init__.py helm/brightdata-exporter/Chart.yaml CHANGELOG.md
    git commit -m "chore(release): v{{VERSION}}"
    echo
    echo "✓ release commit created locally — push to fire CI → auto-tag → release"
    echo "    git push origin main"

# Update pre-commit hook versions
pre-commit-update:
    uv run pre-commit autoupdate

# The whole bundle CI would run — fast feedback before pushing
ci: lint typecheck test helm-lint helm-template-full
    @echo "✓ all CI checks passed"

# Fuller bundle including container build + scan + multiarch (~2 min)
ci-full: ci docker-build trivy docker-build-multiarch
    @echo "✓ all extended CI checks passed"

# ---------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------

# Remove caches + build artifacts (keeps deps + .venv)
clean:
    rm -rf build dist htmlcov .pytest_cache .mypy_cache .ruff_cache
    find . -type d -name __pycache__ -prune -exec rm -rf {} +
    @echo "✓ caches cleared"

# Nuke everything including venv + dev image (full reset)
clean-all: clean
    rm -rf .venv
    docker rmi brightdata-exporter:dev 2>/dev/null || true
    @echo "✓ full reset done"
