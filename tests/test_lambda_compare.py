"""Paired reports compare only three matched, sanitized trial summaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.lambda_compare import TELEMETRY_METRICS, load_trials, mean_ci, render, summarize


def _report(root: Path, trial: int, variant: str, *, ttft: float) -> str:
    run_id = f"lambda-nominal-{variant}-20261013-{130000 + trial:06d}"
    path = root / "benchmarks/reports" / run_id
    path.mkdir(parents=True)
    classes = {
        workload_class: {
            "completed": 110,
            "ttftP95Ms": ttft,
            "tpotP95Ms": 10.0,
            "e2eP95Ms": ttft + 10,
            "outputTokensPerSecond": 60.0,
            "successRate": 1.0,
            "outputTokens": 1000,
        }
        for workload_class in ("chat", "agentic-style", "batch")
    }
    summary = {
        "schemaVersion": 1,
        "runId": run_id,
        "variant": variant,
        "trial": trial,
        "status": "measured-variant-one-of-three-trials",
        "scheduled": 330,
        "sent": 330,
        "completed": 330,
        "rejected": 0,
        "failed": 0,
        "campaignSha256": "campaign",
        "workloadSha256": "workload",
        "offeredPlan": {"same": True},
        "servedModelRevisions": {"qwen3": "revision"},
        "vllmImage": "pinned-image",
        "eppValuesSha256": "candidate-hash" if variant == "large-flow6" else "baseline-hash",
        "startUtc": "2026-10-13T12:57:24.2Z",
        "endUtc": "2026-10-13T13:07:27.4Z",
        "classes": classes,
        "rows": [
            {
                "tenant": tenant,
                "class": workload_class,
                "scheduled": 37,
                "completed": 37,
                "rejected": 0,
                "failed": 0,
                "ttftP95Ms": ttft,
            }
            for tenant in ("tenant-a", "tenant-b", "tenant-c")
            for workload_class in ("chat", "agentic-style", "batch")
        ],
        "cost": {
            "tenantCostIsEstimateNotInvoice": True,
            "infrastructureAuditUsd": 4.3,
            "openCostNodeAssetsUsd": 4.25,
            "reconciliationToleranceUsd": 0.4,
            "nodeHourlyUsd": {"node": 25.6},
            "fleetHourlyUsd": 25.6,
        },
    }
    (path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (path / "telemetry.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "runId": run_id,
                "variant": variant,
                "trial": trial,
                "windowStartUtc": "2026-10-13T12:57:24Z",
                "windowEndUtc": "2026-10-13T13:07:28Z",
                "windowPrecisionSeconds": 1,
                "metrics": {metric: 12.5 for metric in TELEMETRY_METRICS},
            }
        ),
        encoding="utf-8",
    )
    return run_id


def test_three_trial_baseline_and_paired_candidate(tmp_path: Path) -> None:
    trials = (1, 2, 3)
    baseline_ids = [
        _report(tmp_path, trial, "baseline", ttft=3000 + trial * 100) for trial in trials
    ]
    candidate_ids = [
        _report(tmp_path, trial, "large-flow6", ttft=2000 + trial * 100) for trial in trials
    ]
    baseline = load_trials(baseline_ids, tmp_path)
    candidate = load_trials(candidate_ids, tmp_path)
    result = summarize(baseline, candidate)
    assert result["status"] == "paired-comparison"
    delta = result["classes"]["agentic-style"]["ttftP95Ms"]["pairedDelta"]
    assert delta["mean"] == -1000
    assert delta["ci95High"] == -1000
    assert result["slo"]["agentic-style"]["ttftP95Ms"]["baseline"] == "fail"
    assert result["slo"]["agentic-style"]["taskP95Ms"]["baseline"].startswith("inconclusive")
    assert result["slo"]["batch"]["withinMs"]["baseline"].startswith("not measured")
    assert result["telemetry"]["largeBatchQueueP95Ms"]["pairedDelta"]["mean"] == 0
    assert result["tenantClasses"]["tenant-a"]["chat"]["ttftP95Ms"]["pairedDelta"]["mean"] == -1000
    assert "Candidate" in render(result)["index.html"]
    assert "dispatched requests in that priority band" in render(result)["index.html"]
    assert "telemetry,largeBatchQueueP95Ms" in render(result)["summary.csv"]


def test_comparison_rejects_incomplete_trials_and_drift(tmp_path: Path) -> None:
    ids = [_report(tmp_path, trial, "baseline", ttft=1000) for trial in (1, 2, 3)]
    with pytest.raises(ValueError, match="three distinct"):
        load_trials(ids[:2] + ids[:1], tmp_path)
    path = tmp_path / "benchmarks/reports" / ids[2] / "summary.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["cost"]["nodeHourlyUsd"] = {"foreign-node": 25.6}
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="hardware"):
        load_trials(ids, tmp_path)


def test_student_t_interval_for_three_seeded_trials() -> None:
    result = mean_ci([1.0, 2.0, 3.0])
    assert result["mean"] == 2.0
    assert result["ci95Low"] < 0
    assert result["ci95High"] > 4


def test_second_reviewed_flow_variant_is_comparable(tmp_path: Path) -> None:
    baseline = load_trials(
        [_report(tmp_path, trial, "baseline", ttft=3000) for trial in (1, 2, 3)], tmp_path
    )
    candidate = load_trials(
        [_report(tmp_path, trial, "large-flow8", ttft=1800) for trial in (1, 2, 3)], tmp_path
    )
    result = summarize(baseline, candidate)
    assert result["classes"]["agentic-style"]["ttftP95Ms"]["candidate"]["mean"] == 1800
