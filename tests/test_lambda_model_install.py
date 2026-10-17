"""Reviewed Lambda vLLM manifests stay pinned, private, and node-specific."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "deploy/lambda/model-install.py"
    spec = importlib.util.spec_from_file_location("lambda_model_install", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_interactive_manifest_is_pinned_and_internal() -> None:
    module = _module()
    lock = json.loads(module.LOCK.read_text())
    versions = json.loads(module.VERSIONS.read_text())
    model = lock["pools"]["interactive"]
    image = versions["inference"]["vllmLinuxAmd64Image"]
    value = module.deployment(
        "small-1", "kip-lambda-test-small-1", model, "qwen3-8b-small", image, "a" * 64
    )
    pod = value["spec"]["template"]["spec"]
    server = pod["containers"][0]
    assert value["spec"]["replicas"] == 1
    assert value["spec"]["progressDeadlineSeconds"] == 2700
    assert value["spec"]["strategy"]["type"] == "Recreate"
    labels = value["spec"]["template"]["metadata"]["labels"]
    assert labels["kube-inference-platform.io/served-model"] == "qwen3-8b"
    assert labels["kube-inference-platform.io/worker-role"] == "combined"
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "kip-lambda-test-small-1"}
    assert server["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert server["command"] == ["vllm", "serve"]
    assert server["args"][0] == "Qwen/Qwen3-8B"
    assert model["revision"] in server["args"]
    assert "--trust-remote-code" not in server["args"]
    assert "--quantization" not in server["args"]
    assert "runtimeClassName" not in pod
    assert module.service(model)["spec"]["type"] == "ClusterIP"


def test_large_model_requests_four_whole_gpus() -> None:
    module = _module()
    model = json.loads(module.LOCK.read_text())["pools"]["reasoning-batch"]
    value = module.deployment(
        "large", "kip-lambda-test-large", model, "qwen3-next-80b", "pinned-image", "b" * 64
    )
    server = value["spec"]["template"]["spec"]["containers"][0]
    assert server["resources"]["requests"]["nvidia.com/gpu"] == "4"
    assert server["resources"]["limits"]["nvidia.com/gpu"] == "4"
    assert server["args"][server["args"].index("--tensor-parallel-size") + 1] == "4"
