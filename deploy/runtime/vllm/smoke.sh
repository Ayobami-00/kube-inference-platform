#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
KIP_RUNTIME_ROOT=$SCRIPT_DIR
readonly KIP_RUNTIME_ROOT
KIP_DEPLOY_ROOT=$(cd "$KIP_RUNTIME_ROOT/../.." && pwd)
readonly KIP_DEPLOY_ROOT
KIP_CLUSTER_ROOT="$KIP_DEPLOY_ROOT/cluster"
readonly KIP_CLUSTER_ROOT
KIP_GPU_ROOT="$KIP_DEPLOY_ROOT/gpu"
readonly KIP_GPU_ROOT

# shellcheck source=deploy/lib/ui.sh
source "$KIP_DEPLOY_ROOT/lib/ui.sh"
# shellcheck source=deploy/runtime/vllm/versions.env
source "$KIP_RUNTIME_ROOT/versions.env"
# shellcheck source=deploy/cluster/lib/common.sh
source "$KIP_CLUSTER_ROOT/lib/common.sh"
# shellcheck source=deploy/cluster/lib/kubernetes.sh
source "$KIP_CLUSTER_ROOT/lib/kubernetes.sh"
# shellcheck source=deploy/runtime/vllm/lib/common.sh
source "$KIP_RUNTIME_ROOT/lib/common.sh"
# shellcheck source=deploy/runtime/vllm/lib/render.sh
source "$KIP_RUNTIME_ROOT/lib/render.sh"
# shellcheck source=deploy/runtime/vllm/lib/health.sh
source "$KIP_RUNTIME_ROOT/lib/health.sh"
# shellcheck source=deploy/runtime/vllm/lib/smoke.sh
source "$KIP_RUNTIME_ROOT/lib/smoke.sh"

export LC_ALL=C
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_RUNTIME=${KIP_RUNTIME:-vllm}
KIP_MODEL_PROFILE=${KIP_MODEL_PROFILE:-qwen2.5-7b-instruct}
KIP_WORK_DIR=""
KIP_RUNTIME_SMOKE_ACTIVE=0

kip_ui_init
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
[ "$KIP_RUNTIME" = vllm ] || kip_usage_error "the vLLM adapter requires RUNTIME=vllm."
[ "$#" -eq 0 ] || kip_usage_error "runtime-smoke does not accept positional arguments."
kip_runtime_load_profile

KIP_WORK_DIR=$(command mktemp -d "${TMPDIR:-/tmp}/kip-runtime-smoke.XXXXXX")
readonly KIP_WORK_DIR
kip_runtime_smoke_exit_cleanup() {
    if [ "$KIP_RUNTIME_SMOKE_ACTIVE" -eq 1 ]; then
        kip_runtime_smoke_cleanup || true
    fi
    kip_runtime_stop_port_forward
    command rm -rf "$KIP_WORK_DIR"
}
trap kip_runtime_smoke_exit_cleanup EXIT
trap 'kip_unexpected_error "$?" "$LINENO"' ERR
kip_runtime_render_manifests

kip_logo
kip_heading "OpenAI-Compatible Runtime Smoke Test · v0.1"
kip_reset_results 3

if KIP_NO_LOGO=1 KIP_RUNTIME="$KIP_RUNTIME" KIP_MODEL_PROFILE="$KIP_MODEL_PROFILE" \
    "$KIP_RUNTIME_ROOT/validate.sh" >/dev/null; then
    kip_record PASS "Runtime prerequisite" "Read-only runtime validation passed."
else
    kip_record FAIL "Runtime prerequisite" "make runtime-check must pass first."
    kip_summary "Runtime smoke test"
    exit 1
fi

KIP_RUNTIME_SMOKE_ACTIVE=1
if kip_runtime_run_smoke; then
    KIP_RUNTIME_SMOKE_ACTIVE=0
    kip_record PASS "Inference checkpoint" \
        "Health, discovery, non-streaming, and incremental SSE checks passed."
else
    KIP_RUNTIME_SMOKE_ACTIVE=0
    kip_record FAIL "Inference checkpoint" "OpenAI-compatible inference checks failed."
    kip_summary "Runtime smoke test"
    exit 1
fi

if kip_runtime_run_cluster_check >/dev/null && kip_runtime_run_gpu_check >/dev/null; then
    kip_record PASS "Final platform health" "Cluster and GPU health remain valid."
else
    kip_record FAIL "Final platform health" "Cluster or GPU validation failed after inference."
fi

kip_summary "Runtime smoke test"
if [ "$KIP_FAILED" -eq 0 ]; then
    exit 0
fi
exit 1
