"""Repository paths used by benchmark orchestration."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = PACKAGE_ROOT.parent


def repository_root() -> Path:
    """Return the configured repository root (injectable for fixture tests)."""
    override = os.environ.get("KIP_REPOSITORY_ROOT")
    return Path(override).resolve() if override else REPOSITORY_ROOT


def suites_root() -> Path:
    """Return the committed benchmark-suite directory."""
    return repository_root() / "benchmarks" / "suites"


def work_root() -> Path:
    """Return the ignored raw benchmark work directory."""
    override = os.environ.get("KIP_BENCH_WORK_ROOT")
    return Path(override).resolve() if override else repository_root() / "benchmarks" / ".work"


def reports_root() -> Path:
    """Return the sanitized report directory."""
    override = os.environ.get("KIP_BENCH_REPORTS_ROOT")
    return Path(override).resolve() if override else repository_root() / "benchmarks" / "reports"


def cost_profiles_root() -> Path:
    """Return the committed cost-profile directory."""
    override = os.environ.get("KIP_COST_PROFILES_ROOT")
    return Path(override).resolve() if override else repository_root() / "costs" / "profiles"


def kubernetes_assets_root() -> Path:
    """Return the committed AIPerf Kubernetes asset directory."""
    return repository_root() / "benchmarks" / "kubernetes"
