"""Read-only Vast offer and account inspection with fail-closed rental filtering."""

from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://cloud.vast.ai/api/v0"
MAX_ACTIVE_HOURLY_USD = 4.0
TOTAL_BILLED_CAP_USD = 50.0
TEARDOWN_RESERVE_USD = 5.0
# Request enough decimal GB to retain >=250 GiB usable after the VM image and
# filesystem overhead. The marketplace query prices this same requested size.
REQUESTED_DISK_GB = 320
EUROPE_COUNTRY_CODES = frozenset(
    {
        "AT",
        "BE",
        "BG",
        "CH",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GB",
        "GR",
        "HR",
        "HU",
        "IE",
        "IS",
        "IT",
        "LT",
        "LU",
        "LV",
        "NL",
        "NO",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
    }
)


class FleetError(Exception):
    """An offer, credential, or account preflight failure."""


@dataclass(frozen=True)
class Candidate:
    """Only non-sensitive marketplace properties needed for selection."""

    offer_id: int
    machine_id: int
    host_id: int
    region: str
    gpu: str
    gpu_ram_gb: float
    cpu: float
    ram_gib: float
    disk_gib: float
    reliability: float
    advertised_hourly_usd: float
    direct_ports: int
    static_ip: bool


def _region_group(region: str) -> str:
    """Use geography only as a planning preference, never as network acceptance."""
    country = region.rsplit(", ", 1)[-1]
    return "europe" if country in EUROPE_COUNTRY_CODES else country


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FleetError(f"missing or invalid offer field: {name}")
    result = float(value)
    if not math.isfinite(result):
        raise FleetError(f"non-finite offer field: {name}")
    return result


def _secret_file(path: Path) -> str:
    if path.is_symlink():
        raise FleetError("Vast key file must not be a symlink")
    try:
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise FleetError("Vast key file must be regular and mode 0600 or stricter")
        key = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise FleetError("Vast key file is unavailable") from error
    if not key or any(character.isspace() for character in key):
        raise FleetError("Vast key file must contain one non-empty token")
    return key


class VastReadClient:
    """Read-only REST client; no mutating HTTP method is implemented."""

    def __init__(self, key_file: Path) -> None:
        self._key = _secret_file(key_file)

    def get(self, path: str, *, query: dict[str, str] | None = None) -> dict[str, Any]:
        if not path.startswith("/") or ".." in path or "?" in path:
            raise FleetError("invalid Vast API path")
        url = f"{API_BASE}{path}"
        if query:
            url += "?" + urlencode(query)
        request = Request(
            url,
            headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed HTTPS origin
                payload = json.load(response)
        except Exception as error:
            # Never include urllib exception text; it may contain request details.
            raise FleetError("Vast read-only API request failed") from error
        if not isinstance(payload, dict):
            raise FleetError("Vast API returned an unexpected response")
        return payload


def parse_offer(raw: dict[str, Any], *, role: str) -> Candidate | None:
    """Reject incomplete offers rather than guessing at units or VM capabilities."""
    try:
        offer_id = int(_number(raw.get("id"), "id"))
        machine_id = int(_number(raw.get("machine_id"), "machine_id"))
        host_id = int(_number(raw.get("host_id"), "host_id"))
        gpu = raw.get("gpu_name")
        region = raw.get("geolocation")
        if not isinstance(gpu, str) or not gpu or not isinstance(region, str) or not region:
            return None
        if raw.get("verified") is not True and raw.get("verification") != "verified":
            return None
        if raw.get("vms_enabled") is not True:
            return None
        if raw.get("rentable") is False:
            return None
        if _number(raw.get("num_gpus"), "num_gpus") != 1:
            return None
        # Vast's available GPU memory can be a few MiB below a card's nominal
        # size (for example 24564 for a 24-GB-class RTX 4090). The pilot GPU
        # floor is decimal GB; system RAM/disk floors are explicitly GiB.
        gpu_ram_gb = _number(raw.get("gpu_ram"), "gpu_ram") / 1000
        cpu = _number(raw.get("cpu_cores_effective"), "cpu_cores_effective")
        ram_gib = _number(raw.get("cpu_ram"), "cpu_ram") / 1073.741824
        disk_gib = _number(raw.get("disk_space"), "disk_space") / 1.073741824
        reliability = _number(raw.get("reliability"), "reliability")
        hourly = _number(raw.get("dph_total"), "dph_total")
        ports = int(_number(raw.get("direct_port_count"), "direct_port_count"))
        static_ip = raw.get("static_ip") is True
    except (FleetError, TypeError, ValueError):
        return None
    if offer_id <= 0 or machine_id <= 0 or host_id <= 0 or hourly <= 0 or reliability < 0.98:
        return None
    if disk_gib < 250 or ports < 4:
        return None
    if role == "worker" and (gpu_ram_gb < 24 or cpu < 16 or ram_gib < 64):
        return None
    if role == "control" and (cpu < 8 or ram_gib < 32):
        return None
    if role not in {"worker", "control"}:
        raise FleetError("unknown fleet role")
    return Candidate(
        offer_id,
        machine_id,
        host_id,
        region,
        gpu,
        gpu_ram_gb,
        cpu,
        ram_gib,
        disk_gib,
        reliability,
        hourly,
        ports,
        static_ip,
    )


def select_three_node_pilot(
    raw_offers: list[dict[str, Any]], *, allow_mixed: bool = False
) -> tuple[Candidate, ...]:
    """Prefer matching regional workers; mixed geography/GPU needs explicit opt-in."""
    workers = [item for raw in raw_offers if (item := parse_offer(raw, role="worker"))]
    controls = [item for raw in raw_offers if (item := parse_offer(raw, role="control"))]
    possibilities: list[tuple[tuple[float, ...], tuple[Candidate, ...]]] = []
    for first_index, first in enumerate(workers):
        for second in workers[first_index + 1 :]:
            matching_workers = first.gpu == second.gpu and first.region == second.region
            if not matching_workers and not allow_mixed:
                continue
            if first.machine_id == second.machine_id or first.host_id == second.host_id:
                continue
            for control in controls:
                if not allow_mixed and control.region != first.region:
                    continue
                same_machine = control.machine_id in {first.machine_id, second.machine_id}
                same_host = control.host_id in {first.host_id, second.host_id}
                if same_machine or same_host:
                    continue
                total = sum(node.advertised_hourly_usd for node in (first, second, control))
                if total > MAX_ACTIVE_HOURLY_USD:
                    continue
                topology_tier = (
                    0
                    if matching_workers and control.region == first.region
                    else 1
                    if first.region == second.region == control.region
                    else 2
                    if first.region == second.region
                    else 3
                    if control.region in {first.region, second.region}
                    else 4
                )
                preferred = 0 if "A100" in first.gpu.upper() and "A100" in second.gpu.upper() else 1
                ranking = (
                    topology_tier,
                    preferred,
                    len({_region_group(item.region) for item in (control, first, second)}),
                    sum(not item.static_ip for item in (control, first, second)),
                    total,
                    -min(first.reliability, second.reliability),
                )
                possibilities.append((ranking, (control, first, second)))
    if not possibilities:
        raise FleetError("no matching three-node pilot meets region, host, or hourly gates")
    return min(possibilities, key=lambda item: item[0])[1]


def _owned_first_worker(
    instances: list[dict[str, Any]], lease: dict[str, Any]
) -> tuple[Candidate, int, str]:
    """Turn only the exact lease-bound instance into a planning constraint."""
    if lease.get("managedBy") != "kube-inference-platform" or lease.get("phase") != "created":
        raise FleetError("first-worker lease is not an active KIP-owned rental")
    identity = lease.get("instanceId")
    label = lease.get("label")
    if not isinstance(identity, int) or identity < 1 or not isinstance(label, str):
        raise FleetError("first-worker lease has invalid identity")
    matches = [item for item in instances if item.get("id") == identity]
    if len(matches) != 1 or matches[0].get("label") != label:
        raise FleetError("first-worker lease does not match exactly one Vast instance")
    instance = matches[0]
    if instance.get("machine_id") != lease.get("machineId") or instance.get(
        "template_hash_id"
    ) != lease.get("templateHash"):
        raise FleetError("first-worker machine or Ubuntu VM template drifted")
    if lease.get("hostId") is not None and instance.get("host_id") != lease["hostId"]:
        raise FleetError("first-worker physical host identity drifted")
    host_id = int(_number(instance.get("host_id"), "first-worker host ID"))
    if host_id < 1:
        raise FleetError("first-worker physical host ID is invalid")
    gpu = instance.get("gpu_name")
    region = instance.get("geolocation")
    if gpu != lease.get("gpu") or not isinstance(region, str) or not region:
        raise FleetError("first-worker GPU or region is unavailable")
    hourly = _number(instance.get("dph_total"), "first-worker billed rate")
    gpu_ram = _number(instance.get("gpu_ram"), "first-worker GPU memory") / 1000
    cpu = _number(instance.get("cpu_cores_effective"), "first-worker CPU")
    ram = _number(instance.get("cpu_ram"), "first-worker RAM") / 1073.741824
    disk = _number(instance.get("disk_space"), "first-worker disk") / 1.073741824
    if hourly <= 0 or gpu_ram < 24 or cpu < 16 or ram < 64 or disk < 250:
        raise FleetError("first-worker billed price or hardware no longer meets pilot gates")
    first = Candidate(
        offer_id=int(lease["offerId"]),
        machine_id=int(lease["machineId"]),
        host_id=host_id,
        region=region,
        gpu=gpu,
        gpu_ram_gb=gpu_ram,
        cpu=cpu,
        ram_gib=ram,
        disk_gib=disk,
        reliability=1.0,
        advertised_hourly_usd=hourly,
        direct_ports=0,
        static_ip=False,
    )
    return first, identity, str(instance.get("actual_status", "unknown"))


def select_remaining_pilot(
    raw_offers: list[dict[str, Any]], first: Candidate, *, allow_mixed: bool
) -> tuple[Candidate, Candidate, Candidate]:
    """Keep the rented first worker, then choose one control and one other host."""
    workers = [item for raw in raw_offers if (item := parse_offer(raw, role="worker"))]
    controls = [item for raw in raw_offers if (item := parse_offer(raw, role="control"))]
    possibilities: list[tuple[tuple[float, ...], tuple[Candidate, Candidate, Candidate]]] = []
    for second in workers:
        if second.machine_id == first.machine_id or second.host_id == first.host_id:
            continue
        match = second.gpu == first.gpu and second.region == first.region
        if not match and not allow_mixed:
            continue
        for control in controls:
            if control.machine_id in {first.machine_id, second.machine_id} or control.host_id in {
                first.host_id,
                second.host_id,
            }:
                continue
            if not allow_mixed and control.region != first.region:
                continue
            total = sum(item.advertised_hourly_usd for item in (first, second, control))
            if total > MAX_ACTIVE_HOURLY_USD:
                continue
            ranking = (
                0 if match else 1,
                len({_region_group(item.region) for item in (first, second, control)}),
                len({first.region, second.region, control.region}),
                0 if control.region == first.region else 1,
                total,
                -min(second.reliability, control.reliability),
            )
            possibilities.append((ranking, (control, first, second)))
    if not possibilities:
        raise FleetError("no second worker/control offers fit the owned first worker")
    return min(possibilities, key=lambda item: item[0])[1]


def read_only_plan(
    client: VastReadClient,
    *,
    allow_mixed: bool = False,
    first_worker_lease: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Inspect availability and account headroom; never create a rental."""
    account = client.get("/users/current/")
    credit_value = account.get("credit")
    if credit_value is None:
        credit_value = account.get("balance")
    available_credit = _number(credit_value, "account credit")
    existing = client.get("/instances/")
    existing_items = existing.get("instances")
    if not isinstance(existing_items, list):
        raise FleetError("cannot inspect existing Vast instances")
    if any(not isinstance(item, dict) for item in existing_items):
        raise FleetError("Vast instance inventory is malformed")
    first_worker = None
    first_worker_id = None
    first_worker_status = None
    if first_worker_lease is not None:
        first_worker, first_worker_id, first_worker_status = _owned_first_worker(
            existing_items, first_worker_lease
        )
        if any(item.get("id") != first_worker_id for item in existing_items):
            raise FleetError("account has instances outside the first-worker lease")
    active_hourly = 0.0
    for item in existing_items:
        # A Vast instance can remain rented/billable even while its reported
        # status is offline. Count every listed instance until release is
        # confirmed rather than understating the active-spend gate.
        active_hourly += _number(item.get("dph_total"), "existing instance dph_total")
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
    offers = client.get("/bundles/", query={"q": json.dumps(offer_query)})
    raw_offers = offers.get("offers")
    if not isinstance(raw_offers, list) or any(not isinstance(item, dict) for item in raw_offers):
        raise FleetError("Vast offer list is malformed")
    eligible_workers = [raw for raw in raw_offers if parse_offer(raw, role="worker")]
    eligible_controls = [raw for raw in raw_offers if parse_offer(raw, role="control")]
    blockers: list[str] = []
    if available_credit <= TEARDOWN_RESERVE_USD:
        blockers.append("available credit does not exceed the $5 teardown reserve")
    try:
        selected = (
            select_remaining_pilot(raw_offers, first_worker, allow_mixed=allow_mixed)
            if first_worker is not None
            else select_three_node_pilot(raw_offers, allow_mixed=allow_mixed)
        )
    except FleetError:
        selected = ()
        if allow_mixed:
            blockers.append("no distinct-host three-node VM offer set meets hardware/hourly gates")
        else:
            blockers.append("no matching same-region, distinct-host three-node VM offer set")
    hourly = sum(item.advertised_hourly_usd for item in selected)
    if first_worker is not None and first_worker_status != "running":
        blockers.append("owned first worker is not reported running by Vast")
    # A short first-worker control may proceed before the account is topped up
    # to the full pilot ceiling. Never promise the entire pilot from this credit.
    first_worker_hours = 8.0
    first_worker_headroom = (
        selected[1].advertised_hourly_usd * first_worker_hours if selected else 0.0
    )
    if selected and available_credit - TEARDOWN_RESERVE_USD < first_worker_headroom:
        blockers.append("credit cannot cover eight first-worker hours plus the $5 reserve")
    additional_hourly = hourly - (first_worker.advertised_hourly_usd if first_worker else 0)
    if selected and active_hourly + additional_hourly > MAX_ACTIVE_HOURLY_USD:
        blockers.append("existing rentals plus candidates exceed the $4/hour active cap")
    topology = None
    if selected:
        control, first, second = selected
        topology = {
            "matchingWorkerGpu": first.gpu == second.gpu,
            "workersSameRegion": first.region == second.region,
            "allNodesSameRegion": control.region == first.region == second.region,
            "requiresCrossRegionNetworkGate": len({node.region for node in selected}) > 1,
            "dynamicPublicEndpointCount": sum(not node.static_ip for node in selected),
        }
    return {
        "status": "blocked" if blockers else "candidate-only",
        "blockers": blockers,
        "warning": (
            "Marketplace rates are not verified billed rates. VM capability, requested-disk "
            "pricing, UDP/TUN access, dynamic public endpoints, driver control, and account "
            "charges must pass live "
            "gates before rental."
        ),
        "availableCreditUsd": round(available_credit, 2),
        "additionalCreditNeededUsd": round(max(TOTAL_BILLED_CAP_USD - available_credit, 0), 2),
        "firstWorkerEightHourEstimateUsd": round(first_worker_headroom, 2) if selected else None,
        "requestedDiskGb": REQUESTED_DISK_GB,
        "existingInstanceCount": len(existing_items),
        "existingActiveHourlyUsd": round(active_hourly, 4),
        "ownedFirstWorkerInstanceId": first_worker_id,
        "ownedFirstWorkerStatus": first_worker_status,
        "additionalAdvertisedHourlyUsd": round(additional_hourly, 4) if selected else None,
        "eligibleWorkerOfferCount": len(eligible_workers),
        "eligibleControlOfferCount": len(eligible_controls),
        "mixedPlacementAllowed": allow_mixed,
        "topology": topology,
        "pilotAdvertisedHourlyUsd": round(hourly, 4) if selected else None,
        "activeHourlyCapUsd": MAX_ACTIVE_HOURLY_USD,
        "totalBilledCapUsd": TOTAL_BILLED_CAP_USD,
        "teardownThresholdUsd": TOTAL_BILLED_CAP_USD - TEARDOWN_RESERVE_USD,
        "candidates": [
            {
                "role": role,
                "offerId": item.offer_id,
                "machineId": item.machine_id,
                "hostId": item.host_id,
                "region": item.region,
                "gpu": item.gpu,
                "gpuRamGb": round(item.gpu_ram_gb, 1),
                "cpu": item.cpu,
                "ramGiB": round(item.ram_gib, 1),
                "diskGiB": item.disk_gib,
                "reliability": item.reliability,
                "staticPublicIp": item.static_ip,
                "advertisedHourlyUsd": item.advertised_hourly_usd,
                "alreadyRented": first_worker is not None and role == "worker-1",
            }
            for role, item in zip(
                ("control", "worker-1", "worker-2")[: len(selected)], selected, strict=True
            )
        ],
    }


def configured_key_file() -> Path:
    """Locate an ignored, owner-protected key file without printing its content."""
    override = os.environ.get("KIP_VAST_API_KEY_FILE")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[1] / ".secrets" / "vast-api-key"
