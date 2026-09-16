#!/usr/bin/env bash

# Temporary OpenAI-compatible inference client and cleanup helpers.

kip_runtime_smoke_diagnostics() {
    local output

    printf '\nRuntime smoke diagnostics:\n' >&2
    for command_name in describe logs events; do
        case "$command_name" in
            describe)
                output=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" describe pod \
                    kip-runtime-smoke 2>&1 || true)
                ;;
            logs)
                output=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" logs \
                    kip-runtime-smoke --all-containers 2>&1 || true)
                ;;
            events)
                output=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" get events \
                    --field-selector involvedObject.name=kip-runtime-smoke \
                    --sort-by=.lastTimestamp 2>&1 || true)
                ;;
        esac
        printf '%s\n%s\n' "--- $command_name ---" "${output:-no output}" >&2
    done
}

kip_runtime_smoke_cleanup() {
    local result=0

    kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" delete pod kip-runtime-smoke \
        --ignore-not-found --wait=true >/dev/null 2>&1 || result=1
    kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" delete configmap kip-runtime-smoke-client \
        --ignore-not-found --wait=true >/dev/null 2>&1 || result=1
    return "$result"
}

kip_runtime_run_smoke() {
    local config output result=0

    kip_runtime_smoke_cleanup || return 1
    config=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" create configmap \
        kip-runtime-smoke-client \
        --from-file="client.py=$KIP_RUNTIME_ROOT/smoke/client.py" \
        --dry-run=client -o yaml 2>/dev/null) || return 1
    printf '%s\n' "$config" | kip_kubectl apply -f - >/dev/null || return 1
    if ! kip_kubectl apply -f "$KIP_WORK_DIR/smoke-pod.yaml" >/dev/null; then
        result=1
    elif ! kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" wait \
        --for='jsonpath={.status.phase}=Succeeded' pod/kip-runtime-smoke \
        --timeout=5m >/dev/null; then
        result=1
    else
        output=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" logs kip-runtime-smoke 2>&1) \
            || result=1
        if [ "$result" -eq 0 ]; then
            printf '%s\n' "$output" | command grep -Fq 'RUNTIME_SMOKE_OK' || result=1
        fi
    fi

    if [ "$result" -ne 0 ]; then
        kip_runtime_smoke_diagnostics
    else
        kip_verbose_output "OpenAI-compatible smoke client" "$output"
        printf '%s\n' "$output" | command grep -E '^(PASS|RUNTIME_SMOKE_OK)' || true
    fi
    if ! kip_runtime_smoke_cleanup; then
        printf '%s\n' "Failed to remove runtime smoke resources." >&2
        result=1
    fi
    [ "$result" -eq 0 ]
}
