"""Nominal report requires actual AIPerf samples and reconciled model cost."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.lambda_nominal_report import (
    aggregate_samples,
    percentile,
    read_records,
    reconcile_cost,
    task_durations,
)


def test_percentile_and_record_integrity(tmp_path: Path) -> None:
    assert percentile([1, 2, 3, 4, 5], 0.95) == 4.8
    path = tmp_path / "profile_export.jsonl"
    metrics = {
        name: {"unit": unit, "value": 10}
        for name, unit in {
            "time_to_first_token": "ms",
            "inter_token_latency": "ms",
            "request_latency": "ms",
            "input_sequence_length": "tokens",
            "output_token_count": "tokens",
        }.items()
    }
    row = {
        "metadata": {
            "x_request_id": "request-1",
            "benchmark_phase": "profiling",
            "request_start_ns": 1,
            "request_end_ns": 2,
        },
        "metrics": metrics,
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    samples = read_records(path, 1)
    assert samples[0]["metrics"]["time_to_first_token"] == 10
    assert aggregate_samples(samples)["ttftP99Ms"] == 10
    with pytest.raises(ValueError, match="sample count"):
        read_records(path, 2)
    row["metrics"]["time_to_first_token"]["unit"] = "s"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sample"):
        read_records(path, 1)


def test_agentic_task_duration_uses_dependent_turn_boundaries() -> None:
    turns = [
        {"conversationId": "one", "startNs": 1_000_000, "endNs": 2_000_000},
        {"conversationId": "one", "startNs": 5_000_000, "endNs": 8_000_000},
        {"conversationId": "two", "startNs": 3_000_000, "endNs": 9_000_000},
    ]
    assert sorted(task_durations(turns, 2)) == [6.0, 7.0]
    with pytest.raises(ValueError, match="count differs"):
        task_durations(turns, 3)


def _cost_fixture() -> tuple[dict, dict, dict, dict]:
    prices = (1.29, 1.99, 22.32)
    nodes = [f"kip-lambda-test-{index}" for index in range(3)]
    snapshot = {
        "billingSource": "User-confirmed Lambda billing: listed instance rates only",
        "activeHourlyUsd": "25.60",
        "nodes": [
            {"node": name, "hourlyUsd": str(price)}
            for name, price in zip(nodes, prices, strict=True)
        ],
    }
    assets = {
        "data": {
            name: {
                "type": "Node",
                "properties": {"name": name},
                "minutes": 10,
                "totalCost": price / 6,
            }
            for name, price in zip(nodes, prices, strict=True)
        }
    }

    def inference(basis: str) -> dict:
        return {
            "data": {
                "inferenceCosts": {
                    model: {
                        "costBasis": basis,
                        "totalCost": 0.4 if model == "qwen3-8b" else 1.0,
                        "promptTokens": 100,
                        "generationTokens": 200,
                    }
                    for model in ("qwen3-8b", "qwen3-next-80b-a3b-instruct")
                }
            }
        }

    return snapshot, assets, inference("allocation"), inference("usage")


def test_cost_report_separates_bill_and_model_estimates() -> None:
    snapshot, assets, allocated, used = _cost_fixture()
    result = reconcile_cost(snapshot, assets, allocated, used, 600)
    assert result["fleetHourlyUsd"] == 25.6
    assert result["infrastructureAuditUsd"] == pytest.approx(4.266667)
    assert result["modelAllocated"]["qwen3-8b"]["usd"] == 0.4
    assert result["unattributedIdleSharedUsd"] == pytest.approx(2.866667)
    assert result["tenantCostIsEstimateNotInvoice"] is True


def test_cost_report_refuses_price_and_token_gaps() -> None:
    snapshot, assets, allocated, used = _cost_fixture()
    assets["data"]["kip-lambda-test-2"]["totalCost"] *= 2
    with pytest.raises(ValueError, match="node rate"):
        reconcile_cost(snapshot, assets, allocated, used, 600)
    assets = _cost_fixture()[1]
    allocated["data"]["inferenceCosts"]["qwen3-8b"]["promptTokens"] = 0
    with pytest.raises(ValueError, match="token attribution"):
        reconcile_cost(snapshot, assets, allocated, used, 600)
