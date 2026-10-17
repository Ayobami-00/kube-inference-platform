"""Task-owned Lambda teardown is report-gated, ordered, and uncertainty-safe."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.lambda_cloud import LambdaError
from fleet.lambda_down import captured_reports, down
from fleet.lambda_rental import _save_private, load_lease, new_lease

RUN_ID = "lambda-nine-cohort-20261013-123456"


class Client:
    def __init__(self, lease: dict) -> None:
        self.items = {
            role: {
                "id": node["instanceId"],
                "name": node["name"],
                "region": {"name": node["region"]},
                "status": "active",
                "tags": [
                    {"key": "kip-campaign", "value": lease["campaignId"]},
                    {"key": "kip-role", "value": role},
                ],
            }
            for role, node in lease["nodes"].items()
        }
        self.calls: list[str] = []

    def instances(self) -> list[dict]:
        return [item for item in self.items.values() if item["status"] != "terminated"]

    def terminate(self, instance_id: str) -> None:
        self.calls.append(instance_id)
        for item in self.items.values():
            if item["id"] == instance_id:
                item["status"] = "terminating"


def fixture(tmp_path: Path) -> tuple[Path, Client]:
    lease = new_lease("us-east-1")
    for role in ("control-speech", "small-1", "large"):
        lease["nodes"][role] = {
            "role": role,
            "phase": "active",
            "instanceId": f"task-{role}-id",
            "instanceType": "reviewed-type",
            "name": f"kip-owned-{role}",
            "region": "us-east-1",
            "createdAt": "2026-10-13T10:00:00Z",
            "endedAt": None,
            "priceCentsPerHour": 100,
        }
    path = tmp_path / "lease.json"
    _save_private(path, lease)
    report = tmp_path / "benchmarks/reports" / RUN_ID
    report.mkdir(parents=True)
    (report / "index.html").write_text("<html></html>", encoding="utf-8")
    (report / "summary.csv").write_text("header\n", encoding="utf-8")
    (report / "summary.json").write_text(
        json.dumps({"runId": RUN_ID, "status": "short-integration-trace-passed"}),
        encoding="utf-8",
    )
    return path, Client(lease)


def test_teardown_orders_exact_owned_nodes_and_never_retries(tmp_path: Path) -> None:
    lease_path, client = fixture(tmp_path)
    assert down(client, lease_path=lease_path, root=tmp_path, run_ids=[RUN_ID]) == {
        "control-speech": "terminating",
        "small-1": "terminating",
        "large": "terminating",
    }
    assert client.calls == ["task-large-id", "task-small-1-id", "task-control-speech-id"]
    client.items["large"]["status"] = "terminated"
    client.items["small-1"]["status"] = "terminated"
    client.items["control-speech"]["status"] = "terminated"
    assert set(down(client, lease_path=lease_path, root=tmp_path, run_ids=[RUN_ID]).values()) == {
        "terminated"
    }
    assert len(client.calls) == 3
    assert all(node["endedAt"] for node in load_lease(lease_path)["nodes"].values())


def test_teardown_refuses_missing_report_and_foreign_tags(tmp_path: Path) -> None:
    lease_path, client = fixture(tmp_path)
    with pytest.raises(LambdaError, match="report run ID"):
        down(client, lease_path=lease_path, root=tmp_path, run_ids=["../private"])
    client.items["large"]["tags"][0]["value"] = "foreign"
    with pytest.raises(LambdaError, match="ownership"):
        down(client, lease_path=lease_path, root=tmp_path, run_ids=[RUN_ID])
    assert client.calls == []
    assert load_lease(lease_path)["nodes"]["large"]["phase"] == "active"


def test_report_capture_requires_real_sanitized_files(tmp_path: Path) -> None:
    with pytest.raises(LambdaError, match="local"):
        captured_reports(tmp_path, [RUN_ID])
