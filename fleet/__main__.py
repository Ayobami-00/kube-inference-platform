"""Run the read-only fleet preflight."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .check import check_fleet
from .compat import load_compatibility_lock
from .mesh_plan import owned_peer_plans
from .rental import (
    VastRentalClient,
    _load_lease,
    release_complete_fleet,
    release_first_worker,
    release_fleet_control,
    release_second_worker,
    rent_first_worker,
    rent_next_fleet_node,
)
from .stage_validation import validate_and_record_control
from .vast import FleetError, VastReadClient, configured_key_file, read_only_plan


def main() -> int:
    parser = argparse.ArgumentParser(description="Vast.ai multi-node pilot preparation")
    parser.add_argument(
        "action",
        choices=(
            "plan",
            "compat-check",
            "check",
            "rent-first",
            "release-first",
            "rent-control",
            "certify-control",
            "rent-gpu2",
            "release-control",
            "release-gpu2",
            "release-fleet",
            "mesh-plan",
        ),
    )
    parser.add_argument(
        "--allow-mixed",
        action="store_true",
        help="allow heterogeneous/cross-region candidates for the read-only plan",
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=Path("fleet/.work/pilot.json"),
        help="owned pilot inventory",
    )
    parser.add_argument(
        "--lease",
        type=Path,
        default=Path("fleet/.work/first-worker-lease.json"),
        help="ignored, task-owned first-worker lease journal",
    )
    parser.add_argument(
        "--fleet-lease",
        type=Path,
        default=Path("fleet/.work/fleet-lease.json"),
        help="ignored, task-owned staged fleet rental journal",
    )
    parser.add_argument("--node-name", choices=("kip-control", "kip-gpu-1", "kip-gpu-2"))
    parser.add_argument("--public-key-dir", type=Path, default=Path("fleet/.work"))
    args = parser.parse_args()
    try:
        if args.action == "release-first":
            result = release_first_worker(VastRentalClient(configured_key_file()), args.lease)
        elif args.action == "release-fleet":
            result = release_complete_fleet(
                VastRentalClient(configured_key_file()), args.lease, args.fleet_lease
            )
        elif args.action == "release-gpu2":
            result = release_second_worker(
                VastRentalClient(configured_key_file()), args.lease, args.fleet_lease
            )
        elif args.action == "release-control":
            result = release_fleet_control(
                VastRentalClient(configured_key_file()), args.lease, args.fleet_lease
            )
        elif args.action == "rent-control":
            load_compatibility_lock()
            result = rent_next_fleet_node(
                VastRentalClient(configured_key_file()),
                args.lease,
                args.fleet_lease,
                Path.home() / ".ssh" / "id_ed25519.pub",
                role="control",
            )
        elif args.action == "certify-control":
            result = validate_and_record_control(
                VastRentalClient(configured_key_file()),
                args.lease,
                args.fleet_lease,
                key=Path.home() / ".ssh" / "id_ed25519",
                known_hosts=Path(".secrets/vast-known-hosts"),
            )
        elif args.action == "rent-gpu2":
            load_compatibility_lock()
            result = rent_next_fleet_node(
                VastRentalClient(configured_key_file()),
                args.lease,
                args.fleet_lease,
                Path.home() / ".ssh" / "id_ed25519.pub",
                role="gpu2",
            )
        elif args.action == "mesh-plan":
            if args.node_name is None:
                raise FleetError("mesh-plan requires --node-name")
            plans = owned_peer_plans(
                VastReadClient(configured_key_file()),
                args.lease,
                args.fleet_lease,
                args.public_key_dir,
            )
            if args.node_name not in plans:
                raise FleetError("requested mesh node has not been rented and verified")
            result = plans[args.node_name]
        elif args.action == "rent-first":
            load_compatibility_lock()
            result = rent_first_worker(
                VastRentalClient(configured_key_file()),
                args.lease,
                Path.home() / ".ssh" / "id_ed25519.pub",
            )
        elif args.action == "check":
            result = check_fleet(args.inventory)
        elif args.action == "compat-check":
            lock = load_compatibility_lock()
            result = {
                "status": lock["status"],
                "base": lock["base"],
                "gpu": lock["gpu"],
                "inferenceVersions": {
                    "llmd": lock["inference"]["llmdVersion"],
                    "vllm": lock["inference"]["vllmVersion"],
                },
            }
        else:
            load_compatibility_lock()
            first_worker_lease = _load_lease(args.lease)
            result = read_only_plan(
                VastReadClient(configured_key_file()),
                allow_mixed=args.allow_mixed,
                first_worker_lease=(
                    first_worker_lease
                    if first_worker_lease and first_worker_lease.get("phase") == "created"
                    else None
                ),
            )
    except FleetError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, indent=2))
    return 1 if result.get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
