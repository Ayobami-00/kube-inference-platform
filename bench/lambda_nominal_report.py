"""Sanitized exact-window Lambda nominal baseline with audited node rental cost.

Only aggregate metrics, public model names, reviewed seeds, and published rental
rates enter the report. Prompts, responses, credentials, and raw traces remain
in the ignored private work directory.
"""

# ruff: noqa: E501 -- static HTML and human-readable data-quality text are literal.

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import importlib
import io
import json
import math
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .lambda_campaign import load_campaign
from .lambda_nominal import CLASS_PLAN, VARIANTS, inspect_cohort, validate_plan
from .lambda_trace import NINE_COHORTS
from .pilot_dataset import LAMBDA_TOKENIZERS
from .workloads import derived_seed, load_workload_contract

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = re.compile(r"lambda-nominal-(?:baseline|large-flow6|large-flow8)-[0-9]{8}-[0-9]{6}\Z")
MODEL_NAMES = {row[2] for row in NINE_COHORTS}
RECORD_METRICS = {
    "time_to_first_token": "ms",
    "inter_token_latency": "ms",
    "request_latency": "ms",
    "input_sequence_length": "tokens",
    "output_token_count": "tokens",
}


def percentile(values: list[float], fraction: float) -> float:
    if not values or not 0 <= fraction <= 1 or any(not math.isfinite(v) for v in values):
        raise ValueError("percentile samples are missing or invalid")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 3)


def aggregate_samples(samples: list[dict]) -> dict:
    if not samples:
        raise ValueError("AIPerf metric samples are empty")
    metrics = [item["metrics"] for item in samples]
    elapsed = (
        max(item["endNs"] for item in samples) - min(item["startNs"] for item in samples)
    ) / 1e9
    if elapsed <= 0:
        raise ValueError("AIPerf metric sample window is invalid")
    result = {"completed": len(samples)}
    for prefix, name in (
        ("ttft", "time_to_first_token"),
        ("tpot", "inter_token_latency"),
        ("e2e", "request_latency"),
    ):
        values = [item[name] for item in metrics]
        for label, fraction in (("P50Ms", 0.5), ("P95Ms", 0.95), ("P99Ms", 0.99)):
            result[prefix + label] = percentile(values, fraction)
    result["inputTokens"] = round(sum(item["input_sequence_length"] for item in metrics))
    result["outputTokens"] = round(sum(item["output_token_count"] for item in metrics))
    result["outputTokensPerSecond"] = round(result["outputTokens"] / elapsed, 3)
    result["requestsPerSecond"] = round(len(samples) / elapsed, 3)
    return result


def task_durations(samples: list[dict], expected_sessions: int) -> list[float]:
    sessions: dict[str, list[dict]] = defaultdict(list)
    for item in samples:
        identifier = item.get("conversationId")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("AIPerf agentic task identity is missing")
        sessions[identifier].append(item)
    if len(sessions) != expected_sessions:
        raise ValueError("AIPerf agentic task count differs from scheduled sessions")
    return [
        (max(item["endNs"] for item in turns) - min(item["startNs"] for item in turns)) / 1e6
        for turns in sessions.values()
    ]


def read_records(path: Path, expected: int, successful_ids: set[str] | None = None) -> list[dict]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("private AIPerf metric records are missing or unsafe")
    rows = []
    seen = set()
    included = set()
    with path.open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            metadata = record.get("metadata") or {}
            identifier = metadata.get("x_request_id")
            start, end = metadata.get("request_start_ns"), metadata.get("request_end_ns")
            if (
                not isinstance(identifier, str)
                or not identifier
                or identifier in seen
                or metadata.get("benchmark_phase") != "profiling"
                or type(start) is not int
                or type(end) is not int
                or not 0 < start <= end
            ):
                raise ValueError("AIPerf metric record identity/timestamps are invalid")
            seen.add(identifier)
            if successful_ids is not None and identifier not in successful_ids:
                continue
            included.add(identifier)
            samples = {}
            for name, unit in RECORD_METRICS.items():
                item = (record.get("metrics") or {}).get(name) or {}
                value = item.get("value")
                if (
                    item.get("unit") != unit
                    or isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError(f"AIPerf {name} sample is missing or invalid")
                samples[name] = float(value)
            rows.append(
                {
                    "startNs": start,
                    "endNs": end,
                    "conversationId": metadata.get("conversation_id"),
                    "metrics": samples,
                }
            )
    if len(rows) != expected:
        raise ValueError("AIPerf metric sample count differs from scheduled turns")
    if successful_ids is not None and successful_ids != included:
        raise ValueError("AIPerf metric outcome IDs are inconsistent")
    return rows


def request_json(base: str, path: str, params: dict[str, str]) -> dict:
    if not re.fullmatch(r"http://10\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}:9003", base):
        raise ValueError("OpenCost must use the private reviewed ClusterIP service")
    url = base + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as response:
        if response.status != 200:
            raise ValueError("OpenCost returned a non-200 response")
        value = json.load(response)
    if value.get("code") != 200:
        raise ValueError("OpenCost exact-window query failed")
    return value


def reconcile_cost(
    snapshot: dict, assets: dict, allocation: dict, usage: dict, elapsed_seconds: float
) -> dict:
    prices = {item["node"]: float(item["hourlyUsd"]) for item in snapshot["nodes"]}
    nodes = {
        value.get("properties", {}).get("name"): value
        for value in (assets.get("data") or {}).values()
        if isinstance(value, dict) and value.get("type") == "Node"
    }
    if set(nodes) != set(prices):
        raise ValueError("OpenCost exact-window assets do not match task-owned Lambda nodes")
    audit = sum(prices.values()) * elapsed_seconds / 3600
    observed = 0.0
    for node, price in prices.items():
        item = nodes[node]
        minutes = item.get("minutes")
        cost = item.get("totalCost")
        if (
            isinstance(minutes, bool)
            or isinstance(cost, bool)
            or not isinstance(minutes, (int, float))
            or not isinstance(cost, (int, float))
            or not math.isfinite(minutes)
            or not math.isfinite(cost)
            or minutes <= 0
            or cost <= 0
            or abs(cost * 60 / minutes - price) > max(0.01, price * 0.05)
        ):
            raise ValueError("OpenCost exact-window node rate does not reconcile")
        observed += cost
    tolerance = max(audit * 0.05, sum(prices.values()) / 60)
    if abs(observed - audit) > tolerance:
        raise ValueError("OpenCost node total differs from the rental audit beyond tolerance")

    def models(payload: dict, basis: str) -> dict[str, dict]:
        values = (payload.get("data") or {}).get("inferenceCosts")
        if not isinstance(values, dict) or set(values) != MODEL_NAMES:
            raise ValueError(f"OpenCost {basis} model attribution is incomplete")
        result = {}
        for model, item in values.items():
            total = item.get("totalCost")
            prompt = item.get("promptTokens")
            output = item.get("generationTokens")
            if (
                item.get("costBasis") != basis
                or any(
                    isinstance(v, bool) or not isinstance(v, (int, float))
                    for v in (total, prompt, output)
                )
                or not all(math.isfinite(v) for v in (total, prompt, output))
                or total <= 0
                or prompt <= 0
                or output <= 0
            ):
                raise ValueError(f"OpenCost {basis} {model} cost or token attribution is missing")
            result[model] = {
                "usd": round(total, 6),
                "promptTokens": prompt,
                "generationTokens": output,
            }
        return result

    allocated = models(allocation, "allocation")
    used = models(usage, "usage")
    if sum(item["usd"] for item in allocated.values()) > audit + tolerance:
        raise ValueError("OpenCost model allocation exceeds the node rental audit")
    return {
        "currency": "USD",
        "billingSource": snapshot["billingSource"],
        "nodeHourlyUsd": prices,
        "fleetHourlyUsd": float(snapshot["activeHourlyUsd"]),
        "infrastructureAuditUsd": round(audit, 6),
        "openCostNodeAssetsUsd": round(observed, 6),
        "reconciliationToleranceUsd": round(tolerance, 6),
        "modelAllocated": allocated,
        "modelUsage": used,
        "unattributedIdleSharedUsd": round(
            max(0, audit - sum(item["usd"] for item in allocated.values())), 6
        ),
        "tenantCostIsEstimateNotInvoice": True,
    }


def build(run_id: str, root: Path = ROOT, *, cost_base: str) -> dict:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("Lambda nominal run ID is not reviewed")
    work = root / "benchmarks/.work" / run_id
    if work.is_symlink() or not work.is_dir():
        raise ValueError("private nominal run is missing or unsafe")
    run = json.loads((work / "run.json").read_text(encoding="utf-8"))
    campaign = load_campaign(root / "benchmarks/campaigns/lambda-v1.json")
    contract = load_workload_contract("pilot-v1")
    models_lock = json.loads((root / "deploy/lambda/models.json").read_text(encoding="utf-8"))
    versions_lock = json.loads((root / "deploy/fleet/versions.json").read_text(encoding="utf-8"))
    served_revisions = {
        item["servedName"]: item["revision"] for item in models_lock["pools"].values()
    }
    plan = validate_plan(run.get("trial"))
    if (
        run.get("status") != "complete"
        or run.get("variant") not in VARIANTS
        or not run_id.startswith(f"lambda-nominal-{run['variant']}-")
        or run.get("runId") != run_id
        or run.get("campaignSha256") != campaign["sha256"]
        or run.get("workloadSha256") != contract.sha256
        or run.get("classPlan") != CLASS_PLAN
        or run.get("minimumScheduledTurnsByClass") != plan["minimumScheduledTurnsByClass"]
    ):
        raise ValueError("nominal run manifest is incomplete or drifted")
    groups = defaultdict(list)
    agentic_tasks: list[float] = []
    rows = []
    starts, ends = [], []
    for tenant, workload_class, model, _ in NINE_COHORTS:
        cohort = work / f"{tenant}-{workload_class}"
        dataset = cohort / "dataset.jsonl"
        if cohort.is_symlink() or dataset.is_symlink() or not dataset.is_file():
            raise ValueError("private nominal dataset is missing or unsafe")
        manifest = json.loads((cohort / "dataset.manifest.json").read_text(encoding="utf-8"))
        expected = manifest["plannedTurnCount"]
        expected_item = CLASS_PLAN[workload_class]
        if (
            manifest.get("contractSha256") != contract.sha256
            or manifest.get("tenant") != tenant
            or manifest.get("class") != workload_class
            or manifest.get("trial") != run["trial"]
            or manifest.get("seed") != derived_seed(contract, tenant, workload_class, run["trial"])
            or manifest.get("sessionCount") != expected_item["sessionsPerTenant"]
            or manifest.get("offeredSessionsPerSecond")
            != expected_item["offeredSessionsPerSecondPerTenant"]
            or not isinstance(expected, int)
            or expected < expected_item["sessionsPerTenant"]
            or manifest.get("tokenizerRepository") != LAMBDA_TOKENIZERS[model][0]
            or manifest.get("tokenizerRevision") != LAMBDA_TOKENIZERS[model][1]
            or manifest.get("datasetSha256") != hashlib.sha256(dataset.read_bytes()).hexdigest()
        ):
            raise ValueError(f"{tenant}/{workload_class} dataset provenance drifted")
        raw = cohort / "raw"
        if raw.is_symlink() or not raw.is_dir():
            raise ValueError("private nominal AIPerf output is missing or unsafe")
        outcome = inspect_cohort(work, tenant, workload_class, run["trial"])
        successful_ids = {
            item["metadata"]["x_request_id"]
            for item in (
                json.loads(line)
                for line in (raw / "profile_export_raw.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            )
            if item["status"] == 200
        }
        samples = read_records(raw / "profile_export.jsonl", outcome["completed"], successful_ids)
        if workload_class == "agentic-style":
            agentic_tasks.extend(task_durations(samples, expected_item["sessionsPerTenant"]))
        starts.extend(item["startNs"] for item in samples)
        ends.extend(item["endNs"] for item in samples)
        groups[workload_class].extend(samples)
        rows.append(
            {
                "tenant": tenant,
                "class": workload_class,
                "model": model,
                "scheduled": expected,
                "rejected": outcome["rejected"],
                "failed": outcome["failed"],
                **aggregate_samples(samples),
            }
        )
    if not starts or not ends:
        raise ValueError("nominal AIPerf timestamps are unavailable")
    start = datetime.fromtimestamp(min(starts) / 1e9, UTC)
    end = datetime.fromtimestamp(max(ends) / 1e9, UTC)
    elapsed = (end - start).total_seconds()
    if elapsed <= 0 or elapsed > 20 * 60:
        raise ValueError("nominal trial wall time is outside the reviewed bound")
    class_results = {}
    for workload_class, samples in groups.items():
        count = len(samples)
        if count < campaign["minimumCompletedPerClassPerNominalTrial"]:
            raise ValueError("nominal class has too few completed samples for P95")
        class_results[workload_class] = aggregate_samples(samples)
        scheduled = sum(row["scheduled"] for row in rows if row["class"] == workload_class)
        rejected = sum(row["rejected"] for row in rows if row["class"] == workload_class)
        class_results[workload_class]["scheduled"] = scheduled
        class_results[workload_class]["rejected"] = rejected
        class_results[workload_class]["successRate"] = round(count / scheduled, 6)
        threshold = contract.data["classes"][workload_class]["slo"]["successRateMin"]
        class_results[workload_class]["trialSuccessRateSlo"] = (
            "pass" if count / scheduled >= threshold else "miss"
        )
    class_results["agentic-style"]["taskCount"] = len(agentic_tasks)
    class_results["agentic-style"]["taskP95Ms"] = percentile(agentic_tasks, 0.95)
    class_results["agentic-style"]["taskSampleStatus"] = "provisional-27-tasks"
    expected_models = {"qwen3-8b", "qwen3-next-80b-a3b-instruct"}
    if expected_models != MODEL_NAMES:
        raise ValueError("reviewed Lambda model routing drifted")
    verify_price = importlib.import_module("deploy.lambda.verify_pricing").verify
    snapshot = verify_price(
        root / "fleet/.work/lambda-cost-ledger.json",
        root / "fleet/.work/lambda-opencost-pricing.csv",
    )
    # OpenCost accepts whole-second UTC boundaries but rejects microseconds.
    # Enclose the measured requests by less than one second on each side and
    # retain both sets of boundaries in the published audit trail.
    cost_start = start.replace(microsecond=0)
    cost_end = end.replace(microsecond=0)
    if end.microsecond:
        cost_end += timedelta(seconds=1)
    window = ",".join(
        moment.isoformat().replace("+00:00", "Z") for moment in (cost_start, cost_end)
    )
    assets = request_json(cost_base, "/assets", {"window": window})
    allocated = request_json(
        cost_base,
        "/inferenceCost/total",
        {"window": window, "costBasis": "allocation", "aggregate": "model_name"},
    )
    used = request_json(
        cost_base,
        "/inferenceCost/total",
        {"window": window, "costBasis": "usage", "aggregate": "model_name"},
    )
    cost = reconcile_cost(
        snapshot, assets, allocated, used, (cost_end - cost_start).total_seconds()
    )
    cost["windowStartUtc"] = cost_start.isoformat().replace("+00:00", "Z")
    cost["windowEndUtc"] = cost_end.isoformat().replace("+00:00", "Z")
    cost["windowPrecisionSeconds"] = 1
    totals_by_model = defaultdict(float)
    for row in rows:
        totals_by_model[row["model"]] += row["inputTokens"] + row["outputTokens"]
    for row in rows:
        token_share = (row["inputTokens"] + row["outputTokens"]) / totals_by_model[row["model"]]
        row["estimatedAllocatedUsd"] = round(
            cost["modelAllocated"][row["model"]]["usd"] * token_share, 6
        )
        row["estimatedUsageUsd"] = round(cost["modelUsage"][row["model"]]["usd"] * token_share, 6)
    start_utc = start.isoformat().replace("+00:00", "Z")
    end_utc = end.isoformat().replace("+00:00", "Z")
    from_ms, to_ms = min(starts) // 1_000_000, max(ends) // 1_000_000
    grafana = (
        "http://localhost:3000/d/kip-llmd-lambda-campaign/kip-llmd-lambda-campaign?"
        + urllib.parse.urlencode({"from": from_ms, "to": to_ms, "timezone": "utc"})
    )
    return {
        "schemaVersion": 1,
        "runId": run_id,
        "variant": run["variant"],
        "trial": run["trial"],
        "status": "measured-variant-one-of-three-trials",
        "campaignSha256": campaign["sha256"],
        "workloadSha256": contract.sha256,
        "servedModelRevisions": served_revisions,
        "vllmImage": versions_lock["inference"]["vllmLinuxAmd64Image"],
        "eppValuesSha256": run.get("eppValuesSha256"),
        "offeredPlan": CLASS_PLAN,
        "startUtc": start_utc,
        "endUtc": end_utc,
        "elapsedSeconds": round(elapsed, 3),
        "scheduled": sum(row["scheduled"] for row in rows),
        "sent": sum(row["scheduled"] for row in rows),
        "completed": sum(row["completed"] for row in rows),
        "rejected": sum(row["rejected"] for row in rows),
        "failed": sum(row["failed"] for row in rows),
        "outputTokensPerSecond": round(sum(row["outputTokens"] for row in rows) / elapsed, 3),
        "classes": class_results,
        "rows": rows,
        "cost": cost,
        "grafanaUrl": grafana,
        "dataQuality": [
            "One of three required nominal trials; confidence intervals and final SLO decisions are pending.",
            "This first nominal rate is provisional; a sustainable mixed-load calibration and 70% rate have not yet been established.",
            "HTTP 429s are measured gateway/EPP rejections, not silently discarded completions; EPP queue and dispatch transitions are not yet reconciled to this exact window.",
            "Agentic task P95 uses 27 dependent AIPerf replay sessions and is provisional; batch ten-minute completion and voice SLOs require separate evidence.",
            "Tenant/model cost shares are token-proportional estimates, not Lambda invoice lines.",
            "OpenCost supports whole-second windows; the audited cost window encloses measured request timestamps by less than one second at each edge.",
        ],
    }


def render(summary: dict) -> dict[str, str]:
    columns = (
        "tenant",
        "class",
        "model",
        "completed",
        "rejected",
        "ttftP95Ms",
        "tpotP95Ms",
        "e2eP95Ms",
        "inputTokens",
        "outputTokens",
        "estimatedAllocatedUsd",
    )
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows({key: row[key] for key in columns} for row in summary["rows"])
    headers = "".join(f"<th>{html.escape(key)}</th>" for key in columns)
    rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(row[key]))}</td>" for key in columns) + "</tr>"
        for row in summary["rows"]
    )
    classes = "".join(
        f"<li>{html.escape(name)}: {value['completed']} turns, "
        f"TTFT P95 {value['ttftP95Ms']:.1f} ms, TPOT P95 {value['tpotP95Ms']:.1f} ms</li>"
        for name, value in summary["classes"].items()
    )
    warnings = "".join(f"<li>{html.escape(item)}</li>" for item in summary["dataQuality"])
    cost = summary["cost"]
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KIP Lambda nominal {html.escape(summary["variant"])} · {html.escape(summary["runId"])}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:1150px;margin:40px auto;padding:0 20px;background:#0b1220;color:#e7eef8}}.muted{{color:#a8b6c9}}.card{{background:#172337;border:1px solid #344359;border-radius:12px;padding:20px;margin:22px 0}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}th,td{{padding:8px;border-bottom:1px solid #344359;text-align:right}}th:first-child,td:first-child{{text-align:left}}a{{color:#86c5ff}}</style>
<h1>Three-tenant nominal text · {html.escape(summary["variant"])}</h1><p class="muted">{html.escape(summary["runId"])} · trial {summary["trial"]} of 3 · {summary["startUtc"]} to {summary["endUtc"]}</p>
<div class="card"><h2>Completion and class latency</h2><p>{summary["completed"]} of {summary["scheduled"]} scheduled turns completed with SSE final markers; {summary["rejected"]} were rejected with HTTP 429.</p><ul>{classes}</ul></div>
<div class="card"><h2>Cost accounting</h2><p>Listed fleet rate: ${cost["fleetHourlyUsd"]:.2f}/hour. Exact-window rental audit: ${cost["infrastructureAuditUsd"]:.4f}; OpenCost node assets: ${cost["openCostNodeAssetsUsd"]:.4f}. Unattributed idle/shared remainder: ${cost["unattributedIdleSharedUsd"]:.4f}.</p><p>Tenant shares below are token-proportional model-allocation estimates, not invoice charges. {html.escape(cost["billingSource"])}</p></div>
<div class="card"><h2>Tenant and class metrics</h2><table><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table></div>
<div class="card"><h2>Data quality and next comparisons</h2><ul>{warnings}</ul><p>No optimization win or final SLO claim is made from one trial. A paired variant must replay the same seeds, hardware, model revision, and offered rate.</p><a href="{html.escape(summary["grafanaUrl"], quote=True)}">Exact Grafana window</a> (private port-forward required).</div>
<p><a href="summary.json">Summary JSON</a> · <a href="summary.csv">Summary CSV</a></p></html>"""
    return {
        "summary.json": json.dumps(summary, sort_keys=True, indent=2) + "\n",
        "summary.csv": buffer.getvalue(),
        "index.html": document,
    }


def publish(run_id: str, *, cost_base: str, root: Path = ROOT) -> Path:
    contents = render(build(run_id, root, cost_base=cost_base))
    destination = root / "benchmarks/reports" / run_id
    destination.mkdir(parents=True, exist_ok=False)
    for name, value in contents.items():
        (destination / name).write_text(value, encoding="utf-8")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a sanitized Lambda nominal baseline")
    parser.add_argument("run_id")
    parser.add_argument("--opencost-url", required=True)
    args = parser.parse_args()
    try:
        print(publish(args.run_id, cost_base=args.opencost_url))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Lambda nominal report failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
