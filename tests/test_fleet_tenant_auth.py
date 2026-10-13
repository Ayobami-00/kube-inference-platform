"""The pilot auth service must fail closed on every client-controlled priority path."""

import hashlib
import importlib.util
import json
import subprocess
from email.message import Message
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "kip_tenant_auth", ROOT / "deploy/fleet/tenant-auth.py"
)
assert SPEC and SPEC.loader
AUTH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTH)


@pytest.fixture
def credentials():
    return {
        tenant: {
            workload_class: hashlib.sha256(f"{tenant}/{workload_class}".encode()).hexdigest()
            for workload_class in AUTH.CLASSES
        }
        for tenant in AUTH.TENANTS
    }


def test_each_credential_fixes_tenant_and_workload_class(credentials):
    for tenant in AUTH.TENANTS:
        for workload_class in AUTH.CLASSES:
            status, headers = AUTH.authorize(
                {"Authorization": f"Bearer {credentials[tenant][workload_class]}"},
                credentials,
            )
            assert status == 200
            assert headers == {
                "x-llm-d-inference-fairness-id": tenant,
                "x-llm-d-inference-objective": AUTH.OBJECTIVE[workload_class],
                "x-kip-tenant": tenant,
                "x-kip-workload-class": workload_class,
            }


@pytest.mark.parametrize(
    "spoof",
    [
        "x-llm-d-inference-objective",
        "x-llm-d-inference-fairness-id",
        "x-llm-d-model-name-rewrite",
        "x-llm-d-slo-ttft-ms",
        "x-llm-d-unknown-control",
        "x-gateway-inference-objective",
        "x-gateway-inference-fairness-id",
        "x-gateway-model-name-rewrite",
        "x-slo-ttft-ms",
        "x-slo-tpot-ms",
        "x-kip-tenant",
        "x-kip-workload-class",
    ],
)
def test_spoofed_control_headers_are_rejected(credentials, spoof):
    status, headers = AUTH.authorize(
        {
            "Authorization": f"Bearer {credentials['tenant-a']['chat']}",
            spoof.upper(): "tenant-c",
        },
        credentials,
    )
    assert (status, headers) == (403, {})


@pytest.mark.parametrize("header", ["", "Bearer invalid", "Basic abc", "Bearer " + "0" * 64])
def test_missing_or_invalid_token_is_rejected(credentials, header):
    status, headers = AUTH.authorize({"Authorization": header}, credentials)
    assert (status, headers) == (403, {})


def test_duplicate_authorization_headers_are_rejected(credentials):
    headers = Message()
    headers.add_header("Authorization", f"Bearer {credentials['tenant-a']['chat']}")
    headers.add_header("authorization", f"Bearer {credentials['tenant-c']['voice']}")
    assert AUTH.authorize(headers, credentials) == (403, {})


def test_credential_schema_rejects_missing_duplicate_or_malformed_tokens(tmp_path, credentials):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps(credentials), encoding="utf-8")
    assert AUTH.load_credentials(path) == credentials
    credentials["tenant-b"]["voice"] = credentials["tenant-a"]["voice"]
    path.write_text(json.dumps(credentials), encoding="utf-8")
    with pytest.raises(ValueError, match="uniqueness"):
        AUTH.load_credentials(path)
    credentials["tenant-b"]["voice"] = "short"
    path.write_text(json.dumps(credentials), encoding="utf-8")
    with pytest.raises(ValueError, match="format"):
        AUTH.load_credentials(path)


def test_generator_creates_private_unique_credentials_once(tmp_path):
    script = ROOT / "deploy/fleet/tenant-keys.py"
    path = tmp_path / "private" / "keys.json"
    for _ in range(2):
        subprocess.run(["python3", str(script), str(path)], check=True)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    credentials = AUTH.load_credentials(path)
    assert len({value for classes in credentials.values() for value in classes.values()}) == 12


def test_tenant_auth_manifests_are_private_and_fail_closed():
    deployment, service = list(
        yaml.safe_load_all((ROOT / "deploy/fleet/tenant-auth.yaml").read_text())
    )
    policy = yaml.safe_load((ROOT / "deploy/fleet/tenant-security-policy.yaml").read_text())
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["template"]["spec"]["nodeSelector"] == {
        "kube-inference-platform.io/fleet-role": "control-plane"
    }
    assert container["resources"]["requests"].get("nvidia.com/gpu") is None
    assert container["image"].endswith(
        "@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d"
    )
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert service["spec"]["type"] == "ClusterIP"
    ext_auth = policy["spec"]["extAuth"]
    assert ext_auth["failOpen"] is False
    assert ext_auth["http"]["pathOverride"] == "/authorize"
    assert set(ext_auth["headersToExtAuth"]) >= {
        "x-llm-d-inference-objective",
        "x-llm-d-inference-fairness-id",
        *AUTH.DEPRECATED_CONTROL_HEADERS,
    }
    assert set(ext_auth["http"]["headersToBackend"]) == {
        "x-llm-d-inference-objective",
        "x-llm-d-inference-fairness-id",
        "x-kip-tenant",
        "x-kip-workload-class",
    }


def test_flow_control_values_and_objectives_are_tenant_bounded():
    lock = json.loads((ROOT / "deploy/fleet/flow-control-lock.json").read_text())
    values = yaml.safe_load((ROOT / "deploy/fleet/llmd-router-flow-values.yaml").read_text())
    config = yaml.safe_load(
        values["router"]["epp"]["pluginsCustomConfig"]["kip-pilot-flow-plugins.yaml"]
    )
    objectives = list(
        yaml.safe_load_all((ROOT / "deploy/fleet/inference-objectives.yaml").read_text())
    )
    assert lock["llmdRouterVersion"] == "v0.10.0"
    assert lock["tenantAuthLinuxAmd64Image"].endswith(
        "@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d"
    )
    assert lock["preTenantRouterValuesSha256"] == (
        "871c210a7ab76455d925ebd6081cb543c40bd01ddd93c86b38963a541477dbf2"
    )
    assert lock["preWeightedPickerValuesSha256"] == (
        "89f23e13b2a218da0795bd06936ae66e609d68ae0a12a00679953f907539e491"
    )
    assert lock["preControlPlanePlacementValuesSha256"] == (
        "e0f286ff950877aedea44081f5389229fa392817f1ea4ee234487a5eeb0d5595"
    )
    assert (
        lock["preTenantControlPlanePlacementValuesSha256"]
        == hashlib.sha256((ROOT / "deploy/fleet/llmd-router-values.yaml").read_bytes()).hexdigest()
    )
    assert values["router"]["epp"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"] == ["kip-control"]
    assert values["router"]["epp"]["tolerations"] == [
        {
            "key": "node-role.kubernetes.io/control-plane",
            "operator": "Exists",
            "effect": "NoSchedule",
        }
    ]
    assert config["featureGates"] == ["flowControl"]
    assert config["flowControl"]["saturationDetector"] == {"pluginRef": "concurrency-detector"}
    assert {item["type"] for item in config["plugins"]} >= {"weighted-random-picker"}
    assert config["schedulingProfiles"][0]["plugins"][-1] == {"pluginRef": "weighted-random-picker"}
    assert {item["priority"] for item in config["flowControl"]["priorityBands"]} == {
        100,
        0,
        -10,
    }
    assert (
        next(item for item in config["plugins"] if item["type"] == "concurrency-detector")[
            "parameters"
        ]["maxConcurrency"]
        == 6
    )
    assert {item["metadata"]["name"]: item["spec"]["priority"] for item in objectives} == {
        "kip-voice": 100,
        "kip-interactive": 0,
        "kip-batch": -10,
    }
    assert all(item["spec"]["poolRef"]["name"] == "kip-qwen25-pilot" for item in objectives)


def test_tenant_install_rejects_arguments_before_mutation():
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/fleet/tenant-install.sh"), "unexpected"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "accepts no arguments" in result.stderr
