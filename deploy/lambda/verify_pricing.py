#!/usr/bin/env python3
"""Verify copied Lambda price evidence without cloud credentials on the node."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import stat
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROLES = ("control-speech", "small-1", "large")
GPU_COUNTS = (1, 1, 8)
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


def _private(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("price evidence must be regular, non-symlink mode-0600 files")
    return path.read_text(encoding="utf-8")


def _csv(snapshot: dict) -> str:
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


def verify(ledger_path: Path, csv_path: Path, *, now: datetime | None = None) -> dict:
    try:
        ledger = json.loads(_private(ledger_path))
        actual_csv = _private(csv_path)
        snapshot = ledger["observations"][-1]
        observed = datetime.fromisoformat(snapshot["observedAtUtc"].replace("Z", "+00:00"))
        now = now or datetime.now(UTC)
        age = (now - observed).total_seconds()
        if (
            not math.isfinite(age)
            or not 0 <= age <= 1800
            or observed.utcoffset().total_seconds() != 0
        ):
            raise ValueError("price evidence is stale or not UTC")
        campaign = snapshot["pilotId"]
        if (
            ledger["schemaVersion"] != 1
            or ledger["pilotId"] != campaign
            or not re.fullmatch(r"[0-9a-f]{32}", campaign)
            or snapshot["schemaVersion"] != 1
            or snapshot["currency"] != "USD"
            or not snapshot["billingSource"].startswith("User-confirmed Lambda billing")
        ):
            raise ValueError("price evidence identity or billing source is invalid")
        rows = snapshot["nodes"]
        if [row["role"] for row in rows] != list(ROLES):
            raise ValueError("price evidence has an unexpected node set")
        rates = []
        for row, role, gpu in zip(rows, ROLES, GPU_COUNTS, strict=True):
            if (
                row["node"] != f"kip-lambda-{campaign[:8]}-{role}"
                or row["physicalGpus"] != gpu
                or not isinstance(row["instanceId"], str)
                or not row["instanceId"]
                or row["source"] != snapshot["billingSource"]
            ):
                raise ValueError("price evidence node identity or GPU count drifted")
            rate = Decimal(row["hourlyUsd"])
            if not rate.is_finite() or rate <= 0:
                raise ValueError("price evidence has an invalid node rate")
            rates.append(rate)
        if sum(rates) != Decimal(snapshot["activeHourlyUsd"]):
            raise ValueError("price evidence node rates do not sum to fleet rate")
        if _csv(snapshot) != actual_csv:
            raise ValueError("OpenCost CSV differs from exact Lambda price evidence")
    except (OSError, KeyError, TypeError, IndexError, InvalidOperation, AttributeError) as error:
        raise ValueError("price evidence is malformed") from error
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify task-owned Lambda node price files")
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--csv", required=True, type=Path)
    args = parser.parse_args()
    try:
        snapshot = verify(args.ledger, args.csv)
    except ValueError as error:
        print(f"Lambda pricing verification failed: {error}", file=sys.stderr)
        return 1
    print(f"PASS: {len(snapshot['nodes'])} task-owned Lambda node prices verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
