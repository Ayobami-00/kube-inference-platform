"""Read-only reconciliation of OpenCost with exact, task-owned Vast node rates."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

from .cost_ledger import verify_pricing_files
from .vast import FleetError


def _service_ip(name: str) -> str:
    result = subprocess.run(
        [
            "kubectl",
            "-n",
            "kip-observability",
            "get",
            "service",
            name,
            "-o",
            "jsonpath={.spec.clusterIP}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value or value == "None":
        raise FleetError(f"owned observability Service {name} is unavailable")
    return value


def _get_json(base: str, path: str, query: dict[str, str]) -> dict[str, Any]:
    with urlopen(f"{base}{path}?{urlencode(query)}", timeout=30) as response:  # noqa: S310
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise FleetError(f"{path} returned a non-object response")
    return payload


def _near(actual: Decimal, expected: Decimal, label: str, tolerance: Decimal) -> None:
    if expected <= 0 or actual <= 0:
        raise FleetError(f"{label} has missing or non-positive cost")
    if abs(actual - expected) > expected * tolerance:
        raise FleetError(f"{label} does not reconcile with the verified Vast rental rate")


def reconcile(
    snapshot: dict[str, Any],
    *,
    prometheus: str,
    opencost: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Require hourly gauges, exact-window assets, and idle-inclusive allocation."""
    duration = end - start
    if (
        start.tzinfo is None
        or end.tzinfo is None
        or start.utcoffset() != timedelta(0)
        or end.utcoffset() != timedelta(0)
        or duration < timedelta(minutes=1)
        or duration.total_seconds() % 60 != 0
        or start.second
        or start.microsecond
        or end.second
        or end.microsecond
    ):
        raise FleetError("cost-check requires a positive whole-minute UTC window")
    hours = Decimal(str(duration.total_seconds())) / Decimal(3600)
    expected = {node["node"]: Decimal(node["hourlyUsd"]) for node in snapshot["nodes"]}
    prom = _get_json(
        prometheus,
        "/api/v1/query",
        {"query": 'max by(node) (node_total_hourly_cost{job="kip-opencost"})'},
    )
    if prom.get("status") != "success":
        raise FleetError("Prometheus did not return current OpenCost node prices")
    rates = {
        item["metric"]["node"]: Decimal(item["value"][1])
        for item in prom.get("data", {}).get("result", [])
    }
    if set(rates) != set(expected):
        raise FleetError("OpenCost hourly metrics do not cover exactly the owned fleet")
    for node, rate in rates.items():
        _near(rate, expected[node], f"{node} hourly gauge", Decimal("0.001"))

    window = f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')},{end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    assets_response = _get_json(opencost, "/assets", {"window": window, "aggregate": "node"})
    if assets_response.get("code") != 200 or not isinstance(assets_response.get("data"), dict):
        raise FleetError("OpenCost did not return node assets for the exact minute")
    assets = {
        key.rsplit("/", 1)[-1]: Decimal(str(value["totalCost"]))
        for key, value in assets_response["data"].items()
        if isinstance(value, dict) and value.get("type") == "Node"
    }
    if set(assets) != set(expected):
        raise FleetError("OpenCost assets do not cover exactly the owned fleet")
    for node, cost in assets.items():
        _near(cost, expected[node] * hours, f"{node} asset window", Decimal("0.05"))

    allocation_response = _get_json(
        opencost,
        "/allocation/compute",
        {
            "window": window,
            "aggregate": "node",
            "includeIdle": "true",
            "shareIdle": "true",
            "idleByNode": "true",
            "accumulate": "true",
        },
    )
    data = allocation_response.get("data")
    if allocation_response.get("code") != 200 or not isinstance(data, list) or not data:
        raise FleetError("OpenCost did not return idle-inclusive allocation")
    if not isinstance(data[0], dict):
        raise FleetError("OpenCost allocation has an invalid node map")
    allocations = {
        node: Decimal(str(value["totalCost"]))
        for node, value in data[0].items()
        if node in expected and isinstance(value, dict)
    }
    if set(allocations) != set(expected):
        raise FleetError("OpenCost allocation does not cover exactly the owned fleet")
    for node, cost in allocations.items():
        _near(cost, assets[node], f"{node} allocation window", Decimal("0.05"))
    audit_total = sum(expected.values(), Decimal(0)) * hours
    allocated_total = sum(allocations.values(), Decimal(0))
    _near(allocated_total, audit_total, "fleet allocated minute", Decimal("0.05"))
    return {
        "window": window,
        "durationMinutes": int(duration.total_seconds() / 60),
        "auditUsd": round(float(audit_total), 6),
        "allocatedUsd": round(float(allocated_total), 6),
        "nodes": {
            node: {
                "vastHourlyUsd": str(expected[node]),
                "opencostHourlyUsd": round(float(rates[node]), 6),
                "assetWindowUsd": round(float(assets[node]), 6),
                "allocatedWindowUsd": round(float(allocations[node]), 6),
            }
            for node in sorted(expected)
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile owned OpenCost against Vast rates")
    parser.add_argument("--ledger", type=Path, default=Path("fleet/.work/cost-ledger.json"))
    parser.add_argument("--csv", type=Path, default=Path("fleet/.work/opencost-pricing.csv"))
    args = parser.parse_args()
    try:
        snapshot = verify_pricing_files(args.ledger, args.csv)
        now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)  # noqa: UP017
        end = now - timedelta(minutes=1)
        result = reconcile(
            snapshot,
            prometheus=f"http://{_service_ip('kip-monitoring-prometheus')}:9090",
            opencost=f"http://{_service_ip('kip-opencost')}:9003",
            start=end - timedelta(minutes=1),
            end=end,
        )
    except (FleetError, OSError, ValueError, KeyError, IndexError, json.JSONDecodeError) as error:
        print(f"fleet cost check failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
