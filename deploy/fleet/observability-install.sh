#!/usr/bin/env bash
# Fleet-specific monitoring installation; run on the task-owned control plane.
# The existing v0.1 observability command remains single-node-only.
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
KIP_DEPLOY_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
KIP_REPOSITORY_ROOT=$(cd "$KIP_DEPLOY_ROOT/.." && pwd)
KIP_CLUSTER_ROOT="$KIP_DEPLOY_ROOT/cluster"
KIP_GPU_ROOT="$KIP_DEPLOY_ROOT/gpu"
KIP_OBSERVABILITY_ROOT="$KIP_DEPLOY_ROOT/observability"
readonly SCRIPT_DIR KIP_DEPLOY_ROOT KIP_REPOSITORY_ROOT KIP_CLUSTER_ROOT KIP_GPU_ROOT KIP_OBSERVABILITY_ROOT

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

export LC_ALL=C
# kubectl receives the target kubeconfig through the shared wrapper, while
# Helm reads KUBECONFIG directly. Keep both on the same owned cluster.
export KUBECONFIG="${KIP_KUBECONFIG:-/etc/kubernetes/admin.conf}"
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_FLEET_PROFILE=${KIP_FLEET_PROFILE:-vast}
KIP_WORK_DIR=""
KIP_OBSERVABILITY_NODE_NAME=kip-control
kip_ui_init
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
[ "$KIP_FLEET_PROFILE" = vast ] || [ "$KIP_FLEET_PROFILE" = lambda ] \
    || kip_usage_error 'KIP_FLEET_PROFILE must be vast or lambda.'
[ "$#" -eq 0 ] || kip_usage_error "fleet-observability-install accepts no positional arguments."
KIP_WORK_DIR=$(command mktemp -d "${TMPDIR:-/tmp}/kip-fleet-monitoring.XXXXXX")
readonly KIP_WORK_DIR
trap 'command rm -rf "$KIP_WORK_DIR"' EXIT
trap 'kip_unexpected_error "$?" "$LINENO"' ERR
if [ "$KIP_FLEET_PROFILE" = lambda ]; then
    KIP_OBSERVABILITY_NODE_NAME=$(command hostname)
    [[ "$KIP_OBSERVABILITY_NODE_NAME" =~ ^kip-lambda-[0-9a-f]{8}-control-speech$ ]] \
        || kip_usage_error 'Lambda monitoring must run on the owned east control plane.'
    KIP_FLEET_MONITORING_OVERLAY="$KIP_WORK_DIR/observability-values.yaml"
    command sed "s/__CONTROL_NODE__/$KIP_OBSERVABILITY_NODE_NAME/g" \
        "$KIP_DEPLOY_ROOT/lambda/observability-values.yaml.tpl" \
        >"$KIP_FLEET_MONITORING_OVERLAY"
    KIP_FLEET_MONITORING_MONITORS="$KIP_DEPLOY_ROOT/lambda/observability-monitors.yaml"
    KIP_FLEET_MONITORING_RULES="$KIP_DEPLOY_ROOT/lambda/observability-rules.yaml"
    KIP_FLEET_MONITORING_EPP_RBAC="$KIP_DEPLOY_ROOT/lambda/observability-epp-rbac.yaml"
    KIP_FLEET_MONITORING_DASHBOARD="$KIP_WORK_DIR/observability-dashboard.json"
    kip_exec python3 "$KIP_DEPLOY_ROOT/lambda/render-dashboard.py" \
        "$SCRIPT_DIR/observability-dashboard.json" \
        "$KIP_FLEET_MONITORING_DASHBOARD"
else
    KIP_FLEET_MONITORING_OVERLAY="$SCRIPT_DIR/observability-values.yaml"
    KIP_FLEET_MONITORING_MONITORS="$SCRIPT_DIR/observability-monitors.yaml"
    KIP_FLEET_MONITORING_RULES="$SCRIPT_DIR/observability-rules.yaml"
    KIP_FLEET_MONITORING_EPP_RBAC="$SCRIPT_DIR/observability-epp-rbac.yaml"
    KIP_FLEET_MONITORING_DASHBOARD="$SCRIPT_DIR/observability-dashboard.json"
fi
readonly KIP_FLEET_PROFILE KIP_FLEET_MONITORING_OVERLAY KIP_FLEET_MONITORING_MONITORS \
    KIP_FLEET_MONITORING_RULES KIP_FLEET_MONITORING_DASHBOARD \
    KIP_FLEET_MONITORING_EPP_RBAC

kip_fleet_monitoring_nodes_ready() {
    if [ "$KIP_FLEET_PROFILE" = lambda ]; then
        kip_kubectl get nodes -o json | kip_exec jq -e \
            --arg control "$KIP_OBSERVABILITY_NODE_NAME" '
            (.items | length) == 3
            and ([.items[].metadata.labels["kube-inference-platform.io/lambda-role"]] | sort)
                == ["control-speech", "large", "small-1"]
            and all(.items[];
                .spec.unschedulable != true
                and any(.status.conditions[]?;
                    .type == "Ready" and .status == "True")
                and (.status.allocatable["nvidia.com/gpu"] // "0") ==
                    (if .metadata.labels["kube-inference-platform.io/lambda-role"] == "large"
                        then "8" else "1" end))
            and any(.items[]; .metadata.name == $control)
        ' >/dev/null || return 1
        local deployment
        for deployment in qwen3-8b-small qwen3-8b-large qwen3-next-80b \
            kip-qwen3-8b-epp kip-qwen3-next-epp; do
            kip_kubectl -n kip-inference get deployment "$deployment" -o json \
                | kip_exec jq -e '.status.readyReplicas == 1' >/dev/null || return 1
        done
        return 0
    fi
    [ "$(command hostname)" = kip-control ] || return 1
    kip_kubectl get nodes -o json | kip_exec jq -e '
        ([.items[].metadata.name] | sort) == ["kip-control", "kip-gpu-1", "kip-gpu-2"]
        and all(.items[];
            .spec.unschedulable != true
            and any(.status.conditions[]?; .type == "Ready" and .status == "True")
            and (if .metadata.name == "kip-control" then
                (.status.allocatable["nvidia.com/gpu"] // "0") == "0"
                else (.status.allocatable["nvidia.com/gpu"] // "0") == "1" end))
    ' >/dev/null || return 1
    kip_kubectl -n kip-inference get deployment kip-qwen25-pilot -o json \
        | kip_exec jq -e '.status.readyReplicas == 2 and .status.availableReplicas == 2' \
        >/dev/null || return 1
    kip_kubectl -n kip-inference get deployment kip-qwen25-pilot-epp -o json \
        | kip_exec jq -e '.status.readyReplicas == 1' >/dev/null
}

kip_fleet_monitoring_release_safe() {
    local chart status values
    if ! kip_exec helm status "$KIP_MONITORING_RELEASE" \
        --namespace "$KIP_OBSERVABILITY_NAMESPACE" >/dev/null 2>&1; then
        # A foreign partial set of Prometheus CRDs must never be adopted.
        local crd
        for crd in $KIP_MONITORING_CRDS; do
            kip_kubectl get crd "$crd" >/dev/null 2>&1 && return 1
        done
        return 0
    fi
    status=$(kip_exec helm status "$KIP_MONITORING_RELEASE" \
        --namespace "$KIP_OBSERVABILITY_NAMESPACE" --output json \
        | kip_exec jq -er '.info.status') || return 1
    case "$status" in deployed | failed | pending-upgrade) ;; *) return 1 ;; esac
    chart=$(kip_exec helm list --namespace "$KIP_OBSERVABILITY_NAMESPACE" \
        --filter "^$KIP_MONITORING_RELEASE$" --output json \
        | kip_exec jq -er --arg release "$KIP_MONITORING_RELEASE" \
            '.[] | select(.name == $release) | .chart') || return 1
    [ "$chart" = "kube-prometheus-stack-$KIP_KUBE_PROMETHEUS_STACK_VERSION" ] \
        || return 1
    values=$(kip_exec helm get values "$KIP_MONITORING_RELEASE" \
        --namespace "$KIP_OBSERVABILITY_NAMESPACE" --output json) || return 1
    printf '%s' "$values" | kip_exec jq -e \
        --arg digest "$KIP_KUBE_PROMETHEUS_STACK_DIGEST" \
        '.kip.managedBy == "kube-inference-platform" and .kip.chartDigest == $digest' \
        >/dev/null || return 1
    kip_exec python3 "$SCRIPT_DIR/helm-values-check.py" \
        "$KIP_MONITORING_RELEASE" "$KIP_OBSERVABILITY_NAMESPACE" \
        "$KIP_OBSERVABILITY_ROOT/values/kube-prometheus-stack.yaml" \
        "$KIP_FLEET_MONITORING_OVERLAY"
}

kip_fleet_monitoring_named_resources_safe() {
    local name
    if kip_observability_namespace_exists && ! kip_observability_namespace_owned; then
        return 1
    fi
    kip_observability_storage_resources_safe || return 1
    kip_observability_resource_owned_or_absent secret kip-grafana-admin \
        "$KIP_OBSERVABILITY_NAMESPACE" || return 1
    if kip_kubectl get crd servicemonitors.monitoring.coreos.com >/dev/null 2>&1; then
        for name in kip-fleet-vllm kip-fleet-epp kip-fleet-dcgm; do
            kip_observability_resource_owned_or_absent servicemonitor "$name" \
                "$KIP_OBSERVABILITY_NAMESPACE" || return 1
        done
    fi
    kip_observability_resource_owned_or_absent clusterrolebinding \
        kip-fleet-prometheus-epp-metrics || return 1
    if [ "$KIP_FLEET_PROFILE" = lambda ]; then
        kip_observability_resource_owned_or_absent clusterrole \
            kip-epp-metrics-reader || return 1
        kip_observability_resource_owned_or_absent clusterrolebinding \
            kip-epp-auth-delegator || return 1
    fi
    kip_observability_resource_owned_or_absent configmap \
        kip-dashboard-llmd-fleet-pilot "$KIP_OBSERVABILITY_NAMESPACE" || return 1
    if kip_kubectl get crd prometheusrules.monitoring.coreos.com >/dev/null 2>&1; then
        kip_observability_resource_owned_or_absent prometheusrule \
            kip-fleet-inference "$KIP_OBSERVABILITY_NAMESPACE" || return 1
    fi
}

kip_fleet_monitoring_verify_chart() {
    local output actual
    output=$(kip_exec helm pull "$KIP_KUBE_PROMETHEUS_STACK_CHART" \
        --version "$KIP_KUBE_PROMETHEUS_STACK_VERSION" \
        --destination "$KIP_WORK_DIR" 2>&1) || return 1
    kip_verbose_output "Monitoring chart digest" "$output"
    actual=$(printf '%s\n' "$output" | command awk '/^Digest:[[:space:]]*/ {print $2; exit}')
    [ "$actual" = "$KIP_KUBE_PROMETHEUS_STACK_DIGEST" ]
}

kip_logo
kip_heading "Fleet observability · pilot"
kip_reset_results 10
if kip_fleet_monitoring_nodes_ready; then
    kip_record PASS "Fleet prerequisites" "Reviewed nodes, model replicas, and endpoint pickers are healthy."
else
    kip_record FAIL "Fleet prerequisites" "Reviewed nodes, model replicas, and endpoint pickers are required."
    kip_summary "Fleet observability installation"
    exit 1
fi
if kip_observability_required_commands_available \
    && kip_exec python3 -c 'import yaml' \
    && kip_observability_disk_ready; then
    kip_record PASS "Capacity and tools" "Pinned-tool prerequisites and 60 GiB monitoring headroom passed."
else
    kip_record FAIL "Capacity and tools" "Missing required tool or 60 GiB monitoring headroom."
    kip_summary "Fleet observability installation"
    exit 1
fi
if kip_fleet_monitoring_named_resources_safe && kip_fleet_monitoring_release_safe; then
    kip_record PASS "Ownership" "No unmanaged monitoring namespace, release, storage, or named resources."
else
    kip_record FAIL "Ownership" "Monitoring state is unmanaged or values have drifted."
    kip_summary "Fleet observability installation"
    exit 1
fi
if kip_fleet_monitoring_verify_chart; then
    kip_record PASS "Chart pin" "Prometheus stack tag matches its recorded OCI digest."
else
    kip_record FAIL "Chart pin" "Monitoring chart digest mismatch or retrieval failure."
    kip_summary "Fleet observability installation"
    exit 1
fi
if kip_observability_reconcile_dcgm_exporter; then
    kip_record PASS "DCGM Exporter" "Operator-owned pinned exporter enabled on GPU workers."
else
    kip_record FAIL "DCGM Exporter" "$KIP_OBSERVABILITY_DCGM_ERROR"
    kip_summary "Fleet observability installation"
    exit 1
fi
storage_manifest="$KIP_WORK_DIR/storage.yaml"
kip_observability_render_storage "$storage_manifest"
if kip_kubectl apply -f "$KIP_OBSERVABILITY_ROOT/manifests/namespace.yaml" >/dev/null \
    && kip_observability_host_storage_ready \
    && kip_kubectl apply -f "$storage_manifest" >/dev/null \
    && kip_observability_grafana_secret_ready; then
    kip_record PASS "Local storage" "Owned static volumes and Grafana Secret prepared on control plane."
else
    kip_record FAIL "Local storage" "Could not safely prepare monitoring storage or Secret."
    kip_summary "Fleet observability installation"
    exit 1
fi
if kip_exec helm upgrade --install "$KIP_MONITORING_RELEASE" \
    "$KIP_KUBE_PROMETHEUS_STACK_CHART@$KIP_KUBE_PROMETHEUS_STACK_DIGEST" \
    --namespace "$KIP_OBSERVABILITY_NAMESPACE" \
    --values "$KIP_OBSERVABILITY_ROOT/values/kube-prometheus-stack.yaml" \
    --values "$KIP_FLEET_MONITORING_OVERLAY" \
    --wait --timeout 20m \
    && kip_observability_mark_crds_owned; then
    kip_record PASS "Prometheus stack" "Prometheus, Grafana, and exporters installed internally."
else
    kip_record FAIL "Prometheus stack" "Helm convergence failed; live Pods remain for diagnosis."
    kip_summary "Fleet observability installation"
    exit 1
fi
if kip_kubectl apply -f "$KIP_FLEET_MONITORING_EPP_RBAC" >/dev/null \
    && kip_kubectl apply -f "$KIP_FLEET_MONITORING_MONITORS" >/dev/null; then
    kip_record PASS "Inference telemetry" "vLLM, llm-d EPP, and DCGM scrapes configured."
else
    kip_record FAIL "Inference telemetry" "Scrape monitors or EPP metrics RBAC failed."
    kip_summary "Fleet observability installation"
    exit 1
fi
dashboard=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" create configmap \
    kip-dashboard-llmd-fleet-pilot \
    --from-file="llmd-fleet-pilot.json=$KIP_FLEET_MONITORING_DASHBOARD" \
    --dry-run=client -o json)
dashboard=$(printf '%s\n' "$dashboard" | kip_exec jq -c \
    '.metadata.labels = {"app.kubernetes.io/managed-by":"kube-inference-platform","grafana_dashboard":"1"}')
if kip_kubectl apply -f "$KIP_FLEET_MONITORING_RULES" >/dev/null \
    && printf '%s\n' "$dashboard" | kip_kubectl apply -f - >/dev/null; then
    kip_record PASS "Fleet dashboard" "Recorded multi-node, tenant, queue, runtime, and GPU views provisioned."
else
    kip_record FAIL "Fleet dashboard" "Recording rules or Grafana provisioning failed."
    kip_summary "Fleet observability installation"
    exit 1
fi
if kip_fleet_monitoring_release_safe \
    && kip_observability_crds_owned \
    && kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get prometheus kip-monitoring-prometheus \
        >/dev/null 2>&1; then
    kip_record PASS "Exact rerun state" "Release values and owned CRDs match the fleet configuration."
else
    kip_record FAIL "Exact rerun state" "Release values or CRD ownership did not converge."
    kip_summary "Fleet observability installation"
    exit 1
fi
kip_summary "Fleet observability installation"
