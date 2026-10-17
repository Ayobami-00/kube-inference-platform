"""Trusted identity and pool-specific objective contracts for Lambda."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def auth_module():
    source = ROOT / "deploy/lambda/tenant-auth.py"
    spec = importlib.util.spec_from_file_location("lambda_tenant_auth", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def credentials():
    return {
        tenant: {
            name: f"{i:064x}"
            for i, name in enumerate(("chat", "agentic", "batch", "voice"), start=offset)
        }
        for tenant, offset in (("tenant-a", 1), ("tenant-b", 5), ("tenant-c", 9))
    }


def test_tenant_identity_is_independent_of_pool_and_class() -> None:
    auth = auth_module()
    keys = credentials()
    for model, pool in auth.MODELS.items():
        for workload_class, band in auth.CLASS_BAND.items():
            status, headers = auth.authorize(
                {
                    "Authorization": f"Bearer {keys['tenant-b'][workload_class]}",
                    "X-Gateway-Base-Model-Name": model,
                },
                keys,
            )
            assert status == 200
            assert headers["x-llm-d-inference-fairness-id"] == "tenant-b"
            assert headers["x-llm-d-inference-objective"] == f"{pool}-{band}"
            assert headers["x-kip-workload-class"] == workload_class


def test_spoofed_identity_priority_and_unknown_model_fail_closed() -> None:
    auth = auth_module()
    keys = credentials()
    baseline = {
        "Authorization": f"Bearer {keys['tenant-a']['chat']}",
        "X-Gateway-Base-Model-Name": "qwen3-8b",
    }
    for extra in (
        {"X-LLM-D-Inference-Objective": "kip-qwen3-8b-voice"},
        {"X-KIP-Tenant": "tenant-c"},
        {"X-Gateway-Inference-Fairness-Id": "tenant-c"},
    ):
        assert auth.authorize({**baseline, **extra}, keys) == (403, {})
    assert auth.authorize({**baseline, "X-Gateway-Base-Model-Name": "unknown"}, keys) == (403, {})
    assert auth.authorize({"X-Gateway-Base-Model-Name": "qwen3-8b"}, keys) == (403, {})


def test_policy_is_fail_closed_at_gateway_scope() -> None:
    text = (ROOT / "deploy/lambda/tenant-security-policy.yaml").read_text()
    assert "kind: Gateway\n      name: kip-inference-gateway" in text
    assert "failOpen: false" in text
    assert "x-gateway-base-model-name" in text
    assert "x-llm-d-inference-objective" in text
