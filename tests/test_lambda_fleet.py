"""No-network safety fixtures for the Lambda provider adapter."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from fleet.lambda_cloud import (
    LambdaClient,
    LambdaError,
    parse_offers,
    preferred_region,
    read_key,
    role_candidates,
)
from fleet.lambda_rental import (
    Billing,
    _save_private,
    accrued_cost,
    budget_gate,
    candidate_image,
    load_billing,
    load_lease,
    matching_instance,
    new_lease,
    projected_cost,
    read_only_plan,
)


def _offer(name: str, gpu: str, gpus: int, cents: int, regions: list[str]) -> dict:
    return {
        "instance_type": {
            "name": name,
            "description": name,
            "gpu_description": gpu,
            "price_cents_per_hour": cents,
            "specs": {
                "gpus": gpus,
                "vcpus": 104,
                "memory_gib": 900,
                "storage_gib": 4096,
            },
            "architecture": "x86_64",
        },
        "regions_with_capacity_available": [
            {"name": region, "description": region} for region in regions
        ],
    }


def _offers() -> dict:
    return {
        "gpu_1x_a10": _offer("gpu_1x_a10", "NVIDIA A10", 1, 129, ["us-east-1"]),
        "gpu_1x_h100_sxm": _offer(
            "gpu_1x_h100_sxm", "NVIDIA H100 SXM", 1, 429, ["us-east-1", "us-west-1"]
        ),
        "gpu_4x_h100_sxm": _offer("gpu_4x_h100_sxm", "NVIDIA H100 SXM", 4, 1636, ["us-east-1"]),
        "gpu_1x_b200": _offer("gpu_1x_b200", "NVIDIA B200 SXM", 1, 699, []),
    }


def _node(role: str, cents: int = 429) -> dict:
    return {
        "role": role,
        "phase": "active",
        "name": "kip-lambda-test-" + role,
        "instanceId": "12345678-1234-1234-1234-123456789abc",
        "instanceType": "gpu_1x_h100_sxm",
        "priceCentsPerHour": cents,
        "createdAt": "2026-10-13T12:00:00Z",
        "endedAt": None,
    }


def test_key_must_be_private_regular_file(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("fixture-only\n")
    os.chmod(key, 0o644)
    with pytest.raises(LambdaError, match="0600"):
        read_key(key)
    os.chmod(key, 0o600)
    assert read_key(key) == "fixture-only"
    alias = tmp_path / "alias"
    alias.symlink_to(key)
    with pytest.raises(LambdaError, match="non-symlink"):
        read_key(alias)


def test_approved_curl_transport_never_places_key_in_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "lambda-key"
    key.write_text("fixture-secret-123")
    os.chmod(key, 0o600)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[0] == "curl"
        assert "fixture-secret-123" not in " ".join(command)
        assert command[-1] == "https://cloud.lambda.ai/api/v1/instances"
        assert "fixture-secret-123" in str(kwargs["input"])
        return subprocess.CompletedProcess(command, 0, '{"data":[]}\n200', "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert LambdaClient(key).instances() == []


def test_approved_curl_transport_sanitizes_api_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "lambda-key"
    key.write_text("fixture-secret-123")
    os.chmod(key, 0o600)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 0, '{"error":{"code":"bad secret: fixture-secret-123"}}\n403', ""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(LambdaError, match="HTTP 403: unavailable") as error:
        LambdaClient(key).instances()
    assert "fixture-secret-123" not in str(error.value)


def test_same_region_and_exact_hardware_selection() -> None:
    region, selected = preferred_region(parse_offers(_offers()))
    assert region == "us-east-1"
    assert {role: value.price_cents_per_hour for role, value in selected.items()} == {
        "control-speech": 129,
        "small-1": 429,
        "small-2": 429,
        "large": 1636,
    }
    assert sum(item.price_cents_per_hour for item in selected.values()) == 2623
    with_a100 = _offers()
    with_a100["gpu_1x_a100_sxm4"] = _offer(
        "gpu_1x_a100_sxm4", "A100 (40 GB SXM4)", 1, 199, ["us-east-1"]
    )
    assert [offer.name for offer in role_candidates(parse_offers(with_a100), "control-speech")] == [
        "gpu_1x_a10"
    ]
    mismatched = _offers()
    mismatched["gpu_4x_h100_sxm"]["regions_with_capacity_available"] = [
        {"name": "us-west-1", "description": "west"}
    ]
    with pytest.raises(LambdaError, match="no region"):
        preferred_region(parse_offers(mismatched))


def test_offers_reject_invalid_price_and_require_real_capacity() -> None:
    invalid = _offers()
    invalid["gpu_1x_a10"]["instance_type"]["price_cents_per_hour"] = 0
    with pytest.raises(LambdaError, match="price"):
        parse_offers(invalid)
    no_a10 = _offers()
    no_a10["gpu_1x_a10"]["regions_with_capacity_available"] = []
    with pytest.raises(LambdaError, match="not all available"):
        preferred_region(parse_offers(no_a10))


def test_billing_requires_provenance_and_private_mode(tmp_path: Path) -> None:
    path = tmp_path / "billing.json"
    value = {"schemaVersion": 1, "taxPercent": "20", "storageUsdPerHour": "0", "source": "invoice"}
    path.write_text(json.dumps(value))
    os.chmod(path, 0o644)
    with pytest.raises(LambdaError, match="0600"):
        load_billing(path)
    os.chmod(path, 0o600)
    assert load_billing(path).multiplier == Decimal("1.2")
    value["taxPercent"] = "-1"
    _save_private(path, value)
    with pytest.raises(LambdaError, match="range"):
        load_billing(path)


def test_owned_lease_cost_and_projection(tmp_path: Path) -> None:
    path = tmp_path / "lease.json"
    lease = new_lease("us-east-1")
    lease["nodes"]["small-1"] = _node("small-1")
    _save_private(path, lease)
    assert load_lease(path) == lease
    billing = Billing(Decimal("20"), Decimal("0"), "invoice")
    now = datetime(2026, 10, 13, 13, 0, 0, tzinfo=UTC)
    assert accrued_cost(lease, billing, now) == Decimal("5.15")
    _, selected = preferred_region(parse_offers(_offers()))
    # Projection uses real clock for accrued amounts; only assert the hard invariant.
    assert projected_cost(lease, billing, {"large": selected["large"]}, Decimal(1), now) > 0
    lease["managedBy"] = "other-project"
    _save_private(path, lease)
    with pytest.raises(LambdaError, match="ownership"):
        load_lease(path)


def test_budget_guard_has_target_and_absolute_stop() -> None:
    assert budget_gate(Decimal("299.99"), new_work=True) == "within-budget"
    assert budget_gate(Decimal("315"), new_work=False) == "teardown-only"
    with pytest.raises(LambdaError, match="target"):
        budget_gate(Decimal("300"), new_work=True)
    with pytest.raises(LambdaError, match="absolute"):
        budget_gate(Decimal("330"), new_work=False)


def test_instance_ownership_requires_id_name_region_and_tags() -> None:
    lease = new_lease("us-east-1")
    lease["nodes"]["small-1"] = _node("small-1")
    instance = {
        "id": lease["nodes"]["small-1"]["instanceId"],
        "name": lease["nodes"]["small-1"]["name"],
        "status": "active",
        "region": {"name": "us-east-1"},
        "tags": [
            {"key": "kip-campaign", "value": lease["campaignId"]},
            {"key": "kip-role", "value": "small-1"},
        ],
    }
    assert matching_instance(lease, "small-1", [instance]) == instance
    bad = {**instance, "tags": [{"key": "kip-campaign", "value": "someone-else"}]}
    with pytest.raises(LambdaError, match="ownership"):
        matching_instance(lease, "small-1", [bad])


def test_read_only_plan_requires_billing_and_one_ssh_key(tmp_path: Path) -> None:
    class FixtureClient:
        def instance_types(self) -> dict:
            return _offers()

        def images(self) -> list[dict]:
            return [
                {
                    "id": "ubuntu-image-id",
                    "family": "ubuntu-24-04",
                    "name": "Ubuntu 24.04.2 LTS",
                    "architecture": "x86_64",
                    "region": {"name": "us-east-1"},
                    "version": "24.4.2-20250626",
                }
            ]

        def ssh_keys(self) -> list[dict]:
            return [{"name": "my-key"}]

        def instances(self) -> list[dict]:
            return []

    client = FixtureClient()
    assert candidate_image(client.images(), "us-east-1") == "ubuntu-image-id"
    path = tmp_path / "missing-lease.json"
    assert read_only_plan(client, lease_path=path)["status"] == "blocked"
    billing = Billing(Decimal("20"), Decimal(0), "invoice")
    result = read_only_plan(client, billing, lease_path=path)
    assert result["status"] == "ready"
    assert result["baseHourlyUsd"] == "26.23"


def test_paired_h100_is_only_a_partial_candidate_without_independent_workers(
    tmp_path: Path,
) -> None:
    class FixtureClient:
        def instance_types(self) -> dict:
            offers = _offers()
            offers["gpu_1x_h100_sxm"]["regions_with_capacity_available"] = []
            offers["gpu_4x_h100_sxm"]["regions_with_capacity_available"] = []
            offers["gpu_2x_h100_sxm5"] = _offer(
                "gpu_2x_h100_sxm5", "H100 (80 GB SXM5)", 2, 838, ["us-east-1"]
            )
            return offers

        def images(self) -> list[dict]:
            return []

        def ssh_keys(self) -> list[dict]:
            return [{"name": "my-key"}]

        def instances(self) -> list[dict]:
            return []

    result = read_only_plan(
        FixtureClient(),
        Billing(Decimal(0), Decimal(0), "invoice"),
        lease_path=tmp_path / "none.json",
    )
    assert result["status"] == "blocked"
    assert result["pairedH100Candidate"]["availableWithA10Regions"] == ["us-east-1"]
    assert result["pairedH100Candidate"]["workerFailureGate"] == "unsupported"
