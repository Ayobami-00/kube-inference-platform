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
# shellcheck source=deploy/runtime/vllm/lib/health.sh
source "$KIP_RUNTIME_ROOT/lib/health.sh"

export LC_ALL=C
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_RUNTIME=${KIP_RUNTIME:-vllm}
KIP_MODEL_PROFILE=${KIP_MODEL_PROFILE:-qwen2.5-7b-instruct}
KIP_WORK_DIR=""

kip_ui_init
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
[ "$KIP_RUNTIME" = vllm ] || kip_usage_error "the vLLM adapter requires RUNTIME=vllm."
[ "$#" -eq 0 ] || kip_usage_error "runtime-check does not accept positional arguments."
kip_runtime_load_profile

KIP_WORK_DIR=$(command mktemp -d "${TMPDIR:-/tmp}/kip-runtime-check.XXXXXX")
readonly KIP_WORK_DIR
kip_runtime_validate_cleanup() {
    kip_runtime_stop_port_forward
    command rm -rf "$KIP_WORK_DIR"
}
trap kip_runtime_validate_cleanup EXIT
trap 'kip_unexpected_error "$?" "$LINENO"' ERR

kip_logo
kip_heading "vLLM Runtime Validation · v0.1"
kip_reset_results 7

if kip_runtime_required_commands_available; then
    kip_record PASS "Inspection commands" "curl, df, jq, kubectl, and sha256sum are available."
else
    kip_record FAIL "Inspection commands" "Required runtime inspection commands are missing."
fi

if kip_runtime_run_cluster_check >/dev/null && kip_runtime_run_gpu_check >/dev/null \
    && kip_runtime_single_gpu_node; then
    kip_record PASS "Platform prerequisites" "Cluster and GPU validation passed."
else
    kip_record FAIL "Platform prerequisites" "Cluster or GPU validation failed."
fi

if kip_runtime_namespace_owned && kip_runtime_namespace_profile_matches \
    && kip_runtime_identity_matches deployment "$KIP_RUNTIME_RESOURCE_NAME" \
    && kip_runtime_identity_matches service "$KIP_RUNTIME_RESOURCE_NAME"; then
    kip_record PASS "Pinned runtime identity" \
        "$KIP_VLLM_VERSION, model revision, image digest, and profile hash match."
else
    kip_record FAIL "Pinned runtime identity" "Runtime ownership or pinned configuration has drifted."
fi

if kip_runtime_deployment_exact; then
    kip_record PASS "vLLM Deployment" "One Recreate replica requests one whole GPU and is available."
else
    kip_record FAIL "vLLM Deployment" "Deployment configuration or availability is unhealthy."
fi

if kip_runtime_pod_healthy; then
    if [ "$KIP_RUNTIME_RESTARTS" -gt 0 ]; then
        kip_record WARN "vLLM Pod" \
            "Pod is Ready but containers report $KIP_RUNTIME_RESTARTS previous restart(s)."
    else
        kip_record PASS "vLLM Pod" "The GPU-backed Pod is Running and Ready without restarts."
    fi
else
    kip_record FAIL "vLLM Pod" "Pod is absent, failed, pending, unready, or crash-looping."
fi

if kip_runtime_service_healthy; then
    kip_record PASS "Runtime Service" "ClusterIP Service has exactly one ready endpoint."
else
    kip_record FAIL "Runtime Service" "Service configuration or ready endpoints are unhealthy."
fi

if kip_runtime_api_healthy; then
    kip_record PASS "Read-only API" "/health and /v1/models expose the pinned served model."
else
    kip_record FAIL "Read-only API" "Health or model discovery failed through the Service."
fi

kip_summary "Runtime validation"
if [ "$KIP_FAILED" -eq 0 ]; then
    exit 0
fi
exit 1
