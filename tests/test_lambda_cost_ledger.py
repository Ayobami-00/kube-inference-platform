"""The Lambda price bridge uses exact owned nodes and rejects stale/drifted rates."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from fleet.cost_ledger import save_snapshot
from fleet.lambda_cloud import LambdaError
from fleet.lambda_cost_ledger import build_snapshot, verify_files
from fleet.lambda_rental import Billing, new_lease

reconcile = importlib.import_module("deploy.lambda.cost_check").reconcile

ROLES = ("control-speech", "small-1", "large")
TYPES = ("gpu_1x_a10", "gpu_1x_a100_sxm4", "gpu_8x_a100_80gb_sxm4")
PRICES = (129, 199, 2232)
GPUS = (1, 1, 8)
ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, lease: dict) -> None:
        self.lease = lease
        self.rate_drift = False

    def instance_types(self) -> dict:
        return {
            kind: {
                "instance_type": {
                    "name": kind,
                    "gpu_description": kind,
                    "price_cents_per_hour": cents
                    + (1 if self.rate_drift and kind == TYPES[2] else 0),
                    "specs": {"gpus": gpus, "vcpus": 120, "memory_gib": 900, "storage_gib": 4096},
                    "architecture": "x86_64",
                },
                "regions_with_capacity_available": [],
            }
            for kind, cents, gpus in zip(TYPES, PRICES, GPUS, strict=True)
        }

    def instances(self) -> list[dict]:
        return [
            {
                "id": node["instanceId"],
                "name": node["name"],
                "region": {"name": "us-east-1"},
                "status": "active",
                "tags": [
                    {"key": "kip-campaign", "value": self.lease["campaignId"]},
                    {"key": "kip-role", "value": role},
                ],
            }
            for role, node in self.lease["nodes"].items()
        ]


def fixture() -> tuple[Client, dict, dict, Billing]:
    lease = new_lease("us-east-1")
    items = []
    for role, kind, cents, gpus in zip(ROLES, TYPES, PRICES, GPUS, strict=True):
        identity = f"task-owned-{role}-12345678"
        name = f"kip-lambda-test-{role}"
        lease["nodes"][role] = {
            "role": role,
            "phase": "active",
            "name": name,
            "instanceId": identity,
            "instanceType": kind,
            "region": "us-east-1",
            "priceCentsPerHour": cents,
            "createdAt": "2026-10-13T12:00:00Z",
            "endedAt": None,
            "expectedPhysicalGpus": gpus,
        }
        items.append(
            {
                "metadata": {
                    "name": name,
                    "labels": {"kube-inference-platform.io/lambda-role": role},
                    "annotations": {"kube-inference-platform.io/lambda-instance-id": identity},
                },
                "spec": {},
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "capacity": {"nvidia.com/gpu": str(gpus)},
                    "allocatable": {"nvidia.com/gpu": str(gpus)},
                },
            }
        )
    billing = Billing(
        Decimal(0),
        Decimal(0),
        "User-confirmed Lambda billing on 2026-10-13: listed instance rates only",
    )
    return Client(lease), lease, {"items": items}, billing


def test_exact_node_prices_and_private_round_trip(tmp_path: Path) -> None:
    client, lease, nodes, billing = fixture()
    now = datetime(2026, 10, 13, 12, 5, tzinfo=UTC)
    snapshot = build_snapshot(client, lease, billing, nodes, now=now)
    assert snapshot["activeHourlyUsd"] == "25.60"
    assert [row["hourlyUsd"] for row in snapshot["nodes"]] == ["1.29", "1.99", "22.32"]
    ledger = tmp_path / "ledger.json"
    csv_path = tmp_path / "prices.csv"
    save_snapshot(snapshot, ledger, csv_path)
    assert verify_files(ledger, csv_path, now=now + timedelta(minutes=1)) == snapshot
    assert csv_path.read_text(encoding="utf-8").count("kip-lambda-test-") == 3
    assert json.loads(ledger.read_text(encoding="utf-8"))["pilotId"] == lease["campaignId"]


def test_rate_and_kubernetes_ownership_drift_block_snapshot() -> None:
    client, lease, nodes, billing = fixture()
    client.rate_drift = True
    with pytest.raises(LambdaError, match="listed rate differs"):
        build_snapshot(client, lease, billing, nodes)
    client.rate_drift = False
    nodes["items"][0]["metadata"]["annotations"][
        "kube-inference-platform.io/lambda-instance-id"
    ] = "foreign-instance"
    with pytest.raises(LambdaError, match="ownership drift"):
        build_snapshot(client, lease, billing, nodes)


def test_unpriced_extras_and_stale_snapshots_fail(tmp_path: Path) -> None:
    client, lease, nodes, billing = fixture()
    with pytest.raises(LambdaError, match="zero separately billed"):
        build_snapshot(client, lease, Billing(Decimal(5), Decimal(0), billing.source), nodes)
    now = datetime(2026, 10, 13, 12, 5, tzinfo=UTC)
    snapshot = build_snapshot(client, lease, billing, nodes, now=now)
    ledger = tmp_path / "ledger.json"
    csv_path = tmp_path / "prices.csv"
    save_snapshot(snapshot, ledger, csv_path)
    with pytest.raises(LambdaError, match="stale"):
        verify_files(ledger, csv_path, now=now + timedelta(minutes=31))
    with pytest.raises(LambdaError, match="not UTC"):
        verify_files(ledger, csv_path, now=now.replace(tzinfo=None))
    csv_path.write_text("wrong")
    with pytest.raises(LambdaError, match="differs"):
        verify_files(ledger, csv_path, now=now + timedelta(minutes=1))


def test_lambda_opencost_values_keep_api_private_and_place_on_control() -> None:
    values = yaml.safe_load((ROOT / "deploy/lambda/opencost-values.yaml").read_text())
    assert values["kip"]["pricingState"] == "fleet-csv"
    assert values["opencost"]["nodeSelector"] == {
        "kube-inference-platform.io/lambda-role": "control-speech"
    }
    assert values["opencost"]["exporter"]["defaultClusterId"] == "kip-lambda"
    assert values["opencost"]["exporter"]["extraEnv"]["USE_CSV_PROVIDER"] == "true"


def test_remote_standalone_price_verifier_accepts_only_matching_private_files(
    tmp_path: Path,
) -> None:
    client, lease, nodes, billing = fixture()
    snapshot = build_snapshot(client, lease, billing, nodes)
    # The remote verifier requires the canonical task node names, not a
    # similarly labelled but different Kubernetes object.
    for row in snapshot["nodes"]:
        row["node"] = f"kip-lambda-{lease['campaignId'][:8]}-{row['role']}"
    ledger = tmp_path / "ledger.json"
    csv_path = tmp_path / "prices.csv"
    save_snapshot(snapshot, ledger, csv_path)
    command = [
        sys.executable,
        str(ROOT / "deploy/lambda/verify_pricing.py"),
        "--ledger",
        str(ledger),
        "--csv",
        str(csv_path),
    ]
    assert subprocess.run(command, capture_output=True, check=False).returncode == 0
    csv_path.write_text("tampered", encoding="utf-8")
    assert subprocess.run(command, capture_output=True, check=False).returncode == 1


def test_opencost_assets_reconcile_to_each_listed_rate() -> None:
    client, lease, nodes, billing = fixture()
    snapshot = build_snapshot(client, lease, billing, nodes)
    assets = {
        "data": {
            row["node"]: {
                "type": "Node",
                "properties": {"name": row["node"]},
                "minutes": 5,
                "totalCost": float(row["hourlyUsd"]) * 5 / 60,
            }
            for row in snapshot["nodes"]
        }
    }
    allocation = {"data": [{row["node"]: {"totalCost": 0.1} for row in snapshot["nodes"]}]}
    assert len(reconcile(snapshot, assets, allocation)) == 3
    first = snapshot["nodes"][0]["node"]
    assets["data"][first]["totalCost"] *= 2
    with pytest.raises(ValueError, match="does not reconcile"):
        reconcile(snapshot, assets, allocation)
