"""Ownership-safe, sequential Vast VM rental for the pilot's first GPU control."""

# Ubuntu 22.04 control VMs use Python 3.10; datetime.UTC was added in 3.11.
# ruff: noqa: UP017

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .vast import (
    API_BASE,
    MAX_ACTIVE_HOURLY_USD,
    REQUESTED_DISK_GB,
    TEARDOWN_RESERVE_USD,
    FleetError,
    VastReadClient,
    _number,
    _region_group,
    _secret_file,
    parse_offer,
    read_only_plan,
)

VM_TEMPLATE_HASH = "fc393e1a4058a2f2b88e0c0e1a92e540"
VM_TEMPLATE_CREATOR_ID = 525202
VM_IMAGE = "docker.io/vastai/kvm:@vastai-automatic-tag"
LEASE_SCHEMA = 1
VM_SSH_KEY_REPAIR = """#!/bin/bash
set -eu
install -d -m 700 -o root -g root /root/.ssh
if [ -f /root/.ssh/authorized_keys ]; then
  chown root:root /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
fi
chmod g-w,o-w /root
"""


class VastRentalClient(VastReadClient):
    """Minimal mutating client; never renders credentials or response bodies."""

    def __init__(self, key_file: Path) -> None:
        super().__init__(key_file)
        self._rental_key = _secret_file(key_file)

    def get_ssh_keys(self) -> list[dict[str, Any]]:
        request = Request(
            f"{API_BASE}/ssh/",
            headers={"Authorization": f"Bearer {self._rental_key}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed HTTPS origin
                payload = json.load(response)
        except Exception as error:
            raise FleetError("could not inspect registered Vast SSH keys") from error
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise FleetError("Vast SSH key response was malformed")
        return payload

    def create_vm(self, offer_id: int, label: str) -> int:
        """Use Vast's official instance endpoint; a timed-out call is never retried blindly."""
        if offer_id < 1 or not label.startswith("kip-pilot-"):
            raise FleetError("invalid owned VM create request")
        body = {
            "client_id": "me",
            "template_hash_id": VM_TEMPLATE_HASH,
            "disk": REQUESTED_DISK_GB,
            "label": label,
            "env": {"-p 51820:51820/udp": "1"},
            "cancel_unavail": True,
            "vm": True,
            "onstart": VM_SSH_KEY_REPAIR,
        }
        request = Request(
            f"{API_BASE}/asks/{offer_id}/",
            data=json.dumps(body, separators=(",", ":")).encode(),
            headers={
                "Authorization": f"Bearer {self._rental_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="PUT",
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS origin
                payload = json.load(response)
        except Exception as error:
            raise FleetError(
                "Vast VM creation outcome is uncertain; inspect the pending lease and account, "
                "do not repeat the create request"
            ) from error
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise FleetError("Vast did not confirm VM creation; inspect the pending lease")
        instance_id = payload.get("new_contract")
        if isinstance(instance_id, bool) or not isinstance(instance_id, int) or instance_id < 1:
            raise FleetError(
                "Vast creation response lacked an instance ID; inspect the pending lease"
            )
        return instance_id

    def destroy_vm(self, instance_id: int) -> None:
        if instance_id < 1:
            raise FleetError("invalid instance ID for owned VM removal")
        request = Request(
            f"{API_BASE}/instances/{instance_id}/",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {self._rental_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="DELETE",
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS origin
                payload = json.load(response)
        except Exception as error:
            raise FleetError(
                "Vast VM deletion outcome is uncertain; inspect account state"
            ) from error
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise FleetError("Vast did not confirm owned VM deletion; inspect account state")


def _save_lease(path: Path, lease: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(lease, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_lease(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o077:
        raise FleetError("lease file must be regular and mode 0600 or stricter")
    try:
        lease = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FleetError("lease file is unreadable") from error
    if not isinstance(lease, dict) or lease.get("schemaVersion") != LEASE_SCHEMA:
        raise FleetError("lease schema or ownership is unrecognized")
    if lease.get("managedBy") != "kube-inference-platform":
        raise FleetError("lease is not owned by KIP")
    return lease


def _matching_instance(client: VastRentalClient, label: str) -> dict[str, Any] | None:
    response = client.get("/instances/")
    items = response.get("instances")
    if not isinstance(items, list):
        raise FleetError("cannot inspect Vast instances after creation")
    matches = [item for item in items if isinstance(item, dict) and item.get("label") == label]
    if len(matches) > 1:
        raise FleetError("multiple Vast instances have the owned lease label")
    return matches[0] if matches else None


def _verify_official_vm_template(client: VastRentalClient) -> None:
    response = client.get(
        "/template/",
        query={
            "select_cols": json.dumps(["*"]),
            "select_filters": json.dumps({"vm": {"eq": True}}),
        },
    )
    templates = response.get("templates")
    if not isinstance(templates, list):
        raise FleetError("cannot verify official Ubuntu VM template")
    matching = [
        item
        for item in templates
        if isinstance(item, dict) and item.get("hash_id") == VM_TEMPLATE_HASH
    ]
    if len(matching) != 1:
        raise FleetError("pinned official Ubuntu VM template is unavailable")
    template = matching[0]
    if (
        template.get("name") != "Ubuntu 22.04 VM"
        or template.get("creator_id") != VM_TEMPLATE_CREATOR_ID
        or template.get("vm") is not True
        or template.get("recommended") is not True
        or template.get("image") != "docker.io/vastai/kvm"
        or template.get("use_ssh") is not True
    ):
        raise FleetError("pinned Ubuntu VM template ownership or capabilities drifted")


@contextmanager
def _lease_lock(lease_path: Path) -> Iterator[None]:
    """Prevent concurrent create attempts in this checkout."""
    import fcntl

    lease_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = lease_path.with_suffix(".lock")
    if lock_path.is_symlink():
        raise FleetError("lease lock must not be a symlink")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def rent_first_worker(
    client: VastRentalClient,
    lease_path: Path,
    local_public_key: Path,
) -> dict[str, Any]:
    """Rent at most one owned VM; uncertain outcomes remain pending, never duplicated."""
    with _lease_lock(lease_path):
        return _rent_first_worker_locked(client, lease_path, local_public_key)


def _rent_first_worker_locked(
    client: VastRentalClient,
    lease_path: Path,
    local_public_key: Path,
) -> dict[str, Any]:
    existing = _load_lease(lease_path)
    if existing and existing.get("phase") == "released":
        if _matching_instance(client, existing["label"]) is not None:
            raise FleetError("released lease still has a Vast instance; refusing new rental")
        old_id = existing.get("instanceId")
        if not isinstance(old_id, int) or old_id < 1:
            raise FleetError("released lease lacks its audited instance ID")
        archived = lease_path.with_name(f"{lease_path.stem}-released-{old_id}.json")
        if archived.exists():
            raise FleetError("released lease audit archive already exists")
        os.replace(lease_path, archived)
        existing = None
    if existing:
        match = _matching_instance(client, existing["label"])
        if match is None:
            raise FleetError(
                "owned lease exists but its VM is absent; inspect before any new rental"
            )
        instance_id = match.get("id")
        if existing.get("instanceId") is not None and instance_id != existing["instanceId"]:
            raise FleetError("owned lease instance ID changed")
        if not isinstance(instance_id, int) or instance_id < 1:
            raise FleetError("owned Vast instance has invalid ID")
        if existing.get("instanceId") is None:
            existing["instanceId"] = instance_id
            existing["phase"] = "created"
            _save_lease(lease_path, existing)
        return existing

    public_parts = local_public_key.read_text(encoding="utf-8").strip().split()
    if len(public_parts) < 2 or not any(
        item.get("public_key", "").split()[:2] == public_parts[:2] for item in client.get_ssh_keys()
    ):
        raise FleetError("this computer's public SSH key is not registered with Vast")
    _verify_official_vm_template(client)
    plan = read_only_plan(client, allow_mixed=True)
    if plan["status"] != "candidate-only" or len(plan["candidates"]) != 3:
        raise FleetError("current Vast credit or offer gates do not allow first-worker rental")
    if plan["existingInstanceCount"] != 0:
        raise FleetError("account contains existing instances; do not infer task ownership")
    worker = plan["candidates"][1]
    label = f"kip-pilot-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:10]}-gpu1"
    lease: dict[str, Any] = {
        "schemaVersion": LEASE_SCHEMA,
        "managedBy": "kube-inference-platform",
        "label": label,
        "phase": "creation-pending",
        "role": "gpu-worker",
        "offerId": worker["offerId"],
        "machineId": worker["machineId"],
        "hostId": worker["hostId"],
        "gpu": worker["gpu"],
        "image": VM_IMAGE,
        "templateHash": VM_TEMPLATE_HASH,
        "requestedDiskGb": REQUESTED_DISK_GB,
        "advertisedHourlyUsd": worker["advertisedHourlyUsd"],
        "createdRequestUtc": datetime.now(timezone.utc).isoformat(),
        "instanceId": None,
    }
    _save_lease(lease_path, lease)
    instance_id = client.create_vm(worker["offerId"], label)
    lease["instanceId"] = instance_id
    lease["phase"] = "created"
    _save_lease(lease_path, lease)
    return lease


def release_first_worker(client: VastRentalClient, lease_path: Path) -> dict[str, Any]:
    """Destroy only the exact KIP-labeled VM recorded in this checkout's lease."""
    with _lease_lock(lease_path):
        lease = _load_lease(lease_path)
        if lease is None:
            raise FleetError("no KIP first-worker lease exists")
        if lease.get("phase") == "released":
            return lease
        instance_id = lease.get("instanceId")
        if not isinstance(instance_id, int) or instance_id < 1:
            raise FleetError("lease lacks a confirmed VM ID; inspect account before deletion")
        match = _matching_instance(client, lease["label"])
        if match is None:
            raise FleetError("owned VM is absent; inspect account before marking released")
        if match.get("id") != instance_id or match.get("machine_id") != lease.get("machineId"):
            raise FleetError("owned VM identity differs from lease; refusing deletion")
        client.destroy_vm(instance_id)
        lease["phase"] = "released"
        lease["releasedUtc"] = datetime.now(timezone.utc).isoformat()
        _save_lease(lease_path, lease)
        return lease


def _verify_lease_instance(client: VastRentalClient, lease: dict[str, Any]) -> dict[str, Any]:
    instance = _matching_instance(client, lease["label"])
    if instance is None or instance.get("id") != lease.get("instanceId"):
        raise FleetError("task-owned VM lease has no matching Vast instance")
    if instance.get("machine_id") != lease.get("machineId") or instance.get(
        "template_hash_id"
    ) != lease.get("templateHash"):
        raise FleetError("task-owned VM machine or template identity differs")
    if lease.get("hostId") is not None and instance.get("host_id") != lease["hostId"]:
        raise FleetError("task-owned VM physical host identity differs")
    if instance.get("actual_status") != "running" or instance.get("cur_state") != "running":
        raise FleetError("task-owned VM is not reported running by Vast")
    return instance


def _refresh_second_worker_offer(
    client: VastRentalClient,
    first_instance: dict[str, Any],
    control_instance: dict[str, Any],
    current_hourly: float,
    excluded_host_ids: set[int] | None = None,
) -> dict[str, Any]:
    """Select a currently rentable, distinct-host worker before the final create call."""
    offer_query = {
        "verified": {"eq": True},
        "external": {"eq": False},
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "vms_enabled": {"eq": True},
        "num_gpus": {"eq": 1},
        "allocated_storage": REQUESTED_DISK_GB,
        "type": "on-demand",
        "limit": 1000,
    }
    response = client.get("/bundles/", query={"q": json.dumps(offer_query)})
    raw = response.get("offers")
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise FleetError("live Vast worker offer list is malformed")
    existing_hosts = {first_instance.get("host_id"), control_instance.get("host_id")}
    existing_hosts.update(excluded_host_ids or set())
    existing_machines = {first_instance.get("machine_id"), control_instance.get("machine_id")}
    first_gpu = first_instance.get("gpu_name")
    first_region = first_instance.get("geolocation")
    control_region = control_instance.get("geolocation")
    candidates = [
        item
        for offer in raw
        if (item := parse_offer(offer, role="worker"))
        and item.host_id not in existing_hosts
        and item.machine_id not in existing_machines
        and current_hourly + item.advertised_hourly_usd <= MAX_ACTIVE_HOURLY_USD
    ]
    if not candidates:
        raise FleetError("no live distinct-host second GPU worker fits the rental cap")
    chosen = min(
        candidates,
        key=lambda item: (
            item.gpu != first_gpu,
            item.region != first_region,
            item.region not in {first_region, control_region},
            len(
                {
                    _region_group(region)
                    for region in (item.region, first_region, control_region)
                    if isinstance(region, str)
                }
            ),
            not item.static_ip,
            item.advertised_hourly_usd,
            -item.reliability,
        ),
    )
    return {
        "offerId": chosen.offer_id,
        "machineId": chosen.machine_id,
        "hostId": chosen.host_id,
        "advertisedHourlyUsd": chosen.advertised_hourly_usd,
        "gpu": chosen.gpu,
        "region": chosen.region,
    }


def _registered_public_key(client: VastRentalClient, local_public_key: Path) -> None:
    public_parts = local_public_key.read_text(encoding="utf-8").strip().split()
    if len(public_parts) < 2 or not any(
        item.get("public_key", "").split()[:2] == public_parts[:2] for item in client.get_ssh_keys()
    ):
        raise FleetError("this computer's public SSH key is not registered with Vast")


def release_fleet_control(
    client: VastRentalClient, first_lease_path: Path, fleet_lease_path: Path
) -> dict[str, Any]:
    """Release only an exact task-owned control VM; retain the audit journal."""
    with _lease_lock(fleet_lease_path):
        first = _load_lease(first_lease_path)
        journal = _load_lease(fleet_lease_path)
        if first is None or journal is None:
            raise FleetError("task-owned first-worker and fleet journals are required")
        if journal.get("firstWorkerInstanceId") != first.get("instanceId"):
            raise FleetError("fleet journal differs from first-worker ownership")
        nodes = journal.get("nodes")
        if not isinstance(nodes, dict) or set(nodes) != {"control", "gpu2"}:
            raise FleetError("fleet rental journal is malformed")
        target = nodes["control"]
        if target.get("phase") == "released":
            if _matching_instance(client, target["label"]) is not None:
                raise FleetError("released control VM still appears in Vast inventory")
            return journal
        if target.get("phase") != "created" or nodes["gpu2"].get("phase") != "planned":
            raise FleetError("control release requires a confirmed VM and no second rental")
        match = _matching_instance(client, target["label"])
        if match is None or match.get("id") != target.get("instanceId"):
            raise FleetError("control VM identity is absent or differs from journal")
        if match.get("machine_id") != target.get("machineId") or match.get(
            "template_hash_id"
        ) != target.get("templateHash"):
            raise FleetError("control VM physical machine or template differs")
        if target.get("hostId") is not None and match.get("host_id") != target["hostId"]:
            raise FleetError("control VM physical host differs from lease")
        client.destroy_vm(target["instanceId"])
        target["phase"] = "released"
        target["releasedUtc"] = datetime.now(timezone.utc).isoformat()
        journal["phase"] = "released"
        _save_lease(fleet_lease_path, journal)
        return journal


def release_second_worker(
    client: VastRentalClient, first_lease_path: Path, fleet_lease_path: Path
) -> dict[str, Any]:
    """Release an unjoined second worker only after exact live identity checks.

    A worker already marked validated belongs to the active cluster and must be
    removed through the complete fleet teardown, not this startup-retry path.
    """
    with _lease_lock(fleet_lease_path):
        first = _load_lease(first_lease_path)
        journal = _load_lease(fleet_lease_path)
        if (
            first is None
            or journal is None
            or journal.get("firstWorkerInstanceId") != first.get("instanceId")
        ):
            raise FleetError("task-owned first-worker and fleet journals are required")
        nodes = journal.get("nodes")
        if not isinstance(nodes, dict) or set(nodes) != {"control", "gpu2"}:
            raise FleetError("fleet rental journal is malformed")
        target = nodes["gpu2"]
        if target.get("validationStatus") == "passed":
            raise FleetError("validated GPU worker requires full fleet teardown")
        if target.get("phase") == "released":
            if _matching_instance(client, target["label"]) is not None:
                raise FleetError("released GPU worker still appears in Vast inventory")
            return journal
        if target.get("phase") != "created":
            raise FleetError("second-worker release requires a confirmed owned VM")
        match = _matching_instance(client, target["label"])
        if (
            match is None
            or match.get("id") != target.get("instanceId")
            or match.get("machine_id") != target.get("machineId")
            or match.get("template_hash_id") != target.get("templateHash")
            or match.get("host_id") != target.get("hostId")
        ):
            raise FleetError("second-worker VM identity differs from journal")
        excluded = target.setdefault("failedHostIds", [])
        if not isinstance(excluded, list) or any(not isinstance(item, int) for item in excluded):
            raise FleetError("second-worker failed-host audit is malformed")
        client.destroy_vm(target["instanceId"])
        if target["hostId"] not in excluded:
            excluded.append(target["hostId"])
        target["phase"] = "released"
        target["releasedUtc"] = datetime.now(timezone.utc).isoformat()
        _save_lease(fleet_lease_path, journal)
        return journal


def release_complete_fleet(
    client: VastRentalClient, first_lease_path: Path, fleet_lease_path: Path
) -> dict[str, Any]:
    """Release exactly the three journaled pilot VMs, newest worker first.

    A deletion-pending phase makes an uncertain API outcome recoverable without
    ever discovering deletion targets from labels or touching unrelated VMs.
    """
    with _lease_lock(fleet_lease_path):
        first = _load_lease(first_lease_path)
        journal = _load_lease(fleet_lease_path)
        if first is None or journal is None:
            raise FleetError("task-owned first-worker and fleet journals are required")
        nodes = journal.get("nodes")
        if (
            journal.get("firstWorkerInstanceId") != first.get("instanceId")
            or not isinstance(nodes, dict)
            or set(nodes) != {"control", "gpu2"}
            or not isinstance(journal.get("pilotId"), str)
            or not isinstance(first.get("label"), str)
            or journal["pilotId"] != first["label"].removesuffix("-gpu1")
        ):
            raise FleetError("fleet teardown journal differs from first-worker ownership")
        targets = (("gpu2", nodes["gpu2"]), ("control", nodes["control"]), ("gpu1", first))
        reviewed: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
        for role, lease in targets:
            if not isinstance(lease, dict) or lease.get("phase") not in {
                "created",
                "deletion-pending",
                "released",
            }:
                raise FleetError(f"{role} is not an exact confirmed pilot rental")
            instance_id = lease.get("instanceId")
            if isinstance(instance_id, bool) or not isinstance(instance_id, int) or instance_id < 1:
                raise FleetError(f"{role} has no confirmed Vast instance ID")
            label = lease.get("label")
            if not isinstance(label, str) or not label.startswith(journal["pilotId"] + "-"):
                raise FleetError(f"{role} label differs from pilot identity")
            match = _matching_instance(client, label)
            if lease["phase"] == "released":
                if match is not None:
                    raise FleetError(f"released {role} still appears in Vast inventory")
            elif match is None and lease["phase"] != "deletion-pending":
                raise FleetError(f"{role} is absent without a deletion-pending journal")
            elif match is not None and (
                match.get("id") != instance_id
                or match.get("machine_id") != lease.get("machineId")
                or match.get("template_hash_id") != lease.get("templateHash")
                or (lease.get("hostId") is not None and match.get("host_id") != lease.get("hostId"))
            ):
                raise FleetError(f"{role} live identity differs from the task-owned lease")
            reviewed.append((role, lease, match))
        for role, lease, match in reviewed:
            if lease["phase"] == "released":
                continue
            if match is not None:
                live = _matching_instance(client, lease["label"])
                if live is None or any(
                    live.get(field) != match.get(field)
                    for field in ("id", "machine_id", "template_hash_id", "host_id")
                ):
                    raise FleetError(f"{role} identity changed before deletion")
                lease["phase"] = "deletion-pending"
                _save_lease(
                    first_lease_path if role == "gpu1" else fleet_lease_path,
                    first if role == "gpu1" else journal,
                )
                client.destroy_vm(lease["instanceId"])
                for _ in range(5):
                    if _matching_instance(client, lease["label"]) is None:
                        break
                    time.sleep(2)
                else:
                    raise FleetError(
                        f"{role} deletion accepted but VM still appears in Vast inventory; "
                        "recheck the deletion-pending lease"
                    )
            lease["phase"] = "released"
            lease["releasedUtc"] = datetime.now(timezone.utc).isoformat()
            _save_lease(
                first_lease_path if role == "gpu1" else fleet_lease_path,
                first if role == "gpu1" else journal,
            )
        journal["phase"] = "released"
        _save_lease(fleet_lease_path, journal)
        return journal


def rent_next_fleet_node(
    client: VastRentalClient,
    first_lease_path: Path,
    fleet_lease_path: Path,
    local_public_key: Path,
    *,
    role: str,
) -> dict[str, Any]:
    """Rent one additional VM from a frozen, ownership-safe mixed-fleet plan.

    Control plane must be rented and independently validated before gpu2 is
    requested. Pending API outcomes are never repeated blindly.
    """
    if role not in {"control", "gpu2"}:
        raise FleetError("next rental role must be control or gpu2")
    with _lease_lock(fleet_lease_path):
        first = _load_lease(first_lease_path)
        if first is None or first.get("phase") != "created":
            raise FleetError("verified first-worker lease is required")
        first_instance = _verify_lease_instance(client, first)
        journal = _load_lease(fleet_lease_path)
        if journal is not None and journal.get("phase") == "released":
            old_nodes = journal.get("nodes")
            if not isinstance(old_nodes, dict) or set(old_nodes) != {"control", "gpu2"}:
                raise FleetError("released fleet journal is malformed")
            old_control = old_nodes["control"]
            old_id = old_control.get("instanceId")
            if (
                old_control.get("phase") != "released"
                or old_nodes["gpu2"].get("phase") != "planned"
                or not isinstance(old_id, int)
                or old_id < 1
            ):
                raise FleetError("released fleet journal contains unresolved rentals")
            if _matching_instance(client, old_control["label"]) is not None:
                raise FleetError("released control VM still appears in Vast inventory")
            archived = fleet_lease_path.with_name(f"{fleet_lease_path.stem}-released-{old_id}.json")
            if archived.exists():
                raise FleetError("released fleet audit archive already exists")
            os.replace(fleet_lease_path, archived)
            journal = None
        if journal is None:
            if role != "control":
                raise FleetError("control-plane rental must precede second GPU worker")
            _registered_public_key(client, local_public_key)
            _verify_official_vm_template(client)
            plan = read_only_plan(client, allow_mixed=True, first_worker_lease=first)
            if plan["status"] != "candidate-only":
                raise FleetError("live remaining-fleet offer or ownership gates did not pass")
            pilot_id = first["label"].removesuffix("-gpu1")
            if not pilot_id.startswith("kip-pilot-") or pilot_id == first["label"]:
                raise FleetError("first-worker label does not identify the pilot")
            candidates = {item["role"]: item for item in plan["candidates"]}
            journal = {
                "schemaVersion": LEASE_SCHEMA,
                "managedBy": "kube-inference-platform",
                "phase": "planned",
                "pilotId": pilot_id,
                "firstWorkerInstanceId": first["instanceId"],
                "nodes": {
                    "control": {
                        "label": f"{pilot_id}-control",
                        "offerId": candidates["control"]["offerId"],
                        "machineId": candidates["control"]["machineId"],
                        "hostId": candidates["control"]["hostId"],
                        "advertisedHourlyUsd": candidates["control"]["advertisedHourlyUsd"],
                        "templateHash": VM_TEMPLATE_HASH,
                        "phase": "planned",
                        "instanceId": None,
                    },
                    "gpu2": {
                        "label": f"{pilot_id}-gpu2",
                        "offerId": candidates["worker-2"]["offerId"],
                        "machineId": candidates["worker-2"]["machineId"],
                        "hostId": candidates["worker-2"]["hostId"],
                        "advertisedHourlyUsd": candidates["worker-2"]["advertisedHourlyUsd"],
                        "templateHash": VM_TEMPLATE_HASH,
                        "phase": "planned",
                        "instanceId": None,
                    },
                },
            }
            _save_lease(fleet_lease_path, journal)
        if (
            journal.get("pilotId") != first["label"].removesuffix("-gpu1")
            or journal.get("firstWorkerInstanceId") != first["instanceId"]
        ):
            raise FleetError("frozen fleet rental journal differs from first-worker ownership")
        nodes = journal.get("nodes")
        if not isinstance(nodes, dict) or set(nodes) != {"control", "gpu2"}:
            raise FleetError("frozen fleet rental journal is malformed")
        if role == "gpu2" and nodes["control"].get("phase") != "created":
            raise FleetError("control-plane rental must be confirmed before gpu2")
        if role == "gpu2" and nodes["control"].get("validationStatus") != "passed":
            raise FleetError(
                "control-plane VM and private network must pass live gates before gpu2"
            )
        control_instance = None
        if nodes["control"].get("phase") == "created":
            control_instance = _verify_lease_instance(client, nodes["control"])
        target = nodes[role]
        if role == "gpu2" and target.get("phase") == "released":
            if _matching_instance(client, target["label"]) is not None:
                raise FleetError("released GPU worker still appears in Vast inventory")
            old_id = target.get("instanceId")
            if not isinstance(old_id, int) or old_id < 1:
                raise FleetError("released GPU worker lacks its audited instance ID")
            excluded = target.setdefault("failedHostIds", [])
            if not isinstance(excluded, list) or any(
                not isinstance(item, int) or item < 1 for item in excluded
            ):
                raise FleetError("second-worker failed-host audit is malformed")
            old_host_id = target.get("hostId")
            if not isinstance(old_host_id, int) or old_host_id < 1:
                raise FleetError("released GPU worker lacks its audited physical host")
            if old_host_id not in excluded:
                excluded.append(old_host_id)
            archive = fleet_lease_path.with_name(
                f"{fleet_lease_path.stem}-gpu2-released-{old_id}.json"
            )
            if archive.exists():
                raise FleetError("released GPU worker audit archive already exists")
            _save_lease(archive, journal)
            target.pop("releasedUtc", None)
            target.pop("validationStatus", None)
            target.pop("validationEvidence", None)
            target["phase"] = "planned"
            target["instanceId"] = None
            _save_lease(fleet_lease_path, journal)
        if target.get("phase") == "created":
            _verify_lease_instance(client, target)
            return journal
        if target.get("phase") == "creation-pending":
            match = _matching_instance(client, target["label"])
            if match is None:
                raise FleetError("rental creation outcome is uncertain; do not repeat it")
            if match.get("machine_id") != target["machineId"]:
                raise FleetError("pending rental appeared on a different physical host")
            if target.get("hostId") is not None and match.get("host_id") != target["hostId"]:
                raise FleetError("pending rental appeared on a different Vast host")
            target["instanceId"] = match["id"]
            target["phase"] = "created"
            _save_lease(fleet_lease_path, journal)
            return journal

        if target.get("phase") != "planned":
            raise FleetError("frozen fleet rental phase is unrecognized")
        account = client.get("/users/current/")
        credit = _number(account.get("credit", account.get("balance")), "account credit")
        instances = client.get("/instances/").get("instances")
        if not isinstance(instances, list):
            raise FleetError("cannot inspect active Vast instances")
        permitted = {first["label"]}
        if nodes["control"].get("phase") == "created":
            permitted.add(nodes["control"]["label"])
        if any(
            not isinstance(item, dict) or item.get("label") not in permitted for item in instances
        ):
            raise FleetError("account contains an unrelated or unrecognized Vast instance")
        current_hourly = sum(
            _number(item.get("dph_total"), "instance billed rate") for item in instances
        )
        if role == "gpu2":
            if control_instance is None:
                raise FleetError("control-plane instance is unavailable")
            excluded_hosts = target.get("failedHostIds", [])
            if not isinstance(excluded_hosts, list) or any(
                not isinstance(item, int) or item < 1 for item in excluded_hosts
            ):
                raise FleetError("second-worker failed-host audit is malformed")
            target.update(
                _refresh_second_worker_offer(
                    client,
                    first_instance,
                    control_instance,
                    current_hourly,
                    set(excluded_hosts),
                )
            )
            _save_lease(fleet_lease_path, journal)
        proposed_hourly = current_hourly + _number(
            target["advertisedHourlyUsd"], "planned VM hourly rate"
        )
        if proposed_hourly > MAX_ACTIVE_HOURLY_USD:
            raise FleetError("new rental would exceed the $4/hour active cap")
        if credit - TEARDOWN_RESERVE_USD < proposed_hourly * 8:
            raise FleetError("credit cannot cover eight fleet hours plus the $5 reserve")
        target["phase"] = "creation-pending"
        target["createdRequestUtc"] = datetime.now(timezone.utc).isoformat()
        _save_lease(fleet_lease_path, journal)
        instance_id = client.create_vm(target["offerId"], target["label"])
        target["instanceId"] = instance_id
        target["phase"] = "created"
        _save_lease(fleet_lease_path, journal)
        return journal
