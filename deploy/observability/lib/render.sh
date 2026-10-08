#!/usr/bin/env bash

# Render and apply storage, dashboards, and cost values.

kip_observability_render_storage() {
    local destination=$1

    command sed \
        -e "s|__STORAGE_ROOT__|$KIP_OBSERVABILITY_STORAGE_ROOT|g" \
        -e "s|__NODE_NAME__|$KIP_OBSERVABILITY_NODE_NAME|g" \
        "$KIP_OBSERVABILITY_ROOT/manifests/storage.yaml.tpl" > "$destination"
}

kip_observability_render_cost_values() {
    local destination=$1

    command sed \
        -e "s|__PROFILE_NAME__|$KIP_COST_PROFILE_NAME|g" \
        -e "s|__PROFILE_HASH__|$KIP_COST_PROFILE_HASH|g" \
        -e "s|__EFFECTIVE_FROM__|$KIP_COST_EFFECTIVE_FROM|g" \
        -e "s|__GPU_HOURLY_RATE_USD__|$KIP_COST_GPU_HOURLY_RATE_USD|g" \
        "$KIP_OBSERVABILITY_ROOT/values/opencost-priced.yaml.tpl" > "$destination"
}

kip_observability_apply_dashboards() {
    local dashboard name output

    for dashboard in "$KIP_OBSERVABILITY_ROOT"/dashboards/*.json; do
        name=$(basename "$dashboard" .json)
        output=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" create configmap \
            "kip-dashboard-$name" \
            --from-file="$name.json=$dashboard" \
            --dry-run=client -o json) || return 1
        output=$(printf '%s\n' "$output" | kip_exec jq -c \
            '.metadata.labels = {
                "app.kubernetes.io/managed-by":"kube-inference-platform",
                "grafana_dashboard":"1"
            }') || return 1
        printf '%s\n' "$output" | kip_kubectl apply -f - >/dev/null || return 1
    done
}

kip_observability_apply_cost_metadata() {
    local configmap rule

    configmap=$(kip_exec jq -n \
        --arg namespace "$KIP_OBSERVABILITY_NAMESPACE" \
        --arg profile "$(kip_exec jq -cS . "$KIP_COST_PROFILE_FILE")" \
        '{apiVersion:"v1",kind:"ConfigMap",metadata:{name:"kip-cost-profile",namespace:$namespace,labels:{"app.kubernetes.io/managed-by":"kube-inference-platform"}},data:{"profile.json":$profile}}') \
        || return 1
    printf '%s\n' "$configmap" | kip_kubectl apply -f - >/dev/null || return 1

    rule=$(kip_exec jq -n \
        --arg namespace "$KIP_OBSERVABILITY_NAMESPACE" \
        --arg profile "$KIP_COST_PROFILE_NAME" \
        --arg hash "$KIP_COST_PROFILE_HASH" \
        --arg provider "$KIP_COST_PROVIDER" \
        --arg machine "$KIP_COST_MACHINE_TYPE" \
        --arg region "$KIP_COST_REGION" \
        --arg source "$KIP_COST_SOURCE" \
        --arg effective "$KIP_COST_EFFECTIVE_FROM" \
        --arg rate "$KIP_COST_NODE_HOURLY_RATE_USD" \
        --arg gpuCount "$KIP_COST_PHYSICAL_GPU_COUNT" '
        {
          apiVersion:"monitoring.coreos.com/v1", kind:"PrometheusRule",
          metadata:{name:"kip-cost-profile",namespace:$namespace,labels:{"app.kubernetes.io/managed-by":"kube-inference-platform",release:"kip-monitoring"}},
          spec:{groups:[{name:"kip.cost.profile",interval:"1m",rules:[{
            record:"kip:cost:profile_info",expr:"vector(1)",labels:{
              profile:$profile,profile_hash:$hash,provider:$provider,machine_type:$machine,
              region:$region,source:$source,effective_from:$effective,currency:"USD",
              node_hourly_rate_usd:$rate,physical_gpu_count:$gpuCount,power_included_in_rate:"true"
            }
          }]}]}
        }') || return 1
    printf '%s\n' "$rule" | kip_kubectl apply -f - >/dev/null
}
