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
[ "$#" -eq 0 ] || kip_usage_error "observability-smoke does not accept positional arguments."
KIP_WORK_DIR=$(command mktemp -d "${TMPDIR:-/tmp}/kip-observability-smoke.XXXXXX")
readonly KIP_WORK_DIR
trap 'command rm -rf "$KIP_WORK_DIR"' EXIT
trap 'kip_unexpected_error "$?" "$LINENO"' ERR

kip_logo
kip_heading "Observability Telemetry Smoke Test · v0.1"
kip_reset_results 8

if kip_observability_release_exact "$KIP_MONITORING_RELEASE" \
    && kip_observability_release_exact "$KIP_OPENCOST_RELEASE" \
    && kip_observability_dcgm_exporter_exact; then
    kip_record PASS "Observability prerequisite" "Pinned monitoring, cost, and GPU telemetry releases are deployed."
else
    kip_record FAIL "Observability prerequisite" "Run observability-install before smoke testing."
    kip_summary "Observability smoke test"
    exit 1
fi

if kip_observability_prometheus_ready \
    && kip_observability_grafana_ready \
    && kip_observability_opencost_ready; then
    kip_record PASS "API readiness" "Prometheus, Grafana, and OpenCost responded through Kubernetes API proxying."
else
    kip_record FAIL "API readiness" "One or more internal APIs failed their health check."
fi

if kip_observability_wait_for_targets; then
    kip_record PASS "Scrape target health" "All active targets are up and the required scrape pools are present."
else
    kip_record FAIL "Scrape target health" "Prometheus reports a down target or a required scrape pool is absent."
fi

# Prometheus is installed after the runtime checkpoint. Generate a fresh,
# sanitized inference transaction while the five-second vLLM target is live so
# latency histograms, token counters, and OpenCost inference attribution can be
# accepted from observed data instead of merely from configured scrape targets.
if KIP_NO_LOGO=1 KIP_VERBOSE="$KIP_VERBOSE" KIP_RUNTIME=vllm \
    KIP_MODEL_PROFILE=qwen2.5-7b-instruct \
    "$KIP_RUNTIME_ROOT/dispatch.sh" smoke >/dev/null; then
    kip_record PASS "Inference telemetry stimulus" \
        "A temporary in-cluster client exercised health, model, completion, and streaming paths."
else
    kip_record FAIL "Inference telemetry stimulus" \
        "The temporary runtime smoke transaction failed; latency and token telemetry cannot be accepted."
fi

kip_note "Waiting up to four minutes for scrape, inference-cost, and recording-rule convergence."
if kip_observability_wait_for_required_metrics; then
    kip_record PASS "Required telemetry" "Runtime, GPU, node, and cost metric families are queryable."
else
    kip_record FAIL "Required telemetry" \
        "A required metric family remained absent after the bounded convergence wait: ${KIP_OBSERVABILITY_MISSING_METRIC:-unknown}."
fi

if kip_observability_dashboards_present \
    && kip_observability_crs_present; then
    kip_record PASS "Operational views" "Dashboards, adapters, and alert rules are provisioned."
else
    kip_record FAIL "Operational views" "Dashboard or telemetry resources are incomplete."
fi

if kip_observability_network_surface_internal; then
    kip_record PASS "Internal network surface" \
        "Observability APIs remain reachable only through ClusterIP Services."
else
    kip_record FAIL "Internal network surface" \
        "A public Service or Ingress violates the internal-only contract."
fi

if kip_observability_run_cluster_check >/dev/null \
    && kip_observability_run_gpu_check >/dev/null \
    && kip_observability_run_runtime_check >/dev/null; then
    kip_record PASS "Final platform health" "Cluster, GPU, and runtime remain healthy after telemetry checks."
else
    kip_record FAIL "Final platform health" "A platform validation failed after observability checks."
fi

kip_summary "Observability smoke test"
[ "$KIP_FAILED" -eq 0 ]
