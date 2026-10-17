"""Lambda's two-model, multi-node telemetry contracts."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def dashboard():
    path = ROOT / "deploy/lambda/render-dashboard.py"
    spec = importlib.util.spec_from_file_location("lambda_dashboard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render(ROOT / "deploy/fleet/observability-dashboard.json")


def test_lambda_dashboard_omits_unverified_cost_and_uses_two_model_rules() -> None:
    item = dashboard()
    assert item["uid"] == "kip-llmd-lambda-campaign"
    assert len(item["panels"]) == 14
    queries = " ".join(
        target["expr"] for panel in item["panels"] for target in panel.get("targets", [])
    )
    assert "kip:lambda:" in queries
    assert "kip:fleet:" not in queries
    assert "rental_usd" not in queries
    assert "Vast" not in str(item)


def test_lambda_monitors_discover_both_pools_without_current_pod_names() -> None:
    path = ROOT / "deploy/lambda/observability-monitors.yaml"
    monitors = list(yaml.safe_load_all(path.read_text()))
    assert [item["metadata"]["name"] for item in monitors] == [
        "kip-fleet-vllm",
        "kip-fleet-epp",
        "kip-fleet-dcgm",
    ]
    vllm, epp, dcgm = monitors
    assert vllm["spec"]["selector"]["matchLabels"]["app.kubernetes.io/component"] == "llm-runtime"
    assert epp["spec"]["selector"]["matchExpressions"][0]["values"] == [
        "kip-qwen3-8b-epp",
        "kip-qwen3-next-epp",
    ]
    assert all(item["spec"]["endpoints"][0]["interval"] == "5s" for item in monitors)
    assert dcgm["spec"]["namespaceSelector"]["matchNames"] == ["gpu-operator"]


def test_lambda_rules_have_model_node_tenant_and_gpu_dimensions() -> None:
    path = ROOT / "deploy/lambda/observability-rules.yaml"
    manifest = yaml.safe_load(path.read_text())
    rules = [rule for group in manifest["spec"]["groups"] for rule in group["rules"]]
    expressions = " ".join(rule["expr"] for rule in rules)
    assert "model_name" in expressions
    assert "fairness_id" in expressions
    assert "DCGM_FI_DEV_GPU_UTIL" in expressions
    assert "llm_d_epp_flow_control_requests_total" in expressions
    assert "qwen25" not in expressions


def test_epp_metrics_keep_authentication_and_bounded_prometheus_access() -> None:
    path = ROOT / "deploy/lambda/observability-epp-rbac.yaml"
    role, reader, delegator = list(yaml.safe_load_all(path.read_text()))
    assert role["metadata"]["name"] == "kip-epp-metrics-reader"
    assert role["rules"] == [{"nonResourceURLs": ["/metrics"], "verbs": ["get"]}]
    assert reader["roleRef"]["name"] == "kip-epp-metrics-reader"
    assert delegator["roleRef"]["name"] == "system:auth-delegator"
    assert {item["name"] for item in delegator["subjects"]} == {
        "kip-qwen3-8b-epp",
        "kip-qwen3-next-epp",
    }
