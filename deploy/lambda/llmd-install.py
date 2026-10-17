#!/usr/bin/env python3
"""Install two private llm-d v0.10 pools over the reviewed Lambda Qwen Deployments.

KIP owns the model Pods. llm-d owns InferencePools/EPP/HTTPRoutes; Envoy AI
Gateway owns the proxy. Routing initially requires an internal explicit model
header; trusted tenant auth and body-derived IPP routing are separate gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FLEET = ROOT / "deploy/fleet"
LOCK = json.loads((FLEET / "versions.json").read_text())
OWNER = "kube-inference-platform"
EPP_TAG = "v0.10.0@sha256:9862ae38658824b2560c547237363fe21f269e649b873a4ee84fe3137b554ba9"
CONTROL = re.compile(r"^kip-lambda-[0-9a-f]{8}-control-speech$")
POOLS = {
    "qwen3-8b": ("kip-qwen3-8b", "qwen3-8b-small", "qwen3-8b-large"),
    "qwen3-next-80b-a3b-instruct": ("kip-qwen3-next", "qwen3-next-80b"),
}


class RoutingError(Exception):
    pass


def run(
    args: list[str], *, data: str | None = None, timeout: int = 600, include_stderr: bool = False
) -> str:
    try:
        response = subprocess.run(
            args, input=data, text=True, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RoutingError(f"{args[0]} failed ({type(error).__name__})") from None
    if response.returncode:
        raise RoutingError(f"{args[0]} {' '.join(args[1:4])} failed; inspect cluster events")
    return response.stdout + response.stderr if include_stderr else response.stdout


def kjson(*args: str) -> dict[str, Any] | None:
    raw = run(["kubectl", *args, "-o", "json"])
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError as error:
        raise RoutingError("Kubernetes returned malformed JSON") from error


def owned(kind: str, name: str, namespace: str | None = None) -> dict[str, Any] | None:
    scope = ["-n", namespace] if namespace else []
    current = kjson(*scope, "get", kind, name, "--ignore-not-found")
    if (
        current
        and current.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/managed-by")
        != OWNER
    ):
        raise RoutingError(f"{kind}/{name} exists without KIP ownership")
    return current


def namespace(name: str) -> None:
    if owned("namespace", name) is None:
        run(
            ["kubectl", "apply", "-f", "-"],
            data=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": name, "labels": {"app.kubernetes.io/managed-by": OWNER}},
                }
            ),
        )


def verify_manifest(url: str, digest: str, target: Path) -> None:
    run(["curl", "-fsSL", "--retry", "3", "--max-time", "120", url, "-o", str(target)], timeout=150)
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise RoutingError("official CRD manifest digest mismatch")


def verify_chart(ref: str, version: str, digest: str, directory: Path) -> None:
    output = run(
        ["helm", "pull", ref, "--version", version, "--destination", str(directory)],
        timeout=180,
        include_stderr=True,
    )
    if f"Digest: {digest}" not in output:
        # Helm sometimes reports OCI digest on stderr, so also verify with a
        # separate show query only if the output format changes. Stop for now.
        raise RoutingError("official Helm chart digest mismatch or missing")


def apply_crds(manifest: Path) -> None:
    resources = run(
        ["kubectl", "create", "--dry-run=client", "-f", str(manifest), "-o", "name"]
    ).splitlines()
    for resource in resources:
        if resource.startswith("customresourcedefinition.apiextensions.k8s.io/"):
            owned("crd", resource.split("/", 1)[1])
    run(
        ["kubectl", "apply", "--server-side", "--field-manager=kip-lambda", "-f", str(manifest)],
        timeout=180,
    )
    for resource in resources:
        if resource.startswith("customresourcedefinition.apiextensions.k8s.io/"):
            run(
                [
                    "kubectl",
                    "label",
                    resource,
                    f"app.kubernetes.io/managed-by={OWNER}",
                    "--overwrite",
                ]
            )


def chart_release(
    name: str,
    namespace_name: str,
    ref: str,
    digest: str,
    values: dict[str, Any],
    work: Path,
    *,
    previous_values: list[dict[str, Any]] | None = None,
) -> None:
    def value_hash(item: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    values_hash = value_hash(values)
    releases = json.loads(run(["helm", "list", "--all-namespaces", "--all", "-o", "json"]))
    matches = [entry for entry in releases if entry.get("name") == name]
    if len(matches) > 1 or (matches and matches[0].get("namespace") != namespace_name):
        raise RoutingError(f"Helm release {name} has foreign namespace or duplicate")
    if matches:
        current = json.loads(
            run(["helm", "get", "values", name, "-n", namespace_name, "-o", "json"])
        )
        kip = current.get("kip", {})
        allowed = [{"owner": OWNER, "chartDigest": digest, "valuesSha256": values_hash}]
        for previous in previous_values or []:
            allowed.append(
                {"owner": OWNER, "chartDigest": digest, "valuesSha256": value_hash(previous)}
            )
        if kip not in allowed:
            raise RoutingError(f"Helm release {name} values or artifact drifted")
    values_file = work / f"{name}.json"
    values_file.write_text(json.dumps(values, sort_keys=True))
    run(
        [
            "helm",
            "upgrade",
            "--install",
            name,
            f"{ref}@{digest}",
            "--namespace",
            namespace_name,
            "--values",
            str(values_file),
            "--set-string",
            f"kip.owner={OWNER}",
            "--set-string",
            f"kip.chartDigest={digest}",
            "--set-string",
            f"kip.valuesSha256={values_hash}",
            "--wait",
            "--timeout",
            "10m",
        ],
        timeout=660,
    )


def pool_values(
    name: str, control: str, *, flow: bool = False, large_max_concurrency: int = 4
) -> dict[str, Any]:
    if large_max_concurrency not in {4, 6, 8}:
        raise RoutingError("large-pool concurrency must be a reviewed variant")
    plugins = """apiVersion: llm-d.ai/v1alpha1
kind: EndpointPickerConfig
plugins:
  - type: approx-prefix-cache-producer
  - type: prefix-cache-scorer
  - type: kv-cache-utilization-scorer
  - type: queue-scorer
schedulingProfiles:
  - name: default
    plugins:
      - pluginRef: queue-scorer
        weight: 2
      - pluginRef: kv-cache-utilization-scorer
        weight: 2
      - pluginRef: prefix-cache-scorer
        weight: 3
"""
    if flow:
        max_concurrency = 6 if name == "qwen3-8b" else large_max_concurrency
        plugins = f"""apiVersion: llm-d.ai/v1alpha1
kind: EndpointPickerConfig
featureGates:
  - flowControl
plugins:
  - type: approx-prefix-cache-producer
  - type: prefix-cache-scorer
  - type: kv-cache-utilization-scorer
  - type: queue-scorer
  - type: weighted-random-picker
  - type: round-robin-fairness-policy
  - type: fcfs-ordering-policy
  - type: concurrency-detector
    parameters:
      maxConcurrency: {max_concurrency}
      concurrencyMode: requests
      headroom: 0.0
schedulingProfiles:
  - name: default
    plugins:
      - pluginRef: queue-scorer
        weight: 2
      - pluginRef: kv-cache-utilization-scorer
        weight: 2
      - pluginRef: prefix-cache-scorer
        weight: 3
      - pluginRef: weighted-random-picker
flowControl:
  maxBytes: 1Gi
  maxRequests: "200"
  defaultRequestTTL: 60s
  saturationDetector:
    pluginRef: concurrency-detector
  priorityBands:
    - priority: 100
      maxRequests: "60"
      fairnessPolicyRef: round-robin-fairness-policy
      orderingPolicyRef: fcfs-ordering-policy
    - priority: 0
      maxRequests: "100"
      fairnessPolicyRef: round-robin-fairness-policy
      orderingPolicyRef: fcfs-ordering-policy
    - priority: -10
      maxRequests: "40"
      fairnessPolicyRef: round-robin-fairness-policy
      orderingPolicyRef: fcfs-ordering-policy
"""
    values = {
        "provider": {"name": "none"},
        "router": {
            "modelServers": {
                "type": "vllm",
                "protocol": "http",
                "matchLabels": {"kube-inference-platform.io/model-pool": name},
                "targetPorts": [{"number": 8000}],
            },
            "inferencePool": {"create": True, "failureMode": "FailClose"},
            "epp": {
                "replicas": 1,
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {
                                            "key": "kubernetes.io/hostname",
                                            "operator": "In",
                                            "values": [control],
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                },
                "tolerations": [
                    {
                        "key": "node-role.kubernetes.io/control-plane",
                        "operator": "Exists",
                        "effect": "NoSchedule",
                    }
                ],
                "pluginsConfigFile": "kip-lambda-plugins.yaml",
                "pluginsCustomConfig": {"kip-lambda-plugins.yaml": plugins},
                "image": {
                    "registry": "ghcr.io/llm-d",
                    "repository": "llm-d-router-endpoint-picker",
                    "tag": EPP_TAG,
                    "pullPolicy": "IfNotPresent",
                },
                "resources": {
                    "requests": {"cpu": "2", "memory": "4Gi"},
                    "limits": {"cpu": "8", "memory": "8Gi"},
                },
            },
        },
        "httpRoute": {
            "create": True,
            "inferenceGatewayName": "kip-inference-gateway",
            "baseModel": name,
            "requestTimeout": "300s",
        },
    }
    if flow:
        # The chart does not roll its Deployment when only its ConfigMap changes.
        # A content-derived Pod annotation forces the endpoint picker to load
        # the new flow-control configuration before Helm --wait succeeds.
        values["router"]["epp"]["podAnnotations"] = {
            "kube-inference-platform.io/plugins-sha256": hashlib.sha256(
                plugins.encode()
            ).hexdigest()
        }
    return values


def require_models() -> None:
    for members in POOLS.values():
        for name in members[1:]:
            current = owned("deployment", name, "kip-inference")
            if current is None or current.get("status", {}).get("readyReplicas") != 1:
                raise RoutingError(f"model Deployment/{name} is not Ready")


def flow_enabled() -> bool:
    policy = owned("securitypolicy", "kip-tenant-auth", "kip-inference")
    if policy is None:
        return False
    if policy.get("spec", {}).get("extAuth", {}).get("failOpen") is not False:
        raise RoutingError("tenant SecurityPolicy is not fail-closed")
    if not any(
        cond.get("type") == "Accepted" and cond.get("status") == "True"
        for parent in policy.get("status", {}).get("ancestors", [])
        for cond in parent.get("conditions", [])
    ):
        raise RoutingError("tenant SecurityPolicy is not Accepted")
    for pool in ("kip-qwen3-8b", "kip-qwen3-next"):
        for band in ("voice", "interactive", "batch"):
            if owned("inferenceobjective", f"{pool}-{band}", "kip-inference") is None:
                raise RoutingError(f"tenant objective {pool}-{band} is absent")
    return True


def install() -> None:
    control = run(["hostname"]).strip()
    if not CONTROL.fullmatch(control) or os.geteuid() != 0:
        raise RoutingError("run as root on the owned Lambda east control plane")
    os.environ["KUBECONFIG"] = "/etc/kubernetes/admin.conf"
    require_models()
    flow = flow_enabled()
    gateway = LOCK["gateway"]
    inference = LOCK["inference"]
    with tempfile.TemporaryDirectory(prefix="kip-lambda-llmd-") as folder:
        work = Path(folder)
        gateway_api = work / "gateway-api.yaml"
        extension = work / "inference-extension.yaml"
        verify_manifest(
            "https://github.com/kubernetes-sigs/gateway-api/releases/download/"
            f"{gateway['gatewayApiVersion']}/standard-install.yaml",
            gateway["gatewayApiStandardManifestSha256"],
            gateway_api,
        )
        verify_manifest(
            "https://github.com/kubernetes-sigs/gateway-api-inference-extension/"
            f"releases/download/{gateway['inferenceExtensionVersion']}/v1-manifests.yaml",
            gateway["inferenceExtensionManifestSha256"],
            extension,
        )
        charts = [
            (
                "oci://docker.io/envoyproxy/gateway-crds-helm",
                gateway["envoyGatewayVersion"],
                gateway["envoyGatewayCrdsChartDigest"],
            ),
            (
                "oci://docker.io/envoyproxy/gateway-helm",
                gateway["envoyGatewayVersion"],
                gateway["envoyGatewayChartDigest"],
            ),
            (
                "oci://docker.io/envoyproxy/ai-gateway-crds-helm",
                gateway["envoyAiGatewayVersion"],
                gateway["envoyAiGatewayCrdsChartDigest"],
            ),
            (
                "oci://docker.io/envoyproxy/ai-gateway-helm",
                gateway["envoyAiGatewayVersion"],
                gateway["envoyAiGatewayChartDigest"],
            ),
            (
                inference["routerGatewayChart"],
                inference["routerGatewayChartVersion"],
                inference["routerGatewayChartDigest"],
            ),
        ]
        for ref, version, digest in charts:
            verify_chart(ref, version, digest, work)
        print("PASS: Gateway API, Envoy, and llm-d artifacts match pinned digests.", flush=True)
        apply_crds(gateway_api)
        apply_crds(extension)
        crd_yaml = run(
            [
                "helm",
                "template",
                "eg-crds",
                f"oci://docker.io/envoyproxy/gateway-crds-helm@{gateway['envoyGatewayCrdsChartDigest']}",
                "--set",
                "crds.gatewayAPI.enabled=false",
                "--set",
                "crds.envoyGateway.enabled=true",
            ]
        )
        crd_file = work / "envoy-crds.yaml"
        crd_file.write_text(crd_yaml)
        apply_crds(crd_file)
        namespace("envoy-gateway-system")
        namespace("envoy-ai-gateway-system")
        namespace("kip-inference")
        run(["kubectl", "apply", "-f", str(FLEET / "gateway-rbac.yaml")])
        # Reviewed controller values were already validated against exact image digests.
        for release, ns, ref, digest, path in (
            (
                "eg",
                "envoy-gateway-system",
                charts[1][0],
                charts[1][2],
                FLEET / "envoy-gateway-values.yaml",
            ),
            ("envoy-ai-gateway-crd", "envoy-ai-gateway-system", charts[2][0], charts[2][2], None),
            (
                "envoy-ai-gateway",
                "envoy-ai-gateway-system",
                charts[3][0],
                charts[3][2],
                FLEET / "envoy-ai-gateway-values.yaml",
            ),
        ):
            args = [
                "helm",
                "upgrade",
                "--install",
                release,
                f"{ref}@{digest}",
                "--namespace",
                ns,
                "--wait",
                "--timeout",
                "10m",
            ]
            if release == "eg":
                args.append("--skip-crds")
            if path:
                args.extend(["--values", str(path)])
            args.extend(
                ["--set-string", f"kip.owner={OWNER}", "--set-string", f"kip.chartDigest={digest}"]
            )
            existing_release = json.loads(
                run(["helm", "list", "--all-namespaces", "--all", "-o", "json"])
            )
            if any(item.get("name") == release for item in existing_release):
                current = json.loads(
                    run(["helm", "get", "values", release, "-n", ns, "-o", "json"])
                )
                if (
                    current.get("kip", {}).get("owner") != OWNER
                    or current["kip"].get("chartDigest") != digest
                ):
                    raise RoutingError(f"existing {release} Helm release is unmanaged")
            run(args, timeout=660)
        for current in (kjson("get", "crd"),):
            for crd in current.get("items", []):
                if crd.get("spec", {}).get("group") == "aigateway.envoyproxy.io":
                    run(
                        [
                            "kubectl",
                            "label",
                            "crd",
                            crd["metadata"]["name"],
                            f"app.kubernetes.io/managed-by={OWNER}",
                            "--overwrite",
                        ]
                    )
        gateway_yaml = (FLEET / "gateway.yaml").read_text().replace("kip-control", control)
        gateway_file = work / "gateway.yaml"
        gateway_file.write_text(gateway_yaml)
        for kind, name, ns in (
            ("envoyproxy", "kip-private-proxy", "envoy-gateway-system"),
            ("gatewayclass", "kip-private-inference", None),
            ("gateway", "kip-inference-gateway", "kip-inference"),
        ):
            owned(kind, name, ns)
        run(["kubectl", "apply", "-f", str(gateway_file)])
        for name, members in POOLS.items():
            release = members[0]
            desired = pool_values(name, control, flow=flow)
            earlier: list[dict[str, Any]] = []
            if flow:
                earlier.append(pool_values(name, control))
                pre_annotation = json.loads(json.dumps(desired))
                pre_annotation["router"]["epp"].pop("podAnnotations")
                earlier.append(pre_annotation)
            chart_release(
                release,
                "kip-inference",
                inference["routerGatewayChart"],
                inference["routerGatewayChartDigest"],
                desired,
                work,
                previous_values=earlier,
            )
            print(
                f"PASS: llm-d InferencePool/{release} and private model-header route"
                f" ({'tenant flow control' if flow else 'baseline routing'}).",
                flush=True,
            )
        current = kjson("get", "services", "--all-namespaces")
        if any(
            item.get("spec", {}).get("type") != "ClusterIP"
            for item in current["items"]
            if item.get("metadata", {}).get("namespace")
            in {"envoy-gateway-system", "envoy-ai-gateway-system"}
        ):
            raise RoutingError("an Envoy Service is not internal-only")
        print("PASS: two llm-d pools installed; Gateway services remain internal-only.")


def main() -> int:
    try:
        install()
    except (RoutingError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
