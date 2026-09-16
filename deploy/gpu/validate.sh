#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
KIP_GPU_ROOT=$SCRIPT_DIR
readonly KIP_GPU_ROOT
KIP_DEPLOY_ROOT=$(cd "$KIP_GPU_ROOT/.." && pwd)
readonly KIP_DEPLOY_ROOT
KIP_CLUSTER_ROOT="$KIP_DEPLOY_ROOT/cluster"
readonly KIP_CLUSTER_ROOT

# shellcheck source=deploy/lib/ui.sh
source "$KIP_DEPLOY_ROOT/lib/ui.sh"
# shellcheck source=deploy/cluster/versions.env
source "$KIP_CLUSTER_ROOT/versions.env"
# shellcheck source=deploy/gpu/versions.env
source "$KIP_GPU_ROOT/versions.env"
# shellcheck source=deploy/cluster/lib/common.sh
source "$KIP_CLUSTER_ROOT/lib/common.sh"
# shellcheck source=deploy/cluster/lib/kubernetes.sh
source "$KIP_CLUSTER_ROOT/lib/kubernetes.sh"
# shellcheck source=deploy/gpu/lib/common.sh
source "$KIP_GPU_ROOT/lib/common.sh"
# shellcheck source=deploy/gpu/lib/preflight.sh
source "$KIP_GPU_ROOT/lib/preflight.sh"
# shellcheck source=deploy/gpu/lib/health.sh
source "$KIP_GPU_ROOT/lib/health.sh"

export LC_ALL=C
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_GPU_DRIVER_MODE=${KIP_GPU_DRIVER_MODE:-}

kip_ui_init
trap 'kip_unexpected_error "$?" "$LINENO"' ERR
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
[ "$#" -eq 0 ] || kip_usage_error "gpu-check does not accept positional arguments."

kip_logo
kip_heading "NVIDIA GPU Stack Validation · v0.1"
kip_reset_results 8

if kip_gpu_required_commands_available; then
    kip_record PASS "Inspection commands" "helm, kubectl, jq, lspci, curl, and sha256sum are available."
else
    kip_record FAIL "Inspection commands" "Required GPU inspection commands are missing."
fi

if kip_gpu_run_cluster_check >/dev/null; then
    kip_record PASS "Cluster prerequisite" "The single-node Kubernetes cluster is healthy."
else
    kip_record FAIL "Cluster prerequisite" "make cluster-check must pass first."
fi

if kip_gpu_resolve_mode && kip_gpu_release_is_exact; then
    kip_record PASS "Pinned Helm release" \
        "GPU Operator $KIP_GPU_OPERATOR_VERSION; $KIP_GPU_DRIVER_MODE driver mode"
else
    kip_record FAIL "Pinned Helm release" "Release, digest, values, or driver mode has drifted."
fi

if kip_gpu_single_ready_node; then
    kip_record PASS "Single GPU node" "Exactly one Ready, schedulable node is present."
else
    kip_record FAIL "Single GPU node" "Exactly one Ready, schedulable node is required."
fi

if kip_gpu_detect_physical_hardware; then
    kip_record PASS "NVIDIA PCI hardware" "$KIP_PHYSICAL_GPU_COUNT physical GPU device(s) detected."
else
    kip_record FAIL "NVIDIA PCI hardware" "No NVIDIA display-class PCI device was detected."
fi

if kip_gpu_cluster_policy_ready; then
    kip_record PASS "ClusterPolicy" "ClusterPolicy/cluster-policy reports ready."
else
    kip_record FAIL "ClusterPolicy" "ClusterPolicy/cluster-policy is absent or not ready."
fi

if kip_gpu_operand_pods_healthy; then
    kip_record PASS "GPU Operator operands" "Required operator and GPU workloads are healthy."
else
    kip_record FAIL "GPU Operator operands" "A required operand is absent, pending, failed, or unready."
fi

if kip_gpu_no_partitioning_or_sharing && kip_gpu_capacity_matches_hardware; then
    kip_record PASS "Whole-GPU resources" \
        "$KIP_GPU_NODE_NAME advertises $KIP_PHYSICAL_GPU_COUNT allocatable nvidia.com/gpu."
else
    kip_record FAIL "Whole-GPU resources" \
        "Capacity must match physical GPUs with no MIG or time-slicing state."
fi

kip_summary "GPU validation"
if [ "$KIP_FAILED" -eq 0 ]; then
    exit 0
fi
exit 1
