"""Ownership-safe H100 checkpoint release does not touch unrelated VMs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.lambda_cloud import LambdaError
from fleet.lambda_release import release
from fleet.lambda_rental import _save_private, load_lease, new_lease


class FakeClient:
    def __init__(self, lease: dict) -> None:
        node = lease["nodes"]["h100-control"]
        self.item = {
            "id": node["instanceId"],
            "name": node["name"],
            "region": {"name": node["region"]},
            "status": "active",
            "tags": [
                {"key": "kip-campaign", "value": lease["campaignId"]},
                {"key": "kip-role", "value": "h100-control"},
            ],
        }
        self.terminations: list[str] = []

    def instances(self) -> list[dict]:
        return [self.item] if self.item["status"] != "terminated" else []

    def terminate(self, instance_id: str) -> None:
        self.terminations.append(instance_id)
        self.item["status"] = "terminated"


def fixture(tmp_path: Path) -> tuple[Path, Path, dict]:
    lease = new_lease("us-east-1")
    lease["nodes"]["h100-control"] = {
        "role": "h100-control",
        "phase": "active",
        "instanceId": "task-owned-h100",
        "instanceType": "gpu_1x_h100_pcie",
        "name": "kip-lambda-test-h100-control",
        "region": "us-west-3",
        "createdAt": "2026-10-13T10:00:00Z",
        "endedAt": None,
        "priceCentsPerHour": 329,
    }
    lease_path = tmp_path / "lease.json"
    _save_private(lease_path, lease)
    report = tmp_path / "report"
    report.mkdir()
    (report / "index.html").write_text("<html></html>", encoding="utf-8")
    (report / "summary.csv").write_text("header\n", encoding="utf-8")
    (report / "summary.json").write_text(
        json.dumps(
            {
                "runId": "lambda-h100-pcie-control-20261013",
                "status": "exploratory-hardware-control",
                "model": "qwen3-8b",
                "rows": [{"concurrency": 1}, {"concurrency": 4}],
            }
        ),
        encoding="utf-8",
    )
    return lease_path, report, lease


def test_release_only_exact_owned_h100_after_report_is_local(tmp_path: Path) -> None:
    lease_path, report, lease = fixture(tmp_path)
    client = FakeClient(lease)
    assert release(client, lease_path, report) == "terminated"
    assert client.terminations == ["task-owned-h100"]
    assert load_lease(lease_path)["nodes"]["h100-control"]["phase"] == "terminated"


def test_release_refuses_ownership_drift_before_termination(tmp_path: Path) -> None:
    lease_path, report, lease = fixture(tmp_path)
    client = FakeClient(lease)
    client.item["tags"][0]["value"] = "unrelated"
    with pytest.raises(LambdaError, match="ownership"):
        release(client, lease_path, report)
    assert client.terminations == []
    assert load_lease(lease_path)["nodes"]["h100-control"]["phase"] == "active"
