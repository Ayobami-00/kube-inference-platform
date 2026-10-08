#!/usr/bin/env bash

# Read-only GPU Operator, operand, and resource health checks.

kip_gpu_cluster_policy_ready() {
    local state

    state=$(kip_kubectl get clusterpolicy cluster-policy \
        -o 'jsonpath={.status.state}' 2>/dev/null) || return 1
    [ "$state" = ready ]
}

kip_gpu_operand_pods_healthy() {
    local dcgm_state output required pattern

    output=$(kip_kubectl -n "$KIP_GPU_NAMESPACE" get pods \
        -o 'jsonpath={range .items[*]}{.metadata.name}|{.status.phase}|{range .status.containerStatuses[*]}{.ready}:{.state.waiting.reason},{end}{"\n"}{end}' \
        2>/dev/null) || return 1
    [ -n "$output" ] || return 1
    kip_verbose_output "kubectl get pods -n $KIP_GPU_NAMESPACE" "$output"
    printf '%s\n' "$output" | command awk -F'|' '
        $2 == "Succeeded" {next}
        $2 != "Running" || $3 !~ /true:/ || $3 ~ /false:/ ||
            $3 ~ /(CrashLoopBackOff|ImagePullBackOff|ErrImagePull)/ {bad=1}
        END {exit bad}
    ' || return 1

    required='gpu-operator node-feature-discovery nvidia-container-toolkit nvidia-device-plugin gpu-feature-discovery nvidia-operator-validator'
    dcgm_state=$(kip_gpu_dcgm_exporter_state) || return 1
    if [ "$dcgm_state" = true ]; then
        required="$required nvidia-dcgm-exporter"
    fi
    if [ "$KIP_GPU_DRIVER_MODE" = operator ]; then
        required="$required nvidia-driver"
    fi
    for pattern in $required; do
        printf '%s\n' "$output" | command grep -Fq "$pattern" || return 1
    done
}

kip_gpu_capacity_matches_hardware() {
    local allocatable capacity node_json rows

    node_json=$(kip_kubectl get nodes -o json 2>/dev/null) || return 1
    rows=$(printf '%s' "$node_json" | kip_exec jq -r '
        .items[]
        | [.metadata.name,
           (.status.capacity["nvidia.com/gpu"] // "0"),
           (.status.allocatable["nvidia.com/gpu"] // "0")]
        | @tsv' 2>/dev/null) || return 1
    [ "$(printf '%s\n' "$rows" | command awk 'NF {count++} END {print count + 0}')" -eq 1 ] \
        || return 1
    KIP_GPU_NODE_NAME=$(printf '%s\n' "$rows" | command awk -F'\t' 'NR == 1 {print $1}')
    capacity=$(printf '%s\n' "$rows" | command awk -F'\t' 'NR == 1 {print $2}')
    allocatable=$(printf '%s\n' "$rows" | command awk -F'\t' 'NR == 1 {print $3}')
    case "$capacity:$allocatable" in
        *[!0-9:]*) return 1 ;;
    esac
    [ "$capacity" -gt 0 ] \
        && [ "$capacity" -eq "$allocatable" ] \
        && [ "$capacity" -eq "$KIP_PHYSICAL_GPU_COUNT" ]
}

kip_gpu_wait_until_ready() {
    local deadline interval now timeout

    timeout=${KIP_GPU_WAIT_TIMEOUT_SECONDS:-1500}
    interval=${KIP_GPU_WAIT_INTERVAL_SECONDS:-10}
    case "$timeout:$interval" in
        *[!0-9:]*) return 1 ;;
    esac
    [ "$interval" -gt 0 ] || return 1
    deadline=$(( $(command date +%s) + timeout ))

    while true; do
        if kip_gpu_cluster_policy_ready \
            && kip_gpu_operand_pods_healthy \
            && kip_gpu_capacity_matches_hardware; then
            return 0
        fi
        now=$(command date +%s)
        [ "$now" -lt "$deadline" ] || return 1
        command sleep "$interval"
    done
}
