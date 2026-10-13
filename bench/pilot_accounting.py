"""Fail-closed, content-free request accounting for the proposed fleet pilot.

This is deliberately separate from AIPerf's latency calculations. It reconciles
client, Gateway, and llm-d event identities without inventing admissions from
client-side success. Only aggregate counts may be published in a report.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from .errors import ValidationError
from .workloads import CLASSES, TENANTS

STAGES = (
    "offered",
    "sent",
    "admitted",
    "queued",
    "dispatched",
    "completed",
    "rejected",
    "failed",
)
TERMINAL = {"completed", "rejected", "failed"}
ALLOWED_KEYS = {"requestId", "tenant", "class", "stage", "timestampUtc", "streamDone"}


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError("pilot event timestampUtc must be an ISO-8601 UTC string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError("pilot event timestampUtc is malformed") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValidationError("pilot event timestampUtc must be UTC")
    return parsed


def reconcile_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Count offered/served outcomes and disclose impossible or missing transitions."""
    grouped: dict[str, list[tuple[str, datetime, bool | None]]] = defaultdict(list)
    identity: dict[str, tuple[str, str]] = {}
    for event in events:
        if not isinstance(event, dict) or set(event) - ALLOWED_KEYS:
            raise ValidationError("pilot event contains unknown fields")
        request_id = event.get("requestId")
        tenant = event.get("tenant")
        workload_class = event.get("class")
        stage = event.get("stage")
        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > 128
            or tenant not in TENANTS
            or workload_class not in CLASSES
            or stage not in STAGES
        ):
            raise ValidationError("pilot event identity or stage is invalid")
        done = event.get("streamDone")
        if done is not None and (stage != "completed" or not isinstance(done, bool)):
            raise ValidationError("streamDone is permitted only on completed events")
        pair = (tenant, workload_class)
        if request_id in identity and identity[request_id] != pair:
            raise ValidationError("pilot requestId changed tenant or class")
        identity[request_id] = pair
        grouped[request_id].append((stage, _timestamp(event.get("timestampUtc")), done))

    totals: Counter[str] = Counter()
    by_cohort: dict[str, Counter[str]] = defaultdict(Counter)
    warnings: Counter[str] = Counter()
    if not grouped:
        warnings["no offered requests"] = 1
    for request_id, records in grouped.items():
        records.sort(key=lambda item: item[1])
        stages = [stage for stage, _, _ in records]
        stage_set = set(stages)
        cohort = "/".join(identity[request_id])

        def warn(detail: str, *, cohort_name: str = cohort) -> None:
            warnings[f"{cohort_name}: {detail}"] += 1

        if len(stages) != len(stage_set):
            warn("duplicate stage")
        if not stages or stages[0] != "offered":
            warn("missing/late offered event")
        if len(stage_set & TERMINAL) > 1:
            warn("conflicting terminal outcomes")
        if "completed" in stage_set and not {"sent", "admitted", "dispatched"} <= stage_set:
            warn("completed without sent/admitted/dispatched evidence")
        if "dispatched" in stage_set and "admitted" not in stage_set:
            warn("dispatch without admission evidence")
        if "queued" in stage_set and "admitted" not in stage_set:
            warn("queue without admission evidence")
        if "rejected" in stage_set and "dispatched" in stage_set:
            warn("rejection after dispatch")
        ordered = [stage for stage in stages if stage not in TERMINAL]
        canonical = [stage for stage in STAGES if stage not in TERMINAL]
        if ordered != sorted(ordered, key=canonical.index):
            warn("non-monotonic request stages")
        if any(stage in TERMINAL for stage in stages[:-1]):
            warn("terminal event was followed by another event")
        if "completed" in stage_set:
            completed = next(done for stage, _, done in records if stage == "completed")
            if completed is not True:
                warn("completion lacks stream-end evidence")
        for stage in stage_set:
            totals[stage] += 1
            by_cohort[cohort][stage] += 1
        if not stage_set & TERMINAL:
            totals["inFlight"] += 1
            by_cohort[cohort]["inFlight"] += 1
    return {
        "valid": not warnings,
        "requestCount": len(grouped),
        "totals": {key: totals[key] for key in (*STAGES, "inFlight")},
        "byTenantClass": {
            cohort: {key: counts[key] for key in (*STAGES, "inFlight")}
            for cohort, counts in sorted(by_cohort.items())
        },
        "warnings": [
            {"detail": detail, "count": count} for detail, count in sorted(warnings.items())
        ],
    }
