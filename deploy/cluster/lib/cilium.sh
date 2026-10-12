#!/usr/bin/env bash

# Cilium installation and mutating post-install smoke validation.

kip_cilium_cli() {
    COLUMNS=120 kip_exec cilium "$@"
}

kip_install_cilium() {
    kip_exec helm upgrade --install cilium \
        "oci://quay.io/cilium/charts/cilium@$KIP_CILIUM_CHART_DIGEST" \
        --namespace kube-system \
        --values "$KIP_CLUSTER_ROOT/cilium-values.yaml" \
        --wait \
        --timeout 10m
    kip_kubectl -n kube-system rollout status daemonset/cilium --timeout=10m >/dev/null
    kip_kubectl -n kube-system rollout status deployment/cilium-operator --timeout=10m \
        >/dev/null
    kip_cilium_cli status --wait --wait-duration 10m \
        --interactive=false --output json >/dev/null
}

kip_run_dns_service_smoke() {
    local pod_name=kip-network-smoke
    local result=0
    local logs

    kip_kubectl delete pod "$pod_name" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    kip_kubectl run "$pod_name" \
        --image=busybox:1.37.0 \
        --restart=Never \
        --command -- sh -c \
        'nslookup kubernetes.default.svc.cluster.local && nc -zvw5 kubernetes.default.svc.cluster.local 443' \
        >/dev/null || return 1
    if ! kip_kubectl wait \
        --for='jsonpath={.status.phase}=Succeeded' \
        "pod/$pod_name" \
        --timeout=2m >/dev/null; then
        logs=$(kip_kubectl logs "$pod_name" 2>&1 || true)
        kip_verbose_output "network smoke pod" "$logs"
        result=1
    fi
    kip_kubectl delete pod "$pod_name" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    return "$result"
}

kip_run_cilium_connectivity() {
    local result=0

    if ! kip_cilium_cli connectivity test \
        --single-node \
        --ip-families ipv4 \
        --hubble=false \
        --test-concurrency 1 \
        --timeout 25m; then
        result=1
    fi
    if ! kip_cilium_cli connectivity test --cleanup; then
        result=1
    fi
    return "$result"
}
