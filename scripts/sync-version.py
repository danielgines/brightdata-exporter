#!/usr/bin/env python3
"""Single-source-of-truth version sync.

The version in ``pyproject.toml`` is canonical. This script propagates
that value into the two other places that need to carry it literally:

  - ``src/brightdata_exporter/__init__.py`` — the runtime ``__version__``
    string (read by the package's own logging + by callers via
    ``importlib.metadata`` fallback paths)
  - ``helm/brightdata-exporter/Chart.yaml`` — both ``version`` and
    ``appVersion`` fields, since Helm packages the chart with those values
    and consumers see them in ``helm list`` / ``helm show chart``

It runs in two contexts:

  1. **Pre-commit hook** — every commit. If any derived file is out of
     sync, this script rewrites it. ``pre-commit`` then aborts the
     commit so the operator can ``git add`` the freshly-synced files
     and retry. (The "files modified by hook" failure mode is the
     standard pre-commit UX and is deliberate — it surfaces the
     change before it slips into the commit silently.)

  2. **`just release VERSION`** — bumps ``pyproject.toml`` to the new
     version then invokes this script to propagate. One operator
     command per release.

Exits 0 when nothing changed, 1 when changes were written, 2 on parse
failure. Pre-commit treats exit ≠ 0 as "this hook failed, abort the
commit", which is what we want.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT_PY = REPO_ROOT / "src" / "brightdata_exporter" / "__init__.py"
CHART_YAML = REPO_ROOT / "helm" / "brightdata-exporter" / "Chart.yaml"


def read_canonical_version() -> str:
    """Pull the version from pyproject.toml [project].version — the
    one source-of-truth for the whole repo."""
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise SystemExit(
            "ERR: pyproject.toml has no [project].version — set it before running this."
        )
    return version


def replace_in_file(path: Path, pattern: re.Pattern[str], replacement: str) -> bool:
    """Apply ``pattern.sub(replacement, ...)`` to ``path`` in-place.

    Returns True iff content actually changed; the caller uses this to
    decide whether to flag the run as "modified" for pre-commit.
    """
    original = path.read_text()
    new = pattern.sub(replacement, original, count=1)
    if new != original:
        path.write_text(new)
        return True
    return False


def sync_init_py(version: str) -> bool:
    """Rewrite the ``__version__ = "..."`` line in __init__.py."""
    return replace_in_file(
        INIT_PY,
        re.compile(r'^(__version__\s*=\s*)"[^"]*"', re.MULTILINE),
        rf'\g<1>"{version}"',
    )


def sync_chart_yaml(version: str) -> bool:
    """Rewrite ``version:``, ``appVersion:``, and the
    ``artifacthub.io/images`` annotation's image tag in Chart.yaml.

    Helm package + push uses exactly what's in this file at packaging
    time. The ArtifactHub `images` annotation references the same image
    by exact tag so ArtifactHub's CVE scan integration always points at
    the chart's matching container image.
    """
    changed_version = replace_in_file(
        CHART_YAML,
        re.compile(r"^(version:\s+).*$", re.MULTILINE),
        rf"\g<1>{version}",
    )
    # appVersion has different quoting convention (string with quotes).
    changed_app = replace_in_file(
        CHART_YAML,
        re.compile(r"^(appVersion:\s+).*$", re.MULTILINE),
        rf'\g<1>"{version}"',
    )
    # `artifacthub.io/images` annotation embeds the exact image tag.
    # Match the line ``image: ghcr.io/.../brightdata-exporter:<tag>``
    # and rewrite the tag to the canonical version.
    changed_image = replace_in_file(
        CHART_YAML,
        re.compile(
            r"(image:\s+ghcr\.io/[^\s:]+/brightdata-exporter:)[^\s]+",
            re.MULTILINE,
        ),
        rf"\g<1>{version}",
    )
    return changed_version or changed_app or changed_image


def main() -> int:
    canonical = read_canonical_version()
    changes: list[str] = []
    if sync_init_py(canonical):
        changes.append(str(INIT_PY.relative_to(REPO_ROOT)))
    if sync_chart_yaml(canonical):
        changes.append(str(CHART_YAML.relative_to(REPO_ROOT)))
    if not changes:
        print(f"version sync: pyproject={canonical} — all sources already aligned")
        return 0
    print(f"version sync: pyproject={canonical} — wrote new value to:")
    for f in changes:
        print(f"  - {f}")
    print()
    print("git add the changed files and re-commit (pre-commit's standard 'fix-and-retry' flow).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
