"""Vast port mappings become only exact, private WireGuard peer routes."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest

from deploy.fleet.mesh import validate_plan
from fleet.mesh_plan import MeshEndpoint, owned_endpoint, owned_peer_plans, peer_plans
from fleet.rental import _save_lease
from fleet.vast import FleetError

KEYS = [base64.b64encode(bytes([number]) * 32).decode() for number in range(1, 5)]


def instance() -> dict[str, object]:
    return {
        "id": 101,
        "label": "kip-pilot-test-gpu1",
        "machine_id": 900,
        "host_id": 901,
        "template_hash_id": "reviewed-template",
        "actual_status": "running",
        "cur_state": "running",
        "public_ipaddr": "8.8.8.8",
        "ports": {
            "22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "58021"}],
            "51820/udp": [{"HostIp": "0.0.0.0", "HostPort": "58050"}],
        },
    }


def lease() -> dict[str, object]:
    return {
        "managedBy": "kube-inference-platform",
        "phase": "created",
        "instanceId": 101,
        "label": "kip-pilot-test-gpu1",
        "machineId": 900,
        "hostId": 901,
        "templateHash": "reviewed-template",
    }


def test_exact_owned_port_mapping_and_public_key_required() -> None:
    endpoint = owned_endpoint(
        instance(),
        lease(),
        name="kip-gpu-1",
        role="gpu-worker",
        private_ip="10.200.0.2",
        public_key=KEYS[0],
    )
    assert endpoint.ssh_port == 58021
    assert endpoint.udp_port == 58050
    assert endpoint.private_ip == "10.200.0.2"
    drift = instance()
    drift["machine_id"] = 999
    with pytest.raises(FleetError, match="identity differs"):
        owned_endpoint(
            drift,
            lease(),
            name="kip-gpu-1",
            role="gpu-worker",
            private_ip="10.200.0.2",
            public_key=KEYS[0],
        )
    drift = instance()
    drift["host_id"] = 999
    with pytest.raises(FleetError, match="identity differs"):
        owned_endpoint(
            drift,
            lease(),
            name="kip-gpu-1",
            role="gpu-worker",
            private_ip="10.200.0.2",
            public_key=KEYS[0],
        )
    offline = instance()
    offline["actual_status"] = "offline"
    with pytest.raises(FleetError, match="not reported running"):
        owned_endpoint(
            offline,
            lease(),
            name="kip-gpu-1",
            role="gpu-worker",
            private_ip="10.200.0.2",
            public_key=KEYS[0],
        )


def test_missing_udp_mapping_refuses_mesh() -> None:
    raw = instance()
    raw["ports"].pop("51820/udp")  # type: ignore[union-attr]
    with pytest.raises(FleetError, match="no 51820/udp"):
        owned_endpoint(
            raw,
            lease(),
            name="kip-gpu-1",
            role="gpu-worker",
            private_ip="10.200.0.2",
            public_key=KEYS[0],
        )


def test_peer_plans_are_target_validated_and_reject_duplicate_instances() -> None:
    nodes = [
        MeshEndpoint(
            "kip-control", "control-plane", 100, "10.200.0.1", "8.8.8.8", 50001, 50011, KEYS[0]
        ),
        MeshEndpoint(
            "kip-gpu-1", "gpu-worker", 101, "10.200.0.2", "1.1.1.1", 50002, 50012, KEYS[1]
        ),
        MeshEndpoint(
            "kip-gpu-2", "gpu-worker", 102, "10.200.0.3", "9.9.9.9", 50003, 50013, KEYS[2]
        ),
    ]
    plans = peer_plans(nodes)
    assert set(plans) == {node.name for node in nodes}
    for plan in plans.values():
        assert len(validate_plan(plan)[1]) == 2
    assert plans["kip-gpu-1"]["peers"][0]["endpointPort"] == 50011
    staged = peer_plans(nodes[:2])
    assert set(staged) == {"kip-control", "kip-gpu-1"}
    assert all(len(validate_plan(plan)[1]) == 1 for plan in staged.values())
    with pytest.raises(FleetError, match="first GPU worker"):
        peer_plans([nodes[0], nodes[2]])
    with pytest.raises(FleetError, match="duplicated"):
        peer_plans([nodes[0], nodes[1], replace(nodes[2], instance_id=101)])


def test_owned_plans_require_exact_live_leases_distinct_hosts_and_bound_keys(
    tmp_path: Path,
) -> None:
    first = instance()
    control = instance()
    control.update(
        {
            "id": 102,
            "label": "kip-pilot-test-control",
            "machine_id": 902,
            "host_id": 903,
            "public_ipaddr": "1.1.1.1",
        }
    )
    first_path = tmp_path / "first.json"
    fleet_path = tmp_path / "fleet.json"
    _save_lease(first_path, {"schemaVersion": 1, **lease()})
    _save_lease(
        fleet_path,
        {
            "schemaVersion": 1,
            "managedBy": "kube-inference-platform",
            "firstWorkerInstanceId": 101,
            "nodes": {
                "control": {
                    "phase": "created",
                    "instanceId": 102,
                    "label": "kip-pilot-test-control",
                    "machineId": 902,
                    "hostId": 903,
                    "templateHash": "reviewed-template",
                },
                "gpu2": {"phase": "planned"},
            },
        },
    )
    (tmp_path / "first-worker-wireguard-public.json").write_text(
        json.dumps({"instanceId": 101, "publicKey": KEYS[0]})
    )
    (tmp_path / "control-wireguard-public.json").write_text(
        json.dumps({"instanceId": 102, "publicKey": KEYS[1]})
    )

    class Client:
        def get(self, path: str) -> dict[str, object]:
            assert path == "/instances/"
            return {"instances": [first, control]}

    plans = owned_peer_plans(Client(), first_path, fleet_path, tmp_path)  # type: ignore[arg-type]
    assert set(plans) == {"kip-control", "kip-gpu-1"}
    control["host_id"] = first["host_id"]
    journal = json.loads(fleet_path.read_text())
    journal["nodes"]["control"]["hostId"] = first["host_id"]
    _save_lease(fleet_path, journal)
    with pytest.raises(FleetError, match="distinct physical"):
        owned_peer_plans(Client(), first_path, fleet_path, tmp_path)  # type: ignore[arg-type]
    control["host_id"] = 903
    journal["nodes"]["control"]["hostId"] = 903
    _save_lease(fleet_path, journal)
    (tmp_path / "control-wireguard-public.json").write_text(
        json.dumps({"instanceId": 999, "publicKey": KEYS[1]})
    )
    with pytest.raises(FleetError, match="bound to another VM"):
        owned_peer_plans(Client(), first_path, fleet_path, tmp_path)  # type: ignore[arg-type]
