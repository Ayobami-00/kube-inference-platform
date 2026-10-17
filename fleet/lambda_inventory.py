"""Pure, role-aware Kubernetes node/GPU validation for owned Lambda leases.

The labels and instance-ID annotations are placed only after an SSH-verified join.
Reading a matching label alone is never proof that a cloud VM is task-owned.
"""

from __future__ import annotations

from typing import Any

from .lambda_cloud import LambdaError

ROLE_LABEL = "kube-inference-platform.io/lambda-role"
INSTANCE_ANNOTATION = "kube-inference-platform.io/lambda-instance-id"
EXPECTED_GPUS = {
    "control-speech": 1,
    "small-1": 1,
    "small-2": 1,
    "large": 4,
    "nvfp4": 1,
    "h100-control": 1,
}


def _gpu_count(value: Any) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise LambdaError("Kubernetes GPU count is missing or malformed")
    return int(value)


def validate_owned_nodes(
    nodes: dict[str, Any], lease: dict[str, Any], *, region: str | None = None
) -> dict[str, Any]:
    """Require every active owned VM to have one Ready, schedulable node.

    This deliberately rejects extra role-labelled nodes and never assumes that
    a four-GPU machine is four independent workers.
    """
    items = nodes.get("items") if isinstance(nodes, dict) else None
    if not isinstance(items, list):
        raise LambdaError("Kubernetes node list is malformed")
    active = {
        role: node
        for role, node in lease["nodes"].items()
        if node["phase"] == "active"
        and (region is None or node.get("region", lease["region"]) == region)
    }
    observed: dict[str, dict[str, Any]] = {}
    names: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise LambdaError("Kubernetes node entry is malformed")
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            raise LambdaError("Kubernetes node metadata is malformed")
        name = metadata.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise LambdaError("Kubernetes node name is missing or duplicated")
        names.add(name)
        labels = metadata.get("labels", {})
        annotations = metadata.get("annotations", {})
        if not isinstance(labels, dict) or not isinstance(annotations, dict):
            raise LambdaError("Kubernetes node labels or annotations are malformed")
        role = labels.get(ROLE_LABEL)
        if role is None:
            continue
        if role not in active or role in observed:
            raise LambdaError("unowned or duplicate Lambda Kubernetes node role")
        if annotations.get(INSTANCE_ANNOTATION) != active[role].get("instanceId"):
            raise LambdaError("Lambda Kubernetes node instance ownership drift")
        status = item.get("status")
        spec = item.get("spec", {})
        if not isinstance(status, dict) or not isinstance(spec, dict):
            raise LambdaError("Kubernetes node status or spec is malformed")
        conditions = status.get("conditions")
        if not isinstance(conditions, list) or not any(
            isinstance(condition, dict)
            and condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in conditions
        ):
            raise LambdaError(f"Lambda node {role} is not Ready")
        if spec.get("unschedulable") is True:
            raise LambdaError(f"Lambda node {role} is cordoned")
        capacity = status.get("capacity", {})
        allocatable = status.get("allocatable", {})
        if not isinstance(capacity, dict) or not isinstance(allocatable, dict):
            raise LambdaError("Kubernetes node resources are malformed")
        expected = active[role].get("expectedPhysicalGpus", EXPECTED_GPUS[role])
        if type(expected) is not int or expected < EXPECTED_GPUS[role]:
            raise LambdaError("owned Lambda physical GPU expectation is invalid")
        if (
            _gpu_count(capacity.get("nvidia.com/gpu")) != expected
            or _gpu_count(allocatable.get("nvidia.com/gpu")) != expected
        ):
            raise LambdaError(f"Lambda node {role} does not expose {expected} whole GPUs")
        observed[role] = {
            "nodeName": name,
            "instanceId": active[role]["instanceId"],
            "allocatableGpus": expected,
            "ready": True,
        }
    missing = sorted(set(active) - set(observed))
    if missing:
        raise LambdaError(f"owned Lambda Kubernetes nodes are missing: {', '.join(missing)}")
    if not active:
        raise LambdaError("no active owned Lambda instances to validate")
    return {"status": "passed", "nodes": dict(sorted(observed.items()))}
