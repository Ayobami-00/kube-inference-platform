"""Fail-closed, content-free accounting across AIPerf, Envoy, and llm-d.

The raw records and access logs remain local and ignored. Only aggregate counts
and worker names leave this module; no request IDs, addresses, or payloads do.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bench.pilot_accounting import reconcile_events
from bench.workloads import CLASSES, TENANTS

from .aiperf_text import RUN_ID_PATTERN as RUN_ID
from .aiperf_text import stream_outcomes
from .vast import FleetError

EPP_EVENTS = {
    "EPP received request",
    "EPP sent request body response(s) to proxy",
    "EPP sent response body back to proxy",
}
EPP_REJECT_EVENTS = {
    "EPP received request",
    "Error handling request",
    "Failed to process request",
}


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _nanoseconds(value: Any) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FleetError("AIPerf request timestamp is missing")
    return datetime.fromtimestamp(value / 1_000_000_000, UTC)


def _datetime(value: Any) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value) or value <= 0:
            raise FleetError("Gateway or EPP timestamp is invalid")
        return datetime.fromtimestamp(value, UTC)
    if not isinstance(value, str):
        raise FleetError("Gateway or EPP timestamp is missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise FleetError("Gateway or EPP timestamp is malformed") from error
    if result.utcoffset() is None or result.utcoffset() != timedelta(0):
        raise FleetError("Gateway or EPP timestamp is not UTC")
    return result


def summarize_evidence(
    records: list[dict[str, Any]],
    done_ids: set[str],
    gateway: list[dict[str, Any]],
    epp: list[dict[str, Any]],
    pod_nodes: dict[str, str],
    *,
    tenant: str,
    workload_class: str,
    statuses: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Join sessions by AIPerf correlation and turns by causal ordering/time.

    Envoy rewrites client X-Request-ID. Its own X-Request-ID is used only for
    the Gateway-to-EPP join; the AIPerf correlation ID joins client to Gateway.
    """
    if tenant not in TENANTS or workload_class not in CLASSES or not records:
        raise FleetError("pilot accounting identity or records are invalid")
    client: dict[str, list[dict[str, Any]]] = defaultdict(list)
    client_ids: set[str] = set()
    for record in records:
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            raise FleetError("AIPerf record lacks metadata")
        correlation = metadata.get("x_correlation_id")
        identifier = metadata.get("x_request_id")
        if (
            not isinstance(correlation, str)
            or not correlation
            or not isinstance(identifier, str)
            or not identifier
            or identifier in client_ids
            or isinstance(metadata.get("turn_index"), bool)
            or not isinstance(metadata.get("turn_index"), int)
        ):
            raise FleetError("AIPerf record identity is missing or duplicated")
        client_ids.add(identifier)
        client[correlation].append(metadata)
    statuses = statuses or {identifier: 200 for identifier in client_ids}
    if (
        set(statuses) != client_ids
        or set(statuses.values()) - {200, 429}
        or done_ids != {identifier for identifier, status in statuses.items() if status == 200}
    ):
        raise FleetError("AIPerf status and SSE [DONE] evidence do not cover every request")

    gateway_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in gateway:
        correlation = entry.get("x_correlation_id")
        if correlation in client:
            gateway_by_session[correlation].append(entry)
    if set(gateway_by_session) != set(client):
        raise FleetError("Gateway logs do not cover every AIPerf session")

    epp_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    gateway_ids = {
        entry.get("x_request_id") for entries in gateway_by_session.values() for entry in entries
    }
    if None in gateway_ids or len(gateway_ids) != len(records):
        raise FleetError("Gateway request IDs are missing or duplicated")
    for entry in epp:
        if entry.get("x-request-id") in gateway_ids:
            epp_by_id[entry["x-request-id"]].append(entry)
    if set(epp_by_id) != gateway_ids:
        raise FleetError("EPP logs do not cover every Gateway request")

    events: list[dict[str, Any]] = []
    workers: Counter[str] = Counter()
    offered_delay_ms: list[float] = []
    sent_to_gateway_ms: list[float] = []
    gateway_to_dispatch_ms: list[float] = []
    gateway_duration_ms: list[float] = []
    normalized_early_gateway_starts = 0
    aiperf_zero_duration_rejections = 0
    session_completion_ms: list[float] = []
    offered_to_completed_ms: list[float] = []
    for correlation, turns in client.items():
        turns.sort(key=lambda item: item["turn_index"])
        session_start = _nanoseconds(turns[0].get("request_start_ns"))
        session_end = _nanoseconds(turns[-1].get("request_end_ns"))
        if session_end < session_start:
            raise FleetError("AIPerf session chronology is inconsistent")
        if all(statuses[turn["x_request_id"]] == 200 for turn in turns):
            session_completion_ms.append((session_end - session_start).total_seconds() * 1000)
        access = sorted(
            gateway_by_session[correlation], key=lambda item: _datetime(item.get("start_time"))
        )
        if len(turns) != len(access) or [turn["turn_index"] for turn in turns] != list(
            range(len(turns))
        ):
            raise FleetError("AIPerf turn and Gateway request counts/order do not match")
        for turn, proxy in zip(turns, access, strict=True):
            offered = _nanoseconds(turn.get("credit_issued_ns"))
            sent = _nanoseconds(turn.get("request_start_ns"))
            admitted = _datetime(proxy.get("start_time"))
            completed = _nanoseconds(turn.get("request_end_ns"))
            identifier = turn["x_request_id"]
            status = statuses[identifier]
            # With the Gateway colocated on the client node, Envoy can mark
            # stream start a few microseconds before AIPerf records its send
            # timestamp. Allow at most 1 ms of instrumentation ordering
            # uncertainty; still reject meaningful clock or lifecycle drift.
            if not (
                offered <= sent
                and sent <= completed
                and sent - timedelta(milliseconds=1) <= admitted
                and admitted - sent <= timedelta(seconds=10)
                and (status == 429 or admitted <= completed)
            ):
                raise FleetError(
                    "client-to-Gateway event chronology is inconsistent "
                    f"(offered-to-send={(sent - offered).total_seconds() * 1000:.3f} ms, "
                    f"send-to-Gateway={(admitted - sent).total_seconds() * 1000:.3f} ms, "
                    f"Gateway-to-complete={(completed - admitted).total_seconds() * 1000:.3f} ms)"
                )
            offered_delay_ms.append((sent - offered).total_seconds() * 1000)
            sent_to_gateway_ms.append(max(0.0, (admitted - sent).total_seconds() * 1000))
            logical_admitted = max(admitted, sent)
            normalized_early_gateway_starts += int(admitted < sent)
            if status == 200:
                offered_to_completed_ms.append((completed - offered).total_seconds() * 1000)
            if proxy.get("response_code") != status or proxy.get("response_flags") != "-":
                raise FleetError("Gateway status does not match the exact AIPerf outcome")
            proxy_duration = proxy.get("duration_ms")
            if (
                isinstance(proxy_duration, bool)
                or not isinstance(proxy_duration, (int, float))
                or not math.isfinite(proxy_duration)
                or proxy_duration < 0
            ):
                raise FleetError("Gateway did not record a valid request duration")
            gateway_duration_ms.append(float(proxy_duration))
            rejection_terminal = admitted + timedelta(milliseconds=float(proxy_duration))
            if status == 429:
                if not 55_000 <= proxy_duration <= 65_000:
                    raise FleetError("llm-d TTL rejection did not have the reviewed 60-second wait")
                if completed == sent:
                    aiperf_zero_duration_rejections += 1
                elif abs((completed - rejection_terminal).total_seconds()) > 2:
                    raise FleetError("AIPerf rejection end differs from Envoy terminal time")
            proxy_id = proxy["x_request_id"]
            picker = epp_by_id[proxy_id]
            expected_events = EPP_EVENTS if status == 200 else EPP_REJECT_EVENTS
            if Counter(item.get("msg") for item in picker) != Counter(expected_events):
                raise FleetError("EPP event lifecycle is incomplete or duplicated")
            stages = [("offered", offered), ("sent", sent), ("admitted", logical_admitted)]
            if status == 200:
                selected = next(
                    item
                    for item in picker
                    if item.get("msg") == "EPP sent request body response(s) to proxy"
                )
                dispatched = _datetime(selected.get("ts"))
                if not admitted <= dispatched <= completed:
                    raise FleetError("EPP dispatch chronology is inconsistent")
                gateway_to_dispatch_ms.append((dispatched - admitted).total_seconds() * 1000)
                upstream = proxy.get("upstream_host")
                if not isinstance(upstream, str) or ":" not in upstream:
                    raise FleetError("Gateway did not record a model upstream")
                worker = pod_nodes.get(upstream.rsplit(":", 1)[0])
                if worker not in {"kip-gpu-1", "kip-gpu-2"}:
                    raise FleetError("Gateway dispatched outside the two owned GPU workers")
                workers[worker] += 1
                stages.extend((("dispatched", dispatched), ("completed", completed)))
            else:
                if proxy.get("upstream_host") not in (None, "", "-"):
                    raise FleetError("EPP-rejected request unexpectedly reached a model worker")
                stages.append(("rejected", rejection_terminal))
            for stage, instant in stages:
                event: dict[str, Any] = {
                    "requestId": identifier,
                    "tenant": tenant,
                    "class": workload_class,
                    "stage": stage,
                    "timestampUtc": _utc_iso(instant),
                }
                if stage == "completed":
                    event["streamDone"] = True
                events.append(event)
    result = reconcile_events(events)
    if not result["valid"] or sum(
        result["totals"][terminal] for terminal in ("completed", "rejected")
    ) != len(records):
        raise FleetError("cross-layer request accounting did not reconcile")

    def p95(values: list[float]) -> float:
        return round(sorted(values)[int(0.95 * (len(values) - 1))], 6)

    return {
        "valid": True,
        "counts": result["totals"],
        "byTenantClass": result["byTenantClass"],
        "dispatchedByWorker": dict(sorted(workers.items())),
        "offeredToSentP95Ms": p95(offered_delay_ms),
        "sentToGatewayP95Ms": p95(sent_to_gateway_ms),
        "gatewayToEppDispatchP95Ms": p95(gateway_to_dispatch_ms)
        if gateway_to_dispatch_ms
        else None,
        "gatewayDurationP95Ms": p95(gateway_duration_ms),
        "timestampOrderingToleranceMs": 1,
        "normalizedEarlyGatewayStarts": normalized_early_gateway_starts,
        "aiperfZeroDurationRejections": aiperf_zero_duration_rejections,
        "rejectionTerminalSource": (
            "Envoy access-log start + duration for exact 60-second queue-TTL 429"
        ),
        "sessionCompletionP95Ms": p95(session_completion_ms) if session_completion_ms else None,
        "offeredToCompletedWithinTenMinutesRate": sum(
            value <= 600_000 for value in offered_to_completed_ms
        )
        / len(records),
        "evidence": "AIPerf credit/send + Envoy auth/route + EPP selection + final SSE [DONE]",
        "queueEvidence": "aggregate EPP metrics only; no per-request queue event inferred",
    }


def _kubectl(*arguments: str) -> str:
    result = subprocess.run(
        ["kubectl", *arguments], capture_output=True, text=True, check=False, timeout=60
    )
    if result.returncode:
        raise FleetError("required private Kubernetes log or inventory is unavailable")
    return result.stdout


def _pod_name(namespace: str, prefix: str) -> str:
    pods = json.loads(_kubectl("-n", namespace, "get", "pods", "-o", "json"))["items"]
    names = [
        item["metadata"]["name"]
        for item in pods
        if item["metadata"]["name"].startswith(prefix)
        and item.get("status", {}).get("phase") == "Running"
    ]
    if len(names) != 1:
        raise FleetError("exactly one owned Gateway or EPP Pod is required")
    return names[0]


def _json_lines(value: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in value.splitlines() if line.lstrip().startswith("{")]


def inspect_run(root: Path, run_id: str, tenant: str, workload_class: str) -> dict[str, Any]:
    if not RUN_ID.fullmatch(run_id) or tenant not in TENANTS or workload_class not in CLASSES:
        raise FleetError("pilot accounting requires reviewed run, tenant, and class names")
    base = root / "benchmarks" / ".work" / run_id / f"{tenant}-{workload_class}" / "raw"
    summary = json.loads((base / "profile_export_aiperf.json").read_text(encoding="utf-8"))
    records = _json_lines((base / "profile_export.jsonl").read_text(encoding="utf-8"))
    statuses, done_ids = stream_outcomes(
        base / "profile_export_raw.jsonl", len(records), allow_rejected=True
    )
    rejected = sum(status == 429 for status in statuses.values())
    errors = summary.get("error_summary") or []
    if summary.get("aiperf_version") != "0.12.0" or (
        (summary.get("request_count") or {}).get("avg") != len(records) - rejected
        or (summary.get("completed_request_count") or {}).get("avg") not in (None, len(records))
        or (summary.get("error_request_count") or {}).get("avg") not in (None, rejected)
    ):
        raise FleetError("AIPerf summary counts do not match private request outcomes")
    if rejected and (
        len(errors) != 1
        or errors[0].get("count") != rejected
        or errors[0].get("error_details", {}).get("code") != 429
        or "request TTL expired" not in errors[0].get("error_details", {}).get("message", "")
    ):
        raise FleetError("only exact llm-d queue-TTL rejection is supported in pilot reports")
    if not rejected and errors:
        raise FleetError("AIPerf reported errors without matching private request outcomes")
    started = _datetime(summary.get("start_time") + "Z") - timedelta(minutes=1)
    since = _utc_iso(started)
    gateway_name = _pod_name("envoy-gateway-system", "envoy-kip-inference-kip-inference-gateway-")
    epp_name = _pod_name("kip-inference", "kip-qwen25-pilot-epp-")
    gateway = _json_lines(
        _kubectl(
            "-n",
            "envoy-gateway-system",
            "logs",
            gateway_name,
            "-c",
            "envoy",
            f"--since-time={since}",
        )
    )
    epp = _json_lines(_kubectl("-n", "kip-inference", "logs", epp_name, f"--since-time={since}"))
    pod_json = json.loads(_kubectl("-n", "kip-inference", "get", "pods", "-o", "json"))
    pod_nodes = {
        item.get("status", {}).get("podIP"): item.get("spec", {}).get("nodeName")
        for item in pod_json["items"]
    }
    outcome = summarize_evidence(
        records,
        done_ids,
        gateway,
        epp,
        pod_nodes,
        tenant=tenant,
        workload_class=workload_class,
        statuses=statuses,
    )
    outcome.update({"runId": run_id, "tenant": tenant, "class": workload_class})
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile a pilot AIPerf run across private layers"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--class", dest="workload_class", required=True)
    args = parser.parse_args()
    try:
        result = inspect_run(
            Path(__file__).resolve().parent.parent,
            args.run_id,
            args.tenant,
            args.workload_class,
        )
    except (FleetError, OSError, ValueError, KeyError, IndexError, json.JSONDecodeError) as error:
        print(f"pilot accounting failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
