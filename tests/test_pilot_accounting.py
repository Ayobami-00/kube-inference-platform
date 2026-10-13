"""Cross-layer offered/admitted/served accounting never fabricates missing data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bench.errors import ValidationError
from bench.pilot_accounting import reconcile_events


def event(
    request_id: str,
    stage: str,
    offset_ms: int,
    *,
    tenant: str = "tenant-a",
    workload_class: str = "chat",
    done: bool | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "requestId": request_id,
        "tenant": tenant,
        "class": workload_class,
        "stage": stage,
        "timestampUtc": (datetime(2026, 10, 12, tzinfo=UTC) + timedelta(milliseconds=offset_ms))
        .isoformat()
        .replace("+00:00", "Z"),
    }
    if done is not None:
        value["streamDone"] = done
    return value


def test_complete_queue_rejection_and_client_backlog_are_separate() -> None:
    records = [
        event("secret-a", "offered", 0),
        event("secret-a", "sent", 10),
        event("secret-a", "admitted", 11),
        event("secret-a", "queued", 12),
        event("secret-a", "dispatched", 20),
        event("secret-a", "completed", 100, done=True),
        event("secret-b", "offered", 0, tenant="tenant-b", workload_class="batch"),
        event("secret-b", "sent", 5, tenant="tenant-b", workload_class="batch"),
        event("secret-b", "rejected", 8, tenant="tenant-b", workload_class="batch"),
        event("secret-c", "offered", 0),
    ]
    result = reconcile_events(records)
    assert result["valid"] is True
    assert result["totals"] == {
        "offered": 3,
        "sent": 2,
        "admitted": 1,
        "queued": 1,
        "dispatched": 1,
        "completed": 1,
        "rejected": 1,
        "failed": 0,
        "inFlight": 1,
    }
    assert result["byTenantClass"]["tenant-b/batch"]["rejected"] == 1
    assert "secret-a" not in str(result)


def test_missing_gateway_or_stream_evidence_invalidates_report() -> None:
    records = [
        event("sensitive-id", "offered", 0),
        event("sensitive-id", "sent", 1),
        event("sensitive-id", "completed", 3),
    ]
    result = reconcile_events(records)
    assert result["valid"] is False
    assert result["totals"]["completed"] == 1
    assert any("admitted/dispatched" in item["detail"] for item in result["warnings"])
    assert "sensitive-id" not in str(result)


def test_empty_trace_cannot_publish_as_healthy() -> None:
    result = reconcile_events([])
    assert result["valid"] is False
    assert result["requestCount"] == 0


def test_non_monotonic_or_conflicting_events_invalid() -> None:
    records = [
        event("r1", "offered", 0),
        event("r1", "dispatched", 2),
        event("r1", "admitted", 3),
        event("r1", "failed", 4),
        event("r1", "completed", 5, done=True),
    ]
    result = reconcile_events(records)
    assert result["valid"] is False
    assert any("non-monotonic" in item["detail"] for item in result["warnings"])
    assert any("conflicting terminal" in item["detail"] for item in result["warnings"])


@pytest.mark.parametrize("mutation", ("unknown", "identity", "timestamp", "stage", "done"))
def test_invalid_event_schema_is_rejected(mutation: str) -> None:
    record = event("r1", "offered", 0)
    if mutation == "unknown":
        record["prompt"] = "secret"
    elif mutation == "identity":
        record["tenant"] = "unreviewed"
    elif mutation == "timestamp":
        record["timestampUtc"] = "2026-10-12T00:00:00"
    elif mutation == "stage":
        record["stage"] = "fabricated"
    else:
        record["streamDone"] = True
    with pytest.raises(ValidationError):
        reconcile_events([record])
