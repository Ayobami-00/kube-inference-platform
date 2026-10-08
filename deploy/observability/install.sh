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

# shellcheck source=deploy/lib/ui.sh
source "$KIP_DEPLOY_ROOT/lib/ui.sh"
# shellcheck source=deploy/observability/versions.env
source "$KIP_OBSERVABILITY_ROOT/versions.env"
# shellcheck source=deploy/gpu/versions.env
source "$KIP_GPU_ROOT/versions.env"
# shellcheck source=deploy/cluster/lib/common.sh
source "$KIP_CLUSTER_ROOT/lib/common.sh"
# shellcheck source=deploy/cluster/lib/kubernetes.sh
source "$KIP_CLUSTER_ROOT/lib/kubernetes.sh"
# shellcheck source=deploy/gpu/lib/common.sh
source "$KIP_GPU_ROOT/lib/common.sh"
# shellcheck source=deploy/gpu/lib/operator.sh
source "$KIP_GPU_ROOT/lib/operator.sh"
# shellcheck source=deploy/observability/lib/common.sh
source "$KIP_OBSERVABILITY_ROOT/lib/common.sh"
# shellcheck source=deploy/observability/lib/dcgm.sh
source "$KIP_OBSERVABILITY_ROOT/lib/dcgm.sh"
# shellcheck source=deploy/observability/lib/preflight.sh
source "$KIP_OBSERVABILITY_ROOT/lib/preflight.sh"
# shellcheck source=deploy/observability/lib/render.sh
source "$KIP_OBSERVABILITY_ROOT/lib/render.sh"
# shellcheck source=deploy/observability/lib/health.sh
source "$KIP_OBSERVABILITY_ROOT/lib/health.sh"

export LC_ALL=C
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_COST_PROFILE=${KIP_COST_PROFILE:-}
KIP_WORK_DIR=""

kip_ui_init
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
[ "$#" -eq 0 ] || kip_usage_error "observability-install does not accept positional arguments."
if [ -n "$KIP_COST_PROFILE" ]; then
    kip_observability_load_cost_profile
fi

KIP_WORK_DIR=$(command mktemp -d "${TMPDIR:-/tmp}/kip-observability.XXXXXX")
readonly KIP_WORK_DIR
kip_observability_cleanup() {
    command rm -rf "$KIP_WORK_DIR"
}
trap kip_observability_cleanup EXIT
trap 'kip_unexpected_error "$?" "$LINENO"' ERR

kip_observability_verify_chart() {
    local actual chart=$1 digest=$2 output version=$3

    output=$(kip_exec helm pull "$chart" --version "$version" \
        --destination "$KIP_WORK_DIR" 2>&1) || return 1
    kip_verbose_output "Helm chart resolution" "$output"
    actual=$(printf '%s\n' "$output" | command awk '/^Digest:[[:space:]]*/ {print $2; exit}')
    [ "$actual" = "$digest" ]
}

kip_logo
kip_heading "Observability and Cost Accounting Installation · v0.1"
kip_reset_results 13

if kip_observability_required_commands_available; then
    kip_record PASS "Inspection commands" "helm, kubectl, jq, curl, OpenSSL, and checksum tools are available."
else
    kip_record FAIL "Inspection commands" "Required observability commands are missing."
    kip_summary "Observability installation"
    exit 1
fi

if kip_observability_recover_gpu_operator; then
    kip_record PASS "GPU Operator release state" \
        "The exact pinned release is deployed; an interrupted owned revision was safely recovered when necessary."
else
    kip_record FAIL "GPU Operator release state" \
        "GPU Operator is missing, drifted, or could not recover its exact owned revision."
    kip_summary "Observability installation"
    exit 1
fi

if kip_observability_platform_ready; then
    kip_record PASS "Platform prerequisites" "Cluster, GPU, runtime, and single-node health checks passed."
else
    kip_record FAIL "Platform prerequisites" \
        "Cluster, GPU, and runtime checks must pass; run make gpu-install if DCGM Exporter is absent or drifted."
    kip_summary "Observability installation"
    exit 1
fi

if kip_observability_disk_ready; then
    kip_record PASS "Storage capacity" "At least $KIP_OBSERVABILITY_REQUIRED_FREE_GIB GiB is available for platform telemetry."
else
    kip_record FAIL "Storage capacity" "At least $KIP_OBSERVABILITY_REQUIRED_FREE_GIB GiB of free storage is required."
    kip_summary "Observability installation"
    exit 1
fi

if kip_observability_safe_state \
    && kip_observability_prepare_install_pricing_state; then
    kip_record PASS "Safe installation state" \
        "No unmanaged namespace, release, version, or pricing-state drift was detected."
else
    kip_record FAIL "Safe installation state" "$KIP_OBSERVABILITY_PREFLIGHT_ERROR"
    kip_summary "Observability installation"
    exit 1
fi

if kip_observability_verify_chart "$KIP_KUBE_PROMETHEUS_STACK_CHART" \
    "$KIP_KUBE_PROMETHEUS_STACK_DIGEST" "$KIP_KUBE_PROMETHEUS_STACK_VERSION" \
    && kip_observability_verify_chart "$KIP_OPENCOST_CHART" \
        "$KIP_OPENCOST_CHART_DIGEST" "$KIP_OPENCOST_CHART_VERSION"; then
    kip_record PASS "Pinned chart artifacts" "Prometheus stack and OpenCost chart digests were verified."
else
    kip_record FAIL "Pinned chart artifacts" "A chart tag did not resolve to its recorded digest."
    kip_summary "Observability installation"
    exit 1
fi

if ! kip_observability_recover_unhealthy_release "$KIP_MONITORING_RELEASE" \
    || ! kip_observability_recover_unhealthy_release "$KIP_OPENCOST_RELEASE"; then
    kip_record FAIL "Helm recovery" \
        "An unhealthy release no longer has the exact pinned KIP identity or could not be safely removed."
    kip_summary "Observability installation"
    exit 1
fi

if kip_observability_reconcile_dcgm_exporter; then
    kip_record PASS "DCGM Exporter" \
        "GPU Operator owns the digest-pinned exporter at a one-second collection interval."
else
    kip_record FAIL "DCGM Exporter" "$KIP_OBSERVABILITY_DCGM_ERROR"
    kip_summary "Observability installation"
    exit 1
fi

storage_manifest="$KIP_WORK_DIR/storage.yaml"
kip_observability_render_storage "$storage_manifest"
if kip_kubectl apply -f "$KIP_OBSERVABILITY_ROOT/manifests/namespace.yaml" >/dev/null \
    && kip_observability_host_storage_ready \
    && kip_kubectl apply -f "$storage_manifest" >/dev/null \
    && kip_observability_grafana_secret_ready; then
    kip_record PASS "Persistent storage" "Prometheus, Grafana, and Alertmanager local volumes are ready."
else
    kip_record FAIL "Persistent storage" "Namespace, storage, or Grafana credential setup failed."
    kip_summary "Observability installation"
    exit 1
fi

if kip_exec helm upgrade --install "$KIP_MONITORING_RELEASE" \
    "$KIP_KUBE_PROMETHEUS_STACK_CHART@$KIP_KUBE_PROMETHEUS_STACK_DIGEST" \
    --namespace "$KIP_OBSERVABILITY_NAMESPACE" \
    --values "$KIP_OBSERVABILITY_ROOT/values/kube-prometheus-stack.yaml" \
    --wait --timeout 20m \
    && kip_observability_mark_crds_owned; then
    kip_record PASS "Prometheus and Grafana" "The pinned monitoring stack was reconciled through internal services."
else
    kip_record FAIL "Prometheus and Grafana" "Monitoring Helm reconciliation failed."
    kip_summary "Observability installation"
    exit 1
fi

if kip_kubectl apply -f "$KIP_OBSERVABILITY_ROOT/manifests/service-monitors.yaml" >/dev/null \
    && kip_kubectl apply -f "$KIP_OBSERVABILITY_ROOT/manifests/prometheus-rules.yaml" >/dev/null \
    && kip_observability_apply_dashboards; then
    kip_record PASS "Telemetry contract" "Runtime, GPU, Cilium, rules, alerts, and six dashboards were provisioned."
else
    kip_record FAIL "Telemetry contract" "Scrape resources, rules, or dashboards could not be applied."
    kip_summary "Observability installation"
    exit 1
fi

opencost_values="$KIP_OBSERVABILITY_ROOT/values/opencost-unpriced.yaml"
if [ -n "$KIP_COST_PROFILE" ]; then
    if ! kip_observability_cost_profile_matches_cluster; then
        kip_record FAIL "OpenCost pricing" "Profile GPU count does not match Kubernetes capacity."
        kip_summary "Observability installation"
        exit 1
    fi
    opencost_values="$KIP_WORK_DIR/opencost-priced.yaml"
    kip_observability_render_cost_values "$opencost_values"
fi
if kip_exec helm upgrade --install "$KIP_OPENCOST_RELEASE" \
    "$KIP_OPENCOST_CHART@$KIP_OPENCOST_CHART_DIGEST" \
    --namespace "$KIP_OBSERVABILITY_NAMESPACE" \
    --values "$opencost_values" --wait --timeout 10m; then
    if [ -n "$KIP_COST_PROFILE" ]; then
        if kip_observability_set_custom_cost_enabled true \
            && kip_observability_apply_cost_metadata \
            && kip_observability_profile_pricing_matches_release; then
            kip_record PASS "OpenCost pricing" "$KIP_COST_PROFILE configured at $KIP_COST_NODE_HOURLY_RATE_USD USD/node-hour."
        else
            kip_record FAIL "OpenCost pricing" \
                "OpenCost deployed, but its live price or reviewed profile metadata is not exact."
            kip_summary "Observability installation"
            exit 1
        fi
    else
        if kip_observability_set_custom_cost_enabled false; then
            kip_record WARN "OpenCost pricing" "OpenCost is healthy; pricing is intentionally not configured."
        else
            kip_record FAIL "OpenCost pricing" "Failed to reconcile OpenCost's unpriced state."
            kip_summary "Observability installation"
            exit 1
        fi
    fi
else
    kip_record FAIL "OpenCost pricing" "OpenCost Helm reconciliation failed."
    kip_summary "Observability installation"
    exit 1
fi

if kip_observability_workloads_ready \
    && kip_observability_pods_healthy \
    && kip_observability_component_images_exact \
    && kip_observability_storage_bound; then
    kip_record PASS "Observability workloads" "All monitoring and cost workloads are Ready with bound storage."
else
    kip_record FAIL "Observability workloads" "A workload is unhealthy or a persistent claim is unbound."
    kip_summary "Observability installation"
    exit 1
fi

if KIP_NO_LOGO=1 KIP_VERBOSE="$KIP_VERBOSE" "$KIP_OBSERVABILITY_ROOT/smoke.sh"; then
    kip_record PASS "Telemetry checkpoint" "APIs, targets, metric adapters, and dashboards passed smoke validation."
else
    kip_record FAIL "Telemetry checkpoint" "Observability smoke validation failed."
fi

kip_summary "Observability installation"
if [ "$KIP_FAILED" -ne 0 ]; then
    exit 1
fi
printf '\n%sCheckpoint passed:%s inference and infrastructure telemetry are queryable.\n' \
    "$KIP_COLOR_GREEN" "$KIP_COLOR_RESET"
if [ -z "$KIP_COST_PROFILE" ]; then
    printf '%sNext step:%s configure a reviewed whole-node cost profile before benchmarking.\n' \
        "$KIP_COLOR_CYAN" "$KIP_COLOR_RESET"
fi
