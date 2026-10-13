#!/usr/bin/env bash

# Shared observability configuration, ownership, and command helpers.

KIP_OBSERVABILITY_NAMESPACE=${KIP_OBSERVABILITY_NAMESPACE:-kip-observability}
KIP_MONITORING_RELEASE=${KIP_MONITORING_RELEASE:-kip-monitoring}
KIP_OPENCOST_RELEASE=${KIP_OPENCOST_RELEASE:-kip-opencost}
KIP_COST_PROFILE_DIR=${KIP_COST_PROFILE_DIR:-$KIP_REPOSITORY_ROOT/costs/profiles}
KIP_OBSERVABILITY_NODE_NAME=""
KIP_OBSERVABILITY_PREFLIGHT_ERROR=""
KIP_COST_PROFILE_STATE="unpriced"
KIP_COST_PROFILE_HASH=""
KIP_COST_PROFILE_FILE=""
KIP_COST_PROFILE_NAME=""
KIP_COST_NODE_HOURLY_RATE_USD=""
KIP_COST_GPU_HOURLY_RATE_USD=""
KIP_COST_PROVIDER=""
KIP_COST_MACHINE_TYPE=""
KIP_COST_REGION=""
KIP_COST_SOURCE=""
KIP_COST_EFFECTIVE_FROM=""
KIP_COST_PHYSICAL_GPU_COUNT=""

kip_observability_required_commands_available() {
    local command_name

    for command_name in curl df helm jq kubectl openssl sed sha256sum; do
        kip_has_command "$command_name" || return 1
    done
}

kip_observability_run_cluster_check() {
    KIP_NO_LOGO=1 KIP_VERBOSE="$KIP_VERBOSE" \
        "${KIP_CLUSTER_CHECK_SCRIPT:-$KIP_CLUSTER_ROOT/validate.sh}"
}

kip_observability_run_gpu_check() {
    KIP_NO_LOGO=1 KIP_VERBOSE="$KIP_VERBOSE" \
        "${KIP_GPU_CHECK_SCRIPT:-$KIP_GPU_ROOT/validate.sh}"
}

kip_observability_run_runtime_check() {
    KIP_NO_LOGO=1 KIP_VERBOSE="$KIP_VERBOSE" \
        KIP_RUNTIME="${KIP_RUNTIME:-vllm}" \
        KIP_MODEL_PROFILE="${KIP_MODEL_PROFILE:-qwen2.5-7b-instruct}" \
        "${KIP_RUNTIME_CHECK_SCRIPT:-$KIP_RUNTIME_ROOT/dispatch.sh}" check
}

kip_observability_namespace_exists() {
    kip_kubectl get namespace "$KIP_OBSERVABILITY_NAMESPACE" >/dev/null 2>&1
}

kip_observability_namespace_owned() {
    local owner

    owner=$(kip_kubectl get namespace "$KIP_OBSERVABILITY_NAMESPACE" \
        -o 'jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null) \
        || return 1
    [ "$owner" = kube-inference-platform ]
}

kip_observability_release_status_json() {
    kip_exec helm status "$1" \
        --namespace "$KIP_OBSERVABILITY_NAMESPACE" \
        --output json 2>/dev/null
}

kip_observability_release_exists() {
    kip_observability_release_status_json "$1" >/dev/null 2>&1
}

kip_observability_release_identity_exact() {
    local actual_chart actual_values_hash expected_chart expected_digest list_json
    local pricing_state recorded_digest release=$1 status status_json values_json values_exact=1

    case "$release" in
        "$KIP_MONITORING_RELEASE")
            expected_chart="kube-prometheus-stack-$KIP_KUBE_PROMETHEUS_STACK_VERSION"
            expected_digest=$KIP_KUBE_PROMETHEUS_STACK_DIGEST
            ;;
        "$KIP_OPENCOST_RELEASE")
            expected_chart="opencost-$KIP_OPENCOST_CHART_VERSION"
            expected_digest=$KIP_OPENCOST_CHART_DIGEST
            ;;
        *) return 1 ;;
    esac
    status_json=$(kip_observability_release_status_json "$release") || return 1
    status=$(printf '%s' "$status_json" | kip_exec jq -er '.info.status') || return 1
    list_json=$(kip_exec helm list \
        --namespace "$KIP_OBSERVABILITY_NAMESPACE" \
        --filter "^$release$" --output json 2>/dev/null) || return 1
    actual_chart=$(printf '%s' "$list_json" | kip_exec jq -er \
        --arg release "$release" --arg namespace "$KIP_OBSERVABILITY_NAMESPACE" \
        '.[] | select(.name == $release and .namespace == $namespace) | .chart') || return 1
    values_json=$(kip_exec helm get values "$release" \
        --namespace "$KIP_OBSERVABILITY_NAMESPACE" --output json 2>/dev/null) || return 1
    recorded_digest=$(printf '%s' "$values_json" | kip_exec jq -er \
        'select(.kip.managedBy == "kube-inference-platform") | .kip.chartDigest') \
        || return 1
    case "$release" in
        "$KIP_MONITORING_RELEASE")
            printf '%s' "$values_json" | kip_exec jq -e \
                --arg prometheus "${KIP_PROMETHEUS_IMAGE_DIGEST#sha256:}" \
                --arg alertmanager "${KIP_ALERTMANAGER_IMAGE_DIGEST#sha256:}" \
                --arg operator "${KIP_PROMETHEUS_OPERATOR_IMAGE_DIGEST#sha256:}" \
                --arg reloader "${KIP_PROMETHEUS_CONFIG_RELOADER_IMAGE_DIGEST#sha256:}" \
                --arg grafana "${KIP_GRAFANA_IMAGE_DIGEST#sha256:}" \
                --arg sidecar "${KIP_GRAFANA_SIDECAR_IMAGE_DIGEST#sha256:}" \
                --arg busybox "${KIP_BUSYBOX_IMAGE_DIGEST#sha256:}" \
                --arg ksm "$KIP_KUBE_STATE_METRICS_IMAGE_DIGEST" \
                --arg node "$KIP_NODE_EXPORTER_IMAGE_DIGEST" '
                .prometheus.prometheusSpec.image.sha == $prometheus
                and .alertmanager.alertmanagerSpec.image.sha == $alertmanager
                and .prometheusOperator.image.sha == $operator
                and .prometheusOperator.prometheusConfigReloader.image.sha == $reloader
                and .grafana.image.sha == $grafana
                and .grafana.sidecar.image.sha == $sidecar
                and .grafana.initChownData.image.sha == $busybox
                and .["kube-state-metrics"].image.sha == $ksm
                and .["prometheus-node-exporter"].image.digest == $node
            ' >/dev/null || values_exact=0
            actual_values_hash=$(printf '%s' "$values_json" | kip_exec jq -cS -j . \
                | kip_exec sha256sum | command awk '{print $1}') || return 1
            [ "$actual_values_hash" = "$KIP_KUBE_PROMETHEUS_STACK_VALUES_SHA256" ] \
                || values_exact=0
            ;;
        "$KIP_OPENCOST_RELEASE")
            printf '%s' "$values_json" | kip_exec jq -e \
                --arg image "$KIP_OPENCOST_IMAGE" '
                .opencost.exporter.image.fullImageName == $image
                and .opencost.ui.enabled == false
                and .plugins.enabled == false
                and .clusterName == "cluster.local"
                and .opencost.exporter.defaultClusterId == "kip-v01"
            ' >/dev/null || values_exact=0
            pricing_state=$(printf '%s' "$values_json" | kip_exec jq -er \
                '.kip.pricingState | select(. == "priced" or . == "unpriced")') \
                || return 1
            if [ "$pricing_state" = unpriced ]; then
                actual_values_hash=$(printf '%s' "$values_json" | kip_exec jq -cS -j . \
                    | kip_exec sha256sum | command awk '{print $1}') || return 1
                [ "$actual_values_hash" = "$KIP_OPENCOST_UNPRICED_VALUES_SHA256" ] \
                    || values_exact=0
            else
                actual_values_hash=$(printf '%s' "$values_json" | kip_exec jq -cS -j '
                    del(
                      .kip.costProfile,
                      .kip.costProfileSha256,
                      .podLabels,
                      .podAnnotations,
                      .opencost.customPricing.costModel.description,
                      .opencost.customPricing.costModel.GPU
                    )
                ' | kip_exec sha256sum | command awk '{print $1}') || return 1
                [ "$actual_values_hash" = "$KIP_OPENCOST_PRICED_STATIC_VALUES_SHA256" ] \
                    || values_exact=0
            fi
            ;;
    esac
    case "$status" in
        deployed | failed | pending-install | pending-upgrade | pending-rollback) ;;
        *) return 1 ;;
    esac
    [ "$actual_chart" = "$expected_chart" ] \
        && [ "$recorded_digest" = "$expected_digest" ] \
        && [ "$values_exact" -eq 1 ]
}

kip_observability_release_exact() {
    local status status_json

    kip_observability_release_identity_exact "$1" || return 1
    status_json=$(kip_observability_release_status_json "$1") || return 1
    status=$(printf '%s' "$status_json" | kip_exec jq -er '.info.status') || return 1
    [ "$status" = deployed ]
}

kip_observability_recover_unhealthy_release() {
    local output release=$1

    kip_observability_release_exists "$release" || return 0
    kip_observability_release_identity_exact "$release" || return 1
    kip_observability_release_exact "$release" && return 0
    output=$(kip_exec helm uninstall "$release" \
        --namespace "$KIP_OBSERVABILITY_NAMESPACE" \
        --wait --timeout 10m 2>&1) || return 1
    kip_verbose_output "helm recovery uninstall $release" "$output"
}

kip_observability_safe_state() {
    local release

    if kip_observability_namespace_exists && ! kip_observability_namespace_owned; then
        KIP_OBSERVABILITY_PREFLIGHT_ERROR="Namespace $KIP_OBSERVABILITY_NAMESPACE is not KIP-managed."
        return 1
    fi
    for release in "$KIP_MONITORING_RELEASE" "$KIP_OPENCOST_RELEASE"; do
        if kip_observability_release_exists "$release" \
            && ! kip_observability_release_identity_exact "$release"; then
            KIP_OBSERVABILITY_PREFLIGHT_ERROR="Helm release $release is unmanaged or version-drifted."
            return 1
        fi
    done
    if ! kip_observability_monitoring_crds_safe; then
        KIP_OBSERVABILITY_PREFLIGHT_ERROR="Prometheus Operator CRDs exist without the exact KIP monitoring release."
        return 1
    fi
    if ! kip_observability_storage_resources_safe; then
        KIP_OBSERVABILITY_PREFLIGHT_ERROR="A project-named StorageClass or persistent volume is not KIP-managed."
        return 1
    fi
    if kip_observability_namespace_exists \
        && ! kip_observability_namespaced_resources_safe; then
        KIP_OBSERVABILITY_PREFLIGHT_ERROR="A project-named observability resource is not KIP-managed."
        return 1
    fi
}

kip_observability_resource_owned_or_absent() {
    local kind=$1 name=$2 namespace=${3:-} owner output

    if [ -n "$namespace" ]; then
        output=$(kip_kubectl -n "$namespace" get "$kind" "$name" \
            --ignore-not-found -o json 2>/dev/null) || return 1
    else
        output=$(kip_kubectl get "$kind" "$name" \
            --ignore-not-found -o json 2>/dev/null) || return 1
    fi
    [ -n "$output" ] || return 0
    owner=$(printf '%s' "$output" | kip_exec jq -er \
        '.metadata.labels["app.kubernetes.io/managed-by"] // ""') || return 1
    [ "$owner" = kube-inference-platform ]
}

kip_observability_storage_resources_safe() {
    local name

    kip_observability_resource_owned_or_absent storageclass kip-local-static || return 1
    for name in kip-prometheus kip-alertmanager kip-grafana; do
        kip_observability_resource_owned_or_absent persistentvolume "$name" || return 1
    done
}

kip_observability_namespaced_resources_safe() {
    local name

    for name in kip-cilium-agent-metrics kip-cilium-operator-metrics; do
        kip_observability_resource_owned_or_absent service "$name" kube-system || return 1
    done
    kip_observability_resource_owned_or_absent secret kip-grafana-admin \
        "$KIP_OBSERVABILITY_NAMESPACE" || return 1
    kip_observability_resource_owned_or_absent configmap kip-cost-profile \
        "$KIP_OBSERVABILITY_NAMESPACE" || return 1
    for name in platform-fleet model-serving gpu-node-health distributed-topology \
        benchmark-explorer inference-cost; do
        kip_observability_resource_owned_or_absent configmap "kip-dashboard-$name" \
            "$KIP_OBSERVABILITY_NAMESPACE" || return 1
    done
    for name in kip-vllm kip-dcgm-exporter kip-cilium-agent kip-cilium-operator; do
        kip_observability_resource_owned_or_absent servicemonitor "$name" \
            "$KIP_OBSERVABILITY_NAMESPACE" || return 1
    done
    for name in kip-inference-platform kip-cost-profile; do
        kip_observability_resource_owned_or_absent prometheusrule "$name" \
            "$KIP_OBSERVABILITY_NAMESPACE" || return 1
    done
}

kip_observability_monitoring_crds_safe() {
    local crd digest existing=0 owner required=0

    for crd in $KIP_MONITORING_CRDS; do
        required=$((required + 1))
        if kip_kubectl get crd "$crd" >/dev/null 2>&1; then
            existing=$((existing + 1))
        fi
    done
    [ "$existing" -eq 0 ] && return 0
    # The immutable release values are the recovery proof if a previous process
    # stopped after Helm created CRDs but before KIP could add its annotations.
    kip_observability_release_identity_exact "$KIP_MONITORING_RELEASE" || return 1
    [ "$existing" -eq "$required" ] || return 1
    # A fully unlabelled CRD is safe only as the short crash-recovery window
    # described above. Any explicit, conflicting owner or chart digest is drift
    # and must never be overwritten by a reconciliation run.
    for crd in $KIP_MONITORING_CRDS; do
        owner=$(kip_kubectl get crd "$crd" \
            -o 'jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}' \
            2>/dev/null) || return 1
        digest=$(kip_kubectl get crd "$crd" \
            -o 'jsonpath={.metadata.annotations.kube-inference-platform\.io/chart-digest}' \
            2>/dev/null) || return 1
        case "$owner" in
            "" | kube-inference-platform) ;;
            *) return 1 ;;
        esac
        case "$digest" in
            "" | "$KIP_KUBE_PROMETHEUS_STACK_DIGEST") ;;
            *) return 1 ;;
        esac
    done
}

kip_observability_mark_crds_owned() {
    local crd

    for crd in $KIP_MONITORING_CRDS; do
        kip_kubectl get crd "$crd" >/dev/null 2>&1 || return 1
        kip_kubectl label crd "$crd" \
            app.kubernetes.io/managed-by=kube-inference-platform --overwrite >/dev/null \
            || return 1
        kip_kubectl annotate crd "$crd" \
            "kube-inference-platform.io/chart-digest=$KIP_KUBE_PROMETHEUS_STACK_DIGEST" \
            --overwrite >/dev/null || return 1
    done
}

kip_observability_crds_owned() {
    local crd digest owner

    for crd in $KIP_MONITORING_CRDS; do
        owner=$(kip_kubectl get crd "$crd" \
            -o 'jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null) \
            || return 1
        digest=$(kip_kubectl get crd "$crd" \
            -o 'jsonpath={.metadata.annotations.kube-inference-platform\.io/chart-digest}' \
            2>/dev/null) || return 1
        [ "$owner" = kube-inference-platform ] \
            && [ "$digest" = "$KIP_KUBE_PROMETHEUS_STACK_DIGEST" ] \
            || return 1
    done
}

kip_observability_single_ready_node() {
    local output

    output=$(kip_kubectl get nodes -o json 2>/dev/null) || return 1
    [ "$(printf '%s' "$output" | kip_exec jq -r '[.items[] | select(.spec.unschedulable != true and any(.status.conditions[]?; .type == "Ready" and .status == "True"))] | length')" -eq 1 ] \
        || return 1
    KIP_OBSERVABILITY_NODE_NAME=$(printf '%s' "$output" | kip_exec jq -er \
        '.items[] | select(.spec.unschedulable != true and any(.status.conditions[]?; .type == "Ready" and .status == "True")) | .metadata.name') \
        || return 1
    [ -n "$KIP_OBSERVABILITY_NODE_NAME" ]
}

kip_observability_free_kib() {
    local path=$1

    if [ -n "${KIP_TEST_OBSERVABILITY_FREE_KIB:-}" ]; then
        printf '%s' "$KIP_TEST_OBSERVABILITY_FREE_KIB"
        return
    fi
    kip_exec df -Pk "$path" 2>/dev/null | command awk 'NR == 2 {print $4}'
}

kip_observability_disk_ready() {
    local available required=$((KIP_OBSERVABILITY_REQUIRED_FREE_GIB * 1024 * 1024))
    local probe=$KIP_VAR_ROOT/lib

    [ -d "$probe" ] || probe=$KIP_VAR_ROOT
    available=$(kip_observability_free_kib "$probe") || return 1
    [ -n "$available" ] && [ "$available" -ge "$required" ]
}

kip_observability_profile_name_valid() {
    case "$1" in
        "" | *[!a-z0-9-]* | -* | *- | *--* | *.template) return 1 ;;
        *) [ "${#1}" -le 63 ] ;;
    esac
}

kip_observability_profile_path() {
    printf '%s/%s.json' "$KIP_COST_PROFILE_DIR" "$1"
}

kip_observability_validate_cost_profile_schema() {
    local profile=$1

    kip_exec jq -e '
        ((keys - ["sourceType"]) | sort) == ["currency", "effectiveFrom", "machineType", "name", "nodeHourlyRateUsd", "physicalGpuCount", "powerIncludedInRate", "provider", "region", "schemaVersion", "source"]
        and (.sourceType == null or .sourceType == "public-listing" or .sourceType == "user-confirmed-billing" or .sourceType == "provider-live-rate")
        and .schemaVersion == 1
        and (.name | type == "string" and length >= 1 and length <= 63 and test("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"))
        and .currency == "USD"
        and (.nodeHourlyRateUsd | type == "number" and . > 0 and . < 1000000)
        and (.provider | type == "string" and length >= 1 and length <= 128 and (contains("REPLACE_ME") | not))
        and (.machineType | type == "string" and length >= 1 and length <= 128 and (contains("REPLACE_ME") | not))
        and (.region | type == "string" and length >= 1 and length <= 128 and (contains("REPLACE_ME") | not))
        and (.source | type == "string" and length >= 1 and length <= 512 and test("^https://[^/@?#[:space:]]+(/[^?#[:space:]]*)?$"))
        and (.effectiveFrom | type == "string" and test("^[0-9]{4}-[0-9]{2}-[0-9]{2}$"))
        and ((.effectiveFrom + "T00:00:00Z") | fromdateiso8601 | . <= now)
        and (.physicalGpuCount | type == "number" and . == floor and . >= 1 and . <= 1024)
        and .powerIncludedInRate == true
        and ([.provider, .machineType, .region, .source] | all(test("^[[:print:]]+$")))
    ' "$profile" >/dev/null
}

kip_observability_load_cost_profile() {
    local expected_name profile

    kip_observability_profile_name_valid "$KIP_COST_PROFILE" \
        || kip_usage_error "COST_PROFILE must be a committed lowercase profile name; received '$KIP_COST_PROFILE'."
    profile=$(kip_observability_profile_path "$KIP_COST_PROFILE")
    [ -r "$profile" ] || kip_usage_error "cost profile is not readable: $KIP_COST_PROFILE."
    kip_observability_validate_cost_profile_schema "$profile" \
        || kip_usage_error "cost profile failed strict v1 validation: $KIP_COST_PROFILE."
    expected_name=$(kip_exec jq -r '.name' "$profile")
    [ "$expected_name" = "$KIP_COST_PROFILE" ] \
        || kip_usage_error "COST_PROFILE does not match the profile name."

    KIP_COST_PROFILE_FILE=$profile
    KIP_COST_PROFILE_NAME=$expected_name
    KIP_COST_PROFILE_HASH=$(kip_exec jq -cS . "$profile" \
        | kip_exec sha256sum | command awk '{print $1}') || return 1
    KIP_COST_NODE_HOURLY_RATE_USD=$(kip_exec jq -r '.nodeHourlyRateUsd' "$profile")
    KIP_COST_PHYSICAL_GPU_COUNT=$(kip_exec jq -r '.physicalGpuCount' "$profile")
    KIP_COST_GPU_HOURLY_RATE_USD=$(kip_exec jq -nr \
        --argjson rate "$KIP_COST_NODE_HOURLY_RATE_USD" \
        --argjson count "$KIP_COST_PHYSICAL_GPU_COUNT" '$rate / $count')
    KIP_COST_PROVIDER=$(kip_exec jq -r '.provider' "$profile")
    KIP_COST_MACHINE_TYPE=$(kip_exec jq -r '.machineType' "$profile")
    KIP_COST_REGION=$(kip_exec jq -r '.region' "$profile")
    KIP_COST_SOURCE=$(kip_exec jq -r '.source' "$profile")
    KIP_COST_EFFECTIVE_FROM=$(kip_exec jq -r '.effectiveFrom' "$profile")
    KIP_COST_PROFILE_STATE=priced
}

kip_observability_cluster_gpu_count() {
    local nodes

    nodes=$(kip_kubectl get nodes -o json 2>/dev/null) || return 1
    printf '%s' "$nodes" | kip_exec jq -er \
        '[.items[].status.capacity["nvidia.com/gpu"] // "0" | tonumber] | add // 0'
}

kip_observability_cost_profile_matches_cluster() {
    local actual

    actual=$(kip_observability_cluster_gpu_count) || return 1
    [ "$actual" -eq "$KIP_COST_PHYSICAL_GPU_COUNT" ]
}

kip_observability_active_profile_json() {
    local owner

    owner=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get configmap kip-cost-profile \
        -o 'jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null) \
        || return 1
    [ "$owner" = kube-inference-platform ] || return 1
    kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get configmap kip-cost-profile \
        -o 'jsonpath={.data.profile\.json}' 2>/dev/null
}

kip_observability_active_profile_state() {
    local output

    if ! kip_observability_namespace_exists; then
        printf '%s\n' absent
        return 0
    fi
    output=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get \
        configmap kip-cost-profile --ignore-not-found -o json 2>/dev/null) \
        || return 1
    if [ -n "$output" ]; then
        printf '%s\n' present
    else
        printf '%s\n' absent
    fi
}

kip_observability_release_pricing_state() {
    kip_exec helm get values "$KIP_OPENCOST_RELEASE" \
        --namespace "$KIP_OBSERVABILITY_NAMESPACE" --output json 2>/dev/null \
        | kip_exec jq -er '.kip.pricingState | select(. == "priced" or . == "unpriced")'
}

kip_observability_profile_metadata_matches_release() {
    local description values

    values=$(kip_exec helm get values "$KIP_OPENCOST_RELEASE" \
        --namespace "$KIP_OBSERVABILITY_NAMESPACE" --output json 2>/dev/null) \
        || return 1
    description="KIP whole-node profile $KIP_COST_PROFILE_NAME ($KIP_COST_PROFILE_HASH)"
    printf '%s' "$values" | kip_exec jq -e \
        --arg profile "$KIP_COST_PROFILE_NAME" \
        --arg hash "$KIP_COST_PROFILE_HASH" \
        --arg effective "$KIP_COST_EFFECTIVE_FROM" \
        --arg description "$description" '
        .kip.pricingState == "priced"
        and .kip.costProfile == $profile
        and .kip.costProfileSha256 == $hash
        and .podLabels == {
          "kube-inference-platform.io/cost-profile": $profile
        }
        and .podAnnotations == {
          "kube-inference-platform.io/cost-profile-sha256": $hash,
          "kube-inference-platform.io/pricing-effective-from": $effective
        }
        and .opencost.customPricing.costModel.description == $description
    ' >/dev/null
}

kip_observability_profile_identity_matches() {
    local expected_name=$1 expected_hash=$2 actual_name=$3 actual_hash=$4

    [ "$expected_name" = "$actual_name" ] \
        && [ "$expected_hash" = "$actual_hash" ]
}

kip_observability_set_custom_cost_enabled() {
    local enabled=$1

    case "$enabled" in
        true | false) ;;
        *) return 2 ;;
    esac
    # OpenCost chart 2.5.30 renders CUSTOM_COST_ENABLED from plugins.enabled
    # instead of customPricing.enabled. Reconcile that single owned Deployment
    # field explicitly; this avoids enabling the unrelated mutable plugin init.
    kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" set env \
        deployment/kip-opencost "CUSTOM_COST_ENABLED=$enabled" >/dev/null
}

kip_observability_custom_cost_enabled_is() {
    local actual expected=$1 workload

    workload=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get \
        deployment kip-opencost -o json 2>/dev/null) || return 1
    actual=$(printf '%s' "$workload" | kip_exec jq -er '
        [.spec.template.spec.containers[].env[]?
          | select(.name == "CUSTOM_COST_ENABLED")
          | .value]
        | select(length == 1)
        | .[0]
    ') || return 1
    [ "$actual" = "$expected" ]
}

kip_observability_pricing_configmap_matches_profile() {
    local configmap description

    configmap=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get \
        configmap kip-opencost-pricing -o json 2>/dev/null) || return 1
    description="KIP whole-node profile $KIP_COST_PROFILE_NAME ($KIP_COST_PROFILE_HASH)"
    printf '%s' "$configmap" | kip_exec jq -e \
        --arg gpu "$KIP_COST_GPU_HOURLY_RATE_USD" \
        --arg description "$description" '
        (.data | keys | sort) == [
          "CPU", "GPU", "RAM", "description", "internetNetworkEgress",
          "provider", "regionNetworkEgress", "spotCPU", "spotRAM",
          "storage", "zoneNetworkEgress"
        ]
        and .data.CPU == "0"
        and .data.GPU == $gpu
        and .data.RAM == "0"
        and .data.description == $description
        and .data.internetNetworkEgress == "0"
        and .data.provider == "custom"
        and .data.regionNetworkEgress == "0"
        and .data.spotCPU == "0"
        and .data.spotRAM == "0"
        and .data.storage == "0"
        and .data.zoneNetworkEgress == "0"
    ' >/dev/null
}

kip_observability_profile_pricing_matches_release() {
    local actual_gpu description values

    values=$(kip_exec helm get values "$KIP_OPENCOST_RELEASE" \
        --namespace "$KIP_OBSERVABILITY_NAMESPACE" --output json 2>/dev/null) \
        || return 1
    actual_gpu=$(printf '%s' "$values" | kip_exec jq -er \
        '.opencost.customPricing.costModel.GPU') || return 1
    description="KIP whole-node profile $KIP_COST_PROFILE_NAME ($KIP_COST_PROFILE_HASH)"
    kip_exec jq -ne --argjson expected "$KIP_COST_GPU_HOURLY_RATE_USD" \
        --argjson actual "$actual_gpu" \
        '($expected - $actual | fabs) < 0.000000001' >/dev/null \
        && printf '%s' "$values" | kip_exec jq -e \
            --arg profile "$KIP_COST_PROFILE_NAME" \
            --arg hash "$KIP_COST_PROFILE_HASH" \
            --arg effective "$KIP_COST_EFFECTIVE_FROM" \
            --arg description "$description" '
            .kip.pricingState == "priced"
            and .kip.costProfile == $profile
            and .kip.costProfileSha256 == $hash
            and .podLabels == {
              "kube-inference-platform.io/cost-profile": $profile
            }
            and .podAnnotations == {
              "kube-inference-platform.io/cost-profile-sha256": $hash,
              "kube-inference-platform.io/pricing-effective-from": $effective
            }
            and .opencost.customPricing.enabled == true
            and .opencost.customPricing.configmapName == "kip-opencost-pricing"
            and .opencost.customPricing.createConfigmap == true
            and .opencost.customPricing.provider == "custom"
            and .opencost.customPricing.costModel.description == $description
            and .opencost.customPricing.costModel.CPU == 0
            and .opencost.customPricing.costModel.spotCPU == 0
            and .opencost.customPricing.costModel.RAM == 0
            and .opencost.customPricing.costModel.spotRAM == 0
            and .opencost.customPricing.costModel.storage == 0
            and .opencost.customPricing.costModel.zoneNetworkEgress == 0
            and .opencost.customPricing.costModel.regionNetworkEgress == 0
            and .opencost.customPricing.costModel.internetNetworkEgress == 0
            and .opencost.exporter.inferenceCostTracking.enabled == true
            and .opencost.exporter.inferenceCostTracking.collectionInterval == "1m"
            and .opencost.ui.enabled == false
        ' >/dev/null \
        && kip_observability_custom_cost_enabled_is true \
        && kip_observability_pricing_configmap_matches_profile
}

kip_observability_load_active_profile() {
    local active active_hash active_name temporary

    active=$(kip_observability_active_profile_json) || return 1
    [ -n "$active" ] || return 1
    temporary="${KIP_WORK_DIR:-${TMPDIR:-/tmp}}/kip-active-cost-profile.json"
    printf '%s\n' "$active" > "$temporary"
    kip_observability_validate_cost_profile_schema "$temporary" || return 1
    active_name=$(printf '%s' "$active" | kip_exec jq -er '.name') || return 1
    kip_observability_profile_name_valid "$active_name" || return 1
    active_hash=$(printf '%s' "$active" | kip_exec jq -cS . \
        | kip_exec sha256sum | command awk '{print $1}') || return 1
    command rm -f "$temporary"

    # Cluster metadata is evidence, never profile authority. Reload the
    # same-name reviewed repository profile and require canonical equality so
    # coordinated ConfigMap/Helm tampering cannot manufacture a trusted price.
    KIP_COST_PROFILE=$active_name
    kip_observability_load_cost_profile || return 1
    [ "$active_hash" = "$KIP_COST_PROFILE_HASH" ]
}

kip_observability_prepare_install_pricing_state() {
    local active_state existing_state=absent requested_hash requested_name

    requested_name=$KIP_COST_PROFILE_NAME
    requested_hash=$KIP_COST_PROFILE_HASH
    active_state=$(kip_observability_active_profile_state) || {
        KIP_OBSERVABILITY_PREFLIGHT_ERROR="The active cost-profile ConfigMap could not be inspected."
        return 1
    }
    if kip_observability_release_exists "$KIP_OPENCOST_RELEASE"; then
        existing_state=$(kip_observability_release_pricing_state) || {
            KIP_OBSERVABILITY_PREFLIGHT_ERROR="The existing OpenCost release has an invalid pricing state."
            return 1
        }
    fi

    case "$existing_state:$active_state" in
        priced:present)
            if ! kip_observability_load_active_profile \
                || ! kip_observability_profile_metadata_matches_release; then
                KIP_OBSERVABILITY_PREFLIGHT_ERROR="The priced OpenCost release has an invalid or drifted reviewed cost profile."
                return 1
            fi
            if [ -n "$requested_name" ] \
                && ! kip_observability_profile_identity_matches \
                    "$requested_name" "$requested_hash" \
                    "$KIP_COST_PROFILE_NAME" "$KIP_COST_PROFILE_HASH"; then
                KIP_OBSERVABILITY_PREFLIGHT_ERROR="The requested cost profile differs from the active priced profile; use cost-configure for an explicit price change."
                return 1
            fi
            ;;
        priced:absent)
            KIP_OBSERVABILITY_PREFLIGHT_ERROR="OpenCost is priced but the reviewed kip-cost-profile ConfigMap is missing."
            return 1
            ;;
        unpriced:present)
            KIP_OBSERVABILITY_PREFLIGHT_ERROR="OpenCost is unpriced but a kip-cost-profile ConfigMap is present."
            return 1
            ;;
        unpriced:absent) ;;
        absent:present)
            KIP_OBSERVABILITY_PREFLIGHT_ERROR="A kip-cost-profile ConfigMap exists without an OpenCost release."
            return 1
            ;;
        absent:absent) ;;
        *)
            KIP_OBSERVABILITY_PREFLIGHT_ERROR="OpenCost pricing state is inconsistent."
            return 1
            ;;
    esac
}

kip_observability_api_raw() {
    kip_kubectl get --raw "$1" 2>/dev/null
}

kip_observability_service_proxy_path() {
    local service=$1 port=$2 path=${3#/}

    printf '/api/v1/namespaces/%s/services/http:%s:%s/proxy/%s' \
        "$KIP_OBSERVABILITY_NAMESPACE" "$service" "$port" "$path"
}
