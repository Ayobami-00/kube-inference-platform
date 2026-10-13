"""Build public-key-only peer plans from exactly owned Vast VM instances."""

from __future__ import annotations

import base64
import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .rental import _load_lease
from .vast import FleetError, VastReadClient

NODE_ADDRESSES = {
    "kip-control": ("control-plane", "10.200.0.1"),
    "kip-gpu-1": ("gpu-worker", "10.200.0.2"),
    "kip-gpu-2": ("gpu-worker", "10.200.0.3"),
    "kip-speech-1": ("speech-worker", "10.200.0.4"),
}


@dataclass(frozen=True)
class MeshEndpoint:
    name: str
    role: str
    instance_id: int
    private_ip: str
    public_ip: str
    ssh_port: int
    udp_port: int
    public_key: str


def _host_port(raw: dict[str, Any], protocol: str) -> int:
    mappings = raw.get("ports", {}).get(protocol)
    if not isinstance(mappings, list):
        raise FleetError(f"owned VM has no {protocol} port mapping")
    ipv4 = [item for item in mappings if isinstance(item, dict) and item.get("HostIp") == "0.0.0.0"]
    if len(ipv4) != 1:
        raise FleetError(f"owned VM has ambiguous {protocol} IPv4 port mapping")
    try:
        port = int(ipv4[0]["HostPort"])
    except (ValueError, TypeError, KeyError) as error:
        raise FleetError(f"owned VM has invalid {protocol} mapped port") from error
    if port < 1 or port > 65535:
        raise FleetError(f"owned VM has invalid {protocol} mapped port")
    return port


def owned_endpoint(
    instance: dict[str, Any],
    lease: dict[str, Any],
    *,
    name: str,
    role: str,
    private_ip: str,
    public_key: str,
) -> MeshEndpoint:
    """Refuse stale/misidentified VMs, missing UDP ingress, or network drift."""
    if NODE_ADDRESSES.get(name) != (role, private_ip):
        raise FleetError("mesh name, role, and private address must match reviewed slots")
    try:
        mesh_ip = ipaddress.IPv4Address(private_ip)
        public_ip = ipaddress.IPv4Address(instance.get("public_ipaddr"))
    except (ValueError, TypeError) as error:
        raise FleetError("owned VM mesh/public address is invalid") from error
    if mesh_ip not in ipaddress.IPv4Network("10.200.0.0/24") or not public_ip.is_global:
        raise FleetError("owned VM mesh/public address is outside its contract")
    if (
        lease.get("managedBy") != "kube-inference-platform"
        or lease.get("phase") != "created"
        or instance.get("id") != lease.get("instanceId")
        or instance.get("label") != lease.get("label")
        or instance.get("machine_id") != lease.get("machineId")
        or (lease.get("hostId") is not None and instance.get("host_id") != lease["hostId"])
        or instance.get("template_hash_id") != lease.get("templateHash")
    ):
        raise FleetError("owned VM identity differs from its lease")
    if instance.get("actual_status") != "running" or instance.get("cur_state") != "running":
        raise FleetError("owned VM is not reported running by Vast")
    if not isinstance(public_key, str) or len(public_key) != 44:
        raise FleetError("WireGuard public key is malformed")
    try:
        if len(base64.b64decode(public_key, validate=True)) != 32:
            raise FleetError("WireGuard public key has the wrong byte length")
    except ValueError as error:
        raise FleetError("WireGuard public key is not base64") from error
    return MeshEndpoint(
        name=name,
        role=role,
        instance_id=lease["instanceId"],
        private_ip=str(mesh_ip),
        public_ip=str(public_ip),
        ssh_port=_host_port(instance, "22/tcp"),
        udp_port=_host_port(instance, "51820/udp"),
        public_key=public_key,
    )


def peer_plans(endpoints: list[MeshEndpoint]) -> dict[str, dict[str, Any]]:
    if len(endpoints) not in {2, 3, 4}:
        raise FleetError("mesh requires staged control/GPU peers, or three/four nodes")
    controls = [node for node in endpoints if node.role == "control-plane"]
    if len(controls) != 1 or controls[0].private_ip != "10.200.0.1":
        raise FleetError("mesh must use the reviewed stable control-plane address")
    gpu_names = {node.name for node in endpoints if node.role == "gpu-worker"}
    if gpu_names != {"kip-gpu-1"} and gpu_names != {"kip-gpu-1", "kip-gpu-2"}:
        raise FleetError("mesh requires the first GPU worker before the second")
    if len(endpoints) == 4 and {node.name for node in endpoints} != set(NODE_ADDRESSES):
        raise FleetError("four-node mesh requires the reviewed speech-worker slot")
    if len(endpoints) == 3 and "kip-speech-1" in {node.name for node in endpoints}:
        raise FleetError("speech worker cannot join before the second GPU worker")
    if any(NODE_ADDRESSES.get(node.name) != (node.role, node.private_ip) for node in endpoints):
        raise FleetError("mesh contains a node outside reviewed name/role/address slots")
    if (
        len({node.name for node in endpoints}) != len(endpoints)
        or len({node.instance_id for node in endpoints}) != len(endpoints)
        or len({node.private_ip for node in endpoints}) != len(endpoints)
        or len({node.public_key for node in endpoints}) != len(endpoints)
    ):
        raise FleetError("mesh node names, instances, addresses, or keys are duplicated")
    plans: dict[str, dict[str, Any]] = {}
    for node in endpoints:
        plans[node.name] = {
            "schemaVersion": 1,
            "node": {"name": node.name, "privateIpv4": node.private_ip},
            "peers": [
                {
                    "name": peer.name,
                    "privateIpv4": peer.private_ip,
                    "publicKey": peer.public_key,
                    "endpointIpv4": peer.public_ip,
                    "endpointPort": peer.udp_port,
                }
                for peer in sorted(endpoints, key=lambda item: item.private_ip)
                if peer.name != node.name
            ],
        }
    return plans


def _public_key_record(path: Path, instance_id: int) -> str:
    if path.is_symlink():
        raise FleetError("public-key record must not be a symlink")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FleetError("task-owned public-key record is unavailable") from error
    if not isinstance(record, dict) or set(record) != {"instanceId", "publicKey"}:
        raise FleetError("task-owned public-key record has unexpected fields")
    if record["instanceId"] != instance_id:
        raise FleetError("WireGuard public key is bound to another VM instance")
    return record["publicKey"]


def owned_peer_plans(
    client: VastReadClient,
    first_lease_path: Path,
    fleet_lease_path: Path,
    public_key_dir: Path,
) -> dict[str, dict[str, Any]]:
    """Make plans only from exact live leases and target-generated public keys."""
    first = _load_lease(first_lease_path)
    journal = _load_lease(fleet_lease_path)
    if first is None or first.get("phase") != "created" or journal is None:
        raise FleetError("active first-worker and fleet journals are required")
    nodes = journal.get("nodes")
    if (
        journal.get("firstWorkerInstanceId") != first.get("instanceId")
        or not isinstance(nodes, dict)
        or set(nodes) != {"control", "gpu2"}
        or nodes["control"].get("phase") != "created"
    ):
        raise FleetError("fleet journal lacks the owned control-plane VM")
    second_phase = nodes["gpu2"].get("phase")
    if second_phase not in {"planned", "created"}:
        raise FleetError("second-worker rental has unresolved state")
    raw = client.get("/instances/").get("instances")
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise FleetError("Vast instance inventory is unavailable")
    planned = [
        (
            "kip-control",
            "control-plane",
            "10.200.0.1",
            {**nodes["control"], "managedBy": journal["managedBy"]},
            "control",
        ),
        ("kip-gpu-1", "gpu-worker", "10.200.0.2", first, "first-worker"),
    ]
    if second_phase == "created":
        planned.append(
            (
                "kip-gpu-2",
                "gpu-worker",
                "10.200.0.3",
                {**nodes["gpu2"], "managedBy": journal["managedBy"]},
                "gpu2",
            )
        )
    endpoints: list[MeshEndpoint] = []
    host_ids: set[int] = set()
    for name, role, private_ip, lease, key_name in planned:
        matches = [item for item in raw if item.get("id") == lease.get("instanceId")]
        if len(matches) != 1:
            raise FleetError("owned VM is absent or duplicated in Vast inventory")
        instance = matches[0]
        host_id = instance.get("host_id")
        if not isinstance(host_id, int) or host_id < 1 or host_id in host_ids:
            raise FleetError("mesh VMs do not occupy distinct physical Vast hosts")
        host_ids.add(host_id)
        public_key = _public_key_record(
            public_key_dir / f"{key_name}-wireguard-public.json", lease["instanceId"]
        )
        endpoints.append(
            owned_endpoint(
                instance,
                lease,
                name=name,
                role=role,
                private_ip=private_ip,
                public_key=public_key,
            )
        )
    return peer_plans(endpoints)
