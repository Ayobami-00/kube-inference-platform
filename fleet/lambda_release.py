"""Ownership-safe release of the completed, isolated Lambda H100 control VM."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .lambda_cloud import LambdaClient, LambdaError
from .lambda_rental import LEASE_PATH, _save_private, load_lease, matching_instance

REPORT = Path("benchmarks/reports/lambda-h100-pcie-control-20261013")


def require_report(path: Path = REPORT) -> None:
    if path.is_symlink() or not path.is_dir():
        raise LambdaError("H100 control report is not safely stored locally")
    required = ("index.html", "summary.json", "summary.csv")
    if any(not (path / name).is_file() or (path / name).is_symlink() for name in required):
        raise LambdaError("H100 control report is incomplete")
    summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
    if (
        summary.get("runId") != "lambda-h100-pcie-control-20261013"
        or summary.get("status") != "exploratory-hardware-control"
        or summary.get("model") != "qwen3-8b"
        or len(summary.get("rows", [])) != 2
    ):
        raise LambdaError("H100 control report failed its checkpoint gate")


def release(client: LambdaClient, lease_path: Path = LEASE_PATH, report: Path = REPORT) -> str:
    require_report(report)
    lease = load_lease(lease_path)
    if lease is None or "h100-control" not in lease["nodes"]:
        raise LambdaError("no task-owned H100 control lease exists")
    node = lease["nodes"]["h100-control"]
    if node["region"] == lease["region"] or node["instanceType"] != "gpu_1x_h100_pcie":
        raise LambdaError("H100 control lease no longer describes the isolated west VM")
    if node["phase"] == "terminated":
        if matching_instance(lease, "h100-control", client.instances()) is not None:
            raise LambdaError("terminated H100 control lease still appears in Lambda inventory")
        return "already-terminated"
    if node["phase"] not in {"active", "terminating"}:
        raise LambdaError("H100 control lease is not releasable")
    instances = client.instances()
    instance = matching_instance(lease, "h100-control", instances)
    if instance is None:
        if node["phase"] != "terminating":
            raise LambdaError("active H100 control lease disappeared before termination")
    elif instance.get("status") not in {"active", "terminating", "terminated"}:
        raise LambdaError("H100 control instance is in an unexpected state")
    if node["phase"] == "active":
        if instance is None or instance.get("status") != "active":
            raise LambdaError("H100 control instance is not active; refuse an uncertain release")
        node["phase"] = "terminating"
        _save_private(lease_path, lease)
        client.terminate(node["instanceId"])
    for _ in range(18):
        instance = matching_instance(lease, "h100-control", client.instances())
        if instance is None or instance.get("status") == "terminated":
            node["phase"] = "terminated"
            node["endedAt"] = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            _save_private(lease_path, lease)
            return "terminated"
        time.sleep(5)
    raise LambdaError("termination outcome is pending; lease remains in terminating phase")


def main() -> int:
    parser = argparse.ArgumentParser(description="Release the completed task-owned H100 control VM")
    parser.add_argument("--confirm", required=True, choices=("h100-control",))
    args = parser.parse_args()
    try:
        print(json.dumps({"role": args.confirm, "status": release(LambdaClient())}))
    except (LambdaError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"H100 release failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
