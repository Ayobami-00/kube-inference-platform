"""Exact-window telemetry snapshots contain only reviewed aggregate fields."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench import lambda_telemetry as telemetry


def _summary(root: Path, run_id: str) -> Path:
    folder = root / "benchmarks/reports" / run_id
    folder.mkdir(parents=True)
    (folder / "summary.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "runId": run_id,
                "variant": "baseline",
                "trial": 1,
                "startUtc": "2026-10-13T12:57:24.2Z",
                "endUtc": "2026-10-13T13:07:27.4Z",
                "cost": {
                    "nodeHourlyUsd": {
                        "kip-lambda-2acad4b0-control-speech": 1.29,
                        "kip-lambda-2acad4b0-large": 22.32,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_queries_target_priority_and_model_histograms() -> None:
    expressions = telemetry.queries(604, "kip-lambda-2acad4b0-large")
    assert 'priority="-10"' in expressions["largeBatchQueueP95Ms"]
    assert 'outcome="Dispatched"' in expressions["largeInteractiveQueueP95Ms"]
    assert 'model_name="qwen3-next-80b-a3b-instruct"' in expressions["largeVllmQueueP95Ms"]
    assert "[604s]" in expressions["largeNodeGpuUtilMeanPercent"]


def test_capture_is_sanitized_and_refuses_republication(tmp_path: Path, monkeypatch) -> None:
    run_id = "lambda-nominal-baseline-20261013-125717"
    folder = _summary(tmp_path, run_id)
    calls = []

    def query(base: str, expression: str, at_seconds: int) -> float:
        calls.append((base, expression, at_seconds))
        return 12.5

    monkeypatch.setattr(telemetry, "query_scalar", query)
    output = telemetry.capture(run_id, "http://10.108.232.53:9090", tmp_path)
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["windowStartUtc"] == "2026-10-13T12:57:24Z"
    assert result["windowEndUtc"] == "2026-10-13T13:07:28Z"
    assert len(calls) == len(result["metrics"]) == 7
    assert set(result) == {
        "schemaVersion",
        "runId",
        "variant",
        "trial",
        "windowStartUtc",
        "windowEndUtc",
        "windowPrecisionSeconds",
        "metrics",
        "dataQuality",
    }
    assert not any(word in output.read_text() for word in ("prompt", "response", "token"))
    assert folder.joinpath("summary.json").is_file()
    with pytest.raises(ValueError, match="already published"):
        telemetry.capture(run_id, "http://10.108.232.53:9090", tmp_path)


def test_rejects_unreviewed_run_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not reviewed"):
        telemetry.capture("../private", "http://10.108.232.53:9090", tmp_path)
