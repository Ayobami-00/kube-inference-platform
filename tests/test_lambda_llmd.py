"""Two reviewed, internal llm-d pools stay separate from KIP model ownership."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "deploy/lambda/llmd-install.py"
    spec = importlib.util.spec_from_file_location("lambda_llmd_install", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_two_pools_use_distinct_model_and_header_routes() -> None:
    module = _module()
    assert set(module.POOLS) == {"qwen3-8b", "qwen3-next-80b-a3b-instruct"}
    control = "kip-lambda-deadbeef-control-speech"
    first = module.pool_values("qwen3-8b", control)
    second = module.pool_values("qwen3-next-80b-a3b-instruct", control)
    for model, values in (("qwen3-8b", first), ("qwen3-next-80b-a3b-instruct", second)):
        assert values["router"]["modelServers"]["matchLabels"] == {
            "kube-inference-platform.io/model-pool": model
        }
        assert values["httpRoute"]["baseModel"] == model
        assert values["httpRoute"]["inferenceGatewayName"] == "kip-inference-gateway"
        assert values["router"]["inferencePool"]["failureMode"] == "FailClose"
        epp = values["router"]["epp"]
        assert epp["image"]["tag"] == module.EPP_TAG
        affinity = epp["affinity"]["nodeAffinity"]
        assert control in str(affinity)


def test_flow_values_preserve_pool_selection_and_enable_three_priority_bands() -> None:
    module = _module()
    control = "kip-lambda-deadbeef-control-speech"
    for model, concurrency in (("qwen3-8b", 6), ("qwen3-next-80b-a3b-instruct", 4)):
        baseline = module.pool_values(model, control)
        flow = module.pool_values(model, control, flow=True)
        assert flow["router"]["modelServers"] == baseline["router"]["modelServers"]
        assert flow["httpRoute"] == baseline["httpRoute"]
        plugins = flow["router"]["epp"]["pluginsCustomConfig"]["kip-lambda-plugins.yaml"]
        assert "featureGates:\n  - flowControl" in plugins
        assert f"maxConcurrency: {concurrency}" in plugins
        assert "priority: 100" in plugins
        assert "priority: 0" in plugins
        assert "priority: -10" in plugins
        assert "round-robin-fairness-policy" in plugins
        assert flow["router"]["epp"]["podAnnotations"]["kube-inference-platform.io/plugins-sha256"]


def test_reviewed_large_pool_flow_variant_changes_only_concurrency() -> None:
    import runpy

    root = Path(__file__).resolve().parents[1]
    variant = runpy.run_path(str(root / "deploy/lambda/flow-variant.py"))
    module = {"pool_values": _module().pool_values}
    control = "kip-lambda-deadbeef-control-speech"
    baseline = variant["desired_values"](module, control, "baseline")
    candidate = variant["desired_values"](module, control, "large-flow6")
    higher = variant["desired_values"](module, control, "large-flow8")
    assert baseline["router"]["modelServers"] == candidate["router"]["modelServers"]
    assert baseline["httpRoute"] == candidate["httpRoute"]
    base_epp = baseline["router"]["epp"]
    candidate_epp = candidate["router"]["epp"]
    assert "maxConcurrency: 4" in base_epp["pluginsCustomConfig"]["kip-lambda-plugins.yaml"]
    assert "maxConcurrency: 6" in candidate_epp["pluginsCustomConfig"]["kip-lambda-plugins.yaml"]
    assert (
        "maxConcurrency: 8"
        in higher["router"]["epp"]["pluginsCustomConfig"]["kip-lambda-plugins.yaml"]
    )
    assert higher["router"]["modelServers"] == baseline["router"]["modelServers"]
    assert (
        candidate_epp["podAnnotations"]["kube-inference-platform.io/flow-variant"] == "large-flow6"
    )
    assert base_epp["image"] == candidate_epp["image"]
