"""Deterministic, sanitized static benchmark report generation."""

# ruff: noqa: E501 -- embedded HTML/SVG stays readable as a literal artifact template.

from __future__ import annotations

import csv
import html
import ipaddress
import json
import math
import re
import shutil
import statistics
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from .config import CostProfile, canonical_json, load_suite, validate_identifier
from .errors import BenchError, ValidationError

_TOP_LEVEL_KEYS = {
    "schemaVersion",
    "run",
    "suite",
    "costProfile",
    "trials",
    "opencost",
    "prometheus",
    "dataQuality",
}
_RUN_KEYS = {
    "id",
    "startedAt",
    "endedAt",
    "gitRevision",
    "gitTreeState",
    "gitWorkspaceSha256",
    "aiperfVersion",
    "aiperfImage",
    "componentVersions",
    "runtime",
    "runtimePodUid",
    "runtimePodRestartCount",
    "runtimeVersion",
    "runtimeImage",
    "runtimeProfileSha256",
    "modelProfile",
    "modelName",
    "modelRevision",
    "gpuModel",
    "gpuCount",
    "grafanaBaseUrl",
}
_COMPONENT_VERSION_KEYS = {
    "aiperf",
    "kubernetes",
    "containerd",
    "helm",
    "cilium",
    "gpuOperator",
    "nvidiaDriver",
    "dcgmExporter",
    "vllm",
    "kubePrometheusStack",
    "prometheus",
    "grafanaChart",
    "opencost",
}
_REPORT_SUITE_KEYS = {
    "name",
    "sha256",
    "manifest",
    "concurrency",
    "requestsPerCell",
    "trialsPerCell",
    "confidenceLevel",
}
_TRIAL_KEYS = {
    "concurrency",
    "trial",
    "startedAt",
    "endedAt",
    "requests",
    "successfulRequests",
    "failedRequests",
    "inputTokens",
    "outputTokens",
    "metrics",
    "telemetry",
    "cost",
}
_METRIC_KEYS = {
    "ttftMs",
    "itlMs",
    "e2eMs",
    "requestThroughputPerSecond",
    "outputTokenThroughputPerSecond",
    "streamingTimingEvidence",
}
_PERCENTILE_KEYS = {"p50", "p95", "p99"}
_TELEMETRY_KEYS = {
    "gpuUtilizationAveragePct",
    "gpuMemoryUsedAverageBytes",
    "powerAverageWatts",
    "temperatureMaxC",
    "kvCacheAveragePct",
    "requestRunningMax",
    "requestQueueMax",
    "promptTokenRateAveragePerSecond",
    "generatedTokenRateAveragePerSecond",
    "cachedTokenRateAveragePerSecond",
    "serverTtftP50Ms",
    "serverTtftP95Ms",
    "serverTtftP99Ms",
    "serverTpotP50Ms",
    "serverTpotP95Ms",
    "serverTpotP99Ms",
    "serverE2eP50Ms",
    "serverE2eP95Ms",
    "serverE2eP99Ms",
    "gpuPcieTxAverageBytesPerSecond",
    "gpuPcieRxAverageBytesPerSecond",
    "gpuSmClockAverageMhz",
    "gpuMemoryClockAverageMhz",
    "gpuClockThrottleMaxMask",
    "gpuXidMaxCode",
}
_OPTIONAL_TELEMETRY_KEYS = {
    "gpuPcieTxAverageBytesPerSecond",
    "gpuPcieRxAverageBytesPerSecond",
}
_TRIAL_COST_KEYS = {"allocatedUsd", "usageUsd", "inputUsd", "outputUsd", "energyKwh"}
_OPENCOST_KEYS = {
    "windowStart",
    "windowEnd",
    "queriedAt",
    "allocatedUsd",
    "usageUsd",
    "inputUsd",
    "outputUsd",
}
_PROMETHEUS_KEYS = {"stepSeconds", "contextSeconds", "seriesComplete"}
_DATA_QUALITY_KEYS = {"status", "warnings", "missingSeries"}
_FORBIDDEN_KEYS = re.compile(
    r"(?:raw[_-]?(?:prompt|response|trace)|prompt[_-]?text|response[_-]?(?:body|text)|"
    r"api[_-]?key|password|secret|credential|authorization|bearer|kubeconfig)",
    re.IGNORECASE,
)
_IP_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])(?:\d{1,3}\.){3}\d{1,3}(?![A-Za-z0-9])")


def _exact_keys(value: dict[str, Any], expected: set[str], where: str) -> None:
    missing = sorted(expected - value.keys())
    unknown = sorted(value.keys() - expected)
    if missing or unknown:
        raise ValidationError(f"{where} fields mismatch (missing={missing}, unknown={unknown})")


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{where} must be an object")
    return value


def _number(value: Any, where: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{where} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValidationError(f"{where} must be finite and >= {minimum}")
    return result


def _positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{where} must be a positive integer")
    return value


def _timestamp(value: Any, where: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError(f"{where} must be an RFC3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValidationError(f"{where} is not a valid timestamp") from error
    return parsed.astimezone(UTC)


def _scan_for_sensitive_data(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _FORBIDDEN_KEYS.search(str(key)):
                raise ValidationError(
                    f"report input contains prohibited sensitive field at {path}.{key}"
                )
            _scan_for_sensitive_data(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_for_sensitive_data(child, f"{path}[{index}]")
    elif isinstance(value, str):
        for candidate in _IP_CANDIDATE.findall(value):
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue
            raise ValidationError(f"report input contains an IP address at {path}")


def _validate_cost_profile_embedded(value: Any, expected: CostProfile) -> None:
    profile = _mapping(value, "costProfile")
    expected_value = dict(expected.data)
    expected_value["sha256"] = expected.sha256
    if profile != expected_value:
        raise ValidationError("embedded cost profile does not match the selected committed profile")


def _validate_percentiles(value: Any, where: str) -> None:
    percentiles = _mapping(value, where)
    _exact_keys(percentiles, _PERCENTILE_KEYS, where)
    previous = -1.0
    for key in ("p50", "p95", "p99"):
        current = _number(percentiles[key], f"{where}.{key}")
        if current < previous:
            raise ValidationError(f"{where} percentiles must be monotonically non-decreasing")
        previous = current


def validate_normalized_input(
    data: dict[str, Any], *, run_id: str, cost_profile: CostProfile
) -> dict[str, Any]:
    """Validate the publication boundary before any output is written."""
    _scan_for_sensitive_data(data)
    _exact_keys(data, _TOP_LEVEL_KEYS, "report input")
    if data["schemaVersion"] != 1:
        raise ValidationError("report input schemaVersion must be 1")
    run = _mapping(data["run"], "run")
    _exact_keys(run, _RUN_KEYS, "run")
    validate_identifier(run_id, kind="run ID", run_id=True)
    if run["id"] != run_id:
        raise ValidationError("run.id does not match requested run ID")
    started = _timestamp(run["startedAt"], "run.startedAt")
    ended = _timestamp(run["endedAt"], "run.endedAt")
    if ended <= started:
        raise ValidationError("run.endedAt must be after run.startedAt")
    for key in (
        "gitRevision",
        "aiperfVersion",
        "aiperfImage",
        "runtime",
        "runtimePodUid",
        "runtimeVersion",
        "runtimeImage",
        "modelProfile",
        "modelName",
        "modelRevision",
        "gpuModel",
    ):
        if not isinstance(run[key], str) or not run[key].strip():
            raise ValidationError(f"run.{key} must be a non-empty string")
    if not re.fullmatch(r"[a-f0-9]{40}", run["gitRevision"]):
        raise ValidationError("run.gitRevision must be a full lowercase commit SHA")
    if not isinstance(run["gitTreeState"], str) or run["gitTreeState"] not in {
        "clean",
        "dirty",
    }:
        raise ValidationError("run.gitTreeState must be clean or dirty")
    if not isinstance(run["gitWorkspaceSha256"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", run["gitWorkspaceSha256"]
    ):
        raise ValidationError("run.gitWorkspaceSha256 must be a lowercase SHA-256 digest")
    if "@sha256:" not in run["aiperfImage"]:
        raise ValidationError("run.aiperfImage must be pinned by digest")
    if "@sha256:" not in run["runtimeImage"]:
        raise ValidationError("run.runtimeImage must be pinned by digest")
    if not isinstance(run["runtimeProfileSha256"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", run["runtimeProfileSha256"]
    ):
        raise ValidationError("run.runtimeProfileSha256 must be a lowercase SHA-256 digest")
    component_versions = _mapping(run["componentVersions"], "run.componentVersions")
    _exact_keys(component_versions, _COMPONENT_VERSION_KEYS, "run.componentVersions")
    for component, version in component_versions.items():
        if not isinstance(version, str) or not version.strip() or len(version) > 160:
            raise ValidationError(
                f"run.componentVersions.{component} must be a non-empty version string"
            )
    if component_versions["aiperf"] != run["aiperfVersion"]:
        raise ValidationError("AIPerf run and component versions do not match")
    if (
        isinstance(run["runtimePodRestartCount"], bool)
        or not isinstance(run["runtimePodRestartCount"], int)
        or run["runtimePodRestartCount"] < 0
    ):
        raise ValidationError("run.runtimePodRestartCount must be a non-negative integer")
    _positive_int(run["gpuCount"], "run.gpuCount")
    grafana = urlsplit(run["grafanaBaseUrl"])
    if grafana.scheme not in {"http", "https"} or grafana.hostname != "localhost":
        raise ValidationError("run.grafanaBaseUrl must use localhost port-forward access")

    suite = _mapping(data["suite"], "suite")
    _exact_keys(suite, _REPORT_SUITE_KEYS, "suite")
    validate_identifier(suite["name"], kind="suite name")
    if not isinstance(suite["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", suite["sha256"]):
        raise ValidationError("suite.sha256 must be a lowercase SHA-256 digest")
    committed_suite = load_suite(suite["name"])
    if suite["sha256"] != committed_suite.sha256:
        raise ValidationError("suite.sha256 does not match the committed reviewed suite")
    manifest = _mapping(suite["manifest"], "suite.manifest")
    if manifest != committed_suite.data:
        raise ValidationError("suite.manifest does not match the committed reviewed suite")
    if not isinstance(suite["concurrency"], list) or not suite["concurrency"]:
        raise ValidationError("suite.concurrency must be a non-empty list")
    suite_concurrency = [
        _positive_int(value, "suite concurrency") for value in suite["concurrency"]
    ]
    if suite_concurrency != sorted(set(suite_concurrency)):
        raise ValidationError("suite.concurrency must contain sorted unique values")
    requests_per_cell = _positive_int(suite["requestsPerCell"], "suite requestsPerCell")
    trials_per_cell = _positive_int(suite["trialsPerCell"], "suite trialsPerCell")
    confidence = _number(suite["confidenceLevel"], "suite confidenceLevel")
    if confidence != 0.95:
        raise ValidationError("suite.confidenceLevel must be 0.95")
    if suite["concurrency"] != manifest["sweep"]["concurrency"]:
        raise ValidationError("suite concurrency does not match its embedded manifest")
    if requests_per_cell != manifest["sweep"]["requestsPerCell"]:
        raise ValidationError("suite request count does not match its embedded manifest")
    if trials_per_cell != manifest["sweep"]["trialsPerCell"]:
        raise ValidationError("suite trial count does not match its embedded manifest")
    if confidence != manifest["sweep"]["confidenceLevel"]:
        raise ValidationError("suite confidence level does not match its embedded manifest")
    _validate_cost_profile_embedded(data["costProfile"], cost_profile)
    trials = data["trials"]
    if not isinstance(trials, list) or not trials:
        raise ValidationError("trials must be a non-empty array")
    seen: set[tuple[int, int]] = set()
    missing_optional_telemetry = False
    for index, raw_trial in enumerate(trials):
        trial = _mapping(raw_trial, f"trials[{index}]")
        _exact_keys(trial, _TRIAL_KEYS, f"trials[{index}]")
        concurrency = _positive_int(trial["concurrency"], "trial concurrency")
        trial_number = _positive_int(trial["trial"], "trial number")
        if (concurrency, trial_number) in seen:
            raise ValidationError("trials contain a duplicate concurrency/trial pair")
        seen.add((concurrency, trial_number))
        trial_started = _timestamp(trial["startedAt"], "trial.startedAt")
        trial_ended = _timestamp(trial["endedAt"], "trial.endedAt")
        if trial_started < started or trial_ended > ended or trial_ended <= trial_started:
            raise ValidationError("trial interval must be contained within the experiment interval")
        requests = _positive_int(trial["requests"], "trial requests")
        successful = _positive_int(trial["successfulRequests"], "successful requests")
        failed = trial["failedRequests"]
        if isinstance(failed, bool) or not isinstance(failed, int) or failed < 0:
            raise ValidationError("failedRequests must be a non-negative integer")
        if successful + failed != requests:
            raise ValidationError("successfulRequests + failedRequests must equal requests")
        if requests != requests_per_cell:
            raise ValidationError("trial request count does not match the suite")
        _positive_int(trial["inputTokens"], "trial inputTokens")
        _positive_int(trial["outputTokens"], "trial outputTokens")
        metrics = _mapping(trial["metrics"], "trial.metrics")
        _exact_keys(metrics, _METRIC_KEYS, "trial.metrics")
        for name in ("ttftMs", "itlMs", "e2eMs"):
            _validate_percentiles(metrics[name], f"trial.metrics.{name}")
        _number(metrics["requestThroughputPerSecond"], "request throughput")
        _number(metrics["outputTokenThroughputPerSecond"], "token throughput")
        if metrics["streamingTimingEvidence"] is not True:
            raise ValidationError(
                "every measured trial must contain complete AIPerf streaming timing evidence"
            )
        telemetry = _mapping(trial["telemetry"], "trial.telemetry")
        _exact_keys(telemetry, _TELEMETRY_KEYS, "trial.telemetry")
        for key, value in telemetry.items():
            if key in _OPTIONAL_TELEMETRY_KEYS and value is None:
                missing_optional_telemetry = True
                continue
            _number(value, f"trial.telemetry.{key}")
        cost = _mapping(trial["cost"], "trial.cost")
        _exact_keys(cost, _TRIAL_COST_KEYS, "trial.cost")
        for key, value in cost.items():
            _number(value, f"trial.cost.{key}")
        if float(cost["allocatedUsd"]) <= 0:
            raise ValidationError("trial allocated cost must be positive")
        if float(cost["usageUsd"]) <= 0:
            raise ValidationError("trial usage cost must be positive")
        if float(cost["inputUsd"]) <= 0 or float(cost["outputUsd"]) <= 0:
            raise ValidationError("trial input/output cost attribution must be positive")
        if float(cost["energyKwh"]) <= 0:
            raise ValidationError("trial energy attribution must be positive")
        if not math.isclose(
            float(cost["allocatedUsd"]),
            float(cost["inputUsd"]) + float(cost["outputUsd"]),
            rel_tol=0.05,
            abs_tol=1e-9,
        ):
            raise ValidationError("trial input/output costs do not reconcile with allocated cost")

    expected_cells = {
        (concurrency, trial)
        for concurrency in suite_concurrency
        for trial in range(1, trials_per_cell + 1)
    }
    if seen != expected_cells:
        missing = sorted(expected_cells - seen)
        extra = sorted(seen - expected_cells)
        raise ValidationError(
            f"trials do not match the suite matrix (missing={missing}, extra={extra})"
        )

    opencost = _mapping(data["opencost"], "opencost")
    _exact_keys(opencost, _OPENCOST_KEYS, "opencost")
    if opencost["windowStart"] != run["startedAt"] or opencost["windowEnd"] != run["endedAt"]:
        raise ValidationError("OpenCost window must exactly match the experiment window")
    _timestamp(opencost["queriedAt"], "opencost.queriedAt")
    for key in ("allocatedUsd", "usageUsd", "inputUsd", "outputUsd"):
        _number(opencost[key], f"opencost.{key}")
    if float(opencost["allocatedUsd"]) <= 0 or float(opencost["usageUsd"]) <= 0:
        raise ValidationError("OpenCost allocated and usage totals must be positive")
    if float(opencost["inputUsd"]) <= 0 or float(opencost["outputUsd"]) <= 0:
        raise ValidationError("OpenCost inference token attribution must be positive")
    expected_cost = cost_profile.node_hourly_rate * (ended - started).total_seconds() / 3600
    tolerance = max(expected_cost * 0.05, cost_profile.node_hourly_rate / 60)
    difference = abs(float(opencost["allocatedUsd"]) - expected_cost)
    if difference > tolerance:
        raise ValidationError(
            "OpenCost allocated total does not reconcile with hourly-rate audit "
            f"(expected={expected_cost:.6f}, actual={opencost['allocatedUsd']:.6f}, "
            f"tolerance={tolerance:.6f})"
        )

    prometheus = _mapping(data["prometheus"], "prometheus")
    _exact_keys(prometheus, _PROMETHEUS_KEYS, "prometheus")
    _positive_int(prometheus["stepSeconds"], "Prometheus step")
    _positive_int(prometheus["contextSeconds"], "Prometheus context")
    if prometheus["seriesComplete"] is not True:
        raise ValidationError("required Prometheus series are incomplete")
    quality = _mapping(data["dataQuality"], "dataQuality")
    _exact_keys(quality, _DATA_QUALITY_KEYS, "dataQuality")
    if quality["status"] not in {"complete", "warning"}:
        raise ValidationError("dataQuality.status must be complete or warning")
    for key in ("warnings", "missingSeries"):
        if not isinstance(quality[key], list) or not all(
            isinstance(item, str) for item in quality[key]
        ):
            raise ValidationError(f"dataQuality.{key} must be an array of strings")
    if missing_optional_telemetry and (
        quality["status"] != "warning" or not quality["missingSeries"]
    ):
        raise ValidationError("unavailable PCIe profiling must be disclosed as a warning")
    return data


def _mean(values: list[float]) -> float:
    return statistics.fmean(values)


def _ci95(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    # Student-t critical values for 95% two-sided confidence, df=1..30.
    critical = {
        1: 12.706,
        2: 4.303,
        3: 3.182,
        4: 2.776,
        5: 2.571,
        6: 2.447,
        7: 2.365,
        8: 2.306,
        9: 2.262,
        10: 2.228,
    }.get(len(values) - 1, 1.96)
    return critical * statistics.stdev(values) / math.sqrt(len(values))


def _cv(values: list[float]) -> float:
    mean = _mean(values)
    return statistics.stdev(values) / mean if len(values) > 1 and mean else 0.0


def aggregate_trials(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Aggregate trial metrics per concurrency without replacing AIPerf request metrics."""
    groups: dict[int, list[dict[str, Any]]] = {}
    for trial in data["trials"]:
        groups.setdefault(trial["concurrency"], []).append(trial)
    result = []
    for concurrency in sorted(groups):
        trials = sorted(groups[concurrency], key=lambda item: item["trial"])
        latency_values = {
            f"{metric}{percentile.upper()}Ms": [
                float(item["metrics"][f"{metric}Ms"][percentile]) for item in trials
            ]
            for metric in ("ttft", "itl", "e2e")
            for percentile in ("p50", "p95", "p99")
        }
        throughput = [float(item["metrics"]["requestThroughputPerSecond"]) for item in trials]
        output_throughput = [
            float(item["metrics"]["outputTokenThroughputPerSecond"]) for item in trials
        ]
        allocated = sum(float(item["cost"]["allocatedUsd"]) for item in trials)
        usage = sum(float(item["cost"]["usageUsd"]) for item in trials)
        successes = sum(int(item["successfulRequests"]) for item in trials)
        inputs = sum(int(item["inputTokens"]) for item in trials)
        outputs = sum(int(item["outputTokens"]) for item in trials)
        input_cost = sum(float(item["cost"]["inputUsd"]) for item in trials)
        output_cost = sum(float(item["cost"]["outputUsd"]) for item in trials)
        average_telemetry = (
            "gpuUtilizationAveragePct",
            "gpuMemoryUsedAverageBytes",
            "powerAverageWatts",
            "kvCacheAveragePct",
            "promptTokenRateAveragePerSecond",
            "generatedTokenRateAveragePerSecond",
            "cachedTokenRateAveragePerSecond",
            "serverTtftP50Ms",
            "serverTtftP95Ms",
            "serverTtftP99Ms",
            "serverTpotP50Ms",
            "serverTpotP95Ms",
            "serverTpotP99Ms",
            "serverE2eP50Ms",
            "serverE2eP95Ms",
            "serverE2eP99Ms",
            "gpuPcieTxAverageBytesPerSecond",
            "gpuPcieRxAverageBytesPerSecond",
            "gpuSmClockAverageMhz",
            "gpuMemoryClockAverageMhz",
        )
        maximum_telemetry = (
            "temperatureMaxC",
            "requestRunningMax",
            "requestQueueMax",
            "gpuClockThrottleMaxMask",
            "gpuXidMaxCode",
        )
        row = {
            "concurrency": concurrency,
            "trials": len(trials),
            "successfulRequests": successes,
            "failedRequests": sum(int(item["failedRequests"]) for item in trials),
            "requestThroughputPerSecond": _mean(throughput),
            "requestThroughputPerSecondCi95": _ci95(throughput),
            "requestThroughputPerSecondCv": _cv(throughput),
            "outputTokenThroughputPerSecond": _mean(output_throughput),
            "outputTokenThroughputPerSecondCi95": _ci95(output_throughput),
            "outputTokenThroughputPerSecondCv": _cv(output_throughput),
            "allocatedCostUsd": allocated,
            "usageCostUsd": usage,
            "allocatedCostPerRequestUsd": allocated / successes,
            "usageCostPerRequestUsd": usage / successes,
            "inputCostPerMillionTokensUsd": input_cost * 1_000_000 / inputs,
            "outputCostPerMillionTokensUsd": output_cost * 1_000_000 / outputs,
            "blendedCostPerMillionTokensUsd": allocated * 1_000_000 / (inputs + outputs),
            "requestsPerUsd": successes / allocated,
            "tokensPerUsd": (inputs + outputs) / allocated,
            "energyKwh": sum(float(item["cost"]["energyKwh"]) for item in trials),
            "energyWhPerRequest": (
                sum(float(item["cost"]["energyKwh"]) for item in trials) * 1000 / successes
            ),
            "energyKwhPerMillionTokens": (
                sum(float(item["cost"]["energyKwh"]) for item in trials)
                * 1_000_000
                / (inputs + outputs)
            ),
        }
        for name in average_telemetry:
            available = [
                float(item["telemetry"][name])
                for item in trials
                if item["telemetry"][name] is not None
            ]
            row[name] = _mean(available) if available else None
        row.update(
            {
                name: max(float(item["telemetry"][name]) for item in trials)
                for name in maximum_telemetry
            }
        )
        row["gpuMemoryUsedAverageGiB"] = row["gpuMemoryUsedAverageBytes"] / (1024**3)
        row["gpuPcieTxAverageMiBPerSecond"] = (
            row["gpuPcieTxAverageBytesPerSecond"] / (1024**2)
            if row["gpuPcieTxAverageBytesPerSecond"] is not None
            else None
        )
        row["gpuPcieRxAverageMiBPerSecond"] = (
            row["gpuPcieRxAverageBytesPerSecond"] / (1024**2)
            if row["gpuPcieRxAverageBytesPerSecond"] is not None
            else None
        )
        for name, values in latency_values.items():
            row[name] = _mean(values)
            row[f"{name}Ci95"] = _ci95(values)
            row[f"{name}Cv"] = _cv(values)
        result.append(row)
    return result


def _format_number(value: Any, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _svg_chart(
    title: str,
    rows: list[dict[str, Any]],
    series: list[tuple[str, str, str]],
    *,
    y_label: str,
) -> str:
    width, height = 760, 360
    left, right, top, bottom = 70, 24, 45, 60
    plot_width = width - left - right
    plot_height = height - top - bottom
    x_values = [float(row["concurrency"]) for row in rows]
    y_values = [float(row[key]) for key, _, _ in series for row in rows if row[key] is not None]
    x_min, x_max = min(x_values), max(x_values)
    y_min, y_max = 0.0, max(y_values) if y_values else 1.0
    if y_max == 0:
        y_max = 1.0

    def x_pos(value: float) -> float:
        return left + (value - x_min) / max(x_max - x_min, 1) * plot_width

    def y_pos(value: float) -> float:
        return top + plot_height - (value - y_min) / (y_max - y_min) * plot_height

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
        '<rect width="100%" height="100%" fill="#10151f"/>',
        f'<text x="{left}" y="26" fill="#f2f5f8" font-family="system-ui" '
        f'font-size="17">{html.escape(title)}</text>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="#607086"/>',
        f'<line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" '
        f'y2="{top + plot_height}" stroke="#607086"/>',
    ]
    for tick in range(5):
        value = y_max * tick / 4
        y = y_pos(value)
        parts.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_width}" y2="{y:.2f}" '
            'stroke="#263244"/>'
        )
        parts.append(
            f'<text x="{left - 8}" y="{y + 4:.2f}" text-anchor="end" fill="#aab7c4" '
            f'font-family="system-ui" font-size="11">{value:.2f}</text>'
        )
    for row in rows:
        x = x_pos(float(row["concurrency"]))
        parts.append(
            f'<text x="{x:.2f}" y="{top + plot_height + 22}" text-anchor="middle" '
            f'fill="#aab7c4" font-family="system-ui" font-size="11">{row["concurrency"]}</text>'
        )
    for series_index, (key, label, color) in enumerate(series):
        available = [row for row in rows if row[key] is not None]
        points = " ".join(
            f"{x_pos(float(row['concurrency'])):.2f},{y_pos(float(row[key])):.2f}"
            for row in available
        )
        if points:
            parts.append(
                f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>'
            )
        for row in available:
            parts.append(
                f'<circle cx="{x_pos(float(row["concurrency"])):.2f}" '
                f'cy="{y_pos(float(row[key])):.2f}" r="3" fill="{color}"/>'
            )
        legend_x = left + series_index * 210
        parts.append(
            f'<rect x="{legend_x}" y="{height - 22}" width="12" height="3" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{legend_x + 18}" y="{height - 17}" fill="#d8e0e8" '
            f'font-family="system-ui" font-size="11">{html.escape(label)}</text>'
        )
    if not y_values:
        parts.append(
            f'<text x="{left + plot_width / 2:.2f}" y="{top + plot_height / 2:.2f}" '
            'text-anchor="middle" fill="#aab7c4" font-family="system-ui" '
            'font-size="14">N/A: telemetry unavailable on this GPU</text>'
        )
    parts.extend(
        [
            f'<text x="{left + plot_width / 2}" y="{height - 34}" text-anchor="middle" '
            'fill="#aab7c4" font-family="system-ui" font-size="12">Concurrency</text>',
            f'<text transform="translate(16 {top + plot_height / 2}) rotate(-90)" '
            f'text-anchor="middle" fill="#aab7c4" font-family="system-ui" '
            f'font-size="12">{html.escape(y_label)}</text>',
            "</svg>\n",
        ]
    )
    return "".join(parts)


def _grafana_links(run: dict[str, Any]) -> list[tuple[str, str]]:
    base = run["grafanaBaseUrl"].rstrip("/")
    common = {
        "from": run["startedAt"],
        "to": run["endedAt"],
        "var-runtime": run["runtime"],
        "var-model_name": run["modelName"],
        "var-model_profile": run["modelProfile"],
    }
    dashboards = (
        ("Platform fleet", "kip-platform-fleet"),
        ("Model serving", "kip-model-serving"),
        ("GPU and node", "kip-gpu-node-health"),
        ("Distributed topology", "kip-distributed-topology"),
        ("Benchmark explorer", "kip-benchmark-explorer"),
        ("Inference cost", "kip-inference-cost"),
    )
    links = []
    for label, uid in dashboards:
        parameters = dict(common)
        links.append((label, f"{base}/d/{uid}?{urlencode(parameters)}"))
    return links


def _svg_frontier(rows: list[dict[str, Any]]) -> str:
    """Render throughput vs latency, labelling cost and Pareto membership."""
    width, height = 760, 380
    left, right, top, bottom = 78, 35, 48, 65
    plot_width = width - left - right
    plot_height = height - top - bottom
    x_values = [float(row["e2eP99Ms"]) for row in rows]
    y_values = [float(row["outputTokenThroughputPerSecond"]) for row in rows]
    x_min, x_max = min(x_values), max(x_values)
    y_min, y_max = min(y_values), max(y_values)

    def position(value: float, low: float, high: float, start: float, length: float) -> float:
        return start + (value - low) / max(high - low, 1) * length

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="Throughput latency cost frontier">',
        '<rect width="100%" height="100%" fill="#10151f"/>',
        f'<text x="{left}" y="27" fill="#f2f5f8" font-family="system-ui" font-size="17">Throughput / latency / cost frontier</text>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="#607086"/>',
        f'<line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" stroke="#607086"/>',
    ]
    for row in rows:
        x = position(float(row["e2eP99Ms"]), x_min, x_max, left, plot_width)
        y = (
            top
            + plot_height
            - position(float(row["outputTokenThroughputPerSecond"]), y_min, y_max, 0, plot_height)
        )
        color = "#5ed6c1" if row["paretoOptimal"] else "#607086"
        parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="7" fill="{color}"/>')
        parts.append(
            f'<text x="{x + 10:.2f}" y="{y - 6:.2f}" fill="#eef3f8" font-family="system-ui" font-size="11">c{row["concurrency"]} · ${row["blendedCostPerMillionTokensUsd"]:.3f}/1M</text>'
        )
    parts.extend(
        [
            f'<text x="{left + plot_width / 2}" y="{height - 22}" text-anchor="middle" fill="#aab7c4" font-family="system-ui" font-size="12">E2E P99 milliseconds (lower is better)</text>',
            f'<text transform="translate(18 {top + plot_height / 2}) rotate(-90)" text-anchor="middle" fill="#aab7c4" font-family="system-ui" font-size="12">Output tokens / second (higher is better)</text>',
            '<circle cx="560" cy="25" r="5" fill="#5ed6c1"/><text x="570" y="29" fill="#aab7c4" font-family="system-ui" font-size="11">Pareto optimal</text>',
            "</svg>\n",
        ]
    )
    return "".join(parts)


def _render_html(data: dict[str, Any], summary: dict[str, Any]) -> str:
    run = data["run"]
    profile = data["costProfile"]
    rows = summary["concurrencyResults"]
    headings = (
        ("concurrency", "Concurrency"),
        ("ttftP50Ms", "TTFT P50 ms"),
        ("ttftP95Ms", "TTFT P95 ms"),
        ("ttftP99Ms", "TTFT P99 ms"),
        ("itlP50Ms", "AIPerf ITL P50 ms"),
        ("itlP95Ms", "AIPerf ITL P95 ms"),
        ("itlP99Ms", "AIPerf ITL P99 ms"),
        ("e2eP50Ms", "E2E P50 ms"),
        ("e2eP95Ms", "E2E P95 ms"),
        ("e2eP99Ms", "E2E P99 ms"),
        ("requestThroughputPerSecond", "Req/s"),
        ("outputTokenThroughputPerSecond", "Output tok/s"),
        ("gpuUtilizationAveragePct", "GPU util %"),
        ("gpuMemoryUsedAverageGiB", "GPU memory GiB"),
        ("powerAverageWatts", "Power W"),
        ("temperatureMaxC", "Temperature max C"),
        ("kvCacheAveragePct", "KV-cache %"),
        ("requestRunningMax", "Running max"),
        ("requestQueueMax", "Waiting max"),
        ("promptTokenRateAveragePerSecond", "Server prompt tok/s"),
        ("generatedTokenRateAveragePerSecond", "Server generated tok/s"),
        ("cachedTokenRateAveragePerSecond", "Server cached tok/s"),
        ("serverTtftP95Ms", "Server TTFT P95 ms"),
        ("serverTpotP95Ms", "Server TPOT P95 ms"),
        ("serverE2eP95Ms", "Server E2E P95 ms"),
        ("gpuPcieTxAverageMiBPerSecond", "PCIe TX MiB/s"),
        ("gpuPcieRxAverageMiBPerSecond", "PCIe RX MiB/s"),
        ("gpuSmClockAverageMhz", "SM clock MHz"),
        ("gpuMemoryClockAverageMhz", "Memory clock MHz"),
        ("gpuClockThrottleMaxMask", "Throttle mask max"),
        ("gpuXidMaxCode", "XID max code"),
        ("allocatedCostUsd", "Allocated USD"),
        ("usageCostUsd", "Usage USD"),
        ("allocatedCostPerRequestUsd", "Allocated USD/request"),
        ("usageCostPerRequestUsd", "Usage USD/request"),
        ("inputCostPerMillionTokensUsd", "Input USD/1M"),
        ("outputCostPerMillionTokensUsd", "Output USD/1M"),
        ("blendedCostPerMillionTokensUsd", "USD/1M tokens"),
        ("requestsPerUsd", "Requests/USD"),
        ("tokensPerUsd", "Tokens/USD"),
        ("energyWhPerRequest", "Wh/request"),
        ("energyKwhPerMillionTokens", "kWh/1M tokens"),
        ("paretoOptimal", "Frontier"),
    )
    header = "".join(f"<th>{html.escape(label)}</th>" for _, label in headings)
    body = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape(_format_number(row[key]))}</td>" for key, _ in headings)
        + "</tr>"
        for row in rows
    )
    variability_metrics = (
        ("ttftP95Ms", "TTFT P95", "ms"),
        ("ttftP99Ms", "TTFT P99", "ms"),
        ("itlP95Ms", "AIPerf ITL P95", "ms"),
        ("itlP99Ms", "AIPerf ITL P99", "ms"),
        ("e2eP95Ms", "E2E P95", "ms"),
        ("e2eP99Ms", "E2E P99", "ms"),
        ("requestThroughputPerSecond", "Request throughput", "req/s"),
        ("outputTokenThroughputPerSecond", "Output-token throughput", "tok/s"),
    )
    variability_body = "".join(
        "<tr>"
        f"<td>{row['concurrency']}</td>"
        f"<td>{html.escape(label)}</td>"
        f"<td>{row[key]:.3f} {unit}</td>"
        f"<td>±{row[f'{key}Ci95']:.3f} {unit}</td>"
        f"<td>{row[f'{key}Cv'] * 100:.2f}%</td>"
        "</tr>"
        for row in rows
        for key, label, unit in variability_metrics
    )
    links = "".join(
        f'<li><a href="{html.escape(url, quote=True)}">{html.escape(label)}</a></li>'
        for label, url in _grafana_links(run)
    )
    warnings = data["dataQuality"]["warnings"]
    warning_markup = (
        "".join(f"<li>{html.escape(item)}</li>" for item in warnings)
        if warnings
        else "<li>None</li>"
    )
    trial_headings = (
        ("concurrency", "Concurrency"),
        ("trial", "Trial"),
        ("successfulRequests", "Successes"),
        ("failedRequests", "Errors"),
        ("allocatedCostUsd", "Allocated USD"),
        ("usageCostUsd", "Usage USD"),
        ("allocatedCostPerRequestUsd", "Allocated USD/request"),
        ("usageCostPerRequestUsd", "Usage USD/request"),
        ("inputCostPerMillionTokensUsd", "Input USD/1M"),
        ("outputCostPerMillionTokensUsd", "Output USD/1M"),
        ("blendedCostPerMillionTokensUsd", "Blended USD/1M"),
        ("energyWhPerRequest", "Wh/request"),
    )
    trial_header = "".join(f"<th>{html.escape(label)}</th>" for _, label in trial_headings)
    trial_body = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape(_format_number(row[key]))}</td>" for key, _ in trial_headings)
        + "</tr>"
        for row in summary["trialResults"]
    )
    reconciliation = summary["cost"]
    suite_manifest = html.escape(json.dumps(summary["suite"]["manifest"], sort_keys=True, indent=2))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KIP benchmark {html.escape(run["id"])}</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0b1018; --panel:#141c28; --text:#eef3f8; --muted:#aab7c4; --accent:#5ed6c1; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,sans-serif; }}
    main {{ max-width:1200px; margin:auto; padding:32px 24px 64px; }}
    h1,h2 {{ line-height:1.2; }} .muted {{ color:var(--muted); }}
    .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px; }}
    .card,section {{ background:var(--panel); border:1px solid #263244; border-radius:10px; padding:16px; margin:14px 0; }}
    .value {{ color:var(--accent); font-size:1.5rem; font-weight:650; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }} th,td {{ padding:8px; border-bottom:1px solid #2b384a; text-align:right; }} th:first-child,td:first-child {{ text-align:left; }}
    img {{ max-width:100%; height:auto; }} a {{ color:#77c8ff; }} code {{ color:#d6b7ff; }} pre {{ overflow:auto; padding:12px; background:#0b1018; border-radius:6px; }}
  </style>
</head>
<body><main>
  <h1>Kube Inference Platform benchmark</h1>
  <p class="muted">Run <code>{html.escape(run["id"])}</code> · {html.escape(run["startedAt"])} to {html.escape(run["endedAt"])}</p>
  <div class="cards">
    <div class="card"><div class="muted">Model</div><div class="value">{html.escape(run["modelName"])}</div></div>
    <div class="card"><div class="muted">Experiment cost</div><div class="value">${summary["cost"]["experimentAllocatedUsd"]:.4f}</div></div>
    <div class="card"><div class="muted">Measured cost</div><div class="value">${summary["cost"]["measuredAllocatedUsd"]:.4f}</div></div>
    <div class="card"><div class="muted">Node rate</div><div class="value">${profile["nodeHourlyRateUsd"]:.4f}/h</div></div>
    <div class="card"><div class="muted">Requests / USD</div><div class="value">{summary["cost"]["requestsPerUsd"]:.1f}</div></div>
    <div class="card"><div class="muted">Tokens / USD</div><div class="value">{summary["cost"]["tokensPerUsd"]:.0f}</div></div>
    <div class="card"><div class="muted">Energy / request</div><div class="value">{summary["totals"]["energyWhPerRequest"]:.4f} Wh</div></div>
    <div class="card"><div class="muted">Errors</div><div class="value">{summary["totals"]["failedRequests"]}</div></div>
  </div>
  <section><h2>Streaming evidence boundary</h2>
    <p>AIPerf ran in streaming mode, the non-cancelled summary contained positive client-observed TTFT and inter-token latency (ITL) percentiles, and request success/error accounting matched the suite total. ITL is not labeled as server TPOT; server TPOT is reported separately from vLLM's time-per-output-token histogram.</p>
    <p class="muted">AIPerf summary-only artifacts do not retain SSE frames, so this evidence cannot prove terminal <code>[DONE]</code> framing. The runtime smoke test is the explicit incremental-SSE and <code>[DONE]</code> protocol checkpoint.</p>
  </section>
  <section><h2>Suite and workload manifest</h2>
    <p>Committed suite <strong>{html.escape(summary["suite"]["name"])}</strong> · SHA-256 <code>{html.escape(summary["suite"]["sha256"])}</code></p>
    <pre><code>{suite_manifest}</code></pre>
  </section>
  <section><h2>Performance and cost</h2><div style="overflow:auto"><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div></section>
  <section><h2>Per-trial cost accounting</h2><div style="overflow:auto"><table><thead><tr>{trial_header}</tr></thead><tbody>{trial_body}</tbody></table></div></section>
  <section><h2>Trial variability (95% confidence)</h2><div style="overflow:auto"><table><thead><tr><th>Concurrency</th><th>Metric</th><th>Mean</th><th>95% CI half-width</th><th>CV</th></tr></thead><tbody>{variability_body}</tbody></table></div></section>
  <section><h2>Latency</h2><img src="charts/latency.svg" alt="Latency by concurrency"></section>
  <section><h2>Throughput</h2><img src="charts/throughput.svg" alt="Throughput by concurrency"></section>
  <section><h2>Server token rates</h2><img src="charts/server-token-rates.svg" alt="Prometheus server token rates by concurrency"></section>
  <section><h2>Server latency</h2><img src="charts/server-latency.svg" alt="Prometheus server latency by concurrency"></section>
  <section><h2>Cost efficiency</h2><img src="charts/cost.svg" alt="Cost by concurrency"></section>
  <section><h2>GPU efficiency</h2><img src="charts/gpu.svg" alt="GPU utilization by concurrency"></section>
  <section><h2>GPU memory</h2><img src="charts/memory.svg" alt="GPU memory by concurrency"></section>
  <section><h2>Power and temperature</h2><img src="charts/power-temperature.svg" alt="Power and temperature by concurrency"></section>
  <section><h2>GPU PCIe traffic</h2><img src="charts/gpu-pcie.svg" alt="GPU PCIe traffic by concurrency"></section>
  <section><h2>GPU clocks</h2><img src="charts/gpu-clocks.svg" alt="GPU clocks by concurrency"></section>
  <section><h2>GPU health signals</h2><img src="charts/gpu-health.svg" alt="GPU throttle and XID signals by concurrency"></section>
  <section><h2>KV cache and queue</h2><img src="charts/runtime-pressure.svg" alt="KV cache and request queue by concurrency"></section>
  <section><h2>Energy intensity</h2><img src="charts/energy.svg" alt="Energy per request by concurrency"></section>
  <section><h2>Throughput, latency, and cost frontier</h2><img src="charts/frontier.svg" alt="Frontier points by concurrency"></section>
  <section><h2>Cost reconciliation</h2>
    <p>Rate audit: <strong>${reconciliation["experimentAuditUsd"]:.6f}</strong>; OpenCost allocated: <strong>${reconciliation["experimentAllocatedUsd"]:.6f}</strong>; difference: ${reconciliation["reconciliationDifferenceUsd"]:.6f}; permitted tolerance: ${reconciliation["reconciliationToleranceUsd"]:.6f}; status: <strong>{reconciliation["reconciliationStatus"]}</strong>.</p>
    <p>Measured allocated / usage cost per successful request: ${reconciliation["measuredAllocatedCostPerSuccessfulRequestUsd"]:.6f} / ${reconciliation["measuredUsageCostPerSuccessfulRequestUsd"]:.6f}.</p>
    <p>Measured input / output / blended cost per million tokens: ${reconciliation["measuredInputCostPerMillionTokensUsd"]:.4f} / ${reconciliation["measuredOutputCostPerMillionTokensUsd"]:.4f} / ${reconciliation["measuredBlendedCostPerMillionTokensUsd"]:.4f}.</p>
    <p>Measured energy: {summary["totals"]["energyKwh"]:.6f} kWh; {summary["totals"]["energyKwhPerMillionTokens"]:.6f} kWh per million tokens.</p>
  </section>
  <section><h2>Cost provenance</h2>
    <p>{html.escape(profile["provider"])} · {html.escape(profile["machineType"])} · {html.escape(profile["region"])}</p>
    <p>Whole-node USD/hour: <strong>{profile["nodeHourlyRateUsd"]:.6f}</strong>; effective {html.escape(profile["effectiveFrom"])}; power included.</p>
    <p><a href="{html.escape(profile["source"], quote=True)}">{("User-confirmed billing portal" if profile.get("sourceType") == "user-confirmed-billing" else "Provider live instance rate" if profile.get("sourceType") == "provider-live-rate" else "Published rate source")}</a> · profile SHA-256 <code>{html.escape(profile["sha256"])}</code></p>
    <p class="muted">Costs are transparent rate-based estimates, not a provider invoice. Allocated cost includes idle node time; usage cost is an efficiency view. Per-trial charges use the reviewed node hourly rate times exact elapsed time, attributed by OpenCost's model share; usage and input/output splits retain OpenCost's measured proportions. This avoids treating coarse short-window OpenCost totals as exact elapsed billing.</p>
  </section>
  <section><h2>Reproducibility</h2>
    <p>Git commit <code>{html.escape(run["gitRevision"])}</code> · tree state <strong>{html.escape(run["gitTreeState"])}</strong> · workspace SHA-256 <code>{html.escape(run["gitWorkspaceSha256"])}</code></p>
    <p>AIPerf <strong>{html.escape(run["aiperfVersion"])}</strong> · image <code>{html.escape(run["aiperfImage"])}</code></p>
    <p>Runtime profile <strong>{html.escape(run["modelProfile"])}</strong> · profile SHA-256 <code>{html.escape(run["runtimeProfileSha256"])}</code> · {html.escape(run["gpuModel"])} × {run["gpuCount"]}</p>
    <p>Serving Pod UID <code>{html.escape(run["runtimePodUid"])}</code> · restart count held at <strong>{run["runtimePodRestartCount"]}</strong> for the full run.</p>
    <p>Component versions: <code>{html.escape(json.dumps(run["componentVersions"], sort_keys=True, separators=(",", ":")))}</code></p>
  </section>
  <section><h2>Grafana drill-down</h2><ul>{links}</ul></section>
  <section><h2>Data quality</h2><p>Status: <strong>{html.escape(data["dataQuality"]["status"])}</strong></p><ul>{warning_markup}</ul></section>
</main></body></html>
"""


def build_summary(data: dict[str, Any], profile: CostProfile) -> dict[str, Any]:
    """Build a stable, publication-safe summary."""
    aggregate = aggregate_trials(data)
    for row in aggregate:
        row["paretoOptimal"] = not any(
            other["outputTokenThroughputPerSecond"] >= row["outputTokenThroughputPerSecond"]
            and other["e2eP99Ms"] <= row["e2eP99Ms"]
            and other["blendedCostPerMillionTokensUsd"] <= row["blendedCostPerMillionTokensUsd"]
            and (
                other["outputTokenThroughputPerSecond"] > row["outputTokenThroughputPerSecond"]
                or other["e2eP99Ms"] < row["e2eP99Ms"]
                or other["blendedCostPerMillionTokensUsd"] < row["blendedCostPerMillionTokensUsd"]
            )
            for other in aggregate
            if other is not row
        )
    measured_allocated = sum(row["allocatedCostUsd"] for row in aggregate)
    measured_usage = sum(row["usageCostUsd"] for row in aggregate)
    experiment_seconds = (
        _timestamp(data["run"]["endedAt"], "run.endedAt")
        - _timestamp(data["run"]["startedAt"], "run.startedAt")
    ).total_seconds()
    audit = profile.node_hourly_rate * experiment_seconds / 3600
    actual = float(data["opencost"]["allocatedUsd"])
    tolerance = max(audit * 0.05, profile.node_hourly_rate / 60)
    successes = sum(row["successfulRequests"] for row in aggregate)
    failures = sum(row["failedRequests"] for row in aggregate)
    input_tokens = sum(int(item["inputTokens"]) for item in data["trials"])
    output_tokens = sum(int(item["outputTokens"]) for item in data["trials"])
    measured_energy = sum(row["energyKwh"] for row in aggregate)
    trial_results = []
    for item in data["trials"]:
        successful = int(item["successfulRequests"])
        input_count = int(item["inputTokens"])
        output_count = int(item["outputTokens"])
        allocated = float(item["cost"]["allocatedUsd"])
        energy = float(item["cost"]["energyKwh"])
        trial_results.append(
            {
                "concurrency": int(item["concurrency"]),
                "trial": int(item["trial"]),
                "startedAt": item["startedAt"],
                "endedAt": item["endedAt"],
                "successfulRequests": successful,
                "failedRequests": int(item["failedRequests"]),
                "allocatedCostUsd": allocated,
                "usageCostUsd": float(item["cost"]["usageUsd"]),
                "allocatedCostPerRequestUsd": allocated / successful,
                "usageCostPerRequestUsd": float(item["cost"]["usageUsd"]) / successful,
                "inputCostPerMillionTokensUsd": (
                    float(item["cost"]["inputUsd"]) * 1_000_000 / input_count
                ),
                "outputCostPerMillionTokensUsd": (
                    float(item["cost"]["outputUsd"]) * 1_000_000 / output_count
                ),
                "blendedCostPerMillionTokensUsd": (
                    allocated * 1_000_000 / (input_count + output_count)
                ),
                "energyWhPerRequest": energy * 1000 / successful,
            }
        )
    return {
        "schemaVersion": 1,
        "generatedAt": data["run"]["endedAt"],
        "run": data["run"],
        "suite": {
            "name": data["suite"]["name"],
            "sha256": data["suite"]["sha256"],
            "manifest": data["suite"]["manifest"],
        },
        "costProfile": {
            "name": profile.name,
            "sha256": profile.sha256,
            "nodeHourlyRateUsd": profile.node_hourly_rate,
            "gpuHourlyRateUsd": profile.gpu_hourly_rate,
            "source": profile.data["source"],
            "effectiveFrom": profile.data["effectiveFrom"],
        },
        "cost": {
            "trialAttributionMethod": "exact-node-time-times-opencost-model-share",
            "experimentAllocatedUsd": actual,
            "experimentUsageUsd": float(data["opencost"]["usageUsd"]),
            "experimentInputUsd": float(data["opencost"]["inputUsd"]),
            "experimentOutputUsd": float(data["opencost"]["outputUsd"]),
            "experimentAuditUsd": audit,
            "reconciliationDifferenceUsd": abs(actual - audit),
            "reconciliationToleranceUsd": tolerance,
            "reconciliationStatus": "pass",
            "measuredAllocatedUsd": measured_allocated,
            "measuredUsageUsd": measured_usage,
            "measuredAllocatedCostPerSuccessfulRequestUsd": measured_allocated / successes,
            "measuredUsageCostPerSuccessfulRequestUsd": measured_usage / successes,
            "measuredInputCostPerMillionTokensUsd": (
                sum(float(item["cost"]["inputUsd"]) for item in data["trials"])
                * 1_000_000
                / input_tokens
            ),
            "measuredOutputCostPerMillionTokensUsd": (
                sum(float(item["cost"]["outputUsd"]) for item in data["trials"])
                * 1_000_000
                / output_tokens
            ),
            "measuredBlendedCostPerMillionTokensUsd": (
                measured_allocated * 1_000_000 / (input_tokens + output_tokens)
            ),
            "requestsPerUsd": successes / measured_allocated,
            "tokensPerUsd": (input_tokens + output_tokens) / measured_allocated,
        },
        "totals": {
            "successfulRequests": successes,
            "failedRequests": failures,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "energyKwh": measured_energy,
            "energyWhPerRequest": measured_energy * 1000 / successes,
            "energyKwhPerMillionTokens": measured_energy
            * 1_000_000
            / (input_tokens + output_tokens),
        },
        "concurrencyResults": aggregate,
        "trialResults": trial_results,
        "dataQuality": data["dataQuality"],
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise BenchError("cannot write an empty benchmark summary")
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def generate_report(
    data: dict[str, Any],
    *,
    run_id: str,
    cost_profile: CostProfile,
    output: Path,
    replace: bool = False,
) -> Path:
    """Validate and atomically publish deterministic HTML/JSON/CSV/SVG output."""
    validate_normalized_input(data, run_id=run_id, cost_profile=cost_profile)
    if output.exists():
        if not replace:
            raise BenchError(f"refusing to overwrite existing report directory: {output}")
        try:
            existing = json.loads((output / "reproducibility.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BenchError(f"refusing to replace an unrecognized report: {output}") from error
        if existing.get("runId") != run_id:
            raise BenchError(f"refusing to replace a report owned by another run: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}-", dir=output.parent))
    try:
        charts = temporary / "charts"
        charts.mkdir()
        summary = build_summary(data, cost_profile)
        (temporary / "summary.json").write_bytes(canonical_json(summary))
        _write_csv(temporary / "summary.csv", summary["concurrencyResults"])
        reproducibility = {
            "schemaVersion": 1,
            "runId": run_id,
            "gitRevision": data["run"]["gitRevision"],
            "gitTreeState": data["run"]["gitTreeState"],
            "gitWorkspaceSha256": data["run"]["gitWorkspaceSha256"],
            "aiperfVersion": data["run"]["aiperfVersion"],
            "aiperfImage": data["run"]["aiperfImage"],
            "componentVersions": data["run"]["componentVersions"],
            "runtimePodUid": data["run"]["runtimePodUid"],
            "runtimePodRestartCount": data["run"]["runtimePodRestartCount"],
            "runtimeImage": data["run"]["runtimeImage"],
            "runtimeProfileSha256": data["run"]["runtimeProfileSha256"],
            "modelRevision": data["run"]["modelRevision"],
            "suite": summary["suite"],
            "costProfile": summary["costProfile"],
        }
        (temporary / "reproducibility.json").write_bytes(canonical_json(reproducibility))
        rows = summary["concurrencyResults"]
        (charts / "latency.svg").write_text(
            _svg_chart(
                "Tail latency by concurrency",
                rows,
                [
                    ("ttftP95Ms", "TTFT P95", "#5ed6c1"),
                    ("itlP99Ms", "AIPerf ITL P99", "#77c8ff"),
                    ("e2eP99Ms", "E2E P99", "#ff9f68"),
                ],
                y_label="Milliseconds",
            ),
            encoding="utf-8",
        )
        (charts / "throughput.svg").write_text(
            _svg_chart(
                "Output-token throughput",
                rows,
                [("outputTokenThroughputPerSecond", "Output tokens/s", "#77c8ff")],
                y_label="Tokens / second",
            ),
            encoding="utf-8",
        )
        (charts / "server-token-rates.svg").write_text(
            _svg_chart(
                "Prometheus-correlated server token rates",
                rows,
                [
                    ("promptTokenRateAveragePerSecond", "Prompt tokens/s", "#77c8ff"),
                    (
                        "generatedTokenRateAveragePerSecond",
                        "Generated tokens/s",
                        "#5ed6c1",
                    ),
                    ("cachedTokenRateAveragePerSecond", "Cached tokens/s", "#d6b7ff"),
                ],
                y_label="Tokens / second",
            ),
            encoding="utf-8",
        )
        (charts / "server-latency.svg").write_text(
            _svg_chart(
                "Prometheus-correlated server P95 latency",
                rows,
                [
                    ("serverTtftP95Ms", "Server TTFT P95", "#5ed6c1"),
                    ("serverTpotP95Ms", "Server TPOT P95", "#77c8ff"),
                    ("serverE2eP95Ms", "Server E2E P95", "#ff9f68"),
                ],
                y_label="Milliseconds",
            ),
            encoding="utf-8",
        )
        (charts / "cost.svg").write_text(
            _svg_chart(
                "Allocated token cost",
                rows,
                [
                    ("inputCostPerMillionTokensUsd", "Input USD / 1M", "#77c8ff"),
                    ("outputCostPerMillionTokensUsd", "Output USD / 1M", "#ff9f68"),
                    ("blendedCostPerMillionTokensUsd", "Blended USD / 1M", "#d6b7ff"),
                ],
                y_label="USD / 1M tokens",
            ),
            encoding="utf-8",
        )
        (charts / "gpu.svg").write_text(
            _svg_chart(
                "Average GPU utilization",
                rows,
                [("gpuUtilizationAveragePct", "GPU utilization", "#f7d774")],
                y_label="Percent",
            ),
            encoding="utf-8",
        )
        (charts / "memory.svg").write_text(
            _svg_chart(
                "Average GPU memory used",
                rows,
                [("gpuMemoryUsedAverageGiB", "GPU memory GiB", "#77c8ff")],
                y_label="GiB",
            ),
            encoding="utf-8",
        )
        (charts / "power-temperature.svg").write_text(
            _svg_chart(
                "GPU power and temperature",
                rows,
                [
                    ("powerAverageWatts", "Power watts", "#f7d774"),
                    ("temperatureMaxC", "Temperature C", "#ff776d"),
                ],
                y_label="Watts / Celsius",
            ),
            encoding="utf-8",
        )
        (charts / "gpu-pcie.svg").write_text(
            _svg_chart(
                "GPU PCIe traffic",
                rows,
                [
                    ("gpuPcieTxAverageMiBPerSecond", "PCIe TX MiB/s", "#77c8ff"),
                    ("gpuPcieRxAverageMiBPerSecond", "PCIe RX MiB/s", "#d6b7ff"),
                ],
                y_label="MiB / second",
            ),
            encoding="utf-8",
        )
        (charts / "gpu-clocks.svg").write_text(
            _svg_chart(
                "GPU clocks",
                rows,
                [
                    ("gpuSmClockAverageMhz", "SM clock", "#f7d774"),
                    ("gpuMemoryClockAverageMhz", "Memory clock", "#77c8ff"),
                ],
                y_label="MHz",
            ),
            encoding="utf-8",
        )
        (charts / "gpu-health.svg").write_text(
            _svg_chart(
                "GPU health event signals",
                rows,
                [
                    ("gpuClockThrottleMaxMask", "Throttle reason mask", "#ff9f68"),
                    ("gpuXidMaxCode", "XID code", "#ff776d"),
                ],
                y_label="Code / bitmask (zero is clear)",
            ),
            encoding="utf-8",
        )
        (charts / "runtime-pressure.svg").write_text(
            _svg_chart(
                "KV-cache pressure and waiting requests",
                rows,
                [
                    ("kvCacheAveragePct", "KV-cache percent", "#5ed6c1"),
                    ("requestQueueMax", "Waiting requests", "#ff9f68"),
                ],
                y_label="Percent / requests",
            ),
            encoding="utf-8",
        )
        (charts / "energy.svg").write_text(
            _svg_chart(
                "Energy intensity",
                rows,
                [("energyWhPerRequest", "Wh / request", "#a7e06f")],
                y_label="Wh / request",
            ),
            encoding="utf-8",
        )
        (charts / "frontier.svg").write_text(_svg_frontier(rows), encoding="utf-8")
        (temporary / "index.html").write_text(_render_html(data, summary), encoding="utf-8")
        if output.exists():
            backup = output.parent / f".{output.name}.previous"
            if backup.exists():
                raise BenchError(f"stale report backup requires inspection: {backup}")
            output.replace(backup)
            try:
                temporary.replace(output)
            except Exception:
                backup.replace(output)
                raise
            shutil.rmtree(backup)
        else:
            temporary.replace(output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output


def load_normalized_input(path: Path) -> dict[str, Any]:
    """Read normalized, already-sanitized AIPerf/Prometheus/OpenCost input."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValidationError(f"normalized report input does not exist: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot read normalized report input: {error}") from error
    if not isinstance(value, dict):
        raise ValidationError("normalized report input must be a JSON object")
    return value
