#!/usr/bin/env bash

# Shared GPU configuration, Helm-state, and validation helpers.

KIP_GPU_NAMESPACE="gpu-operator"
KIP_GPU_RELEASE="gpu-operator"
KIP_GPU_NODE_NAME=""
KIP_PHYSICAL_GPU_COUNT=0
KIP_GPU_PREFLIGHT_ERROR=""

kip_gpu_validate_driver_mode() {
    case "$1" in
        operator | preinstalled) return 0 ;;
        *) kip_usage_error "GPU_DRIVER_MODE must be operator or preinstalled; received '$1'." ;;
    esac
}

kip_gpu_required_commands_available() {
    local command_name

    for command_name in curl helm jq kubectl lspci sha256sum; do
        kip_has_command "$command_name" || return 1
    done
}

kip_gpu_helm_status_json() {
    kip_exec helm status "$KIP_GPU_RELEASE" \
        --namespace "$KIP_GPU_NAMESPACE" \
        --output json 2>/dev/null
}

kip_gpu_helm_values_json() {
    kip_exec helm get values "$KIP_GPU_RELEASE" \
        --namespace "$KIP_GPU_NAMESPACE" \
        --output json 2>/dev/null
}

kip_gpu_release_exists() {
    kip_gpu_helm_status_json >/dev/null 2>&1
}

kip_gpu_recorded_mode() {
    local values

    values=$(kip_gpu_helm_values_json) || return 1
    printf '%s' "$values" | kip_exec jq -er '.kip.driverMode'
}

kip_gpu_release_is_exact() {
    local baseline_matches driver_enabled expected_driver_enabled recorded_digest recorded_mode
    local chart status
    local list_json status_json values_json

    status_json=$(kip_gpu_helm_status_json) || return 1
    list_json=$(kip_exec helm list \
        --namespace "$KIP_GPU_NAMESPACE" \
        --filter "^$KIP_GPU_RELEASE$" \
        --output json 2>/dev/null) || return 1
    values_json=$(kip_gpu_helm_values_json) || return 1
    status=$(printf '%s' "$status_json" | kip_exec jq -er '.info.status') || return 1
    chart=$(printf '%s' "$list_json" | kip_exec jq -er \
        --arg name "$KIP_GPU_RELEASE" \
        --arg namespace "$KIP_GPU_NAMESPACE" \
        '.[] | select(.name == $name and .namespace == $namespace) | .chart') || return 1
    recorded_mode=$(printf '%s' "$values_json" | kip_exec jq -er '.kip.driverMode') || return 1
    recorded_digest=$(printf '%s' "$values_json" | kip_exec jq -er '.kip.chartDigest') || return 1
    driver_enabled=$(printf '%s' "$values_json" | kip_exec jq -r '.driver.enabled') || return 1
    baseline_matches=$(printf '%s' "$values_json" | kip_exec jq -r \
        --arg driverVersion "$KIP_NVIDIA_DRIVER_VERSION" '
        (.clusterPolicy.deployCR == true)
        and (.gpuCluster.deployCR == false)
        and (.psa.enabled == true)
        and (.nfd.enabled == true)
        and (.cdi.enabled == true)
        and (.cdi.nriPluginEnabled == false)
        and (.toolkit.enabled == true)
        and (.devicePlugin.enabled == true)
        and (.dcgm.enabled == false)
        and (.dcgmExporter.enabled == false)
        and (.mig.strategy == "none")
        and (.migManager.enabled == false)
        and (.driver.version == $driverVersion)
        and any(.toolkit.env[];
            .name == "CONTAINERD_CONFIG" and .value == "/etc/containerd/config.toml")
        and any(.toolkit.env[];
            .name == "CONTAINERD_SOCKET" and .value == "/run/containerd/containerd.sock")
        and any(.toolkit.env[];
            .name == "RUNTIME_CONFIG_SOURCE"
            and .value == "file=/etc/containerd/config.toml")
        and any(.toolkit.env[];
            .name == "RUNTIME_DROP_IN_CONFIG"
            and .value == "/etc/containerd/conf.d/99-nvidia.toml")' 2>/dev/null) || return 1

    [ "$KIP_GPU_DRIVER_MODE" = operator ] && expected_driver_enabled=true \
        || expected_driver_enabled=false
    case "$driver_enabled" in
        true | false) ;;
        *) return 1 ;;
    esac
    [ "$status" = deployed ] \
        && [ "$chart" = "gpu-operator-$KIP_GPU_OPERATOR_VERSION" ] \
        && [ "$recorded_mode" = "$KIP_GPU_DRIVER_MODE" ] \
        && [ "$recorded_digest" = "$KIP_GPU_OPERATOR_CHART_DIGEST" ] \
        && [ "$driver_enabled" = "$expected_driver_enabled" ] \
        && [ "$baseline_matches" = true ]
}

kip_gpu_resolve_mode() {
    local recorded

    if [ -n "${KIP_GPU_DRIVER_MODE:-}" ]; then
        kip_gpu_validate_driver_mode "$KIP_GPU_DRIVER_MODE"
        return
    fi
    recorded=$(kip_gpu_recorded_mode) || return 1
    kip_gpu_validate_driver_mode "$recorded"
    KIP_GPU_DRIVER_MODE=$recorded
}

kip_gpu_host_driver_healthy() {
    local inventory topology

    kip_has_command nvidia-smi || return 1
    inventory=$(kip_exec nvidia-smi --query-gpu=index,name,uuid,driver_version \
        --format=csv,noheader 2>/dev/null) || return 1
    [ -n "$inventory" ] || return 1
    topology=$(kip_exec nvidia-smi topo -m 2>/dev/null) || return 1
    [ -n "$topology" ]
}

kip_gpu_running_kernel_headers_available() {
    local kernel output

    kip_has_command apt-cache || return 1
    kernel=$(kip_exec uname -r 2>/dev/null) || return 1
    [ -n "$kernel" ] || return 1
    output=$(kip_exec apt-cache madison "linux-headers-$kernel" 2>/dev/null) || return 1
    [ -n "$output" ]
}

kip_gpu_detect_physical_hardware() {
    local devices

    devices=$(kip_exec lspci -Dn 2>/dev/null) || return 1
    KIP_PHYSICAL_GPU_COUNT=$(printf '%s\n' "$devices" | command awk '
        $2 ~ /^03(00|02|80):$/ && tolower($3) ~ /^10de:/ {count++}
        END {print count + 0}
    ')
    [ "$KIP_PHYSICAL_GPU_COUNT" -gt 0 ]
}

kip_gpu_single_ready_node() {
    local output

    output=$(kip_kubectl get nodes \
        -o 'jsonpath={range .items[*]}{.metadata.name}|{.spec.unschedulable}|{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' \
        2>/dev/null) || return 1
    [ "$(printf '%s\n' "$output" | command awk 'NF {count++} END {print count + 0}')" -eq 1 ] \
        || return 1
    printf '%s\n' "$output" | command awk -F'|' \
        'NF == 3 && $1 != "" && $2 != "true" && $3 == "True" {good=1} END {exit !good}'
}

kip_gpu_no_partitioning_or_sharing() {
    local configs labels resources node_json

    node_json=$(kip_kubectl get nodes -o json 2>/dev/null) || return 1
    resources=$(printf '%s' "$node_json" | kip_exec jq -r '
        .items[] | ((.status.capacity // {}) + (.status.allocatable // {}) | keys[])' \
        2>/dev/null) || return 1
    if printf '%s\n' "$resources" | command grep -Eq '^nvidia\.com/mig-'; then
        return 1
    fi
    labels=$(printf '%s' "$node_json" | kip_exec jq -r '
        .items[] | .metadata.labels["nvidia.com/mig.config"] // ""' 2>/dev/null) || return 1
    if printf '%s\n' "$labels" | command awk 'NF && $0 != "all-disabled" {bad=1} END {exit bad}'; then
        :
    else
        return 1
    fi

    configs=$(kip_kubectl get configmaps --all-namespaces -o json 2>/dev/null) || return 1
    configs=$(printf '%s' "$configs" | kip_exec jq -r '
        .items[]
        | select((.metadata.name | test("device-plugin"; "i"))
            or (.metadata.name == "kip-whole-gpu"))
        | .data[]? // empty' 2>/dev/null) || return 1
    ! printf '%s\n' "$configs" | command grep -Eq '(^|[[:space:]])(sharing:|replicas:)'
}
