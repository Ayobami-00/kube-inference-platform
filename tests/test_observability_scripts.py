"""Static and subprocess checks for the observability deployment layer."""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
OBSERVABILITY = ROOT / "deploy" / "observability"


def test_observability_shell_is_valid() -> None:
    scripts = sorted(OBSERVABILITY.rglob("*.sh"))
    assert scripts
    completed = subprocess.run(
        ["/bin/bash", "-n", *map(str, scripts)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_invalid_verbose_is_usage_error() -> None:
    completed = subprocess.run(
        ["/bin/bash", str(OBSERVABILITY / "validate.sh")],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "KIP_VERBOSE": "yes", "NO_COLOR": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "VERBOSE must be 0 or 1" in completed.stderr


@pytest.mark.parametrize("action", ["unknown", "../configure", ""])
def test_cost_action_allowlist(action: str) -> None:
    arguments = ["/bin/bash", str(OBSERVABILITY / "cost.sh")]
    if action:
        arguments.append(action)
    completed = subprocess.run(
        arguments,
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "NO_COLOR": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2


def test_versions_and_values_pin_immutable_artifacts() -> None:
    versions = (OBSERVABILITY / "versions.env").read_text()
    monitoring = (OBSERVABILITY / "values" / "kube-prometheus-stack.yaml").read_text()
    opencost = (OBSERVABILITY / "values" / "opencost-unpriced.yaml").read_text()

    assert 'KIP_KUBE_PROMETHEUS_STACK_VERSION="91.4.0"' in versions
    assert (
        'KIP_KUBE_PROMETHEUS_STACK_DIGEST="sha256:'
        '0032315f2580f488ce433ff9d18de24ce631857ff7f1d73693171eeeb7affd52"' in versions
    )
    assert 'KIP_PROMETHEUS_OPERATOR_VERSION="v0.94.0"' in versions
    assert (
        'KIP_PROMETHEUS_CONFIG_RELOADER_IMAGE_DIGEST="sha256:'
        'e4a878ac166b119d48f54226901863623dffba556020529563c4a62e35546fca"' in versions
    )
    assert 'KIP_OPENCOST_CHART_VERSION="2.5.30"' in versions
    assert (
        'KIP_OPENCOST_CHART_DIGEST="sha256:'
        '534e846fc11b8e7e9967dd07fe808e552395a0985457f0cec6e0715d6c177bbc"' in versions
    )
    assert "retention: 15d" in monitoring
    assert "retentionSize: 20GB" in monitoring
    assert "scrapeInterval: 15s" in monitoring
    assert monitoring.count("sha256:") >= 1
    assert "fullImageName: ghcr.io/opencost/opencost:1.121.1@sha256:" in opencost
    assert "enabled: false" in opencost  # OpenCost UI/custom pricing are safe by default.
    crd_line = next(
        line for line in versions.splitlines() if line.startswith("KIP_MONITORING_CRDS=")
    )
    assert len(crd_line.split('"', 2)[1].split()) == 10
    assert "prometheuses.monitoring.coreos.com" in crd_line


def test_full_helm_value_hashes_match_committed_configuration() -> None:
    versions = {}
    for line in (OBSERVABILITY / "versions.env").read_text().splitlines():
        if line.startswith("KIP_") and '="' in line and line.endswith('"'):
            name, value = line.split("=", 1)
            versions[name] = value[1:-1]

    def canonical_hash(value: object) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()

    monitoring = yaml.safe_load(
        (OBSERVABILITY / "values" / "kube-prometheus-stack.yaml").read_text()
    )
    unpriced = yaml.safe_load((OBSERVABILITY / "values" / "opencost-unpriced.yaml").read_text())
    assert canonical_hash(monitoring) == versions["KIP_KUBE_PROMETHEUS_STACK_VALUES_SHA256"]
    assert canonical_hash(unpriced) == versions["KIP_OPENCOST_UNPRICED_VALUES_SHA256"]

    priced_text = (OBSERVABILITY / "values" / "opencost-priced.yaml.tpl").read_text()
    priced = yaml.safe_load(
        priced_text.replace("__PROFILE_NAME__", "profile")
        .replace("__PROFILE_HASH__", "0" * 64)
        .replace("__EFFECTIVE_FROM__", "2026-01-01")
        .replace("__GPU_HOURLY_RATE_USD__", "1.25")
    )
    del priced["kip"]["costProfile"]
    del priced["kip"]["costProfileSha256"]
    del priced["podLabels"]
    del priced["podAnnotations"]
    del priced["opencost"]["customPricing"]["costModel"]["description"]
    del priced["opencost"]["customPricing"]["costModel"]["GPU"]
    assert canonical_hash(priced) == versions["KIP_OPENCOST_PRICED_STATIC_VALUES_SHA256"]

    monitor_hash_names = {
        "kip-vllm": "KIP_SERVICEMONITOR_VLLM_SPEC_SHA256",
        "kip-dcgm-exporter": "KIP_SERVICEMONITOR_DCGM_SPEC_SHA256",
        "kip-cilium-agent": "KIP_SERVICEMONITOR_CILIUM_AGENT_SPEC_SHA256",
        "kip-cilium-operator": "KIP_SERVICEMONITOR_CILIUM_OPERATOR_SPEC_SHA256",
    }
    for document in yaml.safe_load_all(
        (OBSERVABILITY / "manifests" / "service-monitors.yaml").read_text()
    ):
        if document and document["kind"] == "ServiceMonitor":
            assert (
                canonical_hash(document["spec"])
                == versions[monitor_hash_names[document["metadata"]["name"]]]
            )
    rules = yaml.safe_load((OBSERVABILITY / "manifests" / "prometheus-rules.yaml").read_text())
    assert canonical_hash(rules["spec"]) == versions["KIP_PROMETHEUS_RULE_SPEC_SHA256"]


def test_install_verifies_tags_then_uses_digest_addressed_charts() -> None:
    install = (OBSERVABILITY / "install.sh").read_text()
    assert 'helm pull "$chart" --version "$version"' in install
    assert '[ "$actual" = "$digest" ]' in install
    assert '"$KIP_KUBE_PROMETHEUS_STACK_CHART@$KIP_KUBE_PROMETHEUS_STACK_DIGEST"' in install
    assert '"$KIP_OPENCOST_CHART@$KIP_OPENCOST_CHART_DIGEST"' in install
    assert (
        "kip:\n  chartDigest:"
        in (OBSERVABILITY / "values" / "kube-prometheus-stack.yaml").read_text()
    )
    common = (OBSERVABILITY / "lib" / "common.sh").read_text()
    health = (OBSERVABILITY / "lib" / "health.sh").read_text()
    assert "prometheusConfigReloader.image.sha" in common
    assert "kip_observability_component_images_exact" in health
    assert 'test("@sha256:[0-9a-f]{64}$")' in health


def test_grafana_password_is_applied_over_stdin_not_process_arguments() -> None:
    preflight = (OBSERVABILITY / "lib" / "preflight.sh").read_text()
    assert "| kip_kubectl apply -f -" in preflight
    assert "--from-literal" not in preflight
    assert "admin-password=$password" not in preflight


def test_dashboard_configmaps_add_labels_through_portable_json_rendering() -> None:
    render = (OBSERVABILITY / "lib" / "render.sh").read_text()
    assert "create configmap" in render
    assert "--dry-run=client -o json" in render
    assert '"grafana_dashboard":"1"' in render
    assert "--labels=" not in render


def test_cilium_service_monitors_have_owned_discovery_services() -> None:
    documents = list(
        yaml.safe_load_all((OBSERVABILITY / "manifests" / "service-monitors.yaml").read_text())
    )
    services = {
        document["metadata"]["name"]: document
        for document in documents
        if document["kind"] == "Service"
    }
    assert set(services) == {
        "kip-cilium-agent-metrics",
        "kip-cilium-operator-metrics",
    }
    assert services["kip-cilium-agent-metrics"]["spec"]["ports"] == [
        {"name": "prometheus", "port": 9962, "targetPort": "prometheus"}
    ]
    assert services["kip-cilium-operator-metrics"]["spec"]["ports"] == [
        {"name": "prometheus", "port": 9963, "targetPort": 9963}
    ]
    for service in services.values():
        assert service["metadata"]["namespace"] == "kube-system"
        assert (
            service["metadata"]["labels"]["app.kubernetes.io/managed-by"]
            == "kube-inference-platform"
        )


@pytest.mark.parametrize("preexisting", ["absent", "empty", "foreign"])
def test_observability_storage_requires_or_creates_exact_marker(
    tmp_path: Path, preexisting: str
) -> None:
    storage = tmp_path / "observability"
    work = tmp_path / "work"
    work.mkdir()
    if preexisting in {"empty", "foreign"}:
        storage.mkdir()
    if preexisting == "foreign":
        (storage / "foreign.db").write_text("preserve\n")
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{ROOT / "deploy" / "cluster" / "lib" / "common.sh"}"
KIP_WORK_DIR="{work}"
KIP_OBSERVABILITY_STORAGE_ROOT="{storage}"
source "{OBSERVABILITY / "lib" / "preflight.sh"}"
kip_acquire_privileges() {{ KIP_USE_SUDO=0; }}
kip_observability_host_storage_ready
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script], cwd=ROOT, check=False, capture_output=True, text=True
    )
    if preexisting == "foreign":
        assert completed.returncode == 1
        assert (storage / "foreign.db").read_text() == "preserve\n"
        assert not (storage / ".kip-managed-v1").exists()
    else:
        assert completed.returncode == 0, completed.stderr
        assert (storage / ".kip-managed-v1").read_text() == (
            "kube-inference-platform observability storage v1\n"
        )
        assert all((storage / name).is_dir() for name in ("prometheus", "alertmanager", "grafana"))


@pytest.mark.parametrize(
    ("existing", "exact_release", "expected"),
    [("none", "0", 0), ("all", "0", 1), ("all", "1", 0)],
)
def test_prometheus_crd_preflight_does_not_adopt_unrelated_installation(
    existing: str, exact_release: str, expected: int
) -> None:
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{OBSERVABILITY / "versions.env"}"
source "{ROOT / "deploy" / "cluster" / "lib" / "common.sh"}"
KIP_REPOSITORY_ROOT="{ROOT}"
source "{OBSERVABILITY / "lib" / "common.sh"}"
kip_kubectl() {{ [ "$KIP_TEST_EXISTING" = all ]; }}
kip_observability_release_identity_exact() {{ [ "$KIP_TEST_EXACT_RELEASE" = 1 ]; }}
kip_observability_monitoring_crds_safe
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "NO_COLOR": "1",
            "KIP_TEST_EXISTING": existing,
            "KIP_TEST_EXACT_RELEASE": exact_release,
        },
        check=False,
    )
    assert completed.returncode == expected


@pytest.mark.parametrize(("identity_exact", "expected"), [("1", 0), ("0", 1)])
def test_safe_state_recovers_unhealthy_release_only_with_exact_identity(
    identity_exact: str, expected: int
) -> None:
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{OBSERVABILITY / "versions.env"}"
source "{ROOT / "deploy" / "cluster" / "lib" / "common.sh"}"
KIP_REPOSITORY_ROOT="{ROOT}"
source "{OBSERVABILITY / "lib" / "common.sh"}"
kip_observability_namespace_exists() {{ return 1; }}
kip_observability_release_exists() {{ return 0; }}
kip_observability_release_identity_exact() {{ [ "$KIP_TEST_IDENTITY_EXACT" = 1 ]; }}
kip_observability_monitoring_crds_safe() {{ return 0; }}
kip_observability_storage_resources_safe() {{ return 0; }}
kip_observability_safe_state
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "NO_COLOR": "1",
            "KIP_TEST_IDENTITY_EXACT": identity_exact,
        },
        check=False,
    )
    assert completed.returncode == expected


def test_safe_state_passes_each_release_name_to_existence_check() -> None:
    common = (OBSERVABILITY / "lib" / "common.sh").read_text()
    assert 'kip_observability_release_exists "$release"' in common


def test_named_resource_inspection_errors_fail_closed() -> None:
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{OBSERVABILITY / "versions.env"}"
source "{ROOT / "deploy" / "cluster" / "lib" / "common.sh"}"
KIP_REPOSITORY_ROOT="{ROOT}"
source "{OBSERVABILITY / "lib" / "common.sh"}"
kip_kubectl() {{ return 1; }}
kip_observability_resource_owned_or_absent secret kip-grafana-admin kip-observability
'''
    completed = subprocess.run(["/bin/bash", "-c", script], cwd=ROOT, check=False)
    assert completed.returncode == 1


def test_deployed_health_check_remains_stricter_than_recovery_identity() -> None:
    common = (OBSERVABILITY / "lib" / "common.sh").read_text()
    assert "failed | pending-install | pending-upgrade | pending-rollback" in common
    assert 'kip_observability_release_identity_exact "$1" || return 1' in common
    assert '[ "$status" = deployed ]' in common


@pytest.mark.parametrize("tampered", [False, True])
def test_active_cost_profile_must_match_committed_reviewed_profile(
    tmp_path: Path, tampered: bool
) -> None:
    sha = shutil.which("sha256sum") or shutil.which("shasum")
    if sha is None:
        pytest.skip("a SHA-256 command is required")
    sha_setup = (
        'kip_exec() { command "$@"; };'
        if Path(sha).name == "sha256sum"
        else (
            "kip_exec() { "
            f'if [ "$1" = sha256sum ]; then shift; "{sha}" -a 256 "$@"; '
            'else command "$@"; fi; }'
        )
    )
    profile = {
        "schemaVersion": 1,
        "name": "reviewed-node",
        "currency": "USD",
        "nodeHourlyRateUsd": 1.25,
        "provider": "Example Provider",
        "machineType": "A10 dedicated node",
        "region": "test-region",
        "source": "https://pricing.example.test/a10",
        "effectiveFrom": "2026-01-01",
        "physicalGpuCount": 1,
        "powerIncludedInRate": True,
    }
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "reviewed-node.json").write_text(json.dumps(profile))
    active = dict(profile)
    if tampered:
        active["nodeHourlyRateUsd"] = 99.0
    active_path = tmp_path / "active.json"
    active_path.write_text(json.dumps(active))
    work = tmp_path / "work"
    work.mkdir()
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{OBSERVABILITY / "versions.env"}"
source "{ROOT / "deploy" / "cluster" / "lib" / "common.sh"}"
KIP_REPOSITORY_ROOT="{ROOT}"
KIP_COST_PROFILE_DIR="{profile_dir}"
KIP_WORK_DIR="{work}"
source "{OBSERVABILITY / "lib" / "common.sh"}"
{sha_setup}
kip_observability_active_profile_json() {{ command cat "{active_path}"; }}
kip_observability_load_active_profile
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env={**os.environ, "NO_COLOR": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == (1 if tampered else 0), completed.stderr


@pytest.mark.parametrize(
    ("tamper", "expected"),
    [
        ("none", 0),
        ("gpu", 1),
        ("cpu", 1),
        ("provider", 1),
        ("description", 1),
        ("extra-key", 1),
    ],
)
def test_live_opencost_pricing_configmap_must_exactly_match_profile(
    tmp_path: Path, tamper: str, expected: int
) -> None:
    data = {
        "CPU": "0",
        "GPU": "1.25",
        "RAM": "0",
        "description": "KIP whole-node profile reviewed-node (profile-hash)",
        "internetNetworkEgress": "0",
        "provider": "custom",
        "regionNetworkEgress": "0",
        "spotCPU": "0",
        "spotRAM": "0",
        "storage": "0",
        "zoneNetworkEgress": "0",
    }
    if tamper == "gpu":
        data["GPU"] = "9.99"
    elif tamper == "cpu":
        data["CPU"] = "0.01"
    elif tamper == "provider":
        data["provider"] = "default"
    elif tamper == "description":
        data["description"] = "an unreviewed price"
    elif tamper == "extra-key":
        data["currency"] = "USD"
    configmap = tmp_path / "pricing.json"
    configmap.write_text(json.dumps({"data": data}))
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{OBSERVABILITY / "versions.env"}"
source "{ROOT / "deploy" / "cluster" / "lib" / "common.sh"}"
KIP_REPOSITORY_ROOT="{ROOT}"
source "{OBSERVABILITY / "lib" / "common.sh"}"
kip_exec() {{ command "$@"; }}
kip_kubectl() {{ command cat "$KIP_TEST_CONFIGMAP"; }}
KIP_COST_PROFILE_NAME=reviewed-node
KIP_COST_PROFILE_HASH=profile-hash
KIP_COST_GPU_HOURLY_RATE_USD=1.25
kip_observability_pricing_configmap_matches_profile
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env={**os.environ, "KIP_TEST_CONFIGMAP": str(configmap), "NO_COLOR": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == expected, completed.stderr


@pytest.mark.parametrize(
    ("tamper", "expected"),
    [("none", 0), ("label", 1), ("annotation", 1), ("description", 1)],
)
def test_priced_helm_metadata_maps_and_description_are_exact(
    tmp_path: Path, tamper: str, expected: int
) -> None:
    values = {
        "kip": {
            "pricingState": "priced",
            "costProfile": "reviewed-node",
            "costProfileSha256": "profile-hash",
        },
        "podLabels": {"kube-inference-platform.io/cost-profile": "reviewed-node"},
        "podAnnotations": {
            "kube-inference-platform.io/cost-profile-sha256": "profile-hash",
            "kube-inference-platform.io/pricing-effective-from": "2026-01-01",
        },
        "opencost": {
            "customPricing": {
                "costModel": {
                    "description": ("KIP whole-node profile reviewed-node (profile-hash)")
                }
            }
        },
    }
    if tamper == "label":
        values["podLabels"]["unexpected"] = "value"
    elif tamper == "annotation":
        del values["podAnnotations"]["kube-inference-platform.io/pricing-effective-from"]
    elif tamper == "description":
        values["opencost"]["customPricing"]["costModel"]["description"] = "drift"
    values_file = tmp_path / "values.json"
    values_file.write_text(json.dumps(values))
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{OBSERVABILITY / "versions.env"}"
source "{ROOT / "deploy" / "cluster" / "lib" / "common.sh"}"
KIP_REPOSITORY_ROOT="{ROOT}"
source "{OBSERVABILITY / "lib" / "common.sh"}"
kip_exec() {{
    if [ "$1" = helm ]; then
        command cat "$KIP_TEST_VALUES"
    else
        command "$@"
    fi
}}
KIP_COST_PROFILE_NAME=reviewed-node
KIP_COST_PROFILE_HASH=profile-hash
KIP_COST_EFFECTIVE_FROM=2026-01-01
kip_observability_profile_metadata_matches_release
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env={**os.environ, "KIP_TEST_VALUES": str(values_file), "NO_COLOR": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == expected, completed.stderr


@pytest.mark.parametrize(
    (
        "release_state",
        "active_state",
        "load_ok",
        "metadata_ok",
        "requested_name",
        "active_name",
        "expected",
    ),
    [
        ("absent", "absent", "1", "1", "", "reviewed-node", 0),
        ("unpriced", "absent", "1", "1", "", "reviewed-node", 0),
        ("unpriced", "present", "1", "1", "", "reviewed-node", 1),
        ("absent", "present", "1", "1", "", "reviewed-node", 1),
        ("priced", "absent", "1", "1", "", "reviewed-node", 1),
        ("priced", "present", "0", "1", "", "reviewed-node", 1),
        ("priced", "present", "1", "0", "", "reviewed-node", 1),
        ("priced", "present", "1", "1", "", "reviewed-node", 0),
        ("priced", "present", "1", "1", "other-node", "reviewed-node", 1),
    ],
)
def test_install_pricing_state_is_fail_closed(
    release_state: str,
    active_state: str,
    load_ok: str,
    metadata_ok: str,
    requested_name: str,
    active_name: str,
    expected: int,
) -> None:
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{OBSERVABILITY / "versions.env"}"
source "{ROOT / "deploy" / "cluster" / "lib" / "common.sh"}"
KIP_REPOSITORY_ROOT="{ROOT}"
source "{OBSERVABILITY / "lib" / "common.sh"}"
kip_observability_active_profile_state() {{ printf '%s\n' "$KIP_TEST_ACTIVE_STATE"; }}
kip_observability_release_exists() {{ [ "$KIP_TEST_RELEASE_STATE" != absent ]; }}
kip_observability_release_pricing_state() {{ printf '%s\n' "$KIP_TEST_RELEASE_STATE"; }}
kip_observability_load_active_profile() {{
    [ "$KIP_TEST_LOAD_OK" = 1 ] || return 1
    KIP_COST_PROFILE="$KIP_TEST_ACTIVE_NAME"
    KIP_COST_PROFILE_NAME="$KIP_TEST_ACTIVE_NAME"
    KIP_COST_PROFILE_HASH=active-hash
}}
kip_observability_profile_metadata_matches_release() {{
    [ "$KIP_TEST_METADATA_OK" = 1 ]
}}
KIP_COST_PROFILE_NAME="$KIP_TEST_REQUESTED_NAME"
if [ -n "$KIP_COST_PROFILE_NAME" ]; then KIP_COST_PROFILE_HASH=requested-hash; fi
kip_observability_prepare_install_pricing_state
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env={
            **os.environ,
            "KIP_TEST_RELEASE_STATE": release_state,
            "KIP_TEST_ACTIVE_STATE": active_state,
            "KIP_TEST_LOAD_OK": load_ok,
            "KIP_TEST_METADATA_OK": metadata_ok,
            "KIP_TEST_REQUESTED_NAME": requested_name,
            "KIP_TEST_ACTIVE_NAME": active_name,
            "NO_COLOR": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == expected, completed.stderr


def test_install_preserves_pricing_state_before_unhealthy_release_recovery() -> None:
    install = (OBSERVABILITY / "install.sh").read_text()
    assert install.index("kip_observability_prepare_install_pricing_state") < install.index(
        "kip_observability_recover_unhealthy_release"
    )


@pytest.mark.parametrize(
    ("identity_exact", "healthy", "expected", "uninstalls"),
    [("1", "0", 0, 1), ("1", "1", 0, 0), ("0", "0", 1, 0)],
)
def test_unhealthy_release_recovery_never_uninstalls_drift(
    tmp_path: Path,
    identity_exact: str,
    healthy: str,
    expected: int,
    uninstalls: int,
) -> None:
    log = tmp_path / "helm.log"
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{OBSERVABILITY / "versions.env"}"
source "{ROOT / "deploy" / "cluster" / "lib" / "common.sh"}"
KIP_REPOSITORY_ROOT="{ROOT}"
source "{OBSERVABILITY / "lib" / "common.sh"}"
kip_observability_release_exists() {{ return 0; }}
kip_observability_release_identity_exact() {{ [ "$KIP_TEST_IDENTITY_EXACT" = 1 ]; }}
kip_observability_release_exact() {{ [ "$KIP_TEST_HEALTHY" = 1 ]; }}
kip_exec() {{ printf '%s\n' "$*" >> "$KIP_TEST_LOG"; }}
kip_observability_recover_unhealthy_release "$KIP_MONITORING_RELEASE"
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env={
            **os.environ,
            "KIP_TEST_IDENTITY_EXACT": identity_exact,
            "KIP_TEST_HEALTHY": healthy,
            "KIP_TEST_LOG": str(log),
            "NO_COLOR": "1",
        },
        check=False,
    )
    assert completed.returncode == expected
    recorded = log.read_text() if log.exists() else ""
    assert recorded.count("helm uninstall") == uninstalls


def test_crd_ownership_records_manager_and_chart_digest() -> None:
    common = (OBSERVABILITY / "lib" / "common.sh").read_text()
    assert "kip_observability_mark_crds_owned" in common
    assert "app.kubernetes.io/managed-by=kube-inference-platform" in common
    assert "kube-inference-platform.io/chart-digest" in common
    assert "kip_observability_crds_owned" in common


@pytest.mark.parametrize(
    ("owner", "expected"),
    [("kube-inference-platform", 0), ("someone-else", 1)],
)
def test_named_storage_collision_requires_kip_ownership(owner: str, expected: int) -> None:
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{OBSERVABILITY / "versions.env"}"
source "{ROOT / "deploy" / "cluster" / "lib" / "common.sh"}"
KIP_REPOSITORY_ROOT="{ROOT}"
source "{OBSERVABILITY / "lib" / "common.sh"}"
kip_kubectl() {{
    printf '{{"metadata":{{"labels":{{"app.kubernetes.io/managed-by":"%s"}}}}}}' \
        "$KIP_TEST_OWNER"
}}
kip_observability_resource_owned_or_absent persistentvolume kip-prometheus
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env={**os.environ, "KIP_TEST_OWNER": owner, "NO_COLOR": "1"},
        check=False,
    )
    assert completed.returncode == expected


def test_opencost_chart_workaround_reconciles_owned_custom_cost_flag() -> None:
    common = (OBSERVABILITY / "lib" / "common.sh").read_text()
    install = (OBSERVABILITY / "install.sh").read_text()
    cost = (OBSERVABILITY / "cost.sh").read_text()
    assert "CUSTOM_COST_ENABLED=$enabled" in common
    assert "plugins.enabled" in common  # Documents the pinned chart defect.
    assert "kip_observability_set_custom_cost_enabled true" in install
    assert "kip_observability_set_custom_cost_enabled false" in install
    assert "kip_observability_profile_pricing_matches_release" in cost
    assert "kip_observability_custom_cost_enabled_is true" in common


def test_smoke_uses_bounded_metric_convergence_wait() -> None:
    health = (OBSERVABILITY / "lib" / "health.sh").read_text()
    smoke = (OBSERVABILITY / "smoke.sh").read_text()
    assert "kip_observability_wait_for_required_metrics" in health
    assert "KIP_OBSERVABILITY_METRICS_ATTEMPTS:-24" in health
    assert "KIP_OBSERVABILITY_METRICS_WAIT_SECONDS:-10" in health
    assert "kip_observability_wait_for_required_metrics" in smoke
    assert '"$KIP_RUNTIME_ROOT/dispatch.sh" smoke' in smoke
    assert "Inference telemetry stimulus" in smoke


REQUIRED_OBSERVABILITY_METRICS = (
    "up",
    "kip:runtime:pod_info",
    "kip:runtime:replicas_ready",
    "kip:runtime:container_restarts",
    "kip:requests:running",
    "kip:requests:waiting",
    "kip:requests:rate5m",
    "kip:requests:success_rate5m",
    "kip:requests:error_rate5m",
    "kip:tokens:prompt_rate5m",
    "kip:tokens:generation_rate5m",
    "kip:tokens:total_rate5m",
    "kip:kv_cache:usage_ratio",
    "kip:preemptions:rate5m",
    "kip:latency:ttft_seconds_p50",
    "kip:latency:ttft_seconds_p95",
    "kip:latency:ttft_seconds_p99",
    "kip:latency:tpot_seconds_p50",
    "kip:latency:tpot_seconds_p95",
    "kip:latency:tpot_seconds_p99",
    "kip:latency:e2e_seconds_p50",
    "kip:latency:e2e_seconds_p95",
    "kip:latency:e2e_seconds_p99",
    "kip:latency:queue_seconds_p50",
    "kip:latency:queue_seconds_p95",
    "kip:latency:queue_seconds_p99",
    "kip:latency:prefill_seconds_p50",
    "kip:latency:prefill_seconds_p95",
    "kip:latency:prefill_seconds_p99",
    "kip:latency:decode_seconds_p50",
    "kip:latency:decode_seconds_p95",
    "kip:latency:decode_seconds_p99",
    "kip:gpu:utilization_ratio",
    "kip:gpu:memory_used_bytes",
    "kip:gpu:memory_total_bytes",
    "kip:gpu:power_watts",
    "kip:gpu:temperature_celsius",
    "kip:gpu:xid_errors",
    "kip:gpu:clock_throttle",
    "kip:gpu:sm_clock_mhz",
    "kip:gpu:memory_clock_mhz",
    "kip:gpu:runtime_utilization_ratio",
    "kip:gpu:runtime_memory_used_bytes",
    "kip:gpu:runtime_memory_total_bytes",
    "kip:gpu:runtime_power_watts",
    "kip:gpu:runtime_temperature_celsius",
    "kip:gpu:runtime_xid_errors",
    "kip:gpu:runtime_clock_throttle",
    "kip:gpu:runtime_sm_clock_mhz",
    "kip:gpu:runtime_memory_clock_mhz",
    "kip:kubernetes:nodes_ready",
    "kip:kubernetes:pods_unready",
    "kip:node:cpu_utilization_ratio",
    "kip:node:memory_utilization_ratio",
    "kip:node:filesystem_available_ratio",
    "kip:node:network_receive_bytes_per_second",
    "kip:node:network_transmit_bytes_per_second",
    '{__name__=~"cilium_.+"}',
)


def test_every_required_metric_family_is_a_hard_acceptance_gate() -> None:
    metrics = " ".join(shlex.quote(metric) for metric in REQUIRED_OBSERVABILITY_METRICS)
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{OBSERVABILITY / "lib" / "health.sh"}"
kip_observability_release_pricing_state() {{ printf '%s\\n' unpriced; }}
kip_observability_query() {{ [ "$1" != "$KIP_TEST_MISSING" ]; }}
for missing in {metrics}; do
    KIP_TEST_MISSING=$missing
    if kip_observability_required_metrics_ready; then
        printf 'unexpected success for %s\\n' "$missing" >&2
        exit 1
    fi
    [ "$KIP_OBSERVABILITY_MISSING_METRIC" = "$missing" ] || exit 1
done
KIP_TEST_MISSING=none
kip_observability_required_metrics_ready
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env={**os.environ, "NO_COLOR": "1", "KIP_VERBOSE": "0"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_manifests_keep_services_internal_and_scrape_at_required_rates() -> None:
    monitors = (OBSERVABILITY / "manifests" / "service-monitors.yaml").read_text()
    monitoring = (OBSERVABILITY / "values" / "kube-prometheus-stack.yaml").read_text()
    opencost = (OBSERVABILITY / "values" / "opencost-unpriced.yaml").read_text()
    combined = f"{monitoring}\n{opencost}\n{monitors}"

    assert "NodePort" not in combined
    assert "LoadBalancer" not in combined
    assert "type: ClusterIP" in combined
    assert "name: kip-vllm" in monitors
    assert "name: kip-dcgm-exporter" in monitors
    assert "interval: 5s" in monitors
    assert "scrapeTimeout: 3s" in monitors
    assert "sourceLabels: [__meta_kubernetes_pod_name]" in monitors
    assert "sourceLabels: [__meta_kubernetes_pod_node_name]" in monitors
    assert (
        "sourceLabels: [__meta_kubernetes_pod_label_kube_inference_platform_io_deployment]"
        in monitors
    )
    assert "__meta_kubernetes_service_label_kube_inference_platform_io_worker_role" not in monitors
    assert "__meta_kubernetes_pod_label_kube_inference_platform_io_worker_role" in monitors
    assert "targetLabel: model_revision" in monitors
    assert "targetLabel: replica" in monitors
    assert "targetLabel: pipeline_rank" in monitors
    assert "targetLabel: tensor_rank" in monitors
    assert "collectionInterval: 1m" in opencost
    assert "modelLabel: kube-inference-platform.io/served-model" in opencost
    assert "sharedInfraLabel: kube-inference-platform.io/inference-shared" in opencost
    assert "clusterName: cluster.local" in opencost
    assert "defaultClusterId: kip-v01" in opencost
    assert "searchNamespace: kip-observability" in monitoring
    for component in (
        "kubeControllerManager",
        "kubeEtcd",
        "kubeProxy",
        "kubeScheduler",
    ):
        assert f"{component}:\n  enabled: false" in monitoring


@pytest.mark.parametrize(("failure", "expected"), [("none", 0), ("down", 1), ("missing", 1)])
def test_required_prometheus_target_health_gate(
    failure: str, expected: int, tmp_path: Path
) -> None:
    pools = [
        "/kip-vllm/",
        "/kip-dcgm-exporter/",
        "/kip-cilium-agent/",
        "/kip-cilium-operator/",
        "/kip-opencost/",
        "/kip-monitoring-prometheus-node-exporter/",
        "/kip-monitoring-kube-state-metrics/",
        "/kip-monitoring-kubelet/",
        "/kip-monitoring-apiserver/",
        "/kip-monitoring-coredns/",
        "/kip-monitoring-operator/",
    ]
    targets = [
        {
            "scrapePool": f"serviceMonitor/kip-observability{pool}0",
            "health": "up",
        }
        for pool in pools
    ]
    if failure == "down":
        targets[0]["health"] = "down"
    elif failure == "missing":
        targets.pop()
    fixture = tmp_path / "targets.json"
    fixture.write_text(json.dumps({"status": "success", "data": {"activeTargets": targets}}))
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{OBSERVABILITY / "versions.env"}"
source "{ROOT / "deploy" / "cluster" / "lib" / "common.sh"}"
KIP_REPOSITORY_ROOT="{ROOT}"
source "{OBSERVABILITY / "lib" / "common.sh"}"
source "{OBSERVABILITY / "lib" / "health.sh"}"
kip_observability_api_raw() {{ command cat "$KIP_TARGET_FIXTURE"; }}
kip_observability_targets_healthy
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env={**os.environ, "KIP_TARGET_FIXTURE": str(fixture), "NO_COLOR": "1"},
        check=False,
    )
    assert completed.returncode == expected


def test_scrape_target_discovery_waits_for_initial_convergence() -> None:
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{OBSERVABILITY / "lib" / "health.sh"}"
KIP_OBSERVABILITY_TARGET_ATTEMPTS=2
KIP_OBSERVABILITY_TARGET_WAIT_SECONDS=1
calls=0
kip_observability_targets_healthy() {{
    calls=$((calls + 1))
    [ "$calls" -eq 2 ]
}}
kip_observability_wait_for_targets
printf '%s' "$calls"
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout == "2"


@pytest.mark.parametrize(
    ("service_type", "ingress_count", "expected"),
    [("ClusterIP", 0, 0), ("NodePort", 0, 1), ("LoadBalancer", 0, 1), ("ClusterIP", 1, 1)],
)
def test_live_observability_network_surface_must_remain_internal(
    service_type: str, ingress_count: int, expected: int, tmp_path: Path
) -> None:
    services = {
        "items": [
            {
                "spec": {"type": service_type, "ports": [{"port": 9090}]},
                "status": {"loadBalancer": {}},
            }
        ]
    }
    ingresses = {"items": [{} for _ in range(ingress_count)]}
    services_path = tmp_path / "services.json"
    ingresses_path = tmp_path / "ingresses.json"
    services_path.write_text(json.dumps(services))
    ingresses_path.write_text(json.dumps(ingresses))
    script = f'''
source "{OBSERVABILITY / "lib" / "health.sh"}"
kip_exec() {{ "$@"; }}
kip_kubectl() {{
    case " $* " in
        *" get services "*) command cat "{services_path}" ;;
        *" get ingresses "*) command cat "{ingresses_path}" ;;
        *) return 1 ;;
    esac
}}
KIP_OBSERVABILITY_NAMESPACE=kip-observability
kip_observability_network_surface_internal
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script], cwd=ROOT, check=False, capture_output=True, text=True
    )
    assert completed.returncode == expected, completed.stderr


def test_kube_state_metrics_exports_only_required_inference_labels() -> None:
    monitoring = (OBSERVABILITY / "values" / "kube-prometheus-stack.yaml").read_text()
    assert "metricLabelsAllowlist:" in monitoring
    for resource in ("pods=[", "deployments=["):
        assert resource in monitoring
    for label in (
        "kube-inference-platform.io/runtime",
        "kube-inference-platform.io/model-profile",
        "kube-inference-platform.io/model-revision",
        "kube-inference-platform.io/served-model",
        "kube-inference-platform.io/worker-role",
        "kube-inference-platform.io/inference-shared",
    ):
        assert monitoring.count(label) == 2
    assert "pods=[*]" not in monitoring
    assert "deployments=[*]" not in monitoring


def test_recording_rules_and_alerts_use_stable_contract() -> None:
    rules = (OBSERVABILITY / "manifests" / "prometheus-rules.yaml").read_text()
    required_records = {
        "kip:requests:running",
        "kip:requests:waiting",
        "kip:requests:rate5m",
        "kip:tokens:prompt_rate5m",
        "kip:tokens:generation_rate5m",
        "kip:tokens:total_rate5m",
        "kip:latency:ttft_seconds_p50",
        "kip:latency:ttft_seconds_p95",
        "kip:latency:ttft_seconds_p99",
        "kip:latency:tpot_seconds_p99",
        "kip:latency:e2e_seconds_p99",
        "kip:kv_cache:usage_ratio",
        "kip:gpu:utilization_ratio",
        "kip:gpu:power_watts",
        "kip:gpu:sm_clock_mhz",
        "kip:gpu:runtime_utilization_ratio",
        "kip:gpu:runtime_memory_used_bytes",
        "kip:gpu:runtime_power_watts",
        "kip:gpu:runtime_temperature_celsius",
        "kip:gpu:runtime_xid_errors",
        "kip:kubernetes:nodes_ready",
        "kip:node:cpu_utilization_ratio",
        "kip:node:memory_utilization_ratio",
        "kip:node:filesystem_available_ratio",
        "kip:node:network_receive_bytes_per_second",
        "kip:cost:cluster_hourly_usd",
        "kip:cost:gpu_hourly_usd_per_device",
        "kip:cost:inference_hourly_usd",
        "kip:cost:per_million_tokens_usd",
        "kip:cost:per_request_usd",
        "kip:cost:runtime_gpu_requested_hourly_usd",
        "kip:cost:shared_gpu_requested_hourly_usd",
        "kip:cost:unattributed_gpu_requested_hourly_usd",
        "kip:cost:gpu_unrequested_hourly_usd",
        "kip:efficiency:tokens_per_usd",
        "kip:energy:runtime_watt_hours_per_request",
        "kip:energy:runtime_kilowatt_hours_per_million_tokens",
    }
    assert required_records <= {
        line.split("record:", 1)[1].strip() for line in rules.splitlines() if "record:" in line
    }
    for alert in (
        "KIPRuntimeTargetMissing",
        "KIPDCGMExporterMissing",
        "KIPRuntimeQueueGrowing",
        "KIPKVCachePressure",
        "KIPGPUTemperatureHigh",
        "KIPGPUXIDError",
        "KIPPersistentVolumeFilling",
        "KIPOpenCostStale",
    ):
        assert f"alert: {alert}" in rules
    assert "vllm:request_time_per_output_token_seconds_bucket" in rules
    assert "vllm:inter_token_latency_seconds_bucket" not in rules
    assert "vllm:time_per_output_token_seconds_bucket" not in rules
    assert "kip:cost:inference_hourly_usd / on (cluster, namespace, model_name)" in rules
    assert 'kip:cost:inference_hourly_usd{cost_basis="allocation"} / on' not in rules
    assert 'finished_reason!~"abort|error"' in rules
    assert "or 0 * kip:requests:rate5m" in rules
    assert "http_requests_total" in rules
    assert "llm_total_hourly_cost" in rules
    assert "llm_cost_per_million_tokens" in rules
    assert "container_gpu_allocation" in rules
    assert "group_left() kip:cost:gpu_hourly_usd_per_device" in rules
    assert "vllm:request_failure_total" not in rules
    assert "gpu_uuid" in rules
    assert "gpu_model" in rules
    assert "kip:runtime:pod_identity" in rules
    assert "kip:runtime:deployment_identity" in rules
    assert "kube_deployment_status_replicas_ready" in rules
    assert "kube_pod_container_status_restarts_total" in rules
    assert "count by (cluster, namespace, deployment) (kip:runtime:deployment_info) == 1" in rules
    assert "group_left (runtime, model_profile, model_name, model_revision, worker_role)" in rules
    assert "worker_role, replica, pipeline_rank, tensor_rank" in rules
    assert "on (cluster, namespace, node, pod)" in rules
    assert "group_left (deployment, runtime, model_profile, model_name" in rules
    assert "vllm:kv_cache_usage_perc) / 100" not in rules
    assert 'enabled: "false"' in rules
    assert "absent_over_time(node_gpu_hourly_cost[5m]) and on() kip:cost:profile_info" in rules


def test_unpriced_opencost_defaults_cannot_publish_kip_costs() -> None:
    rules_path = OBSERVABILITY / "manifests" / "prometheus-rules.yaml"
    resource = yaml.safe_load(rules_path.read_text())
    cost_rules = [
        rule
        for group in resource["spec"]["groups"]
        for rule in group["rules"]
        if rule.get("record", "").startswith(("kip:cost:", "kip:efficiency:"))
    ]
    assert cost_rules
    assert all("kip:cost:profile_info" in rule["expr"] for rule in cost_rules)

    dashboard = json.loads((OBSERVABILITY / "dashboards" / "inference-cost.json").read_text())
    status = next(panel for panel in dashboard["panels"] if panel["title"] == "Pricing status")
    assert status["targets"][0]["expr"] == (
        "1 - clamp_max(count(kip:cost:profile_info) or vector(0), 1)"
    )
    mappings = status["fieldConfig"]["defaults"]["mappings"][0]["options"]
    assert mappings["0"]["text"] == "Configured"
    assert mappings["1"]["text"] == "Pricing not configured"


def test_six_dashboards_are_valid_and_future_ready() -> None:
    dashboards = sorted((OBSERVABILITY / "dashboards").glob("*.json"))
    assert len(dashboards) == 6
    uids: set[str] = set()
    required_dimensions = {
        "cluster",
        "namespace",
        "deployment",
        "runtime",
        "model_profile",
        "model_name",
        "worker_role",
        "replica",
        "pipeline_rank",
        "tensor_rank",
        "node",
        "pod",
        "gpu_uuid",
    }
    all_content = ""
    for path in dashboards:
        dashboard = json.loads(path.read_text())
        assert dashboard["uid"].startswith("kip-")
        assert dashboard["uid"] not in uids
        assert dashboard["editable"] is False
        assert dashboard["timezone"] == "utc"
        assert dashboard["panels"]
        assert required_dimensions <= {
            variable["name"] for variable in dashboard["templating"]["list"]
        }
        uids.add(dashboard["uid"])
        all_content += path.read_text()

    assert "Pricing not configured" in all_content
    assert "kip:latency:ttft_seconds_p95" in all_content
    assert "kip:gpu:runtime_power_watts" in all_content
    assert "kip:efficiency:tokens_per_usd" in all_content
    assert "kip:cost:inference_hourly_usd" in all_content
    assert "kip:cost:per_million_tokens_usd" in all_content
    assert "kip:cost:per_request_usd" in all_content
    assert 'cost_basis=\\"allocation\\"' in all_content
    assert 'cost_basis=\\"usage\\"' in all_content
    assert "allocated USD/request" in all_content
    assert "usage USD/request" in all_content
    assert "kip:cost:shared_gpu_requested_hourly_usd" in all_content
    assert "kip:cost:unattributed_gpu_requested_hourly_usd" in all_content
    assert '"legendFormat":"idle"' not in all_content
    assert "1 - clamp_max(count(kip:cost:profile_info) or vector(0), 1)" in all_content

    benchmark = json.loads((OBSERVABILITY / "dashboards" / "benchmark-explorer.json").read_text())
    annotation = benchmark["annotations"]["list"][0]
    assert annotation["target"] == {
        "type": "tags",
        "tags": ["kip-benchmark"],
        "limit": 100,
        "matchAny": False,
    }
    assert "run_id" not in {item["name"] for item in benchmark["templating"]["list"]}


@pytest.mark.parametrize("drift", [False, True])
def test_live_dashboard_set_and_content_are_exact(tmp_path: Path, drift: bool) -> None:
    objects = {}
    names = []
    for dashboard in sorted((OBSERVABILITY / "dashboards").glob("*.json")):
        name = f"kip-dashboard-{dashboard.stem}"
        names.append(name)
        content = dashboard.read_text()
        if drift and dashboard.stem == "platform-fleet":
            content += " "
        objects[name] = {
            "metadata": {
                "name": name,
                "labels": {
                    "app.kubernetes.io/managed-by": "kube-inference-platform",
                    "grafana_dashboard": "1",
                },
            },
            "data": {f"{dashboard.stem}.json": content},
        }
        (tmp_path / f"{name}.json").write_text(json.dumps(objects[name]))
    (tmp_path / "list.json").write_text(
        json.dumps({"items": [{"metadata": {"name": name}} for name in names]})
    )
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{OBSERVABILITY / "versions.env"}"
KIP_OBSERVABILITY_ROOT="{OBSERVABILITY}"
source "{OBSERVABILITY / "lib" / "health.sh"}"
kip_exec() {{ command "$@"; }}
kip_kubectl() {{
    case " $* " in
        *" get configmaps "*) command cat "{tmp_path / "list.json"}" ;;
        *" get configmap kip-dashboard-"*)
            for argument in "$@"; do
                case "$argument" in
                    kip-dashboard-*) command cat "{tmp_path}/$argument.json"; return ;;
                esac
            done
            return 1 ;;
        *) return 1 ;;
    esac
}}
kip_observability_dashboards_present
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script], cwd=ROOT, check=False, capture_output=True, text=True
    )
    assert completed.returncode == (1 if drift else 0), completed.stderr


@pytest.mark.parametrize("drift", [False, True])
def test_live_scrape_and_rule_specs_are_exact(tmp_path: Path, drift: bool) -> None:
    documents = [
        document
        for document in yaml.safe_load_all(
            (OBSERVABILITY / "manifests" / "service-monitors.yaml").read_text()
        )
        if document and document["kind"] == "ServiceMonitor"
    ]
    documents.append(
        yaml.safe_load((OBSERVABILITY / "manifests" / "prometheus-rules.yaml").read_text())
    )
    for index, document in enumerate(documents):
        if drift and index == 0:
            document["spec"]["endpoints"][0]["interval"] = "30s"
        document["metadata"].setdefault("labels", {})["app.kubernetes.io/managed-by"] = (
            "kube-inference-platform"
        )
        key = f"{document['kind'].lower()}-{document['metadata']['name']}"
        (tmp_path / f"{key}.json").write_text(json.dumps(document))
    script = f'''
source "{ROOT / "deploy" / "lib" / "ui.sh"}"
source "{OBSERVABILITY / "versions.env"}"
source "{OBSERVABILITY / "lib" / "health.sh"}"
kip_exec() {{ command "$@"; }}
kip_observability_crds_owned() {{ return 0; }}
kip_kubectl() {{
    local kind="" name="" previous=""
    for argument in "$@"; do
        if [ "$previous" = get ]; then kind=$argument; fi
        if [ -n "$kind" ] && [ "$previous" = "$kind" ]; then name=$argument; fi
        previous=$argument
    done
    command cat "{tmp_path}/$kind-$name.json"
}}
KIP_OBSERVABILITY_NAMESPACE=kip-observability
kip_observability_crs_present
'''
    completed = subprocess.run(
        ["/bin/bash", "-c", script], cwd=ROOT, check=False, capture_output=True, text=True
    )
    assert completed.returncode == (1 if drift else 0), completed.stderr


def test_runtime_dashboards_filter_workloads_and_use_attributed_gpu_metrics() -> None:
    workload_metric_prefixes = (
        "kip:requests:",
        "kip:tokens:",
        "kip:latency:",
        "kip:kv_cache:",
        "kip:gpu:runtime_",
        "kip:runtime:pod_info",
        "kip:energy:runtime_",
    )
    workload_variables = {
        "$cluster",
        "$namespace",
        "$deployment",
        "$runtime",
        "$model_profile",
        "$model_name",
        "$worker_role",
        "$replica",
        "$pipeline_rank",
        "$tensor_rank",
    }

    for path in sorted((OBSERVABILITY / "dashboards").glob("*.json")):
        dashboard = json.loads(path.read_text())
        variables = {item["name"]: item for item in dashboard["templating"]["list"]}
        gpu_query = variables["gpu_uuid"]["query"]["query"]
        assert "kip:gpu:runtime_utilization_ratio" in gpu_query

        for panel in dashboard["panels"]:
            for target in panel.get("targets", []):
                expression = target.get("expr", "")
                if any(prefix in expression for prefix in workload_metric_prefixes):
                    assert workload_variables <= {
                        variable for variable in workload_variables if variable in expression
                    }, f"{path.name}: {panel['title']} does not fully scope {expression}"

    all_content = "\n".join(
        path.read_text() for path in (OBSERVABILITY / "dashboards").glob("*.json")
    )
    assert "avg(kip:gpu:utilization_ratio)" not in all_content
    assert "sum(kip:gpu:power_watts)" not in all_content


def test_storage_contract_uses_local_retained_volumes() -> None:
    storage = (OBSERVABILITY / "manifests" / "storage.yaml.tpl").read_text()
    assert "provisioner: kubernetes.io/no-provisioner" in storage
    assert storage.count("persistentVolumeReclaimPolicy: Retain") == 3
    assert "storage: 30Gi" in storage
    assert storage.count("storage: 2Gi") >= 3
    assert "__NODE_NAME__" in storage
    assert "__STORAGE_ROOT__" in storage


def test_no_persistent_credentials_or_external_cost_integrations() -> None:
    content = "\n".join(path.read_text() for path in OBSERVABILITY.rglob("*.*") if path.is_file())
    assert "cloudIntegrationJSON:" not in content
    assert "cloudProviderApiKey:" not in content
    assert "billingDataDataset" not in content
    assert "ADMIN_TOKEN" not in content
    assert "openssl rand -base64 32" in content
