#!/usr/bin/env bash

# GPU-node, disk, Secret, and managed-state preflight checks.

kip_runtime_single_gpu_node() {
    local output

    output=$(kip_kubectl get nodes -o json 2>/dev/null) || return 1
    printf '%s' "$output" | kip_exec jq -e '
        [.items[]
         | select(.spec.unschedulable != true)
         | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))
         | select(((.status.allocatable["nvidia.com/gpu"] // "0") | tonumber) > 0)]
        | length == 1
    ' >/dev/null
}

kip_runtime_existing_path() {
    local path=$1

    while [ ! -e "$path" ] && [ "$path" != / ]; do
        path=$(command dirname "$path")
    done
    printf '%s' "$path"
}

kip_runtime_disk_space_ready() {
    local cache_available cache_device cache_path containerd_available containerd_device
    local containerd_path
    local min_cache=$((25 * 1024 * 1024))
    local min_combined=$((60 * 1024 * 1024))
    local min_containerd=$((30 * 1024 * 1024))

    if [ "${KIP_TESTING:-0}" = 1 ] && [ -n "${KIP_TEST_CACHE_FREE_KIB:-}" ]; then
        cache_available=$KIP_TEST_CACHE_FREE_KIB
        containerd_available=${KIP_TEST_CONTAINERD_FREE_KIB:-$cache_available}
        if [ "${KIP_TEST_SAME_FS:-1}" = 1 ]; then
            [ "$cache_available" -ge "$min_combined" ]
        else
            [ "$cache_available" -ge "$min_cache" ] \
                && [ "$containerd_available" -ge "$min_containerd" ]
        fi
        return
    fi

    cache_path=$(kip_runtime_existing_path "$KIP_RUNTIME_CACHE_PATH")
    containerd_path=$(kip_runtime_existing_path "$KIP_VAR_ROOT/lib/containerd")
    cache_device=$(kip_exec df -Pk "$cache_path" | command awk 'END {print $1}') || return 1
    cache_available=$(kip_exec df -Pk "$cache_path" | command awk 'END {print $4}') || return 1
    containerd_device=$(kip_exec df -Pk "$containerd_path" | command awk 'END {print $1}') \
        || return 1
    containerd_available=$(kip_exec df -Pk "$containerd_path" | command awk 'END {print $4}') \
        || return 1
    case "$cache_available:$containerd_available" in
        *[!0-9:]*) return 1 ;;
    esac
    if [ "$cache_device" = "$containerd_device" ]; then
        [ "$cache_available" -ge "$min_combined" ]
    else
        [ "$cache_available" -ge "$min_cache" ] \
            && [ "$containerd_available" -ge "$min_containerd" ]
    fi
}

kip_runtime_secret_ready() {
    [ -z "$KIP_RUNTIME_HF_SECRET_NAME" ] && return 0
    kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" get secret "$KIP_RUNTIME_HF_SECRET_NAME" \
        -o 'jsonpath={.data.token}' 2>/dev/null | command grep -q .
}

kip_runtime_namespace_owned() {
    local value

    value=$(kip_kubectl get namespace "$KIP_RUNTIME_NAMESPACE" \
        -o 'jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null) \
        || return 1
    [ "$value" = kube-inference-platform ]
}

kip_runtime_namespace_profile_matches() {
    local profile profile_hash

    profile=$(kip_kubectl get namespace "$KIP_RUNTIME_NAMESPACE" \
        -o 'jsonpath={.metadata.annotations.kube-inference-platform/profile}' 2>/dev/null) \
        || return 1
    profile_hash=$(kip_kubectl get namespace "$KIP_RUNTIME_NAMESPACE" \
        -o 'jsonpath={.metadata.annotations.kube-inference-platform/profile-sha256}' \
        2>/dev/null) || return 1
    [ "$profile" = "$KIP_MODEL_PROFILE" ] \
        && [ "$profile_hash" = "$KIP_RUNTIME_PROFILE_HASH" ]
}

kip_runtime_namespace_has_only_expected_workloads() {
    local resources

    resources=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" get deployment,service \
        -o json 2>/dev/null) || return 1
    printf '%s' "$resources" | kip_exec jq -e --arg name "$KIP_RUNTIME_RESOURCE_NAME" '
        all(.items[]?;
            (.kind == "Deployment" or .kind == "Service")
            and .metadata.name == $name
            and .metadata.labels["app.kubernetes.io/managed-by"]
                == "kube-inference-platform")
    ' >/dev/null
}

kip_runtime_preflight_state() {
    local deployment_exists=0 namespace_exists=0 service_exists=0

    KIP_RUNTIME_PREFLIGHT_ERROR=""
    KIP_RUNTIME_PREFLIGHT_STATE=""
    if kip_kubectl get namespace "$KIP_RUNTIME_NAMESPACE" >/dev/null 2>&1; then
        namespace_exists=1
        if ! kip_runtime_namespace_owned \
            || ! kip_runtime_namespace_profile_matches \
            || ! kip_runtime_namespace_has_only_expected_workloads; then
            KIP_RUNTIME_PREFLIGHT_ERROR="Runtime namespace ownership or profile has drifted."
            return 1
        fi
    fi
    if kip_runtime_resource_exists deployment "$KIP_RUNTIME_RESOURCE_NAME"; then
        deployment_exists=1
        if ! kip_runtime_identity_matches deployment "$KIP_RUNTIME_RESOURCE_NAME" \
            || ! kip_runtime_deployment_contract_matches; then
            KIP_RUNTIME_PREFLIGHT_ERROR="Runtime Deployment identity or pinned configuration has drifted."
            return 1
        fi
    fi
    if kip_runtime_resource_exists service "$KIP_RUNTIME_RESOURCE_NAME"; then
        service_exists=1
        if ! kip_runtime_identity_matches service "$KIP_RUNTIME_RESOURCE_NAME" \
            || ! kip_runtime_service_contract_matches; then
            KIP_RUNTIME_PREFLIGHT_ERROR="Runtime Service identity or pinned configuration has drifted."
            return 1
        fi
    fi

    if [ "$namespace_exists" -eq 0 ] && { [ "$deployment_exists" -ne 0 ] || [ "$service_exists" -ne 0 ]; }; then
        KIP_RUNTIME_PREFLIGHT_ERROR="Runtime resources exist outside the managed namespace contract."
        return 1
    fi
    if [ "$namespace_exists" -eq 0 ]; then
        KIP_RUNTIME_PREFLIGHT_STATE=fresh
    elif [ "$deployment_exists" -eq 1 ] && [ "$service_exists" -eq 1 ]; then
        KIP_RUNTIME_PREFLIGHT_STATE=matching
    else
        KIP_RUNTIME_PREFLIGHT_STATE=partial
    fi
}
