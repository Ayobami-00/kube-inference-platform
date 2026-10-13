"""The fleet text replay validates trace provenance before invoking AIPerf."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.pilot_dataset import generate_text_dataset, save_generated
from bench.workloads import load_workload_contract
from fleet.aiperf_text import _dataset, _profile, _run_id, stream_outcomes, validate_raw_streams
from fleet.vast import FleetError


@pytest.fixture
def generated(tmp_path: Path) -> tuple[Path, Path]:
    contract = load_workload_contract()
    trace = generate_text_dataset(
        contract,
        tenant="tenant-a",
        workload_class="chat",
        trial=1,
        sessions=2,
        offered_sessions_per_second=1.0,
        count_tokens=lambda text: len(text.split()),
    )
    path = (
        tmp_path
        / "benchmarks"
        / ".work"
        / "datasets"
        / contract.name
        / "tenant-a"
        / "chat"
        / "trial-1.jsonl"
    )
    save_generated(trace, path)
    return tmp_path, path


def test_replay_accepts_exact_private_generated_trace(generated: tuple[Path, Path]) -> None:
    root, path = generated
    loaded, manifest = _dataset(root, "tenant-a", "chat", 1)
    assert loaded == path
    assert manifest["sessionCount"] == 2


def test_shared_run_id_is_strict_and_path_safe() -> None:
    assert _run_id("pilot-text-20261013-020000-sample100") == (
        "pilot-text-20261013-020000-sample100"
    )
    for value in ("../escape", "/tmp/pilot-text-20261013-020000", "pilot-text-a", ""):
        if value:
            with pytest.raises(FleetError, match="run ID"):
                _run_id(value)


def test_only_reviewed_aiperf_profiles_can_be_selected() -> None:
    root = Path(__file__).resolve().parents[1]
    assert _profile(root, "pilot-text-matrix").name == "pilot-text-matrix.yaml"
    with pytest.raises(FleetError, match="reviewed"):
        _profile(root, "../../private")


def test_replay_rejects_modified_trace_or_manifest(generated: tuple[Path, Path]) -> None:
    root, path = generated
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(FleetError, match="drifted"):
        _dataset(root, "tenant-a", "chat", 1)
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")
    manifest_path = path.with_suffix(".manifest.json")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["trial"] = 2
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(FleetError, match="drifted"):
        _dataset(root, "tenant-a", "chat", 1)


def test_replay_rejects_public_or_wrong_class_trace(generated: tuple[Path, Path]) -> None:
    root, path = generated
    path.chmod(0o644)
    with pytest.raises(FleetError, match="mode 0600"):
        _dataset(root, "tenant-a", "chat", 1)
    with pytest.raises(FleetError, match="reviewed class"):
        _dataset(root, "tenant-a", "voice", 1)


def test_raw_stream_evidence_requires_exact_final_done(tmp_path: Path) -> None:
    path = tmp_path / "private-raw.jsonl"

    def row(identifier: str, final: str = "[DONE]") -> dict[str, object]:
        return {
            "metadata": {"x_request_id": identifier},
            "status": 200,
            "request_headers": {"Authorization": "Bearer must-never-be-published"},
            "payload": {"messages": [{"content": "synthetic private prompt"}]},
            "responses": [
                {"packets": [{"name": "data", "value": '{"delta":"synthetic"}'}]},
                {"packets": [{"name": "data", "value": final}]},
            ],
        }

    path.write_text("\n".join(json.dumps(item) for item in (row("a"), row("b"))) + "\n")
    assert validate_raw_streams(path, 2) == {"a", "b"}
    path.write_text(json.dumps(row("a", "missing")) + "\n")
    with pytest.raises(FleetError, match=r"SSE \[DONE\]") as error:
        validate_raw_streams(path, 1)
    assert "synthetic private prompt" not in str(error.value)
    assert "must-never-be-published" not in str(error.value)


def test_partial_outcomes_keep_429_separate_from_completed_streams(tmp_path: Path) -> None:
    path = tmp_path / "private-raw.jsonl"
    rows = [
        {
            "metadata": {"x_request_id": "successful-private-id"},
            "status": 200,
            "responses": [{"packets": [{"name": "data", "value": "[DONE]"}]}],
        },
        {
            "metadata": {"x_request_id": "rejected-private-id"},
            "status": 429,
            "responses": [],
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    with pytest.raises(FleetError, match="incomplete"):
        validate_raw_streams(path, 2)
    statuses, done = stream_outcomes(path, 2, allow_rejected=True)
    assert statuses == {"successful-private-id": 200, "rejected-private-id": 429}
    assert done == {"successful-private-id"}
