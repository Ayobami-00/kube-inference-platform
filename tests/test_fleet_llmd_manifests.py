"""Pin the ownership and scheduling boundaries of the fleet inference path."""

import json
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_two_model_replicas_are_kip_owned_and_gpu_separated():
    lock = json.loads((ROOT / "deploy/fleet/versions.json").read_text())
    resources = list(yaml.safe_load_all((ROOT / "deploy/fleet/model.yaml").read_text()))
    assert [(item["kind"], item["metadata"]["name"]) for item in resources] == [
        ("Namespace", "kip-inference"),
        ("Deployment", "kip-qwen25-pilot"),
        ("Service", "kip-qwen25-pilot"),
    ]
    deployment = resources[1]
    spec = deployment["spec"]
    pod = spec["template"]
    container = pod["spec"]["containers"][0]
    assert deployment["metadata"]["labels"]["app.kubernetes.io/managed-by"] == (
        "kube-inference-platform"
    )
    assert spec["replicas"] == 2
    assert spec["strategy"]["rollingUpdate"] == {"maxSurge": 0, "maxUnavailable": 1}
    assert pod["spec"]["nodeSelector"] == {"kube-inference-platform.io/fleet-role": "gpu-worker"}
    assert (
        pod["spec"]["affinity"]["podAntiAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ][0]["topologyKey"]
        == "kubernetes.io/hostname"
    )
    assert container["image"] == lock["inference"]["vllmLinuxAmd64Image"]
    assert container["resources"]["requests"]["nvidia.com/gpu"] == "1"
    assert container["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert "--enable-prefix-caching" in container["args"]
    assert "--trust-remote-code" not in container["args"]
    assert {item["name"]: item["value"] for item in container["env"]}[
        "VLLM_HTTP_TIMEOUT_KEEP_ALIVE"
    ] == "60"
    assert pod["metadata"]["labels"]["kube-inference-platform.io/model-revision"] == (
        "a09a35458c702b33eeacc393d103063234e8bc28"
    )
    assert resources[2]["spec"]["type"] == "ClusterIP"


def test_llmd_chart_values_select_only_the_kip_model_pods_and_fail_closed():
    lock = json.loads((ROOT / "deploy/fleet/versions.json").read_text())
    values = yaml.safe_load((ROOT / "deploy/fleet/llmd-router-values.yaml").read_text())
    router = values["router"]
    assert values["provider"]["name"] == "none"
    assert router["modelServers"]["matchLabels"] == {
        "app.kubernetes.io/instance": "kip-qwen25-pilot"
    }
    assert router["modelServers"]["targetPorts"] == [{"number": 8000}]
    assert router["inferencePool"] == {"create": True, "failureMode": "FailClose"}
    assert router["epp"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchExpressions"] == [
        {"key": "kubernetes.io/hostname", "operator": "In", "values": ["kip-control"]}
    ]
    assert router["epp"]["tolerations"] == [
        {
            "key": "node-role.kubernetes.io/control-plane",
            "operator": "Exists",
            "effect": "NoSchedule",
        }
    ]
    image = router["epp"]["image"]
    assert (
        f"{image['registry']}/{image['repository']}:{image['tag']}"
        == lock["inference"]["eppLinuxAmd64Image"]
    )
    assert values["httpRoute"]["inferenceGatewayName"] == "kip-inference-gateway"
    plugin_text = router["epp"]["pluginsCustomConfig"]["kip-pilot-plugins.yaml"]
    plugins = yaml.safe_load(plugin_text)
    assert router["epp"]["pluginsConfigFile"] == "kip-pilot-plugins.yaml"
    assert plugins["apiVersion"] == "llm-d.ai/v1alpha1"
    assert {item["type"] for item in plugins["plugins"]} == {
        "approx-prefix-cache-producer",
        "prefix-cache-scorer",
        "kv-cache-utilization-scorer",
        "queue-scorer",
    }
    assert [item["pluginRef"] for item in plugins["schedulingProfiles"][0]["plugins"]] == [
        "queue-scorer",
        "kv-cache-utilization-scorer",
        "prefix-cache-scorer",
    ]


def test_envoy_gateway_data_plane_is_private_and_inference_aware():
    lock = json.loads((ROOT / "deploy/fleet/versions.json").read_text())
    resources = list(yaml.safe_load_all((ROOT / "deploy/fleet/gateway.yaml").read_text()))
    assert [item["kind"] for item in resources] == ["EnvoyProxy", "GatewayClass", "Gateway"]
    proxy, gateway_class, gateway = resources
    assert proxy["spec"]["provider"]["kubernetes"]["envoyService"]["type"] == "ClusterIP"
    assert proxy["spec"]["provider"]["kubernetes"]["envoyDeployment"]["pod"]["nodeSelector"] == {
        "kubernetes.io/hostname": "kip-control"
    }
    assert proxy["spec"]["provider"]["kubernetes"]["envoyDeployment"]["pod"]["tolerations"] == [
        {
            "key": "node-role.kubernetes.io/control-plane",
            "operator": "Exists",
            "effect": "NoSchedule",
        }
    ]
    access_log = proxy["spec"]["telemetry"]["accessLog"]["settings"][0]
    assert access_log["sinks"] == [{"type": "File", "file": {"path": "/dev/stdout"}}]
    fields = access_log["format"]["json"]
    assert fields["x_correlation_id"] == "%REQ(X-CORRELATION-ID)%"
    assert fields["x_request_id"] == "%REQ(X-REQUEST-ID)%"
    assert "authorization" not in str(fields).lower()
    assert gateway_class["spec"]["parametersRef"] == {
        "group": "gateway.envoyproxy.io",
        "kind": "EnvoyProxy",
        "name": "kip-private-proxy",
        "namespace": "envoy-gateway-system",
    }
    assert gateway["spec"]["gatewayClassName"] == "kip-private-inference"
    assert gateway["spec"]["listeners"][0]["allowedRoutes"]["namespaces"]["from"] == "Same"
    values = yaml.safe_load((ROOT / "deploy/fleet/envoy-gateway-values.yaml").read_text())
    assert (
        values["global"]["images"]["envoyGateway"]["image"]
        == lock["gateway"]["envoyGatewayLinuxAmd64Image"]
    )
    assert (
        values["global"]["images"]["envoyProxy"]["image"]
        == lock["gateway"]["envoyProxyLinuxAmd64Image"]
    )
    assert (
        values["global"]["images"]["ratelimit"]["image"]
        == lock["gateway"]["envoyRateLimitLinuxAmd64Image"]
    )
    manager = values["config"]["envoyGateway"]["extensionManager"]
    assert manager["backendResources"] == [
        {"group": "inference.networking.k8s.io", "kind": "InferencePool", "version": "v1"}
    ]
    ai_values = yaml.safe_load((ROOT / "deploy/fleet/envoy-ai-gateway-values.yaml").read_text())
    assert ai_values["controller"]["service"]["type"] == "ClusterIP"
    for key, section in {
        "envoyAiGatewayControllerLinuxAmd64Image": "controller",
        "envoyAiGatewayExtprocLinuxAmd64Image": "extProc",
    }.items():
        image = ai_values[section]["image"]
        assert f"{image['repository']}:{image['tag']}" == lock["gateway"][key]


def test_llmd_installer_fails_closed_before_cluster_mutation():
    installer = ROOT / "deploy/fleet/llmd-install.sh"
    result = subprocess.run(
        ["bash", str(installer), "unexpected"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "accepts no arguments" in result.stderr
    script = installer.read_text(encoding="utf-8")
    assert "require_model_ready" in script
    assert "require_owned_crd_group" in script
    assert "require_chart_release" in script
    assert ".kip.valuesSha256 == $hash" in script
    assert "--set-string kip.owner=kube-inference-platform" in script
    assert "verify_manifest" in script
    assert "verify_chart" in script
    assert "--server-side --field-manager=kip-pilot" in script
    assert '[ "$service_types" = ClusterIP ]' in script
    assert "kctl get services -A -o json" in script
    assert '"meta.helm.sh/release-name"] == "kip-qwen25-pilot"' in script
    assert '"kube-inference-platform.io/router-values-sha256"' in script
    assert "patch deployment kip-qwen25-pilot-epp --type merge" in script
    assert "rollout status deployment/kip-qwen25-pilot-epp" in script
    assert "preControlPlanePlacementValuesSha256" in script
    assert "private EnvoyProxy placement or access log drifted" in script
    assert "Envoy data plane is not pinned to kip-control" in script
    assert "rollout restart deployment/kip-qwen25-pilot" not in script
    assert script.index('kctl apply -f "$script_dir/gateway-rbac.yaml"') < script.index(
        "helm upgrade --install eg"
    )
