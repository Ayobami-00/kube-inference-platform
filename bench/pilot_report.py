"""Publish a sanitized, explicitly partial fleet mixed-text report.

Only whitelisted AIPerf aggregates, cross-layer counts, Prometheus telemetry,
and reconciled whole-node rental estimates are published. Raw inputs, outputs,
headers, access logs, request IDs, and network addresses remain ignored locally.
"""

# ruff: noqa: E501 -- embedded HTML/SVG and PromQL remain legible as literal templates.

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import math
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fleet.aiperf_accounting import RUN_ID, inspect_run
from fleet.cost_check import _get_json, _service_ip, reconcile
from fleet.cost_ledger import verify_pricing_files
from fleet.vast import FleetError

from .pilot_dataset import TOKENIZER_REVISION
from .report import _scan_for_sensitive_data
from .workloads import TENANTS, load_workload_contract

SCENARIOS = {
    "pilot-three-tenant-text": "mixed-text-pilot-trial",
    "pilot-chat-calibration": "isolated-text-calibration",
    "pilot-nine-cohort-smoke": "mixed-text-pilot-trial",
    "pilot-worker-loss-during": "replica-failure-checkpoint",
    "pilot-worker-loss-recovered": "replica-failure-checkpoint",
}
DEFAULT_SCENARIO = "pilot-three-tenant-text"
TEXT_CLASSES = {"chat", "agentic-style", "batch"}
MODEL_REVISION = TOKENIZER_REVISION
SAMPLE_MINIMUM = 100


def _scenario(root: Path, name: str) -> dict[str, Any]:
    if name not in SCENARIOS:
        raise FleetError("pilot report scenario is not reviewed or allowlisted")
    path = root / "benchmarks" / "campaigns" / "scenarios" / f"{name}.json"
    if not path.is_file() or path.is_symlink():
        raise FleetError("reviewed pilot scenario is missing or unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or set(value) != {"schemaVersion", "name", "kind", "cohorts"}
        or value["schemaVersion"] != 1
        or value["name"] != name
        or value["kind"] != SCENARIOS[name]
        or not isinstance(value["cohorts"], list)
        or not 1 <= len(value["cohorts"]) <= 9
    ):
        raise FleetError("reviewed pilot scenario schema is invalid")
    pairs: set[tuple[str, str]] = set()
    for item in value["cohorts"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"tenant", "class"}
            or not isinstance(item["tenant"], str)
            or not isinstance(item["class"], str)
            or item["tenant"] not in TENANTS
            or item["class"] not in TEXT_CLASSES
            or (item["tenant"], item["class"]) in pairs
        ):
            raise FleetError("reviewed pilot scenario cohort is invalid or duplicated")
        pairs.add((item["tenant"], item["class"]))
    return {**value, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise FleetError("AIPerf run timestamp is invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise FleetError("AIPerf run timestamp is malformed") from error
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    if result.utcoffset() != timedelta(0):
        raise FleetError("AIPerf run timestamp must be UTC")
    return result


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _metric(summary: dict[str, Any], key: str, statistic: str) -> float:
    value = summary.get(key)
    if not isinstance(value, dict):
        raise FleetError(f"AIPerf aggregate {key} is missing")
    result = value.get(statistic)
    if (
        isinstance(result, bool)
        or not isinstance(result, (int, float))
        or not math.isfinite(result)
    ):
        raise FleetError(f"AIPerf aggregate {key}.{statistic} is invalid")
    return round(float(result), 6)


def _validate_failure_phase(scenario_name: str, entries: list[dict[str, Any]]) -> None:
    """Require exact observed worker routing for reviewed failure checkpoints."""
    if scenario_name not in {"pilot-worker-loss-during", "pilot-worker-loss-recovered"}:
        return
    if len(entries) != 1:
        raise FleetError("replica failure checkpoint requires one reviewed cohort")
    routes = entries[0]["workerDispatches"]
    completed = entries[0]["counts"]["completed"]
    if scenario_name == "pilot-worker-loss-during":
        if routes != {"kip-gpu-2": completed}:
            raise FleetError("replica loss was not served entirely by surviving GPU worker 2")
    elif set(routes) != {"kip-gpu-1", "kip-gpu-2"} or sum(routes.values()) != completed:
        raise FleetError("recovery checkpoint did not reach both GPU workers")


def _dataset_provenance(
    root: Path, summary: dict[str, Any], tenant: str, kind: str
) -> dict[str, Any]:
    datasets = (summary.get("input_config") or {}).get("datasets")
    if not isinstance(datasets, list) or len(datasets) != 1:
        raise FleetError("AIPerf summary must reference one reviewed dataset")
    value = datasets[0]
    path = Path(value.get("path", ""))
    parent = root / "benchmarks" / ".work" / "datasets" / "pilot-v1" / tenant / kind
    if path.parent != parent or not path.name.startswith("trial-") or path.suffix != ".jsonl":
        raise FleetError("AIPerf dataset path is outside the reviewed pilot contract")
    from fleet.aiperf_text import _dataset

    try:
        trial = int(path.stem.removeprefix("trial-"))
    except ValueError as error:
        raise FleetError("AIPerf trial number is invalid") from error
    exact, manifest = _dataset(root, tenant, kind, trial)
    if path != exact:
        raise FleetError("AIPerf used a different dataset than its reviewed manifest")
    return {
        "trial": trial,
        "sessions": manifest["sessionCount"],
        "seed": manifest["seed"],
        "offeredSessionsPerSecond": manifest["offeredSessionsPerSecond"],
        "arrivalSchedule": manifest.get("arrivalSchedule", "legacy-unversioned"),
        "datasetSha256": manifest["datasetSha256"],
        "plannedTurns": manifest["plannedTurnCount"],
    }


def _telemetry(prometheus: str, end: datetime, minutes: int) -> dict[str, Any]:
    duration = f"{minutes}m"
    queries = {
        "gpuUtilizationRatioByNode": f"avg by(node)(avg_over_time(kip:fleet:gpu_utilization_ratio[{duration}]))",
        "gpuMemoryMaxMiBByNode": f"max by(node)(max_over_time(kip:fleet:gpu_memory_used_mib[{duration}]))",
        "gpuPowerAverageWattsByNode": f"avg by(node)(avg_over_time(kip:fleet:gpu_power_watts[{duration}]))",
        "runtimeWaitingMax": f"max(max_over_time(kip:fleet:requests_waiting[{duration}]))",
        "eppQueueMax": f"max_over_time((sum(llm_d_epp_flow_control_queue_size))[{duration}:5s])",
    }
    result: dict[str, Any] = {}
    for label, expression in queries.items():
        response = _get_json(
            prometheus,
            "/api/v1/query",
            {"query": expression, "time": str(end.timestamp())},
        )
        if response.get("status") != "success":
            raise FleetError(f"Prometheus telemetry query {label} failed")
        series = response.get("data", {}).get("result", [])
        if label.endswith("ByNode"):
            values = {
                item.get("metric", {}).get("node"): round(float(item["value"][1]), 6)
                for item in series
            }
            if not {"kip-gpu-1", "kip-gpu-2"} <= set(values):
                raise FleetError(f"Prometheus telemetry {label} lacks both GPU workers")
            result[label] = {node: values[node] for node in ("kip-gpu-1", "kip-gpu-2")}
        else:
            if len(series) != 1:
                raise FleetError(f"Prometheus telemetry {label} is missing")
            result[label] = round(float(series[0]["value"][1]), 6)
    result["eppQueueSource"] = "llm_d_epp_flow_control_queue_size; true flow-control backlog"
    return result


def _routing_provenance(root: Path, started: datetime) -> dict[str, Any]:
    """Bind a trial to the exact owned llm-d release active before it began."""
    expected = hashlib.sha256(
        (root / "deploy" / "fleet" / "llmd-router-flow-values.yaml").read_bytes()
    ).hexdigest()
    lock = json.loads((root / "deploy" / "fleet" / "versions.json").read_text(encoding="utf-8"))
    values_result = subprocess.run(
        ["helm", "get", "values", "kip-qwen25-pilot", "-n", "kip-inference", "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    history_result = subprocess.run(
        ["helm", "history", "kip-qwen25-pilot", "-n", "kip-inference", "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if values_result.returncode or history_result.returncode:
        raise FleetError("owned llm-d release provenance is unavailable")
    values = json.loads(values_result.stdout)
    history = json.loads(history_result.stdout)
    if (
        not isinstance(history, list)
        or not history
        or values.get("kip", {}).get("owner") != "kube-inference-platform"
        or values.get("kip", {}).get("chartDigest") != lock["inference"]["routerGatewayChartDigest"]
        or values.get("kip", {}).get("valuesSha256") != expected
        or values.get("router", {}).get("epp", {}).get("pluginsConfigFile")
        != "kip-pilot-flow-plugins.yaml"
        or history[-1].get("status") != "deployed"
    ):
        raise FleetError("llm-d release values or ownership drifted from the trial")
    # A later exact-value Helm reconcile may advance the release revision
    # without changing or restarting EPP. The Pod hash and creation time below
    # are the authoritative proof of which policy was loaded during the run.
    reconciled_at = _timestamp(history[-1].get("updated"))
    deployment_result = subprocess.run(
        [
            "kubectl",
            "-n",
            "kip-inference",
            "get",
            "deployment",
            "kip-qwen25-pilot-epp",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    pods_result = subprocess.run(
        [
            "kubectl",
            "-n",
            "kip-inference",
            "get",
            "pods",
            "-l",
            "llm-d-router-gateway=kip-qwen25-pilot-epp",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if deployment_result.returncode or pods_result.returncode:
        raise FleetError("live endpoint picker provenance is unavailable")
    deployment = json.loads(deployment_result.stdout)
    pod_items = json.loads(pods_result.stdout).get("items", [])
    if (
        deployment.get("metadata", {}).get("annotations", {}).get("meta.helm.sh/release-name")
        != "kip-qwen25-pilot"
        or deployment.get("spec", {})
        .get("template", {})
        .get("metadata", {})
        .get("annotations", {})
        .get("kube-inference-platform.io/router-values-sha256")
        != expected
        or len(pod_items) != 1
    ):
        raise FleetError(
            "live endpoint picker Deployment or Pod does not match the reviewed policy"
        )
    pod = pod_items[0]
    if (
        pod.get("metadata", {})
        .get("annotations", {})
        .get("kube-inference-platform.io/router-values-sha256")
        != expected
        or _timestamp(pod.get("metadata", {}).get("creationTimestamp")) > started
        or not any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )
        or not pod.get("status", {}).get("containerStatuses")
        or any(
            item.get("restartCount") != 0
            for item in pod.get("status", {}).get("containerStatuses", [])
        )
    ):
        raise FleetError("endpoint picker was not running the reviewed policy before the trial")
    return {
        "picker": "weighted-random-picker",
        "valuesSha256": expected,
        "helmRevision": history[-1]["revision"],
        "lastHelmReconcileAtReportUtc": _utc(reconciled_at),
        "pickerPodCreatedAtUtc": _utc(_timestamp(pod["metadata"]["creationTimestamp"])),
    }


def collect(root: Path, run_id: str, scenario_name: str = DEFAULT_SCENARIO) -> dict[str, Any]:
    if not RUN_ID.fullmatch(run_id):
        raise FleetError("pilot report requires an allowlisted run ID")
    scenario = _scenario(root, scenario_name)
    contract = load_workload_contract()
    entries: list[dict[str, Any]] = []
    starts: list[datetime] = []
    ends: list[datetime] = []
    for cohort in scenario["cohorts"]:
        tenant, kind = cohort["tenant"], cohort["class"]
        summary_path = (
            root
            / "benchmarks"
            / ".work"
            / run_id
            / f"{tenant}-{kind}"
            / "raw"
            / "profile_export_aiperf.json"
        )
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("aiperf_version") != "0.12.0" or summary.get("was_cancelled"):
            raise FleetError("a mixed-smoke AIPerf cohort is incompatible or cancelled")
        provenance = _dataset_provenance(root, summary, tenant, kind)
        accounting = inspect_run(root, run_id, tenant, kind)
        count = accounting["counts"]["completed"]
        if (
            accounting["counts"]["offered"] != provenance["plannedTurns"]
            or count + accounting["counts"]["rejected"] != provenance["plannedTurns"]
            or _metric(summary, "request_count", "avg") != count
        ):
            raise FleetError("mixed-smoke cohort outcomes differ from its reviewed trace")
        start = _timestamp(summary["start_time"])
        end = _timestamp(summary["end_time"])
        if end <= start:
            raise FleetError("mixed-smoke AIPerf cohort has invalid UTC boundaries")
        starts.append(start)
        ends.append(end)
        entries.append(
            {
                "tenant": tenant,
                "class": kind,
                "dataset": provenance,
                "startedAtUtc": _utc(start),
                "endedAtUtc": _utc(end),
                "counts": accounting["counts"],
                "workerDispatches": accounting["dispatchedByWorker"],
                "offeredToSentP95Ms": accounting["offeredToSentP95Ms"],
                "sentToGatewayP95Ms": accounting["sentToGatewayP95Ms"],
                "gatewayToEppDispatchP95Ms": accounting["gatewayToEppDispatchP95Ms"],
                "gatewayDurationP95Ms": accounting["gatewayDurationP95Ms"],
                "normalizedEarlyGatewayStarts": accounting["normalizedEarlyGatewayStarts"],
                "timestampOrderingToleranceMs": accounting["timestampOrderingToleranceMs"],
                "aiperfZeroDurationRejections": accounting["aiperfZeroDurationRejections"],
                "rejectionTerminalSource": accounting["rejectionTerminalSource"],
                "sessionCompletionP95Ms": accounting["sessionCompletionP95Ms"],
                "offeredToCompletedWithinTenMinutesRate": accounting[
                    "offeredToCompletedWithinTenMinutesRate"
                ],
                "ttftMs": {
                    key: _metric(summary, "time_to_first_token", key)
                    for key in ("p50", "p95", "p99")
                },
                "tpotMs": {
                    key: _metric(summary, "inter_token_latency", key)
                    for key in ("p50", "p95", "p99")
                },
                "e2eMs": {
                    key: _metric(summary, "request_latency", key) for key in ("p50", "p95", "p99")
                },
                "requestThroughputPerSecond": _metric(summary, "request_throughput", "avg"),
                "outputTokensPerSecond": _metric(summary, "output_token_throughput", "avg"),
                "inputTokens": int(_metric(summary, "total_isl", "avg")),
                "outputTokens": int(_metric(summary, "total_osl", "avg")),
                "sampleThresholdMet": count >= SAMPLE_MINIMUM,
            }
        )
    _validate_failure_phase(scenario_name, entries)
    start = min(starts).replace(second=0, microsecond=0)
    end = max(ends).replace(second=0, microsecond=0) + timedelta(minutes=1)
    if end - start > timedelta(hours=1):
        raise FleetError("mixed-smoke report window is unexpectedly long")
    routing = _routing_provenance(root, min(starts))
    cost_snapshot = verify_pricing_files(
        root / "fleet" / ".work" / "cost-ledger.json",
        root / "fleet" / ".work" / "opencost-pricing.csv",
    )
    prometheus = f"http://{_service_ip('kip-monitoring-prometheus')}:9090"
    opencost = f"http://{_service_ip('kip-opencost')}:9003"
    cost = reconcile(cost_snapshot, prometheus=prometheus, opencost=opencost, start=start, end=end)
    if abs(cost["allocatedUsd"] - cost["auditUsd"]) > max(
        cost["auditUsd"] * 0.05, float(cost_snapshot["activeHourlyUsd"]) / 60
    ):
        raise FleetError("mixed-smoke OpenCost allocation exceeds audit tolerance")
    telemetry = _telemetry(prometheus, end, cost["durationMinutes"])
    total_output = sum(entry["outputTokens"] for entry in entries)
    if total_output <= 0:
        raise FleetError("mixed-smoke has no attributable output tokens")
    for entry in entries:
        # Explicitly a heuristic share of the whole-node audit total, not a
        # billable tenant allocation or a measured resource-consumption cost.
        entry["estimatedTenantUsdByOutputShare"] = round(
            cost["allocatedUsd"] * entry["outputTokens"] / total_output, 6
        )
        slo = contract.data["classes"][entry["class"]]["slo"]
        entry["proposedSlo"] = slo
        entry["successRate"] = round(entry["counts"]["completed"] / entry["counts"]["offered"], 6)
        entry["observedTtftTargetMet"] = (
            entry["ttftMs"]["p95"] <= slo["ttftP95Ms"] if "ttftP95Ms" in slo else None
        )
        checks = {"success": entry["successRate"] >= slo["successRateMin"]}
        if "ttftP95Ms" in slo:
            checks["ttft"] = entry["ttftMs"]["p95"] <= slo["ttftP95Ms"]
        if "tpotP95Ms" in slo:
            checks["tpot"] = entry["tpotMs"]["p95"] <= slo["tpotP95Ms"]
        if "taskP95Ms" in slo:
            checks["taskCompletion"] = entry["sessionCompletionP95Ms"] <= slo["taskP95Ms"]
        if "withinRateMin" in slo:
            checks["completionWithinTenMinutes"] = (
                entry["offeredToCompletedWithinTenMinutesRate"] >= slo["withinRateMin"]
            )
        entry["observedSloChecks"] = checks
    dashboard = "http://localhost:3000/d/kip-llmd-fleet-pilot/kip-llm-d-fleet-pilot?" + urlencode(
        {
            "from": _utc(start),
            "to": _utc(end),
            "timezone": "utc",
            "var-cluster": "kip-pilot",
            "var-model": "qwen2.5-7b-instruct",
        }
    )
    remaining = (
        "This small replica-loss checkpoint does not establish sustained failure tolerance or in-flight request continuity; voice, noisy-tenant stress, and clean rebuild remain untested."
        if scenario["kind"] == "replica-failure-checkpoint"
        else "No voice pipeline, noisy-tenant stress, worker-failure, or clean-rebuild evidence yet."
    )
    warnings = [
        "Only one trial per class is present; three independent trials and confidence intervals are required for nominal SLO acceptance.",
        remaining,
        "Per-request EPP queue transitions are unavailable; queued is not inferred from aggregate samples.",
        "Tenant cost is an output-token-share estimate, not bill-reconciling attribution.",
    ]
    if any(not entry["sampleThresholdMet"] for entry in entries):
        warnings.insert(0, "At least one class has fewer than 100 completed samples in this trial.")
    rejected = sum(entry["counts"]["rejected"] for entry in entries)
    if rejected:
        warnings.insert(
            0,
            f"{rejected} requests were rejected with exact AIPerf/Gateway/EPP evidence of llm-d queue-TTL HTTP 429; they are not counted as completed or token-producing requests.",
        )
        warnings.append(
            "Output-token-share tenant cost attribution excludes the queue occupancy of rejected requests."
        )
        warnings.append(
            "AIPerf exported zero-duration error rows for queue-TTL rejections; their terminal times come from matching Envoy access-log start plus duration."
        )
    normalized = sum(entry["normalizedEarlyGatewayStarts"] for entry in entries)
    if normalized:
        warnings.insert(
            0,
            f"{normalized} colocated Gateway start timestamps preceded the client send marker by at most 1 ms and were normalized to send time for lifecycle ordering; raw timing evidence remains private.",
        )
    if any(entry["offeredToSentP95Ms"] > 500 for entry in entries):
        warnings.insert(
            0,
            "Client offered-to-send delay exceeds 500 ms in at least one class; offered load and admitted load must not be conflated.",
        )
    if any(not all(entry["observedSloChecks"].values()) for entry in entries):
        warnings.insert(0, "At least one proposed class SLO check was missed in this trial.")
    report = {
        "schemaVersion": 1,
        "kind": scenario["kind"],
        "status": "partial",
        "runId": run_id,
        "scenario": {key: scenario[key] for key in ("name", "kind", "sha256")},
        "window": {
            "startedAtUtc": _utc(min(starts)),
            "endedAtUtc": _utc(max(ends)),
            "costWindow": cost["window"],
        },
        "workload": {
            "contract": contract.name,
            "sha256": contract.sha256,
            "rootSeed": contract.data["rootSeed"],
        },
        "runtime": {
            "name": "vllm",
            "version": "0.30.0",
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "modelRevision": MODEL_REVISION,
            "replicas": 2,
        },
        "routing": routing,
        "observedServingWorkers": sorted(
            {worker for entry in entries for worker in entry["workerDispatches"]}
        ),
        "cohorts": entries,
        "telemetry": telemetry,
        "cost": {
            "currency": "USD",
            "basis": "verified whole-node Vast /instances rates; OpenCost shareIdle=true, idleByNode=true",
            "rateObservedAtUtc": cost_snapshot["observedAtUtc"],
            "fleetHourlyUsd": float(cost_snapshot["activeHourlyUsd"]),
            "nodeHourlyUsd": {
                node["node"]: float(node["hourlyUsd"]) for node in cost_snapshot["nodes"]
            },
            "auditUsd": cost["auditUsd"],
            "allocatedUsd": cost["allocatedUsd"],
            "reconciliationPercent": round(
                100 * abs(cost["allocatedUsd"] - cost["auditUsd"]) / cost["auditUsd"], 4
            ),
            "tenantAttribution": "illustrative output-token share of allocated infrastructure cost; not invoice or measured per-tenant resource usage",
        },
        "grafanaUrl": dashboard,
        "dataQuality": {
            "status": "partial",
            "warnings": warnings,
        },
    }
    _scan_for_sensitive_data(report)
    return report


def _chart(report: dict[str, Any]) -> str:
    bars = []
    height = 65 + 66 * len(report["cohorts"])
    for index, entry in enumerate(report["cohorts"]):
        width = min(720, entry["ttftMs"]["p95"] / 3000 * 720)
        y = 45 + index * 66
        label = html.escape(f"{entry['tenant']} / {entry['class']}")
        bars.append(f'<text x="0" y="{y}" fill="#e5e7eb">{label}</text>')
        bars.append(f'<rect x="0" y="{y + 10}" width="{width:.1f}" height="22" fill="#38bdf8"/>')
        bars.append(
            f'<text x="{width + 8:.1f}" y="{y + 27}" fill="#e5e7eb">{entry["ttftMs"]["p95"]:.0f} ms</text>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="900" height="{height}" viewBox="0 0 900 {height}" role="img" aria-label="TTFT P95 by cohort">'
        f'<rect width="900" height="{height}" fill="#0f172a"/><text x="0" y="22" fill="#f8fafc" font-size="18">TTFT P95 — pilot trial</text>'
        + "".join(bars)
        + "</svg>\n"
    )


def _html(report: dict[str, Any]) -> str:
    rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(value))}</td>"
            for value in (
                entry["tenant"],
                entry["class"],
                entry["counts"]["offered"],
                entry["counts"]["completed"],
                entry["counts"]["rejected"],
                round(entry["ttftMs"]["p95"], 2),
                round(entry["tpotMs"]["p95"], 2),
                round(entry["e2eMs"]["p95"], 2),
                round(entry["outputTokensPerSecond"], 2),
                ", ".join(
                    f"{name}:{'pass' if passed else 'miss'}"
                    for name, passed in entry.get("observedSloChecks", {}).items()
                ),
                entry["estimatedTenantUsdByOutputShare"],
            )
        )
        + "</tr>"
        for entry in report["cohorts"]
    )
    warnings = "".join(
        f"<li>{html.escape(value)}</li>" for value in report["dataQuality"]["warnings"]
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'self';">
<title>KIP mixed text pilot · {html.escape(report["runId"])}</title>
<style>body{{font:16px system-ui;background:#0b1220;color:#e5e7eb;max-width:1050px;margin:40px auto;padding:0 24px}}h1,h2{{color:#f8fafc}}.badge{{background:#9a3412;padding:4px 9px;border-radius:5px}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #334155;padding:10px;text-align:left}}code{{color:#7dd3fc}}a{{color:#7dd3fc}}.card{{background:#142033;padding:18px;border-radius:9px;margin:20px 0}}.small{{color:#cbd5e1}}</style></head>
<body><h1>KIP text pilot <span class="badge">PARTIAL</span></h1>
<p>Run <code>{html.escape(report["runId"])}</code> · {html.escape(report["window"]["startedAtUtc"])} – {html.escape(report["window"]["endedAtUtc"])}</p>
<p>This is an interim single-trial validation of reviewed synthetic tenant cohorts through the llm-d-routed pilot pool. It is <strong>not</strong> the accepted nominal SLO campaign.</p>
<div class="card"><h2>Performance and estimated cost</h2><table><thead><tr><th>Tenant</th><th>Class</th><th>Offered</th><th>Completed</th><th>Rejected</th><th>TTFT P95 ms</th><th>TPOT P95 ms</th><th>E2E P95 ms</th><th>Output tok/s</th><th>Proposed SLO checks</th><th>Tenant estimate USD*</th></tr></thead><tbody>{rows}</tbody></table>
<p class="small">*Illustrative output-token-share of allocated infrastructure cost; not an invoice or measured per-tenant usage.</p></div>
<img src="charts/ttft-p95.svg" alt="TTFT P95 by cohort" width="900">
<div class="card"><h2>Cost audit</h2><p>Verified fleet rental: ${report["cost"]["fleetHourlyUsd"]:.4f}/hour. Exact {report["window"]["costWindow"]} audit ${report["cost"]["auditUsd"]:.6f}; OpenCost idle-inclusive allocation ${report["cost"]["allocatedUsd"]:.6f}; difference {report["cost"]["reconciliationPercent"]:.3f}%.</p></div>
<div class="card"><h2>Data quality and remaining acceptance</h2><ul>{warnings}</ul></div>
<p><a href="{html.escape(report["grafanaUrl"], quote=True)}">Open exact-window Grafana dashboard</a> (requires local port-forward). <a href="summary.json">JSON</a> · <a href="summary.csv">CSV</a> · <a href="reproducibility.json">Reproducibility</a>.</p>
</body></html>\n"""


def _reproducibility(report: dict[str, Any], root: Path) -> dict[str, Any]:
    lock_path = root / "deploy" / "fleet" / "versions.json"
    model_path = root / "deploy" / "fleet" / "model.yaml"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    cohorts = [
        {
            "tenant": entry["tenant"],
            "class": entry["class"],
            "trial": entry["dataset"]["trial"],
            "seed": entry["dataset"]["seed"],
            "datasetSha256": entry["dataset"]["datasetSha256"],
            "sessions": entry["dataset"]["sessions"],
            "offeredSessionsPerSecond": entry["dataset"]["offeredSessionsPerSecond"],
            "arrivalSchedule": entry["dataset"].get("arrivalSchedule", "legacy-unversioned"),
            "commands": [
                f"make workload-generate TENANT={entry['tenant']} WORKLOAD_CLASS={entry['class']} TRIAL={entry['dataset']['trial']} SESSIONS={entry['dataset']['sessions']} RATE={entry['dataset']['offeredSessionsPerSecond']}",
                f"make campaign-text-replay TENANT={entry['tenant']} WORKLOAD_CLASS={entry['class']} TRIAL={entry['dataset']['trial']} RUN_ID={report['runId']}",
                f"make campaign-accounting RUN_ID={report['runId']} TENANT={entry['tenant']} WORKLOAD_CLASS={entry['class']}",
            ],
        }
        for entry in report["cohorts"]
    ]
    value = {
        "schemaVersion": 1,
        "runId": report["runId"],
        "scenario": report.get("scenario"),
        "workloadContractSha256": report["workload"]["sha256"],
        "versionsLockSha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "modelManifestSha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "routerValuesSha256": report.get("routing", {}).get("valuesSha256"),
        "routerHelmRevisionAtReport": report.get("routing", {}).get("helmRevision"),
        "aiperfVersion": "0.12.0",
        "components": {
            "kubernetes": lock["base"]["kubernetes"],
            "containerd": lock["base"]["containerd"],
            "cilium": lock["base"]["cilium"],
            "gpuOperator": lock["base"]["gpuOperator"],
            "nvidiaDriver": lock["gpu"]["driverVersion"],
            "vllmImage": lock["inference"]["vllmLinuxAmd64Image"],
            "llmd": lock["inference"]["llmdVersion"],
            "eppImage": lock["inference"]["eppLinuxAmd64Image"],
            "envoyGateway": lock["gateway"]["envoyGatewayVersion"],
            "envoyAiGateway": lock["gateway"]["envoyAiGatewayVersion"],
        },
        "cohorts": cohorts,
        "costWindow": report["window"]["costWindow"],
        "costRateSource": report["cost"]["basis"],
        "reportCommand": f"make campaign-report RUN_ID={report['runId']} SCENARIO={report.get('scenario', {}).get('name', DEFAULT_SCENARIO)}",
        "gitRevision": "not-recorded-on-git-free-control-checkout",
    }
    _scan_for_sensitive_data(value)
    return value


def publish(report: dict[str, Any], destination: Path) -> None:
    """Write only the sanitized Git-eligible bundle; refuse to overwrite drift."""
    output = io.StringIO(newline="")
    fields = (
        "tenant",
        "class",
        "offered",
        "completed",
        "rejected",
        "ttftP50Ms",
        "ttftP95Ms",
        "ttftP99Ms",
        "tpotP95Ms",
        "e2eP95Ms",
        "outputTokensPerSecond",
        "sentToGatewayP95Ms",
        "gatewayToEppDispatchP95Ms",
        "gatewayDurationP95Ms",
        "successRate",
        "sessionCompletionP95Ms",
        "offeredToCompletedWithinTenMinutesRate",
        "observedProposedSloMet",
        "inputTokens",
        "outputTokens",
        "estimatedTenantUsdByOutputShare",
    )
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for entry in report["cohorts"]:
        writer.writerow(
            {
                "tenant": entry["tenant"],
                "class": entry["class"],
                "offered": entry["counts"].get("offered"),
                "completed": entry["counts"]["completed"],
                "rejected": entry["counts"].get("rejected"),
                "ttftP50Ms": entry["ttftMs"]["p50"],
                "ttftP95Ms": entry["ttftMs"]["p95"],
                "ttftP99Ms": entry["ttftMs"]["p99"],
                "tpotP95Ms": entry["tpotMs"]["p95"],
                "e2eP95Ms": entry["e2eMs"]["p95"],
                "outputTokensPerSecond": entry["outputTokensPerSecond"],
                "sentToGatewayP95Ms": entry.get("sentToGatewayP95Ms"),
                "gatewayToEppDispatchP95Ms": entry.get("gatewayToEppDispatchP95Ms"),
                "gatewayDurationP95Ms": entry.get("gatewayDurationP95Ms"),
                "successRate": entry.get("successRate"),
                "sessionCompletionP95Ms": entry.get("sessionCompletionP95Ms"),
                "offeredToCompletedWithinTenMinutesRate": entry.get(
                    "offeredToCompletedWithinTenMinutesRate"
                ),
                "observedProposedSloMet": (
                    all(entry["observedSloChecks"].values())
                    if entry.get("observedSloChecks")
                    else None
                ),
                "inputTokens": entry["inputTokens"],
                "outputTokens": entry["outputTokens"],
                "estimatedTenantUsdByOutputShare": entry["estimatedTenantUsdByOutputShare"],
            }
        )
    files = {
        "summary.json": json.dumps(report, indent=2, sort_keys=True) + "\n",
        "summary.csv": output.getvalue(),
        "index.html": _html(report),
        "charts/ttft-p95.svg": _chart(report),
        "reproducibility.json": json.dumps(
            _reproducibility(report, destination.parents[2]), indent=2, sort_keys=True
        )
        + "\n",
    }
    if destination.exists():
        if not destination.is_dir() or any(
            not (destination / name).is_file()
            or (destination / name).read_text(encoding="utf-8") != content
            for name, content in files.items()
        ):
            raise FleetError("existing sanitized pilot report differs; refusing overwrite")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kip-report-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        for name, content in files.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        staging.rename(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a sanitized partial fleet text report")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default=DEFAULT_SCENARIO)
    args = parser.parse_args()
    try:
        root = Path(__file__).resolve().parent.parent
        report = collect(root, args.run_id, args.scenario)
        destination = root / "benchmarks" / "reports" / args.run_id
        publish(report, destination)
    except (FleetError, OSError, ValueError, KeyError, IndexError, json.JSONDecodeError) as error:
        print(f"pilot report failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": report["status"],
                "runId": args.run_id,
                "report": str(destination / "index.html"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
