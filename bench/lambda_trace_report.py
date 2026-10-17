"""Publish a sanitized, explicitly non-SLO report for the short Lambda trace."""

# ruff: noqa: E501 -- static HTML and human-readable quality warnings are literal.

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import math
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from .lambda_campaign import load_campaign
from .lambda_trace import MATRICES, validate_packets
from .pilot_dataset import LAMBDA_TOKENIZERS
from .workloads import derived_seed, load_workload_contract

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = re.compile(r"lambda-(?:three-class|nine-cohort)-[0-9]{8}-[0-9]{6}\Z")


def metric(summary: dict, name: str, statistic: str) -> float:
    value = (summary.get(name) or {}).get(statistic)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"AIPerf {name}.{statistic} is missing or invalid")
    return round(float(value), 3)


def boundaries(raw: Path) -> tuple[int, int]:
    starts: list[int] = []
    ends: list[int] = []
    with raw.open(encoding="utf-8") as source:
        for line in source:
            metadata = json.loads(line).get("metadata") or {}
            start, end = metadata.get("request_start_ns"), metadata.get("request_end_ns")
            if type(start) is not int or type(end) is not int or not 0 < start <= end:
                raise ValueError("AIPerf raw timestamps are missing or invalid")
            starts.append(start)
            ends.append(end)
    if not starts:
        raise ValueError("AIPerf raw timestamps are empty")
    return min(starts), max(ends)


def stamp(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, UTC).isoformat().replace("+00:00", "Z")


def build(run_id: str, root: Path = ROOT) -> dict:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("Lambda trace run ID is not reviewed")
    matrix = "nine" if run_id.startswith("lambda-nine-cohort-") else "three"
    cohorts, sessions, rate, _ = MATRICES[matrix]
    work = root / "benchmarks/.work" / run_id
    if work.is_symlink() or not work.is_dir():
        raise ValueError("private Lambda trace run is unavailable")
    campaign = load_campaign(root / "benchmarks/campaigns/lambda-v1.json")
    contract = load_workload_contract("pilot-v1")
    rows = []
    earliest: int | None = None
    latest: int | None = None
    for tenant, workload_class, model, _ in cohorts:
        cohort = work / f"{tenant}-{workload_class}"
        manifest_path = cohort / "dataset.manifest.json"
        dataset_path = cohort / "dataset.jsonl"
        raw_dir = cohort / "raw"
        for path in (manifest_path, dataset_path, raw_dir):
            if path.is_symlink() or not path.exists():
                raise ValueError("private cohort artifact is missing or unsafe")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        repository, revision = LAMBDA_TOKENIZERS[model]
        expected = manifest.get("plannedTurnCount")
        if (
            manifest.get("tenant") != tenant
            or manifest.get("class") != workload_class
            or manifest.get("trial") != 1
            or manifest.get("contractSha256") != contract.sha256
            or manifest.get("seed") != derived_seed(contract, tenant, workload_class, 1)
            or manifest.get("sessionCount") != sessions
            or manifest.get("offeredSessionsPerSecond") != rate
            or manifest.get("tokenizerRepository") != repository
            or manifest.get("tokenizerRevision") != revision
            or manifest.get("datasetSha256")
            != hashlib.sha256(dataset_path.read_bytes()).hexdigest()
            or type(expected) is not int
            or expected < sessions
        ):
            raise ValueError(f"{tenant}/{workload_class} dataset provenance drifted")
        summary = json.loads((raw_dir / "profile_export_aiperf.json").read_text(encoding="utf-8"))
        if (
            summary.get("aiperf_version") != "0.12.0"
            or summary.get("was_cancelled")
            or summary.get("error_summary")
            or metric(summary, "request_count", "avg") != expected
            or metric(summary, "time_to_first_token", "count") != expected
        ):
            raise ValueError(f"{tenant}/{workload_class} AIPerf aggregate is incomplete")
        raw = raw_dir / "profile_export_raw.jsonl"
        completed = validate_packets(raw, expected)
        start_ns, end_ns = boundaries(raw)
        earliest = min(earliest, start_ns) if earliest is not None else start_ns
        latest = max(latest, end_ns) if latest is not None else end_ns
        rows.append(
            {
                "tenant": tenant,
                "class": workload_class,
                "model": model,
                "modelRevision": revision,
                "seed": manifest["seed"],
                "datasetSha256": manifest["datasetSha256"],
                "scheduledSessions": sessions,
                "scheduledTurns": expected,
                "sent": completed,
                "clientCompleted": completed,
                "clientFailed": 0,
                "gatewayAdmitted": None,
                "eppQueued": None,
                "dispatched": None,
                "ttftP50Ms": metric(summary, "time_to_first_token", "p50"),
                "ttftP95Ms": metric(summary, "time_to_first_token", "p95"),
                "ttftP99Ms": metric(summary, "time_to_first_token", "p99"),
                "tpotP95Ms": metric(summary, "inter_token_latency", "p95"),
                "e2eP95Ms": metric(summary, "request_latency", "p95"),
                "e2eP99Ms": metric(summary, "request_latency", "p99"),
                "outputTokensPerSecond": metric(summary, "output_token_throughput", "avg"),
                "startUtc": stamp(start_ns),
                "endUtc": stamp(end_ns),
                "sloStatus": "inconclusive-insufficient-samples",
            }
        )
    assert earliest is not None and latest is not None
    from_ms, to_ms = earliest // 1_000_000, latest // 1_000_000
    grafana = (
        "http://localhost:3000/d/kip-llmd-lambda-campaign/kip-llmd-lambda-campaign?"
        + urlencode({"from": from_ms, "to": to_ms, "timezone": "utc", "var-cluster": "kip-lambda"})
    )
    return {
        "schemaVersion": 1,
        "runId": run_id,
        "matrix": matrix,
        "status": "short-integration-trace-passed-not-slo-campaign",
        "campaignSha256": campaign["sha256"],
        "workloadSha256": contract.sha256,
        "rootSeed": campaign["rootSeed"],
        "aiperfVersion": "0.12.0",
        "hardware": "us-east-1 A100-40 and A100-80 serving pool; non-HA control plane",
        "startUtc": stamp(earliest),
        "endUtc": stamp(latest),
        "totalScheduledTurns": sum(row["scheduledTurns"] for row in rows),
        "totalCompleted": sum(row["clientCompleted"] for row in rows),
        "rows": rows,
        "grafanaUrl": grafana,
        "costStatus": "not-published: per-node billing and OpenCost attribution not reconciled",
        "sloStatus": "inconclusive: one short trial, fewer than 100 samples per class",
        "warnings": [
            "This is a short deterministic integration trace, not a calibrated 70% nominal campaign.",
            "Only the nine-cohort matrix exercises every tenant/class pair independently.",
            "Gateway-admitted, EPP-queued, and dispatched counts require exact-window accounting and are not inferred from client success.",
            "Different classes use different models and output lengths; their throughput bars are not an optimization comparison.",
            "No tenant cost or SLO pass claim is made without bill reconciliation and three full trials.",
        ],
    }


def render(summary: dict) -> dict[str, str]:
    columns = (
        "tenant",
        "class",
        "model",
        "scheduledTurns",
        "clientCompleted",
        "ttftP50Ms",
        "ttftP95Ms",
        "tpotP95Ms",
        "e2eP95Ms",
        "outputTokensPerSecond",
    )
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows({key: row[key] for key in columns} for row in summary["rows"])
    maximum = max(row["ttftP95Ms"] for row in summary["rows"])
    bars = "".join(
        '<div class="bar-row"><span>'
        + html.escape(row["class"])
        + '</span><div class="track"><div class="bar" style="width:'
        + f"{row['ttftP95Ms'] / maximum * 100:.1f}%"
        + '"></div></div><strong>'
        + f"{row['ttftP95Ms']:.0f} ms"
        + "</strong></div>"
        for row in summary["rows"]
    )
    table = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(row[key]))}</td>" for key in columns) + "</tr>"
        for row in summary["rows"]
    )
    warnings = "".join(f"<li>{html.escape(item)}</li>" for item in summary["warnings"])
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KIP Lambda three-tenant trace · {summary["runId"]}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:1080px;margin:40px auto;padding:0 20px;background:#0b1220;color:#e7eef8}}h1{{line-height:1.1}}.muted{{color:#a8b6c9}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}th,td{{padding:9px;border-bottom:1px solid #344359;text-align:right}}th:first-child,td:first-child{{text-align:left}}.card{{background:#172337;border:1px solid #344359;border-radius:12px;padding:20px;margin:22px 0}}.bar-row{{display:flex;align-items:center;gap:12px;margin:12px 0}}.bar-row span{{width:130px}}.track{{flex:1;background:#344359;border-radius:5px;height:20px;border-radius:5px}}.bar{{background:#54c7b5;height:20px;border-radius:5px}}.bar-row strong{{width:100px;text-align:right}}a{{color:#86c5ff}}</style>
<h1>Three-tenant text integration trace</h1><p class="muted">{summary["runId"]} · {summary["matrix"]} cohort matrix · AIPerf 0.12.0 · root seed 42 · {summary["startUtc"]} to {summary["endUtc"]}</p>
<p>All {summary["totalCompleted"]} scheduled turns completed with final SSE markers across chat, agentic-style replay, and batch. This is not the full mixed-tenant SLO campaign.</p>
<div class="card"><h2>TTFT P95 by class</h2>{bars}</div>
<div class="card"><h2>Client-observed results</h2><table><thead><tr>{"".join(f"<th>{html.escape(key)}</th>" for key in columns)}</tr></thead><tbody>{table}</tbody></table></div>
<div class="card"><h2>Interpretation and data quality</h2><ul>{warnings}</ul><p>{html.escape(summary["costStatus"])}</p><p><a href="{html.escape(summary["grafanaUrl"], quote=True)}">Open exact Grafana window</a> (requires local port-forward).</p></div>
<p><a href="summary.json">Summary JSON</a> · <a href="summary.csv">Summary CSV</a></p></html>"""
    return {
        "summary.json": json.dumps(summary, sort_keys=True, indent=2) + "\n",
        "summary.csv": buffer.getvalue(),
        "index.html": document,
    }


def publish(run_id: str, root: Path = ROOT) -> Path:
    contents = render(build(run_id, root))
    destination = root / "benchmarks/reports" / run_id
    destination.mkdir(parents=True, exist_ok=False)
    for name, value in contents.items():
        (destination / name).write_text(value, encoding="utf-8")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish sanitized Lambda text-trace checkpoint")
    parser.add_argument("run_id")
    args = parser.parse_args()
    try:
        print(publish(args.run_id))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"Lambda trace report failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
