#!/usr/bin/env bash

# Artifact verification and Helm reconciliation for GPU Operator.

kip_gpu_download_cosign() {
    local cosign_path="$KIP_WORK_DIR/cosign"

    kip_exec curl -fsSL \
        "https://github.com/sigstore/cosign/releases/download/v$KIP_COSIGN_VERSION/cosign-linux-amd64" \
        -o "$cosign_path"
    kip_verify_sha256 "$cosign_path" "$KIP_COSIGN_LINUX_AMD64_SHA256" || return 1
    kip_exec chmod 0755 "$cosign_path"
    printf '%s' "$cosign_path"
}

kip_gpu_verify_chart() {
    local actual_digest cosign_path key_path output pull_dir pull_output

    pull_dir="$KIP_WORK_DIR/chart"
    kip_exec mkdir -p "$pull_dir"
    pull_output=$(kip_exec helm pull "$KIP_GPU_OPERATOR_CHART" \
        --version "$KIP_GPU_OPERATOR_VERSION" \
        --destination "$pull_dir" 2>&1) || return 1
    kip_verbose_output "GPU Operator chart resolution" "$pull_output"
    actual_digest=$(printf '%s\n' "$pull_output" | command awk '
        /^Digest:[[:space:]]*/ {print $2; exit}
    ')
    [ "$actual_digest" = "$KIP_GPU_OPERATOR_CHART_DIGEST" ] || return 1

    cosign_path=$(kip_gpu_download_cosign) || return 1
    key_path="$KIP_WORK_DIR/nvidia-chart-public-key.pem"
    kip_exec curl -fsSL "$KIP_NVIDIA_CHART_SIGNING_KEY_URL" -o "$key_path"
    output=$("$cosign_path" verify \
        --key "$key_path" \
        --insecure-ignore-tlog \
        "$KIP_GPU_OPERATOR_REGISTRY_REF@$KIP_GPU_OPERATOR_CHART_DIGEST" 2>&1) \
        || return 1
    [ -n "$output" ] || return 1
    kip_verbose_output "GPU Operator chart signature" "$output"
}

kip_gpu_install_operator() {
    local driver_enabled=false

    [ "$KIP_GPU_DRIVER_MODE" = operator ] && driver_enabled=true
    kip_exec helm upgrade --install "$KIP_GPU_RELEASE" \
        "$KIP_GPU_OPERATOR_CHART@$KIP_GPU_OPERATOR_CHART_DIGEST" \
        --namespace "$KIP_GPU_NAMESPACE" \
        --create-namespace \
        --values "$KIP_GPU_ROOT/values.yaml" \
        --set "driver.enabled=$driver_enabled" \
        --set-string "driver.version=$KIP_NVIDIA_DRIVER_VERSION" \
        --set-string "kip.driverMode=$KIP_GPU_DRIVER_MODE" \
        --set-string "kip.chartDigest=$KIP_GPU_OPERATOR_CHART_DIGEST" \
        --wait \
        --timeout 25m
}
