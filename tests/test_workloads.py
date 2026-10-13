"""Tests for the reviewed, content-free multi-tenant workload contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bench.config import canonical_json
from bench.errors import ValidationError
from bench.workloads import (
    CLASSES,
    TENANTS,
    derived_seed,
    load_workload_contract,
    preview_trajectories,
)


def test_pilot_contract_valid_and_deterministic() -> None:
    contract = load_workload_contract()
    assert contract.name == "pilot-v1"
    assert contract.data["rootSeed"] == 42
    assert contract.data["contextInputTokenCap"] == 6144
    assert set(contract.data["classes"]) == set(CLASSES)
    assert len(contract.sha256) == 64
    first = preview_trajectories(
        contract, tenant="tenant-a", workload_class="agentic-style", trial=1, sessions=10
    )
    assert first == preview_trajectories(
        contract, tenant="tenant-a", workload_class="agentic-style", trial=1, sessions=10
    )
    assert first["seed"] != derived_seed(contract, "tenant-b", "agentic-style", 1)
    assert first["seed"] != derived_seed(contract, "tenant-a", "agentic-style", 2)
    assert all(
        turn["contextInputTokens"] <= 6144
        for session in first["sessions"]
        for turn in session["turns"]
    )
    assert all(
        "prompt" not in turn and "response" not in turn
        for session in first["sessions"]
        for turn in session["turns"]
    )


def test_voice_preview_contains_audio_metadata_not_audio() -> None:
    preview = preview_trajectories(
        load_workload_contract(), tenant="tenant-c", workload_class="voice", trial=2
    )
    assert all("utteranceDurationMs" in session for session in preview["sessions"])
    assert b"audio" not in canonical_json(preview).lower()


@pytest.mark.parametrize("mutation", ["unknown", "wrong_tenants", "bad_mix", "bad_cap"])
def test_contract_rejects_drift(tmp_path: Path, mutation: str) -> None:
    data = json.loads(load_workload_contract().path.read_text(encoding="utf-8"))
    if mutation == "unknown":
        data["unreviewed"] = True
    elif mutation == "wrong_tenants":
        data["tenants"] = ["tenant-a"]
    elif mutation == "bad_mix":
        data["textMix"]["chat"] = 0.9
    else:
        data["contextInputTokenCap"] = 100
    (tmp_path / "pilot-v1.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_workload_contract(root=tmp_path)


def test_invalid_preview_inputs_fail() -> None:
    contract = load_workload_contract()
    for tenant in ("../secrets", "tenant-x"):
        with pytest.raises(ValidationError):
            derived_seed(contract, tenant, CLASSES[0], 1)
    for workload_class in ("../secrets", "custom"):
        with pytest.raises(ValidationError):
            derived_seed(contract, TENANTS[0], workload_class, 1)
    with pytest.raises(ValidationError):
        preview_trajectories(contract, tenant="tenant-a", workload_class="chat", sessions=1001)


def test_cli_validates_and_previews_without_benchmark_dependency() -> None:
    root = Path(__file__).resolve().parents[1]
    for command in (
        ["workload-validate", "--contract", "pilot-v1"],
        ["workload-preview", "--tenant", "tenant-a", "--class", "chat"],
    ):
        result = subprocess.run(
            [sys.executable, "-m", "bench", *command],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["contract"] == "pilot-v1"
