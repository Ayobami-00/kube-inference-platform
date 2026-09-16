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
# shellcheck source=deploy/gpu/lib/operator.sh
source "$KIP_GPU_ROOT/lib/operator.sh"
# shellcheck source=deploy/gpu/lib/health.sh
source "$KIP_GPU_ROOT/lib/health.sh"
# shellcheck source=deploy/gpu/lib/smoke.sh
source "$KIP_GPU_ROOT/lib/smoke.sh"

export LC_ALL=C
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_GPU_DRIVER_MODE=${KIP_GPU_DRIVER_MODE:-operator}
KIP_WORK_DIR=""

kip_ui_init
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
kip_gpu_validate_driver_mode "$KIP_GPU_DRIVER_MODE"
[ "$#" -eq 0 ] || kip_usage_error "gpu-install does not accept positional arguments."

KIP_WORK_DIR=$(command mktemp -d "${TMPDIR:-/tmp}/kip-gpu.XXXXXX")
readonly KIP_WORK_DIR
kip_gpu_cleanup() {
    command rm -rf "$KIP_WORK_DIR"
}
trap kip_gpu_cleanup EXIT
trap 'kip_unexpected_error "$?" "$LINENO"' ERR

kip_logo
kip_heading "NVIDIA GPU Stack Installation · v0.1"
kip_reset_results 8

if kip_gpu_run_cluster_check >/dev/null && kip_gpu_single_ready_node; then
    kip_record PASS "Cluster prerequisite" "One Ready, schedulable Kubernetes node."
else
    kip_record FAIL "Cluster prerequisite" "make cluster-check must pass on one node."
    kip_summary "GPU installation"
    exit 1
fi

if kip_gpu_required_commands_available && kip_gpu_detect_physical_hardware; then
    kip_record PASS "NVIDIA hardware" "$KIP_PHYSICAL_GPU_COUNT physical GPU device(s) detected."
else
    kip_record FAIL "NVIDIA hardware" "Required commands or NVIDIA PCI hardware are missing."
    kip_summary "GPU installation"
    exit 1
fi

gpu_state=""
if gpu_state=$(kip_gpu_preflight_state) \
    && kip_gpu_driver_mode_preflight "$gpu_state" \
    && kip_gpu_no_partitioning_or_sharing; then
    kip_record PASS "Safe installation state" \
        "$gpu_state installation; $KIP_GPU_DRIVER_MODE driver mode; whole GPUs only."
else
    kip_record FAIL "Safe installation state" \
        "Conflicting, unmanaged, partitioned, shared, or driver-drift state detected."
    kip_summary "GPU installation"
    exit 1
fi

if kip_gpu_verify_chart; then
    kip_record PASS "Verified chart artifact" \
        "$KIP_GPU_OPERATOR_VERSION tag, digest, checksum, and NVIDIA signature verified."
else
    kip_record FAIL "Verified chart artifact" "Artifact identity or signature verification failed."
    kip_summary "GPU installation"
    exit 1
fi

if kip_gpu_install_operator; then
    kip_record PASS "GPU Operator release" \
        "$KIP_GPU_RELEASE reconciled in $KIP_GPU_NAMESPACE without automatic rollback."
else
    kip_record FAIL "GPU Operator release" \
        "Helm reconciliation failed; workloads remain available for diagnosis."
    kip_summary "GPU installation"
    exit 1
fi

if KIP_NO_LOGO=1 KIP_GPU_DRIVER_MODE="$KIP_GPU_DRIVER_MODE" \
    "$KIP_GPU_ROOT/validate.sh" >/dev/null; then
    kip_gpu_capacity_matches_hardware
    kip_record PASS "Kubernetes GPU resources" \
        "$KIP_GPU_NODE_NAME exposes $KIP_PHYSICAL_GPU_COUNT whole nvidia.com/gpu resource(s)."
else
    kip_record FAIL "Kubernetes GPU resources" "GPU Operator health or capacity validation failed."
    kip_summary "GPU installation"
    exit 1
fi

if kip_gpu_run_smoke; then
    kip_record PASS "CUDA smoke Pod" "A one-GPU Pod completed nvidia-smi successfully."
else
    kip_record FAIL "CUDA smoke Pod" "The temporary one-GPU nvidia-smi Pod failed."
    kip_summary "GPU installation"
    exit 1
fi

if kip_gpu_run_cluster_check >/dev/null; then
    kip_record PASS "Final cluster health" "The cluster remains healthy after GPU validation."
else
    kip_record FAIL "Final cluster health" "Cluster validation failed after the GPU smoke test."
    kip_summary "GPU installation"
    exit 1
fi

kip_summary "GPU installation"
printf '\n%sHard checkpoint passed:%s Kubernetes can schedule and expose the GPU.\n' \
    "$KIP_COLOR_GREEN" "$KIP_COLOR_RESET"
printf '%sNext milestone:%s deploy the first vLLM model server.\n' \
    "$KIP_COLOR_CYAN" "$KIP_COLOR_RESET"
