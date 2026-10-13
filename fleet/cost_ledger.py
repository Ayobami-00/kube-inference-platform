"""Snapshot verified Vast rates for heterogeneous-node OpenCost pricing.

The ignored ledger is the rental audit source. OpenCost uses the derived CSV
for workload allocation; that allocation is never presented as a Vast invoice.
"""

# Ubuntu 22.04 ships Python 3.10; datetime.UTC is not available there.
# ruff: noqa: UP017

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .vast import FleetError, VastReadClient, configured_key_file

EXPECTED_ROLES = ("control", "gpu1", "gpu2")
NODE_NAMES = {"control": "kip-control", "gpu1": "kip-gpu-1", "gpu2": "kip-gpu-2"}
CSV_FIELDS = (
    "EndTimestamp",
    "InstanceID",
    "Region",
    "AssetClass",
    "InstanceIDField",
    "InstanceType",
    "MarketPriceHourly",
    "Version",
)


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise FleetError(f"invalid Vast {field}") from error
    if not parsed.is_finite() or parsed <= 0:
        raise FleetError(f"invalid Vast {field}")
    return parsed


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _lease_expectations(
    first_path: Path, fleet_path: Path
) -> tuple[str, dict[str, dict[str, Any]]]:
    try:
        first = json.loads(first_path.read_text(encoding="utf-8"))
        fleet = json.loads(fleet_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FleetError("owned Vast lease journals are missing or malformed") from error
    pilot = fleet.get("pilotId")
    if not isinstance(pilot, str) or not pilot.startswith("kip-pilot-"):
        raise FleetError("fleet lease has no task-owned pilot identity")
    expected = {
        "gpu1": first,
        "control": fleet.get("nodes", {}).get("control"),
        "gpu2": fleet.get("nodes", {}).get("gpu2"),
    }
    if any(not isinstance(value, dict) for value in expected.values()):
        raise FleetError("all three task-owned lease records are required")
    for role, lease in expected.items():
        if lease.get("phase") != "created":
            raise FleetError(f"owned {role} lease is not active")
        if lease.get("label") != f"{pilot}-{role}":
            raise FleetError(f"owned {role} lease label differs from the pilot identity")
        if isinstance(lease.get("instanceId"), bool) or not isinstance(
            lease.get("instanceId"), int
        ):
            raise FleetError(f"owned {role} lease has no valid instance ID")
    return pilot, expected


def verified_snapshot(
    client: VastReadClient,
    first_path: Path,
    fleet_path: Path,
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    pilot, expected = _lease_expectations(first_path, fleet_path)
    observed_at = observed_at or datetime.now(tz=timezone.utc)
    if observed_at.tzinfo is None:
        raise FleetError("cost observation timestamp must be timezone-aware")
    response = client.get("/instances/")
    if not isinstance(response, dict) or not isinstance(response.get("instances"), list):
        raise FleetError("Vast instance listing is malformed")
    owned = [
        item
        for item in response["instances"]
        if isinstance(item, dict) and str(item.get("label", "")).startswith(pilot)
    ]
    if len(owned) != 3:
        raise FleetError("Vast does not show exactly three task-owned pilot instances")
    by_id = {item.get("id"): item for item in owned}
    if len(by_id) != 3 or set(by_id) != {lease["instanceId"] for lease in expected.values()}:
        raise FleetError("Vast instance IDs differ from the exact owned lease journals")
    nodes: list[dict[str, Any]] = []
    active_rate = Decimal(0)
    for role in EXPECTED_ROLES:
        lease = expected[role]
        instance = by_id[lease["instanceId"]]
        if instance.get("label") != lease["label"] or instance.get("actual_status") != "running":
            raise FleetError(f"owned {role} Vast instance is not running with its exact label")
        rate = _decimal(instance.get("dph_total"), f"{role} billed hourly rate")
        try:
            started = float(instance["start_date"])
        except (KeyError, TypeError, ValueError) as error:
            raise FleetError(f"owned {role} instance has no valid rental start time") from error
        if started <= 0 or started > observed_at.timestamp():
            raise FleetError(f"owned {role} rental start time is outside the observation")
        active_rate += rate
        nodes.append(
            {
                "node": NODE_NAMES[role],
                "role": role,
                "instanceId": lease["instanceId"],
                "startedUtc": _utc(started),
                "hourlyUsd": str(rate),
                "source": "Vast /instances dph_total",
            }
        )
    account = client.get("/users/current/")
    if not isinstance(account, dict):
        raise FleetError("Vast account response is malformed")
    credit = _decimal(account.get("credit", account.get("balance")), "account credit")
    if active_rate > Decimal("4") or credit <= Decimal("5"):
        raise FleetError("the fleet breaches its $4/hour cap or $5 remaining-credit reserve")
    return {
        "schemaVersion": 1,
        "pilotId": pilot,
        "observedAtUtc": observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "currency": "USD",
        "activeHourlyUsd": str(active_rate),
        "creditUsd": str(credit),
        "nodes": nodes,
    }


def pricing_csv(snapshot: dict[str, Any]) -> str:
    """Map exact whole-node rates; OpenCost splits CPU/RAM/GPU from each rate."""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\n")
    for node in snapshot["nodes"]:
        row = dict.fromkeys(CSV_FIELDS, "")
        row.update(
            {
                "InstanceID": node["node"],
                "AssetClass": "node",
                "InstanceIDField": "metadata.name",
                "MarketPriceHourly": node["hourlyUsd"],
                "Version": snapshot["pilotId"],
            }
        )
        writer.writerow(row)
    return output.getvalue()


def _atomic_private_write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = handle.name
            os.fchmod(handle.fileno(), 0o600)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def save_snapshot(snapshot: dict[str, Any], ledger_path: Path, csv_path: Path) -> None:
    if ledger_path.exists():
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise FleetError("existing private cost ledger is malformed") from error
        if ledger.get("schemaVersion") != 1 or ledger.get("pilotId") != snapshot["pilotId"]:
            raise FleetError("existing private cost ledger belongs to another pilot")
        observations = ledger.get("observations")
        if not isinstance(observations, list) or not observations:
            raise FleetError("existing private cost ledger has no observations")
        if snapshot["observedAtUtc"] <= observations[-1]["observedAtUtc"]:
            raise FleetError("cost observations must advance in UTC")
        observations.append(snapshot)
    else:
        ledger = {"schemaVersion": 1, "pilotId": snapshot["pilotId"], "observations": [snapshot]}
    _atomic_private_write(ledger_path, json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    _atomic_private_write(csv_path, pricing_csv(snapshot))


def verify_pricing_files(
    ledger_path: Path, csv_path: Path, *, now: datetime | None = None
) -> dict[str, Any]:
    """Verify an exported snapshot without needing a Vast credential on a VM."""
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        observed_csv = csv_path.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as error:
        raise FleetError("private fleet pricing snapshot is missing or malformed") from error
    if ledger.get("schemaVersion") != 1 or not isinstance(ledger.get("observations"), list):
        raise FleetError("private fleet pricing ledger has an invalid schema")
    if not ledger["observations"]:
        raise FleetError("private fleet pricing ledger has no observations")
    snapshot = ledger["observations"][-1]
    if not isinstance(snapshot, dict) or snapshot.get("pilotId") != ledger.get("pilotId"):
        raise FleetError("private fleet pricing identity is inconsistent")
    now = now or datetime.now(tz=timezone.utc)
    try:
        observed = datetime.fromisoformat(snapshot["observedAtUtc"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError) as error:
        raise FleetError("private fleet pricing timestamp is invalid") from error
    age = (now - observed).total_seconds()
    if age < 0 or age > 1800:
        raise FleetError("private Vast rate observation is more than 30 minutes old")
    nodes = snapshot.get("nodes")
    if not isinstance(nodes, list) or [node.get("node") for node in nodes] != [
        "kip-control",
        "kip-gpu-1",
        "kip-gpu-2",
    ]:
        raise FleetError("private fleet pricing node set is incomplete or reordered")
    rates = [_decimal(node.get("hourlyUsd"), "node hourly rate") for node in nodes]
    if sum(rates) != _decimal(snapshot.get("activeHourlyUsd"), "active hourly rate"):
        raise FleetError("private fleet node rates do not sum to the active rental rate")
    if sum(rates) > Decimal("4") or _decimal(snapshot.get("creditUsd"), "credit") <= 5:
        raise FleetError("private fleet pricing violates the spending limit or reserve")
    if observed_csv != pricing_csv(snapshot):
        raise FleetError("OpenCost CSV differs from the verified Vast rate snapshot")
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Snapshot task-owned Vast rates for fleet pricing")
    parser.add_argument(
        "--first-lease", type=Path, default=Path("fleet/.work/first-worker-lease.json")
    )
    parser.add_argument("--fleet-lease", type=Path, default=Path("fleet/.work/fleet-lease.json"))
    parser.add_argument("--ledger", type=Path, default=Path("fleet/.work/cost-ledger.json"))
    parser.add_argument("--csv", type=Path, default=Path("fleet/.work/opencost-pricing.csv"))
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.verify_only:
            snapshot = verify_pricing_files(args.ledger, args.csv)
        else:
            snapshot = verified_snapshot(
                VastReadClient(configured_key_file()), args.first_lease, args.fleet_lease
            )
            save_snapshot(snapshot, args.ledger, args.csv)
    except FleetError as error:
        parser.exit(1, f"error: {error}\n")
    print(
        json.dumps(
            {
                "pilotId": snapshot["pilotId"],
                "observedAtUtc": snapshot["observedAtUtc"],
                "activeHourlyUsd": snapshot["activeHourlyUsd"],
                "creditUsd": snapshot["creditUsd"],
                "nodeHourlyUsd": {node["node"]: node["hourlyUsd"] for node in snapshot["nodes"]},
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
