"""Exact-window fleet prices must reconcile before reports claim costs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fleet import cost_check
from fleet.vast import FleetError

START = datetime(2026, 10, 13, 0, 42, tzinfo=UTC)
RATES = {"kip-control": 0.68, "kip-gpu-1": 0.62, "kip-gpu-2": 0.74}
SNAPSHOT = {"nodes": [{"node": name, "hourlyUsd": str(rate)} for name, rate in RATES.items()]}


def fixture_response(path: str, query: dict[str, str]) -> dict:
    if path == "/api/v1/query":
        return {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"node": name}, "value": [0, str(rate)]}
                    for name, rate in RATES.items()
                ]
            },
        }
    if path == "/assets":
        return {
            "code": 200,
            "data": {
                f"custom/node/{name}": {"type": "Node", "totalCost": rate / 60}
                for name, rate in RATES.items()
            },
        }
    assert path == "/allocation/compute"
    assert query["includeIdle"] == "true"
    assert query["idleByNode"] == "true"
    return {
        "code": 200,
        "data": [{name: {"totalCost": rate / 60} for name, rate in RATES.items()}],
    }


def test_reconciles_all_nodes_and_requires_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cost_check, "_get_json", lambda _base, path, query: fixture_response(path, query)
    )
    result = cost_check.reconcile(
        SNAPSHOT,
        prometheus="http://prometheus",
        opencost="http://opencost",
        start=START,
        end=START + timedelta(minutes=1),
    )
    assert result["window"] == "2026-10-13T00:42:00Z,2026-10-13T00:43:00Z"
    assert result["auditUsd"] == 0.034
    assert result["allocatedUsd"] == 0.034
    assert set(result["nodes"]) == set(RATES)


def test_reconciles_three_minute_report_window(monkeypatch: pytest.MonkeyPatch) -> None:
    def get(_base: str, path: str, query: dict[str, str]) -> dict:
        response = fixture_response(path, query)
        if path != "/api/v1/query":
            assert query["window"].endswith("00:45:00Z")
        if path == "/assets":
            for asset in response["data"].values():
                asset["totalCost"] *= 3
        if path == "/allocation/compute":
            for allocation in response["data"][0].values():
                allocation["totalCost"] *= 3
        return response

    monkeypatch.setattr(cost_check, "_get_json", get)
    result = cost_check.reconcile(
        SNAPSHOT,
        prometheus="http://prometheus",
        opencost="http://opencost",
        start=START,
        end=START + timedelta(minutes=3),
    )
    assert result["durationMinutes"] == 3
    assert result["auditUsd"] == 0.102
    assert result["allocatedUsd"] == 0.102


@pytest.mark.parametrize("broken", ["missing-node", "wrong-rate", "missing-idle"])
def test_rejects_incomplete_or_mispriced_costs(
    monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    def get(_base: str, path: str, query: dict[str, str]) -> dict:
        response = fixture_response(path, query)
        if broken == "missing-node" and path == "/assets":
            response["data"].pop("custom/node/kip-gpu-2")
        elif broken == "wrong-rate" and path == "/api/v1/query":
            response["data"]["result"][1]["value"][1] = "0.25"
        elif broken == "missing-idle" and path == "/allocation/compute":
            response["data"][0]["kip-control"]["totalCost"] = 0.001
        return response

    monkeypatch.setattr(cost_check, "_get_json", get)
    with pytest.raises(FleetError):
        cost_check.reconcile(
            SNAPSHOT,
            prometheus="http://prometheus",
            opencost="http://opencost",
            start=START,
            end=START + timedelta(minutes=1),
        )
