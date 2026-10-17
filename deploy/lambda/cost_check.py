#!/usr/bin/env python3
"""Read-only exact-node OpenCost versus Lambda rental-rate checkpoint."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from .verify_pricing import verify
except ImportError:  # Direct target-local script invocation.
    from verify_pricing import verify


def _service_address() -> str:
    result = subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            os.environ.get("KIP_KUBECONFIG", "/etc/kubernetes/admin.conf"),
            "-n",
            "kip-observability",
            "get",
            "service",
            "kip-opencost",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    service = json.loads(result.stdout)
    spec = service["spec"]
    ip = spec["clusterIP"]
    if spec["type"] != "ClusterIP" or not isinstance(ip, str) or not ip.startswith("10."):
        raise ValueError("OpenCost is not on a reviewed private ClusterIP")
    if not any(port.get("port") == 9003 for port in spec.get("ports", [])):
        raise ValueError("OpenCost internal API port is missing")
    return f"http://{ip}:9003"


def _read(base: str, path: str, params: dict[str, str]) -> dict:
    url = base + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as response:
        if response.status != 200:
            raise ValueError("OpenCost API returned a non-200 response")
        payload = json.load(response)
    if payload.get("code") != 200:
        raise ValueError("OpenCost API reported an error")
    return payload


def reconcile(snapshot: dict, assets: dict, allocations: dict) -> dict[str, float]:
    expected = {row["node"]: float(row["hourlyUsd"]) for row in snapshot["nodes"]}
    records = assets.get("data")
    if not isinstance(records, dict):
        raise ValueError("OpenCost assets are missing")
    nodes = {
        item.get("properties", {}).get("name"): item
        for item in records.values()
        if isinstance(item, dict) and item.get("type") == "Node"
    }
    if set(nodes) != set(expected):
        raise ValueError("OpenCost asset node identities differ from Lambda lease")
    windows = allocations.get("data")
    if not isinstance(windows, list) or len(windows) != 1 or not isinstance(windows[0], dict):
        raise ValueError("OpenCost node allocation window is missing")
    if not set(expected).issubset(windows[0]):
        raise ValueError("OpenCost node allocations are incomplete")
    observed_rates: dict[str, float] = {}
    for name, price in expected.items():
        asset = nodes[name]
        minutes = asset.get("minutes")
        total = asset.get("totalCost")
        allocated = windows[0][name].get("totalCost")
        if (
            not isinstance(minutes, (int, float))
            or not isinstance(total, (int, float))
            or not isinstance(allocated, (int, float))
            or isinstance(minutes, bool)
            or isinstance(total, bool)
            or isinstance(allocated, bool)
            or not all(math.isfinite(value) for value in (minutes, total, allocated))
            or minutes < 1
            or total <= 0
            or allocated < 0
        ):
            raise ValueError("OpenCost node price or allocation is unavailable")
        actual = total * 60 / minutes
        if abs(actual - price) > max(0.01, price * 0.05):
            raise ValueError("OpenCost node hourly rate does not reconcile to Lambda billing")
        observed_rates[name] = round(actual, 4)
    return observed_rates


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Lambda node pricing in internal OpenCost")
    parser.add_argument("--ledger", type=Path, default=Path("fleet/.work/lambda-cost-ledger.json"))
    parser.add_argument("--csv", type=Path, default=Path("fleet/.work/lambda-opencost-pricing.csv"))
    args = parser.parse_args()
    try:
        snapshot = verify(args.ledger, args.csv)
        base = _service_address()
        assets = _read(base, "/assets", {"window": "15m"})
        allocations = _read(
            base,
            "/allocation/compute",
            {
                "window": "15m",
                "aggregate": "node",
                "includeIdle": "true",
                "shareIdle": "true",
                "idleByNode": "true",
            },
        )
        rates = reconcile(snapshot, assets, allocations)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"Lambda cost check failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "passed",
                "billingSource": snapshot["billingSource"],
                "nodeHourlyUsd": rates,
                "clusterHourlyUsd": snapshot["activeHourlyUsd"],
                "tenantAttribution": "not-an-invoice",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
