#!/usr/bin/env bash

# Read-only safety gates before GPU Operator installation or reconciliation.

kip_gpu_run_cluster_check() {
    local cluster_check=${KIP_CLUSTER_CHECK_SCRIPT:-$KIP_DEPLOY_ROOT/cluster/validate.sh}

    KIP_NO_LOGO=1 KIP_VERBOSE="$KIP_VERBOSE" "$cluster_check"
}

kip_gpu_has_orphaned_state() {
    local crds daemonsets releases

    releases=$(kip_exec helm list --all-namespaces --all --output json 2>/dev/null) || return 0
    if printf '%s' "$releases" | kip_exec jq -e \
        '.[] | select(.name == "gpu-operator" or (.chart | test("gpu-operator"; "i")))' \
        >/dev/null 2>&1; then
        return 0
    fi
    crds=$(kip_kubectl get crd \
        clusterpolicies.nvidia.com gpuclusters.nvidia.com nvidiadrivers.nvidia.com \
        --ignore-not-found -o name 2>/dev/null) || return 0
    [ -z "$crds" ] || return 0
    daemonsets=$(kip_kubectl get daemonsets --all-namespaces \
        -o 'jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' \
        2>/dev/null) || return 0
    printf '%s\n' "$daemonsets" | command grep -Eqi \
        '(nvidia-device-plugin|nvidia-container-toolkit|nvidia-driver|nvidia-mig-manager)'
}

kip_gpu_preflight_state() {
    if kip_gpu_release_exists; then
        kip_gpu_release_is_exact || return 1
        printf '%s' matching
        return
    fi
    kip_gpu_has_orphaned_state && return 1
    printf '%s' fresh
}

kip_gpu_driver_mode_preflight() {
    local state=$1

    case "$KIP_GPU_DRIVER_MODE" in
        operator)
            if [ "$state" = fresh ] && kip_gpu_host_driver_healthy; then
                return 1
            fi
            ;;
        preinstalled)
            kip_gpu_host_driver_healthy || return 1
            ;;
    esac
}
