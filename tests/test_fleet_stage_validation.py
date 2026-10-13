"""Second rental is unlocked only by live, ownership-bound two-node evidence."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fleet import stage_validation
from fleet.vast import FleetError


def _fixtures(tmp_path: Path) -> tuple[Path, Path, list[dict[str, object]]]:
    first = {
        "schemaVersion": 1,
        "managedBy": "kube-inference-platform",
        "phase": "created",
        "label": "kip-pilot-a-gpu1",
        "instanceId": 101,
        "machineId": 201,
        "hostId": 301,
        "templateHash": "template",
        "gpu": "RTX 4090",
    }
    journal = {
        "schemaVersion": 1,
        "managedBy": "kube-inference-platform",
        "pilotId": "kip-pilot-a",
        "firstWorkerInstanceId": 101,
        "nodes": {
            "control": {
                "phase": "created",
                "label": "kip-pilot-a-control",
                "instanceId": 102,
                "machineId": 202,
                "hostId": 302,
                "templateHash": "template",
            },
            "gpu2": {"phase": "planned"},
        },
    }
    first_path = tmp_path / "first.json"
    fleet_path = tmp_path / "fleet.json"
    for path, payload in ((first_path, first), (fleet_path, journal)):
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(path, 0o600)
    instances = [
        {
            "id": number,
            "label": label,
            "machine_id": machine,
            "host_id": host,
            "template_hash_id": "template",
            "actual_status": "running",
            "cur_state": "running",
            "num_gpus": 1,
            "gpu_name": "RTX 4090",
            "dph_total": 0.6,
        }
        for number, label, machine, host in (
            (101, "kip-pilot-a-gpu1", 201, 301),
            (102, "kip-pilot-a-control", 202, 302),
        )
    ]
    return first_path, fleet_path, instances


def _cluster_json() -> tuple[dict[str, object], dict[str, object]]:
    nodes = {
        "items": [
            {
                "metadata": {
                    "name": name,
                    "labels": {"nvidia.com/gpu.deploy.operands": "false"} if index == 0 else {},
                },
                "spec": {"unschedulable": False},
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "allocatable": {"nvidia.com/gpu": str(index)},
                },
            }
            for index, name in enumerate(("kip-control", "kip-gpu-1"))
        ]
    }
    names = [
        "kube-apiserver-kip-control",
        "etcd-kip-control",
        "kube-controller-manager-kip-control",
        "kube-scheduler-kip-control",
        "cilium-operator-a",
        "coredns-a",
        "coredns-b",
        "kube-proxy-a",
        "kube-proxy-b",
        "cilium-a",
        "cilium-b",
    ]
    pods = {
        "items": [
            {
                "metadata": {"namespace": "kube-system", "name": name},
                "status": {"phase": "Running", "containerStatuses": [{"ready": True}]},
            }
            for name in names
        ]
    }
    return nodes, pods


class FakeClient:
    def __init__(self, instances: list[dict[str, object]]) -> None:
        self.instances = instances

    def get(self, path: str) -> dict[str, object]:
        assert path == "/instances/"
        return {"instances": self.instances}


def test_live_stage_evidence_unlocks_only_owned_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, fleet, instances = _fixtures(tmp_path)
    nodes, pods = _cluster_json()
    calls: list[str] = []

    def fake_ssh(instance: dict[str, object], command: str, **_: object) -> str:
        calls.append(command)
        if "get nodes -o json" in command:
            return json.dumps(nodes)
        if "get pods -A -o json" in command:
            return json.dumps(pods)
        return "passed"

    monkeypatch.setattr(stage_validation, "_ssh_capture", fake_ssh)
    result = stage_validation.validate_and_record_control(
        FakeClient(instances), first, fleet, key=tmp_path / "key", known_hosts=tmp_path / "hosts"
    )
    assert result["status"] == "passed"
    assert result["gpuInventory"] == [
        {"node": "kip-control", "gpuAllocatable": 0},
        {"node": "kip-gpu-1", "gpuAllocatable": 1},
    ]
    assert any("network-smoke.sh" in call for call in calls)
    assert any("gpu-install.sh" in call for call in calls)
    assert json.loads(fleet.read_text())["nodes"]["control"]["validationStatus"] == "passed"


def test_failed_live_gpu_checkpoint_does_not_unlock_rental(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, fleet, instances = _fixtures(tmp_path)

    def fake_ssh(instance: dict[str, object], command: str, **_: object) -> str:
        if "gpu-install.sh" in command:
            raise FleetError("GPU smoke failed")
        return "passed"

    monkeypatch.setattr(stage_validation, "_ssh_capture", fake_ssh)
    with pytest.raises(FleetError, match="GPU smoke failed"):
        stage_validation.validate_and_record_control(
            FakeClient(instances),
            first,
            fleet,
            key=tmp_path / "key",
            known_hosts=tmp_path / "hosts",
        )
    assert "validationStatus" not in json.loads(fleet.read_text())["nodes"]["control"]
