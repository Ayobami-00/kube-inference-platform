"""The nominal trace is deterministic and has enough samples for per-class P95."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bench.lambda_nominal import CLASS_PLAN, inspect_cohort, validate_plan
from bench.workloads import derived_seed, load_workload_contract


@pytest.mark.parametrize("trial", [1, 2, 3])
def test_three_reviewed_nominal_trials_fit_window_and_sample_floor(trial: int) -> None:
    plan = validate_plan(trial)
    assert plan["minutes"] == 10
    assert plan["lastOfferedMs"] < 600_000
    assert plan["minimumScheduledTurnsByClass"] == {
        "chat": 126,
        "agentic-style": 108,
        "batch": 102,
    }
    assert plan["classPlan"] == CLASS_PLAN


@pytest.mark.parametrize("trial", [0, 4, True, "1"])
def test_unreviewed_nominal_trial_is_rejected(trial: object) -> None:
    with pytest.raises(ValueError, match="reviewed seeds"):
        validate_plan(trial)  # type: ignore[arg-type]


def test_nominal_cohort_counts_one_reviewed_429_as_rejected(tmp_path: Path) -> None:
    cohort = tmp_path / "tenant-c-batch"
    raw = cohort / "raw"
    raw.mkdir(parents=True)
    dataset = b"synthetic fixed data\n"
    (cohort / "dataset.jsonl").write_bytes(dataset)
    contract = load_workload_contract()
    (cohort / "dataset.manifest.json").write_text(
        json.dumps(
            {
                "tenant": "tenant-c",
                "class": "batch",
                "trial": 1,
                "contractSha256": contract.sha256,
                "seed": derived_seed(contract, "tenant-c", "batch", 1),
                "sessionCount": 34,
                "offeredSessionsPerSecond": 0.06,
                "plannedTurnCount": 34,
                "datasetSha256": hashlib.sha256(dataset).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    (raw / "profile_export_aiperf.json").write_text(
        json.dumps(
            {
                "aiperf_version": "0.12.0",
                "was_cancelled": False,
                "request_count": {"avg": 33},
                "time_to_first_token": {"count": 33},
                "error_summary": [
                    {"count": 1, "error_details": {"code": 429, "type": "Too Many Requests"}}
                ],
            }
        ),
        encoding="utf-8",
    )
    records = [
        {
            "metadata": {"x_request_id": f"request-{index}", "benchmark_phase": "profiling"},
            "status": 200 if index < 33 else 429,
            "responses": [{"packets": [{"name": "data", "value": "[DONE]"}]} if index < 33 else {}],
        }
        for index in range(34)
    ]
    (raw / "profile_export_raw.jsonl").write_text(
        "\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8"
    )
    result = inspect_cohort(tmp_path, "tenant-c", "batch", 1)
    assert result["scheduled"] == result["sent"] == 34
    assert result["completed"] == 33
    assert result["rejected"] == 1
    records[-1]["status"] = 500
    (raw / "profile_export_raw.jsonl").write_text(
        "\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invalid request identity/status"):
        inspect_cohort(tmp_path, "tenant-c", "batch", 1)
