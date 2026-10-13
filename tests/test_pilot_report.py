"""Interim pilot reports contain only sanitized, reproducible aggregates."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bench.pilot_report import (
    _chart,
    _routing_provenance,
    _scenario,
    _validate_failure_phase,
    publish,
)
from fleet.vast import FleetError

ROOT = Path(__file__).resolve().parents[1]


def test_only_reviewed_text_scenarios_are_accepted(tmp_path: Path) -> None:
    target = tmp_path / "benchmarks" / "campaigns" / "scenarios"
    target.mkdir(parents=True)
    for name in (
        "pilot-three-tenant-text",
        "pilot-chat-calibration",
        "pilot-nine-cohort-smoke",
        "pilot-worker-loss-during",
        "pilot-worker-loss-recovered",
    ):
        source = ROOT / "benchmarks" / "campaigns" / "scenarios" / f"{name}.json"
        shutil.copyfile(source, target / f"{name}.json")
        loaded = _scenario(tmp_path, name)
        assert loaded["name"] == name
        assert len(loaded["sha256"]) == 64
        if name == "pilot-nine-cohort-smoke":
            assert len(loaded["cohorts"]) == 9
    with pytest.raises(FleetError, match="allowlisted"):
        _scenario(tmp_path, "../../escape")
    changed = json.loads((target / "pilot-chat-calibration.json").read_text(encoding="utf-8"))
    changed["cohorts"].append(changed["cohorts"][0])
    (target / "pilot-chat-calibration.json").write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(FleetError, match="duplicated"):
        _scenario(tmp_path, "pilot-chat-calibration")


def test_failure_checkpoint_requires_surviving_then_restored_routing() -> None:
    entry = {"counts": {"completed": 11}, "workerDispatches": {"kip-gpu-2": 11}}
    _validate_failure_phase("pilot-worker-loss-during", [entry])
    with pytest.raises(FleetError, match="both GPU workers"):
        _validate_failure_phase("pilot-worker-loss-recovered", [entry])
    entry["workerDispatches"] = {"kip-gpu-1": 4, "kip-gpu-2": 7}
    _validate_failure_phase("pilot-worker-loss-recovered", [entry])
    with pytest.raises(FleetError, match="surviving"):
        _validate_failure_phase("pilot-worker-loss-during", [entry])


def test_routing_provenance_requires_owned_hash_and_pre_run_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "deploy" / "fleet"
    target.mkdir(parents=True)
    for name in ("versions.json", "llmd-router-flow-values.yaml"):
        shutil.copyfile(ROOT / "deploy" / "fleet" / name, target / name)
    expected = hashlib.sha256((target / "llmd-router-flow-values.yaml").read_bytes()).hexdigest()
    lock = json.loads((target / "versions.json").read_text(encoding="utf-8"))
    values = {
        "kip": {
            "owner": "kube-inference-platform",
            "chartDigest": lock["inference"]["routerGatewayChartDigest"],
            "valuesSha256": expected,
        },
        "router": {"epp": {"pluginsConfigFile": "kip-pilot-flow-plugins.yaml"}},
    }
    history = [{"revision": 6, "status": "deployed", "updated": "2026-10-13T01:52:18Z"}]
    deployment = {
        "metadata": {"annotations": {"meta.helm.sh/release-name": "kip-qwen25-pilot"}},
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"kube-inference-platform.io/router-values-sha256": expected}
                }
            }
        },
    }
    pod = {
        "metadata": {
            "annotations": {"kube-inference-platform.io/router-values-sha256": expected},
            "creationTimestamp": "2026-10-13T01:52:30Z",
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [{"restartCount": 0}],
        },
    }

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "helm":
            payload = values if command[1] == "get" else history
        else:
            payload = deployment if "deployment" in command else {"items": [pod]}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("bench.pilot_report.subprocess.run", fake_run)
    start = datetime(2026, 10, 13, 1, 53, tzinfo=UTC)
    result = _routing_provenance(tmp_path, start)
    assert result["valuesSha256"] == expected
    assert result["helmRevision"] == 6
    assert result["pickerPodCreatedAtUtc"] == "2026-10-13T01:52:30.000000Z"
    history[0]["updated"] = "2026-10-13T01:54:00Z"
    assert _routing_provenance(tmp_path, start)["pickerPodCreatedAtUtc"] == (
        "2026-10-13T01:52:30.000000Z"
    )
    pod["metadata"]["creationTimestamp"] = "2026-10-13T01:54:30Z"
    with pytest.raises(FleetError, match="not running"):
        _routing_provenance(tmp_path, start)
    pod["metadata"]["creationTimestamp"] = "2026-10-13T01:52:30Z"
    values["kip"]["owner"] = "unmanaged"
    with pytest.raises(FleetError, match="drifted"):
        _routing_provenance(tmp_path, start)
    values["kip"]["owner"] = "kube-inference-platform"
    pod["metadata"]["annotations"]["kube-inference-platform.io/router-values-sha256"] = "old"
    with pytest.raises(FleetError, match="not running"):
        _routing_provenance(tmp_path, start)


def fixture_report() -> dict:
    return {
        "runId": "pilot-text-20261013-012123",
        "window": {
            "startedAtUtc": "2026-10-13T01:21:29Z",
            "endedAtUtc": "2026-10-13T01:22:38Z",
            "costWindow": "2026-10-13T01:21:00Z,2026-10-13T01:23:00Z",
        },
        "workload": {"sha256": "a" * 64},
        "cohorts": [
            {
                "tenant": "tenant-a",
                "class": "chat",
                "dataset": {
                    "trial": 6,
                    "seed": 42,
                    "datasetSha256": "b" * 64,
                    "sessions": 4,
                    "offeredSessionsPerSecond": 0.8,
                },
                "counts": {"offered": 17, "completed": 17, "rejected": 0},
                "ttftMs": {"p50": 1000, "p95": 1800, "p99": 2000},
                "tpotMs": {"p95": 20},
                "e2eMs": {"p95": 5000},
                "outputTokensPerSecond": 53,
                "inputTokens": 30000,
                "outputTokens": 3143,
                "estimatedTenantUsdByOutputShare": 0.06,
            }
        ],
        "cost": {
            "fleetHourlyUsd": 2.0474,
            "auditUsd": 0.068247,
            "allocatedUsd": 0.06831,
            "reconciliationPercent": 0.09,
            "basis": "verified whole-node Vast rates and OpenCost allocation",
        },
        "grafanaUrl": "http://localhost:3000/d/kip-llmd-fleet-pilot/kip-llm-d-fleet-pilot",
        "dataQuality": {"warnings": ["Insufficient samples for nominal SLO acceptance."]},
    }


def test_chart_height_scales_for_nine_cohorts() -> None:
    report = fixture_report()
    report["cohorts"] = report["cohorts"] * 9
    assert 'height="659"' in _chart(report)


def test_sanitized_bundle_is_atomic_allowlisted_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "deploy" / "fleet"
    source.mkdir(parents=True)
    shutil.copyfile(ROOT / "deploy/fleet/versions.json", source / "versions.json")
    shutil.copyfile(ROOT / "deploy/fleet/model.yaml", source / "model.yaml")
    destination = tmp_path / "benchmarks" / "reports" / "pilot-text-20261013-012123"
    report = fixture_report()
    publish(report, destination)
    publish(report, destination)
    assert {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    } == {
        "index.html",
        "summary.json",
        "summary.csv",
        "reproducibility.json",
        "charts/ttft-p95.svg",
    }
    body = "".join(
        path.read_text(encoding="utf-8") for path in destination.rglob("*") if path.is_file()
    )
    assert "Authorization" not in body
    assert "Rejected" in body
    assert "[DONE]" not in body
    assert "10.0.0.1" not in body
    assert json.loads((destination / "summary.json").read_text()) == report
    report["cohorts"][0]["ttftMs"]["p95"] = 1900
    with pytest.raises(FleetError, match="refusing overwrite"):
        publish(report, destination)
