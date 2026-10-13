"""Read-only Kubernetes checks for task-owned control-plane and worker inventory."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .inventory import FleetInventory, load_inventory
from .vast import FleetError


def _kubectl_json(*arguments: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["kubectl", *arguments, "-o", "json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise FleetError(f"kubectl read failed for {' '.join(arguments)}") from error
    if not isinstance(payload, dict):
        raise FleetError("kubectl returned an unexpected JSON document")
    return payload


def validate_cluster_state(
    inventory: FleetInventory, nodes_payload: dict[str, Any], pods_payload: dict[str, Any]
) -> dict[str, Any]:
    """Require exact owned node set, readiness, GPUs, and healthy core Pods."""
    node_items = nodes_payload.get("items")
    pod_items = pods_payload.get("items")
    if not isinstance(node_items, list) or not isinstance(pod_items, list):
        raise FleetError("Kubernetes node/pod list is malformed")
    actual: dict[str, dict[str, Any]] = {}
    for item in node_items:
        if not isinstance(item, dict):
            raise FleetError("Kubernetes node list contains malformed item")
        name = item.get("metadata", {}).get("name")
        if not isinstance(name, str) or name in actual:
            raise FleetError("Kubernetes node names are missing or duplicated")
        actual[name] = item
    expected = {node.name for node in inventory.nodes}
    if set(actual) != expected:
        raise FleetError("Kubernetes node set differs from the task-owned fleet inventory")
    gpu_inventory = []
    for expected_node in inventory.nodes:
        item = actual[expected_node.name]
        conditions = item.get("status", {}).get("conditions", [])
        if not any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in conditions
            if isinstance(condition, dict)
        ):
            raise FleetError(f"node {expected_node.name} is not Ready")
        if item.get("spec", {}).get("unschedulable") is True:
            raise FleetError(f"node {expected_node.name} is cordoned")
        allocatable = item.get("status", {}).get("allocatable", {})
        try:
            gpu_count = int(allocatable.get("nvidia.com/gpu", "0"))
        except (TypeError, ValueError) as error:
            raise FleetError(f"node {expected_node.name} has invalid GPU allocatable") from error
        operands_disabled = (
            expected_node.role == "control-plane"
            and item.get("metadata", {}).get("labels", {}).get("nvidia.com/gpu.deploy.operands")
            == "false"
        )
        expected_allocatable = 0 if operands_disabled else expected_node.physical_gpu_count
        if gpu_count != expected_allocatable:
            raise FleetError(f"node {expected_node.name} GPU allocatable differs from inventory")
        gpu_inventory.append({"node": expected_node.name, "gpuAllocatable": gpu_count})
    system_pods = [
        item
        for item in pod_items
        if isinstance(item, dict) and item.get("metadata", {}).get("namespace") == "kube-system"
    ]
    if not system_pods:
        raise FleetError("no kube-system Pods found")
    healthy_names: list[str] = []
    for pod in system_pods:
        name = pod.get("metadata", {}).get("name", "unknown")
        status = pod.get("status", {})
        phase = status.get("phase")
        if phase == "Succeeded":
            continue
        if phase != "Running":
            raise FleetError(f"core Pod {name} is not Running or successfully Completed")
        container_statuses = status.get("containerStatuses", [])
        if not container_statuses or not all(
            isinstance(item, dict) and item.get("ready") is True for item in container_statuses
        ):
            raise FleetError(f"core Pod {name} has unready containers")
        healthy_names.append(name)
    required = {
        "kube-apiserver-": 1,
        "etcd-": 1,
        "kube-controller-manager-": 1,
        "kube-scheduler-": 1,
        "kube-proxy-": len(actual),
        "cilium-": len(actual),
        "cilium-operator-": 1,
        "coredns-": 2,
    }
    for prefix, count in required.items():
        matching = sum(
            name.startswith(prefix)
            and (prefix != "cilium-" or not name.startswith("cilium-operator-"))
            for name in healthy_names
        )
        if matching < count:
            raise FleetError(f"expected at least {count} healthy {prefix} kube-system Pods")
    return {
        "status": "healthy",
        "pilotId": inventory.pilot_id,
        "readyNodes": len(actual),
        "corePods": len(system_pods),
        "gpuInventory": gpu_inventory,
    }


def check_fleet(path: Path) -> dict[str, Any]:
    inventory = load_inventory(path)
    return validate_cluster_state(
        inventory,
        _kubectl_json("get", "nodes"),
        _kubectl_json("get", "pods", "-A"),
    )
