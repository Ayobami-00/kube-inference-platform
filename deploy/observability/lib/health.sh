#!/usr/bin/env bash

# Helm workload, scrape target, and API health checks.

KIP_OBSERVABILITY_MISSING_METRIC=""

kip_observability_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        command sha256sum "$@"
    else
        command shasum -a 256 "$@"
    fi
}

kip_observability_workloads_ready() {
    kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" rollout status \
        deployment/kip-monitoring-operator --timeout=10m >/dev/null \
        && kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" rollout status \
            deployment/kip-monitoring-grafana --timeout=10m >/dev/null \
        && kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" rollout status \
            deployment/kip-opencost --timeout=10m >/dev/null \
        && kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" rollout status \
            statefulset/prometheus-kip-monitoring-prometheus --timeout=10m >/dev/null \
        && kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" rollout status \
            statefulset/alertmanager-kip-monitoring-alertmanager --timeout=10m >/dev/null
}

kip_observability_pods_healthy() {
    local pods

    pods=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get pods -o json 2>/dev/null) \
        || return 1
    printf '%s' "$pods" | kip_exec jq -e '
        (.items | length) > 0
        and all(.items[];
          (.status.phase == "Succeeded")
          or (.status.phase == "Running"
            and (.status.containerStatuses | length > 0)
            and all(.status.containerStatuses[]; .ready == true and (.state.running != null))))
    ' >/dev/null
}

kip_observability_component_images_exact() {
    local pods

    pods=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get pods -o json 2>/dev/null) \
        || return 1
    printf '%s' "$pods" | kip_exec jq -e \
        --arg prometheus "$KIP_PROMETHEUS_IMAGE_DIGEST" \
        --arg alertmanager "$KIP_ALERTMANAGER_IMAGE_DIGEST" \
        --arg operator "$KIP_PROMETHEUS_OPERATOR_IMAGE_DIGEST" \
        --arg reloader "$KIP_PROMETHEUS_CONFIG_RELOADER_IMAGE_DIGEST" \
        --arg grafana "$KIP_GRAFANA_IMAGE_DIGEST" \
        --arg sidecar "$KIP_GRAFANA_SIDECAR_IMAGE_DIGEST" \
        --arg busybox "$KIP_BUSYBOX_IMAGE_DIGEST" \
        --arg ksm "$KIP_KUBE_STATE_METRICS_IMAGE_DIGEST" \
        --arg node "$KIP_NODE_EXPORTER_IMAGE_DIGEST" \
        --arg opencost "${KIP_OPENCOST_IMAGE##*@}" '
        [.items[].spec | ((.initContainers // []) + (.containers // []))[] | .image] as $images
        | ($images | length) > 0
        and all($images[]; test("@sha256:[0-9a-f]{64}$"))
        and ([$prometheus,$alertmanager,$operator,$reloader,$grafana,$sidecar,$busybox,$ksm,$node,$opencost]
          | all(. as $digest | any($images[]; contains($digest))))
    ' >/dev/null
}

kip_observability_storage_bound() {
    local claims

    claims=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get pvc -o json 2>/dev/null) \
        || return 1
    printf '%s' "$claims" | kip_exec jq -e '
        ([.items[] | select(.status.phase == "Bound")] | length) >= 3
        and all(.items[]; .status.phase == "Bound")
    ' >/dev/null
}

kip_observability_crs_present() {
    local expected kind name object owner spec_hash

    kip_observability_crds_owned || return 1
    while IFS='|' read -r kind name expected; do
        object=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get "$kind" "$name" \
            -o json 2>/dev/null) || return 1
        owner=$(printf '%s' "$object" | kip_exec jq -er \
            '.metadata.labels["app.kubernetes.io/managed-by"] // ""') || return 1
        spec_hash=$(printf '%s' "$object" | kip_exec jq -cS -j '.spec' \
            | kip_observability_sha256 | command awk '{print $1}') || return 1
        [ "$owner" = kube-inference-platform ] && [ "$spec_hash" = "$expected" ] \
            || return 1
    done <<EOF
servicemonitor|kip-vllm|$KIP_SERVICEMONITOR_VLLM_SPEC_SHA256
servicemonitor|kip-dcgm-exporter|$KIP_SERVICEMONITOR_DCGM_SPEC_SHA256
servicemonitor|kip-cilium-agent|$KIP_SERVICEMONITOR_CILIUM_AGENT_SPEC_SHA256
servicemonitor|kip-cilium-operator|$KIP_SERVICEMONITOR_CILIUM_OPERATOR_SPEC_SHA256
prometheusrule|kip-inference-platform|$KIP_PROMETHEUS_RULE_SPEC_SHA256
EOF
}

kip_observability_prometheus_ready() {
    local path response

    path=$(kip_observability_service_proxy_path kip-monitoring-prometheus 9090 /-/ready)
    response=$(kip_observability_api_raw "$path") || return 1
    printf '%s' "$response" | command grep -qi 'ready'
}

kip_observability_grafana_ready() {
    local path response

    path=$(kip_observability_service_proxy_path kip-monitoring-grafana 80 /api/health)
    response=$(kip_observability_api_raw "$path") || return 1
    printf '%s' "$response" | kip_exec jq -e '.database == "ok"' >/dev/null
}

kip_observability_opencost_ready() {
    local path

    path=$(kip_observability_service_proxy_path kip-opencost 9003 /healthz)
    kip_observability_api_raw "$path" >/dev/null
}

kip_observability_targets_healthy() {
    local path response

    path=$(kip_observability_service_proxy_path kip-monitoring-prometheus 9090 \
        "/api/v1/targets?state=active")
    response=$(kip_observability_api_raw "$path") || return 1
    printf '%s' "$response" | kip_exec jq -e '
        def has_pool($fragment):
          any(.data.activeTargets[]?;
            (.scrapePool | contains($fragment)) and .health == "up");
        .status == "success"
        and (.data.activeTargets | length) > 0
        and all(.data.activeTargets[]; .health == "up")
        and has_pool("/kip-vllm/")
        and has_pool("/kip-dcgm-exporter/")
        and has_pool("/kip-cilium-agent/")
        and has_pool("/kip-cilium-operator/")
        and has_pool("/kip-opencost/")
        and has_pool("/kip-monitoring-prometheus-node-exporter/")
        and has_pool("/kip-monitoring-kube-state-metrics/")
        and has_pool("/kip-monitoring-kubelet/")
        and has_pool("/kip-monitoring-apiserver/")
        and has_pool("/kip-monitoring-coredns/")
        and has_pool("/kip-monitoring-operator/")
    ' >/dev/null
}

kip_observability_wait_for_targets() {
    local attempt=1 attempts=${KIP_OBSERVABILITY_TARGET_ATTEMPTS:-24}
    local interval=${KIP_OBSERVABILITY_TARGET_WAIT_SECONDS:-5}

    case "$attempts:$interval" in
        *[!0-9:]* | 0:* | *:0) return 2 ;;
    esac
    while [ "$attempt" -le "$attempts" ]; do
        if kip_observability_targets_healthy; then
            return 0
        fi
        kip_verbose_output "Scrape target convergence" \
            "attempt $attempt/$attempts incomplete; retrying in ${interval}s"
        [ "$attempt" -eq "$attempts" ] && break
        command sleep "$interval"
        attempt=$((attempt + 1))
    done
    return 1
}

kip_observability_query_json() {
    local encoded metric=$1 path

    encoded=$(kip_exec jq -rn --arg query "$metric" '$query | @uri') || return 1
    path=$(kip_observability_service_proxy_path kip-monitoring-prometheus 9090 \
        "/api/v1/query?query=$encoded")
    kip_observability_api_raw "$path"
}

kip_observability_query() {
    local metric=$1 response

    response=$(kip_observability_query_json "$metric") || return 1
    printf '%s' "$response" | kip_exec jq -e \
        '.status == "success" and (.data.result | length) > 0' >/dev/null
}

kip_observability_query_positive() {
    local metric=$1 response

    response=$(kip_observability_query_json "$metric") || return 1
    printf '%s' "$response" | kip_exec jq -e '
        .status == "success"
        and any(.data.result[]?; (.value[1] | tonumber? // 0) > 0)
    ' >/dev/null
}

kip_observability_required_metrics_ready() {
    local metric pricing_state

    KIP_OBSERVABILITY_MISSING_METRIC=""
    # These are the stable families consumed by the operational dashboards.
    # Query existence rather than positivity: queues, errors, XIDs, throttling,
    # restarts, and idle resource rates are legitimately zero on a healthy node.
    # DCGM profiling PCIe throughput is optional: some supported NVIDIA cards
    # expose ordinary device metrics but no DCGM_FI_PROF_PCIE_* series.
    while IFS= read -r metric; do
        [ -n "$metric" ] || continue
        if ! kip_observability_query "$metric"; then
            KIP_OBSERVABILITY_MISSING_METRIC=$metric
            kip_verbose_output "Missing metric contract" "$metric"
            return 1
        fi
    done <<'EOF'
up
kip:runtime:pod_info
kip:runtime:replicas_ready
kip:runtime:container_restarts
kip:requests:running
kip:requests:waiting
kip:requests:rate5m
kip:requests:success_rate5m
kip:requests:error_rate5m
kip:tokens:prompt_rate5m
kip:tokens:generation_rate5m
kip:tokens:total_rate5m
kip:kv_cache:usage_ratio
kip:preemptions:rate5m
kip:latency:ttft_seconds_p50
kip:latency:ttft_seconds_p95
kip:latency:ttft_seconds_p99
kip:latency:tpot_seconds_p50
kip:latency:tpot_seconds_p95
kip:latency:tpot_seconds_p99
kip:latency:e2e_seconds_p50
kip:latency:e2e_seconds_p95
kip:latency:e2e_seconds_p99
kip:latency:queue_seconds_p50
kip:latency:queue_seconds_p95
kip:latency:queue_seconds_p99
kip:latency:prefill_seconds_p50
kip:latency:prefill_seconds_p95
kip:latency:prefill_seconds_p99
kip:latency:decode_seconds_p50
kip:latency:decode_seconds_p95
kip:latency:decode_seconds_p99
kip:gpu:utilization_ratio
kip:gpu:memory_used_bytes
kip:gpu:memory_total_bytes
kip:gpu:power_watts
kip:gpu:temperature_celsius
kip:gpu:xid_errors
kip:gpu:clock_throttle
kip:gpu:sm_clock_mhz
kip:gpu:memory_clock_mhz
kip:gpu:runtime_utilization_ratio
kip:gpu:runtime_memory_used_bytes
kip:gpu:runtime_memory_total_bytes
kip:gpu:runtime_power_watts
kip:gpu:runtime_temperature_celsius
kip:gpu:runtime_xid_errors
kip:gpu:runtime_clock_throttle
kip:gpu:runtime_sm_clock_mhz
kip:gpu:runtime_memory_clock_mhz
kip:kubernetes:nodes_ready
kip:kubernetes:pods_unready
kip:node:cpu_utilization_ratio
kip:node:memory_utilization_ratio
kip:node:filesystem_available_ratio
kip:node:network_receive_bytes_per_second
kip:node:network_transmit_bytes_per_second
{__name__=~"cilium_.+"}
EOF

    pricing_state=$(kip_observability_release_pricing_state) || return 1
    if [ "$pricing_state" = unpriced ]; then
        return 0
    fi
    # A priced stack must expose the reviewed positive GPU rate as well as the
    # OpenCost inference allocation/cost families used by dashboards/reports.
    kip_observability_active_profile_json >/dev/null 2>&1 \
        && kip_observability_query_positive node_gpu_hourly_cost \
        && kip_observability_query_positive 'kip:cost:cluster_hourly_usd' \
        && kip_observability_query 'count(kip:cost:inference_hourly_usd{cost_basis=~"allocation|usage"}) >= 2' \
        && kip_observability_query 'count(kip:cost:per_request_usd{cost_basis=~"allocation|usage"}) >= 2' \
        && kip_observability_query 'count(kip:cost:per_million_tokens_usd{phase=~"prompt|generation"}) >= 2' \
        && kip_observability_query 'kip:cost:per_million_tokens_usd{phase=""}'
}

kip_observability_wait_for_required_metrics() {
    local attempt=1 attempts=${KIP_OBSERVABILITY_METRICS_ATTEMPTS:-24}
    local interval=${KIP_OBSERVABILITY_METRICS_WAIT_SECONDS:-10}

    case "$attempts:$interval" in
        *[!0-9:]* | 0:* | *:0) return 2 ;;
    esac
    while [ "$attempt" -le "$attempts" ]; do
        if kip_observability_required_metrics_ready; then
            return 0
        fi
        kip_verbose_output "Telemetry convergence" \
            "attempt $attempt/$attempts incomplete; retrying in ${interval}s"
        [ "$attempt" -eq "$attempts" ] && break
        command sleep "$interval"
        attempt=$((attempt + 1))
    done
    return 1
}

kip_observability_dashboards_present() {
    local actual_hash dashboard expected_hash key name object owner resources

    resources=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get configmaps \
        -l 'grafana_dashboard=1,app.kubernetes.io/managed-by=kube-inference-platform' \
        -o json 2>/dev/null) || return 1
    printf '%s' "$resources" | kip_exec jq -e '
        [.items[].metadata.name] | sort == [
          "kip-dashboard-benchmark-explorer",
          "kip-dashboard-distributed-topology",
          "kip-dashboard-gpu-node-health",
          "kip-dashboard-inference-cost",
          "kip-dashboard-model-serving",
          "kip-dashboard-platform-fleet"
        ]
    ' >/dev/null || return 1

    for dashboard in "$KIP_OBSERVABILITY_ROOT"/dashboards/*.json; do
        name=$(basename "$dashboard" .json)
        key="$name.json"
        object=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get configmap \
            "kip-dashboard-$name" -o json 2>/dev/null) || return 1
        owner=$(printf '%s' "$object" | kip_exec jq -er \
            '.metadata.labels["app.kubernetes.io/managed-by"] // ""') || return 1
        [ "$owner" = kube-inference-platform ] || return 1
        printf '%s' "$object" | kip_exec jq -e --arg key "$key" \
            '(.data | keys) == [$key]' >/dev/null || return 1
        actual_hash=$(printf '%s' "$object" | kip_exec jq -rj --arg key "$key" \
            '.data[$key]' | kip_observability_sha256 | command awk '{print $1}') || return 1
        expected_hash=$(kip_observability_sha256 "$dashboard" | command awk '{print $1}') \
            || return 1
        [ "$actual_hash" = "$expected_hash" ] || return 1
    done
}

kip_observability_network_surface_internal() {
    local ingresses services

    services=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get services -o json \
        2>/dev/null) || return 1
    printf '%s' "$services" | kip_exec jq -e '
        (.items | length) > 0
        and all(.items[];
          (.spec.type // "ClusterIP") == "ClusterIP"
          and ((.spec.externalIPs // []) | length) == 0
          and ((.status.loadBalancer.ingress // []) | length) == 0
          and all(.spec.ports[]?; has("nodePort") | not))
    ' >/dev/null || return 1

    ingresses=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get ingresses -o json \
        2>/dev/null) || return 1
    printf '%s' "$ingresses" | kip_exec jq -e '(.items | length) == 0' >/dev/null
}
