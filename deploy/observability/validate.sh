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
source "$KIP_GPU_ROOT/versions.env"
source "$KIP_CLUSTER_ROOT/lib/common.sh"
source "$KIP_CLUSTER_ROOT/lib/kubernetes.sh"
source "$KIP_GPU_ROOT/lib/common.sh"
source "$KIP_OBSERVABILITY_ROOT/lib/common.sh"
source "$KIP_OBSERVABILITY_ROOT/lib/dcgm.sh"
source "$KIP_OBSERVABILITY_ROOT/lib/health.sh"

export LC_ALL=C
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_WORK_DIR=""

kip_ui_init
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
[ "$#" -eq 0 ] || kip_usage_error "observability-check does not accept positional arguments."
KIP_WORK_DIR=$(command mktemp -d "${TMPDIR:-/tmp}/kip-observability-check.XXXXXX")
readonly KIP_WORK_DIR
trap 'command rm -rf "$KIP_WORK_DIR"' EXIT
trap 'kip_unexpected_error "$?" "$LINENO"' ERR

kip_logo
kip_heading "Observability and Cost Accounting Validation · v0.1"
kip_reset_results 13

if kip_observability_required_commands_available; then
    kip_record PASS "Inspection commands" "Required read-only inspection commands are available."
else
    kip_record FAIL "Inspection commands" "helm, kubectl, jq, curl, and checksum tools are required."
fi

if kip_observability_run_cluster_check >/dev/null \
    && kip_observability_run_gpu_check >/dev/null \
    && kip_observability_run_runtime_check >/dev/null; then
    kip_record PASS "Platform prerequisites" "Cluster, GPU, and runtime remain healthy."
else
    kip_record FAIL "Platform prerequisites" "A prerequisite platform validation failed."
fi

if kip_observability_namespace_owned \
    && kip_observability_release_exact "$KIP_MONITORING_RELEASE" \
    && kip_observability_release_exact "$KIP_OPENCOST_RELEASE"; then
    kip_record PASS "Pinned Helm releases" "Monitoring $KIP_KUBE_PROMETHEUS_STACK_VERSION and OpenCost $KIP_OPENCOST_CHART_VERSION are deployed."
else
    kip_record FAIL "Pinned Helm releases" "Namespace ownership, release state, or chart versions have drifted."
fi

if kip_observability_dcgm_exporter_exact; then
    kip_record PASS "DCGM Exporter" "GPU Operator owns the exact one-second, digest-pinned exporter workload."
else
    kip_record FAIL "DCGM Exporter" "${KIP_OBSERVABILITY_DCGM_ERROR:-The exporter configuration has drifted.}"
fi

if kip_observability_pods_healthy; then
    kip_record PASS "Workload health" "All observability Pods are currently healthy."
else
    kip_record FAIL "Workload health" "A monitoring or cost Pod is absent, pending, failed, or unready."
fi

if kip_observability_component_images_exact; then
    kip_record PASS "Immutable workload images" "Every running component uses its recorded linux/amd64 image digest."
else
    kip_record FAIL "Immutable workload images" "An observability Pod image is mutable, missing, or digest-drifted."
fi

if kip_observability_storage_bound; then
    kip_record PASS "Persistent storage" "Prometheus, Grafana, and Alertmanager claims are bound."
else
    kip_record FAIL "Persistent storage" "A required persistent claim is absent or unbound."
fi

if kip_observability_crs_present; then
    kip_record PASS "Telemetry resources" "Runtime/GPU ServiceMonitors and KIP recording rules are present."
else
    kip_record FAIL "Telemetry resources" "A KIP ServiceMonitor or PrometheusRule is missing."
fi

if kip_observability_prometheus_ready \
    && kip_observability_grafana_ready \
    && kip_observability_opencost_ready; then
    kip_record PASS "Internal APIs" "Prometheus, Grafana, and OpenCost health endpoints passed."
else
    kip_record FAIL "Internal APIs" "An internal observability API is not ready."
fi

if kip_observability_targets_healthy; then
    kip_record PASS "Prometheus targets" "Every active target is up and all required platform scrape pools are present."
else
    kip_record FAIL "Prometheus targets" "A target is down or a required runtime, GPU, network, node, or Kubernetes pool is missing."
fi

if kip_observability_dashboards_present; then
    kip_record PASS "Provisioned dashboards" "Six KIP dashboards are available to Grafana."
else
    kip_record FAIL "Provisioned dashboards" \
        "A managed dashboard name, label, key, or JSON payload differs from the committed set."
fi

if kip_observability_network_surface_internal; then
    kip_record PASS "Internal network surface" \
        "Every observability Service is ClusterIP-only and no Ingress is present."
else
    kip_record FAIL "Internal network surface" \
        "A Service exposes NodePort/external addresses or an Ingress is present."
fi

pricing_state=$(kip_observability_release_pricing_state 2>/dev/null) || pricing_state=invalid
if kip_observability_active_profile_json >/dev/null 2>&1; then
    if kip_observability_load_active_profile \
        && kip_observability_cost_profile_matches_cluster \
        && kip_observability_profile_metadata_matches_release \
        && kip_observability_profile_pricing_matches_release; then
        kip_record PASS "Cost profile" "$KIP_COST_PROFILE at $KIP_COST_NODE_HOURLY_RATE_USD USD/node-hour."
    else
        kip_record FAIL "Cost profile" "The active profile, Helm metadata, or GPU capacity has drifted."
    fi
elif [ "$pricing_state" = priced ]; then
    kip_record FAIL "Cost profile" "OpenCost is priced but its reviewed KIP profile metadata is missing."
elif [ "$pricing_state" = unpriced ]; then
    kip_record WARN "Cost profile" "Pricing not configured; benchmark publication remains blocked."
else
    kip_record FAIL "Cost profile" "OpenCost pricing state is missing or invalid."
fi

kip_summary "Observability validation"
[ "$KIP_FAILED" -eq 0 ]
