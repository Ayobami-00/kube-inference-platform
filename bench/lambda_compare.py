"""Compare three seeded, sanitized Lambda nominal reports without raw traces."""

# ruff: noqa: E501 -- compact chart HTML and diagnostic text are intentional.

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import math
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from .workloads import load_workload_contract

ROOT = Path(__file__).resolve().parents[1]
REPORT_ID = re.compile(r"lambda-nominal-(?:baseline|large-flow6|large-flow8)-[0-9]{8}-[0-9]{6}\Z")
METRICS = ("ttftP95Ms", "tpotP95Ms", "e2eP95Ms", "outputTokensPerSecond", "successRate")
TELEMETRY_METRICS = (
    "smallInteractiveQueueP95Ms",
    "largeInteractiveQueueP95Ms",
    "largeBatchQueueP95Ms",
    "largeVllmQueueP95Ms",
    "largeVllmPrefillP95Ms",
    "largeVllmTtftP95Ms",
    "largeNodeGpuUtilMeanPercent",
)
ECONOMIC_METRICS = ("infrastructureUsdPerSuccess", "outputTokensPerUsd", "outputUsdPerMillion")
CLASSES = ("chat", "agentic-style", "batch")
TENANTS = ("tenant-a", "tenant-b", "tenant-c")
T_95_DF2 = 4.3026527299


def mean_ci(values: list[float]) -> dict:
    if len(values) != 3 or any(not math.isfinite(value) for value in values):
        raise ValueError("comparison requires three finite seeded trials")
    mean = sum(values) / 3
    sample_sd = math.sqrt(sum((value - mean) ** 2 for value in values) / 2)
    half = T_95_DF2 * sample_sd / math.sqrt(3)
    return {
        "mean": round(mean, 6),
        "ci95Low": round(mean - half, 6),
        "ci95High": round(mean + half, 6),
        "coefficientOfVariation": round(sample_sd / abs(mean), 6) if mean else None,
    }


def slo_matrix(baseline: list[dict], candidate: list[dict] | None) -> dict:
    contract = load_workload_contract().data["classes"]
    matrix = {}
    for workload_class in CLASSES:
        thresholds = contract[workload_class]["slo"]
        metrics = {}
        for threshold_name, limit in thresholds.items():
            metric_name = "successRate" if threshold_name == "successRateMin" else threshold_name
            variants = {"baseline": baseline}
            if candidate:
                variants["candidate"] = candidate
            results = {}
            for variant, reports in variants.items():
                if threshold_name in {"withinMs", "withinRateMin"}:
                    results[variant] = "not measured: arrival-to-completion samples unavailable"
                elif threshold_name == "taskP95Ms" and any(
                    report["classes"][workload_class].get("taskCount", 0) < 100
                    for report in reports
                ):
                    results[variant] = "inconclusive: fewer than 100 tasks per trial"
                else:
                    values = [report["classes"][workload_class][metric_name] for report in reports]
                    passed = (
                        all(value >= limit for value in values)
                        if threshold_name.endswith("Min")
                        else all(value <= limit for value in values)
                    )
                    results[variant] = "pass" if passed else "fail"
            metrics[threshold_name] = {"threshold": limit, **results}
        matrix[workload_class] = metrics
    return matrix


def economics(report: dict) -> dict[str, float]:
    audit = float(report["cost"]["infrastructureAuditUsd"])
    successes = report["completed"]
    output_tokens = sum(report["classes"][name]["outputTokens"] for name in CLASSES)
    if not audit > 0 or not successes > 0 or not output_tokens > 0:
        raise ValueError("nominal report lacks positive bill-audit efficiency denominators")
    return {
        "infrastructureUsdPerSuccess": audit / successes,
        "outputTokensPerUsd": output_tokens / audit,
        "outputUsdPerMillion": audit * 1_000_000 / output_tokens,
    }


def tenant_measures(
    reports: list[dict], tenant: str, workload_class: str
) -> tuple[list[float], list[float]]:
    rows = [report["tenantRows"][(tenant, workload_class)] for report in reports]
    return (
        [float(row["ttftP95Ms"]) for row in rows],
        [row["completed"] / row["scheduled"] for row in rows],
    )


def load_trials(run_ids: list[str], root: Path = ROOT) -> list[dict]:
    if len(run_ids) != 3 or len(set(run_ids)) != 3:
        raise ValueError("comparison needs three distinct report IDs")
    reports = []
    for run_id in run_ids:
        if not REPORT_ID.fullmatch(run_id):
            raise ValueError("comparison report ID is not reviewed")
        path = root / "benchmarks/reports" / run_id / "summary.json"
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            raise ValueError("sanitized nominal report is missing or unsafe")
        report = json.loads(path.read_text(encoding="utf-8"))
        if (
            report.get("runId") != run_id
            or report.get("schemaVersion") != 1
            or report.get("status")
            not in {"measured-baseline-one-of-three-trials", "measured-variant-one-of-three-trials"}
            or report.get("scheduled") != report.get("sent")
            or report.get("sent")
            != sum(report.get(key, -1) for key in ("completed", "rejected", "failed"))
            or set(report.get("classes", {})) != set(CLASSES)
        ):
            raise ValueError("nominal report outcome transitions are incomplete")
        cost = report.get("cost") or {}
        if not cost.get("tenantCostIsEstimateNotInvoice") or abs(
            cost.get("infrastructureAuditUsd", math.inf)
            - cost.get("openCostNodeAssetsUsd", -math.inf)
        ) > cost.get("reconciliationToleranceUsd", -math.inf):
            raise ValueError("nominal report cost does not reconcile")
        telemetry_path = path.parent / "telemetry.json"
        if telemetry_path.is_symlink() or not telemetry_path.is_file():
            raise ValueError("nominal report lacks its exact-window telemetry snapshot")
        telemetry = json.loads(telemetry_path.read_text(encoding="utf-8"))
        start = datetime.fromisoformat(report["startUtc"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(report["endUtc"].replace("Z", "+00:00"))
        telemetry_start = datetime.fromisoformat(telemetry["windowStartUtc"].replace("Z", "+00:00"))
        telemetry_end = datetime.fromisoformat(telemetry["windowEndUtc"].replace("Z", "+00:00"))
        if (
            telemetry.get("schemaVersion") != 1
            or telemetry.get("runId") != run_id
            or telemetry.get("variant") != report.get("variant")
            or telemetry.get("trial") != report.get("trial")
            or telemetry.get("windowPrecisionSeconds") != 1
            or not (0 <= (start - telemetry_start).total_seconds() < 1)
            or not (0 <= (telemetry_end - end).total_seconds() < 1)
            or set(telemetry.get("metrics") or {}) != set(TELEMETRY_METRICS)
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                for value in telemetry["metrics"].values()
            )
        ):
            raise ValueError("nominal report telemetry provenance or metrics are incomplete")
        report["telemetry"] = telemetry["metrics"]
        tenant_rows = {}
        for row in report.get("rows", []):
            key = (row.get("tenant"), row.get("class"))
            if (
                key in tenant_rows
                or key[0] not in TENANTS
                or key[1] not in CLASSES
                or row.get("scheduled", 0) <= 0
                or row.get("completed", -1) + row.get("rejected", -1) + row.get("failed", -1)
                != row["scheduled"]
                or not isinstance(row.get("ttftP95Ms"), (int, float))
            ):
                raise ValueError("nominal tenant/class outcome or latency is invalid")
            tenant_rows[key] = row
        if set(tenant_rows) != {(tenant, name) for tenant in TENANTS for name in CLASSES}:
            raise ValueError("nominal report lacks the nine synthetic tenant/class cohorts")
        report["tenantRows"] = tenant_rows
        reports.append(report)
    reports.sort(key=lambda item: item["trial"])
    if [item["trial"] for item in reports] != [1, 2, 3]:
        raise ValueError("comparison requires exactly seeded trials 1, 2, and 3")
    anchor = reports[0]
    known_epp_hashes = {item.get("eppValuesSha256") for item in reports} - {None}
    if len(known_epp_hashes) > 1:
        raise ValueError("nominal endpoint-picker configuration drifted between trials")
    for item in reports:
        if (
            item["variant"] != anchor["variant"]
            or item["campaignSha256"] != anchor["campaignSha256"]
            or item["workloadSha256"] != anchor["workloadSha256"]
            or item["offeredPlan"] != anchor["offeredPlan"]
            or item["servedModelRevisions"] != anchor["servedModelRevisions"]
            or item["cost"]["nodeHourlyUsd"] != anchor["cost"]["nodeHourlyUsd"]
            or item["cost"]["fleetHourlyUsd"] != anchor["cost"]["fleetHourlyUsd"]
        ):
            raise ValueError("nominal report hardware, model, trace, or pricing drifted")
        for workload_class in CLASSES:
            if item["classes"][workload_class]["completed"] < 100:
                raise ValueError("nominal report lacks the per-class P95 sample floor")
            for metric in METRICS:
                value = item["classes"][workload_class].get(metric)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                ):
                    raise ValueError("nominal class metric is missing or invalid")
    return reports


def summarize(baseline: list[dict], candidate: list[dict] | None = None) -> dict:
    if baseline[0]["variant"] != "baseline":
        raise ValueError("comparison baseline must be the reviewed baseline variant")
    if candidate:
        if candidate[0]["variant"] not in {"large-flow6", "large-flow8"}:
            raise ValueError("candidate must be a reviewed large-pool flow variant")
        if any(not item.get("eppValuesSha256") for item in candidate):
            raise ValueError("candidate trials lack exact endpoint-picker values provenance")
        for old, new in zip(baseline, candidate, strict=True):
            if (
                old["trial"] != new["trial"]
                or old["campaignSha256"] != new["campaignSha256"]
                or old["workloadSha256"] != new["workloadSha256"]
                or old["offeredPlan"] != new["offeredPlan"]
                or old["servedModelRevisions"] != new["servedModelRevisions"]
                or old["cost"]["nodeHourlyUsd"] != new["cost"]["nodeHourlyUsd"]
                or old.get("vllmImage", new.get("vllmImage")) != new.get("vllmImage")
            ):
                raise ValueError("paired comparison changed trial, model, hardware, or trace")
    results = {}
    for workload_class in CLASSES:
        row = {}
        for metric in METRICS:
            values = [float(report["classes"][workload_class][metric]) for report in baseline]
            entry = {"baseline": mean_ci(values)}
            if candidate:
                changed = [float(report["classes"][workload_class][metric]) for report in candidate]
                entry["candidate"] = mean_ci(changed)
                entry["pairedDelta"] = mean_ci(
                    [right - left for left, right in zip(values, changed, strict=True)]
                )
                entry["relativeMeanChangePercent"] = (
                    round((entry["candidate"]["mean"] / entry["baseline"]["mean"] - 1) * 100, 3)
                    if entry["baseline"]["mean"]
                    else None
                )
            row[metric] = entry
        results[workload_class] = row
    telemetry_results = {}
    for metric in TELEMETRY_METRICS:
        old_values = [float(report["telemetry"][metric]) for report in baseline]
        entry = {"baseline": mean_ci(old_values)}
        if candidate:
            new_values = [float(report["telemetry"][metric]) for report in candidate]
            entry["candidate"] = mean_ci(new_values)
            entry["pairedDelta"] = mean_ci(
                [right - left for left, right in zip(old_values, new_values, strict=True)]
            )
        telemetry_results[metric] = entry
    economic_results = {}
    for metric in ECONOMIC_METRICS:
        old_values = [economics(report)[metric] for report in baseline]
        entry = {"baseline": mean_ci(old_values)}
        if candidate:
            new_values = [economics(report)[metric] for report in candidate]
            entry["candidate"] = mean_ci(new_values)
            entry["pairedDelta"] = mean_ci(
                [right - left for left, right in zip(old_values, new_values, strict=True)]
            )
        economic_results[metric] = entry
    tenant_results = {}
    for tenant in TENANTS:
        tenant_results[tenant] = {}
        for workload_class in CLASSES:
            baseline_ttft, baseline_success = tenant_measures(baseline, tenant, workload_class)
            cell = {
                "ttftP95Ms": {"baseline": mean_ci(baseline_ttft)},
                "successRate": {"baseline": mean_ci(baseline_success)},
            }
            if candidate:
                candidate_ttft, candidate_success = tenant_measures(
                    candidate, tenant, workload_class
                )
                for name, old, new in (
                    ("ttftP95Ms", baseline_ttft, candidate_ttft),
                    ("successRate", baseline_success, candidate_success),
                ):
                    cell[name]["candidate"] = mean_ci(new)
                    cell[name]["pairedDelta"] = mean_ci(
                        [right - left for left, right in zip(old, new, strict=True)]
                    )
            tenant_results[tenant][workload_class] = cell
    return {
        "schemaVersion": 1,
        "status": "paired-comparison" if candidate else "three-trial-baseline",
        "campaignSha256": baseline[0]["campaignSha256"],
        "workloadSha256": baseline[0]["workloadSha256"],
        "baselineRunIds": [item["runId"] for item in baseline],
        "candidateRunIds": [item["runId"] for item in candidate] if candidate else [],
        "hardwareNodeHourlyUsd": baseline[0]["cost"]["nodeHourlyUsd"],
        "fleetHourlyUsd": baseline[0]["cost"]["fleetHourlyUsd"],
        "classes": results,
        "telemetry": telemetry_results,
        "economics": economic_results,
        "tenantClasses": tenant_results,
        "slo": slo_matrix(baseline, candidate),
        "baselineInfrastructureUsd": round(
            sum(item["cost"]["infrastructureAuditUsd"] for item in baseline), 6
        ),
        "candidateInfrastructureUsd": (
            round(sum(item["cost"]["infrastructureAuditUsd"] for item in candidate), 6)
            if candidate
            else None
        ),
        "quality": [
            "Confidence intervals are Student-t intervals of three trial-level means, not population P95 intervals.",
            "The offered rate is provisional until mixed-load calibration establishes a sustainable 70% reference.",
            "Batch within-ten-minute completion and voice SLOs remain unverified by these text reports.",
            "Tenant costs are estimated attribution; infrastructure totals are rental-rate audits.",
            "Per-tenant prompt lengths differ by seed; tenant latency differences are not pure scheduler fairness.",
            "llm-d queue P95 covers dispatched requests in a priority band before vLLM; vLLM queue P95 covers model requests after dispatch. Histogram percentiles are approximate and cannot be subtracted or added.",
        ]
        + (
            [
                "Earlier baseline trial reports predate recording the endpoint-picker values hash; "
                "the reviewed baseline release was checked before trial 3."
            ]
            if any(not item.get("eppValuesSha256") for item in baseline)
            else []
        ),
    }


def render(summary: dict) -> dict[str, str]:
    rows = []
    for workload_class, metrics in summary["classes"].items():
        for name, value in metrics.items():
            rows.append(
                {
                    "class": workload_class,
                    "metric": name,
                    "baselineMean": value["baseline"]["mean"],
                    "baselineCiLow": value["baseline"]["ci95Low"],
                    "baselineCiHigh": value["baseline"]["ci95High"],
                    "candidateMean": value.get("candidate", {}).get("mean", ""),
                    "pairedDeltaMean": value.get("pairedDelta", {}).get("mean", ""),
                    "relativeMeanChangePercent": value.get("relativeMeanChangePercent", ""),
                }
            )
    for name, value in summary["telemetry"].items():
        rows.append(
            {
                "class": "telemetry",
                "metric": name,
                "baselineMean": value["baseline"]["mean"],
                "baselineCiLow": value["baseline"]["ci95Low"],
                "baselineCiHigh": value["baseline"]["ci95High"],
                "candidateMean": value.get("candidate", {}).get("mean", ""),
                "pairedDeltaMean": value.get("pairedDelta", {}).get("mean", ""),
                "relativeMeanChangePercent": "",
            }
        )
    for name, value in summary["economics"].items():
        rows.append(
            {
                "class": "infrastructure-efficiency",
                "metric": name,
                "baselineMean": value["baseline"]["mean"],
                "baselineCiLow": value["baseline"]["ci95Low"],
                "baselineCiHigh": value["baseline"]["ci95High"],
                "candidateMean": value.get("candidate", {}).get("mean", ""),
                "pairedDeltaMean": value.get("pairedDelta", {}).get("mean", ""),
                "relativeMeanChangePercent": "",
            }
        )
    for tenant, classes in summary["tenantClasses"].items():
        for workload_class, metrics in classes.items():
            for name, value in metrics.items():
                rows.append(
                    {
                        "class": f"{tenant}/{workload_class}",
                        "metric": name,
                        "baselineMean": value["baseline"]["mean"],
                        "baselineCiLow": value["baseline"]["ci95Low"],
                        "baselineCiHigh": value["baseline"]["ci95High"],
                        "candidateMean": value.get("candidate", {}).get("mean", ""),
                        "pairedDeltaMean": value.get("pairedDelta", {}).get("mean", ""),
                        "relativeMeanChangePercent": "",
                    }
                )
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    bars = []
    for workload_class in CLASSES:
        metrics = summary["classes"][workload_class]
        baseline = metrics["ttftP95Ms"]["baseline"]["mean"]
        candidate = metrics["ttftP95Ms"].get("candidate", {}).get("mean")
        ceiling = max(baseline, candidate or 0, 1)
        bars.append(
            f"<section><h3>{html.escape(workload_class)} · TTFT P95</h3>"
            f"<p>Baseline {baseline:.1f} ms</p><div class='track'><div class='base' style='width:{baseline / ceiling * 100:.1f}%'></div></div>"
            + (
                f"<p>Candidate {candidate:.1f} ms</p><div class='track'><div class='candidate' style='width:{candidate / ceiling * 100:.1f}%'></div></div>"
                if candidate is not None
                else ""
            )
            + "</section>"
        )
    telemetry_bars = []
    for name, metric in summary["telemetry"].items():
        baseline = metric["baseline"]["mean"]
        candidate = metric.get("candidate", {}).get("mean")
        ceiling = max(baseline, candidate or 0, 1)
        unit = "%" if name.endswith("Percent") else " ms"
        telemetry_bars.append(
            f"<section><h3>{html.escape(name)}</h3>"
            f"<p>Baseline {baseline:.1f}{unit}</p><div class='track'><div class='base' style='width:{baseline / ceiling * 100:.1f}%'></div></div>"
            + (
                f"<p>Candidate {candidate:.1f}{unit}</p><div class='track'><div class='candidate' style='width:{candidate / ceiling * 100:.1f}%'></div></div>"
                if candidate is not None
                else ""
            )
            + "</section>"
        )
    cost_bars = []
    for name in ("infrastructureUsdPerSuccess", "outputUsdPerMillion"):
        metric = summary["economics"][name]
        baseline = metric["baseline"]["mean"]
        candidate = metric.get("candidate", {}).get("mean")
        ceiling = max(baseline, candidate or 0, 0.000001)
        cost_bars.append(
            f"<section><h3>{html.escape(name)}</h3>"
            f"<p>Baseline ${baseline:.4f}</p><div class='track'><div class='base' style='width:{baseline / ceiling * 100:.1f}%'></div></div>"
            + (
                f"<p>Candidate ${candidate:.4f}</p><div class='track'><div class='candidate' style='width:{candidate / ceiling * 100:.1f}%'></div></div>"
                if candidate is not None
                else ""
            )
            + "</section>"
        )
    headings = "".join(f"<th>{html.escape(key)}</th>" for key in rows[0])
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in row.values()) + "</tr>"
        for row in rows
    )
    warnings = "".join(f"<li>{html.escape(item)}</li>" for item in summary["quality"])
    slo_rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(value))}</td>"
            for value in (
                workload_class,
                metric,
                result["threshold"],
                result["baseline"],
                result.get("candidate", "not run"),
            )
        )
        + "</tr>"
        for workload_class, metrics in summary["slo"].items()
        for metric, result in metrics.items()
    )
    tenant_rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(value))}</td>"
            for value in (
                tenant,
                workload_class,
                round(values["ttftP95Ms"]["baseline"]["mean"], 1),
                round(values["ttftP95Ms"].get("candidate", {}).get("mean", 0), 1)
                if "candidate" in values["ttftP95Ms"]
                else "not run",
                round(values["successRate"]["baseline"]["mean"] * 100, 2),
                round(values["successRate"].get("candidate", {}).get("mean", 0) * 100, 2)
                if "candidate" in values["successRate"]
                else "not run",
            )
        )
        + "</tr>"
        for tenant, classes in summary["tenantClasses"].items()
        for workload_class, values in classes.items()
    )
    trial_links = "".join(
        f'<li><a href="../{html.escape(run_id, quote=True)}/index.html">'
        f"{html.escape(run_id)}</a></li>"
        for run_id in summary["baselineRunIds"] + summary["candidateRunIds"]
    )
    page = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>KIP Lambda nominal comparison</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:1150px;margin:40px auto;padding:0 20px;background:#0b1220;color:#e7eef8}}.card{{background:#172337;border:1px solid #344359;border-radius:12px;padding:20px;margin:22px 0}}.track{{background:#344359;border-radius:5px;height:20px}}.base,.candidate{{height:20px;border-radius:5px}}.base{{background:#54c7b5}}.candidate{{background:#f6b85a}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}th,td{{padding:7px;border-bottom:1px solid #344359;text-align:right}}th:first-child,td:first-child{{text-align:left}}a{{color:#86c5ff}}</style>
<h1>Nominal multi-tenant comparison</h1><p>{html.escape(summary["status"])} · three matched seeds · ${summary["fleetHourlyUsd"]:.2f}/hour</p>
<div class="card"><h2>TTFT across workload classes</h2>{"".join(bars)}</div>
<div class="card"><h2>llm-d queue, vLLM, and GPU evidence</h2><p>Bars show trial means from exact-window Prometheus snapshots. An llm-d queue P95 is the estimated wait threshold met by about 95% of dispatched requests in that priority band—not the wait of every request. vLLM queue P95 measures a later, model-wide stage, so the two percentiles cannot be added or subtracted. Large-node GPU utilization includes co-located models.</p>{"".join(telemetry_bars)}</div>
<div class="card"><h2>Infrastructure cost efficiency</h2><p>Whole-fleet rental audit divided by completed requests or generated tokens in the measured windows; tenant attribution is separate and estimated.</p>{"".join(cost_bars)}</div>
<div class="card"><h2>Performance, SLO, and cost statistics</h2><table><thead><tr>{headings}</tr></thead><tbody>{body}</tbody></table></div>
<div class="card"><h2>Proposed pilot SLOs</h2><p>Pass requires all three seeded trials to meet the threshold; missing task or arrival samples stay inconclusive.</p><table><thead><tr><th>Class</th><th>Metric</th><th>Threshold</th><th>Baseline</th><th>Candidate</th></tr></thead><tbody>{slo_rows}</tbody></table></div>
<div class="card"><h2>Synthetic tenant views</h2><p>Each tenant independently exercises every class. P95 values are means of trial-level P95s.</p><table><thead><tr><th>Tenant</th><th>Class</th><th>Baseline TTFT P95 ms</th><th>Candidate TTFT P95 ms</th><th>Baseline success %</th><th>Candidate success %</th></tr></thead><tbody>{tenant_rows}</tbody></table></div>
<div class="card"><h2>Interpretation</h2><ul>{warnings}</ul><p>Baseline infrastructure audit: ${summary["baselineInfrastructureUsd"]:.4f}. Candidate audit: {summary["candidateInfrastructureUsd"] if summary["candidateInfrastructureUsd"] is not None else "not run"}.</p></div>
<div class="card"><h2>Seeded trial reports</h2><p>Each trial links to its exact private Grafana time window and includes sanitized telemetry and cost data.</p><ul>{trial_links}</ul></div>
<p><a href="summary.json">Summary JSON</a> · <a href="summary.csv">Summary CSV</a></p></html>"""
    return {
        "summary.json": json.dumps(summary, sort_keys=True, indent=2) + "\n",
        "summary.csv": buffer.getvalue(),
        "index.html": page,
    }


def publish(
    baseline_ids: list[str], candidate_ids: list[str] | None = None, root: Path = ROOT
) -> Path:
    baseline = load_trials(baseline_ids, root)
    candidate = load_trials(candidate_ids, root) if candidate_ids else None
    contents = render(summarize(baseline, candidate))
    kind = "paired" if candidate else "baseline"
    run_id = datetime.now(UTC).strftime(f"lambda-nominal-{kind}-comparison-%Y%m%d-%H%M%S")
    destination = root / "benchmarks/reports" / run_id
    destination.mkdir(parents=True, exist_ok=False)
    for name, value in contents.items():
        (destination / name).write_text(value, encoding="utf-8")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare three seeded Lambda nominal reports")
    parser.add_argument("--baseline", nargs=3, required=True, metavar="RUN_ID")
    parser.add_argument("--candidate", nargs=3, metavar="RUN_ID")
    args = parser.parse_args()
    try:
        print(publish(args.baseline, args.candidate))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Lambda comparison failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
