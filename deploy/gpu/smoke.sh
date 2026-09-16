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
# shellcheck source=deploy/gpu/lib/smoke.sh
source "$KIP_GPU_ROOT/lib/smoke.sh"

export LC_ALL=C
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_GPU_DRIVER_MODE=${KIP_GPU_DRIVER_MODE:-}

kip_ui_init
trap 'kip_unexpected_error "$?" "$LINENO"' ERR
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
[ "$#" -eq 0 ] || kip_usage_error "gpu-smoke does not accept positional arguments."

kip_logo
kip_heading "NVIDIA GPU Smoke Test · v0.1"
kip_reset_results 3

if KIP_NO_LOGO=1 "$KIP_GPU_ROOT/validate.sh" >/dev/null \
    && kip_gpu_resolve_mode \
    && kip_gpu_detect_physical_hardware \
    && kip_gpu_capacity_matches_hardware; then
    kip_record PASS "GPU prerequisite" "Read-only GPU validation passed."
else
    kip_record FAIL "GPU prerequisite" "make gpu-check must pass first."
    kip_summary "GPU smoke test"
    exit 1
fi

if kip_gpu_run_smoke; then
    kip_record PASS "CUDA smoke Pod" "Temporary one-GPU nvidia-smi workload succeeded and was removed."
else
    kip_record FAIL "CUDA smoke Pod" "Smoke execution or cleanup failed."
    kip_summary "GPU smoke test"
    exit 1
fi

if kip_gpu_run_cluster_check >/dev/null; then
    kip_record PASS "Final cluster health" "The cluster remains healthy."
else
    kip_record FAIL "Final cluster health" "Cluster validation failed after the smoke Pod."
fi

kip_summary "GPU smoke test"
if [ "$KIP_FAILED" -eq 0 ]; then
    exit 0
fi
exit 1
