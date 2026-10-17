"""Private, exact-node Lambda price snapshot for OpenCost allocation.

The rental ledger is the infrastructure audit source. OpenCost attributes that
cost to workloads, but its per-tenant figures are estimates, not an invoice.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .cost_ledger import pricing_csv, save_snapshot
from .lambda_cloud import LambdaClient, LambdaError, parse_offers
from .lambda_inventory import validate_owned_nodes
from .lambda_rental import (
    BILLING_PATH,
    LEASE_PATH,
    Billing,
    load_billing,
    load_lease,
    matching_instance,
)

LEDGER_PATH = Path("fleet/.work/lambda-cost-ledger.json")
CSV_PATH = Path("fleet/.work/lambda-opencost-pricing.csv")
ACTIVE_ROLES = ("control-speech", "small-1", "large")


def build_snapshot(
    client: LambdaClient,
    lease: dict[str, Any],
    billing: Billing,
    nodes_json: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if billing.tax_percent != 0 or billing.storage_usd_per_hour != 0:
        raise LambdaError("Lambda CSV pricing requires zero separately billed tax/storage")
    if not billing.source.startswith("User-confirmed Lambda billing"):
        raise LambdaError("Lambda listed-rate billing needs explicit account provenance")
    active = {role for role, node in lease["nodes"].items() if node["phase"] == "active"}
    if active != set(ACTIVE_ROLES):
        raise LambdaError("Lambda cost snapshot requires the reviewed three-node east fleet")
    validated = validate_owned_nodes(nodes_json, lease, region=lease["region"])
    if set(validated["nodes"]) != active:
        raise LambdaError("Lambda Kubernetes inventory differs from active rental roles")
    offers = {offer.name: offer for offer in parse_offers(client.instance_types())}
    instances = client.instances()
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset().total_seconds() != 0:
        raise LambdaError("Lambda cost snapshot timestamp must be UTC")
    rows = []
    total = Decimal(0)
    for role in ACTIVE_ROLES:
        owned = lease["nodes"][role]
        instance = matching_instance(lease, role, instances)
        if instance is None or instance.get("status") != "active":
            raise LambdaError(f"owned Lambda {role} is not active")
        offer = offers.get(owned["instanceType"])
        if offer is None or offer.price_cents_per_hour != owned["priceCentsPerHour"]:
            raise LambdaError(f"Lambda {role} current listed rate differs from its lease")
        rate = Decimal(owned["priceCentsPerHour"]) / 100
        total += rate
        rows.append(
            {
                "node": validated["nodes"][role]["nodeName"],
                "role": role,
                "instanceId": owned["instanceId"],
                "startedUtc": owned["createdAt"],
                "hourlyUsd": str(rate),
                "physicalGpus": validated["nodes"][role]["allocatableGpus"],
                "source": billing.source,
            }
        )
    return {
        "schemaVersion": 1,
        "pilotId": lease["campaignId"],
        "observedAtUtc": now.isoformat().replace("+00:00", "Z"),
        "currency": "USD",
        "activeHourlyUsd": str(total),
        "billingSource": billing.source,
        "nodes": rows,
    }


def verify_files(
    ledger_path: Path = LEDGER_PATH,
    csv_path: Path = CSV_PATH,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    for path in (ledger_path, csv_path):
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise LambdaError("private Lambda pricing artifact is missing or not mode 0600")
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        csv_text = csv_path.read_text(encoding="utf-8")
        snapshot = ledger["observations"][-1]
        observed = datetime.fromisoformat(snapshot["observedAtUtc"].replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        raise LambdaError("private Lambda pricing snapshot is malformed") from error
    now = now or datetime.now(UTC)
    if now.tzinfo is None or observed.tzinfo is None:
        raise LambdaError("Lambda pricing snapshot is stale or not UTC")
    age = (now - observed).total_seconds()
    if observed.utcoffset().total_seconds() != 0 or not 0 <= age <= 1800:
        raise LambdaError("Lambda pricing snapshot is stale or not UTC")
    if (
        ledger.get("schemaVersion") != 1
        or ledger.get("pilotId") != snapshot.get("pilotId")
        or [row.get("role") for row in snapshot.get("nodes", [])] != list(ACTIVE_ROLES)
        or snapshot.get("currency") != "USD"
        or not str(snapshot.get("billingSource", "")).startswith("User-confirmed Lambda billing")
    ):
        raise LambdaError("Lambda pricing snapshot identity or node set drifted")
    rates = [Decimal(row["hourlyUsd"]) for row in snapshot["nodes"]]
    if any(not rate.is_finite() or rate <= 0 for rate in rates) or sum(rates) != Decimal(
        snapshot["activeHourlyUsd"]
    ):
        raise LambdaError("Lambda per-node prices do not sum to the active rental rate")
    if csv_text != pricing_csv(snapshot):
        raise LambdaError("Lambda OpenCost CSV differs from its private rate snapshot")
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Snapshot exact task-owned Lambda node prices")
    parser.add_argument("--lease", type=Path, default=LEASE_PATH)
    parser.add_argument("--billing", type=Path, default=BILLING_PATH)
    parser.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    parser.add_argument("--csv", type=Path, default=CSV_PATH)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.verify_only:
            snapshot = verify_files(args.ledger, args.csv)
        else:
            from .lambda_join import _address, _ssh

            client = LambdaClient()
            lease = load_lease(args.lease)
            if lease is None:
                raise LambdaError("task-owned Lambda lease is missing")
            control = matching_instance(lease, "control-speech", client.instances())
            if control is None or control.get("status") != "active":
                raise LambdaError("owned Lambda control plane is unavailable")
            nodes = json.loads(_ssh(_address(control, "ip"), "kubectl get nodes -o json"))
            snapshot = build_snapshot(client, lease, load_billing(args.billing), nodes)
            save_snapshot(snapshot, args.ledger, args.csv)
    except (LambdaError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Lambda price snapshot failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "campaignId": snapshot["pilotId"],
                "activeHourlyUsd": snapshot["activeHourlyUsd"],
                "billingSource": snapshot["billingSource"],
                "nodeHourlyUsd": {row["role"]: row["hourlyUsd"] for row in snapshot["nodes"]},
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
