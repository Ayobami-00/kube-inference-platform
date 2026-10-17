#!/usr/bin/env bash
# Install pinned OpenCost with exact, heterogeneous, task-owned node rates.
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
KIP_DEPLOY_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
KIP_REPOSITORY_ROOT=$(cd "$KIP_DEPLOY_ROOT/.." && pwd)
KIP_CLUSTER_ROOT="$KIP_DEPLOY_ROOT/cluster"
KIP_OBSERVABILITY_ROOT="$KIP_DEPLOY_ROOT/observability"
readonly SCRIPT_DIR KIP_DEPLOY_ROOT KIP_REPOSITORY_ROOT KIP_CLUSTER_ROOT KIP_OBSERVABILITY_ROOT

# shellcheck source=deploy/lib/ui.sh
source "$KIP_DEPLOY_ROOT/lib/ui.sh"
# shellcheck source=deploy/observability/versions.env
source "$KIP_OBSERVABILITY_ROOT/versions.env"
# shellcheck source=deploy/cluster/lib/common.sh
source "$KIP_CLUSTER_ROOT/lib/common.sh"
# shellcheck source=deploy/cluster/lib/kubernetes.sh
source "$KIP_CLUSTER_ROOT/lib/kubernetes.sh"
# shellcheck source=deploy/observability/lib/common.sh
source "$KIP_OBSERVABILITY_ROOT/lib/common.sh"

export LC_ALL=C
# Helm and kubectl must target the same owned control-plane API, including
# when the installer is invoked through sudo without a user's kubeconfig.
export KUBECONFIG="${KIP_KUBECONFIG:-/etc/kubernetes/admin.conf}"
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_FLEET_COST_PROVIDER=${KIP_FLEET_COST_PROVIDER:-vast}
case "$KIP_FLEET_COST_PROVIDER" in
    vast)
        KIP_FLEET_COST_LEDGER=${KIP_FLEET_COST_LEDGER:-$KIP_REPOSITORY_ROOT/fleet/.work/cost-ledger.json}
        KIP_FLEET_COST_CSV=${KIP_FLEET_COST_CSV:-$KIP_REPOSITORY_ROOT/fleet/.work/opencost-pricing.csv}
        KIP_FLEET_COST_VALUES=$SCRIPT_DIR/opencost-values.yaml
        ;;
    lambda)
        KIP_FLEET_COST_LEDGER=${KIP_FLEET_COST_LEDGER:-$KIP_REPOSITORY_ROOT/fleet/.work/lambda-cost-ledger.json}
        KIP_FLEET_COST_CSV=${KIP_FLEET_COST_CSV:-$KIP_REPOSITORY_ROOT/fleet/.work/lambda-opencost-pricing.csv}
        KIP_FLEET_COST_VALUES=$KIP_DEPLOY_ROOT/lambda/opencost-values.yaml
        ;;
    *) kip_usage_error 'KIP_FLEET_COST_PROVIDER must be vast or lambda.' ;;
esac
KIP_WORK_DIR=""
KIP_PRICING_CHANGED=0
kip_ui_init
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
[ "$#" -eq 0 ] || kip_usage_error "fleet-cost-install accepts no positional arguments."
KIP_WORK_DIR=$(command mktemp -d "${TMPDIR:-/tmp}/kip-fleet-cost.XXXXXX")
readonly KIP_WORK_DIR
trap 'command rm -rf "$KIP_WORK_DIR"' EXIT
trap 'kip_unexpected_error "$?" "$LINENO"' ERR

kip_fleet_cost_pricing_safe() {
    local actual hash object body_hash
    hash=$(kip_exec sha256sum "$KIP_FLEET_COST_CSV" | command awk '{print $1}') || return 1
    object=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get configmap \
        kip-fleet-opencost-pricing --ignore-not-found -o json) || return 1
    [ -n "$object" ] || return 0
    actual=$(printf '%s' "$object" | kip_exec jq -er \
        '.metadata.annotations["kube-inference-platform.io/pricing-sha256"] // ""') \
        || return 1
    if [ "$actual" != "$hash" ]; then
        [ "${1:-}" = allow-legacy ] || return 1
        # Only the two exact task-owned revisions created during the pilot may
        # migrate: the header-bearing CSV and the GPU-double-counting CSV.
        case "$actual" in
            c6997bd9282983cd5c7f84c1bfb572f158b45b8b19087ec208660d58eef450dd | \
            3fb5053ccf5fa7020e625828075e015aebac8f7a6e5abdd706dfb865b28f38a1) ;;
            *) return 1 ;;
        esac
        body_hash=$(printf '%s' "$object" | kip_exec jq -rj '.data["pricing.csv"]' \
            | kip_exec sha256sum | command awk '{print $1}') || return 1
        [ "$body_hash" = "$actual" ] || return 1
        printf '%s' "$object" | kip_exec jq -e '
            .metadata.labels["app.kubernetes.io/managed-by"] == "kube-inference-platform"
        ' >/dev/null || return 1
        return
    fi
    printf '%s' "$object" | kip_exec jq -e \
        --arg hash "$hash" \
        --rawfile pricing "$KIP_FLEET_COST_CSV" '
        .metadata.labels["app.kubernetes.io/managed-by"] == "kube-inference-platform"
        and .metadata.annotations["kube-inference-platform.io/pricing-sha256"] == $hash
        and .data["pricing.csv"] == $pricing
    ' >/dev/null
}

kip_fleet_cost_release_safe() {
    local chart status values actual_values_hash
    if ! kip_exec helm status "$KIP_OPENCOST_RELEASE" \
        --namespace "$KIP_OBSERVABILITY_NAMESPACE" >/dev/null 2>&1; then
        return 0
    fi
    status=$(kip_exec helm status "$KIP_OPENCOST_RELEASE" \
        --namespace "$KIP_OBSERVABILITY_NAMESPACE" --output json \
        | kip_exec jq -er '.info.status') || return 1
    case "$status" in deployed | failed | pending-upgrade) ;; *) return 1 ;; esac
    chart=$(kip_exec helm list --namespace "$KIP_OBSERVABILITY_NAMESPACE" \
        --filter "^$KIP_OPENCOST_RELEASE$" --output json \
        | kip_exec jq -er --arg release "$KIP_OPENCOST_RELEASE" \
            '.[] | select(.name == $release) | .chart') || return 1
    [ "$chart" = "opencost-$KIP_OPENCOST_CHART_VERSION" ] || return 1
    values=$(kip_exec helm get values "$KIP_OPENCOST_RELEASE" \
        --namespace "$KIP_OBSERVABILITY_NAMESPACE" --output json) || return 1
    printf '%s' "$values" | kip_exec jq -e --arg digest "$KIP_OPENCOST_CHART_DIGEST" \
        '.kip.managedBy == "kube-inference-platform" and .kip.chartDigest == $digest
         and .kip.pricingState == "fleet-csv"' >/dev/null || return 1
    if kip_exec python3 "$SCRIPT_DIR/helm-values-check.py" \
        "$KIP_OPENCOST_RELEASE" "$KIP_OBSERVABILITY_NAMESPACE" \
        "$KIP_OBSERVABILITY_ROOT/values/opencost-unpriced.yaml" \
        "$KIP_FLEET_COST_VALUES"; then
        return 0
    fi
    [ "${1:-}" = allow-legacy ] || return 1
    # One exact, already-owned release may be migrated from the header-bearing
    # CSV and read-only /var/configs revision; any other values drift still fails.
    actual_values_hash=$(printf '%s' "$values" | kip_exec jq -cS -j . \
        | kip_exec sha256sum | command awk '{print $1}') || return 1
    case "$actual_values_hash" in
        17a3d0bce47a781110ff11da0f44ee8994032d45e43af0ae5ee281baf11707e8) return 0 ;;
        d0acdf795f2ad5bede1daa2ff4a6ea3c31c2b7d21e63e2330f8a59e6d450c35e)
            [ "$KIP_FLEET_COST_PROVIDER" = lambda ] ;;
        *) return 1 ;;
    esac
}

kip_fleet_cost_apply_pricing() {
    local hash object existing_hash
    hash=$(kip_exec sha256sum "$KIP_FLEET_COST_CSV" | command awk '{print $1}') || return 1
    existing_hash=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get configmap \
        kip-fleet-opencost-pricing --ignore-not-found -o json \
        | kip_exec jq -r '.metadata.annotations["kube-inference-platform.io/pricing-sha256"] // ""') \
        || return 1
    if [ -n "$existing_hash" ] && [ "$existing_hash" != "$hash" ]; then
        KIP_PRICING_CHANGED=1
    fi
    object=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" create configmap \
        kip-fleet-opencost-pricing \
        --from-file="pricing.csv=$KIP_FLEET_COST_CSV" \
        --dry-run=client -o json) || return 1
    object=$(printf '%s\n' "$object" | kip_exec jq -c --arg hash "$hash" '
        .metadata.labels = {"app.kubernetes.io/managed-by":"kube-inference-platform"}
        | .metadata.annotations = {"kube-inference-platform.io/pricing-sha256":$hash}
    ') || return 1
    printf '%s\n' "$object" | kip_kubectl apply -f - >/dev/null
}

kip_logo
kip_heading "Fleet OpenCost allocation · pilot"
kip_reset_results 7
if { [ "$KIP_FLEET_COST_PROVIDER" = lambda ] || [ "$(command hostname)" = kip-control ]; } \
    && kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get prometheus \
        kip-monitoring-prometheus >/dev/null 2>&1 \
    && kip_exec helm status "$KIP_MONITORING_RELEASE" \
        --namespace "$KIP_OBSERVABILITY_NAMESPACE" --output json \
        | kip_exec jq -e '.info.status == "deployed"' >/dev/null; then
    kip_record PASS "Monitoring prerequisite" "Owned control-plane Prometheus is deployed."
else
    kip_record FAIL "Monitoring prerequisite" "Run fleet-observability-install first."
    kip_summary "Fleet cost installation"
    exit 1
fi
if [ "$KIP_FLEET_COST_PROVIDER" = lambda ]; then
    if kip_exec python3 "$KIP_DEPLOY_ROOT/lambda/verify_pricing.py" \
        --ledger "$KIP_FLEET_COST_LEDGER" --csv "$KIP_FLEET_COST_CSV" >/dev/null; then
        pricing_verified=1
    else
        pricing_verified=0
    fi
else
    if kip_exec python3 -m fleet.cost_ledger --verify-only \
        --ledger "$KIP_FLEET_COST_LEDGER" --csv "$KIP_FLEET_COST_CSV" >/dev/null; then
        pricing_verified=1
    else
        pricing_verified=0
    fi
fi
if [ "$pricing_verified" = 1 ]; then
    kip_record PASS "Rate evidence" "Fresh exact task-owned node rates passed."
else
    kip_record FAIL "Rate evidence" "Refresh and copy the private provider rate snapshot."
    kip_summary "Fleet cost installation"
    exit 1
fi
if kip_observability_namespace_owned \
    && kip_fleet_cost_pricing_safe allow-legacy \
    && kip_fleet_cost_release_safe allow-legacy; then
    kip_record PASS "Ownership" "No unmanaged pricing ConfigMap or drifted OpenCost release."
else
    kip_record FAIL "Ownership" "Pricing ConfigMap or OpenCost release is unmanaged or drifted."
    kip_summary "Fleet cost installation"
    exit 1
fi
chart_output=$(kip_exec helm pull "$KIP_OPENCOST_CHART" \
    --version "$KIP_OPENCOST_CHART_VERSION" --destination "$KIP_WORK_DIR" 2>&1) || {
    kip_record FAIL "Chart pin" "OpenCost OCI chart could not be fetched."
    kip_summary "Fleet cost installation"
    exit 1
}
chart_digest=$(printf '%s\n' "$chart_output" | command awk '/^Digest:[[:space:]]*/ {print $2; exit}')
if [ "$chart_digest" = "$KIP_OPENCOST_CHART_DIGEST" ]; then
    kip_record PASS "Chart pin" "OpenCost OCI chart matches its recorded digest."
else
    kip_record FAIL "Chart pin" "OpenCost chart digest differs from the lockfile."
    kip_summary "Fleet cost installation"
    exit 1
fi
if kip_fleet_cost_apply_pricing && kip_fleet_cost_pricing_safe; then
    kip_record PASS "Per-node price" "Exact task-owned rates mounted through an owned ConfigMap."
else
    kip_record FAIL "Per-node price" "Could not safely configure exact-node CSV pricing."
    kip_summary "Fleet cost installation"
    exit 1
fi
if kip_exec helm upgrade --install "$KIP_OPENCOST_RELEASE" \
    "$KIP_OPENCOST_CHART@$KIP_OPENCOST_CHART_DIGEST" \
    --namespace "$KIP_OBSERVABILITY_NAMESPACE" \
    --values "$KIP_OBSERVABILITY_ROOT/values/opencost-unpriced.yaml" \
    --values "$KIP_FLEET_COST_VALUES" \
    --wait --timeout 10m; then
    kip_record PASS "OpenCost" "Pinned internal API and inference allocation exporter deployed."
else
    kip_record FAIL "OpenCost" "Helm failed; release and Pods remain for diagnosis."
    kip_summary "Fleet cost installation"
    exit 1
fi
if [ "$KIP_PRICING_CHANGED" = 1 ]; then
    # The CSV provider rereads its mounted file hourly. Restart only the owned
    # Deployment after an exact pricing update so allocation reflects it now.
    kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" rollout restart \
        deployment/kip-opencost >/dev/null
fi
if kip_fleet_cost_release_safe \
    && kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" rollout status \
        deployment/kip-opencost --timeout=2m >/dev/null; then
    kip_record PASS "Exact rerun state" "OpenCost chart, values, and readiness match the fleet contract."
else
    kip_record FAIL "Exact rerun state" "OpenCost release drift or unhealthy exporter."
    kip_summary "Fleet cost installation"
    exit 1
fi
kip_summary "Fleet cost installation"
