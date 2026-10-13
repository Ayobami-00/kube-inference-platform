"""The pilot's immutable artifact pins stay separate from the stable v0.1 path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.compat import load_compatibility_lock
from fleet.vast import FleetError


def test_committed_lock_is_consistent_but_not_live_validated() -> None:
    lock = load_compatibility_lock()
    assert lock["status"] == "artifact-locked-target-unvalidated"
    assert lock["gpu"]["driverMode"] == "preinstalled"
    assert lock["gpu"]["driverVersion"] == "580.178.04"
    assert lock["gpu"]["cudaRuntime"] == "13.0"
    assert lock["inference"]["llmdVersion"] == "v0.10.0"
    assert "@sha256:" in lock["inference"]["vllmLinuxAmd64Image"]
    assert "@sha256:" in lock["inference"]["eppLinuxAmd64Image"]


@pytest.mark.parametrize(
    "mutation", ["unknown", "driver", "digest", "base", "version", "image_repo", "gateway"]
)
def test_lock_refuses_drift(tmp_path: Path, mutation: str) -> None:
    lock = load_compatibility_lock()
    if mutation == "unknown":
        lock["other"] = "unsafe"
    elif mutation == "driver":
        lock["gpu"]["driverVersion"] = "595.91.07"
    elif mutation == "digest":
        lock["inference"]["routerGatewayChartDigest"] = "sha256:unverified"
    elif mutation == "base":
        lock["base"]["kubernetes"] = "1.35.0"
    elif mutation == "image_repo":
        lock["inference"]["vllmLinuxAmd64Image"] = (
            "evil.example/vllm:v0.30.0@"
            "sha256:5f5e535216848d0c52159c8c13a0af04be5f6fe1a84e79914300610796f76d40"
        )
    elif mutation == "gateway":
        lock["gateway"]["envoyAiGatewayVersion"] = "latest"
    else:
        lock["inference"]["vllmVersion"] = "latest"
    path = tmp_path / "versions.json"
    path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(FleetError):
        load_compatibility_lock(path)
