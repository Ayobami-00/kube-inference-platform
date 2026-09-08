#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
KIP_CLUSTER_ROOT=$SCRIPT_DIR
readonly KIP_CLUSTER_ROOT
KIP_DEPLOY_ROOT=$(cd "$KIP_CLUSTER_ROOT/.." && pwd)
readonly KIP_DEPLOY_ROOT

# shellcheck source=deploy/lib/ui.sh
source "$KIP_DEPLOY_ROOT/lib/ui.sh"
# shellcheck source=deploy/cluster/versions.env
source "$KIP_CLUSTER_ROOT/versions.env"
# shellcheck source=deploy/cluster/lib/common.sh
source "$KIP_CLUSTER_ROOT/lib/common.sh"
# shellcheck source=deploy/cluster/lib/preflight.sh
source "$KIP_CLUSTER_ROOT/lib/preflight.sh"
# shellcheck source=deploy/cluster/lib/runtime.sh
source "$KIP_CLUSTER_ROOT/lib/runtime.sh"
# shellcheck source=deploy/cluster/lib/kubernetes.sh
source "$KIP_CLUSTER_ROOT/lib/kubernetes.sh"
# shellcheck source=deploy/cluster/lib/cilium.sh
source "$KIP_CLUSTER_ROOT/lib/cilium.sh"

export LC_ALL=C
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_NODE_IP=${KIP_NODE_IP:-}
KIP_POD_CIDR=${KIP_POD_CIDR:-$KIP_DEFAULT_POD_CIDR}
KIP_SERVICE_CIDR=${KIP_SERVICE_CIDR:-$KIP_DEFAULT_SERVICE_CIDR}
KIP_WORK_DIR=""

kip_ui_init
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
[ "$#" -eq 0 ] || kip_usage_error "cluster-bootstrap does not accept positional arguments."
kip_cidr_is_network "$KIP_POD_CIDR" \
    || kip_usage_error "POD_CIDR must be a canonical IPv4 network; received '$KIP_POD_CIDR'."
kip_cidr_is_network "$KIP_SERVICE_CIDR" \
    || kip_usage_error "SERVICE_CIDR must be a canonical IPv4 network; received '$KIP_SERVICE_CIDR'."
if kip_cidrs_overlap "$KIP_POD_CIDR" "$KIP_SERVICE_CIDR"; then
    kip_usage_error "POD_CIDR and SERVICE_CIDR must not overlap."
fi

KIP_WORK_DIR=$(command mktemp -d "${TMPDIR:-/tmp}/kip-cluster.XXXXXX")
readonly KIP_WORK_DIR
kip_cleanup() {
    command rm -rf "$KIP_WORK_DIR"
}
trap kip_cleanup EXIT
trap 'kip_unexpected_error "$?" "$LINENO"' ERR

kip_logo
kip_heading "Single-Node Kubernetes Bootstrap · v0.1"
kip_reset_results 12

if kip_cluster_supported_platform && kip_kernel_supported && kip_hostname_valid; then
    kip_record PASS "Supported platform" \
        "${KIP_OS_NAME:-Ubuntu $KIP_OS_VERSION}; kernel >= 5.10; $KIP_NODE_NAME"
else
    kip_record FAIL "Supported platform" \
        "Ubuntu 22.04/24.04 x86_64, kernel >= 5.10, and a valid hostname are required."
    kip_summary "Cluster bootstrap"
    exit 1
fi

if KIP_NO_LOGO=1 KIP_GPU_REQUIRED=0 "$KIP_DEPLOY_ROOT/host/validate.sh"; then
    kip_record PASS "Host validation" "Required host checks passed; GPU remains deferred."
else
    kip_record FAIL "Host validation" "Run make host-setup and resolve required host failures."
    kip_summary "Cluster bootstrap"
    exit 1
fi

if kip_acquire_privileges; then
    if [ "$KIP_USE_SUDO" = "1" ]; then
        kip_record PASS "Administrative access" "sudo credentials acquired"
    else
        kip_record PASS "Administrative access" "Running as root"
    fi
else
    kip_record FAIL "Administrative access" "Root or functional sudo access is required."
    kip_summary "Cluster bootstrap"
    exit 1
fi

if [ -n "$KIP_NODE_IP" ]; then
    kip_node_ip_is_usable "$KIP_NODE_IP" \
        || kip_usage_error "NODE_IP must be a valid IPv4 address; received '$KIP_NODE_IP'."
    kip_node_ip_is_assigned "$KIP_NODE_IP" \
        || kip_usage_error "NODE_IP '$KIP_NODE_IP' is not assigned to this host."
fi
if kip_resolve_node_ip; then
    kip_record PASS "Node network identity" "$KIP_NODE_NAME at $KIP_NODE_IP_RESOLVED"
else
    kip_record FAIL "Node network identity" \
        "A single IPv4 default route is required; set NODE_IP when route selection is ambiguous."
    kip_summary "Cluster bootstrap"
    exit 1
fi

cluster_state=$(kip_existing_cluster_state)
readonly cluster_state
preflight_error=""
if ! kip_dns_and_time_ready; then
    preflight_error="DNS resolution and synchronized system time are required."
elif ! kip_networks_valid; then
    preflight_error="Pod and Service CIDRs conflict with a host address or route."
elif ! kip_swap_is_supported; then
    preflight_error="Active swap is not represented safely in /etc/fstab."
elif [ "$cluster_state" = partial ]; then
    preflight_error="Partial or inaccessible kubeadm state requires manual recovery."
elif ! kip_check_runtime_conflicts; then
    preflight_error="An unrelated runtime, runtime configuration, or version is present."
elif ! kip_kubernetes_packages_compatible; then
    preflight_error="Installed Kubernetes tools differ from the pinned package version."
elif [ "$cluster_state" = healthy ] && ! kip_existing_cluster_matches; then
    preflight_error="The existing cluster version, CIDRs, or node identity has drifted."
fi
if [ -z "$preflight_error" ]; then
    kip_record PASS "Bootstrap preflight" \
        "$KIP_POD_CIDR pods; $KIP_SERVICE_CIDR services; cluster state $cluster_state"
else
    kip_record FAIL "Bootstrap preflight" "$preflight_error"
    kip_summary "Cluster bootstrap"
    exit 1
fi

if kip_prepare_kernel && kip_disable_swap; then
    kip_record PASS "Kernel and swap" "Networking prerequisites active; swap disabled persistently."
else
    kip_record FAIL "Kernel and swap" "Failed to prepare kernel networking or disable swap."
    kip_summary "Cluster bootstrap"
    exit 1
fi

if kip_install_cluster_packages && kip_install_helm && kip_install_cilium_cli; then
    kip_record PASS "Pinned cluster tooling" \
        "Kubernetes $KIP_KUBERNETES_VERSION; containerd $KIP_CONTAINERD_VERSION; Helm $KIP_HELM_VERSION"
else
    kip_record FAIL "Pinned cluster tooling" "Package or verified binary installation failed."
    kip_summary "Cluster bootstrap"
    exit 1
fi

if kip_configure_containerd && kip_verify_containerd; then
    kip_record PASS "containerd runtime" "CRI v1 is ready with systemd cgroups."
else
    kip_record FAIL "containerd runtime" "containerd configuration or CRI validation failed."
    kip_summary "Cluster bootstrap"
    exit 1
fi

if kip_initialize_kubernetes "$cluster_state" && kip_configure_user_kubeconfig; then
    kip_record PASS "Kubernetes control plane" \
        "Cluster $cluster_state path completed with Kubernetes $KIP_KUBERNETES_VERSION."
else
    kip_record FAIL "Kubernetes control plane" \
        "Initialization failed or the existing cluster differs from the pinned configuration."
    kip_summary "Cluster bootstrap"
    exit 1
fi

if kip_install_cilium; then
    kip_record PASS "Cilium networking" \
        "Cilium $KIP_CILIUM_VERSION is healthy with Kubernetes IPAM and VXLAN."
else
    kip_record FAIL "Cilium networking" "Cilium installation or rollout failed."
    kip_summary "Cluster bootstrap"
    exit 1
fi

if kip_make_single_node_schedulable; then
    kip_record PASS "Single-node scheduling" "The control-plane NoSchedule taint is absent."
else
    kip_record FAIL "Single-node scheduling" "Unable to make the control-plane node schedulable."
    kip_summary "Cluster bootstrap"
    exit 1
fi

if kip_wait_for_cluster \
    && kip_component_ready component=kube-apiserver \
    && kip_component_ready component=etcd \
    && kip_component_ready component=kube-scheduler \
    && kip_component_ready component=kube-controller-manager \
    && kip_component_ready k8s-app=kube-proxy \
    && kip_component_ready k8s-app=cilium \
    && kip_component_ready io.cilium/app=operator \
    && kip_component_ready k8s-app=kube-dns \
    && kip_all_system_pods_healthy \
    && kip_run_dns_service_smoke \
    && kip_run_cilium_connectivity; then
    kip_record PASS "Cluster health" "Node, core pods, DNS, Services, and Cilium connectivity passed."
else
    kip_record FAIL "Cluster health" "One or more readiness or connectivity gates failed."
    kip_summary "Cluster bootstrap"
    exit 1
fi

kip_summary "Cluster bootstrap"
printf '\n%sNext milestone:%s install and validate the NVIDIA GPU Operator.\n' \
    "$KIP_COLOR_CYAN" "$KIP_COLOR_RESET"
