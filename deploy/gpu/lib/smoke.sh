#!/usr/bin/env bash

# Temporary one-GPU nvidia-smi workload and failure diagnostics.

kip_gpu_smoke_diagnostics() {
    local output

    printf '\nGPU smoke diagnostics:\n' >&2
    for command_name in describe logs events; do
        case "$command_name" in
            describe)
                output=$(kip_kubectl -n default describe pod kip-gpu-smoke 2>&1 || true)
                ;;
            logs)
                output=$(kip_kubectl -n default logs kip-gpu-smoke --all-containers 2>&1 || true)
                ;;
            events)
                output=$(kip_kubectl -n default get events \
                    --field-selector involvedObject.name=kip-gpu-smoke \
                    --sort-by=.lastTimestamp 2>&1 || true)
                ;;
        esac
        printf '%s\n%s\n' "--- $command_name ---" "${output:-no output}" >&2
    done
}

kip_gpu_run_smoke() {
    local cleanup_result=0 logs result=0 smoke_node

    kip_kubectl -n default delete pod kip-gpu-smoke \
        --ignore-not-found --wait=true >/dev/null 2>&1 || return 1
    if ! kip_kubectl apply -f "$KIP_GPU_ROOT/manifests/smoke-pod.yaml" >/dev/null; then
        result=1
    elif ! kip_kubectl -n default wait \
        --for='jsonpath={.status.phase}=Succeeded' \
        pod/kip-gpu-smoke --timeout=5m >/dev/null; then
        result=1
    else
        smoke_node=$(kip_kubectl -n default get pod kip-gpu-smoke \
            -o 'jsonpath={.spec.nodeName}' 2>/dev/null) || result=1
        logs=$(kip_kubectl -n default logs kip-gpu-smoke 2>&1) || result=1
        if [ "$result" -eq 0 ]; then
            [ "$smoke_node" = "$KIP_GPU_NODE_NAME" ] || result=1
            printf '%s\n' "$logs" | command awk -F',' '
                NF >= 5 && $1 ~ /^[[:space:]]*[0-9]+[[:space:]]*$/ && $2 != "" &&
                    $3 ~ /GPU-/ && $4 != "" && $5 != "" {good++}
                END {exit !(good > 0)}
            ' || result=1
        fi
    fi

    if [ "$result" -ne 0 ]; then
        kip_gpu_smoke_diagnostics
    else
        kip_verbose_output "nvidia-smi in kip-gpu-smoke" "$logs"
        printf 'GPU smoke: %s\n' "$(printf '%s\n' "$logs" | command head -n 1)"
    fi
    kip_kubectl -n default delete pod kip-gpu-smoke \
        --ignore-not-found --wait=true >/dev/null 2>&1 || cleanup_result=1
    if [ "$cleanup_result" -ne 0 ]; then
        printf '%s\n' "Failed to remove Pod/default/kip-gpu-smoke." >&2
        kip_gpu_smoke_diagnostics
    fi
    [ "$result" -eq 0 ] && [ "$cleanup_result" -eq 0 ]
}
