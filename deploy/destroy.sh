#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
KIP_DEPLOY_ROOT=$SCRIPT_DIR
readonly KIP_DEPLOY_ROOT
KIP_CLUSTER_ROOT="$KIP_DEPLOY_ROOT/cluster"
readonly KIP_CLUSTER_ROOT
KIP_GPU_ROOT="$KIP_DEPLOY_ROOT/gpu"
readonly KIP_GPU_ROOT
KIP_RUNTIME_ROOT="$KIP_DEPLOY_ROOT/runtime/vllm"
readonly KIP_RUNTIME_ROOT

# shellcheck source=deploy/lib/ui.sh
source "$KIP_DEPLOY_ROOT/lib/ui.sh"
# shellcheck source=deploy/cluster/versions.env
source "$KIP_CLUSTER_ROOT/versions.env"
# shellcheck source=deploy/gpu/versions.env
source "$KIP_GPU_ROOT/versions.env"
# shellcheck source=deploy/runtime/vllm/versions.env
source "$KIP_RUNTIME_ROOT/versions.env"
# shellcheck source=deploy/cluster/lib/common.sh
source "$KIP_CLUSTER_ROOT/lib/common.sh"
# shellcheck source=deploy/cluster/lib/preflight.sh
source "$KIP_CLUSTER_ROOT/lib/preflight.sh"
# shellcheck source=deploy/cluster/lib/kubernetes.sh
source "$KIP_CLUSTER_ROOT/lib/kubernetes.sh"

export LC_ALL=C
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_CONFIRM_DESTROY=${KIP_CONFIRM_DESTROY:-}
KIP_KUBECONFIG=${KIP_KUBECONFIG:-}
KIP_OPT_ROOT=${KIP_OPT_ROOT:-/opt}
KIP_SYS_FS_BPF_ROOT=${KIP_SYS_FS_BPF_ROOT:-/sys/fs/bpf}
KIP_DESTROY_STATE_DIR="$KIP_VAR_ROOT/lib/kube-inference-platform"
KIP_DESTROY_STATE_FILE="$KIP_DESTROY_STATE_DIR/destroying"
KIP_RUNTIME_NAMESPACE="kip-runtime"
KIP_RUNTIME_RESOURCE_NAME="vllm-qwen25-7b"
KIP_RUNTIME_PROFILE_FILE="$KIP_RUNTIME_ROOT/profiles/qwen2.5-7b-instruct.json"

kip_ui_init
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
[ "$#" -eq 0 ] || kip_usage_error "destroy does not accept positional arguments."

kip_confirm_destroy() {
    local answer

    case "$KIP_CONFIRM_DESTROY" in
        1) return 0 ;;
        "") ;;
        *) kip_usage_error "CONFIRM_DESTROY must be 1 when provided; received '$KIP_CONFIRM_DESTROY'." ;;
    esac
    if [ ! -t 0 ]; then
        kip_usage_error "Non-interactive destruction requires CONFIRM_DESTROY=1."
    fi
    printf '%s' "Type destroy to remove the KIP-managed GPU and Kubernetes stack: "
    IFS= read -r answer
    [ "$answer" = destroy ] || kip_usage_error "Destruction was not confirmed."
}

kip_destroy_has_owned_markers() {
    [ -r "$KIP_ETC_ROOT/modules-load.d/kube-inference-platform.conf" ] \
        && [ -r "$KIP_ETC_ROOT/sysctl.d/99-kube-inference-platform.conf" ] \
        && command grep -Fq 'Managed by Kube Inference Platform' \
            "$KIP_ETC_ROOT/sysctl.d/99-kube-inference-platform.conf"
}

kip_destroy_has_cluster_state() {
    [ -e "$KIP_ETC_ROOT/kubernetes/admin.conf" ] \
        || [ -e "$KIP_VAR_ROOT/lib/etcd" ] \
        || [ -e "$KIP_VAR_ROOT/lib/kubelet/config.yaml" ] \
        || [ -e "$KIP_ETC_ROOT/containerd/config.toml" ] \
        || kip_package_installed kubeadm \
        || kip_package_installed containerd.io
}

kip_destroy_has_runtime_residue() {
    local bpf_entries links namespaces rules

    [ -e "$KIP_OPT_ROOT/cni/bin/cilium-cni" ] && return 0
    [ -e "$KIP_RUN_ROOT/cilium" ] && return 0
    [ -e "$KIP_RUN_ROOT/nvidia" ] && return 0
    [ -e "$KIP_DESTROY_STATE_DIR/model-cache" ] && return 0
    [ -e "$KIP_SYS_FS_BPF_ROOT/cilium" ] && return 0
    if [ "$KIP_USE_SUDO" = "1" ]; then
        bpf_entries=$(kip_run_privileged find "$KIP_SYS_FS_BPF_ROOT/tc/globals" \
            -maxdepth 1 -name 'cilium*' -print -quit 2>/dev/null || true)
    else
        bpf_entries=$(kip_exec find "$KIP_SYS_FS_BPF_ROOT/tc/globals" \
            -maxdepth 1 -name 'cilium*' -print -quit 2>/dev/null || true)
    fi
    if [ -n "$bpf_entries" ]; then
        return 0
    fi
    namespaces=$(kip_exec ip netns list 2>/dev/null || true)
    printf '%s\n' "$namespaces" | command grep -Eq '^cni-' && return 0
    links=$(kip_exec ip -o link show 2>/dev/null || true)
    printf '%s\n' "$links" | command awk -F': ' '
        $2 ~ /^lxc[[:xdigit:]]+(@|$)/ {found=1}
        END {exit !found}
    ' && return 0
    if kip_has_command iptables-save; then
        if [ "$KIP_USE_SUDO" = "1" ]; then
            rules=$(kip_run_privileged iptables-save 2>/dev/null || true)
        else
            rules=$(kip_exec iptables-save 2>/dev/null || true)
        fi
        printf '%s\n' "$rules" | command grep -Eq 'CILIUM_|KUBE-' && return 0
    fi
    return 1
}

kip_destroy_packages_match() {
    local expected_containerd package_name version

    kip_cluster_detect_os || return 1
    expected_containerd=$(kip_containerd_package_version) || return 1
    for package_name in containerd.io kubelet kubeadm kubectl; do
        if kip_package_installed "$package_name"; then
            version=$(kip_package_version "$package_name") || return 1
            case "$package_name" in
                containerd.io) [ "$version" = "$expected_containerd" ] || return 1 ;;
                *) [ "$version" = "$KIP_KUBERNETES_PACKAGE_VERSION" ] || return 1 ;;
            esac
        fi
    done
}

kip_destroy_api_ready() {
    [ -e "$KIP_ETC_ROOT/kubernetes/admin.conf" ] || return 1
    kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        get --raw=/readyz >/dev/null 2>&1
}

kip_destroy_helm_release_exists() {
    local release=$1 namespace=$2

    kip_run_privileged helm status "$release" --namespace "$namespace" \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" >/dev/null 2>&1
}

kip_destroy_gpu_release_is_owned() {
    local values

    values=$(kip_run_privileged helm get values gpu-operator \
        --namespace gpu-operator --output json \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" 2>/dev/null) || return 1
    printf '%s' "$values" \
        | command grep -Eq \
            "\"chartDigest\"[[:space:]]*:[[:space:]]*\"$KIP_GPU_OPERATOR_CHART_DIGEST\""
}

kip_destroy_runtime_exists() {
    local name

    name=$(kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        get namespace "$KIP_RUNTIME_NAMESPACE" \
        -o 'jsonpath={.metadata.name}' 2>/dev/null) || return 1
    [ "$name" = "$KIP_RUNTIME_NAMESPACE" ]
}

kip_destroy_runtime_profile_hash() {
    kip_exec jq -cS . "$KIP_RUNTIME_PROFILE_FILE" \
        | kip_exec sha256sum \
        | command awk '{print $1}'
}

kip_destroy_runtime_resource_exists() {
    local kind=$1 name

    name=$(kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        -n "$KIP_RUNTIME_NAMESPACE" get "$kind" "$KIP_RUNTIME_RESOURCE_NAME" \
        -o 'jsonpath={.metadata.name}' 2>/dev/null) || return 1
    [ "$name" = "$KIP_RUNTIME_RESOURCE_NAME" ]
}

kip_destroy_runtime_is_owned() {
    local digest kind managed model_revision profile_hash recorded_hash resources

    managed=$(kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        get namespace "$KIP_RUNTIME_NAMESPACE" \
        -o 'jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}' \
        2>/dev/null) || return 1
    [ "$managed" = kube-inference-platform ] || return 1
    profile_hash=$(kip_destroy_runtime_profile_hash) || return 1
    recorded_hash=$(kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        get namespace "$KIP_RUNTIME_NAMESPACE" \
        -o 'jsonpath={.metadata.annotations.kube-inference-platform/profile-sha256}' \
        2>/dev/null) || return 1
    [ "$recorded_hash" = "$profile_hash" ] || return 1

    resources=$(kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        -n "$KIP_RUNTIME_NAMESPACE" get deployment,service -o json 2>/dev/null) || return 1
    printf '%s' "$resources" | kip_exec jq -e --arg name "$KIP_RUNTIME_RESOURCE_NAME" '
        all(.items[]?;
            (.kind == "Deployment" or .kind == "Service")
            and .metadata.name == $name
            and .metadata.labels["app.kubernetes.io/managed-by"]
                == "kube-inference-platform")
    ' >/dev/null || return 1

    model_revision=$(kip_exec jq -r '.model.revision' "$KIP_RUNTIME_PROFILE_FILE") || return 1
    for kind in deployment service; do
        kip_destroy_runtime_resource_exists "$kind" || continue
        managed=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            -n "$KIP_RUNTIME_NAMESPACE" get "$kind" "$KIP_RUNTIME_RESOURCE_NAME" \
            -o 'jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}' \
            2>/dev/null) || return 1
        digest=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            -n "$KIP_RUNTIME_NAMESPACE" get "$kind" "$KIP_RUNTIME_RESOURCE_NAME" \
            -o 'jsonpath={.metadata.annotations.kube-inference-platform/image-digest}' \
            2>/dev/null) || return 1
        recorded_hash=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            -n "$KIP_RUNTIME_NAMESPACE" get "$kind" "$KIP_RUNTIME_RESOURCE_NAME" \
            -o 'jsonpath={.metadata.annotations.kube-inference-platform/profile-sha256}' \
            2>/dev/null) || return 1
        [ "$managed" = kube-inference-platform ] \
            && [ "$digest" = "$KIP_VLLM_IMAGE_DIGEST" ] \
            && [ "$recorded_hash" = "$profile_hash" ] || return 1
        [ "$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            -n "$KIP_RUNTIME_NAMESPACE" get "$kind" "$KIP_RUNTIME_RESOURCE_NAME" \
            -o 'jsonpath={.metadata.annotations.kube-inference-platform/model-revision}' \
            2>/dev/null)" = "$model_revision" ] || return 1
    done
}

kip_destroy_cluster_releases() {
    local output

    if ! kip_destroy_api_ready; then
        kip_note "Kubernetes API is unavailable; kubeadm reset will remove local cluster state."
        return 0
    fi
    if kip_destroy_runtime_exists; then
        kip_destroy_runtime_is_owned || return 1
        kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            delete namespace "$KIP_RUNTIME_NAMESPACE" --wait=true --timeout=10m >/dev/null \
            || return 1
    fi
    if kip_destroy_helm_release_exists gpu-operator gpu-operator; then
        kip_destroy_gpu_release_is_owned || return 1
        output=$(kip_run_privileged helm uninstall gpu-operator \
            --namespace gpu-operator --wait --timeout 10m \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" 2>&1) || return 1
        kip_verbose_output "helm uninstall gpu-operator" "$output"
        kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            delete namespace gpu-operator --ignore-not-found=true \
            --wait=true --timeout=5m >/dev/null
    fi
}

kip_destroy_remove_user_kubeconfig() {
    local source_config target_home target_user user_config

    source_config="$KIP_ETC_ROOT/kubernetes/admin.conf"
    [ -e "$source_config" ] || return 0
    target_user=${KIP_TARGET_USER:-}
    if [ -z "$target_user" ]; then
        if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
            target_user=$SUDO_USER
        elif [ "$(kip_effective_uid)" -ne 0 ]; then
            target_user=$(kip_exec id -un) || return 1
        else
            target_user=root
        fi
    fi
    if [ -n "${KIP_TARGET_HOME:-}" ]; then
        target_home=$KIP_TARGET_HOME
    else
        target_home=$(kip_exec getent passwd "$target_user" 2>/dev/null | command cut -d: -f6)
    fi
    [ -n "$target_home" ] || return 1
    user_config="$target_home/.kube/config"
    if [ -r "$user_config" ] && kip_run_privileged cmp -s "$source_config" "$user_config"; then
        kip_run_privileged rm -f "$user_config"
    elif [ -e "$user_config" ]; then
        kip_note "Preserving $user_config because it no longer matches the KIP admin kubeconfig."
    fi
}

kip_destroy_reset_kubernetes() {
    kip_destroy_remove_user_kubeconfig || return 1
    if kip_has_command kubeadm; then
        kip_run_privileged kubeadm reset --force --cri-socket "$KIP_CRI_SOCKET"
    fi
    kip_run_privileged systemctl stop kubelet >/dev/null 2>&1 || true
    kip_run_privileged systemctl disable kubelet >/dev/null 2>&1 || true
    kip_run_privileged systemctl stop containerd >/dev/null 2>&1 || true
    kip_run_privileged systemctl disable containerd >/dev/null 2>&1 || true
}

kip_destroy_network_filtering() {
    local cleaned current family

    for family in iptables ip6tables; do
        kip_has_command "$family-save" || continue
        kip_has_command "$family-restore" || continue
        current=$(command mktemp "${TMPDIR:-/tmp}/kip-${family}.current.XXXXXX")
        cleaned=$(command mktemp "${TMPDIR:-/tmp}/kip-${family}.clean.XXXXXX")
        if ! kip_run_privileged "$family-save" > "$current"; then
            command rm -f "$current" "$cleaned"
            return 1
        fi
        command grep -Ev 'CILIUM_|KUBE-' "$current" > "$cleaned" || true
        if ! kip_run_privileged "$family-restore" < "$cleaned"; then
            command rm -f "$current" "$cleaned"
            return 1
        fi
        command rm -f "$current" "$cleaned"
    done
}

kip_destroy_remove_bpf_state() {
    local attempt=0 pinned

    while [ "$attempt" -lt 30 ]; do
        kip_run_privileged rm -rf "$KIP_SYS_FS_BPF_ROOT/cilium" >/dev/null 2>&1 || true
        if kip_run_privileged test -d "$KIP_SYS_FS_BPF_ROOT/tc/globals"; then
            kip_run_privileged find "$KIP_SYS_FS_BPF_ROOT/tc/globals" \
                -maxdepth 1 -name 'cilium*' -delete >/dev/null 2>&1 || true
        fi
        pinned=$(kip_run_privileged find "$KIP_SYS_FS_BPF_ROOT/tc/globals" \
            -maxdepth 1 -name 'cilium*' -print -quit 2>/dev/null || true)
        if ! kip_run_privileged test -e "$KIP_SYS_FS_BPF_ROOT/cilium" \
            && [ -z "$pinned" ]; then
            return 0
        fi
        attempt=$((attempt + 1))
        if [ "$attempt" -lt 30 ] && [ "${KIP_TESTING:-0}" != 1 ]; then
            command sleep 2
        fi
    done
    return 1
}

kip_destroy_network_and_state() {
    local interface interfaces mount_path namespace namespaces

    for mount_path in "$KIP_RUN_ROOT/nvidia/driver" "$KIP_RUN_ROOT/cilium/cgroupv2"; do
        if kip_exec mountpoint -q "$mount_path"; then
            kip_run_privileged umount "$mount_path" || return 1
        fi
    done
    for interface in cilium_host cilium_net cilium_vxlan; do
        if kip_exec ip link show "$interface" >/dev/null 2>&1; then
            kip_run_privileged ip link delete "$interface" >/dev/null 2>&1 || return 1
        fi
    done
    namespaces=$(kip_exec ip netns list 2>/dev/null \
        | command awk '$1 ~ /^cni-/ {print $1}' || true)
    while IFS= read -r namespace; do
        [ -n "$namespace" ] || continue
        kip_run_privileged ip netns delete "$namespace" || return 1
    done <<EOF
$namespaces
EOF
    interfaces=$(kip_exec ip -o link show 2>/dev/null \
        | command awk -F': ' '
            $2 ~ /^lxc[[:xdigit:]]+(@|$)/ {
                sub(/@.*/, "", $2)
                print $2
            }
        ' || true)
    while IFS= read -r interface; do
        [ -n "$interface" ] || continue
        kip_run_privileged ip link delete "$interface" >/dev/null 2>&1 || return 1
    done <<EOF
$interfaces
EOF
    kip_destroy_remove_bpf_state || return 1
    kip_destroy_network_filtering || return 1
    kip_run_privileged rm -rf \
        "$KIP_ETC_ROOT/cni/net.d" \
        "$KIP_ETC_ROOT/kubernetes" \
        "$KIP_RUN_ROOT/cilium" \
        "$KIP_RUN_ROOT/containerd" \
        "$KIP_RUN_ROOT/nvidia" \
        "$KIP_VAR_ROOT/lib/cilium" \
        "$KIP_VAR_ROOT/lib/cni" \
        "$KIP_VAR_ROOT/lib/containerd" \
        "$KIP_VAR_ROOT/lib/etcd" \
        "$KIP_VAR_ROOT/lib/kubelet" \
        "$KIP_DESTROY_STATE_DIR/model-cache" \
        "$KIP_USR_LOCAL_BIN/../nvidia/toolkit"
    kip_run_privileged rm -f "$KIP_OPT_ROOT/cni/bin/cilium-cni"
}

kip_destroy_packages_and_configuration() {
    local package_name packages=()

    for package_name in containerd.io kubelet kubeadm kubectl cri-tools kubernetes-cni; do
        if kip_package_installed "$package_name"; then
            packages+=("$package_name")
        fi
    done
    if [ "${#packages[@]}" -gt 0 ]; then
        kip_run_privileged apt-mark unhold "${packages[@]}" >/dev/null 2>&1 || true
        kip_run_privileged env DEBIAN_FRONTEND=noninteractive \
            apt-get purge -y "${packages[@]}"
    fi
    kip_run_privileged rm -rf "$KIP_ETC_ROOT/containerd"
    kip_run_privileged rm -f \
        "$KIP_ETC_ROOT/crictl.yaml" \
        "$KIP_ETC_ROOT/apt/sources.list.d/docker.sources" \
        "$KIP_ETC_ROOT/apt/sources.list.d/kubernetes.list" \
        "$KIP_ETC_ROOT/apt/keyrings/kubernetes-apt-keyring.gpg"
    kip_run_privileged systemctl daemon-reload >/dev/null 2>&1 || true
}

kip_destroy_restore_host() {
    local backup="$KIP_ETC_ROOT/fstab.kip-pre-bootstrap"
    local fstab="$KIP_ETC_ROOT/fstab"
    local restored

    if [ -r "$backup" ]; then
        restored=$(command mktemp "${TMPDIR:-/tmp}/kip-fstab.XXXXXX")
        command cp "$backup" "$restored"
        kip_install_file "$restored" "$fstab"
        command rm -f "$restored"
        kip_run_privileged rm -f "$backup"
    elif [ -r "$fstab" ]; then
        restored=$(command mktemp "${TMPDIR:-/tmp}/kip-fstab.XXXXXX")
        command sed 's/^# KIP_DISABLED_SWAP //' "$fstab" > "$restored"
        kip_install_file "$restored" "$fstab"
        command rm -f "$restored"
    fi
    kip_run_privileged rm -f \
        "$KIP_ETC_ROOT/modules-load.d/kube-inference-platform.conf" \
        "$KIP_ETC_ROOT/sysctl.d/99-kube-inference-platform.conf"
    kip_run_privileged sysctl --system >/dev/null 2>&1 || true
    kip_run_privileged swapon -a >/dev/null 2>&1 || true
}

kip_confirm_destroy
trap 'kip_unexpected_error "$?" "$LINENO"' ERR

kip_logo
kip_heading "Platform Teardown · v0.1"
kip_reset_results 8

if ! kip_destroy_has_cluster_state \
    && [ ! -e "$KIP_DESTROY_STATE_FILE" ] \
    && ! kip_destroy_has_runtime_residue; then
    kip_record SKIP "Managed installation" "No KIP cluster state is present; nothing to remove."
    kip_summary "Platform teardown"
    exit 0
fi

if { kip_destroy_has_owned_markers || [ -e "$KIP_DESTROY_STATE_FILE" ]; } \
    && kip_destroy_packages_match; then
    kip_record PASS "Managed installation" "KIP ownership markers and pinned package versions match."
else
    kip_record FAIL "Managed installation" \
        "Cluster ownership is unrecognized or package versions have drifted; refusing teardown."
    kip_summary "Platform teardown"
    exit 1
fi

if kip_acquire_privileges; then
    kip_record PASS "Administrative access" "Root or sudo access acquired."
else
    kip_record FAIL "Administrative access" "Root or functional sudo access is required."
    kip_summary "Platform teardown"
    exit 1
fi

if kip_make_directory "$KIP_DESTROY_STATE_DIR" 0755 \
    && kip_run_privileged touch "$KIP_DESTROY_STATE_FILE"; then
    :
else
    kip_record FAIL "Teardown state" "Unable to record recoverable teardown state."
    kip_summary "Platform teardown"
    exit 1
fi

if kip_destroy_cluster_releases; then
    kip_record PASS "Cluster releases" \
        "KIP-managed runtime and GPU Operator removed; Cilium retained through kubeadm reset."
else
    kip_record FAIL "Cluster releases" \
        "A runtime or release is unmanaged or could not be removed safely."
    kip_summary "Platform teardown"
    exit 1
fi

if kip_destroy_reset_kubernetes; then
    kip_record PASS "Kubernetes reset" "kubeadm state reset and cluster services stopped."
else
    kip_record FAIL "Kubernetes reset" "kubeadm reset or kubeconfig cleanup failed."
    kip_summary "Platform teardown"
    exit 1
fi

if kip_destroy_network_and_state; then
    kip_record PASS "Runtime state" "KIP CNI, Kubernetes, containerd, and GPU runtime state removed."
else
    kip_record FAIL "Runtime state" "Failed to remove project-owned runtime state."
    kip_summary "Platform teardown"
    exit 1
fi

if kip_destroy_packages_and_configuration; then
    kip_record PASS "Cluster packages" "Pinned Kubernetes and containerd packages and configuration removed."
else
    kip_record FAIL "Cluster packages" "Package or configuration removal failed."
    kip_summary "Platform teardown"
    exit 1
fi

if kip_destroy_restore_host; then
    kip_record PASS "Host restoration" "Original fstab restored; KIP sysctl and module files removed."
else
    kip_record FAIL "Host restoration" "Failed to restore host configuration."
    kip_summary "Platform teardown"
    exit 1
fi

if ! kip_destroy_has_cluster_state \
    && ! kip_destroy_has_runtime_residue \
    && [ ! -e "$KIP_ETC_ROOT/sysctl.d/99-kube-inference-platform.conf" ]; then
    kip_run_privileged rm -rf "$KIP_DESTROY_STATE_DIR"
    kip_record PASS "Final verification" "The host is ready for a fresh host-to-runtime bootstrap."
else
    kip_record FAIL "Final verification" "KIP-managed cluster state remains on the host."
    kip_summary "Platform teardown"
    exit 1
fi

kip_summary "Platform teardown"
printf '\n%sReady for a fresh run:%s make host-setup, make cluster-bootstrap, make gpu-install, then make runtime-deploy.\n' \
    "$KIP_COLOR_GREEN" "$KIP_COLOR_RESET"
