"""Deterministic subprocess tests for single-node cluster tooling."""

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
BOOTSTRAP_SCRIPT = ROOT / "deploy" / "cluster" / "bootstrap.sh"
VALIDATE_SCRIPT = ROOT / "deploy" / "cluster" / "validate.sh"


def write_command(command_dir: Path, name: str, body: str) -> None:
    command = command_dir / name
    command.write_text(f"#!/bin/sh\n{body}\n")
    command.chmod(0o755)


@pytest.fixture
def cluster_fixture(tmp_path: Path) -> dict[str, Path]:
    command_dir = tmp_path / "commands"
    command_dir.mkdir()
    etc_root = tmp_path / "etc"
    run_root = tmp_path / "run"
    var_root = tmp_path / "var"
    bin_root = tmp_path / "bin"
    target_home = tmp_path / "home" / "tester"
    for directory in (etc_root, run_root, var_root, bin_root, target_home):
        directory.mkdir(parents=True)
    os_release = etc_root / "os-release"
    os_release.write_text(
        'ID="ubuntu"\nVERSION_ID="24.04"\nVERSION_CODENAME="noble"\n'
        'PRETTY_NAME="Ubuntu 24.04 LTS"\n'
    )
    (etc_root / "fstab").write_text("UUID=root / ext4 defaults 0 1\n")
    command_log = tmp_path / "commands.log"
    capture_config = tmp_path / "captured-kubeadm.yaml"
    swap_disabled = tmp_path / "swap-disabled"
    packages_state = tmp_path / "packages-installed"

    write_command(
        command_dir,
        "uname",
        """case "${1:-}" in
    -m) printf '%s\\n' "${KIP_TEST_ARCH:-x86_64}" ;;
    -r) printf '%s\\n' "${KIP_TEST_KERNEL:-6.8.0-test}" ;;
esac""",
    )
    write_command(command_dir, "hostname", "printf '%s\\n' \"${KIP_TEST_HOSTNAME:-gpu-node-1}\"")
    write_command(command_dir, "ps", "printf '%s\\n' systemd")
    write_command(
        command_dir,
        "lscpu",
        """printf '%s\\n' 'Architecture: x86_64' 'CPU(s): 32' 'Model name: Test CPU' \
    'Flags: CLUSTER_CPU_SENTINEL'""",
    )
    write_command(
        command_dir,
        "free",
        "printf '%s\\n' '              total used free' 'Mem:         256Gi 10Gi 246Gi'",
    )
    write_command(
        command_dir,
        "lsblk",
        """if [ "${1:-}" = "-dn" ]; then
    printf '%s\\n' disk
else
    printf '%s\\n' 'NAME SIZE TYPE' 'nvme0n1 1.8T disk'
fi""",
    )
    write_command(
        command_dir,
        "ip",
        """args="$*"
case "$args" in
    'link') printf '%s\\n' '1: lo: <LOOPBACK,UP>' '2: eth0: <BROADCAST,UP>' ;;
    '-o link show up') printf '%s\\n' '1: lo: <LOOPBACK,UP>' '2: eth0: <BROADCAST,UP>' ;;
    'route show default') printf '%s\\n' 'default via 192.0.2.1 dev eth0' ;;
    '-4 route show default')
        printf '%s\\n' 'default via 192.0.2.1 dev eth0 src 192.0.2.10'
        if [ "${KIP_TEST_AMBIGUOUS_ROUTE:-0}" = 1 ]; then
            printf '%s\\n' 'default via 198.51.100.1 dev eth1 src 198.51.100.10'
        fi
        ;;
    '-4 -o addr show scope global')
        printf '%s\\n' '2: eth0 inet 192.0.2.10/24 scope global eth0'
        ;;
    '-4 -o addr show dev eth0 scope global')
        printf '%s\\n' '2: eth0 inet 192.0.2.10/24 scope global eth0'
        ;;
    '-4 route show')
        printf '%s\\n' 'default via 192.0.2.1 dev eth0' '192.0.2.0/24 dev eth0'
        if [ "${KIP_TEST_ROUTE_CONFLICT:-0}" = 1 ]; then
            printf '%s\\n' '10.244.0.0/16 via 192.0.2.2 dev eth0'
        fi
        ;;
esac""",
    )
    write_command(
        command_dir,
        "getent",
        """if [ "${1:-}" = hosts ]; then
    printf '%s\\n' '192.0.2.2 resolved.example'
elif [ "${1:-}" = passwd ]; then
    printf '%s\\n' 'tester:x:1000:1000::/home/tester:/bin/bash'
fi""",
    )
    write_command(command_dir, "timedatectl", "printf '%s\\n' \"${KIP_TEST_NTP:-yes}\"")
    write_command(
        command_dir,
        "swapon",
        """if [ -n "${KIP_TEST_SWAP_SOURCE:-}" ] && [ ! -e "$KIP_TEST_SWAP_DISABLED" ]; then
    printf '%s\\n' "$KIP_TEST_SWAP_SOURCE"
fi""",
    )
    write_command(command_dir, "swapoff", 'touch "$KIP_TEST_SWAP_DISABLED"')
    write_command(command_dir, "lspci", "exit 0")
    write_command(command_dir, "containerd", "printf '%s\\n' 'containerd 2.3.4 test-build'")
    write_command(command_dir, "kubelet", "printf '%s\\n' 'Kubernetes v1.36.4'")
    write_command(
        command_dir,
        "dpkg-query",
        """if [ "${KIP_TEST_CONFLICT_PACKAGE:-}" != "" ]; then
    for argument in "$@"; do package_name=$argument; done
    if [ "$package_name" = "$KIP_TEST_CONFLICT_PACKAGE" ]; then
        case "$*" in
            *'${Status}|${Version}'*) printf '%s\\n' 'install ok installed|1.0.0-conflict' ;;
            *) printf '%s\\n' '1.0.0-conflict' ;;
        esac
        exit 0
    fi
fi
[ "${KIP_TEST_PACKAGES_INSTALLED:-0}" = 1 ] || [ -e "$KIP_TEST_PACKAGES_STATE" ] || exit 1
for argument in "$@"; do package_name=$argument; done
case "$package_name" in
    containerd.io) package_version=$KIP_TEST_CONTAINERD_PACKAGE ;;
    kubelet|kubeadm|kubectl) package_version='1.36.4-1.1' ;;
    cri-tools) package_version='1.36.0-1.1' ;;
    kubernetes-cni) package_version='1.7.1-1.1' ;;
    *) exit 1 ;;
esac
case "$*" in
    *'${Status}|${Version}'*) printf '%s\\n' "install ok installed|$package_version" ;;
    *) printf '%s\\n' "$package_version" ;;
esac""",
    )
    write_command(
        command_dir,
        "kubeadm",
        """printf 'kubeadm %s\\n' "$*" >> "$KIP_TEST_COMMAND_LOG"
case "$*" in
    'version -o short') printf '%s\\n' 'v1.36.4' ;;
    *'config images list'*) printf '%s\\n' 'registry.k8s.io/pause:3.10.1' ;;
    *'init --config'*)
        previous=''
        for argument in "$@"; do
            if [ "$previous" = '--config' ]; then cp "$argument" "$KIP_TEST_CAPTURE_CONFIG"; fi
            previous=$argument
        done
        mkdir -p "$KIP_ETC_ROOT/kubernetes"
        printf '%s\\n' test-kubeconfig > "$KIP_ETC_ROOT/kubernetes/admin.conf"
        ;;
esac""",
    )
    write_command(
        command_dir,
        "kubectl",
        """args="$*"
printf 'kubectl %s\\n' "$args" >> "$KIP_TEST_COMMAND_LOG"
case "$args" in
    *'version --client -o json'*)
        printf '%s\\n' '{"clientVersion":{"gitVersion":"v1.36.4"}}'
        ;;
    *'version -o json'*)
        printf '%s\\n' \
            "{\\"clientVersion\\":{\\"gitVersion\\":\\"v1.36.4\\"},\\"serverVersion\\":{\\"gitVersion\\":\\"v${KIP_TEST_SERVER_VERSION:-1.36.4}\\"}}"
        ;;
    *'get --raw=/readyz'*) printf '%s\\n' ok ;;
    *'get configmap kubeadm-config'*)
        printf '%s\\n' 'networking:' '  podSubnet: 10.244.0.0/16' \
            '  serviceSubnet: 10.96.0.0/12'
        ;;
    *'get node gpu-node-1'*) printf '%s' '192.0.2.10' ;;
    *'get nodes -o jsonpath='*'spec.taints'*)
        printf '%s\\n' 'node-role.kubernetes.io/control-plane:NoSchedule'
        ;;
    *'get nodes -o jsonpath='*) printf '%s\\n' 'gpu-node-1||True' ;;
    *'get pods -l'*)
        if [ -n "${KIP_TEST_FAIL_LABEL:-}" ] && echo "$args" | grep -q "$KIP_TEST_FAIL_LABEL"; then
            printf '%s\\n' 'Pending|false,'
        else
            printf '%s\\n' 'Running|true,'
        fi
        ;;
    *'get pods -o jsonpath='*)
        printf '%s\\n' \
            'kube-apiserver-gpu-node-1|Running|true,' \
            'etcd-gpu-node-1|Running|true,' \
            'coredns-test|Running|true,' \
            'cilium-test|Running|true,'
        ;;
    *'logs kip-network-smoke'*) printf '%s\\n' smoke-failed ;;
esac""",
    )
    write_command(
        command_dir,
        "systemctl",
        'printf \'systemctl %s\\n\' "$*" >> "$KIP_TEST_COMMAND_LOG"',
    )
    write_command(
        command_dir,
        "crictl",
        """case "$*" in
    *' version')
        printf '%s\\n' 'Version: 0.1.0' 'RuntimeApiVersion: v1'
        ;;
    *)
        printf '%s\\n' \
            "{\\"status\\":{\\"conditions\\":[{\\"type\\":\\"RuntimeReady\\",\\"status\\":${KIP_TEST_RUNTIME_READY:-true}}]}}"
        ;;
esac""",
    )
    write_command(
        command_dir,
        "helm",
        """if [ "$*" = 'version --short' ]; then
    printf '%s\\n' 'v3.21.4+gtest'
else
    printf 'helm %s\\n' "$*" >> "$KIP_TEST_COMMAND_LOG"
fi""",
    )
    write_command(
        command_dir,
        "cilium",
        """if [ "$*" = 'version --client' ]; then
    printf '%s\\n' 'cilium-cli: v0.19.7'
elif echo "$*" | grep -q 'connectivity test' \
    && [ "${KIP_TEST_CILIUM_CONNECTIVITY_FAIL:-0}" = 1 ]; then
    exit 1
else
    printf 'cilium %s\\n' "$*" >> "$KIP_TEST_COMMAND_LOG"
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
[ -z "$output" ] || printf '%s\\n' fixture > "$output"
""",
    )
    write_command(
        command_dir,
        "gpg",
        """output=''
input=''
while [ "$#" -gt 0 ]; do
    if [ "$1" = '--output' ]; then shift; output=$1; else input=$1; fi
    shift
done
cp "$input" "$output"
""",
    )
    for command_name in ("apt-get", "apt-mark", "modprobe", "sysctl"):
        write_command(
            command_dir,
            command_name,
            f'printf \'{command_name} %s\\n\' "$*" >> "$KIP_TEST_COMMAND_LOG"',
        )
    write_command(
        command_dir,
        "env",
        """printf 'env %s\\n' "$*" >> "$KIP_TEST_COMMAND_LOG"
case "$*" in *'apt-get install'*) touch "$KIP_TEST_PACKAGES_STATE" ;; esac""",
    )

    return {
        "command_dir": command_dir,
        "etc_root": etc_root,
        "run_root": run_root,
        "var_root": var_root,
        "bin_root": bin_root,
        "target_home": target_home,
        "os_release": os_release,
        "command_log": command_log,
        "capture_config": capture_config,
        "swap_disabled": swap_disabled,
        "packages_state": packages_state,
    }


def cluster_environment(
    fixture: dict[str, Path],
    **overrides: str,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "KIP_TESTING": "1",
            "KIP_TEST_EUID": "0",
            "KIP_TEST_COMMAND_DIR": str(fixture["command_dir"]),
            "KIP_TEST_COMMAND_LOG": str(fixture["command_log"]),
            "KIP_TEST_CAPTURE_CONFIG": str(fixture["capture_config"]),
            "KIP_TEST_SWAP_DISABLED": str(fixture["swap_disabled"]),
            "KIP_TEST_PACKAGES_STATE": str(fixture["packages_state"]),
            "KIP_TEST_CONTAINERD_PACKAGE": ("2.3.4-2~ubuntu.24.04~noble"),
            "KIP_ETC_ROOT": str(fixture["etc_root"]),
            "KIP_RUN_ROOT": str(fixture["run_root"]),
            "KIP_VAR_ROOT": str(fixture["var_root"]),
            "KIP_USR_LOCAL_BIN": str(fixture["bin_root"]),
            "KIP_OS_RELEASE_FILE": str(fixture["os_release"]),
            "KIP_TARGET_USER": "tester",
            "KIP_TARGET_HOME": str(fixture["target_home"]),
            "NO_COLOR": "1",
        }
    )
    environment.update(overrides)
    return environment


def run_cluster_script(
    script: Path,
    fixture: dict[str, Path],
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script)],
        cwd=ROOT,
        env=cluster_environment(fixture, **overrides),
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("version", "codename", "package_version"),
    [
        ("22.04", "jammy", "2.3.4-2~ubuntu.22.04~jammy"),
        ("24.04", "noble", "2.3.4-2~ubuntu.24.04~noble"),
    ],
)
def test_bootstrap_maps_containerd_package_to_ubuntu_release(
    cluster_fixture: dict[str, Path],
    version: str,
    codename: str,
    package_version: str,
) -> None:
    cluster_fixture["os_release"].write_text(
        f'ID="ubuntu"\nVERSION_ID="{version}"\nVERSION_CODENAME="{codename}"\n'
        f'PRETTY_NAME="Ubuntu {version} LTS"\n'
    )

    result = run_cluster_script(
        BOOTSTRAP_SCRIPT,
        cluster_fixture,
        KIP_TEST_CONTAINERD_PACKAGE=package_version,
    )

    assert result.returncode == 0, result.stderr
    commands = cluster_fixture["command_log"].read_text()
    assert f"containerd.io={package_version}" in commands
    assert "docker-ce" not in commands
    assert "apt-get upgrade" not in commands


def test_bootstrap_renders_runtime_and_kubeadm_configuration(
    cluster_fixture: dict[str, Path],
) -> None:
    result = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)

    assert result.returncode == 0, result.stderr
    containerd_config = (cluster_fixture["etc_root"] / "containerd" / "config.toml").read_text()
    kubeadm_config = cluster_fixture["capture_config"].read_text()
    assert "SystemdCgroup = true" in containerd_config
    assert 'sandbox = "registry.k8s.io/pause:3.10.1"' in containerd_config
    assert 'advertiseAddress: "192.0.2.10"' in kubeadm_config
    assert 'podSubnet: "10.244.0.0/16"' in kubeadm_config
    assert 'serviceSubnet: "10.96.0.0/12"' in kubeadm_config
    kube_dir = cluster_fixture["target_home"] / ".kube"
    assert stat.S_IMODE(kube_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((kube_dir / "config").stat().st_mode) == 0o600
    commands = cluster_fixture["command_log"].read_text()
    assert "oci://quay.io/cilium/charts/cilium@sha256:" in commands
    assert "taint nodes --all node-role.kubernetes.io/control-plane-" in commands
    assert "cilium connectivity test" in commands
    assert "12 passed" in result.stdout
    assert "install and validate the NVIDIA GPU Operator" in result.stdout


def test_bootstrap_accepts_assigned_node_ip_override(cluster_fixture: dict[str, Path]) -> None:
    result = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture, KIP_NODE_IP="192.0.2.10")

    assert result.returncode == 0
    assert "gpu-node-1 at 192.0.2.10" in result.stdout


def test_bootstrap_requires_override_for_ambiguous_default_routes(
    cluster_fixture: dict[str, Path],
) -> None:
    result = run_cluster_script(
        BOOTSTRAP_SCRIPT,
        cluster_fixture,
        KIP_TEST_AMBIGUOUS_ROUTE="1",
    )

    assert result.returncode == 1
    assert "set NODE_IP when route selection is ambiguous" in result.stdout


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"KIP_POD_CIDR": "10.244.0.1/16"}, "canonical IPv4 network"),
        (
            {"KIP_POD_CIDR": "10.96.0.0/16", "KIP_SERVICE_CIDR": "10.96.0.0/12"},
            "must not overlap",
        ),
        ({"KIP_NODE_IP": "198.51.100.20"}, "is not assigned to this host"),
    ],
)
def test_invalid_bootstrap_configuration_exits_two(
    cluster_fixture: dict[str, Path],
    overrides: dict[str, str],
    message: str,
) -> None:
    result = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture, **overrides)

    assert result.returncode == 2
    assert message in result.stderr


def test_route_conflict_fails_before_package_install(cluster_fixture: dict[str, Path]) -> None:
    result = run_cluster_script(
        BOOTSTRAP_SCRIPT,
        cluster_fixture,
        KIP_TEST_ROUTE_CONFLICT="1",
    )

    assert result.returncode == 1
    commands = (
        cluster_fixture["command_log"].read_text()
        if cluster_fixture["command_log"].exists()
        else ""
    )
    assert "apt-get" not in commands


def test_bootstrap_disables_fstab_swap_idempotently(cluster_fixture: dict[str, Path]) -> None:
    fstab = cluster_fixture["etc_root"] / "fstab"
    fstab.write_text("UUID=root / ext4 defaults 0 1\n/swapfile none swap sw 0 0\n")

    result = run_cluster_script(
        BOOTSTRAP_SCRIPT,
        cluster_fixture,
        KIP_TEST_SWAP_SOURCE="/swapfile",
    )

    assert result.returncode == 0
    assert "# KIP_DISABLED_SWAP /swapfile none swap sw 0 0" in fstab.read_text()
    backup = cluster_fixture["etc_root"] / "fstab.kip-pre-bootstrap"
    assert "/swapfile none swap" in backup.read_text()


def test_unmanaged_swap_fails_before_mutation(cluster_fixture: dict[str, Path]) -> None:
    result = run_cluster_script(
        BOOTSTRAP_SCRIPT,
        cluster_fixture,
        KIP_TEST_SWAP_SOURCE="/dev/zram0",
    )

    assert result.returncode == 1
    assert not cluster_fixture["swap_disabled"].exists()


def test_healthy_existing_cluster_skips_kubeadm_init(cluster_fixture: dict[str, Path]) -> None:
    kubernetes_dir = cluster_fixture["etc_root"] / "kubernetes"
    kubernetes_dir.mkdir()
    (kubernetes_dir / "admin.conf").write_text("existing\n")
    containerd_dir = cluster_fixture["etc_root"] / "containerd"
    containerd_dir.mkdir()
    (containerd_dir / "config.toml").write_text("# Managed by Kube Inference Platform v0.1.\n")

    result = run_cluster_script(
        BOOTSTRAP_SCRIPT,
        cluster_fixture,
        KIP_TEST_PACKAGES_INSTALLED="1",
    )

    assert result.returncode == 0, result.stderr
    commands = cluster_fixture["command_log"].read_text()
    assert "kubeadm init --config" not in commands
    assert "apt-get" not in commands


def test_partial_cluster_state_fails_without_reset(cluster_fixture: dict[str, Path]) -> None:
    manifests = cluster_fixture["etc_root"] / "kubernetes" / "manifests"
    manifests.mkdir(parents=True)

    result = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)

    assert result.returncode == 1
    commands = (
        cluster_fixture["command_log"].read_text()
        if cluster_fixture["command_log"].exists()
        else ""
    )
    assert "kubeadm reset" not in commands
    assert "apt-get" not in commands


def test_unrelated_runtime_package_fails_before_mutation(
    cluster_fixture: dict[str, Path],
) -> None:
    result = run_cluster_script(
        BOOTSTRAP_SCRIPT,
        cluster_fixture,
        KIP_TEST_CONFLICT_PACKAGE="containerd",
    )

    assert result.returncode == 1
    commands = (
        cluster_fixture["command_log"].read_text()
        if cluster_fixture["command_log"].exists()
        else ""
    )
    assert "apt-get" not in commands


def test_unmanaged_containerd_config_fails_before_mutation(
    cluster_fixture: dict[str, Path],
) -> None:
    containerd_dir = cluster_fixture["etc_root"] / "containerd"
    containerd_dir.mkdir()
    (containerd_dir / "config.toml").write_text("version = 2\n")

    result = run_cluster_script(
        BOOTSTRAP_SCRIPT,
        cluster_fixture,
        KIP_TEST_PACKAGES_INSTALLED="1",
    )

    assert result.returncode == 1
    commands = (
        cluster_fixture["command_log"].read_text()
        if cluster_fixture["command_log"].exists()
        else ""
    )
    assert "apt-get" not in commands


def test_existing_cluster_version_drift_fails_before_mutation(
    cluster_fixture: dict[str, Path],
) -> None:
    kubernetes_dir = cluster_fixture["etc_root"] / "kubernetes"
    kubernetes_dir.mkdir()
    (kubernetes_dir / "admin.conf").write_text("existing\n")
    containerd_dir = cluster_fixture["etc_root"] / "containerd"
    containerd_dir.mkdir()
    (containerd_dir / "config.toml").write_text("# Managed by Kube Inference Platform v0.1.\n")

    result = run_cluster_script(
        BOOTSTRAP_SCRIPT,
        cluster_fixture,
        KIP_TEST_PACKAGES_INSTALLED="1",
        KIP_TEST_SERVER_VERSION="1.35.0",
    )

    assert result.returncode == 1
    commands = cluster_fixture["command_log"].read_text()
    assert "apt-get" not in commands


def test_runtime_not_ready_blocks_bootstrap(cluster_fixture: dict[str, Path]) -> None:
    result = run_cluster_script(
        BOOTSTRAP_SCRIPT,
        cluster_fixture,
        KIP_TEST_RUNTIME_READY="false",
    )

    assert result.returncode == 1
    assert "containerd runtime" in result.stdout
    assert "FAIL" in result.stdout


def test_cilium_connectivity_failure_blocks_bootstrap(
    cluster_fixture: dict[str, Path],
) -> None:
    result = run_cluster_script(
        BOOTSTRAP_SCRIPT,
        cluster_fixture,
        KIP_TEST_CILIUM_CONNECTIVITY_FAIL="1",
    )

    assert result.returncode == 1
    assert "Cluster health" in result.stdout
    assert "FAIL" in result.stdout


def test_cluster_validation_passes_for_healthy_fixture(cluster_fixture: dict[str, Path]) -> None:
    result = run_cluster_script(VALIDATE_SCRIPT, cluster_fixture)

    assert result.returncode == 0
    assert "11 passed" in result.stdout
    assert "All nodes are Ready and schedulable" in result.stdout
    assert "--- kubectl get nodes ---" not in result.stdout


def test_cluster_validation_fails_for_unhealthy_coredns(cluster_fixture: dict[str, Path]) -> None:
    result = run_cluster_script(
        VALIDATE_SCRIPT,
        cluster_fixture,
        KIP_TEST_FAIL_LABEL="k8s-app=kube-dns",
    )

    assert result.returncode == 1
    assert "Cluster DNS" in result.stdout
    assert "FAIL" in result.stdout


def test_cluster_validation_verbose_and_no_color(cluster_fixture: dict[str, Path]) -> None:
    result = run_cluster_script(
        VALIDATE_SCRIPT,
        cluster_fixture,
        KIP_VERBOSE="1",
        KIP_FORCE_COLOR="1",
    )

    assert result.returncode == 0
    assert "--- kubectl get --raw=/readyz ---" in result.stdout
    assert "\x1b[" not in result.stdout


def test_invalid_cluster_verbose_exits_two(cluster_fixture: dict[str, Path]) -> None:
    result = run_cluster_script(
        VALIDATE_SCRIPT,
        cluster_fixture,
        KIP_VERBOSE="sometimes",
    )

    assert result.returncode == 2
    assert "VERBOSE must be 0 or 1" in result.stderr


def test_invalid_make_variable_exits_two(cluster_fixture: dict[str, Path]) -> None:
    result = subprocess.run(
        ["make", "cluster-check", "VERBOSE=sometimes"],
        cwd=ROOT,
        env=cluster_environment(cluster_fixture),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "VERBOSE must be 0 or 1" in result.stderr
