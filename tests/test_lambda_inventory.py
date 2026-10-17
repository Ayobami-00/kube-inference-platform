"""Role-aware node validation without a live cluster."""

from __future__ import annotations

import copy

import pytest

from fleet.lambda_cloud import LambdaError
from fleet.lambda_inventory import INSTANCE_ANNOTATION, ROLE_LABEL, validate_owned_nodes
from fleet.lambda_rental import new_lease


def _lease() -> dict:
    lease = new_lease("us-east-1")
    for role in ("control-speech", "small-1", "small-2", "large"):
        lease["nodes"][role] = {
            "phase": "active",
            "instanceId": f"owned-{role}-12345678",
        }
    return lease


def _nodes(lease: dict) -> dict:
    items = []
    for role, node in lease["nodes"].items():
        count = "4" if role == "large" else "1"
        items.append(
            {
                "metadata": {
                    "name": f"kip-{role}",
                    "labels": {ROLE_LABEL: role},
                    "annotations": {INSTANCE_ANNOTATION: node["instanceId"]},
                },
                "spec": {"unschedulable": False},
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "capacity": {"nvidia.com/gpu": count},
                    "allocatable": {"nvidia.com/gpu": count},
                },
            }
        )
    return {"items": items}


def test_four_roles_are_ready_with_exact_whole_gpu_inventory() -> None:
    lease = _lease()
    result = validate_owned_nodes(_nodes(lease), lease)
    assert result["status"] == "passed"
    assert result["nodes"]["large"]["allocatableGpus"] == 4
    assert len(result["nodes"]) == 4


@pytest.mark.parametrize(
    "failure", ["missing", "cordoned", "not-ready", "gpu-mismatch", "id-drift", "extra-role"]
)
def test_node_failure_and_ownership_drift_are_refused(failure: str) -> None:
    lease = _lease()
    nodes = _nodes(lease)
    large = nodes["items"][-1]
    if failure == "missing":
        nodes["items"].pop()
    elif failure == "cordoned":
        large["spec"]["unschedulable"] = True
    elif failure == "not-ready":
        large["status"]["conditions"][0]["status"] = "False"
    elif failure == "gpu-mismatch":
        large["status"]["allocatable"]["nvidia.com/gpu"] = "3"
    elif failure == "id-drift":
        large["metadata"]["annotations"][INSTANCE_ANNOTATION] = "someone-else"
    else:
        extra = copy.deepcopy(large)
        extra["metadata"]["name"] = "unknown-node"
        extra["metadata"]["labels"][ROLE_LABEL] = "unowned"
        nodes["items"].append(extra)
    with pytest.raises(LambdaError):
        validate_owned_nodes(nodes, lease)
