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
KIP_POD_CIDR=${KIP_POD_CIDR:-$KIP_DEFAULT_POD_CIDR}
KIP_SERVICE_CIDR=${KIP_SERVICE_CIDR:-$KIP_DEFAULT_SERVICE_CIDR}

kip_ui_init
trap 'kip_unexpected_error "$?" "$LINENO"' ERR
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
[ "$#" -eq 0 ] || kip_usage_error "cluster-check does not accept positional arguments."

kip_logo
kip_heading "Single-Node Kubernetes Validation · v0.1"
kip_reset_results 11

if kip_cluster_supported_platform; then
    kip_record PASS "Supported platform" "${KIP_OS_NAME:-Ubuntu $KIP_OS_VERSION}; x86_64"
else
    kip_record FAIL "Supported platform" "Ubuntu 22.04/24.04 x86_64 is required."
fi

if kip_cluster_versions_match; then
    kip_record PASS "Pinned versions" \
        "Kubernetes $KIP_KUBERNETES_VERSION and containerd $KIP_CONTAINERD_VERSION"
else
    kip_record FAIL "Pinned versions" "Installed Kubernetes or containerd version differs from v0.1."
fi

if kip_exec systemctl is-active --quiet containerd \
    && kip_exec systemctl is-active --quiet kubelet; then
    kip_record PASS "Node services" "containerd and kubelet are active."
else
    kip_record FAIL "Node services" "containerd and kubelet must both be active."
fi

if kip_acquire_privileges && kip_verify_containerd; then
    kip_record PASS "Container runtime" "containerd CRI v1 is ready."
else
    kip_record FAIL "Container runtime" "containerd socket or CRI readiness failed."
fi

if api_output=$(kip_kubectl get --raw=/readyz 2>&1); then
    kip_record PASS "Kubernetes API" "API readiness endpoint passed."
    kip_verbose_output "kubectl get --raw=/readyz" "$api_output"
else
    kip_record FAIL "Kubernetes API" "API readiness endpoint failed."
fi

if kip_nodes_ready_and_schedulable; then
    kip_record PASS "Node readiness" "All nodes are Ready and schedulable."
else
    kip_record FAIL "Node readiness" "A node is absent, NotReady, or unschedulable."
fi

if kip_component_ready component=kube-apiserver \
    && kip_component_ready component=etcd \
    && kip_component_ready component=kube-scheduler \
    && kip_component_ready component=kube-controller-manager; then
    kip_record PASS "Control plane" "API server, etcd, scheduler, and controller manager are ready."
else
    kip_record FAIL "Control plane" "One or more control-plane components are not ready."
fi

if kip_component_ready k8s-app=kube-proxy; then
    kip_record PASS "kube-proxy" "Service proxy pod is ready."
else
    kip_record FAIL "kube-proxy" "Service proxy pod is not ready."
fi

if kip_component_ready k8s-app=cilium \
    && kip_component_ready io.cilium/app=operator \
    && kip_cilium_cli status --wait --wait-duration 30s >/dev/null; then
    kip_record PASS "Cilium" "Agent and operator are ready."
else
    kip_record FAIL "Cilium" "Agent, operator, or Cilium status is unhealthy."
fi

if kip_component_ready k8s-app=kube-dns; then
    kip_record PASS "Cluster DNS" "CoreDNS is ready."
else
    kip_record FAIL "Cluster DNS" "CoreDNS is not ready."
fi

if kip_all_system_pods_healthy; then
    kip_record PASS "System pods" "All kube-system pods are Running/ready or Succeeded."
else
    kip_record FAIL "System pods" "A kube-system pod is pending, failed, or unready."
fi

kip_summary "Cluster validation"
[ "$KIP_FAILED" -eq 0 ]
