"""Strict validation tests for published whole-node cost profiles."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
COMMON = ROOT / "deploy" / "observability" / "lib" / "common.sh"
CLUSTER_COMMON = ROOT / "deploy" / "cluster" / "lib" / "common.sh"
UI = ROOT / "deploy" / "lib" / "ui.sh"
TEMPLATE = ROOT / "costs" / "profiles" / "a10-node.template.json"
SCHEMA = ROOT / "costs" / "schema-v1.json"


@pytest.fixture
def profile() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "name": "a10-node",
        "currency": "USD",
        "nodeHourlyRateUsd": 1.25,
        "provider": "Example Compute",
        "machineType": "one-a10-24gb",
        "region": "us-test-1",
        "source": "https://example.invalid/public-pricing",
        "effectiveFrom": "2026-09-21",
        "physicalGpuCount": 1,
        "powerIncludedInRate": True,
    }


def validate_profile(path: Path) -> subprocess.CompletedProcess[str]:
    script = (
        f'source "{UI}"; '
        f'source "{CLUSTER_COMMON}"; '
        'KIP_REPOSITORY_ROOT="$1"; '
        f'source "{COMMON}"; '
        'kip_observability_validate_cost_profile_schema "$2"'
    )
    return subprocess.run(
        ["/bin/bash", "-c", script, "cost-profile-test", str(ROOT), str(path)],
        cwd=ROOT,
        env={**os.environ, "NO_COLOR": "1"},
        check=False,
        capture_output=True,
        text=True,
    )


def write_profile(tmp_path: Path, profile: dict[str, object]) -> Path:
    path = tmp_path / "a10-node.json"
    path.write_text(json.dumps(profile))
    return path


def test_unconfigured_template_is_intentionally_not_selectable() -> None:
    template = json.loads(TEMPLATE.read_text())
    assert template["nodeHourlyRateUsd"] is None
    assert template["physicalGpuCount"] == 1
    assert template["powerIncludedInRate"] is True
    assert validate_profile(TEMPLATE).returncode != 0


def test_published_schema_requires_public_https_provenance() -> None:
    schema = json.loads(SCHEMA.read_text())
    assert schema["additionalProperties"] is False
    assert schema["properties"]["source"]["pattern"].startswith("^https://")


def test_valid_whole_node_profile(profile: dict[str, object], tmp_path: Path) -> None:
    assert validate_profile(write_profile(tmp_path, profile)).returncode == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("currency", "GBP"),
        ("nodeHourlyRateUsd", 0),
        ("nodeHourlyRateUsd", -1),
        ("physicalGpuCount", 0),
        ("physicalGpuCount", 1.5),
        ("powerIncludedInRate", False),
        ("effectiveFrom", "21-09-2026"),
        ("effectiveFrom", "2026-13-40"),
        ("effectiveFrom", "2999-01-01"),
        ("provider", "REPLACE_ME"),
        ("source", "REPLACE_WITH_SOURCE"),
        ("source", "http://example.invalid/private-pricing"),
        ("source", "https://billing-user:secret@example.invalid/pricing"),
        ("source", "https://example.invalid/pricing?token=secret"),
        ("source", "https://example.invalid/pricing#private"),
        ("source", "https://example.invalid/pricing?token=signed-secret"),
        ("source", "https://example.invalid/pricing#private-invoice"),
    ],
)
def test_rejects_invalid_profile_fields(
    profile: dict[str, object], tmp_path: Path, field: str, value: object
) -> None:
    profile[field] = value
    assert validate_profile(write_profile(tmp_path, profile)).returncode != 0


def test_rejects_unknown_fields(profile: dict[str, object], tmp_path: Path) -> None:
    profile["electricityRate"] = 0.2
    assert validate_profile(write_profile(tmp_path, profile)).returncode != 0


def test_profile_name_is_path_safe() -> None:
    script = (
        f'source "{UI}"; '
        f'source "{CLUSTER_COMMON}"; '
        'KIP_REPOSITORY_ROOT="$1"; '
        f'source "{COMMON}"; '
        'kip_observability_profile_name_valid "$2"'
    )
    for name, expected in (
        ("a10-node", 0),
        ("a10-node-2026", 0),
        ("../a10-node", 1),
        ("a10_node", 1),
        ("a10-node.template", 1),
        ("A10", 1),
    ):
        completed = subprocess.run(
            ["/bin/bash", "-c", script, "name-test", str(ROOT), name],
            cwd=ROOT,
            check=False,
        )
        assert completed.returncode == expected


def test_requested_profile_identity_mismatch_is_rejected() -> None:
    script = (
        f'source "{UI}"; '
        f'source "{CLUSTER_COMMON}"; '
        f'KIP_REPOSITORY_ROOT="{ROOT}"; source "{COMMON}"; '
        'kip_observability_profile_identity_matches "$1" "$2" "$3" "$4"'
    )
    for values, expected in (
        (("a10-node", "abc", "a10-node", "abc"), 0),
        (("a10-node", "abc", "other-node", "abc"), 1),
        (("a10-node", "abc", "a10-node", "def"), 1),
    ):
        completed = subprocess.run(
            ["/bin/bash", "-c", script, "identity-test", *values],
            cwd=ROOT,
            check=False,
        )
        assert completed.returncode == expected

    cost_script = (ROOT / "deploy" / "observability" / "cost.sh").read_text()
    assert "requested_name=$KIP_COST_PROFILE_NAME" in cost_script
    assert "requested_hash=$KIP_COST_PROFILE_HASH" in cost_script
    assert "does not match active" in cost_script


def test_load_profile_derives_gpu_rate_and_hash(profile: dict[str, object], tmp_path: Path) -> None:
    jq = shutil.which("jq")
    sha = shutil.which("sha256sum") or shutil.which("shasum")
    if jq is None or sha is None:
        pytest.skip("jq and a SHA-256 command are required")
    profile["nodeHourlyRateUsd"] = 4.5
    profile["physicalGpuCount"] = 2
    write_profile(tmp_path, profile)
    sha_setup = (
        'kip_exec() { command "$@"; }; '
        if Path(sha).name == "sha256sum"
        else (
            "kip_exec() { "
            f'if [ "$1" = sha256sum ]; then shift; "{sha}" -a 256 "$@"; '
            'else command "$@"; fi; }; '
        )
    )
    script = (
        f'source "{UI}"; source "{CLUSTER_COMMON}"; '
        f'KIP_REPOSITORY_ROOT="{ROOT}"; source "{COMMON}"; '
        f'KIP_COST_PROFILE_DIR="{tmp_path}"; KIP_COST_PROFILE=a10-node; '
        f"{sha_setup}"
        "kip_observability_load_cost_profile; "
        'printf "%s|%s|%s" "$KIP_COST_NODE_HOURLY_RATE_USD" '
        '"$KIP_COST_GPU_HOURLY_RATE_USD" "$KIP_COST_PROFILE_HASH"'
    )
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{Path(jq).parent}:/usr/bin:/bin", "NO_COLOR": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    rate, gpu_rate, digest = completed.stdout.split("|")
    assert rate == "4.5"
    assert float(gpu_rate) == pytest.approx(2.25)
    assert len(digest) == 64
