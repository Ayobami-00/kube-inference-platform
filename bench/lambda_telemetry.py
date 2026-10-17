"""Capture exact-window, aggregate-only Prometheus evidence for nominal trials.

The output contains no request payloads or metric label values from Prometheus.
It is an additive, sanitized companion to an already-published trial report.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from .lambda_nominal_report import ROOT, RUN_ID

PROM_URL = re.compile(r"http://10\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}:9090\Z")
LARGE_POOL = "kip-qwen3-next"
SMALL_POOL = "kip-qwen3-8b"
LARGE_MODEL = "qwen3-next-80b-a3b-instruct"


def queries(window_seconds: int, large_node: str) -> dict[str, str]:
    window = f"{window_seconds}s"
    result = {}
    for name, pool, priority in (
        ("smallInteractiveQueueP95Ms", SMALL_POOL, "0"),
        ("largeInteractiveQueueP95Ms", LARGE_POOL, "0"),
        ("largeBatchQueueP95Ms", LARGE_POOL, "-10"),
    ):
        result[name] = (
            "1000 * histogram_quantile(0.95, sum by (le) "
            "(increase(llm_d_epp_flow_control_request_queue_duration_seconds_bucket"
            f'{{inference_pool="{pool}",priority="{priority}",outcome="Dispatched"}}'
            f"[{window}])))"
        )
    for name, histogram in (
        ("largeVllmQueueP95Ms", "request_queue_time_seconds"),
        ("largeVllmPrefillP95Ms", "request_prefill_time_seconds"),
        ("largeVllmTtftP95Ms", "time_to_first_token_seconds"),
    ):
        result[name] = (
            "1000 * histogram_quantile(0.95, sum by (le) "
            f'(increase(vllm:{histogram}_bucket{{model_name="{LARGE_MODEL}"}}'
            f"[{window}])))"
        )
    result["largeNodeGpuUtilMeanPercent"] = (
        f'avg(avg_over_time(DCGM_FI_DEV_GPU_UTIL{{node="{large_node}"}}[{window}]))'
    )
    return result


def query_scalar(base: str, expression: str, at_seconds: int) -> float:
    if not PROM_URL.fullmatch(base):
        raise ValueError("Prometheus must use the private reviewed ClusterIP service")
    url = (
        base + "/api/v1/query?" + urllib.parse.urlencode({"query": expression, "time": at_seconds})
    )
    with urllib.request.urlopen(url, timeout=30) as response:
        if response.status != 200:
            raise ValueError("Prometheus exact-window query returned a non-200 response")
        data = json.load(response)
    rows = (data.get("data") or {}).get("result") or []
    if data.get("status") != "success" or len(rows) != 1:
        raise ValueError("Prometheus exact-window metric is missing or ambiguous")
    value = float(rows[0]["value"][1])
    if not math.isfinite(value) or value < 0:
        raise ValueError("Prometheus exact-window metric is non-finite or negative")
    return round(value, 6)


def capture(run_id: str, base: str, root: Path = ROOT) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("telemetry run ID is not reviewed")
    folder = root / "benchmarks/reports" / run_id
    summary_path = folder / "summary.json"
    output = folder / "telemetry.json"
    if folder.is_symlink() or summary_path.is_symlink() or output.is_symlink():
        raise ValueError("telemetry report path is unsafe")
    if output.exists():
        raise ValueError("telemetry snapshot is already published")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("runId") != run_id or summary.get("schemaVersion") != 1:
        raise ValueError("telemetry report is not a reviewed nominal trial")
    start = datetime.fromisoformat(summary["startUtc"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(summary["endUtc"].replace("Z", "+00:00"))
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("telemetry trial window is invalid")
    start_seconds = math.floor(start.timestamp())
    end_seconds = math.ceil(end.timestamp())
    window_seconds = end_seconds - start_seconds
    if not 300 <= window_seconds <= 900:
        raise ValueError("telemetry trial duration is outside the reviewed nominal range")
    large_nodes = [node for node in summary["cost"]["nodeHourlyUsd"] if node.endswith("-large")]
    if len(large_nodes) != 1 or not re.fullmatch(r"kip-lambda-[0-9a-f]{8}-large", large_nodes[0]):
        raise ValueError("telemetry large GPU node is not task-owned")
    values = {
        name: query_scalar(base, expression, end_seconds)
        for name, expression in queries(window_seconds, large_nodes[0]).items()
    }
    payload = {
        "schemaVersion": 1,
        "runId": run_id,
        "variant": summary["variant"],
        "trial": summary["trial"],
        "windowStartUtc": datetime.fromtimestamp(start_seconds, UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "windowEndUtc": datetime.fromtimestamp(end_seconds, UTC).isoformat().replace("+00:00", "Z"),
        "windowPrecisionSeconds": 1,
        "metrics": values,
        "dataQuality": [
            "Prometheus histograms use increase over the whole-second envelope "
            "of the request window.",
            "Endpoint-picker queue P95 includes dispatched requests only; "
            "rejections are counted in summary.json.",
            "Large-node GPU utilization includes all eight GPUs and both "
            "co-located model Deployments.",
        ],
    }
    output.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish sanitized Lambda trial telemetry")
    parser.add_argument("run_id")
    parser.add_argument("--prometheus-url", required=True)
    args = parser.parse_args()
    try:
        print(capture(args.run_id, args.prometheus_url))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Lambda telemetry failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
