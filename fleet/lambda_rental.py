"""Task-owned Lambda leases and conservative, per-minute rental accounting."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from .lambda_cloud import (
    LambdaClient,
    LambdaError,
    Offer,
    adaptive_a100_region,
    parse_offers,
    preferred_region,
    role_candidates,
)

LEASE_PATH = Path("fleet/.work/lambda-lease.json")
BILLING_PATH = Path("fleet/.work/lambda-billing.json")
TARGET_USD = Decimal("300")
ABSOLUTE_USD = Decimal("330")
STAGES = {
    "foundation": ("control-speech", "small-1"),
    "small-pool": ("small-2",),
    "large-pool": ("large",),
    "nvfp4": ("nvfp4",),
    "h100-checkpoint": ("h100-control",),
}
ROLE_ORDER = tuple(role for roles in STAGES.values() for role in roles)
_ID = re.compile(r"^[a-zA-Z0-9-]{8,80}$")


def _utc() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_stamp(value: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as error:
        raise LambdaError("owned Lambda lease timestamp is invalid") from error
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
        raise LambdaError("owned Lambda lease timestamp must be UTC")
    return stamp


def _private_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise LambdaError(f"{path} must be a regular mode-0600 file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LambdaError(f"{path} is unreadable or invalid JSON") from error
    if not isinstance(value, dict):
        raise LambdaError(f"{path} must contain a JSON object")
    return value


def _save_private(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Billing:
    tax_percent: Decimal
    storage_usd_per_hour: Decimal
    source: str

    @property
    def multiplier(self) -> Decimal:
        return Decimal(1) + self.tax_percent / 100


def load_billing(path: Path = BILLING_PATH) -> Billing:
    value = _private_json(path)
    if set(value) != {"schemaVersion", "taxPercent", "storageUsdPerHour", "source"}:
        raise LambdaError("Lambda billing configuration has unknown or missing fields")
    if value["schemaVersion"] != 1 or not isinstance(value["source"], str):
        raise LambdaError("Lambda billing configuration version or source is invalid")
    if not value["source"].strip() or len(value["source"]) > 200:
        raise LambdaError("Lambda billing source must identify a verified account charge")
    try:
        tax = Decimal(str(value["taxPercent"]))
        storage = Decimal(str(value["storageUsdPerHour"]))
    except ArithmeticError as error:
        raise LambdaError("Lambda billing amounts are invalid") from error
    if not tax.is_finite() or not 0 <= tax <= 50 or not storage.is_finite() or storage < 0:
        raise LambdaError("Lambda billing amounts are out of range")
    return Billing(tax, storage, value["source"].strip())


def load_lease(path: Path = LEASE_PATH) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    value = _private_json(path)
    if (
        set(value) != {"schemaVersion", "managedBy", "campaignId", "region", "nodes"}
        or value["schemaVersion"] != 1
        or value["managedBy"] != "kube-inference-platform"
        or not isinstance(value["campaignId"], str)
        or not re.fullmatch(r"[0-9a-f]{32}", value["campaignId"])
        or not isinstance(value["region"], str)
        or not isinstance(value["nodes"], dict)
        or set(value["nodes"]) - set(ROLE_ORDER)
    ):
        raise LambdaError("Lambda lease ownership or schema is invalid")
    for role, node in value["nodes"].items():
        if not isinstance(node, dict) or node.get("role") != role:
            raise LambdaError("Lambda lease node identity is invalid")
        if node.get("phase") not in {"launching", "active", "terminating", "terminated"}:
            raise LambdaError("Lambda lease node phase is invalid")
        identity = node.get("instanceId")
        if identity is not None and (not isinstance(identity, str) or not _ID.fullmatch(identity)):
            raise LambdaError("Lambda lease instance ID is invalid")
        if node["phase"] != "launching" and identity is None:
            raise LambdaError("Lambda lease active node lacks instance ID")
        cents = node.get("priceCentsPerHour")
        if type(cents) is not int or cents <= 0:
            raise LambdaError("Lambda lease price is invalid")
        physical_gpus = node.get("expectedPhysicalGpus")
        if physical_gpus is not None and (
            type(physical_gpus) is not int or not 1 <= physical_gpus <= 8
        ):
            raise LambdaError("Lambda lease expected GPU count is invalid")
        if node.get("region") is not None and (
            not isinstance(node["region"], str)
            or not re.fullmatch(r"[a-z0-9-]{2,40}", node["region"])
        ):
            raise LambdaError("Lambda lease node region is invalid")
        _parse_stamp(node.get("createdAt"))
        if node.get("endedAt") is not None:
            _parse_stamp(node["endedAt"])
    return value


def new_lease(region: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "managedBy": "kube-inference-platform",
        "campaignId": uuid.uuid4().hex,
        "region": region,
        "nodes": {},
    }


def _tags(instance: dict[str, Any]) -> dict[str, str]:
    items = instance.get("tags", [])
    if not isinstance(items, list):
        raise LambdaError("Lambda instance tags are malformed")
    result: dict[str, str] = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            result[item["key"]] = item.get("value")
    return result


def matching_instance(
    lease: dict[str, Any], role: str, instances: list[dict[str, Any]]
) -> dict[str, Any] | None:
    node = lease["nodes"][role]
    matches = [
        item
        for item in instances
        if item.get("id") == node.get("instanceId")
        or (
            item.get("name") == node.get("name")
            and _tags(item).get("kip-campaign") == lease["campaignId"]
            and _tags(item).get("kip-role") == role
        )
    ]
    if len(matches) > 1:
        raise LambdaError("multiple Lambda instances match one owned lease role")
    if not matches:
        return None
    item = matches[0]
    if (
        item.get("name") != node.get("name")
        or _tags(item).get("kip-campaign") != lease["campaignId"]
        or _tags(item).get("kip-role") != role
        or not isinstance(item.get("region"), dict)
        or item["region"].get("name") != node.get("region", lease["region"])
        or (node.get("instanceId") is not None and item.get("id") != node["instanceId"])
    ):
        raise LambdaError("Lambda instance ownership or region drift detected")
    return item


def accrued_cost(lease: dict[str, Any], billing: Billing, now: datetime | None = None) -> Decimal:
    now = now or _utc()
    total = Decimal(0)
    first_start: datetime | None = None
    last_end: datetime | None = None
    for node in lease["nodes"].values():
        start = _parse_stamp(node["createdAt"])
        end = _parse_stamp(node["endedAt"]) if node.get("endedAt") else now
        if end < start:
            raise LambdaError("Lambda lease end precedes start")
        minutes = (Decimal(str((end - start).total_seconds())) / 60).to_integral_value(
            rounding=ROUND_CEILING
        )
        total += Decimal(node["priceCentsPerHour"]) / 100 * minutes / 60
        first_start = min(first_start, start) if first_start else start
        last_end = max(last_end, end) if last_end else end
    total *= billing.multiplier
    if first_start and last_end and billing.storage_usd_per_hour:
        minutes = (Decimal(str((last_end - first_start).total_seconds())) / 60).to_integral_value(
            rounding=ROUND_CEILING
        )
        total += billing.storage_usd_per_hour * minutes / 60
    return total.quantize(Decimal("0.01"))


def projected_cost(
    lease: dict[str, Any],
    billing: Billing,
    selected: dict[str, Offer],
    hours: Decimal,
    now: datetime | None = None,
) -> Decimal:
    if hours <= 0 or not hours.is_finite():
        raise LambdaError("projection horizon must be positive")
    active_cents = sum(
        node["priceCentsPerHour"]
        for node in lease["nodes"].values()
        if node["phase"] != "terminated"
    )
    new_cents = sum(
        offer.price_cents_per_hour for role, offer in selected.items() if role not in lease["nodes"]
    )
    return (
        accrued_cost(lease, billing, now)
        + (
            Decimal(active_cents + new_cents) / 100 * billing.multiplier
            + billing.storage_usd_per_hour
        )
        * hours
    ).quantize(Decimal("0.01"))


def budget_gate(projected_usd: Decimal, *, new_work: bool) -> str:
    """Stop new work at $300; reserve $30 for report capture and teardown."""
    if not projected_usd.is_finite() or projected_usd < 0:
        raise LambdaError("projected Lambda cost is invalid")
    if projected_usd >= ABSOLUTE_USD:
        raise LambdaError(
            "$330 Lambda absolute stop reached; capture results and terminate owned VMs"
        )
    if new_work and projected_usd >= TARGET_USD:
        raise LambdaError("$300 Lambda target reached; no new rental or experiment")
    return "within-budget" if projected_usd < TARGET_USD else "teardown-only"


def candidate_image(images: list[dict[str, Any]], region: str) -> str:
    eligible = [
        item
        for item in images
        if item.get("family") in {"ubuntu-22-04", "ubuntu-24-04"}
        and item.get("architecture") == "x86_64"
        and isinstance(item.get("region"), dict)
        and item["region"].get("name") == region
        and isinstance(item.get("name"), str)
        and re.fullmatch(r"Ubuntu (22|24)\.04(?:\.\d+)? LTS", item["name"])
        and isinstance(item.get("version"), str)
        and isinstance(item.get("id"), str)
    ]
    eligible.sort(key=lambda item: (item["family"] == "ubuntu-24-04", item["version"]))
    if not eligible:
        raise LambdaError("no supported Ubuntu x86-64 Lambda image in selected region")
    return eligible[-1]["id"]


def read_only_plan(
    client: LambdaClient, billing: Billing | None = None, *, lease_path: Path = LEASE_PATH
) -> dict[str, Any]:
    offers = parse_offers(client.instance_types())
    try:
        region, selected = preferred_region(offers)
    except LambdaError:
        region, selected = None, {}
    try:
        adaptive_region, adaptive_selected = adaptive_a100_region(offers)
    except LambdaError:
        adaptive_region, adaptive_selected = None, {}
    image = candidate_image(client.images(), region) if region else None
    keys = client.ssh_keys()
    active_keys = [item.get("name") for item in keys if isinstance(item.get("name"), str)]
    current = client.instances()
    lease = load_lease(lease_path)
    status = "ready" if region and billing and len(active_keys) == 1 else "blocked"
    if lease and region and lease["region"] != region:
        status = "blocked"
    hourly = sum(offer.hourly_usd for offer in selected.values())
    candidate = role_candidates(offers, "paired-small-candidate")
    a10_regions = {
        available_region
        for offer in role_candidates(offers, "control-speech")
        for available_region in offer.regions
    }
    paired_regions = sorted(
        a10_regions
        & {available_region for offer in candidate for available_region in offer.regions}
    )
    return {
        "status": status,
        "region": region,
        "imageId": image,
        "instanceTypes": {role: offer.name for role, offer in selected.items()},
        "baseHourlyUsd": str(hourly) if region else None,
        "billedHourlyEstimateUsd": str((hourly * billing.multiplier).quantize(Decimal("0.01")))
        if billing and region
        else None,
        "capacityByRole": {
            role: sorted({r for offer in role_candidates(offers, role) for r in offer.regions})
            for role in ("control-speech", "small-1", "large")
        },
        "pairedH100Candidate": {
            "availableWithA10Regions": paired_regions,
            "topology": "one two-GPU VM, not two independent workers",
            "workerFailureGate": "unsupported",
        },
        "adaptiveA100Candidate": {
            "region": adaptive_region,
            "instanceTypes": {role: offer.name for role, offer in adaptive_selected.items()},
            "baseHourlyUsd": str(sum(offer.hourly_usd for offer in adaptive_selected.values()))
            if adaptive_region
            else None,
            "secondSmallReplicaPlacement": "8xA100-80 host; not an independent worker",
            "hardwareMatchedH100Claim": False,
        },
        "billingVerified": billing is not None,
        "sshKeyCount": len(active_keys),
        "existingInstanceCount": len(current),
        "ownedInstanceCount": len(lease["nodes"]) if lease else 0,
        "note": "Capacity can disappear; 2x H100 cannot prove cross-worker failure recovery.",
    }
