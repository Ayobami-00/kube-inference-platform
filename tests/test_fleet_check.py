"""Read-only multi-node inventory and Kubernetes health contracts."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from fleet.check import validate_cluster_state
from fleet.inventory import load_inventory
from fleet.vast import FleetError


def raw_inventory() -> dict[str, object]:
    nodes = []
    for index, role in enumerate(("control-plane", "gpu-worker", "gpu-worker"), start=1):
        nodes.append(
            {
                "role": role,
                "name": f"kip-node-{index}",
                "instanceId": 100 + index,
                "machineId": 200 + index,
                "privateIpv4": f"10.71.0.{index}",
                "gpuModel": None if index == 1 else "RTX 4090",
                "physicalGpuCount": 0 if index == 1 else 1,
                "billedHourlyUsd": 0.6,
            }
        )
    return {
        "schemaVersion": 1,
        "managedBy": "kube-inference-platform",
        "pilotId": "pilot-a",
        "apiEndpoint": "api.kip.internal:6443",
        "nodes": nodes,
    }


def write_inventory(tmp_path: Path, raw: dict[str, object]) -> Path:
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def raw_cluster() -> tuple[dict[str, object], dict[str, object]]:
    nodes = [
        {
            "metadata": {"name": f"kip-node-{index}"},
            "spec": {"unschedulable": False},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "allocatable": {"nvidia.com/gpu": str(int(index > 1))},
            },
        }
        for index in range(1, 4)
    ]
    names = [
        "kube-apiserver-kip-node-1",
        "etcd-kip-node-1",
        "kube-controller-manager-kip-node-1",
        "kube-scheduler-kip-node-1",
        "cilium-operator-abc",
        "coredns-abc",
        "coredns-def",
        *(f"kube-proxy-{index}" for index in range(1, 4)),
        *(f"cilium-{index}" for index in range(1, 4)),
    ]
    pods = [
        {
            "metadata": {"namespace": "kube-system", "name": name},
            "status": {"phase": "Running", "containerStatuses": [{"ready": True}]},
        }
        for name in names
    ]
    pods.append(
        {
            "metadata": {"namespace": "kube-system", "name": "install-cni-abc"},
            "status": {"phase": "Succeeded"},
        }
    )
    return {"items": nodes}, {"items": pods}


def test_three_node_inventory_and_healthy_cluster(tmp_path: Path) -> None:
    inventory = load_inventory(write_inventory(tmp_path, raw_inventory()))
    nodes, pods = raw_cluster()
    result = validate_cluster_state(inventory, nodes, pods)
    assert result["readyNodes"] == 3
    assert result["gpuInventory"] == [
        {"node": "kip-node-1", "gpuAllocatable": 0},
        {"node": "kip-node-2", "gpuAllocatable": 1},
        {"node": "kip-node-3", "gpuAllocatable": 1},
    ]


def test_gpu_bearing_control_plane_is_recorded_but_not_a_model_worker(tmp_path: Path) -> None:
    raw = raw_inventory()
    raw["nodes"][0]["gpuModel"] = "RTX 3060"  # type: ignore[index]
    raw["nodes"][0]["physicalGpuCount"] = 1  # type: ignore[index]
    inventory = load_inventory(write_inventory(tmp_path, raw))
    nodes, pods = raw_cluster()
    nodes["items"][0]["status"]["allocatable"]["nvidia.com/gpu"] = "1"  # type: ignore[index]
    result = validate_cluster_state(inventory, nodes, pods)
    assert result["gpuInventory"][0]["gpuAllocatable"] == 1
    assert inventory.nodes[0].role == "control-plane"


def test_gpu_bearing_control_plane_can_explicitly_disable_operator_operands(
    tmp_path: Path,
) -> None:
    raw = raw_inventory()
    raw["nodes"][0]["gpuModel"] = "RTX 4090"  # type: ignore[index]
    raw["nodes"][0]["physicalGpuCount"] = 1  # type: ignore[index]
    inventory = load_inventory(write_inventory(tmp_path, raw))
    nodes, pods = raw_cluster()
    nodes["items"][0]["metadata"]["labels"] = {  # type: ignore[index]
        "nvidia.com/gpu.deploy.operands": "false"
    }
    result = validate_cluster_state(inventory, nodes, pods)
    assert result["gpuInventory"][0]["gpuAllocatable"] == 0


@pytest.mark.parametrize("field", ("instanceId", "machineId", "privateIpv4", "name"))
def test_duplicate_instance_host_address_or_name_rejected(tmp_path: Path, field: str) -> None:
    raw = raw_inventory()
    raw["nodes"][1][field] = raw["nodes"][0][field]  # type: ignore[index]
    with pytest.raises(FleetError, match="duplicate"):
        load_inventory(write_inventory(tmp_path, raw))


@pytest.mark.parametrize("value", (0, -1, float("nan"), float("inf")))
def test_invalid_node_rate_rejected(tmp_path: Path, value: float) -> None:
    raw = raw_inventory()
    raw["nodes"][1]["billedHourlyUsd"] = value  # type: ignore[index]
    with pytest.raises(FleetError, match="billedHourlyUsd"):
        load_inventory(write_inventory(tmp_path, raw))


def test_inventory_symlink_and_hourly_cap_rejected(tmp_path: Path) -> None:
    raw = raw_inventory()
    path = write_inventory(tmp_path, raw)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(FleetError, match="symlink"):
        load_inventory(link)
    raw["nodes"][0]["billedHourlyUsd"] = 2.9  # type: ignore[index]
    with pytest.raises(FleetError, match="exceeds"):
        load_inventory(write_inventory(tmp_path, raw))


def test_unready_or_extra_node_and_gpu_mismatch_rejected(tmp_path: Path) -> None:
    inventory = load_inventory(write_inventory(tmp_path, raw_inventory()))
    nodes, pods = raw_cluster()
    bad = copy.deepcopy(nodes)
    bad["items"][1]["status"]["conditions"][0]["status"] = "False"  # type: ignore[index]
    with pytest.raises(FleetError, match="not Ready"):
        validate_cluster_state(inventory, bad, pods)
    bad = copy.deepcopy(nodes)
    bad["items"][1]["status"]["allocatable"]["nvidia.com/gpu"] = "0"  # type: ignore[index]
    with pytest.raises(FleetError, match="GPU allocatable"):
        validate_cluster_state(inventory, bad, pods)
    bad = copy.deepcopy(nodes)
    bad["items"].append(copy.deepcopy(bad["items"][0]))  # type: ignore[index]
    bad["items"][-1]["metadata"]["name"] = "unmanaged"  # type: ignore[index]
    with pytest.raises(FleetError, match="differs"):
        validate_cluster_state(inventory, bad, pods)


def test_unhealthy_system_pod_rejected(tmp_path: Path) -> None:
    inventory = load_inventory(write_inventory(tmp_path, raw_inventory()))
    nodes, pods = raw_cluster()
    pods["items"][0]["status"]["containerStatuses"][0]["ready"] = False  # type: ignore[index]
    with pytest.raises(FleetError, match="unready"):
        validate_cluster_state(inventory, nodes, pods)


def test_missing_coredns_or_cilium_daemon_rejected(tmp_path: Path) -> None:
    inventory = load_inventory(write_inventory(tmp_path, raw_inventory()))
    nodes, pods = raw_cluster()
    pods["items"] = [
        item for item in pods["items"] if not item["metadata"]["name"].startswith("coredns-")
    ]  # type: ignore[index]
    with pytest.raises(FleetError, match="coredns-"):
        validate_cluster_state(inventory, nodes, pods)
