#!/usr/bin/env python3
"""Require a real CUDA Pod on each reviewed east-region GPU worker."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "deploy/gpu/versions.env"
ROLE_LABEL = "kube-inference-platform.io/lambda-role"
EXPECTED = {"small-1": 1, "large": 8}
PODS = {role: f"kip-lambda-gpu-smoke-{role}" for role in EXPECTED}


class SmokeError(Exception):
    pass


def kubectl(*args: str, data: dict | None = None, timeout: int = 60) -> str:
    result = subprocess.run(
        ["kubectl", *args],
        input=json.dumps(data) if data else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise SmokeError(f"kubectl {' '.join(args[:3])} failed; inspect Pod events")
    return result.stdout


def pinned_image() -> str:
    match = re.search(r'^KIP_CUDA_SMOKE_IMAGE="([^"\n]+)"$', VERSIONS.read_text(), re.MULTILINE)
    if not match or "@sha256:" not in match.group(1):
        raise SmokeError("digest-pinned CUDA smoke image is missing")
    return match.group(1)


def owned_nodes() -> dict[str, str]:
    try:
        items = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    except (ValueError, KeyError, TypeError) as error:
        raise SmokeError("Kubernetes node inventory is malformed") from error
    roles: dict[str, str] = {}
    for node in items:
        role = node.get("metadata", {}).get("labels", {}).get(ROLE_LABEL)
        if role not in EXPECTED:
            continue
        name = node["metadata"]["name"]
        if role in roles or not node["metadata"].get("annotations", {}).get(
            "kube-inference-platform.io/lambda-instance-id"
        ):
            raise SmokeError("GPU worker ownership metadata is missing or duplicated")
        capacity = node.get("status", {}).get("capacity", {}).get("nvidia.com/gpu")
        allocatable = node.get("status", {}).get("allocatable", {}).get("nvidia.com/gpu")
        if capacity != str(EXPECTED[role]) or allocatable != str(EXPECTED[role]):
            raise SmokeError(f"{role} does not advertise exactly {EXPECTED[role]} whole GPUs")
        roles[role] = name
    if set(roles) != set(EXPECTED):
        raise SmokeError("both owned GPU worker roles must be present")
    return roles


def smoke(role: str, node: str, image: str) -> None:
    pod_name = PODS[role]
    existing = kubectl("get", "pod", pod_name, "--ignore-not-found", "-o", "json")
    if existing.strip():
        raise SmokeError(f"Pod/{pod_name} already exists; refusing replacement")
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "labels": {"app.kubernetes.io/managed-by": "kube-inference-platform"},
        },
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": 300,
            "nodeSelector": {"kubernetes.io/hostname": node},
            "containers": [
                {
                    "name": "nvidia-smi",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["nvidia-smi"],
                    "args": [
                        "--query-gpu=index,name,uuid,driver_version,memory.total",
                        "--format=csv,noheader",
                    ],
                    "resources": {
                        "requests": {"nvidia.com/gpu": "1"},
                        "limits": {"nvidia.com/gpu": "1"},
                    },
                }
            ],
        },
    }
    kubectl("apply", "-f", "-", data=pod)
    try:
        kubectl(
            "wait",
            "--for=jsonpath={.status.phase}=Succeeded",
            f"pod/{pod_name}",
            "--timeout=5m",
            timeout=330,
        )
        current = json.loads(kubectl("get", "pod", pod_name, "-o", "json"))
        if current.get("spec", {}).get("nodeName") != node:
            raise SmokeError(f"Pod/{pod_name} scheduled on the wrong node")
        output = kubectl("logs", pod_name)
        if not any(
            re.match(r"^\s*\d+\s*,[^,]+,\s*GPU-[^,]+,[^,]+,[^,]+$", row)
            for row in output.splitlines()
        ):
            raise SmokeError(f"Pod/{pod_name} did not produce a GPU inventory row")
        print(f"PASS: {role} ran nvidia-smi in a temporary CUDA Pod on {node}.")
    finally:
        kubectl("delete", "pod", pod_name, "--wait=true", "--timeout=2m", timeout=150)


def main() -> int:
    try:
        image = pinned_image()
        for role, node in owned_nodes().items():
            smoke(role, node, image)
    except (SmokeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
