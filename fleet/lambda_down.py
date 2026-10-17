"""Release only exact task-owned Lambda VMs after local report capture.

Termination is journaled before each API request. A rerun of a pending operation
only inspects cloud state; it never retries an uncertain terminate call.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from .lambda_cloud import LambdaClient, LambdaError
from .lambda_rental import LEASE_PATH, _save_private, load_lease, matching_instance

ROOT = Path(__file__).resolve().parents[1]
ORDER = ("large", "small-2", "small-1", "nvfp4", "control-speech", "h100-control")
REPORT_ID = re.compile(
    r"lambda-(?:three-class|nine-cohort|h100-pcie-control)-[0-9]{8}(?:-[0-9]{6})?\Z"
)


def captured_reports(root: Path, run_ids: list[str]) -> None:
    if not run_ids or len(set(run_ids)) != len(run_ids):
        raise LambdaError("at least one distinct locally captured report is required")
    for run_id in run_ids:
        if not REPORT_ID.fullmatch(run_id):
            raise LambdaError("report run ID is not reviewed")
        directory = root / "benchmarks/reports" / run_id
        if directory.is_symlink() or not directory.is_dir():
            raise LambdaError(f"sanitized report {run_id} is not local")
        for filename in ("index.html", "summary.json", "summary.csv"):
            item = directory / filename
            if item.is_symlink() or not item.is_file():
                raise LambdaError(f"sanitized report {run_id} is incomplete")
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        if summary.get("runId") != run_id or not isinstance(summary.get("status"), str):
            raise LambdaError(f"sanitized report {run_id} failed identity validation")


def down(
    client: LambdaClient,
    *,
    lease_path: Path = LEASE_PATH,
    root: Path = ROOT,
    run_ids: list[str],
) -> dict[str, str]:
    captured_reports(root, run_ids)
    lease = load_lease(lease_path)
    if lease is None or not lease["nodes"]:
        raise LambdaError("no task-owned Lambda lease exists")
    if set(lease["nodes"]) - set(ORDER):
        raise LambdaError("Lambda lease contains an unreviewed node role")
    instances = client.instances()
    # Complete ownership inspection before sending any destructive request.
    for role, node in lease["nodes"].items():
        instance = matching_instance(lease, role, instances)
        if node["phase"] == "launching":
            raise LambdaError(f"{role} launch outcome is uncertain; refuse teardown")
        if node["phase"] == "active" and (instance is None or instance.get("status") != "active"):
            raise LambdaError(f"active {role} is missing, unhealthy, or not task-owned")
        if (
            node["phase"] == "terminating"
            and instance is not None
            and instance.get("status") not in {"terminating", "terminated"}
        ):
            raise LambdaError(f"uncertain {role} termination has unexpected cloud state")
        if (
            node["phase"] == "terminated"
            and instance is not None
            and instance.get("status") != "terminated"
        ):
            raise LambdaError(f"terminated {role} unexpectedly reappeared")
    for role in ORDER:
        node = lease["nodes"].get(role)
        if node and node["phase"] == "active":
            node["phase"] = "terminating"
            _save_private(lease_path, lease)
            client.terminate(node["instanceId"])
    instances = client.instances()
    statuses = {}
    for role, node in lease["nodes"].items():
        instance = matching_instance(lease, role, instances)
        if node["phase"] == "terminating" and (
            instance is None or instance.get("status") == "terminated"
        ):
            node["phase"] = "terminated"
            node["endedAt"] = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            _save_private(lease_path, lease)
        statuses[role] = node["phase"]
    return statuses


def main() -> int:
    parser = argparse.ArgumentParser(description="Release exact task-owned Lambda campaign VMs")
    parser.add_argument("--report", action="append", required=True, dest="run_ids")
    args = parser.parse_args()
    if os.environ.get("CONFIRM_DESTROY") != "1":
        print("Lambda teardown requires CONFIRM_DESTROY=1", file=sys.stderr)
        return 2
    try:
        statuses = down(LambdaClient(), run_ids=args.run_ids)
    except (LambdaError, OSError, ValueError, TypeError, KeyError) as error:
        print(f"Lambda teardown failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"nodes": statuses}, sort_keys=True))
    return 0 if all(phase == "terminated" for phase in statuses.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
