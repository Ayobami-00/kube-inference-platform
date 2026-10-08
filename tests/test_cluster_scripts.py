"""Deterministic subprocess tests for single-node cluster tooling."""

# ruff: noqa: E501

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
BOOTSTRAP_SCRIPT = ROOT / "deploy" / "cluster" / "bootstrap.sh"
VALIDATE_SCRIPT = ROOT / "deploy" / "cluster" / "validate.sh"
DESTROY_SCRIPT = ROOT / "deploy" / "destroy.sh"
CRI_ENDPOINT = "unix:///run/containerd/containerd.sock"
GPU_CHART_DIGEST = "sha256:23268170f1b608365ef8c15e0c0df87bff0017768e048bd9e73e41a1096299e4"
RUNTIME_PROFILE = ROOT / "deploy" / "runtime" / "vllm" / "profiles" / "qwen2.5-7b-instruct.json"
RUNTIME_IMAGE_DIGEST = "sha256:3e10e8189823e0f7ae4620c271bcdaaf64127ec7d0edc351591a508498b7684a"
RUNTIME_MODEL_REVISION = "a09a35458c702b33eeacc393d103063234e8bc28"


def runtime_profile_hash() -> str:
    canonical = json.dumps(
        json.loads(RUNTIME_PROFILE.read_text()), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(f"{canonical}\n".encode()).hexdigest()


def mark_owned(path: Path, content: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".kip-managed-v1").write_text(f"{content}\n")


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
    opt_root = tmp_path / "opt"
    bpf_root = tmp_path / "bpf"
    target_home = tmp_path / "home" / "tester"
    for directory in (etc_root, run_root, var_root, bin_root, opt_root, bpf_root, target_home):
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
    network_state = tmp_path / "cni-network-present"
    firewall_state = tmp_path / "firewall-cleaned"
    bpf_busy_state = tmp_path / "bpf-busy-once"
    benchmark_work_dir = tmp_path / "benchmark-work"

    jq_path = shutil.which("jq")
    sha_path = shutil.which("sha256sum") or shutil.which("shasum")
    assert jq_path is not None
    assert sha_path is not None
    write_command(command_dir, "jq", f'exec "{jq_path}" "$@"')
    if Path(sha_path).name == "shasum":
        write_command(command_dir, "sha256sum", f'exec "{sha_path}" -a 256 "$@"')
    else:
        write_command(command_dir, "sha256sum", f'exec "{sha_path}" "$@"')

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
    'netns list')
        [ -e "$KIP_TEST_NETWORK_STATE" ] \
            && printf '%s\\n' 'cni-fixture (id: 1)'
        ;;
    'netns delete cni-fixture')
        printf 'ip %s\\n' "$args" >> "$KIP_TEST_COMMAND_LOG"
        rm -f "$KIP_TEST_NETWORK_STATE"
        ;;
    '-o link show')
        [ -e "$KIP_TEST_NETWORK_STATE" ] \
            && printf '%s\\n' '17: lxcabc123@if16: <BROADCAST,UP>'
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
    write_command(
        command_dir,
        "mountpoint",
        """[ "${KIP_TEST_RUNTIME_MOUNTS:-0}" = 1 ]
case "$*" in
    *'/run/nvidia/driver' | *'/run/cilium/cgroupv2') exit 0 ;;
    *) exit 1 ;;
esac""",
    )
    write_command(
        command_dir,
        "umount",
        'printf \'umount %s\\n\' "$*" >> "$KIP_TEST_COMMAND_LOG"',
    )
    write_command(
        command_dir,
        "iptables-save",
        """if [ ! -e "$KIP_TEST_FIREWALL_STATE" ]; then
    printf '%s\\n' '*filter' ':INPUT ACCEPT [0:0]' ':KUBE-SERVICES - [0:0]' \
        '-A INPUT -j KUBE-SERVICES' 'COMMIT'
else
    printf '%s\\n' '*filter' ':INPUT ACCEPT [0:0]' 'COMMIT'
fi""",
    )
    write_command(
        command_dir,
        "iptables-restore",
        """cat > "$KIP_TEST_FIREWALL_CAPTURE"
touch "$KIP_TEST_FIREWALL_STATE"
printf '%s\\n' iptables-restore >> "$KIP_TEST_COMMAND_LOG"
""",
    )
    write_command(command_dir, "lspci", "exit 0")
    write_command(command_dir, "containerd", "printf '%s\\n' 'containerd 2.3.4 test-build'")
    write_command(command_dir, "kubelet", "printf '%s\\n' 'Kubernetes v1.36.4'")
    write_command(
        command_dir,
        "sudo",
        """printf 'sudo %s\\n' "$*" >> "$KIP_TEST_COMMAND_LOG"
case "$*" in
    '-n true') exit 0 ;;
    chown*) exit 0 ;;
esac
if [ "${KIP_TEST_BPF_BUSY_ONCE:-0}" = 1 ] \
    && [ "$*" = "rm -rf $KIP_SYS_FS_BPF_ROOT/cilium" ] \
    && [ ! -e "$KIP_TEST_BPF_BUSY_STATE" ]; then
    touch "$KIP_TEST_BPF_BUSY_STATE"
    exit 1
fi
command_name=$1
shift
if [ -x "$KIP_TEST_COMMAND_DIR/$command_name" ]; then
    exec "$KIP_TEST_COMMAND_DIR/$command_name" "$@"
fi
exec "$command_name" "$@"
""",
    )
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
    *'${Status}|${Version}'*)
        printf '%s\\n' "${KIP_TEST_PACKAGE_SELECTION:-install} ok installed|$package_version"
        ;;
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
    *'get --raw=/readyz'*)
        [ "${KIP_TEST_API_FAIL:-0}" != 1 ] || exit 1
        printf '%s\\n' ok
        ;;
    *'api-resources --verbs=list --namespaced=true -o name'*)
        printf '%s\\n' configmaps
        ;;
    *'get namespaces -o json'*)
        if [ "${KIP_TEST_FOREIGN_NAMESPACE:-0}" = 1 ]; then
            printf '%s\\n' '{"items":[{"metadata":{"name":"default"}},{"metadata":{"name":"kube-system"}},{"metadata":{"name":"user-application"}}]}'
        else
            printf '%s\\n' '{"items":[{"metadata":{"name":"default"}},{"metadata":{"name":"kube-system"}},{"metadata":{"name":"kube-public"}},{"metadata":{"name":"kube-node-lease"}}]}'
        fi
        ;;
    *'get deployments,statefulsets,daemonsets,jobs,cronjobs,pods --all-namespaces -o json'*)
        if [ "${KIP_TEST_FOREIGN_WORKLOAD:-0}" = 1 ]; then
            printf '%s\\n' '{"items":[{"metadata":{"namespace":"default","name":"foreign-app"}}]}'
        else
            printf '%s\\n' '{"items":[]}'
        fi
        ;;
    *'get persistentvolumes -o json'*)
        if [ "${KIP_TEST_FOREIGN_PV:-0}" = 1 ]; then
            printf '%s\\n' '{"items":[{"metadata":{"name":"foreign-pv","labels":{}}}]}'
        else
            printf '%s\\n' '{"items":[]}'
        fi
        ;;
    *'get customresourcedefinition '*' -o jsonpath={.metadata.name}'*)
        [ "${KIP_TEST_FOREIGN_MONITORING_CR:-0}" = 1 ] || exit 1
        previous=''
        for argument in "$@"; do
            if [ "$previous" = customresourcedefinition ]; then
                printf '%s' "$argument"
                break
            fi
            previous=$argument
        done
        ;;
    *'get customresourcedefinition '*' -o jsonpath={.metadata.labels.app\\.kubernetes\\.io/managed-by}'*)
        printf '%s' kube-inference-platform
        ;;
    *'get customresourcedefinition '*' -o jsonpath={.metadata.annotations.kube-inference-platform\\.io/chart-digest}'*)
        printf '%s' 'sha256:0032315f2580f488ce433ff9d18de24ce631857ff7f1d73693171eeeb7affd52'
        ;;
    *'get '*.monitoring.coreos.com' --all-namespaces -o json'*)
        printf '%s\\n' '{"items":[{"metadata":{"namespace":"foreign-observability","name":"foreign-monitor","labels":{"app.kubernetes.io/managed-by":"someone-else"}}}]}'
        ;;
    *'-n kip-benchmark get configmaps -o json'*)
        if [ "${KIP_TEST_BENCHMARK_NAMESPACE_EXTRA:-0}" = 1 ]; then
            printf '%s\\n' '{"items":[{"kind":"ConfigMap","metadata":{"name":"foreign"}}]}'
        else
            printf '%s\\n' '{"items":[]}'
        fi
        ;;
    *'-n kip-observability get configmaps -o json'*)
        if [ "${KIP_TEST_OBSERVABILITY_NAMESPACE_EXTRA:-0}" = 1 ]; then
            printf '%s\\n' '{"items":[{"kind":"ConfigMap","metadata":{"name":"foreign"}}]}'
        else
            printf '%s\\n' '{"items":[]}'
        fi
        ;;
    *'-n kip-runtime get configmaps -o json'*)
        if [ "${KIP_TEST_RUNTIME_NAMESPACE_EXTRA:-0}" = 1 ]; then
            printf '%s\\n' '{"items":[{"kind":"ConfigMap","metadata":{"name":"foreign"}}]}'
        else
            printf '%s\\n' '{"items":[]}'
        fi
        ;;
    *'-n gpu-operator get configmaps -o json'*)
        if [ "${KIP_TEST_GPU_NAMESPACE_EXTRA:-0}" = 1 ]; then
            printf '%s\\n' '{"items":[{"kind":"ConfigMap","metadata":{"name":"foreign"}}]}'
        else
            printf '%s\\n' '{"items":[]}'
        fi
        ;;
    *'get namespace kip-runtime -o jsonpath={.metadata.name}'*)
        [ "${KIP_TEST_RUNTIME_RELEASE:-0}" = 1 ] && printf '%s' kip-runtime || exit 1
        ;;
    *'get namespace kip-runtime -o jsonpath={.metadata.labels.app\\.kubernetes\\.io/managed-by}'*)
        [ "${KIP_TEST_RUNTIME_RELEASE:-0}" = 1 ] || exit 1
        printf '%s' "${KIP_TEST_RUNTIME_OWNER:-kube-inference-platform}"
        ;;
    *'get namespace kip-runtime -o jsonpath={.metadata.annotations.kube-inference-platform/profile-sha256}'*)
        printf '%s' "$KIP_TEST_RUNTIME_PROFILE_HASH" ;;
    *'get namespace kip-benchmark -o jsonpath={.metadata.name}'*)
        [ "${KIP_TEST_BENCHMARK_RELEASE:-0}" = 1 ] && printf '%s' kip-benchmark || exit 1
        ;;
    *'get namespace kip-benchmark -o jsonpath={.metadata.labels.app\\.kubernetes\\.io/managed-by}'*)
        printf '%s' "${KIP_TEST_BENCHMARK_OWNER:-kube-inference-platform}" ;;
    *'-n kip-benchmark get serviceaccount kip-benchmark -o jsonpath={.metadata.name}'*)
        [ "${KIP_TEST_BENCHMARK_RELEASE:-0}" = 1 ] && printf '%s' kip-benchmark || exit 1
        ;;
    *'-n kip-benchmark get serviceaccount kip-benchmark -o jsonpath={.metadata.labels.app\\.kubernetes\\.io/managed-by}'*)
        printf '%s' "${KIP_TEST_BENCHMARK_OWNER:-kube-inference-platform}" ;;
    *'-n kip-benchmark get persistentvolumeclaim kip-benchmark-results -o jsonpath={.metadata.name}'*)
        [ "${KIP_TEST_BENCHMARK_RELEASE:-0}" = 1 ] && printf '%s' kip-benchmark-results || exit 1
        ;;
    *'-n kip-benchmark get persistentvolumeclaim kip-benchmark-results -o jsonpath={.metadata.labels.app\\.kubernetes\\.io/managed-by}'*)
        printf '%s' "${KIP_TEST_BENCHMARK_OWNER:-kube-inference-platform}" ;;
    *'get persistentvolume kip-benchmark-results -o jsonpath={.metadata.name}'*)
        [ "${KIP_TEST_BENCHMARK_RELEASE:-0}" = 1 ] && printf '%s' kip-benchmark-results || exit 1
        ;;
    *'get persistentvolume kip-benchmark-results -o jsonpath={.metadata.labels.app\\.kubernetes\\.io/managed-by}'*)
        printf '%s' "${KIP_TEST_BENCHMARK_OWNER:-kube-inference-platform}" ;;
    *'get namespace kip-observability -o jsonpath={.metadata.name}'*)
        [ "${KIP_TEST_OBSERVABILITY_RELEASE:-0}" = 1 ] && printf '%s' kip-observability || exit 1
        ;;
    *'get namespace kip-observability -o jsonpath={.metadata.labels.app\\.kubernetes\\.io/managed-by}'*)
        printf '%s' "${KIP_TEST_OBSERVABILITY_OWNER:-kube-inference-platform}" ;;
    *'get namespace gpu-operator -o jsonpath={.metadata.name}'*)
        [ "${KIP_TEST_GPU_RELEASE:-0}" = 1 ] && printf '%s' gpu-operator || exit 1
        ;;
    *'get storageclass kip-local-static -o jsonpath={.metadata.name}'*)
        [ "${KIP_TEST_OBSERVABILITY_RELEASE:-0}" = 1 ] && printf '%s' kip-local-static || exit 1
        ;;
    *'get storageclass kip-local-static -o jsonpath={.metadata.labels.app\\.kubernetes\\.io/managed-by}'*)
        printf '%s' "${KIP_TEST_OBSERVABILITY_VOLUME_OWNER:-kube-inference-platform}" ;;
    *'get persistentvolume kip-prometheus -o jsonpath={.metadata.name}'*|*'get persistentvolume kip-alertmanager -o jsonpath={.metadata.name}'*|*'get persistentvolume kip-grafana -o jsonpath={.metadata.name}'*)
        [ "${KIP_TEST_OBSERVABILITY_RELEASE:-0}" = 1 ] || exit 1
        case "$args" in
            *kip-prometheus*) printf '%s' kip-prometheus ;;
            *kip-alertmanager*) printf '%s' kip-alertmanager ;;
            *kip-grafana*) printf '%s' kip-grafana ;;
        esac
        ;;
    *'get persistentvolume kip-prometheus -o jsonpath={.metadata.labels.app\\.kubernetes\\.io/managed-by}'*|*'get persistentvolume kip-alertmanager -o jsonpath={.metadata.labels.app\\.kubernetes\\.io/managed-by}'*|*'get persistentvolume kip-grafana -o jsonpath={.metadata.labels.app\\.kubernetes\\.io/managed-by}'*)
        printf '%s' "${KIP_TEST_OBSERVABILITY_VOLUME_OWNER:-kube-inference-platform}" ;;
    *'-n kip-runtime get deployment,service -o json')
        if [ "${KIP_TEST_RUNTIME_EXTRA:-0}" = 1 ]; then
            printf '%s\\n' '{"items":[{"kind":"Deployment","metadata":{"name":"unmanaged-runtime","labels":{}}}]}'
        else
            printf '%s\\n' '{"items":[{"kind":"Deployment","metadata":{"name":"vllm-qwen25-7b","labels":{"app.kubernetes.io/managed-by":"kube-inference-platform"}}},{"kind":"Service","metadata":{"name":"vllm-qwen25-7b","labels":{"app.kubernetes.io/managed-by":"kube-inference-platform"}}}]}'
        fi
        ;;
    *'-n kip-runtime get deployment vllm-qwen25-7b -o jsonpath={.metadata.name}'*|*'-n kip-runtime get service vllm-qwen25-7b -o jsonpath={.metadata.name}'*)
        [ "${KIP_TEST_RUNTIME_RELEASE:-0}" = 1 ] && printf '%s' vllm-qwen25-7b || exit 1
        ;;
    *'-n kip-runtime get deployment vllm-qwen25-7b -o jsonpath={.metadata.labels.app\\.kubernetes\\.io/managed-by}'*|*'-n kip-runtime get service vllm-qwen25-7b -o jsonpath={.metadata.labels.app\\.kubernetes\\.io/managed-by}'*)
        printf '%s' "${KIP_TEST_RUNTIME_OWNER:-kube-inference-platform}" ;;
    *'-n kip-runtime get deployment vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/image-digest}'*|*'-n kip-runtime get service vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/image-digest}'*)
        printf '%s' "$KIP_TEST_RUNTIME_DIGEST" ;;
    *'-n kip-runtime get deployment vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/profile-sha256}'*|*'-n kip-runtime get service vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/profile-sha256}'*)
        printf '%s' "$KIP_TEST_RUNTIME_PROFILE_HASH" ;;
    *'-n kip-runtime get deployment vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/model-revision}'*|*'-n kip-runtime get service vllm-qwen25-7b -o jsonpath={.metadata.annotations.kube-inference-platform/model-revision}'*)
        printf '%s' "$KIP_TEST_RUNTIME_MODEL_REVISION" ;;
    *'delete namespace kip-runtime '*) printf '%s\\n' runtime-deleted ;;
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
            "{\\"status\\":{\\"conditions\\":[{\\"status\\":${KIP_TEST_RUNTIME_READY:-true},\\"type\\":\\"RuntimeReady\\"}]}}"
        ;;
esac""",
    )
    write_command(
        command_dir,
        "helm",
        """printf 'helm %s\\n' "$*" >> "$KIP_TEST_COMMAND_LOG"
case "$*" in
    'version --short') printf '%s\\n' 'v3.21.4+gtest' ;;
    'status gpu-operator '*'--output json'*)
        [ "${KIP_TEST_GPU_RELEASE:-0}" = 1 ] || exit 1
        printf '%s\\n' '{"info":{"status":"deployed"}}'
        ;;
    'status gpu-operator '*) [ "${KIP_TEST_GPU_RELEASE:-0}" = 1 ] ;;
    'status kip-monitoring '*) [ "${KIP_TEST_OBSERVABILITY_RELEASE:-0}" = 1 ] ;;
    'status kip-opencost '*) [ "${KIP_TEST_OBSERVABILITY_RELEASE:-0}" = 1 ] ;;
    'get values gpu-operator '*)
        [ "${KIP_TEST_GPU_RELEASE:-0}" = 1 ] || exit 1
        toolkit_enabled=true
        [ "${KIP_TEST_GPU_VALUE_DRIFT:-0}" != 1 ] || toolkit_enabled=false
        printf '%s\\n' \
            "{\\\"clusterPolicy\\\":{\\\"deployCR\\\":true},\\\"gpuCluster\\\":{\\\"deployCR\\\":false},\\\"psa\\\":{\\\"enabled\\\":true},\\\"nfd\\\":{\\\"enabled\\\":true},\\\"cdi\\\":{\\\"enabled\\\":true,\\\"nriPluginEnabled\\\":false},\\\"sandboxWorkloads\\\":{},\\\"driver\\\":{\\\"enabled\\\":true,\\\"version\\\":\\\"595.91.07\\\"},\\\"toolkit\\\":{\\\"enabled\\\":$toolkit_enabled,\\\"env\\\":[{\\\"name\\\":\\\"CONTAINERD_CONFIG\\\",\\\"value\\\":\\\"/etc/containerd/config.toml\\\"},{\\\"name\\\":\\\"CONTAINERD_SOCKET\\\",\\\"value\\\":\\\"/run/containerd/containerd.sock\\\"},{\\\"name\\\":\\\"RUNTIME_CONFIG_SOURCE\\\",\\\"value\\\":\\\"file=/etc/containerd/config.toml\\\"},{\\\"name\\\":\\\"RUNTIME_DROP_IN_CONFIG\\\",\\\"value\\\":\\\"/etc/containerd/conf.d/99-nvidia.toml\\\"}]},\\\"devicePlugin\\\":{\\\"enabled\\\":true},\\\"gfd\\\":{},\\\"dcgm\\\":{\\\"enabled\\\":false},\\\"dcgmExporter\\\":{\\\"enabled\\\":true,\\\"repository\\\":\\\"nvcr.io/nvidia/k8s\\\",\\\"image\\\":\\\"dcgm-exporter\\\",\\\"version\\\":\\\"4.6.0-4.8.3-distroless@sha256:b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a\\\",\\\"serviceMonitor\\\":{\\\"enabled\\\":false},\\\"config\\\":{\\\"name\\\":\\\"kip-dcgm-metrics\\\"},\\\"env\\\":[{\\\"name\\\":\\\"DCGM_EXPORTER_INTERVAL\\\",\\\"value\\\":\\\"1000\\\"},{\\\"name\\\":\\\"DCGM_EXPORTER_COLLECTORS\\\",\\\"value\\\":\\\"/etc/dcgm-exporter/dcgm-metrics.csv\\\"}]},\\\"mig\\\":{\\\"strategy\\\":\\\"none\\\"},\\\"migManager\\\":{\\\"enabled\\\":false},\\\"gds\\\":{},\\\"gdrcopy\\\":{},\\\"vgpuManager\\\":{},\\\"vgpuDeviceManager\\\":{},\\\"vfioManager\\\":{},\\\"kataManager\\\":{},\\\"sandboxDevicePlugin\\\":{},\\\"kataSandboxDevicePlugin\\\":{},\\\"ccManager\\\":{},\\\"kip\\\":{\\\"driverMode\\\":\\\"operator\\\",\\\"chartDigest\\\":\\\"$KIP_TEST_GPU_DIGEST\\\",\\\"dcgmExporterEnabled\\\":true}}"
        ;;
    'list --namespace gpu-operator --filter ^gpu-operator$ --output json '*)
        printf '[{"name":"gpu-operator","namespace":"gpu-operator","chart":"gpu-operator-%s"}]\\n' \
            "${KIP_TEST_GPU_CHART_VERSION:-v26.7.0}"
        ;;
    'get values kip-monitoring '*)
        [ "${KIP_TEST_OBSERVABILITY_RELEASE:-0}" = 1 ] || exit 1
        printf '%s\\n' \
            '{\"kip\":{\"managedBy\":\"kube-inference-platform\",\"chartDigest\":\"sha256:0032315f2580f488ce433ff9d18de24ce631857ff7f1d73693171eeeb7affd52\"}}'
        ;;
    'get values kip-opencost '*)
        [ "${KIP_TEST_OBSERVABILITY_RELEASE:-0}" = 1 ] || exit 1
        printf '%s\\n' \
            '{\"kip\":{\"managedBy\":\"kube-inference-platform\",\"chartDigest\":\"sha256:534e846fc11b8e7e9967dd07fe808e552395a0985457f0cec6e0715d6c177bbc\"}}'
        ;;
    'list --namespace kip-observability --filter ^kip-monitoring$ --output json '*)
        printf '[{\"name\":\"kip-monitoring\",\"namespace\":\"kip-observability\",\"chart\":\"kube-prometheus-stack-%s\"}]\\n' \
            "${KIP_TEST_MONITORING_CHART_VERSION:-91.4.0}"
        ;;
    'list --namespace kip-observability --filter ^kip-opencost$ --output json '*)
        printf '[{\"name\":\"kip-opencost\",\"namespace\":\"kip-observability\",\"chart\":\"opencost-%s\"}]\\n' \
            "${KIP_TEST_OPENCOST_CHART_VERSION:-2.5.30}"
        ;;
    'status cilium '*) exit 0 ;;
esac""",
    )
    write_command(
        command_dir,
        "cilium",
        """printf 'cilium-columns %s\\n' "${COLUMNS:-unset}" >> "$KIP_TEST_COMMAND_LOG"
if [ "$*" = 'version --client' ]; then
    printf '%s\\n' 'cilium-cli: v0.19.7'
elif echo "$*" | grep -q 'connectivity test'; then
    printf 'cilium %s\\n' "$*" >> "$KIP_TEST_COMMAND_LOG"
    case "$*" in
        *--cleanup*) ;;
        *) [ "${KIP_TEST_CILIUM_CONNECTIVITY_FAIL:-0}" != 1 ] || exit 1 ;;
    esac
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
    write_command(
        command_dir,
        "apt-get",
        """printf 'apt-get %s\\n' "$*" >> "$KIP_TEST_COMMAND_LOG"
case "$*" in purge*) rm -f "$KIP_TEST_PACKAGES_STATE" ;; esac""",
    )
    for command_name in ("apt-mark", "modprobe", "sysctl"):
        write_command(
            command_dir,
            command_name,
            f'printf \'{command_name} %s\\n\' "$*" >> "$KIP_TEST_COMMAND_LOG"',
        )
    write_command(
        command_dir,
        "env",
        """printf 'env %s\\n' "$*" >> "$KIP_TEST_COMMAND_LOG"
case "$*" in
    *'apt-get install'*) touch "$KIP_TEST_PACKAGES_STATE" ;;
    *'apt-get purge'*) rm -f "$KIP_TEST_PACKAGES_STATE" ;;
esac""",
    )

    return {
        "command_dir": command_dir,
        "etc_root": etc_root,
        "run_root": run_root,
        "var_root": var_root,
        "bin_root": bin_root,
        "opt_root": opt_root,
        "bpf_root": bpf_root,
        "target_home": target_home,
        "os_release": os_release,
        "command_log": command_log,
        "capture_config": capture_config,
        "swap_disabled": swap_disabled,
        "packages_state": packages_state,
        "network_state": network_state,
        "firewall_state": firewall_state,
        "bpf_busy_state": bpf_busy_state,
        "benchmark_work_dir": benchmark_work_dir,
        "firewall_capture": tmp_path / "firewall-restored.txt",
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
            "KIP_TEST_NETWORK_STATE": str(fixture["network_state"]),
            "KIP_TEST_FIREWALL_STATE": str(fixture["firewall_state"]),
            "KIP_TEST_BPF_BUSY_STATE": str(fixture["bpf_busy_state"]),
            "KIP_TEST_FIREWALL_CAPTURE": str(fixture["firewall_capture"]),
            "KIP_TEST_CONTAINERD_PACKAGE": ("2.3.4-2~ubuntu.24.04~noble"),
            "KIP_TEST_GPU_DIGEST": GPU_CHART_DIGEST,
            "KIP_TEST_RUNTIME_DIGEST": RUNTIME_IMAGE_DIGEST,
            "KIP_TEST_RUNTIME_MODEL_REVISION": RUNTIME_MODEL_REVISION,
            "KIP_TEST_RUNTIME_PROFILE_HASH": runtime_profile_hash(),
            "KIP_ETC_ROOT": str(fixture["etc_root"]),
            "KIP_RUN_ROOT": str(fixture["run_root"]),
            "KIP_VAR_ROOT": str(fixture["var_root"]),
            "KIP_USR_LOCAL_BIN": str(fixture["bin_root"]),
            "KIP_OPT_ROOT": str(fixture["opt_root"]),
            "KIP_SYS_FS_BPF_ROOT": str(fixture["bpf_root"]),
            "KIP_OS_RELEASE_FILE": str(fixture["os_release"]),
            "KIP_TARGET_USER": "tester",
            "KIP_TARGET_HOME": str(fixture["target_home"]),
            "KIP_BENCHMARK_WORK_DIR": str(fixture["benchmark_work_dir"]),
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

    assert result.returncode == 0, result.stdout + result.stderr
    commands = cluster_fixture["command_log"].read_text()
    assert f"containerd.io={package_version}" in commands
    assert "docker-ce" not in commands
    assert "apt-get upgrade" not in commands


def test_bootstrap_reuses_provider_docker_repository_without_signed_by_conflict(
    cluster_fixture: dict[str, Path],
) -> None:
    source_dir = cluster_fixture["etc_root"] / "apt" / "sources.list.d"
    source_dir.mkdir(parents=True)
    provider_source = source_dir / "docker.list"
    provider_source.write_text(
        "deb [arch=amd64 signed-by=/etc/apt/cloud-init.gpg.d/docker.gpg] "
        "https://download.docker.com/linux/ubuntu noble stable\n"
    )
    partial_kip_source = source_dir / "docker.sources"

    result = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)

    assert result.returncode == 0, result.stdout + result.stderr
    assert provider_source.exists()
    assert not partial_kip_source.exists()
    commands = cluster_fixture["command_log"].read_text()
    assert "download.docker.com/linux/ubuntu/gpg" not in commands


def test_bootstrap_refuses_unknown_duplicate_docker_source(
    cluster_fixture: dict[str, Path],
) -> None:
    source_dir = cluster_fixture["etc_root"] / "apt" / "sources.list.d"
    source_dir.mkdir(parents=True)
    (source_dir / "docker.list").write_text(
        "deb [arch=amd64 signed-by=/provider/key.gpg] "
        "https://download.docker.com/linux/ubuntu noble stable\n"
    )
    (source_dir / "docker.sources").write_text(
        "URIs: https://download.docker.com/linux/ubuntu\nSigned-By: /unknown/key.gpg\n"
    )

    result = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)

    assert result.returncode == 1
    assert "apt-get update" not in cluster_fixture["command_log"].read_text()


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
    assert "cilium connectivity test --single-node --ip-families ipv4 --hubble=false" in commands
    assert "cilium connectivity test --cleanup" in commands
    assert "cilium status --wait --wait-duration 10m --interactive=false --output json" in commands
    assert "12 passed" in result.stdout
    assert "install and validate the NVIDIA GPU Operator" in result.stdout


def test_cilium_cli_uses_safe_terminal_width(cluster_fixture: dict[str, Path]) -> None:
    result = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture, COLUMNS="0")

    assert result.returncode == 0, result.stdout + result.stderr
    commands = cluster_fixture["command_log"].read_text()
    assert "cilium-columns 120" in commands


def test_bootstrap_accepts_assigned_node_ip_override(cluster_fixture: dict[str, Path]) -> None:
    result = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture, KIP_NODE_IP="192.0.2.10")

    assert result.returncode == 0
    assert "gpu-node-1 at 192.0.2.10" in result.stdout


def test_non_root_runtime_validation_uses_sudo_for_cri_socket(
    cluster_fixture: dict[str, Path],
) -> None:
    result = run_cluster_script(
        BOOTSTRAP_SCRIPT,
        cluster_fixture,
        KIP_TEST_EUID="1000",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    commands = cluster_fixture["command_log"].read_text()
    assert f"sudo crictl --runtime-endpoint {CRI_ENDPOINT} version" in commands
    assert f"sudo crictl --runtime-endpoint {CRI_ENDPOINT} info" in commands
    assert "sudo kubeadm config images pull --config" in commands


def test_non_root_cluster_check_uses_sudo_for_cri_socket(
    cluster_fixture: dict[str, Path],
) -> None:
    result = run_cluster_script(
        VALIDATE_SCRIPT,
        cluster_fixture,
        KIP_TEST_EUID="1000",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    commands = cluster_fixture["command_log"].read_text()
    assert f"sudo crictl --runtime-endpoint {CRI_ENDPOINT} version" in commands
    assert f"sudo crictl --runtime-endpoint {CRI_ENDPOINT} info" in commands


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
        KIP_TEST_PACKAGE_SELECTION="hold",
        KIP_TEST_ROUTE_CONFLICT="1",
    )

    assert result.returncode == 0, result.stderr
    assert "readonly variable" not in result.stderr
    commands = cluster_fixture["command_log"].read_text()
    assert "kubeadm init --config" not in commands
    assert "apt-get" not in commands


def test_partial_cluster_state_fails_without_reset(cluster_fixture: dict[str, Path]) -> None:
    manifests = cluster_fixture["etc_root"] / "kubernetes" / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "kube-apiserver.yaml").write_text("apiVersion: v1\n")

    result = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)

    assert result.returncode == 1
    commands = (
        cluster_fixture["command_log"].read_text()
        if cluster_fixture["command_log"].exists()
        else ""
    )
    assert "kubeadm reset" not in commands
    assert "apt-get" not in commands


def test_package_placeholder_directories_remain_fresh_retry_state(
    cluster_fixture: dict[str, Path],
) -> None:
    manifests = cluster_fixture["etc_root"] / "kubernetes" / "manifests"
    kubelet_dir = cluster_fixture["var_root"] / "lib" / "kubelet"
    manifests.mkdir(parents=True)
    kubelet_dir.mkdir(parents=True)
    (manifests / ".kubelet-keep").touch()
    (kubelet_dir / ".kubelet-keep").touch()

    result = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)

    assert result.returncode == 0, result.stderr
    assert "cluster state fresh" in result.stdout


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


def test_interrupted_package_stage_can_adopt_its_exact_containerd_package(
    cluster_fixture: dict[str, Path],
) -> None:
    containerd_dir = cluster_fixture["etc_root"] / "containerd"
    modules_dir = cluster_fixture["etc_root"] / "modules-load.d"
    sysctl_dir = cluster_fixture["etc_root"] / "sysctl.d"
    containerd_dir.mkdir()
    modules_dir.mkdir()
    sysctl_dir.mkdir()
    (containerd_dir / "config.toml").write_text("version = 3\n")
    (modules_dir / "kube-inference-platform.conf").write_text("overlay\nbr_netfilter\n")
    (sysctl_dir / "99-kube-inference-platform.conf").write_text(
        "# Managed by Kube Inference Platform v0.1.\nnet.ipv4.ip_forward = 1\n"
    )

    result = run_cluster_script(
        BOOTSTRAP_SCRIPT,
        cluster_fixture,
        KIP_TEST_PACKAGES_INSTALLED="1",
        KIP_TEST_PACKAGE_SELECTION="hold",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    config = (containerd_dir / "config.toml").read_text()
    assert "Managed by Kube Inference Platform" in config
    assert (containerd_dir / "config.toml.kip-pre-bootstrap").exists()


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
    assert "cilium connectivity test --cleanup" in cluster_fixture["command_log"].read_text()
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


def test_destroy_removes_owned_cluster_and_restores_host(
    cluster_fixture: dict[str, Path],
) -> None:
    fstab = cluster_fixture["etc_root"] / "fstab"
    fstab.write_text("UUID=root / ext4 defaults 0 1\n/swapfile none swap sw 0 0\n")
    bootstrap = run_cluster_script(
        BOOTSTRAP_SCRIPT,
        cluster_fixture,
        KIP_TEST_SWAP_SOURCE="/swapfile",
    )
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr
    cni_binary = cluster_fixture["opt_root"] / "cni" / "bin" / "cilium-cni"
    cni_binary.parent.mkdir(parents=True)
    cni_binary.write_text("fixture\n")
    cilium_bpf = cluster_fixture["bpf_root"] / "cilium" / "endpoints"
    cilium_bpf.mkdir(parents=True)
    cilium_global = cluster_fixture["bpf_root"] / "tc" / "globals" / "cilium_test"
    cilium_global.parent.mkdir(parents=True)
    cilium_global.write_text("fixture\n")
    nvidia_state = cluster_fixture["run_root"] / "nvidia" / "nvidia-driver.state"
    nvidia_state.parent.mkdir(parents=True)
    nvidia_state.write_text("ready\n")
    model_cache = (
        cluster_fixture["var_root"] / "lib" / "kube-inference-platform" / "model-cache" / "vllm"
    )
    model_cache.mkdir(parents=True)
    mark_owned(model_cache.parent, "kube-inference-platform model cache v1")
    (model_cache / "model.bin").write_text("fixture\n")
    benchmark_results = (
        cluster_fixture["var_root"] / "lib" / "kube-inference-platform" / "benchmark-results"
    )
    benchmark_results.mkdir(parents=True)
    mark_owned(benchmark_results, "kube-inference-platform benchmark results v1")
    (benchmark_results / "raw.json").write_text("fixture\n")
    observability_state = (
        cluster_fixture["var_root"] / "lib" / "kube-inference-platform" / "observability"
    )
    observability_state.mkdir(parents=True)
    mark_owned(observability_state, "kube-inference-platform observability storage v1")
    (observability_state / "prometheus.db").write_text("fixture\n")
    cluster_fixture["benchmark_work_dir"].mkdir()
    (cluster_fixture["benchmark_work_dir"] / "trace.json").write_text("fixture\n")
    cluster_fixture["network_state"].touch()

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
        KIP_TEST_GPU_RELEASE="1",
        KIP_TEST_RUNTIME_RELEASE="1",
        KIP_TEST_BENCHMARK_RELEASE="1",
        KIP_TEST_OBSERVABILITY_RELEASE="1",
        KIP_TEST_RUNTIME_MOUNTS="1",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "8 passed" in result.stdout
    assert "ready for a fresh complete v0.1 bootstrap" in result.stdout
    assert "/swapfile none swap sw 0 0" in fstab.read_text()
    assert "KIP_DISABLED_SWAP" not in fstab.read_text()
    assert not (cluster_fixture["etc_root"] / "kubernetes").exists()
    assert not (cluster_fixture["etc_root"] / "containerd").exists()
    assert not (
        cluster_fixture["etc_root"] / "sysctl.d" / "99-kube-inference-platform.conf"
    ).exists()
    assert not (cluster_fixture["target_home"] / ".kube" / "config").exists()
    assert not cni_binary.exists()
    assert not cilium_bpf.exists()
    assert not cilium_global.exists()
    assert not nvidia_state.exists()
    assert not cluster_fixture["network_state"].exists()
    assert not model_cache.exists()
    assert not benchmark_results.exists()
    assert not observability_state.exists()
    assert not cluster_fixture["benchmark_work_dir"].exists()
    assert not (cluster_fixture["var_root"] / "lib" / "kube-inference-platform").exists()
    assert "KUBE-" not in cluster_fixture["firewall_capture"].read_text()
    commands = cluster_fixture["command_log"].read_text()
    assert "delete namespace kip-runtime" in commands
    assert "delete namespace kip-benchmark" in commands
    assert "helm uninstall kip-opencost" in commands
    assert "helm uninstall kip-monitoring" in commands
    assert "delete namespace kip-observability" in commands
    assert "helm uninstall gpu-operator" in commands
    assert "helm uninstall cilium" not in commands
    assert "umount" in commands
    assert "/run/nvidia/driver" in commands
    assert "/run/cilium/cgroupv2" in commands
    assert "ip netns delete cni-fixture" in commands
    assert "iptables-restore" in commands
    assert f"kubeadm reset --force --cri-socket {CRI_ENDPOINT}" in commands
    assert (
        "apt-mark unhold containerd.io kubelet kubeadm kubectl cri-tools kubernetes-cni" in commands
    )
    assert (
        "apt-get purge -y containerd.io kubelet kubeadm kubectl cri-tools kubernetes-cni"
        in commands
    )
    assert commands.index("delete namespace kip-benchmark") < commands.index(
        "helm uninstall kip-opencost"
    )
    assert commands.index("helm uninstall kip-monitoring") < commands.index(
        "delete namespace kip-runtime"
    )
    assert commands.index("delete namespace kip-runtime") < commands.index(
        "helm uninstall gpu-operator"
    )


def test_destroy_is_idempotent(cluster_fixture: dict[str, Path]) -> None:
    first = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)
    assert first.returncode == 0, first.stdout + first.stderr
    destroyed = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
    )
    assert destroyed.returncode == 0, destroyed.stdout + destroyed.stderr

    repeated = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
    )

    assert repeated.returncode == 0
    assert "nothing to remove" in repeated.stdout


def test_destroy_retries_busy_cilium_bpf_cleanup(
    cluster_fixture: dict[str, Path],
) -> None:
    bootstrap = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr
    cilium_bpf = cluster_fixture["bpf_root"] / "cilium" / "endpoints"
    cilium_bpf.mkdir(parents=True)

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
        KIP_TEST_BPF_BUSY_ONCE="1",
        KIP_TEST_EUID="1000",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert cluster_fixture["bpf_busy_state"].exists()
    assert not (cluster_fixture["bpf_root"] / "cilium").exists()
    assert "8 passed" in result.stdout


def test_destroy_resumes_from_private_teardown_marker(
    cluster_fixture: dict[str, Path],
) -> None:
    state_dir = cluster_fixture["var_root"] / "lib" / "kube-inference-platform"
    state_dir.mkdir(parents=True)
    (state_dir / "destroying").touch()
    nvidia_state = cluster_fixture["run_root"] / "nvidia" / "nvidia-driver.state"
    nvidia_state.parent.mkdir(parents=True)
    nvidia_state.write_text("ready\n")

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not state_dir.exists()
    assert not nvidia_state.exists()
    assert "8 passed" in result.stdout


def test_non_root_destroy_uses_sudo_for_protected_state(
    cluster_fixture: dict[str, Path],
) -> None:
    bootstrap = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
        KIP_TEST_EUID="1000",
        KIP_TEST_GPU_RELEASE="1",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    commands = cluster_fixture["command_log"].read_text()
    assert "sudo helm get values gpu-operator" in commands
    assert "sudo cmp -s" in commands
    assert f"sudo kubeadm reset --force --cri-socket {CRI_ENDPOINT}" in commands


def test_destroy_refuses_unmanaged_cluster_state(cluster_fixture: dict[str, Path]) -> None:
    kubernetes_dir = cluster_fixture["etc_root"] / "kubernetes"
    kubernetes_dir.mkdir()
    (kubernetes_dir / "admin.conf").write_text("unmanaged\n")

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
    )

    assert result.returncode == 1
    assert "ownership is unrecognized" in result.stdout
    commands = (
        cluster_fixture["command_log"].read_text()
        if cluster_fixture["command_log"].exists()
        else ""
    )
    assert "kubeadm reset" not in commands


def test_destroy_refuses_unmanaged_gpu_release(cluster_fixture: dict[str, Path]) -> None:
    bootstrap = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
        KIP_TEST_GPU_RELEASE="1",
        KIP_TEST_GPU_DIGEST="sha256:unmanaged",
    )

    assert result.returncode == 1
    assert "release is unmanaged" in result.stdout
    commands = cluster_fixture["command_log"].read_text()
    assert "helm uninstall gpu-operator" not in commands
    assert "kubeadm reset" not in commands


def test_destroy_refuses_value_drift_in_gpu_release(cluster_fixture: dict[str, Path]) -> None:
    bootstrap = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
        KIP_TEST_GPU_RELEASE="1",
        KIP_TEST_GPU_VALUE_DRIFT="1",
    )

    assert result.returncode == 1
    commands = cluster_fixture["command_log"].read_text()
    assert "helm uninstall gpu-operator" not in commands
    assert "kubeadm reset" not in commands


def test_destroy_refuses_unmanaged_runtime(cluster_fixture: dict[str, Path]) -> None:
    bootstrap = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
        KIP_TEST_RUNTIME_RELEASE="1",
        KIP_TEST_RUNTIME_EXTRA="1",
    )

    assert result.returncode == 1
    assert "release is unmanaged" in result.stdout
    commands = cluster_fixture["command_log"].read_text()
    assert "delete namespace kip-runtime" not in commands
    assert "kubeadm reset" not in commands


def test_destroy_refuses_unmanaged_benchmark_namespace(
    cluster_fixture: dict[str, Path],
) -> None:
    bootstrap = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
        KIP_TEST_BENCHMARK_RELEASE="1",
        KIP_TEST_BENCHMARK_OWNER="someone-else",
    )

    assert result.returncode == 1
    assert "release is unmanaged" in result.stdout
    commands = cluster_fixture["command_log"].read_text()
    assert "delete namespace kip-benchmark" not in commands
    assert "kubeadm reset" not in commands


def test_destroy_refuses_foreign_object_in_managed_namespace(
    cluster_fixture: dict[str, Path],
) -> None:
    bootstrap = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
        KIP_TEST_BENCHMARK_RELEASE="1",
        KIP_TEST_BENCHMARK_NAMESPACE_EXTRA="1",
    )

    assert result.returncode == 1
    commands = cluster_fixture["command_log"].read_text()
    assert "delete namespace kip-benchmark" not in commands
    assert "kubeadm reset" not in commands


@pytest.mark.parametrize("directory", ["benchmark-results", "observability", "model-cache"])
def test_destroy_refuses_unmarked_project_host_storage(
    cluster_fixture: dict[str, Path], directory: str
) -> None:
    bootstrap = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr
    storage = cluster_fixture["var_root"] / "lib" / "kube-inference-platform" / directory
    storage.mkdir(parents=True)
    (storage / "foreign-data").write_text("preserve\n")

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
    )

    assert result.returncode == 1
    assert (storage / "foreign-data").read_text() == "preserve\n"
    commands = cluster_fixture["command_log"].read_text()
    assert "kubeadm reset" not in commands


@pytest.mark.parametrize(
    "overrides",
    [
        {"KIP_TEST_OBSERVABILITY_OWNER": "someone-else"},
        {"KIP_TEST_MONITORING_CHART_VERSION": "0.0.0-drifted"},
        {"KIP_TEST_OBSERVABILITY_VOLUME_OWNER": "someone-else"},
    ],
)
def test_destroy_refuses_unmanaged_observability_state(
    cluster_fixture: dict[str, Path], overrides: dict[str, str]
) -> None:
    bootstrap = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
        KIP_TEST_OBSERVABILITY_RELEASE="1",
        **overrides,
    )

    assert result.returncode == 1
    assert "release is unmanaged" in result.stdout
    commands = cluster_fixture["command_log"].read_text()
    assert "helm uninstall kip-opencost" not in commands
    assert "delete namespace kip-observability" not in commands
    assert "kubeadm reset" not in commands


def test_destroy_preflights_every_layer_before_first_deletion(
    cluster_fixture: dict[str, Path],
) -> None:
    bootstrap = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
        KIP_TEST_BENCHMARK_RELEASE="1",
        KIP_TEST_OBSERVABILITY_RELEASE="1",
        KIP_TEST_OBSERVABILITY_OWNER="someone-else",
    )

    assert result.returncode == 1
    commands = cluster_fixture["command_log"].read_text()
    assert "delete namespace kip-benchmark" not in commands
    assert "helm uninstall kip-opencost" not in commands
    assert "kubeadm reset" not in commands


@pytest.mark.parametrize(
    "override",
    ["KIP_TEST_FOREIGN_NAMESPACE", "KIP_TEST_FOREIGN_WORKLOAD", "KIP_TEST_FOREIGN_PV"],
)
def test_destroy_refuses_foreign_cluster_state_before_mutation(
    cluster_fixture: dict[str, Path], override: str
) -> None:
    bootstrap = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
        **{override: "1"},
    )

    assert result.returncode == 1
    commands = cluster_fixture["command_log"].read_text()
    assert "kubeadm reset" not in commands
    assert "delete namespace" not in commands


def test_destroy_refuses_foreign_monitoring_custom_resource_before_crd_deletion(
    cluster_fixture: dict[str, Path],
) -> None:
    bootstrap = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
        KIP_TEST_FOREIGN_MONITORING_CR="1",
    )

    assert result.returncode == 1
    commands = cluster_fixture["command_log"].read_text()
    assert "delete customresourcedefinition" not in commands
    assert "kubeadm reset" not in commands


def test_destroy_handles_unavailable_api_and_removes_runtime_cache(
    cluster_fixture: dict[str, Path],
) -> None:
    bootstrap = run_cluster_script(BOOTSTRAP_SCRIPT, cluster_fixture)
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr
    model_cache = (
        cluster_fixture["var_root"] / "lib" / "kube-inference-platform" / "model-cache" / "vllm"
    )
    model_cache.mkdir(parents=True)
    mark_owned(model_cache.parent, "kube-inference-platform model cache v1")
    (model_cache / "model.bin").write_text("fixture\n")
    benchmark_results = (
        cluster_fixture["var_root"] / "lib" / "kube-inference-platform" / "benchmark-results"
    )
    benchmark_results.mkdir(parents=True)
    mark_owned(benchmark_results, "kube-inference-platform benchmark results v1")
    (benchmark_results / "raw.json").write_text("fixture\n")
    observability_state = (
        cluster_fixture["var_root"] / "lib" / "kube-inference-platform" / "observability"
    )
    observability_state.mkdir(parents=True)
    mark_owned(observability_state, "kube-inference-platform observability storage v1")
    (observability_state / "prometheus.db").write_text("fixture\n")
    cluster_fixture["benchmark_work_dir"].mkdir()
    (cluster_fixture["benchmark_work_dir"] / "trace.json").write_text("fixture\n")

    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY="1",
        KIP_TEST_API_FAIL="1",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "API is unavailable" in result.stdout
    assert not model_cache.exists()
    assert not benchmark_results.exists()
    assert not observability_state.exists()
    assert not cluster_fixture["benchmark_work_dir"].exists()


@pytest.mark.parametrize("confirmation", ["", "yes", "0"])
def test_destroy_requires_explicit_noninteractive_confirmation(
    cluster_fixture: dict[str, Path], confirmation: str
) -> None:
    result = run_cluster_script(
        DESTROY_SCRIPT,
        cluster_fixture,
        KIP_CONFIRM_DESTROY=confirmation,
    )

    assert result.returncode == 2
    assert "destroy" in result.stderr.lower()
