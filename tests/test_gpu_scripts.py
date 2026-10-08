"""Deterministic subprocess tests for the NVIDIA GPU stack tooling."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
INSTALL_SCRIPT = ROOT / "deploy" / "gpu" / "install.sh"
VALIDATE_SCRIPT = ROOT / "deploy" / "gpu" / "validate.sh"
SMOKE_SCRIPT = ROOT / "deploy" / "gpu" / "smoke.sh"
CHART_DIGEST = "sha256:23268170f1b608365ef8c15e0c0df87bff0017768e048bd9e73e41a1096299e4"
COSIGN_SHA = "4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71"


def write_command(command_dir: Path, name: str, body: str) -> None:
    command = command_dir / name
    command.write_text(f"#!/bin/sh\n{body}\n")
    command.chmod(0o755)


@pytest.fixture
def gpu_fixture(tmp_path: Path) -> dict[str, Path]:
    command_dir = tmp_path / "commands"
    command_dir.mkdir()
    command_log = tmp_path / "commands.log"
    release_state = tmp_path / "release-installed"
    release_mode = tmp_path / "release-mode"
    release_dcgm = tmp_path / "release-dcgm"
    smoke_state = tmp_path / "smoke-created"
    kubeconfig = tmp_path / "admin.conf"
    kubeconfig.write_text("fixture\n")
    cluster_check = tmp_path / "cluster-check"
    cluster_check.write_text(
        "#!/bin/sh\n"
        "printf 'cluster-check\\n' >> \"$KIP_TEST_COMMAND_LOG\"\n"
        '[ "${KIP_TEST_CLUSTER_FAIL:-0}" != 1 ]\n'
    )
    cluster_check.chmod(0o755)

    jq_path = shutil.which("jq")
    assert jq_path is not None
    write_command(command_dir, "jq", f'exec "{jq_path}" "$@"')
    write_command(command_dir, "uname", "printf '%s\\n' '6.8.0-test-generic'")
    write_command(
        command_dir,
        "apt-cache",
        """[ "${KIP_TEST_KERNEL_HEADERS_UNAVAILABLE:-0}" != 1 ] || exit 0
printf '%s\\n' 'linux-headers-6.8.0-test-generic | 6.8.0-test | fixture'""",
    )
    write_command(
        command_dir,
        "lspci",
        """printf 'lspci %s\\n' "$*" >> "$KIP_TEST_COMMAND_LOG"
[ "${KIP_TEST_NO_GPU:-0}" = 1 ] && exit 0
printf '%s\\n' '0000:01:00.0 0302: 10de:2330 (rev a1)'""",
    )
    write_command(
        command_dir,
        "nvidia-smi",
        """printf 'nvidia-smi %s\\n' "$*" >> "$KIP_TEST_COMMAND_LOG"
[ "${KIP_TEST_HOST_DRIVER:-unhealthy}" = healthy ] || exit 1
case "$*" in
    'topo -m') printf '%s\\n' 'GPU0 CPU Affinity' 'GPU0 X 0-31' ;;
    *) printf '%s\\n' '0, Test GPU, GPU-host-uuid, 595.91.07' ;;
esac""",
    )
    write_command(
        command_dir,
        "sha256sum",
        """if [ "${KIP_TEST_COSIGN_CHECKSUM_FAIL:-0}" = 1 ]; then
    printf '%s  %s\\n' bad "$1"
else
    printf '%s  %s\\n' "$KIP_TEST_COSIGN_SHA" "$1"
fi""",
    )
    write_command(
        command_dir,
        "curl",
        """output=''
while [ "$#" -gt 0 ]; do
    if [ "$1" = '-o' ]; then shift; output=$1; fi
    shift
done
[ -n "$output" ] || exit 1
case "$output" in
    */cosign)
        printf '%s\\n' '#!/bin/sh' \
            '[ "${KIP_TEST_COSIGN_FAIL:-0}" != 1 ] || exit 1' \
            'printf "%s\\n" "Verified OK"' > "$output"
        ;;
    *) printf '%s\\n' 'PUBLIC KEY' > "$output" ;;
esac""",
    )
    write_command(
        command_dir,
        "helm",
        """printf 'helm %s\\n' "$*" >> "$KIP_TEST_COMMAND_LOG"
case "${1:-}" in
    pull)
        printf '%s\\n' 'Pulled: nvcr.io/nvidia/cloud-native-charts/gpu-operator:v26.7.0'
        printf 'Digest: %s\\n' "${KIP_TEST_CHART_DIGEST:-$KIP_TEST_EXPECTED_CHART_DIGEST}"
        ;;
    list)
        if [ -e "$KIP_TEST_RELEASE_STATE" ]; then
            printf '%s' '[{"name":"gpu-operator","namespace":"gpu-operator",'
            printf '"chart":"gpu-operator-%s"}]\\n' \
                "${KIP_TEST_RELEASE_VERSION:-v26.7.0}"
        elif [ "${KIP_TEST_UNMANAGED_RELEASE:-0}" = 1 ]; then
            printf '%s' '[{"name":"gpu-operator","namespace":"other",'
            printf '%s\\n' '"chart":"gpu-operator-v26.7.0"}]'
        else
            printf '%s\\n' '[]'
        fi
        ;;
    status)
        [ -e "$KIP_TEST_RELEASE_STATE" ] || exit 1
        printf '{"info":{"status":"%s"}}\\n' "${KIP_TEST_RELEASE_STATUS:-deployed}"
        ;;
    get)
        [ -e "$KIP_TEST_RELEASE_STATE" ] || exit 1
        mode=$(cat "$KIP_TEST_RELEASE_MODE")
        dcgm=false
        [ ! -r "$KIP_TEST_RELEASE_DCGM" ] || dcgm=$(cat "$KIP_TEST_RELEASE_DCGM")
        [ "$mode" = operator ] && enabled=true || enabled=false
        printf '{"kip":{"driverMode":"%s","chartDigest":"%s"' \
            "$mode" "${KIP_TEST_RECORDED_DIGEST:-$KIP_TEST_EXPECTED_CHART_DIGEST}"
        [ "${KIP_TEST_OMIT_DCGM_MARKER:-0}" = 1 ] \
            || printf ',"dcgmExporterEnabled":%s' "${KIP_TEST_RECORDED_DCGM:-$dcgm}"
        printf '%s' '},'
        printf '%s' '"clusterPolicy":{"deployCR":true},'
        printf '%s' '"gpuCluster":{"deployCR":false},"psa":{"enabled":true},'
        printf '"nfd":{"enabled":true},"cdi":{"enabled":%s,"nriPluginEnabled":false},' \
            "${KIP_TEST_CDI_ENABLED:-true}"
        printf '%s' '"toolkit":{"enabled":true,"env":['
        printf '%s' '{"name":"CONTAINERD_CONFIG","value":"/etc/containerd/config.toml"},'
        printf '%s' '{"name":"CONTAINERD_SOCKET","value":"/run/containerd/containerd.sock"},'
        printf '%s%s' '{"name":"RUNTIME_CONFIG_SOURCE",' \
            '"value":"file=/etc/containerd/config.toml"},'
        printf '%s%s' '{"name":"RUNTIME_DROP_IN_CONFIG",' \
            '"value":"/etc/containerd/conf.d/99-nvidia.toml"}]},'
        printf '%s' '"devicePlugin":{"enabled":true},"dcgm":{"enabled":false},'
        printf '"dcgmExporter":{"enabled":%s,' "$dcgm"
        printf '%s' '"repository":"nvcr.io/nvidia/k8s",'
        printf '%s' '"image":"dcgm-exporter",'
        printf '%s%s' '"version":"4.6.0-4.8.3-distroless@sha256:' \
            'b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a",'
        printf '%s' '"serviceMonitor":{"enabled":false},'
        printf '%s' '"config":{"name":"kip-dcgm-metrics"},'
        printf '%s' '"env":[{"name":"DCGM_EXPORTER_INTERVAL","value":"1000"},'
        printf '%s%s' '{"name":"DCGM_EXPORTER_COLLECTORS",' \
            '"value":"/etc/dcgm-exporter/dcgm-metrics.csv"}]},'
        printf '%s' '"mig":{"strategy":"none"},'
        printf '%s' '"migManager":{"enabled":false},'
        [ "${KIP_TEST_EXTRA_VALUE:-0}" != 1 ] \
            || printf '%s' '"unrelated":{"enabled":true},'
        printf '"driver":{"enabled":%s,"version":"595.91.07"}}\\n' "$enabled"
        ;;
    upgrade)
        [ "${KIP_TEST_HELM_FAIL:-0}" != 1 ] || exit 1
        mode=operator
        dcgm=false
        for argument in "$@"; do
            case "$argument" in
                kip.driverMode=*) mode=${argument#*=} ;;
                dcgmExporter.enabled=*) dcgm=${argument#*=} ;;
            esac
        done
        printf '%s' "$mode" > "$KIP_TEST_RELEASE_MODE"
        printf '%s' "$dcgm" > "$KIP_TEST_RELEASE_DCGM"
        touch "$KIP_TEST_RELEASE_STATE"
        ;;
esac""",
    )
    write_command(
        command_dir,
        "kubectl",
        """args="$*"
printf 'kubectl %s\\n' "$args" >> "$KIP_TEST_COMMAND_LOG"
case "$args" in
    *'get nodes -o jsonpath='*)
        if [ "${KIP_TEST_NODE_COUNT:-1}" = 2 ]; then
            printf '%s\\n' 'gpu-node-1||True' 'gpu-node-2||True'
        else
            printf '%s\\n' 'gpu-node-1||True'
        fi
        ;;
    *'get nodes -o json')
        capacity=${KIP_TEST_GPU_CAPACITY:-1}
        allocatable=${KIP_TEST_GPU_ALLOCATABLE:-$capacity}
        mig_resource=''
        mig_label=${KIP_TEST_MIG_LABEL:-}
        [ "${KIP_TEST_MIG_RESOURCE:-0}" = 1 ] \
            && mig_resource=',"nvidia.com/mig-1g.10gb":"1"'
        printf '{"items":[{"metadata":{"name":"gpu-node-1",' 
        printf '"labels":{"nvidia.com/mig.config":"%s"}},' "$mig_label"
        printf '"status":{"capacity":{"nvidia.com/gpu":"%s"%s},' \
            "$capacity" "$mig_resource"
        printf '"allocatable":{"nvidia.com/gpu":"%s"%s}}}]}\\n' \
            "$allocatable" "$mig_resource"
        ;;
    *'get configmaps --all-namespaces -o json')
        if [ "${KIP_TEST_TIME_SLICING:-0}" = 1 ]; then
            printf '%s' '{"items":[{"metadata":{"name":"device-plugin-config"},'
            printf '%s\\n' '"data":{"config":"sharing:\\n  timeSlicing:\\n    replicas: 2"}}]}'
        else
            printf '%s' '{"items":[{"metadata":{"name":"kip-whole-gpu"},'
            printf '%s\\n' '"data":{"whole":"version: v1\\nflags:\\n  migStrategy: none"}}]}'
        fi
        ;;
    *'get crd clusterpolicies.nvidia.com'*)
        [ "${KIP_TEST_ORPHAN_CRD:-0}" = 1 ] \
            && printf '%s%s\\n' 'customresourcedefinition.apiextensions.k8s.io/' \
                'clusterpolicies.nvidia.com'
        exit 0
        ;;
    *'get daemonsets --all-namespaces'*)
        [ "${KIP_TEST_CONFLICT_DAEMONSET:-0}" = 1 ] \
            && printf '%s\\n' 'kube-system/nvidia-device-plugin'
        exit 0
        ;;
    *'get clusterpolicy cluster-policy'*)
        printf '%s' "${KIP_TEST_CLUSTER_POLICY_STATE:-ready}"
        ;;
    *'-n gpu-operator get pods'*)
        mode=operator
        [ -r "$KIP_TEST_RELEASE_MODE" ] && mode=$(cat "$KIP_TEST_RELEASE_MODE")
        phase=${KIP_TEST_OPERAND_PHASE:-Running}
        readiness='true:'
        [ "$phase" = Succeeded ] && readiness=''
        printf '%s\\n' \
            "gpu-operator-abc|$phase|$readiness" \
            "gpu-operator-node-feature-discovery-master-abc|$phase|$readiness" \
            "gpu-operator-node-feature-discovery-worker-abc|$phase|$readiness" \
            "nvidia-container-toolkit-daemonset-abc|$phase|$readiness" \
            "nvidia-device-plugin-daemonset-abc|$phase|$readiness" \
            "gpu-feature-discovery-abc|$phase|$readiness" \
            "nvidia-operator-validator-abc|$phase|$readiness" \
            'nvidia-cuda-validator-abc|Succeeded|'
        [ ! -r "$KIP_TEST_RELEASE_DCGM" ] \
            || [ "$(cat "$KIP_TEST_RELEASE_DCGM")" != true ] \
            || printf '%s\n' "nvidia-dcgm-exporter-abc|$phase|$readiness"
        [ "$mode" != operator ] || printf '%s\\n' "nvidia-driver-daemonset-abc|$phase|$readiness"
        ;;
    *'delete pod kip-gpu-smoke'*)
        if [ -e "$KIP_TEST_SMOKE_STATE" ] \
            && [ "${KIP_TEST_CLEANUP_FAIL:-0}" = 1 ]; then
            exit 1
        fi
        rm -f "$KIP_TEST_SMOKE_STATE"
        ;;
    *'apply -f '*smoke-pod.yaml*)
        [ "${KIP_TEST_APPLY_FAIL:-0}" != 1 ] || exit 1
        touch "$KIP_TEST_SMOKE_STATE"
        ;;
    *'wait --for=jsonpath={.status.phase}=Succeeded pod/kip-gpu-smoke'*)
        [ "${KIP_TEST_SMOKE_WAIT_FAIL:-0}" != 1 ]
        ;;
    *'get pod kip-gpu-smoke -o jsonpath={.spec.nodeName}'*)
        printf '%s' "${KIP_TEST_SMOKE_NODE:-gpu-node-1}"
        ;;
    *'logs kip-gpu-smoke --all-containers'*)
        printf '%s\\n' 'diagnostic logs'
        ;;
    *'logs kip-gpu-smoke'*)
        [ "${KIP_TEST_SMOKE_LOG_FAIL:-0}" != 1 ] || exit 1
        default_log='0, NVIDIA H100 80GB HBM3, GPU-test-uuid, 595.91.07, 81559 MiB'
        printf '%s\\n' "${KIP_TEST_SMOKE_LOG:-$default_log}"
        ;;
    *'describe pod kip-gpu-smoke'*) printf '%s\\n' 'describe output' ;;
    *'get events --field-selector involvedObject.name=kip-gpu-smoke'*)
        printf '%s\\n' 'event output'
        ;;
esac""",
    )

    return {
        "command_dir": command_dir,
        "command_log": command_log,
        "release_state": release_state,
        "release_mode": release_mode,
        "release_dcgm": release_dcgm,
        "smoke_state": smoke_state,
        "kubeconfig": kubeconfig,
        "cluster_check": cluster_check,
    }


def gpu_environment(fixture: dict[str, Path], **overrides: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "KIP_TESTING": "1",
            "KIP_TEST_COMMAND_DIR": str(fixture["command_dir"]),
            "KIP_TEST_COMMAND_LOG": str(fixture["command_log"]),
            "KIP_TEST_RELEASE_STATE": str(fixture["release_state"]),
            "KIP_TEST_RELEASE_MODE": str(fixture["release_mode"]),
            "KIP_TEST_RELEASE_DCGM": str(fixture["release_dcgm"]),
            "KIP_TEST_SMOKE_STATE": str(fixture["smoke_state"]),
            "KIP_TEST_EXPECTED_CHART_DIGEST": CHART_DIGEST,
            "KIP_TEST_COSIGN_SHA": COSIGN_SHA,
            "KIP_CLUSTER_CHECK_SCRIPT": str(fixture["cluster_check"]),
            "KIP_KUBECONFIG": str(fixture["kubeconfig"]),
            "NO_COLOR": "1",
        }
    )
    environment.update(overrides)
    return environment


def run_gpu_script(
    script: Path,
    fixture: dict[str, Path],
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script)],
        cwd=ROOT,
        env=gpu_environment(fixture, **overrides),
        check=False,
        capture_output=True,
        text=True,
    )


def mark_installed(fixture: dict[str, Path], mode: str = "operator", *, dcgm: bool = False) -> None:
    fixture["release_state"].touch()
    fixture["release_mode"].write_text(mode)
    fixture["release_dcgm"].write_text(str(dcgm).lower())


def test_gpu_artifacts_pin_whole_gpu_cluster_policy_stack() -> None:
    versions = (ROOT / "deploy" / "gpu" / "versions.env").read_text()
    values = (ROOT / "deploy" / "gpu" / "values.yaml").read_text()
    manifest = (ROOT / "deploy" / "gpu" / "manifests" / "smoke-pod.yaml").read_text()

    assert 'KIP_GPU_OPERATOR_VERSION="v26.7.0"' in versions
    assert CHART_DIGEST in versions
    assert COSIGN_SHA in versions
    assert 'KIP_NVIDIA_DRIVER_VERSION="595.91.07"' in versions
    assert 'KIP_DCGM_EXPORTER_REPOSITORY="nvcr.io/nvidia/k8s"' in versions
    assert 'KIP_DCGM_EXPORTER_IMAGE="dcgm-exporter"' in versions
    assert (
        'KIP_DCGM_EXPORTER_LINUX_AMD64_DIGEST="sha256:b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a"'
        in versions
    )
    assert "clusterPolicy:\n  deployCR: true" in values
    assert "gpuCluster:\n  deployCR: false" in values
    assert "nriPluginEnabled: false" in values
    assert "dcgmExporter:\n" in values
    assert "  enabled: false" in values
    assert "repository: nvcr.io/nvidia/k8s" in values
    assert "image: dcgm-exporter" in values
    assert (
        'version: "4.6.0-4.8.3-distroless@sha256:'
        'b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a"' in values
    )
    assert "serviceMonitor:\n    enabled: false" in values
    assert "migManager:\n  enabled: false" in values
    assert "runtimeClassName" not in manifest
    assert 'nvidia.com/gpu: "1"' in manifest
    assert "nvidia/cuda@sha256:e711c993" in manifest


def test_operator_managed_install_verifies_and_passes_hard_checkpoint(
    gpu_fixture: dict[str, Path],
) -> None:
    result = run_gpu_script(INSTALL_SCRIPT, gpu_fixture)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "8 passed" in result.stdout
    assert "Hard checkpoint passed" in result.stdout
    commands = gpu_fixture["command_log"].read_text()
    assert f"gpu-operator@{CHART_DIGEST}" in commands
    assert "driver.enabled=true" in commands
    assert "dcgmExporter.repository=nvcr.io/nvidia/k8s" in commands
    assert "dcgmExporter.image=dcgm-exporter" in commands
    assert (
        "dcgmExporter.version=4.6.0-4.8.3-distroless@sha256:"
        "b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a" in commands
    )
    assert "dcgmExporter.enabled=false" in commands
    assert "kip.dcgmExporterEnabled=false" in commands
    assert "kip.driverMode=operator" in commands
    assert "--timeout 25m" in commands
    assert "--atomic" not in commands
    assert "apply -f" in commands
    assert commands.count("delete pod kip-gpu-smoke") >= 2
    assert not gpu_fixture["smoke_state"].exists()


def test_preinstalled_driver_mode_requires_host_driver_and_disables_operator_driver(
    gpu_fixture: dict[str, Path],
) -> None:
    result = run_gpu_script(
        INSTALL_SCRIPT,
        gpu_fixture,
        KIP_GPU_DRIVER_MODE="preinstalled",
        KIP_TEST_HOST_DRIVER="healthy",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    commands = gpu_fixture["command_log"].read_text()
    assert "nvidia-smi topo -m" in commands
    assert "driver.enabled=false" in commands
    assert "kip.driverMode=preinstalled" in commands


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"KIP_VERBOSE": "sometimes"}, "VERBOSE must be 0 or 1"),
        ({"KIP_GPU_DRIVER_MODE": "magic"}, "GPU_DRIVER_MODE must be operator or preinstalled"),
    ],
)
def test_invalid_gpu_configuration_exits_two(
    gpu_fixture: dict[str, Path],
    overrides: dict[str, str],
    message: str,
) -> None:
    result = run_gpu_script(INSTALL_SCRIPT, gpu_fixture, **overrides)

    assert result.returncode == 2
    assert message in result.stderr


@pytest.mark.parametrize(
    "overrides",
    [
        {"KIP_TEST_NO_GPU": "1"},
        {"KIP_TEST_CLUSTER_FAIL": "1"},
        {"KIP_TEST_ORPHAN_CRD": "1"},
        {"KIP_TEST_CONFLICT_DAEMONSET": "1"},
        {"KIP_TEST_UNMANAGED_RELEASE": "1"},
    ],
)
def test_install_preflight_failures_block_helm_mutation(
    gpu_fixture: dict[str, Path], overrides: dict[str, str]
) -> None:
    result = run_gpu_script(INSTALL_SCRIPT, gpu_fixture, **overrides)

    assert result.returncode == 1
    commands = gpu_fixture["command_log"].read_text()
    assert "helm upgrade" not in commands


def test_operator_mode_rejects_working_host_driver_on_fresh_install(
    gpu_fixture: dict[str, Path],
) -> None:
    result = run_gpu_script(INSTALL_SCRIPT, gpu_fixture, KIP_TEST_HOST_DRIVER="healthy")

    assert result.returncode == 1
    assert "Safe installation state" in result.stdout
    assert "helm upgrade" not in gpu_fixture["command_log"].read_text()


def test_operator_mode_rejects_undownloadable_running_kernel_headers(
    gpu_fixture: dict[str, Path],
) -> None:
    result = run_gpu_script(
        INSTALL_SCRIPT,
        gpu_fixture,
        KIP_TEST_KERNEL_HEADERS_UNAVAILABLE="1",
    )

    assert result.returncode == 1
    assert "running kernel headers are not available" in result.stdout
    assert "helm upgrade" not in gpu_fixture["command_log"].read_text()


def test_preinstalled_mode_rejects_unhealthy_host_driver(gpu_fixture: dict[str, Path]) -> None:
    result = run_gpu_script(
        INSTALL_SCRIPT,
        gpu_fixture,
        KIP_GPU_DRIVER_MODE="preinstalled",
    )

    assert result.returncode == 1
    assert "Safe installation state" in result.stdout


@pytest.mark.parametrize(
    "overrides",
    [
        {"KIP_TEST_CHART_DIGEST": "sha256:wrong"},
        {"KIP_TEST_COSIGN_CHECKSUM_FAIL": "1"},
        {"KIP_TEST_COSIGN_FAIL": "1"},
    ],
)
def test_supply_chain_verification_failure_blocks_install(
    gpu_fixture: dict[str, Path], overrides: dict[str, str]
) -> None:
    result = run_gpu_script(INSTALL_SCRIPT, gpu_fixture, **overrides)

    assert result.returncode == 1
    assert "Verified chart artifact" in result.stdout
    assert "helm upgrade" not in gpu_fixture["command_log"].read_text()


@pytest.mark.parametrize(
    "overrides",
    [
        {"KIP_TEST_RELEASE_VERSION": "v26.3.3"},
        {"KIP_TEST_RECORDED_DIGEST": "sha256:drift"},
        {"KIP_TEST_CDI_ENABLED": "false"},
        {"KIP_TEST_EXTRA_VALUE": "1"},
        {"KIP_TEST_RECORDED_DCGM": "true"},
    ],
)
def test_existing_release_drift_is_refused(
    gpu_fixture: dict[str, Path], overrides: dict[str, str]
) -> None:
    mark_installed(gpu_fixture)

    result = run_gpu_script(INSTALL_SCRIPT, gpu_fixture, **overrides)

    assert result.returncode == 1
    assert "helm upgrade" not in gpu_fixture["command_log"].read_text()


def test_exact_failed_release_attempts_safe_reinstall(gpu_fixture: dict[str, Path]) -> None:
    mark_installed(gpu_fixture)

    result = run_gpu_script(INSTALL_SCRIPT, gpu_fixture, KIP_TEST_RELEASE_STATUS="failed")

    # The fixture deliberately keeps reporting Helm status=failed after the
    # reinstall; production Helm advances it to deployed. The important
    # recovery contract here is that only the exact owned release is removed
    # and immediately reinstalled, before health remains fail-closed.
    assert result.returncode == 1
    commands = gpu_fixture["command_log"].read_text()
    assert "helm uninstall gpu-operator" in commands
    assert "helm upgrade --install gpu-operator" in commands


def test_driver_mode_change_is_refused(gpu_fixture: dict[str, Path]) -> None:
    mark_installed(gpu_fixture, "preinstalled")

    result = run_gpu_script(INSTALL_SCRIPT, gpu_fixture)

    assert result.returncode == 1
    assert "helm upgrade" not in gpu_fixture["command_log"].read_text()


@pytest.mark.parametrize("dcgm", [False, True])
def test_healthy_rerun_reconciles_matching_release(
    gpu_fixture: dict[str, Path], dcgm: bool
) -> None:
    mark_installed(gpu_fixture, dcgm=dcgm)

    result = run_gpu_script(INSTALL_SCRIPT, gpu_fixture)

    assert result.returncode == 0, result.stderr
    assert "matching installation" in result.stdout
    commands = gpu_fixture["command_log"].read_text()
    assert "helm upgrade --install" in commands
    assert f"dcgmExporter.enabled={str(dcgm).lower()}" in commands


def test_legacy_pre_observability_release_without_dcgm_marker_is_reconciled(
    gpu_fixture: dict[str, Path],
) -> None:
    mark_installed(gpu_fixture, dcgm=False)

    result = run_gpu_script(INSTALL_SCRIPT, gpu_fixture, KIP_TEST_OMIT_DCGM_MARKER="1")

    assert result.returncode == 0, result.stderr
    commands = gpu_fixture["command_log"].read_text()
    assert "dcgmExporter.enabled=false" in commands
    assert "kip.dcgmExporterEnabled=false" in commands


def test_missing_required_inspection_command_is_a_useful_failure(
    gpu_fixture: dict[str, Path],
) -> None:
    (gpu_fixture["command_dir"] / "lspci").unlink()

    result = run_gpu_script(INSTALL_SCRIPT, gpu_fixture)

    assert result.returncode == 1
    assert "Required commands or NVIDIA PCI hardware are missing" in result.stdout
    assert "helm upgrade" not in gpu_fixture["command_log"].read_text()


def test_helm_failure_keeps_diagnostics_available_without_atomic_rollback(
    gpu_fixture: dict[str, Path],
) -> None:
    result = run_gpu_script(INSTALL_SCRIPT, gpu_fixture, KIP_TEST_HELM_FAIL="1")

    assert result.returncode == 1
    assert "workloads remain available for diagnosis" in result.stdout
    commands = gpu_fixture["command_log"].read_text()
    assert "helm upgrade --install" in commands
    assert "--atomic" not in commands


def test_gpu_install_wait_timeout_blocks_hard_checkpoint(
    gpu_fixture: dict[str, Path],
) -> None:
    result = run_gpu_script(
        INSTALL_SCRIPT,
        gpu_fixture,
        KIP_TEST_CLUSTER_POLICY_STATE="notReady",
        KIP_GPU_WAIT_TIMEOUT_SECONDS="0",
    )

    assert result.returncode == 1
    assert "Kubernetes GPU resources" in result.stdout
    assert "CUDA smoke Pod" not in result.stdout


def test_gpu_check_is_read_only_and_accepts_completed_validator(
    gpu_fixture: dict[str, Path],
) -> None:
    mark_installed(gpu_fixture)

    result = run_gpu_script(VALIDATE_SCRIPT, gpu_fixture)

    assert result.returncode == 0, result.stderr
    assert "8 passed" in result.stdout
    commands = gpu_fixture["command_log"].read_text()
    for mutation in ("helm upgrade", "kubectl apply", "kubectl delete"):
        assert mutation not in commands


@pytest.mark.parametrize(
    "overrides",
    [
        {"KIP_TEST_CLUSTER_POLICY_STATE": "notReady"},
        {"KIP_TEST_OPERAND_PHASE": "Pending"},
        {"KIP_TEST_GPU_CAPACITY": "0"},
        {"KIP_TEST_GPU_ALLOCATABLE": "0"},
        {"KIP_TEST_GPU_CAPACITY": "2"},
        {"KIP_TEST_MIG_RESOURCE": "1"},
        {"KIP_TEST_MIG_LABEL": "all-balanced"},
        {"KIP_TEST_TIME_SLICING": "1"},
    ],
)
def test_gpu_check_rejects_unhealthy_or_shared_state(
    gpu_fixture: dict[str, Path], overrides: dict[str, str]
) -> None:
    mark_installed(gpu_fixture)

    result = run_gpu_script(VALIDATE_SCRIPT, gpu_fixture, **overrides)

    assert result.returncode == 1
    assert "FAIL" in result.stdout


def test_gpu_smoke_succeeds_and_always_removes_pod(gpu_fixture: dict[str, Path]) -> None:
    mark_installed(gpu_fixture)

    result = run_gpu_script(SMOKE_SCRIPT, gpu_fixture)

    assert result.returncode == 0, result.stderr
    assert "3 passed" in result.stdout
    assert "NVIDIA H100" in result.stdout
    assert not gpu_fixture["smoke_state"].exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"KIP_TEST_APPLY_FAIL": "1"},
        {"KIP_TEST_SMOKE_WAIT_FAIL": "1"},
        {"KIP_TEST_SMOKE_LOG": "not valid nvidia-smi output"},
        {"KIP_TEST_SMOKE_NODE": "other-node"},
        {"KIP_TEST_CLEANUP_FAIL": "1"},
    ],
)
def test_gpu_smoke_failure_reports_diagnostics_and_attempts_cleanup(
    gpu_fixture: dict[str, Path], overrides: dict[str, str]
) -> None:
    mark_installed(gpu_fixture)

    result = run_gpu_script(SMOKE_SCRIPT, gpu_fixture, **overrides)

    assert result.returncode == 1
    commands = gpu_fixture["command_log"].read_text()
    assert commands.count("delete pod kip-gpu-smoke") >= 2
    if overrides.get("KIP_TEST_CLEANUP_FAIL") != "1":
        assert "GPU smoke diagnostics" in result.stderr
        assert not gpu_fixture["smoke_state"].exists()


def test_gpu_check_verbose_honors_no_color(gpu_fixture: dict[str, Path]) -> None:
    mark_installed(gpu_fixture)

    result = run_gpu_script(
        VALIDATE_SCRIPT,
        gpu_fixture,
        KIP_VERBOSE="1",
        KIP_FORCE_COLOR="1",
    )

    assert result.returncode == 0
    assert "--- kubectl get pods -n gpu-operator ---" in result.stdout
    assert "\x1b[" not in result.stdout


def test_invalid_make_gpu_driver_mode_exits_two(gpu_fixture: dict[str, Path]) -> None:
    result = subprocess.run(
        ["make", "gpu-install", "GPU_DRIVER_MODE=magic"],
        cwd=ROOT,
        env=gpu_environment(gpu_fixture),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "GPU_DRIVER_MODE must be operator or preinstalled" in result.stderr
