"""Join only SSH/API-verified, task-owned Lambda workers to the private pilot API."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path

from .lambda_cloud import LambdaClient, LambdaError
from .lambda_rental import LEASE_PATH, load_lease, matching_instance

KNOWN_HOSTS = Path("fleet/.work/lambda_known_hosts")
JOINABLE = frozenset({"small-1", "large"})
_TOKEN = re.compile(r"^[a-z0-9]{6}\.[a-z0-9]{16}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_RFC1918 = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def _address(instance: dict, field: str) -> str:
    value = instance.get(field)
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise LambdaError(f"owned instance {field} is missing or invalid") from error
    if not isinstance(address, ipaddress.IPv4Address):
        raise LambdaError(f"owned instance {field} must be IPv4")
    if field == "private_ip":
        if not any(address in network for network in _RFC1918):
            raise LambdaError("owned instance private_ip is not RFC1918")
    elif not address.is_global:
        raise LambdaError("owned instance public ip is not globally routable")
    return str(address)


def _ssh(ip: str, command: str, *, input_text: str | None = None, timeout: int = 30) -> str:
    if not KNOWN_HOSTS.is_file() or KNOWN_HOSTS.is_symlink():
        raise LambdaError("verified Lambda SSH known-hosts file is missing")
    args = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o",
        "ConnectTimeout=8",
        f"ubuntu@{ip}",
        command,
    ]
    try:
        result = subprocess.run(
            args, input=input_text, text=True, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LambdaError(f"owned SSH operation failed ({type(error).__name__})") from None
    if result.returncode:
        # kubeadm diagnostics can contain the private token. Never echo remote stderr.
        raise LambdaError(
            f"owned SSH operation failed (exit {result.returncode}); inspect target logs"
        )
    return result.stdout.strip()


def label_owned_node(
    control_public: str, node_name: str, private_ip: str, role: str, instance_id: str
) -> None:
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", node_name):
        raise LambdaError("owned Kubernetes node name is invalid")
    if not re.fullmatch(r"[a-zA-Z0-9-]{8,80}", instance_id):
        raise LambdaError("owned Lambda instance ID is invalid")
    raw = _ssh(control_public, "kubectl get nodes -o json")
    try:
        items = json.loads(raw)["items"]
    except (ValueError, KeyError, TypeError) as error:
        raise LambdaError("Kubernetes node inventory is malformed") from error
    matching = [item for item in items if item.get("metadata", {}).get("name") == node_name]
    if len(matching) != 1:
        raise LambdaError("owned Kubernetes node is absent or duplicated")
    item = matching[0]
    addresses = item.get("status", {}).get("addresses", [])
    if not any(
        address.get("type") == "InternalIP" and address.get("address") == private_ip
        for address in addresses
    ):
        raise LambdaError("Kubernetes node private IP differs from Lambda API")
    metadata = item["metadata"]
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    role_key = "kube-inference-platform.io/lambda-role"
    id_key = "kube-inference-platform.io/lambda-instance-id"
    if labels.get(role_key) not in (None, role) or annotations.get(id_key) not in (
        None,
        instance_id,
    ):
        raise LambdaError("Kubernetes node has conflicting Lambda ownership metadata")
    if any(
        other is not item
        and (
            other.get("metadata", {}).get("labels", {}).get(role_key) == role
            or other.get("metadata", {}).get("annotations", {}).get(id_key) == instance_id
        )
        for other in items
    ):
        raise LambdaError("another Kubernetes node claims this Lambda role or instance")
    _ssh(control_public, f"kubectl label node {node_name} {role_key}={role} --overwrite")
    _ssh(control_public, f"kubectl annotate node {node_name} {id_key}={instance_id} --overwrite")


def join(role: str, lease_path: Path = LEASE_PATH, *, labels_only: bool = False) -> None:
    if role not in JOINABLE:
        raise LambdaError("worker role is not allowlisted")
    lease = load_lease(lease_path)
    if lease is None or any(
        lease["nodes"].get(name, {}).get("phase") != "active" for name in ("control-speech", role)
    ):
        raise LambdaError("control and worker must have active owned leases")
    client = LambdaClient()
    instances = client.instances()
    control = matching_instance(lease, "control-speech", instances)
    worker = matching_instance(lease, role, instances)
    if (
        not control
        or not worker
        or control.get("status") != "active"
        or worker.get("status") != "active"
    ):
        raise LambdaError("control or worker is not active in Lambda API")
    if control["region"]["name"] != worker["region"]["name"]:
        raise LambdaError("cross-region worker join is prohibited")
    control_public = _address(control, "ip")
    control_private = _address(control, "private_ip")
    worker_public = _address(worker, "ip")
    worker_private = _address(worker, "private_ip")
    for ip, expected_name in (
        (control_public, lease["nodes"]["control-speech"]["name"]),
        (worker_public, lease["nodes"][role]["name"]),
    ):
        if _ssh(ip, "hostname") != expected_name:
            raise LambdaError("SSH host identity does not match the owned Lambda lease")
    if (
        _ssh(
            worker_public,
            f"ping -c 2 -W 2 {control_private} >/dev/null && "
            f"timeout 5 bash -c '</dev/tcp/{control_private}/6443' && echo ready",
        )
        != "ready"
    ):
        raise LambdaError("worker cannot reach the control plane over private IPv4")
    if not labels_only:
        token = _ssh(control_public, "sudo kubeadm token create --ttl 2h")
        ca_hash = _ssh(
            control_public,
            "sudo openssl x509 -pubkey -in "
            "/etc/kubernetes/pki/ca.crt | openssl pkey -pubin -outform DER "
            "| sha256sum | awk '{print $1}'",
        )
        if not _TOKEN.fullmatch(token) or not _HASH.fullmatch(ca_hash):
            raise LambdaError("control returned an invalid private kubeadm join credential")
        plan = (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "controlIpv4": control_private,
                    "token": token,
                    "caHash": f"sha256:{ca_hash}",
                }
            )
            + "\n"
        )
        print(f"Joining owned {role} through private API; token remains in process memory only.")
        output = _ssh(
            worker_public,
            "cd /home/ubuntu/kube-inference-platform && "
            f"sudo env KIP_NODE_IP={worker_private} "
            f"KIP_LAMBDA_CONTROL_IP={control_private} "
            "bash deploy/fleet/worker-join.sh",
            input_text=plan,
            timeout=900,
        )
        if "Fleet worker join completed:" not in output or "0 failed" not in output:
            raise LambdaError("worker join returned without a passing summary")
    label_owned_node(
        control_public,
        lease["nodes"]["control-speech"]["name"],
        control_private,
        "control-speech",
        lease["nodes"]["control-speech"]["instanceId"],
    )
    label_owned_node(
        control_public,
        lease["nodes"][role]["name"],
        worker_private,
        role,
        lease["nodes"][role]["instanceId"],
    )
    print(f"Owned {role} joined and labelled; verify Ready, Cilium, and GPU capacity next.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Join a task-owned Lambda worker")
    parser.add_argument("role", choices=sorted(JOINABLE))
    parser.add_argument("--labels-only", action="store_true")
    args = parser.parse_args()
    try:
        join(args.role, labels_only=args.labels_only)
    except LambdaError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
