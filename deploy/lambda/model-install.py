#!/usr/bin/env python3
"""Install one reviewed Qwen pool member on an exact, owned Lambda GPU node.

This is intentionally separate from the v0.1 single-node runtime and the Vast
pilot's fixed two-replica manifest. A model Deployment is KIP-owned; the future
llm-d InferencePool will only own routing resources.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "deploy/lambda/models.json"
VERSIONS = ROOT / "deploy/fleet/versions.json"
NAMESPACE = "kip-inference"
OWNER = "kube-inference-platform"
ROLE_MODEL = {
    "small-1": ("interactive", "qwen3-8b-small"),
    "small-large": ("interactive", "qwen3-8b-large"),
    "h100-control": ("interactive", "qwen3-8b-h100"),
    "large": ("reasoning-batch", "qwen3-next-80b"),
}


class InstallError(Exception):
    pass


def kubectl(*args: str, data: dict[str, Any] | None = None, timeout: int = 45) -> Any:
    command = ["kubectl", *args]
    try:
        result = subprocess.run(
            command,
            input=json.dumps(data) if data is not None else None,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError(f"kubectl failed ({type(error).__name__})") from None
    if result.returncode:
        raise InstallError(f"kubectl {' '.join(args[:3])} failed; inspect cluster events")
    if "-o" in args and "json" in args:
        if not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except ValueError as error:
            raise InstallError("Kubernetes returned invalid JSON") from error
    return result.stdout


def existing(kind: str, name: str, namespace: str | None = None) -> dict[str, Any] | None:
    args = ["-n", namespace] if namespace else []
    response = kubectl(*args, "get", kind, name, "--ignore-not-found", "-o", "json")
    return response or None


def hash_profile(model: dict[str, Any], role: str, image: str, node: str) -> str:
    value = {"model": model, "role": role, "image": image, "node": node}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def require_owned(current: dict[str, Any] | None, expected_hash: str | None = None) -> None:
    if current is None:
        return
    metadata = current.get("metadata", {})
    if metadata.get("labels", {}).get("app.kubernetes.io/managed-by") != OWNER:
        raise InstallError("target resource exists but is not KIP-owned")
    if (
        expected_hash is not None
        and metadata.get("annotations", {}).get("kube-inference-platform.io/profile-sha256")
        != expected_hash
    ):
        raise InstallError("existing resource has model, image, node, or profile drift")


def require_node(node_name: str, gpus: int) -> None:
    current = existing("node", node_name)
    if current is None:
        raise InstallError("reviewed GPU node is absent")
    if current.get("spec", {}).get("unschedulable") is True:
        raise InstallError("reviewed GPU node is cordoned")
    conditions = current.get("status", {}).get("conditions", [])
    if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
        raise InstallError("reviewed GPU node is not Ready")
    available = current.get("status", {}).get("allocatable", {}).get("nvidia.com/gpu", "0")
    if not isinstance(available, str) or not available.isdecimal() or int(available) < gpus:
        raise InstallError("reviewed node has insufficient whole GPU capacity")


def deployment(
    role: str, node: str, model: dict[str, Any], name: str, image: str, profile_hash: str
) -> dict[str, Any]:
    interactive = role != "large"
    gpu = model["gpusPerReplica"]
    labels = {
        "app.kubernetes.io/managed-by": OWNER,
        "app.kubernetes.io/name": "kip-lambda-model",
        "app.kubernetes.io/instance": name,
        "kube-inference-platform.io/model-pool": model["servedName"],
        "kube-inference-platform.io/served-model": model["servedName"],
        "kube-inference-platform.io/worker-role": "combined",
    }
    args = [
        model["model"],
        "--revision",
        model["revision"],
        "--tokenizer-revision",
        model["revision"],
        "--served-model-name",
        model["servedName"],
        "--dtype",
        model["dtype"],
        "--tensor-parallel-size",
        str(model["tensorParallelSize"]),
        "--pipeline-parallel-size",
        str(model["pipelineParallelSize"]),
        "--max-model-len",
        str(model["maxModelLen"]),
        "--gpu-memory-utilization",
        "0.90",
        "--max-num-seqs",
        "8",
        "--seed",
        "42",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    resources = {
        "requests": {
            "cpu": "8" if interactive else "32",
            "memory": "32Gi" if interactive else "256Gi",
            "nvidia.com/gpu": str(gpu),
        },
        "limits": {
            "cpu": "16" if interactive else "64",
            "memory": "96Gi" if interactive else "512Gi",
            "nvidia.com/gpu": str(gpu),
        },
    }
    cache_path = f"/var/lib/kube-inference-platform/model-cache/lambda/{model['servedName']}"
    container = {
        "name": "vllm",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["vllm", "serve"],
        "args": args,
        "ports": [{"name": "http", "containerPort": 8000}],
        "env": [
            {"name": "HF_HOME", "value": "/var/cache/kip/huggingface"},
            {"name": "VLLM_HTTP_TIMEOUT_KEEP_ALIVE", "value": "60"},
        ],
        "resources": resources,
        "volumeMounts": [
            {"name": "cache", "mountPath": "/var/cache/kip"},
            {"name": "shm", "mountPath": "/dev/shm"},
        ],
        "startupProbe": {
            "httpGet": {"path": "/health", "port": 8000},
            "periodSeconds": 10,
            "timeoutSeconds": 5,
            "failureThreshold": 180,
        },
        "readinessProbe": {
            "httpGet": {"path": "/health", "port": 8000},
            "periodSeconds": 5,
            "timeoutSeconds": 3,
            "failureThreshold": 3,
        },
        "livenessProbe": {
            "httpGet": {"path": "/health", "port": 8000},
            "periodSeconds": 15,
            "timeoutSeconds": 3,
            "failureThreshold": 4,
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": labels,
            "annotations": {
                "kube-inference-platform.io/profile-sha256": profile_hash,
                "kube-inference-platform.io/model-revision": model["revision"],
            },
        },
        "spec": {
            "replicas": 1,
            "progressDeadlineSeconds": 2700,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app.kubernetes.io/instance": name}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "nodeSelector": {"kubernetes.io/hostname": node},
                    "terminationGracePeriodSeconds": 120,
                    "containers": [container],
                    "volumes": [
                        {
                            "name": "cache",
                            "hostPath": {"path": cache_path, "type": "DirectoryOrCreate"},
                        },
                        {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "4Gi"}},
                    ],
                },
            },
        },
    }


def service(model: dict[str, Any]) -> dict[str, Any]:
    name = model["servedName"]
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {
                "app.kubernetes.io/managed-by": OWNER,
                "app.kubernetes.io/component": "llm-runtime",
                "kube-inference-platform.io/model-pool": name,
            },
        },
        "spec": {
            "type": "ClusterIP",
            "ports": [{"name": "http", "port": 8000, "targetPort": "http"}],
            "selector": {
                "kube-inference-platform.io/model-pool": name,
                "app.kubernetes.io/managed-by": OWNER,
            },
        },
    }


def install(role: str, node: str) -> None:
    if role not in ROLE_MODEL or not node.startswith("kip-lambda-") or len(node) > 63:
        raise InstallError("role or node name is not a reviewed Lambda placement")
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    versions = json.loads(VERSIONS.read_text(encoding="utf-8"))
    model = lock["pools"][ROLE_MODEL[role][0]]
    name = ROLE_MODEL[role][1]
    image = versions["inference"]["vllmLinuxAmd64Image"]
    if not image.startswith("docker.io/vllm/vllm-openai:v0.30.0@sha256:"):
        raise InstallError("reviewed vLLM image lock is missing")
    require_node(node, model["gpusPerReplica"])
    profile_hash = hash_profile(model, role, image, node)
    namespace = existing("namespace", NAMESPACE)
    require_owned(namespace)
    current = existing("deployment", name, NAMESPACE) if namespace else None
    require_owned(current, profile_hash)
    current_service = existing("service", model["servedName"], NAMESPACE) if namespace else None
    require_owned(current_service)
    if current is not None:
        template = current.get("spec", {}).get("template", {}).get("spec", {})
        containers = template.get("containers", [])
        if (
            current.get("spec", {}).get("replicas") != 1
            or template.get("nodeSelector", {}).get("kubernetes.io/hostname") != node
            or len(containers) != 1
            or containers[0].get("image") != image
            or containers[0].get("args")
            != deployment(role, node, model, name, image, profile_hash)["spec"]["template"]["spec"][
                "containers"
            ][0]["args"]
            or containers[0].get("resources", {}).get("limits", {}).get("nvidia.com/gpu")
            != str(model["gpusPerReplica"])
        ):
            raise InstallError("existing model Deployment has spec drift")
    if current_service is not None:
        spec = current_service.get("spec", {})
        if (
            spec.get("type") != "ClusterIP"
            or spec.get("selector") != service(model)["spec"]["selector"]
        ):
            raise InstallError("existing model Service has type or selector drift")
    if namespace is None:
        kubectl(
            "apply",
            "-f",
            "-",
            data={
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": NAMESPACE, "labels": {"app.kubernetes.io/managed-by": OWNER}},
            },
        )
    kubectl("apply", "-f", "-", data=deployment(role, node, model, name, image, profile_hash))
    kubectl("apply", "-f", "-", data=service(model))
    kubectl(
        "-n", NAMESPACE, "rollout", "status", f"deployment/{name}", "--timeout=45m", timeout=2800
    )
    print(
        f"PASS: {name} Ready on reviewed GPU node {node}; internal Service {model['servedName']}."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Install a reviewed Lambda model pool member")
    parser.add_argument("role", choices=sorted(ROLE_MODEL))
    parser.add_argument("--node", required=True)
    args = parser.parse_args()
    try:
        install(args.role, args.node)
    except (InstallError, OSError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
