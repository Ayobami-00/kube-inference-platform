"""Normalize AIPerf, Prometheus, and OpenCost data and publish a report."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .aiperf import AIPERF_IMAGE, AIPERF_VERSION
from .clients import GrafanaClient, OpenCostClient, PrometheusClient
from .config import CostProfile, Suite, config_hash, load_runtime_model_identity
from .errors import BenchError, ExternalCommandError
from .paths import reports_root, repository_root
from .report import generate_report, validate_normalized_input

OBSERVABILITY_NAMESPACE = "kip-observability"
PROMETHEUS_SERVICE = "kip-monitoring-prometheus"
OPENCOST_SERVICE = "kip-opencost"
GRAFANA_SERVICE = "kip-monitoring-grafana"
GRAFANA_ADMIN_SECRET = "kip-grafana-admin"
DEFAULT_RUNTIME_IMAGE = (
    "vllm/vllm-openai:v0.29.0-x86_64-cu129@"
    "sha256:3e10e8189823e0f7ae4620c271bcdaaf64127ec7d0edc351591a508498b7684a"
)
_COMPONENT_VERSION_SOURCES = {
    "kubernetes": ("deploy/cluster/versions.env", "KIP_KUBERNETES_VERSION"),
    "containerd": ("deploy/cluster/versions.env", "KIP_CONTAINERD_VERSION"),
    "helm": ("deploy/cluster/versions.env", "KIP_HELM_VERSION"),
    "cilium": ("deploy/cluster/versions.env", "KIP_CILIUM_VERSION"),
    "gpuOperator": ("deploy/gpu/versions.env", "KIP_GPU_OPERATOR_VERSION"),
    "nvidiaDriver": ("deploy/gpu/versions.env", "KIP_NVIDIA_DRIVER_VERSION"),
    "dcgmExporter": ("deploy/gpu/versions.env", "KIP_DCGM_EXPORTER_VERSION"),
    "vllm": ("deploy/runtime/vllm/versions.env", "KIP_VLLM_VERSION"),
    "kubePrometheusStack": (
        "deploy/observability/versions.env",
        "KIP_KUBE_PROMETHEUS_STACK_VERSION",
    ),
    "prometheus": ("deploy/observability/versions.env", "KIP_PROMETHEUS_VERSION"),
    "grafanaChart": ("deploy/observability/versions.env", "KIP_GRAFANA_CHART_VERSION"),
    "opencost": ("deploy/observability/versions.env", "KIP_OPENCOST_VERSION"),
}
_ENV_ASSIGNMENT = re.compile(r'^([A-Z][A-Z0-9_]*)="([^"\n]*)"$')

# The report adapter uses stable platform gauges for runtime state, and raw
# five-second DCGM samples joined to stable Pod identity for GPU state. Counters
# and histograms are deliberately queried from the pinned vLLM metric contract with
# an exact per-trial range selector: the continuously useful recording rules use
# five-minute rates and must not bleed warmup, cooldown, or adjacent cells into
# a benchmark result. Raw names remain contained in this adapter and never leak
# into the normalized report schema.
#
# Field name -> (series aggregation, stable metric or raw override, unit scale). The series
# aggregation is evaluated at every scrape; values are then averaged across the
# exact trial except for state fields whose aggregation is ``max``.
GAUGE_TELEMETRY_QUERIES = {
    "gpuUtilizationAveragePct": ("avg", "kip:gpu:runtime_utilization_ratio", 100.0),
    "gpuMemoryUsedAverageBytes": ("avg", "kip:gpu:runtime_memory_used_bytes", 1.0),
    # Sum attributed GPUs first, then average those totals across the trial. A
    # multi-GPU deployment must not under-report its energy by averaging GPUs.
    "powerAverageWatts": ("sum", "kip:gpu:runtime_power_watts", 1.0),
    "temperatureMaxC": ("max", "kip:gpu:runtime_temperature_celsius", 1.0),
    "kvCacheAveragePct": ("avg", "kip:kv_cache:usage_ratio", 100.0),
    "requestRunningMax": ("max", "kip:requests:running", 1.0),
    "requestQueueMax": ("max", "kip:requests:waiting", 1.0),
    "gpuPcieTxAverageBytesPerSecond": (
        "avg",
        "kip:gpu:runtime_pcie_tx_bytes_per_second",
        1.0,
    ),
    "gpuPcieRxAverageBytesPerSecond": (
        "avg",
        "kip:gpu:runtime_pcie_rx_bytes_per_second",
        1.0,
    ),
    "gpuSmClockAverageMhz": ("avg", "kip:gpu:runtime_sm_clock_mhz", 1.0),
    "gpuMemoryClockAverageMhz": ("avg", "kip:gpu:runtime_memory_clock_mhz", 1.0),
    "gpuClockThrottleMaxMask": ("max", "kip:gpu:runtime_clock_throttle", 1.0),
    "gpuXidMaxCode": ("max", "kip:gpu:runtime_xid_errors", 1.0),
}
OPTIONAL_GPU_TELEMETRY = {
    "gpuPcieTxAverageBytesPerSecond": "kip:gpu:runtime_pcie_tx_bytes_per_second",
    "gpuPcieRxAverageBytesPerSecond": "kip:gpu:runtime_pcie_rx_bytes_per_second",
}

# Benchmark windows can be shorter than the two 15-second recording-rule
# stages. Query raw five-second DCGM samples and join the reviewed runtime
# identity directly, so a preceding idle period cannot be misreported as GPU
# activity inside a short measured cell. The dashboard rules remain unchanged.
RAW_GPU_GAUGE_EXPRESSIONS = {
    "gpuUtilizationAveragePct": "DCGM_FI_DEV_GPU_UTIL / 100",
    "gpuMemoryUsedAverageBytes": "DCGM_FI_DEV_FB_USED * 1024 * 1024",
    "powerAverageWatts": "DCGM_FI_DEV_POWER_USAGE",
    "temperatureMaxC": "DCGM_FI_DEV_GPU_TEMP",
    "gpuPcieTxAverageBytesPerSecond": "DCGM_FI_PROF_PCIE_TX_BYTES",
    "gpuPcieRxAverageBytesPerSecond": "DCGM_FI_PROF_PCIE_RX_BYTES",
    "gpuSmClockAverageMhz": "DCGM_FI_DEV_SM_CLOCK",
    "gpuMemoryClockAverageMhz": "DCGM_FI_DEV_MEM_CLOCK",
    "gpuClockThrottleMaxMask": "DCGM_FI_DEV_CLOCKS_EVENT_REASONS",
    "gpuXidMaxCode": "DCGM_FI_DEV_XID_ERRORS or (DCGM_FI_DEV_GPU_UTIL * 0)",
}

# Field name -> (raw monotonically increasing counter, unit scale).
EXACT_COUNTER_RATE_QUERIES = {
    "promptTokenRateAveragePerSecond": ("vllm:prompt_tokens_total", 1.0),
    "generatedTokenRateAveragePerSecond": ("vllm:generation_tokens_total", 1.0),
    "cachedTokenRateAveragePerSecond": ("vllm:prefix_cache_hits_total", 1.0),
}

# Field name -> (raw histogram bucket, quantile, unit scale).
EXACT_HISTOGRAM_QUERIES = {
    "serverTtftP50Ms": ("vllm:time_to_first_token_seconds_bucket", 0.50, 1000.0),
    "serverTtftP95Ms": ("vllm:time_to_first_token_seconds_bucket", 0.95, 1000.0),
    "serverTtftP99Ms": ("vllm:time_to_first_token_seconds_bucket", 0.99, 1000.0),
    "serverTpotP50Ms": (
        "vllm:request_time_per_output_token_seconds_bucket",
        0.50,
        1000.0,
    ),
    "serverTpotP95Ms": (
        "vllm:request_time_per_output_token_seconds_bucket",
        0.95,
        1000.0,
    ),
    "serverTpotP99Ms": (
        "vllm:request_time_per_output_token_seconds_bucket",
        0.99,
        1000.0,
    ),
    "serverE2eP50Ms": ("vllm:e2e_request_latency_seconds_bucket", 0.50, 1000.0),
    "serverE2eP95Ms": ("vllm:e2e_request_latency_seconds_bucket", 0.95, 1000.0),
    "serverE2eP99Ms": ("vllm:e2e_request_latency_seconds_bucket", 0.99, 1000.0),
}


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no UTC offset")
    return parsed.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _component_versions() -> dict[str, str]:
    """Read reviewed component versions without evaluating shell source files."""
    root = repository_root()
    parsed_files: dict[str, dict[str, str]] = {}
    result = {"aiperf": AIPERF_VERSION}
    for component, (relative_path, variable) in _COMPONENT_VERSION_SOURCES.items():
        if relative_path not in parsed_files:
            path = root / relative_path
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as error:
                raise BenchError(
                    f"cannot read component version manifest {path}: {error}"
                ) from error
            parsed_files[relative_path] = {
                match.group(1): match.group(2)
                for line in lines
                if (match := _ENV_ASSIGNMENT.fullmatch(line.strip()))
            }
        value = parsed_files[relative_path].get(variable)
        if not value or "${" in value:
            raise BenchError(f"component version manifest is missing a literal {variable}")
        result[component] = value
    return result


def _kubectl_json(arguments: list[str], description: str) -> dict[str, Any]:
    """Read one Kubernetes object without accepting caller-supplied identity data."""
    command = os.environ.get("KIP_KUBECTL", "kubectl")
    try:
        result = subprocess.run(
            [command, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as error:
        raise ExternalCommandError(f"required command not found: {command}") from error
    except subprocess.TimeoutExpired as error:
        raise ExternalCommandError(f"timed out reading {description}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostics"
        raise ExternalCommandError(f"cannot read {description}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ExternalCommandError(f"Kubernetes returned invalid JSON for {description}") from error
    if not isinstance(payload, dict):
        raise ExternalCommandError(f"Kubernetes returned a non-object for {description}")
    return payload


def _runtime_profile_document(profile_name: str) -> dict[str, Any]:
    path = repository_root() / "deploy" / "runtime" / "vllm" / "profiles" / f"{profile_name}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BenchError(
            f"cannot read committed runtime profile {profile_name}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise BenchError(f"committed runtime profile {profile_name} is not an object")
    return document


def _gpu_quantity(value: Any, description: str) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise BenchError(f"{description} is not a whole-GPU quantity")
    return int(value)


def _gpu_driver_version_from_nodes(nodes: dict[str, Any]) -> str:
    """Read the installed driver from GPU Feature Discovery, not the Operator pin.

    Preinstalled-driver hosts may legitimately run a different supported version
    from the driver that the Operator would install on a fresh host.
    """
    items = nodes.get("items")
    if not isinstance(items, list):
        raise BenchError("Kubernetes Node inventory is invalid")
    versions: set[str] = set()
    for node in items:
        if not isinstance(node, dict):
            raise BenchError("Kubernetes Node inventory contains a non-object")
        try:
            capacity = _gpu_quantity(
                node["status"]["capacity"].get("nvidia.com/gpu", "0"),
                "Node GPU capacity",
            )
            labels = node["metadata"]["labels"]
        except (KeyError, TypeError) as error:
            raise BenchError("Kubernetes Node is missing GPU driver identity fields") from error
        if capacity == 0:
            continue
        version = labels.get("nvidia.com/cuda.driver-version.full")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version):
            raise BenchError("GPU Node is missing a valid installed NVIDIA driver version")
        versions.add(version)
    if len(versions) != 1:
        raise BenchError("benchmark requires one homogeneous installed NVIDIA driver version")
    return next(iter(versions))


def _platform_identity_from_objects(
    suite: Suite,
    cost_profile: CostProfile,
    component_versions: dict[str, str],
    deployment: dict[str, Any],
    nodes: dict[str, Any],
) -> dict[str, Any]:
    """Validate and project the live runtime and GPU identity into the report contract."""
    target = suite.data["target"]
    profile_name = target["modelProfile"]
    profile_document = _runtime_profile_document(profile_name)
    model_identity = load_runtime_model_identity(profile_name)
    try:
        resource_name = profile_document["resourceName"]
        namespace = profile_document["kubernetes"]["namespace"]
        metadata = deployment["metadata"]
        annotations = metadata["annotations"]
        labels = metadata["labels"]
        pod_labels = deployment["spec"]["template"]["metadata"]["labels"]
        containers = deployment["spec"]["template"]["spec"]["containers"]
    except (KeyError, TypeError) as error:
        raise BenchError("live runtime Deployment is missing required identity metadata") from error
    if not isinstance(resource_name, str) or not isinstance(namespace, str):
        raise BenchError("committed runtime profile has an invalid Kubernetes identity")
    if metadata.get("name") != resource_name or metadata.get("namespace") != namespace:
        raise BenchError("live runtime Deployment does not match the committed resource identity")

    expected_profile_hash = config_hash(profile_document)
    expected_version = component_versions.get("vllm")
    expected_digest = DEFAULT_RUNTIME_IMAGE.partition("@")[2]
    expected_annotations = {
        "kube-inference-platform/runtime-version": expected_version,
        "kube-inference-platform/profile": profile_name,
        "kube-inference-platform/profile-sha256": expected_profile_hash,
        "kube-inference-platform/model-revision": model_identity.revision,
        "kube-inference-platform/image-digest": expected_digest,
        "kube-inference-platform.io/model-id": model_identity.repository,
    }
    for key, expected in expected_annotations.items():
        if not isinstance(expected, str) or annotations.get(key) != expected:
            raise BenchError(f"live runtime Deployment annotation drifted: {key}")

    expected_labels = {
        "app.kubernetes.io/managed-by": "kube-inference-platform",
        "kube-inference-platform.io/runtime": target["runtime"],
        "kube-inference-platform.io/model-profile": profile_name,
        "kube-inference-platform.io/served-model": target["servedModel"],
        "kube-inference-platform.io/model-revision": model_identity.revision,
    }
    for source_name, source in (("Deployment", labels), ("Pod template", pod_labels)):
        if not isinstance(source, dict):
            raise BenchError(f"live runtime {source_name} labels are invalid")
        for key, expected in expected_labels.items():
            if source.get(key) != expected:
                raise BenchError(f"live runtime {source_name} label drifted: {key}")

    if not isinstance(containers, list):
        raise BenchError("live runtime Deployment containers are invalid")
    runtime_containers = [
        container
        for container in containers
        if isinstance(container, dict) and container.get("name") == "vllm"
    ]
    if len(runtime_containers) != 1:
        raise BenchError("live runtime Deployment must contain exactly one vllm container")
    runtime_image = runtime_containers[0].get("image")
    if runtime_image != DEFAULT_RUNTIME_IMAGE:
        raise BenchError("live runtime image differs from the reviewed digest-pinned image")

    node_items = nodes.get("items")
    if not isinstance(node_items, list):
        raise BenchError("Kubernetes Node inventory is invalid")
    gpu_count = 0
    gpu_models: set[str] = set()
    for node in node_items:
        if not isinstance(node, dict):
            raise BenchError("Kubernetes Node inventory contains a non-object")
        try:
            capacity_value = node["status"]["capacity"].get("nvidia.com/gpu", "0")
            allocatable_value = node["status"]["allocatable"].get("nvidia.com/gpu", "0")
            node_labels = node["metadata"]["labels"]
        except (KeyError, TypeError) as error:
            raise BenchError("Kubernetes Node is missing GPU identity fields") from error
        capacity = _gpu_quantity(capacity_value, "Node GPU capacity")
        allocatable = _gpu_quantity(allocatable_value, "Node GPU allocatable capacity")
        if capacity == 0:
            if allocatable != 0:
                raise BenchError("Node advertises allocatable GPUs without GPU capacity")
            continue
        if allocatable != capacity:
            raise BenchError("Node GPU capacity and allocatable whole-GPU count differ")
        gpu_model = node_labels.get("nvidia.com/gpu.product")
        if not isinstance(gpu_model, str) or not gpu_model.strip():
            raise BenchError("GPU Node is missing the nvidia.com/gpu.product identity label")
        gpu_count += capacity
        gpu_models.add(gpu_model.strip())

    if gpu_count != int(cost_profile.data["physicalGpuCount"]):
        raise BenchError("live physical GPU count differs from the selected cost profile")
    if len(gpu_models) != 1:
        raise BenchError("v0.1 benchmark reporting requires one homogeneous GPU model")

    return {
        "runtimeVersion": annotations["kube-inference-platform/runtime-version"],
        "runtimeImage": runtime_image,
        "runtimeProfileSha256": annotations["kube-inference-platform/profile-sha256"],
        "modelProfile": labels["kube-inference-platform.io/model-profile"],
        "modelName": labels["kube-inference-platform.io/served-model"],
        "modelRevision": labels["kube-inference-platform.io/model-revision"],
        "gpuModel": next(iter(gpu_models)),
        "gpuCount": gpu_count,
    }


def _live_platform_identity(
    suite: Suite, cost_profile: CostProfile, component_versions: dict[str, str]
) -> dict[str, Any]:
    """Capture reproducibility identity from live Kubernetes state."""
    profile_document = _runtime_profile_document(suite.data["target"]["modelProfile"])
    try:
        namespace = profile_document["kubernetes"]["namespace"]
        resource_name = profile_document["resourceName"]
    except (KeyError, TypeError) as error:
        raise BenchError("committed runtime profile has no Kubernetes resource identity") from error
    deployment = _kubectl_json(
        ["-n", namespace, "get", "deployment", resource_name, "-o", "json"],
        f"runtime Deployment {namespace}/{resource_name}",
    )
    nodes = _kubectl_json(["get", "nodes", "-o", "json"], "GPU Node inventory")
    # The reviewed manifest pins the Operator-managed driver, but a valid
    # preinstalled-driver node may run another version. Freeze the actual
    # installed version in the report without changing the receipt schema.
    component_versions["nvidiaDriver"] = _gpu_driver_version_from_nodes(nodes)
    return _platform_identity_from_objects(
        suite, cost_profile, component_versions, deployment, nodes
    )


def _live_runtime_workload_identity(suite: Suite) -> dict[str, Any]:
    """Capture the serving Pod identity so restarts during a sweep are detectable."""
    profile_name = suite.data["target"]["modelProfile"]
    profile_document = _runtime_profile_document(profile_name)
    try:
        namespace = profile_document["kubernetes"]["namespace"]
    except (KeyError, TypeError) as error:
        raise BenchError("committed runtime profile has no Kubernetes namespace") from error
    selector = f"app.kubernetes.io/name=vllm,app.kubernetes.io/instance={profile_name}"
    pods = _kubectl_json(
        ["-n", namespace, "get", "pods", "-l", selector, "-o", "json"],
        f"runtime Pods in {namespace}",
    )
    items = pods.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise BenchError("benchmark requires exactly one live runtime Pod")
    pod = items[0]
    try:
        uid = pod["metadata"]["uid"]
        phase = pod["status"]["phase"]
        statuses = pod["status"]["containerStatuses"]
    except (KeyError, TypeError) as error:
        raise BenchError("runtime Pod is missing workload identity fields") from error
    if not isinstance(uid, str) or not uid or len(uid) > 128:
        raise BenchError("runtime Pod UID is invalid")
    if phase != "Running" or not isinstance(statuses, list):
        raise BenchError("runtime Pod is not running")
    runtime_statuses = [
        status for status in statuses if isinstance(status, dict) and status.get("name") == "vllm"
    ]
    if len(runtime_statuses) != 1:
        raise BenchError("runtime Pod must contain exactly one vllm container status")
    status = runtime_statuses[0]
    restart_count = status.get("restartCount")
    if isinstance(restart_count, bool) or not isinstance(restart_count, int) or restart_count < 0:
        raise BenchError("runtime Pod restart count is invalid")
    if status.get("ready") is not True:
        raise BenchError("runtime Pod container is not ready")
    return {"podUid": uid, "restartCount": restart_count}


def capture_benchmark_environment(suite: Suite, cost_profile: CostProfile) -> dict[str, Any]:
    """Freeze reviewed versions and live serving identity at one benchmark boundary."""
    versions = _component_versions()
    return {
        "componentVersions": versions,
        "platformIdentity": _live_platform_identity(suite, cost_profile, versions),
        "runtimeWorkload": _live_runtime_workload_identity(suite),
    }


def _numeric(value: Any, description: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchError(f"{description} is missing or non-numeric")
    return float(value)


def _metric_map(summary: dict[str, Any]) -> dict[str, Any]:
    records = summary.get("records", summary)
    if not isinstance(records, dict):
        raise BenchError("AIPerf summary has no metric mapping")
    return records


def _metric(summary: dict[str, Any], name: str) -> dict[str, Any]:
    value = _metric_map(summary).get(name)
    if not isinstance(value, dict):
        raise BenchError(f"AIPerf summary is missing required metric: {name}")
    return value


def _stat(summary: dict[str, Any], metric: str, stat: str) -> float:
    return _numeric(_metric(summary, metric).get(stat), f"AIPerf {metric}.{stat}")


def _counter_value(summary: dict[str, Any], metric: str) -> int:
    """Read an AIPerf aggregate counter.

    AIPerf 0.12 serializes counters such as ``total_isl`` and
    ``request_count`` in the ``avg`` field even though that value is already
    the aggregate total.  It does not add a sample ``count`` to summary-only
    exports, so treating ``avg`` as a per-request average silently breaks real
    artifacts.
    """
    value = _metric(summary, metric)
    for key in ("sum", "value", "avg"):
        candidate = value.get(key)
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return round(float(candidate))
    raise BenchError(f"AIPerf {metric} aggregate counter is not numeric")


def _request_count(summary: dict[str, Any], expected: int) -> tuple[int, int]:
    records = _metric_map(summary)
    if "request_count" in records:
        successful = _counter_value(summary, "request_count")
    else:
        successful = round(_numeric(_metric(summary, "request_latency").get("count"), "count"))
    error_metric = records.get("error_request_count")
    if isinstance(error_metric, dict):
        failed = _counter_value(summary, "error_request_count")
    elif error_metric is None:
        errors = summary.get("error_summary", records.get("error_summary", []))
        if isinstance(errors, list):
            failed = sum(
                round(float(error.get("count", 1)))
                for error in errors
                if isinstance(error, dict)
                and isinstance(error.get("count", 1), (int, float))
                and not isinstance(error.get("count", 1), bool)
            )
        else:
            failed = 0
        failed = max(failed, expected - successful)
    else:
        raise BenchError("AIPerf error_request_count has an invalid summary value")
    if successful < 1:
        raise BenchError("AIPerf trial has no successful requests")
    if successful + failed != expected:
        raise BenchError(
            "AIPerf request accounting does not match the suite "
            f"(successful={successful}, failed={failed}, expected={expected})"
        )
    return successful, failed


def _find_summary(directory: Path) -> dict[str, Any]:
    paths = sorted(directory.rglob("profile_export_aiperf.json"))
    if len(paths) != 1:
        raise BenchError(
            f"expected exactly one profile_export_aiperf.json under {directory}; found {len(paths)}"
        )
    try:
        value = json.loads(paths[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BenchError(f"cannot parse AIPerf summary {paths[0]}: {error}") from error
    if not isinstance(value, dict):
        raise BenchError(f"AIPerf summary is not a JSON object: {paths[0]}")
    return value


def _aiperf_window(
    summary: dict[str, Any],
    *,
    run_start: str,
    run_end: str,
    cell_start: str,
    cell_end: str,
    cell: str,
) -> tuple[str, str]:
    """Return AIPerf's request window, excluding Job startup and artifact copying.

    AIPerf 0.12 serializes naive datetimes. The benchmark Job fixes ``TZ=UTC``,
    so this boundary deliberately interprets only those AIPerf values as UTC.
    """
    start_value = summary.get("start_time")
    end_value = summary.get("end_time")
    if not isinstance(start_value, str) or not isinstance(end_value, str):
        raise BenchError(f"AIPerf summary for {cell} is missing start_time/end_time")

    def parse_aiperf(value: str) -> datetime:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).astimezone(UTC)

    try:
        start = parse_aiperf(start_value)
        end = parse_aiperf(end_value)
        outer_start = _parse_timestamp(run_start)
        outer_end = _parse_timestamp(run_end)
        orchestration_start = _parse_timestamp(cell_start)
        orchestration_end = _parse_timestamp(cell_end)
    except ValueError as error:
        raise BenchError(f"AIPerf summary for {cell} has an invalid UTC time window") from error
    if end <= start:
        raise BenchError(f"AIPerf summary for {cell} has a non-positive time window")
    if start < outer_start or end > outer_end:
        raise BenchError(f"AIPerf summary for {cell} time window escapes the orchestration run")
    if start < orchestration_start or end > orchestration_end:
        raise BenchError(f"AIPerf summary for {cell} time window escapes its Job boundary")
    return _timestamp(start), _timestamp(end)


def normalize_aiperf_summary(summary: dict[str, Any], expected_requests: int) -> dict[str, Any]:
    """Project an AIPerf aggregate onto the stable, non-sensitive report contract."""
    successful, failed = _request_count(summary, expected_requests)
    if summary.get("was_cancelled") is True:
        raise BenchError("AIPerf trial was cancelled")
    input_tokens = _counter_value(summary, "total_isl")
    output_tokens = _counter_value(summary, "total_osl")
    if input_tokens <= 0 or output_tokens <= 0:
        raise BenchError("AIPerf trial is missing positive input/output token totals")
    records = _metric_map(summary)
    input_config = summary.get("input_config", records.get("input_config"))
    try:
        streaming_enabled = input_config["endpoint"]["streaming"]
        if "phases" in input_config or "datasets" in input_config:
            phases = input_config["phases"]
            datasets = input_config["datasets"]
            if (
                not isinstance(phases, list)
                or len(phases) != 1
                or not isinstance(phases[0], dict)
                or phases[0].get("kind") != "profiling"
                or phases[0].get("type") != "concurrency"
                or not isinstance(datasets, list)
                or len(datasets) != 1
                or not isinstance(datasets[0], dict)
            ):
                raise TypeError
            configured_requests = phases[0]["requests"]
            configured_entries = datasets[0]["entries"]
        else:
            configured_requests = input_config["loadgen"]["request_count"]
            configured_entries = configured_requests
    except (KeyError, TypeError) as error:
        raise BenchError("AIPerf summary is missing its streaming input configuration") from error
    if (
        streaming_enabled is not True
        or configured_requests != expected_requests
        or configured_entries != expected_requests
    ):
        raise BenchError("AIPerf summary does not match the configured streaming request run")
    # Summary-only exports do not contain per-request records or a sample count
    # for these aggregates.  The strongest truthful evidence at this boundary
    # is the pinned streaming configuration plus finite TTFT/ITL percentiles on
    # a non-cancelled run.  The runtime smoke test remains responsible for SSE
    # frame and terminal [DONE] validation.
    for metric in ("time_to_first_token", "inter_token_latency"):
        for stat in ("p50", "p95", "p99"):
            if _stat(summary, metric, stat) <= 0:
                raise BenchError(f"AIPerf {metric}.{stat} lacks positive streaming timing evidence")
    return {
        "requests": expected_requests,
        "successfulRequests": successful,
        "failedRequests": failed,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "metrics": {
            "ttftMs": {
                stat: _stat(summary, "time_to_first_token", stat) for stat in ("p50", "p95", "p99")
            },
            "itlMs": {
                stat: _stat(summary, "inter_token_latency", stat) for stat in ("p50", "p95", "p99")
            },
            "e2eMs": {
                stat: _stat(summary, "request_latency", stat) for stat in ("p50", "p95", "p99")
            },
            "requestThroughputPerSecond": _stat(summary, "request_throughput", "avg"),
            "outputTokenThroughputPerSecond": _stat(summary, "output_token_throughput", "avg"),
            # AIPerf's summary-only export proves that streaming mode produced
            # aggregate TTFT and ITL timing. It does not retain per-request SSE
            # frames and therefore cannot prove terminal [DONE].
            "streamingTimingEvidence": True,
        },
    }


def _series_values(
    result: list[dict[str, Any]],
    description: str,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
) -> list[float]:
    """Return finite samples, optionally clipped back to an exact UTC window."""
    start_epoch = _parse_timestamp(window_start).timestamp() if window_start else None
    end_epoch = _parse_timestamp(window_end).timestamp() if window_end else None
    values = []
    for series in result:
        if not isinstance(series, dict) or not isinstance(series.get("values"), list):
            continue
        for sample in series["values"]:
            if not isinstance(sample, list) or len(sample) != 2:
                continue
            try:
                sampled_at = float(sample[0])
                value = float(sample[1])
            except (TypeError, ValueError):
                continue
            if start_epoch is not None and sampled_at < start_epoch:
                continue
            if end_epoch is not None and sampled_at > end_epoch:
                continue
            if value == value and value not in {float("inf"), float("-inf")}:
                values.append(value)
    if not values:
        raise BenchError(f"Prometheus returned no usable samples for {description}")
    return values


def collect_telemetry(
    prometheus: PrometheusClient,
    *,
    start: str,
    end: str,
    context_seconds: int,
    model_name: str,
    runtime: str,
    model_profile: str,
) -> dict[str, float | None]:
    """Collect state samples and exact-window counter/histogram statistics."""
    query_start = _timestamp(_parse_timestamp(start) - timedelta(seconds=context_seconds))
    query_end = _timestamp(_parse_timestamp(end) + timedelta(seconds=context_seconds))
    delta = _parse_timestamp(end) - _parse_timestamp(start)
    duration_microseconds = delta // timedelta(microseconds=1)
    if duration_microseconds < 1000:
        raise BenchError("benchmark trial telemetry window must be at least one millisecond")

    # PromQL range selectors have millisecond resolution. Flooring (rather than
    # rounding up) guarantees that the selected range starts inside this trial,
    # never in the preceding warmup/cooldown/cell. At most 999 microseconds of
    # this trial can be omitted when AIPerf emits a sub-millisecond boundary.
    duration_milliseconds = duration_microseconds // 1000
    duration_seconds = duration_milliseconds / 1000
    range_selector = f"{duration_milliseconds}ms"
    # Prometheus serializes query timestamps at millisecond precision.  Floor
    # the evaluation instant to that same precision so `_series_values` does
    # not reject a valid sample that is a few microseconds before the original
    # AIPerf boundary after Prometheus has truncated it.
    end_timestamp = _parse_timestamp(end)
    evaluation_end = _timestamp(
        end_timestamp.replace(microsecond=(end_timestamp.microsecond // 1000) * 1000)
    )
    telemetry = {}
    workload_selector = ",".join(
        (
            f"model_name={json.dumps(model_name)}",
            f"runtime={json.dumps(runtime)}",
            f"model_profile={json.dumps(model_profile)}",
        )
    )
    for field, (aggregation, metric, scale) in GAUGE_TELEMETRY_QUERIES.items():
        if field in RAW_GPU_GAUGE_EXPRESSIONS:
            raw = RAW_GPU_GAUGE_EXPRESSIONS[field]
            expression = (
                f"{aggregation}((({raw}) * on (cluster,namespace,node,pod) "
                "group_left (runtime,model_profile,model_name) "
                f"kip:runtime:pod_identity{{{workload_selector}}}))"
            )
        else:
            expression = f"{aggregation}({metric}{{{workload_selector}}})"
        series = prometheus.query_range(expression, start=query_start, end=query_end, step="5s")
        try:
            values = _series_values(
                series,
                expression,
                window_start=start,
                window_end=end,
            )
        except BenchError as error:
            if field not in OPTIONAL_GPU_TELEMETRY or "no usable samples" not in str(error):
                raise
            telemetry[field] = None
            continue
        reduced = max(values) if aggregation == "max" else sum(values) / len(values)
        telemetry[field] = reduced * scale

    # Evaluate counter deltas once, exactly at the trial end. Unlike the
    # dashboard-oriented ``rate(...[5m])`` rules, these expressions cannot
    # include activity from neighboring cells.
    for field, (metric, scale) in EXACT_COUNTER_RATE_QUERIES.items():
        expression = (
            f"sum(increase({metric}{{{workload_selector}}}[{range_selector}]))"
            f" / {duration_seconds:g}"
        )
        values = _series_values(
            prometheus.query_range(expression, start=evaluation_end, end=evaluation_end, step="1s"),
            expression,
            window_start=evaluation_end,
            window_end=evaluation_end,
        )
        telemetry[field] = (sum(values) / len(values)) * scale

    # Build each percentile from bucket increases over this trial, rather than
    # averaging continuously evaluated five-minute percentiles (which is both
    # statistically invalid and contaminated by neighboring traffic).
    for field, (metric, quantile, scale) in EXACT_HISTOGRAM_QUERIES.items():
        expression = (
            f"histogram_quantile({quantile:g}, "
            f"sum by (le) (increase({metric}{{{workload_selector}}}"
            f"[{range_selector}])))"
        )
        values = _series_values(
            prometheus.query_range(expression, start=evaluation_end, end=evaluation_end, step="1s"),
            expression,
            window_start=evaluation_end,
            window_end=evaluation_end,
        )
        telemetry[field] = (sum(values) / len(values)) * scale
    return telemetry


def _cost_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data", payload)
    entries: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("totalCost"), (int, float)):
                entries.append(value)
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(data)
    if not entries:
        raise BenchError("OpenCost response contains no totalCost entries")
    return entries


def _sum_cost(payload: dict[str, Any], key: str = "totalCost") -> float:
    entries = _cost_entries(payload)
    values = [entry.get(key) for entry in entries]
    if any(not isinstance(value, (int, float)) for value in values):
        raise BenchError(f"OpenCost response is missing numeric {key} attribution")
    return sum(float(value) for value in values)


def collect_trial_cost(
    opencost: OpenCostClient,
    *,
    start: str,
    end: str,
    energy_kwh: float,
    model_name: str,
    node_hourly_rate_usd: float,
) -> dict[str, float]:
    """Audit exact elapsed-node cost and attribute it with OpenCost shares.

    OpenCost's minute-resolution short-window totals can materially undercount
    a sub-minute trial. The reviewed node rate prices exact elapsed time;
    OpenCost determines model, active-usage, and token-phase proportions.
    """
    if node_hourly_rate_usd <= 0:
        raise BenchError("the reviewed whole-node hourly rate must be positive")
    elapsed_seconds = (_parse_timestamp(end) - _parse_timestamp(start)).total_seconds()
    if elapsed_seconds <= 0:
        raise BenchError("the cost trial window must have positive duration")
    node_allocation = opencost.allocation(start=start, end=end, share_idle=True)
    allocated = opencost.inference_cost(
        start=start, end=end, cost_basis="allocation", model_name=model_name
    )
    usage = opencost.inference_cost(start=start, end=end, cost_basis="usage", model_name=model_name)
    node_cost = _sum_cost(node_allocation)
    model_cost = _sum_cost(allocated)
    usage_cost = _sum_cost(usage)
    input_cost = _sum_cost(allocated, "inputCost")
    output_cost = _sum_cost(allocated, "outputCost")
    if node_cost <= 0 or model_cost <= 0 or usage_cost <= 0:
        raise BenchError("OpenCost trial allocation or inference usage is not positive")
    if model_cost > node_cost * 1.05:
        raise BenchError("OpenCost model allocation exceeds whole-node allocation")
    phase_cost = input_cost + output_cost
    if input_cost <= 0 or output_cost <= 0 or abs(phase_cost - model_cost) > model_cost * 0.05:
        raise BenchError("OpenCost trial token-phase costs do not reconcile with model allocation")
    audited_model_cost = (
        node_hourly_rate_usd * elapsed_seconds / 3600 * min(model_cost / node_cost, 1.0)
    )
    return {
        "allocatedUsd": audited_model_cost,
        "usageUsd": audited_model_cost * usage_cost / model_cost,
        "inputUsd": audited_model_cost * input_cost / phase_cost,
        "outputUsd": audited_model_cost * output_cost / phase_cost,
        "energyKwh": energy_kwh,
    }


def _wait_for_observability_convergence(
    prometheus: PrometheusClient,
    opencost: OpenCostClient,
    receipt: dict[str, Any],
    model_name: str,
    *,
    timeout_seconds: float = 180,
    poll_seconds: float = 10,
) -> None:
    """Wait until Prometheus and OpenCost cover the completed experiment window."""
    ended_at = receipt.get("endedAt")
    if not isinstance(ended_at, str):
        raise BenchError("benchmark receipt is missing endedAt for telemetry convergence")
    try:
        ended_epoch = _parse_timestamp(ended_at).timestamp()
    except (TypeError, ValueError) as error:
        raise BenchError("benchmark receipt endedAt is not a valid UTC timestamp") from error
    if timeout_seconds < 0 or poll_seconds <= 0:
        raise ValueError("observability convergence timing must be non-negative")

    deadline = time.monotonic() + timeout_seconds
    last_error: BenchError | None = None
    model_selector = f"model_name={json.dumps(model_name)}"
    while True:
        try:
            query_end = _timestamp(max(datetime.now(UTC), _parse_timestamp(ended_at)))
            freshness = _series_values(
                prometheus.query_range(
                    f"max(timestamp(vllm:generation_tokens_total{{{model_selector}}}))",
                    start=ended_at,
                    end=query_end,
                    step="5s",
                ),
                "post-benchmark vLLM scrape freshness",
            )
            if max(freshness) < ended_epoch:
                raise BenchError("Prometheus has not scraped vLLM through the run end")

            allocation = opencost.allocation(
                start=receipt["startedAt"],
                end=ended_at,
                share_idle=True,
            )
            inference = opencost.inference_cost(
                start=receipt["startedAt"],
                end=ended_at,
                cost_basis="allocation",
                model_name=model_name,
            )
            if _sum_cost(allocation) <= 0:
                raise BenchError("OpenCost allocation has not converged to a positive cost")
            if (
                _sum_cost(inference) <= 0
                or _sum_cost(inference, "inputCost") <= 0
                or _sum_cost(inference, "outputCost") <= 0
            ):
                raise BenchError("OpenCost inference token attribution has not converged")
            return
        except BenchError as error:
            last_error = error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            detail = str(last_error) if last_error else "unknown convergence failure"
            raise ExternalCommandError(
                f"observability data did not converge within {timeout_seconds:g}s: {detail}"
            ) from last_error
        time.sleep(min(poll_seconds, remaining))


class _PortForward:
    def __init__(self, service: str, local_port: int, remote_port: int) -> None:
        self.service = service
        self.local_port = local_port
        self.remote_port = remote_port
        self.process: subprocess.Popen[str] | None = None

    def __enter__(self) -> str:
        command = os.environ.get("KIP_KUBECTL", "kubectl")
        try:
            self.process = subprocess.Popen(
                [
                    command,
                    "-n",
                    OBSERVABILITY_NAMESPACE,
                    "port-forward",
                    f"service/{self.service}",
                    f"{self.local_port}:{self.remote_port}",
                    "--address=127.0.0.1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as error:
            raise ExternalCommandError(f"required command not found: {command}") from error
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stderr = self.process.stderr.read() if self.process.stderr else ""
                raise ExternalCommandError(
                    f"port-forward for {self.service} exited early: {stderr.strip()}"
                )
            try:
                with socket.create_connection(("127.0.0.1", self.local_port), timeout=0.2):
                    return f"http://127.0.0.1:{self.local_port}"
            except OSError:
                time.sleep(0.1)
        self.__exit__(None, None, None)
        raise ExternalCommandError(f"timed out starting port-forward for {self.service}")

    def __exit__(self, *_: object) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def _grafana_credentials() -> tuple[str, str]:
    """Read Grafana credentials into process memory without printing or persisting them."""
    command = os.environ.get("KIP_KUBECTL", "kubectl")
    try:
        result = subprocess.run(
            [
                command,
                "-n",
                OBSERVABILITY_NAMESPACE,
                "get",
                "secret",
                GRAFANA_ADMIN_SECRET,
                "-o",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError as error:
        raise ExternalCommandError(f"required command not found: {command}") from error
    except subprocess.TimeoutExpired as error:
        raise ExternalCommandError("timed out reading the Grafana credential Secret") from error
    if result.returncode != 0:
        raise ExternalCommandError("cannot read the managed Grafana credential Secret")
    try:
        payload = json.loads(result.stdout)
        data = payload["data"]
        username = base64.b64decode(data["admin-user"], validate=True).decode("utf-8")
        password = base64.b64decode(data["admin-password"], validate=True).decode("utf-8")
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, binascii.Error) as error:
        raise BenchError("managed Grafana credential Secret is invalid") from error
    if not username or not password:
        raise BenchError("managed Grafana credential Secret contains an empty value")
    return username, password


def record_grafana_annotation(
    receipt: dict[str, Any],
    suite: Suite,
    profile: CostProfile,
    *,
    client: GrafanaClient | None = None,
) -> int:
    """Record the completed benchmark as an exact-window Grafana region."""
    start = _parse_timestamp(receipt["startedAt"])
    end = _parse_timestamp(receipt["endedAt"])
    if end <= start:
        raise BenchError("cannot annotate a non-positive benchmark window")
    tags = [
        "kip-benchmark",
        f"run:{receipt['runId']}",
        f"suite:{suite.name}",
        f"profile:{suite.data['target']['modelProfile']}",
        f"cost-profile:{profile.name}",
    ]
    text = f"KIP benchmark {receipt['runId']} ({suite.name})"

    def create(grafana: GrafanaClient) -> int:
        return grafana.create_region_annotation(
            start_milliseconds=int(start.timestamp() * 1000),
            end_milliseconds=int(end.timestamp() * 1000),
            tags=tags,
            text=text,
        )

    if client is not None:
        return create(client)
    username, password = _grafana_credentials()
    with ExitStack() as stack:
        grafana_url = os.environ.get("KIP_GRAFANA_URL")
        if not grafana_url:
            grafana_url = stack.enter_context(_PortForward(GRAFANA_SERVICE, 13000, 80))
        return create(GrafanaClient(grafana_url, username, password))


def record_grafana_start_annotation(
    run_id: str,
    started_at: str,
    suite: Suite,
    profile: CostProfile,
    *,
    client: GrafanaClient | None = None,
) -> int:
    """Mark the exact experiment start before the first AIPerf Job is created."""
    started = _parse_timestamp(started_at)
    tags = [
        "kip-benchmark",
        "benchmark-start",
        f"run:{run_id}",
        f"suite:{suite.name}",
        f"profile:{suite.data['target']['modelProfile']}",
        f"cost-profile:{profile.name}",
    ]
    text = f"KIP benchmark {run_id} started ({suite.name})"

    def create(grafana: GrafanaClient) -> int:
        return grafana.create_point_annotation(
            timestamp_milliseconds=int(started.timestamp() * 1000), tags=tags, text=text
        )

    if client is not None:
        return create(client)
    username, password = _grafana_credentials()
    with ExitStack() as stack:
        grafana_url = os.environ.get("KIP_GRAFANA_URL")
        if not grafana_url:
            grafana_url = stack.enter_context(_PortForward(GRAFANA_SERVICE, 13000, 80))
        return create(GrafanaClient(grafana_url, username, password))


@contextmanager
def observability_clients() -> Iterator[tuple[PrometheusClient, OpenCostClient]]:
    """Use explicit endpoints or bounded, loopback-only kubectl port-forwards."""
    with ExitStack() as stack:
        prometheus_url = os.environ.get("KIP_PROMETHEUS_URL")
        opencost_url = os.environ.get("KIP_OPENCOST_URL")
        if not prometheus_url:
            prometheus_url = stack.enter_context(_PortForward(PROMETHEUS_SERVICE, 19090, 9090))
        if not opencost_url:
            opencost_url = stack.enter_context(_PortForward(OPENCOST_SERVICE, 19003, 9003))
        yield PrometheusClient(prometheus_url), OpenCostClient(opencost_url)


def normalize_run(
    suite: Suite,
    profile: CostProfile,
    receipt: dict[str, Any],
    work_directory: Path,
    *,
    prometheus: PrometheusClient,
    opencost: OpenCostClient,
) -> dict[str, Any]:
    """Normalize collected artifacts and exact telemetry/cost windows."""
    repository = receipt.get("repository")
    if not isinstance(repository, dict):
        raise BenchError("run receipt is missing the frozen repository identity")
    revision = repository.get("revision")
    tree_state = repository.get("treeState")
    workspace_sha256 = repository.get("workspaceSha256")
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise BenchError("run receipt contains an invalid Git revision")
    if not isinstance(tree_state, str) or tree_state not in {"clean", "dirty"}:
        raise BenchError("run receipt contains an invalid Git tree state")
    if not isinstance(workspace_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", workspace_sha256):
        raise BenchError("run receipt contains an invalid workspace digest")
    environment = receipt.get("environment")
    if not isinstance(environment, dict) or set(environment) != {
        "componentVersions",
        "platformIdentity",
        "runtimeWorkload",
    }:
        raise BenchError("run receipt is missing the frozen benchmark environment")
    component_versions = environment["componentVersions"]
    platform_identity = environment["platformIdentity"]
    runtime_workload = environment["runtimeWorkload"]
    if not isinstance(component_versions, dict) or set(component_versions) != set(
        _COMPONENT_VERSION_SOURCES
    ) | {"aiperf"}:
        raise BenchError("run receipt contains invalid component versions")
    if any(not isinstance(value, str) or not value for value in component_versions.values()):
        raise BenchError("run receipt contains an empty component version")
    if not isinstance(platform_identity, dict) or set(platform_identity) != {
        "runtimeVersion",
        "runtimeImage",
        "runtimeProfileSha256",
        "modelProfile",
        "modelName",
        "modelRevision",
        "gpuModel",
        "gpuCount",
    }:
        raise BenchError("run receipt contains an invalid platform identity")
    if not isinstance(runtime_workload, dict) or set(runtime_workload) != {
        "podUid",
        "restartCount",
    }:
        raise BenchError("run receipt contains an invalid runtime workload identity")
    model_identity = load_runtime_model_identity(suite.data["target"]["modelProfile"])
    expected_platform_values = {
        "runtimeVersion": component_versions["vllm"],
        "runtimeImage": DEFAULT_RUNTIME_IMAGE,
        "modelProfile": suite.data["target"]["modelProfile"],
        "modelName": suite.data["target"]["servedModel"],
        "modelRevision": model_identity.revision,
        "gpuCount": int(profile.data["physicalGpuCount"]),
    }
    for key, expected_value in expected_platform_values.items():
        if platform_identity.get(key) != expected_value:
            raise BenchError(f"run receipt platform identity drifted: {key}")
    profile_sha = platform_identity.get("runtimeProfileSha256")
    if not isinstance(profile_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", profile_sha):
        raise BenchError("run receipt contains an invalid runtime profile digest")
    expected_profile_sha = config_hash(
        _runtime_profile_document(suite.data["target"]["modelProfile"])
    )
    if profile_sha != expected_profile_sha:
        raise BenchError("run receipt platform identity drifted: runtimeProfileSha256")
    gpu_model = platform_identity.get("gpuModel")
    if not isinstance(gpu_model, str) or not gpu_model.strip():
        raise BenchError("run receipt contains an invalid GPU model")
    pod_uid = runtime_workload["podUid"]
    restart_count = runtime_workload["restartCount"]
    if not isinstance(pod_uid, str) or not pod_uid or len(pod_uid) > 128:
        raise BenchError("run receipt contains an invalid runtime Pod UID")
    if isinstance(restart_count, bool) or not isinstance(restart_count, int) or restart_count < 0:
        raise BenchError("run receipt contains an invalid runtime restart count")
    timeline = [
        item
        for item in receipt["timeline"]
        if item.get("status") == "succeeded" and not item.get("warmup")
    ]
    expected = len(suite.data["sweep"]["concurrency"]) * suite.data["sweep"]["trialsPerCell"]
    if len(timeline) != expected:
        raise BenchError(f"run has {len(timeline)} measured cells; expected {expected}")
    context = suite.data["telemetry"]["prometheusContextSeconds"]
    trials = []
    for cell in timeline:
        summary = _find_summary(work_directory / "aiperf" / cell["cell"])
        trial_start, trial_end = _aiperf_window(
            summary,
            run_start=receipt["startedAt"],
            run_end=receipt["endedAt"],
            cell_start=cell["startedAt"],
            cell_end=cell["endedAt"],
            cell=cell["cell"],
        )
        normalized = normalize_aiperf_summary(summary, suite.data["sweep"]["requestsPerCell"])
        telemetry = collect_telemetry(
            prometheus,
            start=trial_start,
            end=trial_end,
            context_seconds=context,
            model_name=suite.data["target"]["servedModel"],
            runtime=suite.data["target"]["runtime"],
            model_profile=suite.data["target"]["modelProfile"],
        )
        duration = (_parse_timestamp(trial_end) - _parse_timestamp(trial_start)).total_seconds()
        energy_kwh = telemetry["powerAverageWatts"] * duration / 3_600_000
        cost = collect_trial_cost(
            opencost,
            start=trial_start,
            end=trial_end,
            energy_kwh=energy_kwh,
            model_name=suite.data["target"]["servedModel"],
            node_hourly_rate_usd=profile.node_hourly_rate,
        )
        trials.append(
            {
                "concurrency": cell["concurrency"],
                "trial": cell["trial"],
                "startedAt": trial_start,
                "endedAt": trial_end,
                **normalized,
                "telemetry": telemetry,
                "cost": cost,
            }
        )

    allocation = opencost.allocation(
        start=receipt["startedAt"],
        end=receipt["endedAt"],
        share_idle=True,
    )
    allocated_inference = opencost.inference_cost(
        start=receipt["startedAt"],
        end=receipt["endedAt"],
        cost_basis="allocation",
        model_name=suite.data["target"]["servedModel"],
    )
    usage_inference = opencost.inference_cost(
        start=receipt["startedAt"],
        end=receipt["endedAt"],
        cost_basis="usage",
        model_name=suite.data["target"]["servedModel"],
    )
    embedded_profile = dict(profile.data)
    embedded_profile["sha256"] = profile.sha256
    failed_requests = sum(int(trial["failedRequests"]) for trial in trials)
    warnings = (
        [f"AIPerf recorded {failed_requests} failed measured requests"] if failed_requests else []
    )
    missing_optional = sorted(
        {
            metric
            for field, metric in OPTIONAL_GPU_TELEMETRY.items()
            if any(trial["telemetry"][field] is None for trial in trials)
        }
    )
    if missing_optional:
        warnings.append(
            "GPU PCIe throughput profiling was not exposed by DCGM for one or more "
            "trial windows; PCIe report values are N/A, not zero."
        )
    resume_history = receipt.get("resumeHistory", [])
    if resume_history:
        warnings.append(
            "The benchmark was resumed after an orchestrator interruption; its frozen "
            "execution plan, completed artifacts, and platform identity were revalidated."
        )
    return {
        "schemaVersion": 1,
        "run": {
            "id": receipt["runId"],
            "startedAt": receipt["startedAt"],
            "endedAt": receipt["endedAt"],
            "gitRevision": revision,
            "gitTreeState": tree_state,
            "gitWorkspaceSha256": workspace_sha256,
            "aiperfVersion": receipt.get("aiperfVersion", AIPERF_VERSION),
            "aiperfImage": receipt.get("aiperfImage", AIPERF_IMAGE),
            "componentVersions": component_versions,
            "runtime": suite.data["target"]["runtime"],
            "runtimePodUid": pod_uid,
            "runtimePodRestartCount": restart_count,
            **platform_identity,
            "grafanaBaseUrl": "http://localhost:3000",
        },
        "suite": {
            "name": suite.name,
            "sha256": suite.sha256,
            "manifest": suite.data,
            "concurrency": suite.data["sweep"]["concurrency"],
            "requestsPerCell": suite.data["sweep"]["requestsPerCell"],
            "trialsPerCell": suite.data["sweep"]["trialsPerCell"],
            "confidenceLevel": suite.data["sweep"]["confidenceLevel"],
        },
        "costProfile": embedded_profile,
        "trials": trials,
        "opencost": {
            "windowStart": receipt["startedAt"],
            "windowEnd": receipt["endedAt"],
            "queriedAt": _timestamp(datetime.now(UTC)),
            "allocatedUsd": _sum_cost(allocation),
            "usageUsd": _sum_cost(usage_inference),
            "inputUsd": _sum_cost(allocated_inference, "inputCost"),
            "outputUsd": _sum_cost(allocated_inference, "outputCost"),
        },
        "prometheus": {
            "stepSeconds": 5,
            "contextSeconds": context,
            "seriesComplete": True,
        },
        "dataQuality": {
            "status": "warning" if warnings else "complete",
            "warnings": warnings,
            "missingSeries": missing_optional,
        },
    }


def complete_and_publish_run(
    suite: Suite,
    profile: CostProfile,
    receipt: dict[str, Any],
    work_directory: Path,
) -> dict[str, Any]:
    """Complete the benchmark-to-report path or fail without publishing partial output."""
    with observability_clients() as (prometheus, opencost):
        _wait_for_observability_convergence(
            prometheus,
            opencost,
            receipt,
            suite.data["target"]["servedModel"],
        )
        normalized = normalize_run(
            suite,
            profile,
            receipt,
            work_directory,
            prometheus=prometheus,
            opencost=opencost,
        )
    validate_normalized_input(normalized, run_id=receipt["runId"], cost_profile=profile)
    annotation_id = record_grafana_annotation(receipt, suite, profile)
    normalized_path = work_directory / "normalized.json"
    normalized_path.write_text(
        json.dumps(normalized, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    report_path = generate_report(
        normalized,
        run_id=receipt["runId"],
        cost_profile=profile,
        output=reports_root() / receipt["runId"],
    )
    return {
        "normalizedInput": str(normalized_path),
        "report": str(report_path),
        "grafanaAnnotationId": annotation_id,
    }
