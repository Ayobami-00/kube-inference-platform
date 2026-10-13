"""Fleet dashboards use stable recording rules and label audit cost honestly."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_fleet_dashboard_cost_panels_are_audit_only() -> None:
    dashboard = json.loads(
        (ROOT / "deploy/fleet/observability-dashboard.json").read_text(encoding="utf-8")
    )
    panels = dashboard["panels"]
    assert len({panel["id"] for panel in panels}) == len(panels)
    cost_panels = [panel for panel in panels if "rental" in panel["title"].lower()]
    assert len(cost_panels) == 2
    assert all("USD/hour" in panel["title"] for panel in cost_panels)
    assert all(
        target["expr"].startswith(("sum(kip:fleet:", "kip:fleet:"))
        for panel in cost_panels
        for target in panel["targets"]
    )
    assert "tenant attribution" in cost_panels[1]["title"].lower()


def test_fleet_cost_rules_normalize_opencost_node_rates() -> None:
    resource = yaml.safe_load(
        (ROOT / "deploy/fleet/observability-rules.yaml").read_text(encoding="utf-8")
    )
    assert resource["kind"] == "PrometheusRule"
    groups = {group["name"]: group for group in resource["spec"]["groups"]}
    rules = {rule["record"]: rule for rule in groups["kip.fleet.cost"]["rules"]}
    assert groups["kip.fleet.cost"]["interval"] == "30s"
    assert set(rules) == {
        "kip:fleet:node_rental_usd_per_hour",
        "kip:fleet:rental_usd_per_hour",
    }
    assert (
        'node_total_hourly_cost{job="kip-opencost"}'
        in rules["kip:fleet:node_rental_usd_per_hour"]["expr"]
    )
    assert rules["kip:fleet:rental_usd_per_hour"]["expr"] == (
        "sum(kip:fleet:node_rental_usd_per_hour)"
    )


def test_fleet_dashboard_uses_actual_flow_control_queue_not_scheduler_average() -> None:
    resource = yaml.safe_load(
        (ROOT / "deploy/fleet/observability-rules.yaml").read_text(encoding="utf-8")
    )
    routing = next(
        group for group in resource["spec"]["groups"] if group["name"] == "kip.fleet.routing"
    )
    rules = {rule["record"]: rule["expr"] for rule in routing["rules"]}
    assert "llm_d_epp_flow_control_queue_size" in rules["kip:fleet:flow_queue_depth"]
    assert "fairness_id" in rules["kip:fleet:flow_queue_depth"]
    assert 'outcome="EvictedTTL"' in rules["kip:fleet:flow_ttl_evictions_rate5m"]
    dashboard = json.loads(
        (ROOT / "deploy/fleet/observability-dashboard.json").read_text(encoding="utf-8")
    )
    queued = next(panel for panel in dashboard["panels"] if panel["id"] == 16)
    assert "kip:fleet:flow_queue_depth" in queued["targets"][0]["expr"]
    assert not any(
        "kip:fleet:pool_queue" in target["expr"]
        for panel in dashboard["panels"]
        for target in panel["targets"]
    )
