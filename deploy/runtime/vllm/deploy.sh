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
# shellcheck source=deploy/runtime/vllm/lib/preflight.sh
source "$KIP_RUNTIME_ROOT/lib/preflight.sh"
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
[ "$#" -eq 0 ] || kip_usage_error "runtime-deploy does not accept positional arguments."
kip_runtime_load_profile

KIP_WORK_DIR=$(command mktemp -d "${TMPDIR:-/tmp}/kip-runtime.XXXXXX")
readonly KIP_WORK_DIR
kip_runtime_deploy_cleanup() {
    if [ "$KIP_RUNTIME_SMOKE_ACTIVE" -eq 1 ]; then
        kip_runtime_smoke_cleanup || true
    fi
    kip_runtime_stop_port_forward
    command rm -rf "$KIP_WORK_DIR"
}
trap kip_runtime_deploy_cleanup EXIT
trap 'kip_unexpected_error "$?" "$LINENO"' ERR

kip_logo
kip_heading "vLLM Runtime Deployment · v0.1"
kip_reset_results 10

if kip_runtime_required_commands_available; then
    kip_record PASS "Runtime configuration" \
        "$KIP_RUNTIME/$KIP_MODEL_PROFILE profile validated and pinned."
else
    kip_record FAIL "Runtime configuration" "Required runtime inspection commands are missing."
    kip_summary "Runtime deployment"
    exit 1
fi

if kip_runtime_run_cluster_check >/dev/null && kip_runtime_run_gpu_check >/dev/null \
    && kip_runtime_single_gpu_node; then
    kip_record PASS "Platform prerequisites" "Cluster and whole-GPU validation passed on one node."
else
    kip_record FAIL "Platform prerequisites" "make cluster-check and make gpu-check must pass first."
    kip_summary "Runtime deployment"
    exit 1
fi

if kip_runtime_disk_space_ready; then
    kip_record PASS "Runtime storage" "Sufficient model-cache and container-image capacity is available."
else
    kip_record FAIL "Runtime storage" \
        "Require 60 GiB on a shared filesystem, or 25 GiB cache plus 30 GiB containerd."
    kip_summary "Runtime deployment"
    exit 1
fi

runtime_state=""
if kip_runtime_preflight_state; then
    runtime_state=$KIP_RUNTIME_PREFLIGHT_STATE
    kip_record PASS "Safe runtime state" "$runtime_state managed runtime installation."
else
    kip_record FAIL "Safe runtime state" \
        "${KIP_RUNTIME_PREFLIGHT_ERROR:-Unmanaged or drifted runtime state detected.}"
    kip_summary "Runtime deployment"
    exit 1
fi

if kip_acquire_privileges \
    && kip_runtime_cache_storage_ready; then
    kip_record PASS "Model cache" "$KIP_RUNTIME_CACHE_PATH is ready for persistent model files."
else
    kip_record FAIL "Model cache" \
        "Root access and an absent or exactly KIP-marked cache root are required."
    kip_summary "Runtime deployment"
    exit 1
fi

kip_runtime_render_manifests
if kip_runtime_secret_ready; then
    kip_record PASS "Model credentials" \
        "${KIP_RUNTIME_HF_SECRET_NAME:-Public model; no Hugging Face Secret required.}"
else
    kip_record FAIL "Model credentials" \
        "The configured Hugging Face Secret or token key is missing."
    kip_summary "Runtime deployment"
    exit 1
fi

if kip_runtime_apply_manifests; then
    kip_record PASS "vLLM rollout" \
        "$KIP_RUNTIME_RESOURCE_NAME reached one Ready replica using $KIP_VLLM_VERSION."
else
    kip_record FAIL "vLLM rollout" "Deployment did not become Ready within 45 minutes."
    kip_runtime_diagnostics
    kip_summary "Runtime deployment"
    exit 1
fi

if KIP_NO_LOGO=1 KIP_RUNTIME="$KIP_RUNTIME" KIP_MODEL_PROFILE="$KIP_MODEL_PROFILE" \
    "$KIP_RUNTIME_ROOT/validate.sh" >/dev/null; then
    kip_record PASS "Runtime health" "Deployment, Pod, Service, health, and model discovery passed."
else
    kip_record FAIL "Runtime health" "Read-only runtime validation failed after rollout."
    kip_runtime_diagnostics
    kip_summary "Runtime deployment"
    exit 1
fi

KIP_RUNTIME_SMOKE_ACTIVE=1
if kip_runtime_run_smoke; then
    KIP_RUNTIME_SMOKE_ACTIVE=0
    kip_record PASS "Inference checkpoint" \
        "Health, models, non-streaming generation, and incremental SSE passed."
else
    KIP_RUNTIME_SMOKE_ACTIVE=0
    kip_record FAIL "Inference checkpoint" "OpenAI-compatible inference smoke testing failed."
    kip_summary "Runtime deployment"
    exit 1
fi

if kip_runtime_run_cluster_check >/dev/null && kip_runtime_run_gpu_check >/dev/null; then
    kip_record PASS "Final platform health" "Cluster and GPU health remain valid after inference."
else
    kip_record FAIL "Final platform health" "Cluster or GPU validation failed after inference."
fi

kip_summary "Runtime deployment"
if [ "$KIP_FAILED" -ne 0 ]; then
    exit 1
fi
printf '\n%sHard checkpoint passed:%s OpenAI-compatible inference is healthy and streaming.\n' \
    "$KIP_COLOR_GREEN" "$KIP_COLOR_RESET"
printf '%sNext milestone:%s implement the asynchronous benchmark client.\n' \
    "$KIP_COLOR_CYAN" "$KIP_COLOR_RESET"
