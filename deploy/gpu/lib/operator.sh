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

kip_gpu_reconcile_operator() {
    local dcgm_enabled=$1 driver_enabled=false

    case "$dcgm_enabled" in
        true | false) ;;
        *) return 1 ;;
    esac
    [ "$KIP_GPU_DRIVER_MODE" = operator ] && driver_enabled=true
    kip_exec helm upgrade --install "$KIP_GPU_RELEASE" \
        "$KIP_GPU_OPERATOR_CHART@$KIP_GPU_OPERATOR_CHART_DIGEST" \
        --namespace "$KIP_GPU_NAMESPACE" \
        --create-namespace \
        --values "$KIP_GPU_ROOT/values.yaml" \
        --set "driver.enabled=$driver_enabled" \
        --set-string "driver.version=$KIP_NVIDIA_DRIVER_VERSION" \
        --set-string "dcgmExporter.repository=$KIP_DCGM_EXPORTER_REPOSITORY" \
        --set-string "dcgmExporter.image=$KIP_DCGM_EXPORTER_IMAGE" \
        --set-string "dcgmExporter.version=$KIP_DCGM_EXPORTER_PINNED_VERSION" \
        --set "dcgmExporter.enabled=$dcgm_enabled" \
        --set "dcgmExporter.serviceMonitor.enabled=false" \
        --set-string "dcgmExporter.config.name=$KIP_DCGM_METRICS_CONFIG_NAME" \
        --set-string "dcgmExporter.env[0].name=DCGM_EXPORTER_INTERVAL" \
        --set-string "dcgmExporter.env[0].value=1000" \
        --set-string "dcgmExporter.env[1].name=DCGM_EXPORTER_COLLECTORS" \
        --set-string "dcgmExporter.env[1].value=$KIP_DCGM_METRICS_COLLECTOR_PATH" \
        --set-string "kip.driverMode=$KIP_GPU_DRIVER_MODE" \
        --set-string "kip.chartDigest=$KIP_GPU_OPERATOR_CHART_DIGEST" \
        --set "kip.dcgmExporterEnabled=$dcgm_enabled" \
        --wait \
        --timeout 25m
}

kip_gpu_install_operator() {
    local dcgm_enabled=false output

    # A later GPU rerun must not undo observability's Operator-owned exporter.
    # Fresh GPU installs defer DCGM until the monitoring stack is installed.
    if kip_gpu_release_exists; then
        dcgm_enabled=$(kip_gpu_dcgm_exporter_state) || return 1
        if ! kip_gpu_release_is_exact; then
            kip_gpu_release_identity_exact_for_dcgm_state "$dcgm_enabled" || return 1
            output=$(kip_exec helm uninstall "$KIP_GPU_RELEASE" \
                --namespace "$KIP_GPU_NAMESPACE" --wait --timeout 10m 2>&1) \
                || return 1
            kip_verbose_output "GPU Operator recovery uninstall" "$output"
        fi
    fi
    kip_gpu_reconcile_operator "$dcgm_enabled"
}
