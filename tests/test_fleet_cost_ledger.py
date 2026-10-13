"""Heterogeneous Vast prices are validated before OpenCost gets a CSV."""

from __future__ import annotations

import csv
import io
import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fleet.cost_ledger import (
    CSV_FIELDS,
    pricing_csv,
    save_snapshot,
    verified_snapshot,
    verify_pricing_files,
)
from fleet.vast import FleetError

PILOT = "kip-pilot-fixture"
OBSERVED = datetime(2026, 10, 13, 0, 0, tzinfo=UTC)


class Client:
    def __init__(self, *, credit: float = 32.97) -> None:
        self.credit = credit
        self.instances = [
            {
                "id": instance_id,
                "label": f"{PILOT}-{role}",
                "dph_total": rate,
                "actual_status": "running",
                "start_date": OBSERVED.timestamp() - 3600,
            }
            for role, instance_id, rate in (
                ("control", 1001, 0.68),
                ("gpu1", 1002, 0.62),
                ("gpu2", 1003, 0.74),
            )
        ]

    def get(self, path: str) -> dict[str, Any]:
        if path == "/users/current/":
            return {"credit": self.credit}
        if path == "/instances/":
            return {"instances": self.instances}
        raise AssertionError(path)


def leases(tmp_path: Path) -> tuple[Path, Path]:
    first = tmp_path / "first.json"
    fleet = tmp_path / "fleet.json"
    first.write_text(
        json.dumps({"instanceId": 1002, "label": f"{PILOT}-gpu1", "phase": "created"}),
        encoding="utf-8",
    )
    fleet.write_text(
        json.dumps(
            {
                "pilotId": PILOT,
                "nodes": {
                    "control": {
                        "instanceId": 1001,
                        "label": f"{PILOT}-control",
                        "phase": "created",
                    },
                    "gpu2": {
                        "instanceId": 1003,
                        "label": f"{PILOT}-gpu2",
                        "phase": "created",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return first, fleet


def test_exact_node_prices_without_double_charging_gpu(tmp_path: Path) -> None:
    client = Client()
    snapshot = verified_snapshot(client, *leases(tmp_path), observed_at=OBSERVED)  # type: ignore[arg-type]
    assert snapshot["activeHourlyUsd"] == "2.04"
    assert [node["node"] for node in snapshot["nodes"]] == [
        "kip-control",
        "kip-gpu-1",
        "kip-gpu-2",
    ]
    # OpenCost v1.121.1 passes field names to its decoder, so the file has no header.
    rows = [
        dict(zip(CSV_FIELDS, row, strict=True))
        for row in csv.reader(io.StringIO(pricing_csv(snapshot)))
    ]
    assert len(rows) == 3
    assert [row["AssetClass"] for row in rows] == ["node", "node", "node"]
    assert rows[0]["MarketPriceHourly"] == "0.68"
    assert rows[1]["MarketPriceHourly"] == "0.62"
    assert rows[2]["MarketPriceHourly"] == "0.74"
    assert {row["InstanceIDField"] for row in rows} == {"metadata.name"}


@pytest.mark.parametrize("problem", ["foreign", "missing", "bad-label", "stopped", "zero-rate"])
def test_refuses_unowned_or_unpriced_instance(tmp_path: Path, problem: str) -> None:
    client = Client()
    first, fleet = leases(tmp_path)
    if problem == "foreign":
        client.instances.append(
            {
                "id": 9999,
                "label": f"{PILOT}-foreign",
                "dph_total": 0.1,
                "actual_status": "running",
            }
        )
    elif problem == "missing":
        client.instances.pop()
    elif problem == "bad-label":
        client.instances[0]["label"] = f"{PILOT}-other"
    elif problem == "stopped":
        client.instances[0]["actual_status"] = "stopped"
    else:
        client.instances[0]["dph_total"] = 0
    with pytest.raises(FleetError):
        verified_snapshot(client, first, fleet, observed_at=OBSERVED)  # type: ignore[arg-type]


def test_refuses_budget_breach_and_future_start(tmp_path: Path) -> None:
    first, fleet = leases(tmp_path)
    client = Client(credit=5)
    with pytest.raises(FleetError, match="reserve"):
        verified_snapshot(client, first, fleet, observed_at=OBSERVED)  # type: ignore[arg-type]
    client = Client()
    client.instances[1]["start_date"] = OBSERVED.timestamp() + 1
    with pytest.raises(FleetError, match="start time"):
        verified_snapshot(client, first, fleet, observed_at=OBSERVED)  # type: ignore[arg-type]


def test_private_ledger_keeps_monotone_rate_observations(tmp_path: Path) -> None:
    client = Client()
    first, fleet = leases(tmp_path)
    ledger = tmp_path / "work" / "ledger.json"
    pricing = tmp_path / "work" / "pricing.csv"
    snapshot = verified_snapshot(client, first, fleet, observed_at=OBSERVED)  # type: ignore[arg-type]
    save_snapshot(snapshot, ledger, pricing)
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    assert stat.S_IMODE(pricing.stat().st_mode) == 0o600
    with pytest.raises(FleetError, match="advance"):
        save_snapshot(snapshot, ledger, pricing)
    client.instances[1]["dph_total"] = 0.66
    later = verified_snapshot(
        client,
        first,
        fleet,
        observed_at=datetime(2026, 10, 13, 1, 0, tzinfo=UTC),
    )  # type: ignore[arg-type]
    save_snapshot(later, ledger, pricing)
    saved = json.loads(ledger.read_text(encoding="utf-8"))
    assert len(saved["observations"]) == 2
    assert saved["observations"][0]["nodes"][1]["hourlyUsd"] == "0.62"
    assert saved["observations"][1]["nodes"][1]["hourlyUsd"] == "0.66"


def test_exported_pricing_requires_fresh_exact_csv(tmp_path: Path) -> None:
    client = Client()
    snapshot = verified_snapshot(client, *leases(tmp_path), observed_at=OBSERVED)  # type: ignore[arg-type]
    ledger = tmp_path / "ledger.json"
    pricing = tmp_path / "pricing.csv"
    save_snapshot(snapshot, ledger, pricing)
    assert verify_pricing_files(ledger, pricing, now=OBSERVED)["activeHourlyUsd"] == "2.04"
    with pytest.raises(FleetError, match="30 minutes"):
        verify_pricing_files(ledger, pricing, now=datetime(2026, 10, 13, 0, 31, tzinfo=UTC))
    pricing.write_text("changed\n", encoding="utf-8")
    with pytest.raises(FleetError, match="differs"):
        verify_pricing_files(ledger, pricing, now=OBSERVED)
