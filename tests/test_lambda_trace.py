"""Short multi-tenant trace accounting and stream-integrity tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.lambda_trace import COHORTS, NINE_COHORTS, validate_packets


def test_reviewed_cohorts_cover_both_models_and_three_tenants() -> None:
    assert {row[0] for row in COHORTS} == {"tenant-a", "tenant-b", "tenant-c"}
    assert {row[1] for row in COHORTS} == {"chat", "agentic-style", "batch"}
    assert {row[2] for row in COHORTS} == {
        "qwen3-8b",
        "qwen3-next-80b-a3b-instruct",
    }
    assert len(NINE_COHORTS) == 9
    assert {(row[0], row[1]) for row in NINE_COHORTS} == {
        (tenant, workload_class)
        for tenant in ("tenant-a", "tenant-b", "tenant-c")
        for workload_class in ("chat", "agentic-style", "batch")
    }


def test_raw_trace_requires_unique_completed_profiling_streams(tmp_path: Path) -> None:
    path = tmp_path / "raw.jsonl"
    rows = [
        {
            "metadata": {"x_request_id": str(index), "benchmark_phase": "profiling"},
            "status": 200,
            "responses": [{"packets": [{"name": "data", "value": "[DONE]"}]}],
        }
        for index in range(3)
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert validate_packets(path, 3) == 3
    rows[-1]["metadata"]["x_request_id"] = "0"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        validate_packets(path, 3)
    rows[-1]["metadata"]["x_request_id"] = "2"
    rows[-1]["responses"][0]["packets"] = []
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete SSE"):
        validate_packets(path, 3)
