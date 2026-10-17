"""Publish a narrow, sanitized AIPerf hardware-control checkpoint.

This is deliberately not a campaign/SLO or bill-reconciled cost report. It
copies only reviewed aggregate numbers, never AIPerf inputs or request traces.
"""

# ruff: noqa: E501 -- reviewed pin and embedded HTML/CSS are intentionally literal.

from __future__ import annotations

import csv
import html
import io
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "lambda-h100-pcie-control-20261013"
SOURCE = ROOT / "benchmarks/.work/lambda-h100-control"
DESTINATION = ROOT / "benchmarks/reports" / RUN_ID
IMAGE = "docker.io/vllm/vllm-openai:v0.30.0@sha256:5f5e535216848d0c52159c8c13a0af04be5f6fe1a84e79914300610796f76d40"
REVISION = "b968826d9c46dd6066d109eabc6255188de91218"


def metric(value: dict, name: str, statistic: str) -> float:
    candidate = value.get(name, {}).get(statistic)
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise ValueError(f"AIPerf {name}.{statistic} is missing")
    result = float(candidate)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"AIPerf {name}.{statistic} is invalid")
    return round(result, 3)


def utc(value: str) -> str:
    # This control ran AIPerf on the local Europe/London machine, whose export
    # records wall-clock timestamps without an offset.
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise ValueError("expected AIPerf local wall-clock timestamp")
    return (
        parsed.replace(tzinfo=ZoneInfo("Europe/London"))
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build(source: Path = SOURCE) -> dict:
    sweep = json.loads((source / "sweep_aggregate/profile_export_aiperf_sweep.json").read_text())
    if (
        sweep.get("failed_runs") != []
        or sweep.get("num_profile_runs") != 2
        or sweep.get("num_successful_runs") != 2
        or sweep.get("metadata", {}).get("num_trials_per_value") != 1
    ):
        raise ValueError("H100 control sweep is incomplete or not the reviewed two-cell run")
    aggregate = {
        item["parameters"]["concurrency"]: item["metrics"]
        for item in sweep["per_combination_metrics"]
    }
    if set(aggregate) != {1, 4}:
        raise ValueError("H100 control concurrency cells differ from the reviewed run")
    rows = []
    for concurrency in (1, 4):
        raw = json.loads(
            (source / f"concurrency_{concurrency}/profile_export_aiperf.json").read_text()
        )
        if (
            raw.get("aiperf_version") != "0.12.0"
            or raw.get("error_summary") != []
            or raw.get("was_cancelled") is not False
        ):
            raise ValueError("AIPerf trial version, errors, or cancellation do not pass")
        item = aggregate[concurrency]
        if metric(item, "request_count", "mean") != 20:
            raise ValueError("reviewed control requires 20 completed requests per cell")
        rows.append(
            {
                "concurrency": concurrency,
                "completedRequests": 20,
                "outputTokensPerRequest": metric(item, "output_sequence_length", "mean"),
                "ttftP50Ms": metric(item, "time_to_first_token", "p50"),
                "ttftP95Ms": metric(item, "time_to_first_token", "p95"),
                "tpotP95Ms": metric(item, "inter_token_latency", "p95"),
                "e2eP95Ms": metric(item, "request_latency", "p95"),
                "outputTokensPerSecond": metric(item, "output_token_throughput", "mean"),
                "startUtc": utc(raw["start_time"]),
                "endUtc": utc(raw["end_time"]),
            }
        )
    return {
        "schemaVersion": 1,
        "runId": RUN_ID,
        "status": "exploratory-hardware-control",
        "model": "qwen3-8b",
        "modelRevision": REVISION,
        "runtimeImage": IMAGE,
        "hardware": "1x H100 PCIe 80GB, us-west-3",
        "aiperfVersion": "0.12.0",
        "promptInputTokensMean": 128,
        "requestedOutputTokensMean": 64,
        "seed": 42,
        "listedInstanceUsdPerHour": 3.29,
        "costStatus": "listed rate only; no bill or per-request attribution published",
        "sloStatus": "inconclusive: one trial and 20 requests per cell",
        "rows": rows,
        "warnings": [
            "Exploratory control only: one trial per concurrency; no confidence interval or SLO claim.",
            "This PCIe H100 is not hardware-matched to the east A100 serving pool.",
            "No GPU/Prometheus telemetry was captured for this control run.",
            "Listed hourly price is not a verified invoice or inference allocation.",
        ],
    }


def render(summary: dict) -> dict[str, str]:
    rows = summary["rows"]
    fields = (
        "concurrency",
        "completedRequests",
        "ttftP50Ms",
        "ttftP95Ms",
        "tpotP95Ms",
        "e2eP95Ms",
        "outputTokensPerSecond",
    )
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows({key: row[key] for key in fields} for row in rows)
    maximum = max(row["outputTokensPerSecond"] for row in rows)
    table_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(row[key]))}</td>" for key in fields) + "</tr>"
        for row in rows
    )
    bars = "".join(
        f'<div class="bar-row"><span>Concurrency {row["concurrency"]}</span>'
        f'<div class="track"><div class="bar" style="width:{row["outputTokensPerSecond"] / maximum * 100:.1f}%"></div></div>'
        f"<strong>{row['outputTokensPerSecond']:.1f} tok/s</strong></div>"
        for row in rows
    )
    warnings = "".join(f"<li>{html.escape(item)}</li>" for item in summary["warnings"])
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KIP H100 PCIe control · {RUN_ID}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:920px;margin:40px auto;padding:0 20px;background:#0b1220;color:#e7eef8}}h1{{line-height:1.1}}.muted{{color:#a8b6c9}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}th,td{{padding:10px;border-bottom:1px solid #344359;text-align:right}}th:first-child,td:first-child{{text-align:left}}.card{{background:#172337;border:1px solid #344359;border-radius:12px;padding:20px;margin:22px 0}}.bar-row{{display:flex;align-items:center;gap:12px;margin:12px 0}}.bar-row span{{width:130px}}.track{{flex:1;background:#344359;border-radius:5px;height:20px}}.bar{{background:#54c7b5;height:20px;border-radius:5px}}.bar-row strong{{width:110px;text-align:right}}a{{color:#86c5ff}}</style>
<h1>H100 PCIe control checkpoint</h1><p class="muted">{RUN_ID} · Qwen3-8B · AIPerf 0.12.0 · seed 42</p>
<p>This is a small hardware-control smoke run, not the multi-tenant campaign or a validated SLO result.</p>
<div class="card"><h2>Output throughput</h2>{bars}</div>
<div class="card"><h2>Latency and throughput</h2><table><thead><tr>{"".join(f"<th>{html.escape(key)}</th>" for key in fields)}</tr></thead><tbody>{table_rows}</tbody></table></div>
<div class="card"><h2>Interpretation</h2><ul>{warnings}</ul><p>Listed instance rate: $3.29/hour. No per-request or bill-reconciled cost is asserted.</p></div>
<p><a href="summary.json">Summary JSON</a> · <a href="summary.csv">Summary CSV</a></p></html>"""
    return {
        "summary.json": json.dumps(summary, sort_keys=True, indent=2) + "\n",
        "summary.csv": buffer.getvalue(),
        "index.html": document,
    }


def publish(source: Path = SOURCE, destination: Path = DESTINATION) -> None:
    contents = render(build(source))
    destination.mkdir(parents=True, exist_ok=True)
    for name, value in contents.items():
        (destination / name).write_text(value, encoding="utf-8")


if __name__ == "__main__":
    publish()
