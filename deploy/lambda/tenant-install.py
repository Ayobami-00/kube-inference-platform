#!/usr/bin/env python3
"""Install trusted A/B/C identity across both private Lambda llm-d routes."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FLEET = ROOT / "deploy/fleet"
HERE = Path(__file__).resolve().parent
KEYS = Path("/var/lib/kube-inference-platform/tenant-keys.json")
OWNER = "kube-inference-platform"
CONTROL = re.compile(r"^kip-lambda-[0-9a-f]{8}-control-speech$")
POOLS = ("kip-qwen3-8b", "kip-qwen3-next")
BANDS = {"voice": 100, "interactive": 0, "batch": -10}


class TenantError(Exception):
    pass


def run(args: list[str], *, data: str | None = None, timeout: int = 300) -> str:
    try:
        result = subprocess.run(
            args, input=data, text=True, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TenantError(f"{args[0]} failed ({type(error).__name__})") from None
    if result.returncode:
        raise TenantError(f"{args[0]} {' '.join(args[1:4])} failed; inspect cluster events")
    return result.stdout


def kjson(*args: str) -> dict[str, Any] | None:
    output = run(["kubectl", *args, "-o", "json"])
    return json.loads(output) if output.strip() else None


def owned(kind: str, name: str, namespace: str | None = None) -> dict[str, Any] | None:
    scope = ["-n", namespace] if namespace else []
    item = kjson(*scope, "get", kind, name, "--ignore-not-found")
    if (
        item
        and item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/managed-by") != OWNER
    ):
        raise TenantError(f"{kind}/{name} is not KIP-owned")
    return item


def require_gateway() -> str:
    gateway = owned("gateway", "kip-inference-gateway", "kip-inference")
    if not gateway or not any(
        item.get("type") == "Programmed" and item.get("status") == "True"
        for item in gateway.get("status", {}).get("conditions", [])
    ):
        raise TenantError("private Gateway is not Programmed")
    for name in POOLS:
        release = json.loads(
            run(["helm", "get", "values", name, "-n", "kip-inference", "-o", "json"])
        )
        if release.get("kip", {}).get("owner") != OWNER:
            raise TenantError(f"llm-d Helm release/{name} is unmanaged")
        pool = kjson("-n", "kip-inference", "get", "inferencepool", name, "--ignore-not-found")
        if not pool:
            raise TenantError(f"InferencePool/{name} is absent")
        route = kjson("-n", "kip-inference", "get", "httproute", name, "--ignore-not-found")
        for kind, item in (("InferencePool", pool), ("HTTPRoute", route)):
            if item and (
                item.get("metadata", {}).get("annotations", {}).get("meta.helm.sh/release-name")
                != name
                or item.get("metadata", {})
                .get("annotations", {})
                .get("meta.helm.sh/release-namespace")
                != "kip-inference"
            ):
                raise TenantError(f"{kind}/{name} is not owned by its reviewed Helm release")
        if not route or not any(
            cond.get("type") == "Accepted" and cond.get("status") == "True"
            for parent in route.get("status", {}).get("parents", [])
            for cond in parent.get("conditions", [])
        ):
            raise TenantError(f"HTTPRoute/{name} is not Accepted")
    addresses = gateway.get("status", {}).get("addresses", [])
    if not addresses or not isinstance(addresses[0].get("value"), str):
        raise TenantError("private Gateway has no address")
    return addresses[0]["value"]


def credentials() -> bytes:
    run(["python3", str(FLEET / "tenant-keys.py"), str(KEYS)])
    if KEYS.is_symlink() or KEYS.stat().st_mode & 0o077:
        raise TenantError("private tenant key file has unsafe permissions")
    raw = KEYS.read_bytes()
    module_path = HERE / "tenant-auth.py"
    spec = importlib.util.spec_from_file_location("kip_lambda_tenant_auth", module_path)
    if spec is None or spec.loader is None:
        raise TenantError("tenant authorization module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.load_credentials(KEYS)
    return raw


def install_auth(control: str, raw: bytes) -> None:
    namespace = "kip-inference"
    source = (HERE / "tenant-auth.py").read_text(encoding="utf-8")
    digest = hashlib.sha256(source.encode()).hexdigest()
    secret = owned("secret", "kip-tenant-credentials", namespace)
    if secret:
        encoded = secret.get("data", {}).get("tokens.json", "")
        if (
            hashlib.sha256(base64.b64decode(encoded, validate=True)).digest()
            != hashlib.sha256(raw).digest()
        ):
            raise TenantError("owned tenant credentials differ from the private local file")
    else:
        run(
            ["kubectl", "apply", "-f", "-"],
            data=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "type": "Opaque",
                    "metadata": {
                        "name": "kip-tenant-credentials",
                        "namespace": namespace,
                        "labels": {"app.kubernetes.io/managed-by": OWNER},
                    },
                    "stringData": {"tokens.json": raw.decode()},
                }
            ),
        )
    current = owned("configmap", "kip-tenant-auth-code", namespace)
    if current:
        if (
            hashlib.sha256(current.get("data", {}).get("auth.py", "").encode()).hexdigest()
            != digest
        ):
            raise TenantError("authorization source has drifted")
    else:
        run(
            ["kubectl", "apply", "-f", "-"],
            data=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": "kip-tenant-auth-code",
                        "namespace": namespace,
                        "labels": {"app.kubernetes.io/managed-by": OWNER},
                    },
                    "data": {"auth.py": source},
                }
            ),
        )
    deployment = owned("deployment", "kip-tenant-auth", namespace)
    service = owned("service", "kip-tenant-auth", namespace)
    if deployment:
        template = deployment["spec"]["template"]
        if (
            template["metadata"]
            .get("annotations", {})
            .get("kube-inference-platform.io/auth-script-sha256")
            != digest
            or template["spec"].get("nodeSelector", {}).get("kubernetes.io/hostname") != control
        ):
            raise TenantError("authorization Deployment has drifted")
    if service and service.get("spec", {}).get("type") != "ClusterIP":
        raise TenantError("authorization Service is not private")
    manifest = (FLEET / "tenant-auth.yaml").read_text(encoding="utf-8")
    marker = "kube-inference-platform.io/fleet-role: control-plane"
    if marker not in manifest:
        raise TenantError("authorization placement template drifted")
    manifest = manifest.replace(marker, f"kubernetes.io/hostname: {control}")
    manifest = manifest.replace("__AUTH_SCRIPT_SHA256__", digest)
    run(["kubectl", "apply", "-f", "-"], data=manifest)
    run(
        [
            "kubectl",
            "-n",
            namespace,
            "rollout",
            "status",
            "deployment/kip-tenant-auth",
            "--timeout=5m",
        ],
        timeout=330,
    )


def install_objectives() -> None:
    lock = json.loads((FLEET / "flow-control-lock.json").read_text())
    with tempfile.TemporaryDirectory(prefix="kip-lambda-objectives-") as folder:
        archive = Path(folder) / "manifests.yaml"
        run(
            [
                "curl",
                "-fsSL",
                "--retry",
                "3",
                "--max-time",
                "120",
                lock["inferenceObjectiveManifestUrl"],
                "-o",
                str(archive),
            ],
            timeout=150,
        )
        if (
            hashlib.sha256(archive.read_bytes()).hexdigest()
            != lock["inferenceObjectiveManifestSha256"]
        ):
            raise TenantError("official llm-d objective CRD digest mismatch")
        output = run(["kubectl", "create", "--dry-run=client", "-f", str(archive), "-o", "json"])
        decoder = json.JSONDecoder()
        items: list[dict[str, Any]] = []
        while output.strip():
            item, end = decoder.raw_decode(output.lstrip())
            items.append(item)
            output = output.lstrip()[end:]
        matches = [
            item
            for item in items
            if item.get("kind") == "CustomResourceDefinition"
            and item.get("metadata", {}).get("name") == "inferenceobjectives.llm-d.ai"
        ]
        if len(matches) != 1:
            raise TenantError("pinned llm-d InferenceObjective CRD is missing")
        owned("crd", "inferenceobjectives.llm-d.ai")
        matches[0].setdefault("metadata", {}).setdefault("labels", {})[
            "app.kubernetes.io/managed-by"
        ] = OWNER
        run(
            ["kubectl", "apply", "--server-side", "--field-manager=kip-lambda", "-f", "-"],
            data=json.dumps(matches[0]),
        )
        run(
            [
                "kubectl",
                "wait",
                "--for=condition=Established",
                "crd/inferenceobjectives.llm-d.ai",
                "--timeout=2m",
            ],
            timeout=150,
        )
    for pool in POOLS:
        for band, priority in BANDS.items():
            name = f"{pool}-{band}"
            existing = owned("inferenceobjective", name, "kip-inference")
            if existing and existing.get("spec") != {
                "priority": priority,
                "poolRef": {"name": pool},
            }:
                raise TenantError(f"InferenceObjective/{name} has drifted")
            run(
                ["kubectl", "apply", "-f", "-"],
                data=json.dumps(
                    {
                        "apiVersion": "llm-d.ai/v1alpha2",
                        "kind": "InferenceObjective",
                        "metadata": {
                            "name": name,
                            "namespace": "kip-inference",
                            "labels": {"app.kubernetes.io/managed-by": OWNER},
                        },
                        "spec": {"priority": priority, "poolRef": {"name": pool}},
                    }
                ),
            )


def install_policy(gateway_ip: str) -> None:
    existing = owned("securitypolicy", "kip-tenant-auth", "kip-inference")
    if existing and (
        existing.get("spec", {}).get("targetRefs", [{}])[0].get("name") != "kip-inference-gateway"
        or existing.get("spec", {}).get("extAuth", {}).get("failOpen") is not False
    ):
        raise TenantError("tenant SecurityPolicy has drifted")
    run(["kubectl", "apply", "-f", str(HERE / "tenant-security-policy.yaml")])
    for _ in range(40):
        policy = owned("securitypolicy", "kip-tenant-auth", "kip-inference")
        if any(
            cond.get("type") == "Accepted" and cond.get("status") == "True"
            for parent in policy.get("status", {}).get("ancestors", [])
            for cond in parent.get("conditions", [])
        ):
            break
        time.sleep(2)
    else:
        raise TenantError("tenant SecurityPolicy was not Accepted")
    status = run(
        [
            "curl",
            "-sS",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "--max-time",
            "10",
            "-H",
            "X-Gateway-Base-Model-Name: qwen3-8b",
            f"http://{gateway_ip}/v1/models",
        ]
    ).strip()
    if status != "403":
        raise TenantError(f"unauthenticated private Gateway was not denied (HTTP {status})")


def install() -> None:
    control = run(["hostname"]).strip()
    if os.geteuid() != 0 or not CONTROL.fullmatch(control):
        raise TenantError("run as root on the owned Lambda east control plane")
    os.environ["KUBECONFIG"] = "/etc/kubernetes/admin.conf"
    gateway_ip = require_gateway()
    raw = credentials()
    install_auth(control, raw)
    print("PASS: private tenant authorization Service is Ready.", flush=True)
    install_objectives()
    print("PASS: six pool-specific llm-d objectives are installed.", flush=True)
    install_policy(gateway_ip)
    print("PASS: both Gateway routes require trusted tenant credentials.", flush=True)


def main() -> int:
    try:
        install()
    except (TenantError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
