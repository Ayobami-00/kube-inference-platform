"""Never duplicate a lease or rent when the first-worker preflight is uncertain."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from fleet.rental import (
    VM_TEMPLATE_HASH,
    _load_lease,
    _save_lease,
    release_complete_fleet,
    release_first_worker,
    release_fleet_control,
    release_second_worker,
    rent_first_worker,
    rent_next_fleet_node,
)
from fleet.vast import FleetError


def offer(
    offer_id: int, machine_id: int, *, gpu: str, region: str, hourly: float
) -> dict[str, Any]:
    return {
        "id": offer_id,
        "machine_id": machine_id,
        "host_id": machine_id,
        "geolocation": region,
        "gpu_name": gpu,
        "num_gpus": 1,
        "gpu_ram": 24576,
        "cpu_cores_effective": 32,
        "cpu_ram": 128000,
        "disk_space": 600,
        "reliability": 0.995,
        "dph_total": hourly,
        "direct_port_count": 32,
        "static_ip": True,
        "vms_enabled": True,
        "verified": True,
        "rentable": True,
    }


class RentalFixture:
    def __init__(self, *, credit: float = 32.97) -> None:
        self.credit = credit
        self.offers = [
            offer(1, 10, gpu="RTX 5060 Ti", region="United Kingdom, GB", hourly=0.26),
            offer(2, 11, gpu="RTX 4090", region="Switzerland, CH", hourly=0.63),
            offer(3, 12, gpu="RTX 3090", region="United Kingdom, GB", hourly=0.48),
        ]
        self.offers[0]["gpu_ram"] = 16384
        self.created: list[dict[str, Any]] = []
        self.instances: list[dict[str, Any]] = []
        self.registered = True
        self.uncertain = False
        self.destroyed: list[int] = []

    def get(self, path: str, *, query: dict[str, str] | None = None) -> dict[str, Any]:
        if path == "/users/current/":
            return {"credit": self.credit}
        if path == "/instances/":
            return {"instances": self.instances}
        if path == "/template/":
            return {
                "templates": [
                    {
                        "hash_id": "fc393e1a4058a2f2b88e0c0e1a92e540",
                        "name": "Ubuntu 22.04 VM",
                        "creator_id": 525202,
                        "vm": True,
                        "recommended": True,
                        "image": "docker.io/vastai/kvm",
                        "use_ssh": True,
                    }
                ]
            }
        assert path == "/bundles/" and query
        return {"offers": self.offers}

    def get_ssh_keys(self) -> list[dict[str, str]]:
        return [{"public_key": "ssh-ed25519 fixture-key"}] if self.registered else []

    def create_vm(self, offer_id: int, label: str) -> int:
        self.created.append({"offer": offer_id, "label": label})
        if self.uncertain:
            raise FleetError("uncertain creation outcome")
        self.instances.append({"id": 101, "label": label, "machine_id": 11})
        return 101

    def destroy_vm(self, instance_id: int) -> None:
        self.destroyed.append(instance_id)
        self.instances = [item for item in self.instances if item.get("id") != instance_id]


def public_key(tmp_path: Path) -> Path:
    path = tmp_path / "id_ed25519.pub"
    path.write_text("ssh-ed25519 fixture-key local\n", encoding="utf-8")
    return path


def test_first_worker_rent_is_owned_and_idempotent(tmp_path: Path) -> None:
    client = RentalFixture()
    lease_path = tmp_path / "work" / "first-worker-lease.json"
    key = public_key(tmp_path)
    lease = rent_first_worker(client, lease_path, key)  # type: ignore[arg-type]
    assert lease["phase"] == "created"
    assert lease["offerId"] == 2
    assert lease["instanceId"] == 101
    assert lease["label"].startswith("kip-pilot-")
    assert lease["requestedDiskGb"] == 320
    assert os.stat(lease_path).st_mode & 0o777 == 0o600
    assert rent_first_worker(client, lease_path, key) == lease  # type: ignore[arg-type]
    assert len(client.created) == 1


def test_uncertain_create_is_not_retried(tmp_path: Path) -> None:
    client = RentalFixture()
    client.uncertain = True
    lease_path = tmp_path / "work" / "lease.json"
    key = public_key(tmp_path)
    with pytest.raises(FleetError, match="uncertain"):
        rent_first_worker(client, lease_path, key)  # type: ignore[arg-type]
    assert _load_lease(lease_path)["phase"] == "creation-pending"  # type: ignore[index]
    with pytest.raises(FleetError, match="lease exists"):
        rent_first_worker(client, lease_path, key)  # type: ignore[arg-type]
    assert len(client.created) == 1
    client.instances.append({"id": 109, "label": client.created[0]["label"]})
    recovered = rent_first_worker(client, lease_path, key)  # type: ignore[arg-type]
    assert recovered["instanceId"] == 109
    assert len(client.created) == 1


def test_refuses_missing_key_low_credit_or_existing_instances(tmp_path: Path) -> None:
    key = public_key(tmp_path)
    client = RentalFixture()
    client.registered = False
    with pytest.raises(FleetError, match="public SSH key"):
        rent_first_worker(client, tmp_path / "missing-key.json", key)  # type: ignore[arg-type]
    client.registered = True
    client.credit = 5
    with pytest.raises(FleetError, match="credit or offer"):
        rent_first_worker(client, tmp_path / "low-credit.json", key)  # type: ignore[arg-type]
    client.credit = 32.97
    client.instances.append(
        {"id": 888, "label": "unrelated", "actual_status": "running", "dph_total": 0.2}
    )
    with pytest.raises(FleetError, match="existing instances"):
        rent_first_worker(client, tmp_path / "unrelated.json", key)  # type: ignore[arg-type]
    assert not client.created


def test_lease_cannot_be_replaced_by_foreign_state(tmp_path: Path) -> None:
    lease_path = tmp_path / "lease.json"
    lease_path.write_text(
        json.dumps({"schemaVersion": 1, "managedBy": "someone-else", "label": "foreign"}),
        encoding="utf-8",
    )
    os.chmod(lease_path, 0o600)
    with pytest.raises(FleetError, match="not owned"):
        rent_first_worker(RentalFixture(), lease_path, public_key(tmp_path))  # type: ignore[arg-type]


def test_first_worker_cleanup_requires_exact_identity(tmp_path: Path) -> None:
    client = RentalFixture()
    lease_path = tmp_path / "lease.json"
    rent_first_worker(client, lease_path, public_key(tmp_path))  # type: ignore[arg-type]
    client.instances[0]["machine_id"] = 999
    with pytest.raises(FleetError, match="identity differs"):
        release_first_worker(client, lease_path)  # type: ignore[arg-type]
    assert not client.destroyed
    client.instances[0]["machine_id"] = 11
    released = release_first_worker(client, lease_path)  # type: ignore[arg-type]
    assert released["phase"] == "released"
    assert client.destroyed == [101]
    assert release_first_worker(client, lease_path) == released  # type: ignore[arg-type]
    assert client.destroyed == [101]
    retry = rent_first_worker(client, lease_path, public_key(tmp_path))  # type: ignore[arg-type]
    assert retry["instanceId"] == 101
    assert retry["label"] != released["label"]
    assert (tmp_path / "lease-released-101.json").exists()


class FleetRentalFixture(RentalFixture):
    def __init__(self, *, credit: float = 50) -> None:
        super().__init__(credit=credit)
        self.instances = [
            {
                "id": 101,
                "label": "kip-pilot-20261012-fixture-gpu1",
                "machine_id": 11,
                "host_id": 11,
                "template_hash_id": VM_TEMPLATE_HASH,
                "actual_status": "running",
                "cur_state": "running",
                "gpu_name": "RTX 4090",
                "gpu_ram": 24576,
                "cpu_cores_effective": 32,
                "cpu_ram": 128000,
                "disk_space": 320,
                "geolocation": "Switzerland, CH",
                "dph_total": 0.63,
            }
        ]

    def create_vm(self, offer_id: int, label: str) -> int:
        selected = next(item for item in self.offers if item["id"] == offer_id)
        instance_id = 102 + len(self.created)
        self.created.append({"offer": offer_id, "label": label})
        if self.uncertain:
            raise FleetError("uncertain creation outcome")
        self.instances.append(
            {
                "id": instance_id,
                "label": label,
                "machine_id": selected["machine_id"],
                "host_id": selected["host_id"],
                "template_hash_id": VM_TEMPLATE_HASH,
                "actual_status": "running",
                "cur_state": "running",
                "dph_total": selected["dph_total"],
            }
        )
        return instance_id


def first_fleet_lease(tmp_path: Path) -> Path:
    path = tmp_path / "first.json"
    _save_lease(
        path,
        {
            "schemaVersion": 1,
            "managedBy": "kube-inference-platform",
            "phase": "created",
            "label": "kip-pilot-20261012-fixture-gpu1",
            "instanceId": 101,
            "machineId": 11,
            "templateHash": VM_TEMPLATE_HASH,
            "offerId": 2,
            "gpu": "RTX 4090",
        },
    )
    return path


def test_fleet_rent_is_staged_and_idempotent(tmp_path: Path) -> None:
    client = FleetRentalFixture()
    first = first_fleet_lease(tmp_path)
    journal_path = tmp_path / "fleet.json"
    key = public_key(tmp_path)
    control = rent_next_fleet_node(client, first, journal_path, key, role="control")  # type: ignore[arg-type]
    assert control["nodes"]["control"]["phase"] == "created"
    assert control["nodes"]["gpu2"]["phase"] == "planned"
    assert len(client.created) == 1
    assert os.stat(journal_path).st_mode & 0o777 == 0o600
    assert rent_next_fleet_node(client, first, journal_path, key, role="control") == control  # type: ignore[arg-type]
    assert len(client.created) == 1
    with pytest.raises(FleetError, match="private network must pass"):
        rent_next_fleet_node(client, first, journal_path, key, role="gpu2")  # type: ignore[arg-type]
    control["nodes"]["control"]["validationStatus"] = "passed"
    _save_lease(journal_path, control)
    complete = rent_next_fleet_node(client, first, journal_path, key, role="gpu2")  # type: ignore[arg-type]
    assert complete["nodes"]["gpu2"]["phase"] == "created"
    assert len(client.created) == 2


def test_second_worker_offer_is_refreshed_after_control_validation(tmp_path: Path) -> None:
    client = FleetRentalFixture()
    first = first_fleet_lease(tmp_path)
    journal_path = tmp_path / "fleet.json"
    key = public_key(tmp_path)
    journal = rent_next_fleet_node(client, first, journal_path, key, role="control")  # type: ignore[arg-type]
    journal["nodes"]["control"]["validationStatus"] = "passed"
    _save_lease(journal_path, journal)
    client.offers = [
        candidate
        for candidate in client.offers
        if candidate["id"] != journal["nodes"]["gpu2"]["offerId"]
    ]
    client.offers.append(offer(4, 13, gpu="RTX 4090", region="Switzerland, CH", hourly=0.72))
    complete = rent_next_fleet_node(client, first, journal_path, key, role="gpu2")  # type: ignore[arg-type]
    assert complete["nodes"]["gpu2"]["offerId"] == 4
    assert complete["nodes"]["gpu2"]["hostId"] == 13
    assert client.created[-1]["offer"] == 4


def complete_fleet_fixture(tmp_path: Path) -> tuple[FleetRentalFixture, Path, Path]:
    client = FleetRentalFixture()
    first = first_fleet_lease(tmp_path)
    journal_path = tmp_path / "fleet.json"
    journal = rent_next_fleet_node(
        client, first, journal_path, public_key(tmp_path), role="control"
    )
    journal["nodes"]["control"]["validationStatus"] = "passed"
    _save_lease(journal_path, journal)
    rent_next_fleet_node(client, first, journal_path, public_key(tmp_path), role="gpu2")
    return client, first, journal_path


def test_complete_fleet_release_is_owned_ordered_and_idempotent(tmp_path: Path) -> None:
    client, first, journal_path = complete_fleet_fixture(tmp_path)
    journal = _load_lease(journal_path)
    assert journal is not None
    gpu2_id = journal["nodes"]["gpu2"]["instanceId"]
    control_id = journal["nodes"]["control"]["instanceId"]
    client.instances.append({"id": 999, "label": "unrelated", "machine_id": 999})
    released = release_complete_fleet(client, first, journal_path)  # type: ignore[arg-type]
    assert client.destroyed == [gpu2_id, control_id, 101]
    assert released["phase"] == "released"
    assert all(item["phase"] == "released" for item in released["nodes"].values())
    assert _load_lease(first)["phase"] == "released"  # type: ignore[index]
    assert client.instances == [{"id": 999, "label": "unrelated", "machine_id": 999}]
    assert release_complete_fleet(client, first, journal_path) == released  # type: ignore[arg-type]
    assert client.destroyed == [gpu2_id, control_id, 101]


def test_complete_fleet_release_refuses_identity_drift_before_any_delete(
    tmp_path: Path,
) -> None:
    client, first, journal_path = complete_fleet_fixture(tmp_path)
    journal = _load_lease(journal_path)
    assert journal is not None
    gpu2_id = journal["nodes"]["gpu2"]["instanceId"]
    next(item for item in client.instances if item["id"] == gpu2_id)["machine_id"] = 999
    with pytest.raises(FleetError, match="live identity differs"):
        release_complete_fleet(client, first, journal_path)  # type: ignore[arg-type]
    assert client.destroyed == []


def test_complete_fleet_release_recovers_uncertain_delete(tmp_path: Path) -> None:
    client, first, journal_path = complete_fleet_fixture(tmp_path)
    original_destroy = client.destroy_vm
    first_call = True

    def uncertain_destroy(instance_id: int) -> None:
        nonlocal first_call
        original_destroy(instance_id)
        if first_call:
            first_call = False
            raise FleetError("uncertain deletion outcome")

    client.destroy_vm = uncertain_destroy  # type: ignore[method-assign]
    with pytest.raises(FleetError, match="uncertain"):
        release_complete_fleet(client, first, journal_path)  # type: ignore[arg-type]
    journal = _load_lease(journal_path)
    assert journal is not None
    assert journal["nodes"]["gpu2"]["phase"] == "deletion-pending"
    released = release_complete_fleet(client, first, journal_path)  # type: ignore[arg-type]
    assert released["phase"] == "released"
    assert len(client.destroyed) == 3


def test_unjoined_second_worker_release_is_exact_and_retry_is_audited(tmp_path: Path) -> None:
    client = FleetRentalFixture()
    first = first_fleet_lease(tmp_path)
    journal_path = tmp_path / "fleet.json"
    key = public_key(tmp_path)
    journal = rent_next_fleet_node(client, first, journal_path, key, role="control")  # type: ignore[arg-type]
    journal["nodes"]["control"]["validationStatus"] = "passed"
    _save_lease(journal_path, journal)
    journal = rent_next_fleet_node(client, first, journal_path, key, role="gpu2")  # type: ignore[arg-type]
    old_id = journal["nodes"]["gpu2"]["instanceId"]
    vm = next(item for item in client.instances if item["id"] == old_id)
    vm["host_id"] = 999
    with pytest.raises(FleetError, match="identity differs"):
        release_second_worker(client, first, journal_path)  # type: ignore[arg-type]
    assert not client.destroyed
    vm["host_id"] = journal["nodes"]["gpu2"]["hostId"]
    vm["actual_status"] = "created"  # A failed VM need not reach SSH-ready state.
    released = release_second_worker(client, first, journal_path)  # type: ignore[arg-type]
    assert released["nodes"]["gpu2"]["phase"] == "released"
    assert client.destroyed == [old_id]
    assert release_second_worker(client, first, journal_path) == released  # type: ignore[arg-type]
    old_host = released["nodes"]["gpu2"]["hostId"]
    client.offers.append(offer(4, 13, gpu="RTX 4090", region="Switzerland, CH", hourly=0.72))
    renewed = rent_next_fleet_node(client, first, journal_path, key, role="gpu2")  # type: ignore[arg-type]
    assert renewed["nodes"]["gpu2"]["phase"] == "created"
    assert renewed["nodes"]["gpu2"]["instanceId"] != old_id
    assert renewed["nodes"]["gpu2"]["hostId"] != old_host
    assert old_host in renewed["nodes"]["gpu2"]["failedHostIds"]
    assert (tmp_path / f"fleet-gpu2-released-{old_id}.json").exists()


def test_validated_second_worker_cannot_use_startup_release(tmp_path: Path) -> None:
    client = FleetRentalFixture()
    first = first_fleet_lease(tmp_path)
    journal_path = tmp_path / "fleet.json"
    key = public_key(tmp_path)
    journal = rent_next_fleet_node(client, first, journal_path, key, role="control")  # type: ignore[arg-type]
    journal["nodes"]["control"]["validationStatus"] = "passed"
    _save_lease(journal_path, journal)
    journal = rent_next_fleet_node(client, first, journal_path, key, role="gpu2")  # type: ignore[arg-type]
    journal["nodes"]["gpu2"]["validationStatus"] = "passed"
    _save_lease(journal_path, journal)
    with pytest.raises(FleetError, match="full fleet teardown"):
        release_second_worker(client, first, journal_path)  # type: ignore[arg-type]
    assert not client.destroyed


def test_fleet_expansion_requires_runway_and_never_repeats_uncertain_create(
    tmp_path: Path,
) -> None:
    client = FleetRentalFixture(credit=12)
    first = first_fleet_lease(tmp_path)
    journal_path = tmp_path / "fleet.json"
    key = public_key(tmp_path)
    with pytest.raises(FleetError, match="eight fleet hours"):
        rent_next_fleet_node(client, first, journal_path, key, role="control")  # type: ignore[arg-type]
    assert not client.created
    client.credit = 32.97
    client.uncertain = True
    with pytest.raises(FleetError, match="uncertain"):
        rent_next_fleet_node(client, first, journal_path, key, role="control")  # type: ignore[arg-type]
    assert _load_lease(journal_path)["nodes"]["control"]["phase"] == "creation-pending"  # type: ignore[index]
    with pytest.raises(FleetError, match="do not repeat"):
        rent_next_fleet_node(client, first, journal_path, key, role="control")  # type: ignore[arg-type]
    assert len(client.created) == 1


def test_released_control_is_audited_and_new_plan_is_fresh(tmp_path: Path) -> None:
    client = FleetRentalFixture()
    first = first_fleet_lease(tmp_path)
    journal_path = tmp_path / "fleet.json"
    key = public_key(tmp_path)
    original = rent_next_fleet_node(client, first, journal_path, key, role="control")  # type: ignore[arg-type]
    old_id = original["nodes"]["control"]["instanceId"]
    released = release_fleet_control(client, first, journal_path)  # type: ignore[arg-type]
    assert released["nodes"]["control"]["phase"] == "released"
    assert client.destroyed == [old_id]
    assert release_fleet_control(client, first, journal_path) == released  # type: ignore[arg-type]
    client.offers[0]["host_id"] = 11
    with pytest.raises(FleetError, match="offer or ownership gates"):
        rent_next_fleet_node(client, first, journal_path, key, role="control")  # type: ignore[arg-type]
    assert (tmp_path / f"fleet-released-{old_id}.json").exists()
    assert not journal_path.exists()
    client.offers[0]["host_id"] = 10
    renewed = rent_next_fleet_node(client, first, journal_path, key, role="control")  # type: ignore[arg-type]
    assert renewed["nodes"]["control"]["instanceId"] != old_id
    assert len(client.created) == 2
