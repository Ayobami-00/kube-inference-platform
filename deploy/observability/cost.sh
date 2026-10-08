#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
KIP_OBSERVABILITY_ROOT=$SCRIPT_DIR
readonly KIP_OBSERVABILITY_ROOT
KIP_DEPLOY_ROOT=$(cd "$KIP_OBSERVABILITY_ROOT/.." && pwd)
readonly KIP_DEPLOY_ROOT
KIP_REPOSITORY_ROOT=$(cd "$KIP_DEPLOY_ROOT/.." && pwd)
readonly KIP_REPOSITORY_ROOT
KIP_CLUSTER_ROOT="$KIP_DEPLOY_ROOT/cluster"
readonly KIP_CLUSTER_ROOT
KIP_GPU_ROOT="$KIP_DEPLOY_ROOT/gpu"
readonly KIP_GPU_ROOT
KIP_RUNTIME_ROOT="$KIP_DEPLOY_ROOT/runtime"
readonly KIP_RUNTIME_ROOT

source "$KIP_DEPLOY_ROOT/lib/ui.sh"
source "$KIP_OBSERVABILITY_ROOT/versions.env"
source "$KIP_CLUSTER_ROOT/lib/common.sh"
source "$KIP_CLUSTER_ROOT/lib/kubernetes.sh"
source "$KIP_OBSERVABILITY_ROOT/lib/common.sh"
source "$KIP_OBSERVABILITY_ROOT/lib/render.sh"
source "$KIP_OBSERVABILITY_ROOT/lib/health.sh"

export LC_ALL=C
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_COST_PROFILE=${KIP_COST_PROFILE:-}
KIP_WORK_DIR=""

kip_ui_init
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
[ "$#" -eq 1 ] || kip_usage_error "cost command requires exactly one action: configure, check, or show."
case "$1" in
    configure | check | show) KIP_COST_ACTION=$1 ;;
    *) kip_usage_error "cost action must be configure, check, or show; received '$1'." ;;
esac
if [ "$KIP_COST_ACTION" = configure ] && [ -z "$KIP_COST_PROFILE" ]; then
    kip_usage_error "COST_PROFILE is required for cost-configure."
fi

KIP_WORK_DIR=$(command mktemp -d "${TMPDIR:-/tmp}/kip-cost.XXXXXX")
readonly KIP_WORK_DIR
trap 'command rm -rf "$KIP_WORK_DIR"' EXIT
trap 'kip_unexpected_error "$?" "$LINENO"' ERR

kip_cost_active_profile_matches_values() {
    kip_observability_profile_pricing_matches_release
}

kip_cost_configure() {
    local values_file="$KIP_WORK_DIR/opencost-priced.yaml"

    kip_observability_load_cost_profile
    kip_logo
    kip_heading "Whole-Node Cost Configuration · v0.1"
    kip_reset_results 6

    if kip_observability_namespace_owned \
        && kip_observability_release_exact "$KIP_MONITORING_RELEASE" \
        && kip_observability_release_exact "$KIP_OPENCOST_RELEASE" \
        && kip_observability_namespaced_resources_safe; then
        kip_record PASS "Observability prerequisite" "Pinned Prometheus and OpenCost releases are deployed."
    else
        kip_record FAIL "Observability prerequisite" "Run observability-install before configuring pricing."
        kip_summary "Cost configuration"
        return 1
    fi

    if kip_observability_cost_profile_matches_cluster; then
        kip_record PASS "Profile validation" \
            "$KIP_COST_PROFILE_NAME is valid for $KIP_COST_PHYSICAL_GPU_COUNT physical GPU(s)."
    else
        kip_record FAIL "Profile validation" "Profile GPU count differs from Kubernetes capacity."
        kip_summary "Cost configuration"
        return 1
    fi

    kip_observability_render_cost_values "$values_file"
    if kip_exec helm upgrade --install "$KIP_OPENCOST_RELEASE" \
        "$KIP_OPENCOST_CHART@$KIP_OPENCOST_CHART_DIGEST" \
        --namespace "$KIP_OBSERVABILITY_NAMESPACE" \
        --values "$values_file" --wait --timeout 10m \
        && kip_observability_set_custom_cost_enabled true; then
        kip_record PASS "OpenCost pricing" \
            "$KIP_COST_NODE_HOURLY_RATE_USD USD/node-hour maps to $KIP_COST_GPU_HOURLY_RATE_USD USD/GPU-hour."
    else
        kip_record FAIL "OpenCost pricing" "OpenCost rejected the rendered whole-node pricing."
        kip_summary "Cost configuration"
        return 1
    fi

    if kip_observability_apply_cost_metadata; then
        kip_record PASS "Published metadata" "Profile provenance and canonical hash are available to reports and Grafana."
    else
        kip_record FAIL "Published metadata" "Failed to publish sanitized profile metadata."
        kip_summary "Cost configuration"
        return 1
    fi

    if kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" rollout status \
        deployment/kip-opencost --timeout=10m >/dev/null \
        && kip_observability_opencost_ready; then
        kip_record PASS "OpenCost health" "OpenCost restarted and its internal health endpoint passed."
    else
        kip_record FAIL "OpenCost health" "The repriced OpenCost Deployment is unhealthy."
    fi

    if kip_cost_active_profile_matches_values; then
        kip_record PASS "Cost invariants" "Only GPU price carries the dedicated node rate; electricity is not double-counted."
    else
        kip_record FAIL "Cost invariants" "Active Helm values differ from the selected cost profile."
    fi

    kip_summary "Cost configuration"
    [ "$KIP_FAILED" -eq 0 ]
}

kip_cost_check() {
    local active_hash active_name pricing_state requested_hash="" requested_name=""

    if [ -n "$KIP_COST_PROFILE" ]; then
        kip_observability_load_cost_profile
        requested_name=$KIP_COST_PROFILE_NAME
        requested_hash=$KIP_COST_PROFILE_HASH
    fi

    kip_logo
    kip_heading "Whole-Node Cost Validation · v0.1"
    kip_reset_results 5

    if kip_observability_release_exact "$KIP_OPENCOST_RELEASE"; then
        kip_record PASS "Pinned OpenCost release" "OpenCost $KIP_OPENCOST_CHART_VERSION is deployed."
    else
        kip_record FAIL "Pinned OpenCost release" "OpenCost is absent, unhealthy, or version-drifted."
    fi

    if kip_observability_opencost_ready; then
        kip_record PASS "OpenCost API" "The internal OpenCost health endpoint passed."
    else
        kip_record FAIL "OpenCost API" "The internal OpenCost health endpoint failed."
    fi

    pricing_state=$(kip_observability_release_pricing_state 2>/dev/null) \
        || pricing_state=invalid
    if kip_observability_active_profile_json >/dev/null 2>&1; then
        if kip_observability_load_active_profile \
            && kip_observability_profile_metadata_matches_release; then
            active_name=$KIP_COST_PROFILE_NAME
            active_hash=$KIP_COST_PROFILE_HASH
            if [ -n "$requested_name" ] \
                && ! kip_observability_profile_identity_matches \
                    "$requested_name" "$requested_hash" "$active_name" "$active_hash"; then
                kip_record FAIL "Published profile" \
                    "Requested $requested_name ($requested_hash) does not match active $active_name ($active_hash)."
            else
                kip_record PASS "Published profile" \
                    "$active_name ($active_hash) is strict, sanitized, and reproducible."
            fi
        else
            kip_record FAIL "Published profile" "The active profile or Helm metadata is malformed, unsafe, or drifted."
        fi
    elif [ "$pricing_state" = priced ]; then
        kip_record FAIL "Published profile" "OpenCost is priced but its reviewed KIP profile is missing."
    elif [ "$pricing_state" = unpriced ]; then
        KIP_COST_PROFILE_STATE=unpriced
        if [ -n "$requested_name" ]; then
            kip_record FAIL "Published profile" \
                "Requested $requested_name is not active; configure it before benchmarking."
        else
            kip_record WARN "Published profile" "Pricing not configured; benchmark publication remains blocked."
        fi
    else
        kip_record FAIL "Published profile" "OpenCost pricing state is missing or invalid."
    fi

    if [ "$KIP_COST_PROFILE_STATE" = priced ]; then
        if kip_observability_cost_profile_matches_cluster \
            && kip_cost_active_profile_matches_values; then
            kip_record PASS "Whole-node allocation" "GPU count and active custom pricing match the published profile."
        else
            kip_record FAIL "Whole-node allocation" "Capacity or OpenCost pricing differs from the profile."
        fi
    else
        kip_record WARN "Whole-node allocation" "No whole-node rate has been selected."
    fi

    if [ "$KIP_COST_PROFILE_STATE" = priced ]; then
        if kip_observability_wait_for_required_metrics; then
            kip_record PASS "Pricing telemetry" "Positive GPU pricing and inference cost metrics are queryable in Prometheus."
        else
            kip_record FAIL "Pricing telemetry" "Configured price metrics remained absent after the bounded convergence wait."
        fi
    else
        kip_record WARN "Pricing telemetry" "Pricing not configured."
    fi

    kip_summary "Cost validation"
    [ "$KIP_FAILED" -eq 0 ]
}

kip_cost_show() {
    if ! kip_observability_active_profile_json >/dev/null 2>&1; then
        printf '%s\n' "Pricing not configured. Select a reviewed profile with make cost-configure COST_PROFILE=<name>."
        return 0
    fi
    kip_observability_load_active_profile || return 1
    printf '%-24s %s\n' \
        "Profile" "$KIP_COST_PROFILE_NAME" \
        "Profile SHA-256" "$KIP_COST_PROFILE_HASH" \
        "Currency" "USD" \
        "Node hourly rate" "$KIP_COST_NODE_HOURLY_RATE_USD" \
        "GPU hourly rate" "$KIP_COST_GPU_HOURLY_RATE_USD" \
        "Physical GPUs" "$KIP_COST_PHYSICAL_GPU_COUNT" \
        "Provider" "$KIP_COST_PROVIDER" \
        "Machine type" "$KIP_COST_MACHINE_TYPE" \
        "Region" "$KIP_COST_REGION" \
        "Effective from" "$KIP_COST_EFFECTIVE_FROM" \
        "Source" "$KIP_COST_SOURCE" \
        "Power included" "true"
}

case "$KIP_COST_ACTION" in
    configure) kip_cost_configure ;;
    check) kip_cost_check ;;
    show) kip_cost_show ;;
esac
