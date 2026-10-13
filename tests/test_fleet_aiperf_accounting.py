"""Cross-layer accounting uses exact joins and publishes only aggregates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fleet.aiperf_accounting import EPP_EVENTS, EPP_REJECT_EVENTS, RUN_ID, summarize_evidence
from fleet.vast import FleetError


def test_accounting_accepts_only_timestamped_shared_pilot_ids() -> None:
    assert RUN_ID.fullmatch("pilot-text-20261013-013700-sample100")
    assert not RUN_ID.fullmatch("pilot-text-20261013-013700-../../escape")


def fixtures() -> tuple[list[dict], set[str], list[dict], list[dict], dict[str, str]]:
    origin = datetime(2026, 10, 13, tzinfo=UTC)
    records = []
    gateway = []
    epp = []
    for index in range(2):
        base = origin + timedelta(seconds=index)
        records.append(
            {
                "metadata": {
                    "x_request_id": f"client-sensitive-{index}",
                    "x_correlation_id": "session-sensitive",
                    "turn_index": index,
                    "credit_issued_ns": int(base.timestamp() * 1e9),
                    "request_start_ns": int((base + timedelta(milliseconds=10)).timestamp() * 1e9),
                    "request_end_ns": int((base + timedelta(milliseconds=200)).timestamp() * 1e9),
                }
            }
        )
        gateway.append(
            {
                "x_correlation_id": "session-sensitive",
                "x_request_id": f"proxy-sensitive-{index}",
                "start_time": (base + timedelta(milliseconds=20))
                .isoformat()
                .replace("+00:00", "Z"),
                "response_code": 200,
                "response_flags": "-",
                "duration_ms": 175,
                "upstream_host": "10.0.0.1:8000",
            }
        )
        epp.extend(
            {
                "x-request-id": f"proxy-sensitive-{index}",
                "msg": message,
                "ts": (base + timedelta(milliseconds=30)).timestamp(),
            }
            for message in EPP_EVENTS
        )
    return (
        records,
        {"client-sensitive-0", "client-sensitive-1"},
        gateway,
        epp,
        {"10.0.0.1": "kip-gpu-1"},
    )


def test_exact_join_counts_only_complete_streams_and_hides_identifiers() -> None:
    records, done, gateway, epp, nodes = fixtures()
    result = summarize_evidence(
        records, done, gateway, epp, nodes, tenant="tenant-a", workload_class="chat"
    )
    assert result["valid"] is True
    assert result["counts"]["offered"] == 2
    assert result["counts"]["sent"] == 2
    assert result["counts"]["admitted"] == 2
    assert result["counts"]["dispatched"] == 2
    assert result["counts"]["completed"] == 2
    assert result["counts"]["queued"] == 0
    assert result["dispatchedByWorker"] == {"kip-gpu-1": 2}
    assert result["sessionCompletionP95Ms"] == pytest.approx(1190, abs=0.01)
    assert result["sentToGatewayP95Ms"] == pytest.approx(10)
    assert result["gatewayToEppDispatchP95Ms"] == pytest.approx(10)
    assert result["gatewayDurationP95Ms"] == pytest.approx(175)
    assert result["offeredToCompletedWithinTenMinutesRate"] == 1
    assert "sensitive" not in str(result)
    assert "10.0.0.1" not in str(result)


@pytest.mark.parametrize("missing", ("done", "gateway", "epp", "upstream"))
def test_missing_layer_fails_closed(missing: str) -> None:
    records, done, gateway, epp, nodes = fixtures()
    if missing == "done":
        done.remove("client-sensitive-0")
    elif missing == "gateway":
        gateway.pop()
    elif missing == "epp":
        epp.pop()
    else:
        nodes.clear()
    with pytest.raises(FleetError):
        summarize_evidence(
            records, done, gateway, epp, nodes, tenant="tenant-a", workload_class="chat"
        )


def test_submillisecond_colocated_timestamp_ordering_is_bounded() -> None:
    records, done, gateway, epp, nodes = fixtures()
    for index, item in enumerate(gateway):
        base = datetime(2026, 10, 13, tzinfo=UTC) + timedelta(seconds=index)
        item["start_time"] = (base + timedelta(milliseconds=9, microseconds=984)).isoformat()
    result = summarize_evidence(
        records, done, gateway, epp, nodes, tenant="tenant-a", workload_class="chat"
    )
    assert result["sentToGatewayP95Ms"] == 0
    assert result["normalizedEarlyGatewayStarts"] == 2
    assert result["timestampOrderingToleranceMs"] == 1
    gateway[0]["start_time"] = (
        datetime(2026, 10, 13, tzinfo=UTC) + timedelta(milliseconds=8)
    ).isoformat()
    with pytest.raises(FleetError, match="chronology"):
        summarize_evidence(
            records, done, gateway, epp, nodes, tenant="tenant-a", workload_class="chat"
        )


def test_exact_queue_ttl_rejection_is_counted_not_dispatched() -> None:
    records, done, gateway, epp, nodes = fixtures()
    done.remove("client-sensitive-1")
    records[1]["metadata"]["request_end_ns"] = records[1]["metadata"]["request_start_ns"]
    gateway[1]["response_code"] = 429
    gateway[1]["duration_ms"] = 60_000
    gateway[1]["upstream_host"] = "-"
    epp[:] = [item for item in epp if item["x-request-id"] != "proxy-sensitive-1"]
    epp.extend(
        {"x-request-id": "proxy-sensitive-1", "msg": message} for message in EPP_REJECT_EVENTS
    )
    statuses = {"client-sensitive-0": 200, "client-sensitive-1": 429}
    result = summarize_evidence(
        records,
        done,
        gateway,
        epp,
        nodes,
        tenant="tenant-a",
        workload_class="chat",
        statuses=statuses,
    )
    assert result["valid"] is True
    assert result["counts"]["offered"] == 2
    assert result["counts"]["admitted"] == 2
    assert result["counts"]["dispatched"] == 1
    assert result["counts"]["completed"] == 1
    assert result["counts"]["rejected"] == 1
    assert result["dispatchedByWorker"] == {"kip-gpu-1": 1}
    assert result["offeredToCompletedWithinTenMinutesRate"] == 0.5
    assert result["aiperfZeroDurationRejections"] == 1
    assert "sensitive" not in str(result)
    gateway[1]["response_code"] = 200
    with pytest.raises(FleetError, match="Gateway status"):
        summarize_evidence(
            records,
            done,
            gateway,
            epp,
            nodes,
            tenant="tenant-a",
            workload_class="chat",
            statuses=statuses,
        )
