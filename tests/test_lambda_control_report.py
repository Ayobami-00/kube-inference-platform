"""Sanitized H100 exploratory control report contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.lambda_control_report import build, render, utc


def metric(value: float) -> dict:
    return {"mean": value, "p50": value, "p95": value}


def source(root: Path) -> Path:
    aggregate = {
        "failed_runs": [],
        "num_profile_runs": 2,
        "num_successful_runs": 2,
        "metadata": {"num_trials_per_value": 1},
        "per_combination_metrics": [
            {
                "parameters": {"concurrency": concurrency},
                "metrics": {
                    "request_count": metric(20),
                    "output_sequence_length": metric(64),
                    "time_to_first_token": metric(100 * concurrency),
                    "inter_token_latency": metric(10),
                    "request_latency": metric(500),
                    "output_token_throughput": metric(70 * concurrency),
                },
            }
            for concurrency in (1, 4)
        ],
    }
    (root / "sweep_aggregate").mkdir(parents=True)
    (root / "sweep_aggregate/profile_export_aiperf_sweep.json").write_text(json.dumps(aggregate))
    for concurrency in (1, 4):
        cell = root / f"concurrency_{concurrency}"
        cell.mkdir()
        (cell / "profile_export_aiperf.json").write_text(
            json.dumps(
                {
                    "aiperf_version": "0.12.0",
                    "error_summary": [],
                    "was_cancelled": False,
                    "start_time": "2026-10-13T12:30:00",
                    "end_time": "2026-10-13T12:31:00",
                    "rawPrompt": "MUST_NOT_PUBLISH",
                }
            )
        )
    return root


def test_control_report_is_deterministic_and_allowlisted(tmp_path: Path) -> None:
    summary = build(source(tmp_path / "raw"))
    rendered = render(summary)
    assert rendered == render(summary)
    assert set(rendered) == {"summary.json", "summary.csv", "index.html"}
    assert "MUST_NOT_PUBLISH" not in json.dumps(rendered)
    assert summary["rows"][0]["startUtc"] == "2026-10-13T11:30:00Z"
    assert summary["sloStatus"].startswith("inconclusive")


def test_control_report_refuses_incomplete_sweep(tmp_path: Path) -> None:
    root = source(tmp_path / "raw")
    path = root / "sweep_aggregate/profile_export_aiperf_sweep.json"
    item = json.loads(path.read_text())
    item["failed_runs"] = ["one"]
    path.write_text(json.dumps(item))
    with pytest.raises(ValueError, match="incomplete"):
        build(root)


def test_aiperf_local_timestamp_is_explicitly_converted_to_utc() -> None:
    assert utc("2026-10-13T12:30:00") == "2026-10-13T11:30:00Z"
