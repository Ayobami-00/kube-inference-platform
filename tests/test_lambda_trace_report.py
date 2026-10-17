"""Sanitized Lambda trace report requires complete reviewed cohort evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.lambda_trace import MATRICES
from bench.lambda_trace_report import build, render
from bench.pilot_dataset import LAMBDA_TOKENIZERS, generate_text_dataset, save_generated
from bench.workloads import load_workload_contract


@pytest.mark.parametrize("matrix", ["three", "nine"])
def test_report_is_sanitized_and_refuses_incomplete_streams(tmp_path: Path, matrix: str) -> None:
    cohorts, sessions, rate, prefix = MATRICES[matrix]
    run_id = f"{prefix}-20261013-000000"
    campaign = tmp_path / "benchmarks/campaigns"
    campaign.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "benchmarks/campaigns/lambda-v1.json"
    (campaign / "lambda-v1.json").write_bytes(source.read_bytes())
    contract = load_workload_contract()
    raw_paths = []
    total = 0
    for tenant, workload_class, model, _ in cohorts:
        work = tmp_path / "benchmarks/.work" / run_id / f"{tenant}-{workload_class}"
        work.mkdir(parents=True)
        repository, revision = LAMBDA_TOKENIZERS[model]
        dataset = generate_text_dataset(
            contract,
            tenant=tenant,
            workload_class=workload_class,
            trial=1,
            sessions=sessions,
            offered_sessions_per_second=rate,
            count_tokens=lambda text: len(text.split()),
            tokenizer_repository=repository,
            tokenizer_revision=revision,
        )
        save_generated(dataset, work / "dataset.jsonl")
        expected = dataset.manifest["plannedTurnCount"]
        total += expected
        artifacts = work / "raw"
        artifacts.mkdir()
        (artifacts / "profile_export_aiperf.json").write_text(
            json.dumps(
                {
                    "aiperf_version": "0.12.0",
                    "was_cancelled": False,
                    "error_summary": [],
                    "request_count": {"avg": expected},
                    "time_to_first_token": {
                        "count": expected,
                        "p50": 30,
                        "p95": 50,
                        "p99": 60,
                    },
                    "inter_token_latency": {"p95": 10},
                    "request_latency": {"p95": 200, "p99": 240},
                    "output_token_throughput": {"avg": 120},
                }
            ),
            encoding="utf-8",
        )
        raw = artifacts / "profile_export_raw.jsonl"
        raw.write_text(
            "\n".join(
                json.dumps(
                    {
                        "metadata": {
                            "x_request_id": f"{tenant}-{index}",
                            "benchmark_phase": "profiling",
                            "request_start_ns": 1_791_890_000_000_000_000 + index * 1_000_000,
                            "request_end_ns": 1_791_890_000_100_000_000 + index * 1_000_000,
                        },
                        "status": 200,
                        "payload": "PRIVATE_SYNTHETIC_PROMPT",
                        "responses": [{"packets": [{"name": "data", "value": "[DONE]"}]}],
                    }
                )
                for index in range(expected)
            )
            + "\n",
            encoding="utf-8",
        )
        raw_paths.append(raw)
    summary = build(run_id, tmp_path)
    assert summary["matrix"] == matrix
    assert summary["totalCompleted"] == total
    assert summary["totalScheduledTurns"] == total
    assert all(row["gatewayAdmitted"] is None for row in summary["rows"])
    output = render(summary)
    assert "PRIVATE_SYNTHETIC_PROMPT" not in "".join(output.values())
    assert "not-published" in output["index.html"]
    raw_paths[-1].write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="request IDs"):
        build(run_id, tmp_path)


def test_report_rejects_unreviewed_run_identity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="run ID"):
        build("../../secrets", tmp_path)
