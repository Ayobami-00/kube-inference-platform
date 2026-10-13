"""Safety tests for read-only Vast offer filtering and budget preflight."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fleet.vast import (
    FleetError,
    _secret_file,
    parse_offer,
    read_only_plan,
    select_three_node_pilot,
)


def offer(
    offer_id: int,
    machine_id: int,
    *,
    gpu: str = "A100 SXM4",
    region: str = "France, FR",
    hourly: float = 1.1,
) -> dict[str, object]:
    return {
        "id": offer_id,
        "machine_id": machine_id,
        "host_id": machine_id,
        "geolocation": region,
        "gpu_name": gpu,
        "num_gpus": 1,
        "gpu_ram": 81920 if "A100" in gpu else 24576,
        "cpu_cores_effective": 32,
        "cpu_ram": 128000,
        "disk_space": 500,
        "reliability": 0.995,
        "dph_total": hourly,
        "direct_port_count": 32,
        "static_ip": True,
        "vms_enabled": True,
        "verified": True,
        "rentable": True,
    }


def test_prefers_matching_a100_distinct_machines() -> None:
    offers = [
        offer(1, 10),
        offer(2, 11),
        offer(3, 12, gpu="RTX 4090", hourly=0.5),
        offer(4, 13, gpu="RTX 4090", hourly=0.5),
        offer(5, 14, gpu="RTX 4060 Ti", hourly=0.3),
    ]
    selected = select_three_node_pilot(offers)
    assert [item.offer_id for item in selected[1:]] == [1, 2]
    assert len({item.machine_id for item in selected}) == 3


def test_rejects_same_machine_and_cross_region() -> None:
    offers = [offer(1, 10), offer(2, 10), offer(3, 11, region="Canada, CA")]
    with pytest.raises(FleetError):
        select_three_node_pilot(offers)


def test_rejects_distinct_machine_ids_on_same_physical_host() -> None:
    offers = [offer(1, 10), offer(2, 11), offer(3, 12)]
    offers[1]["host_id"] = 10
    with pytest.raises(FleetError, match="no matching three-node pilot"):
        select_three_node_pilot(offers)


def test_explicit_mixed_opt_in_retains_distinct_host_and_rate_gates() -> None:
    offers = [
        offer(1, 10, region="France, FR", gpu="A100 SXM4"),
        offer(2, 11, region="Canada, CA", gpu="RTX 4090", hourly=0.6),
        offer(3, 12, region="Canada, CA", gpu="RTX 4060 Ti", hourly=0.3),
    ]
    with pytest.raises(FleetError):
        select_three_node_pilot(offers)
    selected = select_three_node_pilot(offers, allow_mixed=True)
    assert len({item.machine_id for item in selected}) == 3
    assert len({item.region for item in selected}) == 2
    result = read_only_plan(FixtureClient(credit=60), allow_mixed=True)  # type: ignore[arg-type]
    assert result["mixedPlacementAllowed"] is True
    expensive = [offer(1, 10, hourly=2), offer(2, 11, hourly=2), offer(3, 12, hourly=2)]
    with pytest.raises(FleetError):
        select_three_node_pilot(expensive, allow_mixed=True)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gpu_ram", 12288),
        ("cpu_cores_effective", 8),
        ("cpu_ram", 32000),
        ("cpu_ram", 64000),
        ("disk_space", 180),
        ("disk_space", 260),
        ("reliability", 0.9),
        ("direct_port_count", 1),
        ("verified", False),
        ("vms_enabled", False),
        ("dph_total", 0),
        ("dph_total", float("nan")),
    ],
)
def test_worker_gates_fail_closed(field: str, value: object) -> None:
    raw = offer(1, 1)
    raw[field] = value
    assert parse_offer(raw, role="worker") is None


def test_gib_floors_are_not_confused_with_marketplace_decimal_gb() -> None:
    raw = offer(1, 1, gpu="RTX 4090")
    raw["cpu_ram"] = 64000  # 64 decimal GB is below 64 GiB.
    assert parse_offer(raw, role="worker") is None
    raw["cpu_ram"] = 69000
    raw["disk_space"] = 260  # 260 decimal GB is below 250 GiB.
    assert parse_offer(raw, role="worker") is None
    raw["disk_space"] = 270
    candidate = parse_offer(raw, role="worker")
    assert candidate is not None
    assert candidate.gpu_ram_gb >= 24
    assert candidate.ram_gib >= 64
    assert candidate.disk_gib >= 250


def test_dynamic_public_endpoint_is_candidate_only_not_network_acceptance() -> None:
    raw = offer(1, 1, gpu="RTX 4090")
    raw["static_ip"] = False
    raw["gpu_ram"] = 24564
    candidate = parse_offer(raw, role="worker")
    assert candidate is not None
    assert candidate.static_ip is False


def test_secret_file_is_mode_protected(tmp_path: Path) -> None:
    path = tmp_path / "key"
    path.write_text("fixture-token\n", encoding="utf-8")
    os.chmod(path, 0o600)
    assert _secret_file(path) == "fixture-token"
    os.chmod(path, 0o644)
    with pytest.raises(FleetError, match="mode 0600"):
        _secret_file(path)
    os.chmod(path, 0o600)
    (tmp_path / "link").symlink_to(path)
    with pytest.raises(FleetError, match="symlink"):
        _secret_file(tmp_path / "link")


class FixtureClient:
    def __init__(self, *, credit: float, existing: list[dict[str, object]] | None = None) -> None:
        self.credit = credit
        self.existing = existing or []
        self.offers = [offer(1, 10), offer(2, 11), offer(3, 12, hourly=0.3)]
        self.paths: list[str] = []

    def get(self, path: str, *, query: dict[str, str] | None = None) -> dict[str, object]:
        self.paths.append(path)
        if path == "/users/current/":
            return {"credit": self.credit}
        if path == "/instances/":
            return {"instances": self.existing}
        assert path == "/bundles/" and query is not None
        return {"offers": self.offers}


def test_read_only_plan_checks_credit_and_existing_spend() -> None:
    client = FixtureClient(credit=60)
    result = read_only_plan(client)  # type: ignore[arg-type]
    assert result["status"] == "candidate-only"
    assert result["pilotAdvertisedHourlyUsd"] == 2.5
    assert client.paths == ["/users/current/", "/instances/", "/bundles/"]
    limited = read_only_plan(FixtureClient(credit=32.97))  # type: ignore[arg-type]
    assert limited["status"] == "candidate-only"
    assert limited["additionalCreditNeededUsd"] == 17.03
    blocked = read_only_plan(FixtureClient(credit=10))  # type: ignore[arg-type]
    assert blocked["status"] == "blocked"
    assert "credit" in " ".join(blocked["blockers"])
    assert blocked["additionalCreditNeededUsd"] == 40
    over_cap = read_only_plan(
        FixtureClient(credit=60, existing=[{"actual_status": "running", "dph_total": 2}])
    )  # type: ignore[arg-type]
    assert over_cap["status"] == "blocked"
    assert "cap" in " ".join(over_cap["blockers"])


def test_read_only_plan_reports_no_matching_offers_without_traceback() -> None:
    client = FixtureClient(credit=60)
    client.offers = [offer(1, 10), offer(2, 11, region="Canada, CA")]
    result = read_only_plan(client)  # type: ignore[arg-type]
    assert result["status"] == "blocked"
    assert result["candidates"] == []
    assert result["pilotAdvertisedHourlyUsd"] is None


def owned_first_worker() -> tuple[dict[str, object], dict[str, object]]:
    instance = offer(100, 10, gpu="RTX 4090", region="Switzerland, CH", hourly=0.622)
    instance.update(
        {
            "label": "kip-pilot-test-gpu1",
            "template_hash_id": "official-vm-template",
            "actual_status": "running",
            "disk_space": 320,
        }
    )
    lease: dict[str, object] = {
        "managedBy": "kube-inference-platform",
        "phase": "created",
        "instanceId": 100,
        "label": "kip-pilot-test-gpu1",
        "machineId": 10,
        "templateHash": "official-vm-template",
        "gpu": "RTX 4090",
        "offerId": 99,
    }
    return instance, lease


def test_plan_keeps_owned_first_worker_and_prices_only_remaining_rentals() -> None:
    instance, lease = owned_first_worker()
    client = FixtureClient(credit=50, existing=[instance])
    client.offers = [
        offer(1, 11, gpu="RTX 1660", region="Switzerland, CH", hourly=0.2),
        offer(2, 12, gpu="RTX 4090", region="Switzerland, CH", hourly=0.7),
        offer(3, 13, gpu="RTX 3090", region="France, FR", hourly=0.4),
    ]
    result = read_only_plan(
        client,
        allow_mixed=True,
        first_worker_lease=lease,  # type: ignore[arg-type]
    )
    assert result["status"] == "candidate-only"
    assert result["ownedFirstWorkerInstanceId"] == 100
    assert result["pilotAdvertisedHourlyUsd"] == pytest.approx(1.522)
    assert result["additionalAdvertisedHourlyUsd"] == pytest.approx(0.9)
    assert [item["machineId"] for item in result["candidates"]] == [11, 10, 12]
    assert [item["alreadyRented"] for item in result["candidates"]] == [False, True, False]


def test_mixed_plan_prefers_three_european_hosts_over_intercontinental_colocation() -> None:
    instance, lease = owned_first_worker()
    client = FixtureClient(credit=50, existing=[instance])
    client.offers = [
        offer(1, 11, gpu="RTX 3090", region="Quebec, CA", hourly=0.4),
        offer(2, 12, gpu="RTX 3060", region="Quebec, CA", hourly=0.2),
        offer(3, 13, gpu="RTX 3090", region="United Kingdom, GB", hourly=0.5),
        offer(4, 14, gpu="RTX 3060", region="Romania, RO", hourly=0.7),
    ]
    result = read_only_plan(client, allow_mixed=True, first_worker_lease=lease)  # type: ignore[arg-type]
    assert [item["region"] for item in result["candidates"]] == [
        "Romania, RO",
        "Switzerland, CH",
        "United Kingdom, GB",
    ]


def test_owned_worker_drift_and_offline_status_block_more_rentals() -> None:
    instance, lease = owned_first_worker()
    client = FixtureClient(credit=50, existing=[instance])
    client.offers = [
        offer(1, 11, gpu="RTX 1660", region="Switzerland, CH", hourly=0.2),
        offer(2, 12, gpu="RTX 4090", region="Switzerland, CH", hourly=0.7),
    ]
    instance["actual_status"] = "offline"
    result = read_only_plan(
        client,
        allow_mixed=True,
        first_worker_lease=lease,  # type: ignore[arg-type]
    )
    assert result["status"] == "blocked"
    assert "not reported running" in " ".join(result["blockers"])
    assert result["existingActiveHourlyUsd"] == pytest.approx(0.622)
    instance["actual_status"] = "running"
    instance["machine_id"] = 999
    with pytest.raises(FleetError, match="drifted"):
        read_only_plan(client, allow_mixed=True, first_worker_lease=lease)  # type: ignore[arg-type]
