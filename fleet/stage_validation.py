"""Prove the two-node GPU checkpoint before allowing the second Vast rental."""

# Ubuntu 22.04 control VMs use Python 3.10; datetime.UTC was added in 3.11.
# ruff: noqa: UP017

from __future__ import annotations

import ipaddress
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .check import validate_cluster_state
from .inventory import FleetInventory, FleetNode
from .rental import VastRentalClient, _lease_lock, _load_lease, _save_lease, _verify_lease_instance
from .vast import FleetError

ROOT = Path(__file__).resolve().parents[1]


def _ssh_capture(
    instance: dict[str, Any], command: str, *, key: Path, known_hosts: Path, timeout: int = 90
) -> str:
    if not key.is_file() or not known_hosts.is_file() or known_hosts.is_symlink():
        raise FleetError("reviewed SSH key or pinned Vast host-key file is unavailable")
    ports = instance.get("ports", {}).get("22/tcp")
    if not isinstance(ports, list):
        raise FleetError("owned VM lacks an SSH port mapping")
    mapped = [entry for entry in ports if entry.get("HostIp") == "0.0.0.0"]
    if len(mapped) != 1:
        raise FleetError("owned VM has ambiguous SSH port mapping")
    try:
        port = int(mapped[0]["HostPort"])
        address = ipaddress.IPv4Address(instance.get("public_ipaddr"))
    except (TypeError, ValueError, KeyError) as error:
        raise FleetError("owned VM SSH endpoint is malformed") from error
    if not address.is_global or not 1 <= port <= 65535:
        raise FleetError("owned VM SSH endpoint is unsafe")
    try:
        completed = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={known_hosts}",
                "-i",
                str(key),
                "-p",
                str(port),
                f"root@{address}",
                command,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise FleetError("owned VM failed the live stage check; inspect the target") from error
    return completed.stdout


def _json_output(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise FleetError("owned control VM returned invalid Kubernetes JSON") from error
    if not isinstance(payload, dict):
        raise FleetError("owned control VM returned unexpected Kubernetes JSON")
    return payload


def _stage_inventory(
    first: dict[str, Any],
    control: dict[str, Any],
    first_vm: dict[str, Any],
    control_vm: dict[str, Any],
) -> FleetInventory:
    control_gpu_count = int(control_vm.get("num_gpus") or 0)
    control_gpu_model = (
        str(control_vm.get("gpu_name") or "NVIDIA GPU") if control_gpu_count else None
    )
    return FleetInventory(
        pilot_id=first["label"].removesuffix("-gpu1"),
        api_endpoint="api.kip.internal:6443",
        nodes=(
            FleetNode(
                "control-plane",
                "kip-control",
                control["instanceId"],
                control["machineId"],
                "10.200.0.1",
                control_gpu_model,
                control_gpu_count,
                float(control_vm["dph_total"]),
            ),
            FleetNode(
                "gpu-worker",
                "kip-gpu-1",
                first["instanceId"],
                first["machineId"],
                "10.200.0.2",
                first["gpu"],
                1,
                float(first_vm["dph_total"]),
            ),
        ),
    )


def validate_and_record_control(
    client: VastRentalClient,
    first_lease_path: Path,
    fleet_lease_path: Path,
    *,
    key: Path,
    known_hosts: Path,
) -> dict[str, Any]:
    """Repeat live network/GPU smoke, then record an exact ownership-bound gate."""
    with _lease_lock(fleet_lease_path):
        first = _load_lease(first_lease_path)
        journal = _load_lease(fleet_lease_path)
        if first is None or journal is None or first.get("phase") != "created":
            raise FleetError("active task-owned first-worker and fleet leases are required")
        nodes = journal.get("nodes")
        if (
            journal.get("firstWorkerInstanceId") != first.get("instanceId")
            or not isinstance(nodes, dict)
            or set(nodes) != {"control", "gpu2"}
            or nodes["control"].get("phase") != "created"
            or nodes["gpu2"].get("phase") != "planned"
        ):
            raise FleetError("two-node ownership journal is not in the staged phase")
        first_vm = _verify_lease_instance(client, first)
        control_vm = _verify_lease_instance(client, nodes["control"])
        instances = client.get("/instances/").get("instances")
        if not isinstance(instances, list) or {item.get("id") for item in instances} != {
            first["instanceId"],
            nodes["control"]["instanceId"],
        }:
            raise FleetError("Vast account contains an instance outside the staged pilot")

        _ssh_capture(
            first_vm,
            "make -C /root/kube-inference-platform host-check GPU_REQUIRED=1",
            key=key,
            known_hosts=known_hosts,
        )
        _ssh_capture(
            control_vm,
            "/root/kube-inference-platform/deploy/fleet/network-smoke.sh",
            key=key,
            known_hosts=known_hosts,
            timeout=300,
        )
        _ssh_capture(
            control_vm,
            "KIP_FLEET_GPU_COUNT=1 /root/kube-inference-platform/deploy/fleet/gpu-install.sh",
            key=key,
            known_hosts=known_hosts,
            timeout=2100,
        )
        nodes_payload = _json_output(
            _ssh_capture(
                control_vm,
                "kubectl --kubeconfig /etc/kubernetes/admin.conf get nodes -o json",
                key=key,
                known_hosts=known_hosts,
            )
        )
        pods_payload = _json_output(
            _ssh_capture(
                control_vm,
                "kubectl --kubeconfig /etc/kubernetes/admin.conf get pods -A -o json",
                key=key,
                known_hosts=known_hosts,
            )
        )
        summary = validate_cluster_state(
            _stage_inventory(first, nodes["control"], first_vm, control_vm),
            nodes_payload,
            pods_payload,
        )
        nodes["control"]["validationStatus"] = "passed"
        nodes["control"]["validatedUtc"] = datetime.now(timezone.utc).isoformat()
        nodes["control"]["validationEvidence"] = {
            "readyNodes": summary["readyNodes"],
            "gpuInventory": summary["gpuInventory"],
            "checks": ["host-gpu", "cross-node-network", "pinned-gpu-operator", "gpu-pod"],
        }
        _save_lease(fleet_lease_path, journal)
        return {
            "status": "passed",
            "pilotId": journal["pilotId"],
            "readyNodes": summary["readyNodes"],
            "gpuInventory": summary["gpuInventory"],
            "validatedUtc": nodes["control"]["validatedUtc"],
        }
