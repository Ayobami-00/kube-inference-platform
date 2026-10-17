"""Focused tests for the offline-first committed-artifact quality gate."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "validate_artifacts.py"

SPEC = importlib.util.spec_from_file_location("validate_artifacts", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
QUALITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUALITY)


def test_explicit_structural_gate_covers_committed_contracts_without_skip_language() -> None:
    environment = os.environ.copy()
    environment.pop("PROMTOOL", None)
    environment.pop("HELM", None)
    environment.pop("KIP_KUBE_PROMETHEUS_STACK_CHART_ARCHIVE", None)
    environment.pop("KIP_OPENCOST_CHART_ARCHIVE", None)
    environment.pop("KIP_REQUIRE_EXTERNAL_VALIDATORS", None)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--structural-only"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    for contract in (
        "dashboard schemas",
        "profile schemas",
        "Prometheus rule structure",
        "Helm values and manifest YAML",
        "prohibited artifact/credential scan",
    ):
        assert f"PASS {contract}" in completed.stdout
    assert "SKIP" not in completed.stdout
    assert "explicit structural-only mode" in completed.stdout


def test_default_artifact_gate_fails_closed_without_external_validators() -> None:
    environment = os.environ.copy()
    environment.update({"PROMTOOL": "/missing/promtool", "HELM": "/missing/helm"})
    environment.pop("KIP_KUBE_PROMETHEUS_STACK_CHART_ARCHIVE", None)
    environment.pop("KIP_OPENCOST_CHART_ARCHIVE", None)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert "artifact-validator-setup" in completed.stderr


def test_strict_gate_requires_external_validators(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(QUALITY, "_command_path", lambda *_: None)
    _, rules = QUALITY.validate_prometheus_rules(ROOT)
    with pytest.raises(QUALITY.ArtifactError, match="strict validation requires the pinned"):
        QUALITY.validate_with_promtool(ROOT, rules, strict=True)


def test_strict_gate_rejects_wrong_promtool_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    promtool = tmp_path / "promtool"
    promtool.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'promtool, version 0.0.0 (fixture)'\n",
        encoding="utf-8",
    )
    promtool.chmod(0o755)
    monkeypatch.setenv("PROMTOOL", str(promtool))
    _, rules = QUALITY.validate_prometheus_rules(ROOT)
    with pytest.raises(QUALITY.ArtifactError, match="expected 3.14.0, got 0.0.0"):
        QUALITY.validate_with_promtool(ROOT, rules, strict=True)


def test_strict_gate_rejects_chart_bytes_with_matching_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helm = tmp_path / "helm"
    helm.write_text(
        "#!/bin/sh\nif [ \"$1\" = version ]; then printf '%s' 'v3.21.4'; exit 0; fi\nexit 99\n",
        encoding="utf-8",
    )
    helm.chmod(0o755)
    monitoring = tmp_path / "kube-prometheus-stack-91.4.0.tgz"
    opencost = tmp_path / "opencost-2.5.30.tgz"
    monitoring.write_bytes(b"not the reviewed upstream chart")
    opencost.write_bytes(b"not the reviewed upstream chart")
    monkeypatch.setenv("HELM", str(helm))
    monkeypatch.setenv("KIP_KUBE_PROMETHEUS_STACK_CHART_ARCHIVE", str(monitoring))
    monkeypatch.setenv("KIP_OPENCOST_CHART_ARCHIVE", str(opencost))
    with pytest.raises(QUALITY.ArtifactError, match="chart archive checksum mismatch"):
        QUALITY.validate_with_helm(ROOT, strict=True)


def test_make_check_requires_strict_external_validation() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^check:.*\bartifact-check\b", makefile, re.MULTILINE)
    artifact_recipe = makefile.split("artifact-check:", 1)[1].split("\n\n", 1)[0]
    assert "--strict-external" in artifact_recipe
    assert "PROMTOOL=" in artifact_recipe
    assert "HELM=" in artifact_recipe
    assert "KIP_KUBE_PROMETHEUS_STACK_CHART_ARCHIVE=" in artifact_recipe
    assert "KIP_OPENCOST_CHART_ARCHIVE=" in artifact_recipe


def test_validator_setup_is_checksum_pinned_and_never_pipes_to_a_shell() -> None:
    setup = (ROOT / "scripts/setup_artifact_validators.sh").read_text(encoding="utf-8")
    checksums = (ROOT / "scripts/artifact-validator-checksums.env").read_text(encoding="utf-8")
    completed = subprocess.run(
        ["bash", "-n", str(ROOT / "scripts/setup_artifact_validators.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--proto '=https'" in setup
    assert "require_sha256" in setup
    assert "@$KIP_KUBE_PROMETHEUS_STACK_DIGEST" in setup
    assert "@$KIP_OPENCOST_CHART_DIGEST" in setup
    assert re.search(r"curl[^\n]*\|\s*(?:ba)?sh", setup) is None
    checksum_values = re.findall(r'SHA256="([0-9a-f]{64})"', checksums)
    assert len(checksum_values) == 10
    assert len(set(checksum_values)) == 10


@pytest.mark.parametrize(
    "relative",
    [
        "benchmarks/.work/raw.json",
        "benchmarks/reports/run/raw/responses.json",
        "benchmarks/reports/run/responses.jsonl",
        "credentials/provider.json",
        ".env.production",
        "kubeconfig.target",
    ],
)
def test_publishable_scan_rejects_raw_and_secret_paths(tmp_path: Path, relative: str) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fixture", encoding="utf-8")
    with pytest.raises(QUALITY.ArtifactError, match="prohibited"):
        QUALITY.validate_candidate_paths(tmp_path, [path])


def test_publishable_scan_accepts_only_sanitized_report_contract(tmp_path: Path) -> None:
    allowed = [
        tmp_path / "benchmarks/reports/run/index.html",
        tmp_path / "benchmarks/reports/run/summary.json",
        tmp_path / "benchmarks/reports/run/summary.csv",
        tmp_path / "benchmarks/reports/run/reproducibility.json",
        tmp_path / "benchmarks/reports/run/telemetry.json",
        tmp_path / "benchmarks/reports/run/charts/latency.svg",
    ]
    for path in allowed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sanitized", encoding="utf-8")
    assert QUALITY.validate_candidate_paths(tmp_path, allowed) == len(allowed)


def test_publishable_scan_rejects_sensitive_report_content(tmp_path: Path) -> None:
    path = tmp_path / "benchmarks/reports/run/summary.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"rawPrompt":"private"}', encoding="utf-8")
    with pytest.raises(QUALITY.ArtifactError, match="sensitive raw field"):
        QUALITY.validate_candidate_paths(tmp_path, [path])
