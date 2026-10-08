"""Runtime metadata contract used by observability and cost attribution."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
RUNTIME_ROOT = ROOT / "deploy" / "runtime" / "vllm"
RENDER = RUNTIME_ROOT / "lib" / "render.sh"


def render(template: str, destination: Path) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "KIP_RUNTIME_NAMESPACE": "kip-runtime",
            "KIP_RUNTIME_RESOURCE_NAME": "vllm-qwen25-7b",
            "KIP_MODEL_PROFILE": "qwen2.5-7b-instruct",
            "KIP_RUNTIME_PROFILE_HASH": "profile-hash",
            "KIP_VLLM_VERSION": "v0.29.0",
            "KIP_VLLM_IMAGE": "vllm:test",
            "KIP_VLLM_IMAGE_DIGEST": "sha256:image",
            "KIP_RUNTIME_MODEL_ID": "Qwen/Qwen2.5-7B-Instruct",
            "KIP_RUNTIME_MODEL_REVISION": ("a09a35458c702b33eeacc393d103063234e8bc28"),
            "KIP_RUNTIME_SERVED_MODEL_NAME": "qwen2.5-7b-instruct",
            "KIP_RUNTIME_REPLICAS": "1",
            "KIP_RUNTIME_SERVICE_PORT": "8000",
            "KIP_RUNTIME_CACHE_PATH": "/var/lib/kip/cache",
            "KIP_RUNTIME_SHM_SIZE": "4Gi",
            "KIP_RUNTIME_CPU_REQUEST": "4",
            "KIP_RUNTIME_MEMORY_REQUEST": "24Gi",
            "KIP_RUNTIME_EPHEMERAL_REQUEST": "10Gi",
            "KIP_RUNTIME_GPU_REQUEST": "1",
            "KIP_RUNTIME_CPU_LIMIT": "12",
            "KIP_RUNTIME_MEMORY_LIMIT": "48Gi",
            "KIP_RUNTIME_EPHEMERAL_LIMIT": "20Gi",
            "KIP_RUNTIME_GPU_LIMIT": "1",
            "KIP_RUNTIME_DTYPE": "bfloat16",
            "KIP_RUNTIME_TENSOR_PARALLEL_SIZE": "1",
            "KIP_RUNTIME_MAX_MODEL_LENGTH": "8192",
            "KIP_RUNTIME_GPU_MEMORY_UTILIZATION": "0.9",
            "KIP_RUNTIME_MAX_NUMBER_OF_SEQUENCES": "8",
            "KIP_RUNTIME_SEED": "0",
            "KIP_RUNTIME_HF_SECRET_NAME": "",
        }
    )
    source = RUNTIME_ROOT / "manifests" / template
    command = 'source "$1"; kip_runtime_render_file "$2" "$3"'
    result = subprocess.run(
        ["/bin/bash", "-c", command, "render", str(RENDER), str(source), str(destination)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return destination.read_text()


def test_deployment_exposes_stable_observability_dimensions(tmp_path: Path) -> None:
    deployment = render("deployment.yaml.tpl", tmp_path / "deployment.yaml")

    expected_labels = {
        "kube-inference-platform.io/runtime: vllm",
        "kube-inference-platform.io/deployment: vllm-qwen25-7b",
        "kube-inference-platform.io/model-profile: qwen2.5-7b-instruct",
        "kube-inference-platform.io/served-model: qwen2.5-7b-instruct",
        ("kube-inference-platform.io/model-revision: a09a35458c702b33eeacc393d103063234e8bc28"),
        "kube-inference-platform.io/worker-role: combined",
        'kube-inference-platform.io/replica: "0"',
        'kube-inference-platform.io/pipeline-rank: "0"',
        'kube-inference-platform.io/tensor-rank: "0"',
    }
    for label in expected_labels:
        assert deployment.count(label) == 2
    assert "kube-inference-platform.io/model-id: Qwen/Qwen2.5-7B-Instruct" in deployment
    assert "__" not in deployment


def test_service_is_discoverable_on_the_existing_vllm_metrics_port(tmp_path: Path) -> None:
    service = render("service.yaml.tpl", tmp_path / "service.yaml")

    assert 'prometheus.io/scrape: "true"' in service
    assert "prometheus.io/path: /metrics" in service
    assert 'prometheus.io/port: "8000"' in service
    assert "name: http" in service
    assert "port: 8000" in service
    assert "targetPort: http" in service
    assert "appProtocol: http" in service
    assert "kube-inference-platform.io/runtime: vllm" in service
    assert "kube-inference-platform.io/served-model: qwen2.5-7b-instruct" in service
    assert "kube-inference-platform.io/worker-role" not in service
    assert "kube-inference-platform.io/replica" not in service
    assert "kube-inference-platform.io/pipeline-rank" not in service
    assert "kube-inference-platform.io/tensor-rank" not in service
    assert "__" not in service
