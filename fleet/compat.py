"""Strict offline validation of the pilot's separate immutable artifact contract."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .vast import FleetError

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "deploy" / "fleet" / "versions.json"
SHA256 = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")


def _env_value(path: Path, key: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(rf'{re.escape(key)}="([^"]+)"', line)
        if match:
            return match.group(1)
    raise FleetError(f"missing base version pin {key}")


def load_compatibility_lock(path: Path = LOCK_PATH) -> dict[str, Any]:
    """Validate shape, digests, and alignment with the untouched v0.1 base pins."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FleetError("pilot compatibility lock is missing or malformed") from error
    if not isinstance(data, dict) or set(data) != {
        "schemaVersion",
        "status",
        "base",
        "gpu",
        "inference",
        "gateway",
    }:
        raise FleetError("pilot compatibility lock has unexpected fields")
    if data["schemaVersion"] != 1 or data["status"] != "artifact-locked-target-unvalidated":
        raise FleetError("pilot compatibility lock has an invalid schema/status")
    expected = {
        "kubernetes": ("cluster/versions.env", "KIP_KUBERNETES_VERSION"),
        "containerd": ("cluster/versions.env", "KIP_CONTAINERD_VERSION"),
        "cilium": ("cluster/versions.env", "KIP_CILIUM_VERSION"),
        "gpuOperator": ("gpu/versions.env", "KIP_GPU_OPERATOR_VERSION"),
    }
    if not isinstance(data["base"], dict) or set(data["base"]) != set(expected):
        raise FleetError("pilot base version fields differ")
    for key, (relative, variable) in expected.items():
        if data["base"][key] != _env_value(ROOT / "deploy" / relative, variable):
            raise FleetError(f"pilot {key} pin drifts from v0.1 base")
    gpu = data["gpu"]
    if not isinstance(gpu, dict) or set(gpu) != {
        "driverMode",
        "driverVersion",
        "driverMajorMinimum",
        "driverMajorMaximumExclusive",
        "cudaRuntime",
    }:
        raise FleetError("pilot GPU fields differ")
    if (
        gpu["driverMode"] != "preinstalled"
        or gpu["cudaRuntime"] != "13.0"
        or gpu["driverVersion"] != "580.178.04"
        or gpu["driverMajorMinimum"] != 580
        or gpu["driverMajorMaximumExclusive"] != 581
    ):
        raise FleetError("pilot requires the GPU-Operator-validated R580/CUDA 13 path")
    try:
        major = int(gpu["driverVersion"].split(".", 1)[0])
    except (AttributeError, ValueError) as error:
        raise FleetError("pilot driver version is invalid") from error
    if not (gpu["driverMajorMinimum"] <= major < gpu["driverMajorMaximumExclusive"]):
        raise FleetError("pilot driver version is outside the pinned R580 range")
    inference = data["inference"]
    gateway = data["gateway"]
    if not isinstance(inference, dict) or not isinstance(gateway, dict):
        raise FleetError("pilot inference/gateway sections must be objects")
    required_inference = {
        "vllmVersion",
        "vllmLinuxAmd64Image",
        "llmdVersion",
        "eppLinuxAmd64Image",
        "routerGatewayChart",
        "routerGatewayChartVersion",
        "routerGatewayChartDigest",
    }
    required_gateway = {
        "gatewayApiVersion",
        "gatewayApiStandardManifestSha256",
        "inferenceExtensionVersion",
        "inferenceExtensionManifestSha256",
        "envoyGatewayVersion",
        "envoyGatewayLinuxAmd64Image",
        "envoyProxyLinuxAmd64Image",
        "envoyRateLimitLinuxAmd64Image",
        "envoyGatewayChartDigest",
        "envoyGatewayCrdsChartDigest",
        "envoyAiGatewayVersion",
        "envoyAiGatewayControllerLinuxAmd64Image",
        "envoyAiGatewayExtprocLinuxAmd64Image",
        "envoyAiGatewayChartDigest",
        "envoyAiGatewayCrdsChartDigest",
    }
    if set(inference) != required_inference or set(gateway) != required_gateway:
        raise FleetError("pilot inference/gateway lock fields differ")
    if inference["vllmVersion"] != "v0.30.0" or inference["llmdVersion"] != "v0.10.0":
        raise FleetError("pilot inference versions differ from reviewed combination")
    expected_images = {
        "vllmLinuxAmd64Image": "docker.io/vllm/vllm-openai:v0.30.0@sha256:",
        "eppLinuxAmd64Image": ("ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.10.0@sha256:"),
    }
    for key, prefix in expected_images.items():
        if not isinstance(inference[key], str) or not inference[key].startswith(prefix):
            raise FleetError(f"{key} must use the reviewed official image and tag")
    if not isinstance(inference["routerGatewayChart"], str) or not inference[
        "routerGatewayChart"
    ].startswith("oci://ghcr.io/llm-d/"):
        raise FleetError("router chart must come from the official llm-d registry")
    if inference["routerGatewayChartVersion"] != "v0.10.0":
        raise FleetError("router chart version differs from reviewed pilot")
    expected_gateway_versions = {
        "gatewayApiVersion": "v1.5.1",
        "inferenceExtensionVersion": "v1.5.0",
        "envoyGatewayVersion": "v1.8.1",
        "envoyAiGatewayVersion": "v0.7.0",
    }
    for key, version in expected_gateway_versions.items():
        if gateway[key] != version:
            raise FleetError(f"{key} differs from reviewed pilot")
    image = gateway["envoyGatewayLinuxAmd64Image"]
    if (
        not isinstance(image, str)
        or not image.startswith("docker.io/envoyproxy/gateway:v1.8.1@sha256:")
        or not SHA256.fullmatch(image.rsplit("@", 1)[-1])
    ):
        raise FleetError("Envoy Gateway image must use the reviewed immutable amd64 pin")
    proxy_image = gateway["envoyProxyLinuxAmd64Image"]
    if (
        not isinstance(proxy_image, str)
        or not proxy_image.startswith("docker.io/envoyproxy/envoy:distroless-v1.38.1@sha256:")
        or not SHA256.fullmatch(proxy_image.rsplit("@", 1)[-1])
    ):
        raise FleetError("Envoy data-plane image must use the reviewed immutable amd64 pin")
    rate_image = gateway["envoyRateLimitLinuxAmd64Image"]
    if (
        not isinstance(rate_image, str)
        or not rate_image.startswith("docker.io/envoyproxy/ratelimit:ff287602@sha256:")
        or not SHA256.fullmatch(rate_image.rsplit("@", 1)[-1])
    ):
        raise FleetError("Envoy rate-limit image must use the reviewed immutable amd64 pin")
    for key, name in {
        "envoyAiGatewayControllerLinuxAmd64Image": "ai-gateway-controller",
        "envoyAiGatewayExtprocLinuxAmd64Image": "ai-gateway-extproc",
    }.items():
        image = gateway[key]
        if (
            not isinstance(image, str)
            or not image.startswith(f"docker.io/envoyproxy/{name}:v0.7.0@sha256:")
            or not SHA256.fullmatch(image.rsplit("@", 1)[-1])
        ):
            raise FleetError(f"{key} must use the reviewed immutable amd64 pin")
    for key, value in {**inference, **gateway}.items():
        if (key.endswith("Digest") or key.endswith("Sha256")) and (
            not isinstance(value, str) or not SHA256.fullmatch(value)
        ):
            raise FleetError(f"invalid immutable digest: {key}")
    for key in ("vllmLinuxAmd64Image", "eppLinuxAmd64Image"):
        value = inference[key]
        if not isinstance(value, str) or not SHA256.fullmatch(value.rsplit("@", 1)[-1]):
            raise FleetError(f"{key} must carry an immutable image digest")
    return data
