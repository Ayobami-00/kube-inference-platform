"""Read-only Lambda campaign preflight and owned-lease inspection."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .lambda_cloud import KEY_FILE, LambdaClient, LambdaError
from .lambda_inventory import validate_owned_nodes
from .lambda_models import load_models
from .lambda_rental import (
    BILLING_PATH,
    LEASE_PATH,
    accrued_cost,
    load_billing,
    load_lease,
    matching_instance,
    read_only_plan,
)
from .vast import FleetError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lambda multi-tenant campaign safety preflight")
    parser.add_argument("action", choices=("contract", "plan", "check"))
    parser.add_argument("--key-file", type=Path, default=KEY_FILE)
    parser.add_argument("--billing-file", type=Path, default=BILLING_PATH)
    parser.add_argument("--lease", type=Path, default=LEASE_PATH)
    parser.add_argument(
        "--cluster", action="store_true", help="also validate owned Kubernetes nodes"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "contract":
            from bench.lambda_campaign import load_campaign

            from .compat import LOCK_PATH, load_compatibility_lock

            load_compatibility_lock()
            campaign = load_campaign()
            models = load_models()
            result = {
                "status": "passed",
                "campaignSha256": campaign["sha256"],
                "workloadSha256": campaign["workloadSha256"],
                "modelsSha256": models["sha256"],
                "compatibilitySha256": hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest(),
                "targetHardwareValidated": False,
            }
            print(json.dumps(result, sort_keys=True, indent=2))
            return 0
        client = LambdaClient(args.key_file)
        billing = load_billing(args.billing_file) if args.billing_file.exists() else None
        if args.action == "plan":
            result = read_only_plan(client, billing, lease_path=args.lease)
        else:
            lease = load_lease(args.lease)
            if lease is None:
                raise LambdaError("no task-owned Lambda lease exists")
            instances = client.instances()
            nodes = {}
            for role, node in lease["nodes"].items():
                instance = matching_instance(lease, role, instances)
                if node["phase"] == "terminated":
                    healthy = instance is None or instance.get("status") == "terminated"
                else:
                    healthy = instance is not None and instance.get("status") == "active"
                nodes[role] = {
                    "phase": node["phase"],
                    "instanceType": node["instanceType"],
                    "status": instance.get("status") if instance else "not-listed",
                    "ownershipMatches": (
                        instance is not None if node["phase"] != "terminated" else True
                    ),
                    "healthy": healthy,
                }
            result = {
                "status": (
                    "passed" if nodes and all(n["healthy"] for n in nodes.values()) else "blocked"
                ),
                "campaignId": lease["campaignId"],
                "region": lease["region"],
                "nodes": nodes,
                "accruedEstimateUsd": str(accrued_cost(lease, billing)) if billing else None,
                "billingVerified": billing is not None,
                "note": "This checks cloud lease state, not Kubernetes or model health.",
            }
            if args.cluster:
                from .lambda_join import _address, _ssh

                control = matching_instance(lease, "control-speech", instances)
                if control is None or control.get("status") != "active":
                    raise LambdaError("owned east control plane is unavailable")
                try:
                    nodes_json = json.loads(
                        _ssh(_address(control, "ip"), "kubectl get nodes -o json")
                    )
                except ValueError as error:
                    raise LambdaError("Kubernetes node JSON is malformed") from error
                result["kubernetes"] = validate_owned_nodes(
                    nodes_json, lease, region=lease["region"]
                )
                result["note"] = (
                    "Cloud leases and Kubernetes nodes passed; model health is separate."
                )
    except (LambdaError, FleetError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["status"] in {"ready", "passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
