"""Credential and warmup-accounting guards for the private AIPerf checkpoint."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.lambda_gateway_smoke import private_token, validate_artifacts


def test_private_token_rejects_world_readable_file(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "tenant-keys.json"
    path.write_text(json.dumps({"tenant-a": {"chat": "a" * 64}}), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        private_token(path, "tenant-a", "chat")
    path.chmod(0o600)
    assert private_token(path, "tenant-a", "chat") == "a" * 64


def test_aiperf_raw_validation_excludes_warmup(tmp_path: Path) -> None:
    summary = {
        "aiperf_version": "0.12.0",
        "was_cancelled": False,
        "error_summary": [],
        "request_count": {"avg": 24},
        "time_to_first_token": {"count": 24, "p95": 123},
        "request_latency": {"p95": 456},
        "output_token_throughput": {"avg": 78},
    }
    (tmp_path / "profile_export_aiperf.json").write_text(json.dumps(summary), encoding="utf-8")
    raw = tmp_path / "profile_export_raw.jsonl"
    rows = [
        {
            "metadata": {"benchmark_phase": "warmup" if index < 4 else "profiling"},
            "status": 200,
            "responses": [{"packets": [{"name": "data", "value": "[DONE]"}]}],
        }
        for index in range(28)
    ]
    raw.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert validate_artifacts(tmp_path)["completed"] == 24
    rows[-1]["responses"][0]["packets"] = []
    raw.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SSE stream"):
        validate_artifacts(tmp_path)
