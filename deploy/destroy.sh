#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
KIP_DEPLOY_ROOT=$SCRIPT_DIR
readonly KIP_DEPLOY_ROOT
KIP_REPOSITORY_ROOT=$(cd "$KIP_DEPLOY_ROOT/.." && pwd)
readonly KIP_REPOSITORY_ROOT
KIP_CLUSTER_ROOT="$KIP_DEPLOY_ROOT/cluster"
readonly KIP_CLUSTER_ROOT
KIP_GPU_ROOT="$KIP_DEPLOY_ROOT/gpu"
readonly KIP_GPU_ROOT
KIP_RUNTIME_ROOT="$KIP_DEPLOY_ROOT/runtime/vllm"
readonly KIP_RUNTIME_ROOT
KIP_OBSERVABILITY_ROOT="$KIP_DEPLOY_ROOT/observability"
readonly KIP_OBSERVABILITY_ROOT

# shellcheck source=deploy/lib/ui.sh
source "$KIP_DEPLOY_ROOT/lib/ui.sh"
# shellcheck source=deploy/cluster/versions.env
source "$KIP_CLUSTER_ROOT/versions.env"
# shellcheck source=deploy/gpu/versions.env
source "$KIP_GPU_ROOT/versions.env"
# shellcheck source=deploy/runtime/vllm/versions.env
source "$KIP_RUNTIME_ROOT/versions.env"
# shellcheck source=deploy/observability/versions.env
source "$KIP_OBSERVABILITY_ROOT/versions.env"
# shellcheck source=deploy/cluster/lib/common.sh
source "$KIP_CLUSTER_ROOT/lib/common.sh"
# shellcheck source=deploy/cluster/lib/preflight.sh
source "$KIP_CLUSTER_ROOT/lib/preflight.sh"
# shellcheck source=deploy/cluster/lib/kubernetes.sh
source "$KIP_CLUSTER_ROOT/lib/kubernetes.sh"

export LC_ALL=C
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_CONFIRM_DESTROY=${KIP_CONFIRM_DESTROY:-}
KIP_KUBECONFIG=${KIP_KUBECONFIG:-}
KIP_OPT_ROOT=${KIP_OPT_ROOT:-/opt}
KIP_SYS_FS_BPF_ROOT=${KIP_SYS_FS_BPF_ROOT:-/sys/fs/bpf}
KIP_DESTROY_STATE_DIR="$KIP_VAR_ROOT/lib/kube-inference-platform"
KIP_DESTROY_STATE_FILE="$KIP_DESTROY_STATE_DIR/destroying"
KIP_RUNTIME_NAMESPACE="kip-runtime"
KIP_RUNTIME_RESOURCE_NAME="vllm-qwen25-7b"
KIP_RUNTIME_PROFILE_FILE="$KIP_RUNTIME_ROOT/profiles/qwen2.5-7b-instruct.json"
KIP_BENCHMARK_NAMESPACE="kip-benchmark"
KIP_BENCHMARK_RESULTS_VOLUME="kip-benchmark-results"
if [ "${KIP_TESTING:-0}" = 1 ]; then
    KIP_BENCHMARK_WORK_DIR=${KIP_BENCHMARK_WORK_DIR:-$KIP_REPOSITORY_ROOT/benchmarks/.work}
else
    # This path is deleted during teardown. Keep the production target fixed beneath
    # the checked-out repository instead of accepting an arbitrary environment path.
    KIP_BENCHMARK_WORK_DIR="$KIP_REPOSITORY_ROOT/benchmarks/.work"
fi
KIP_OBSERVABILITY_STORAGE_CLASS="kip-local-static"
KIP_OBSERVABILITY_VOLUMES="kip-prometheus kip-alertmanager kip-grafana"
KIP_DESTROY_PREFLIGHT_ERROR=""

kip_ui_init
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
[ "$#" -eq 0 ] || kip_usage_error "destroy does not accept positional arguments."

kip_confirm_destroy() {
    local answer

    case "$KIP_CONFIRM_DESTROY" in
        1) return 0 ;;
        "") ;;
        *) kip_usage_error "CONFIRM_DESTROY must be 1 when provided; received '$KIP_CONFIRM_DESTROY'." ;;
    esac
    if [ ! -t 0 ]; then
        kip_usage_error "Non-interactive destruction requires CONFIRM_DESTROY=1."
    fi
    printf '%s' "Type destroy to remove the KIP-managed GPU and Kubernetes stack: "
    IFS= read -r answer
    [ "$answer" = destroy ] || kip_usage_error "Destruction was not confirmed."
}

kip_destroy_has_owned_markers() {
    [ -r "$KIP_ETC_ROOT/modules-load.d/kube-inference-platform.conf" ] \
        && [ -r "$KIP_ETC_ROOT/sysctl.d/99-kube-inference-platform.conf" ] \
        && command grep -Fq 'Managed by Kube Inference Platform' \
            "$KIP_ETC_ROOT/sysctl.d/99-kube-inference-platform.conf"
}

kip_destroy_has_cluster_state() {
    [ -e "$KIP_ETC_ROOT/kubernetes/admin.conf" ] \
        || [ -e "$KIP_VAR_ROOT/lib/etcd" ] \
        || [ -e "$KIP_VAR_ROOT/lib/kubelet/config.yaml" ] \
        || [ -e "$KIP_ETC_ROOT/containerd/config.toml" ] \
        || kip_package_installed kubeadm \
        || kip_package_installed containerd.io
}

kip_destroy_has_runtime_residue() {
    local benchmark_work bpf_entries links namespaces rules

    [ -e "$KIP_OPT_ROOT/cni/bin/cilium-cni" ] && return 0
    [ -e "$KIP_RUN_ROOT/cilium" ] && return 0
    [ -e "$KIP_RUN_ROOT/nvidia" ] && return 0
    [ -e "$KIP_DESTROY_STATE_DIR/model-cache" ] && return 0
    [ -e "$KIP_DESTROY_STATE_DIR/benchmark-results" ] && return 0
    [ -e "$KIP_DESTROY_STATE_DIR/observability" ] && return 0
    if [ -e "$KIP_BENCHMARK_WORK_DIR" ]; then
        benchmark_work=$(kip_exec find "$KIP_BENCHMARK_WORK_DIR" \
            -mindepth 1 -print -quit 2>/dev/null || true)
        [ -n "$benchmark_work" ] && return 0
    fi
    [ -e "$KIP_SYS_FS_BPF_ROOT/cilium" ] && return 0
    if [ "$KIP_USE_SUDO" = "1" ]; then
        bpf_entries=$(kip_run_privileged find "$KIP_SYS_FS_BPF_ROOT/tc/globals" \
            -maxdepth 1 -name 'cilium*' -print -quit 2>/dev/null || true)
    else
        bpf_entries=$(kip_exec find "$KIP_SYS_FS_BPF_ROOT/tc/globals" \
            -maxdepth 1 -name 'cilium*' -print -quit 2>/dev/null || true)
    fi
    if [ -n "$bpf_entries" ]; then
        return 0
    fi
    namespaces=$(kip_exec ip netns list 2>/dev/null || true)
    printf '%s\n' "$namespaces" | command grep -Eq '^cni-' && return 0
    links=$(kip_exec ip -o link show 2>/dev/null || true)
    printf '%s\n' "$links" | command awk -F': ' '
        $2 ~ /^lxc[[:xdigit:]]+(@|$)/ {found=1}
        END {exit !found}
    ' && return 0
    if kip_has_command iptables-save; then
        if [ "$KIP_USE_SUDO" = "1" ]; then
            rules=$(kip_run_privileged iptables-save 2>/dev/null || true)
        else
            rules=$(kip_exec iptables-save 2>/dev/null || true)
        fi
        printf '%s\n' "$rules" | command grep -Eq 'CILIUM_|KUBE-' && return 0
    fi
    return 1
}

kip_destroy_packages_match() {
    local expected_containerd package_name version

    kip_cluster_detect_os || return 1
    expected_containerd=$(kip_containerd_package_version) || return 1
    for package_name in containerd.io kubelet kubeadm kubectl; do
        if kip_package_installed "$package_name"; then
            version=$(kip_package_version "$package_name") || return 1
            case "$package_name" in
                containerd.io) [ "$version" = "$expected_containerd" ] || return 1 ;;
                *) [ "$version" = "$KIP_KUBERNETES_PACKAGE_VERSION" ] || return 1 ;;
            esac
        fi
    done
}

kip_destroy_api_ready() {
    [ -e "$KIP_ETC_ROOT/kubernetes/admin.conf" ] || return 1
    kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        get --raw=/readyz >/dev/null 2>&1
}

kip_destroy_helm_release_exists() {
    local release=$1 namespace=$2

    kip_run_privileged helm status "$release" --namespace "$namespace" \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" >/dev/null 2>&1
}

kip_destroy_namespace_exists() {
    local namespace=$1 name

    name=$(kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        get namespace "$namespace" -o 'jsonpath={.metadata.name}' 2>/dev/null) \
        || return 1
    [ "$name" = "$namespace" ]
}

kip_destroy_resource_exists() {
    local kind=$1 name=$2 namespace=${3:-} recorded

    if [ -n "$namespace" ]; then
        recorded=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            -n "$namespace" get "$kind" "$name" \
            -o 'jsonpath={.metadata.name}' 2>/dev/null) || return 1
    else
        recorded=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            get "$kind" "$name" -o 'jsonpath={.metadata.name}' 2>/dev/null) \
            || return 1
    fi
    [ "$recorded" = "$name" ]
}

kip_destroy_resource_is_owned() {
    local kind=$1 name=$2 namespace=${3:-} owner

    if [ -n "$namespace" ]; then
        owner=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            -n "$namespace" get "$kind" "$name" \
            -o 'jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}' \
            2>/dev/null) || return 1
    else
        owner=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            get "$kind" "$name" \
            -o 'jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}' \
            2>/dev/null) || return 1
    fi
    [ "$owner" = kube-inference-platform ]
}

kip_destroy_namespace_contents_are_owned() {
    local allowed_releases=${2:-} namespace=$1 operator_pods='[]' output resource resources

    if [ "$namespace" = gpu-operator ] && [ "$allowed_releases" = gpu-operator ]; then
        operator_pods=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            -n gpu-operator get pods -l app.kubernetes.io/component=gpu-operator \
            -o json 2>/dev/null | kip_exec jq -c '
                [.items[]? | select(.metadata.labels["app.kubernetes.io/instance"]
                    == "gpu-operator") | .metadata.name]
            ') || return 1
    fi

    resources=$(kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        api-resources --verbs=list --namespaced=true -o name 2>/dev/null) || return 1
    [ -n "$resources" ] || return 1
    while IFS= read -r resource; do
        [ -n "$resource" ] || continue
        output=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            -n "$namespace" get "$resource" -o json 2>/dev/null) || return 1
        printf '%s' "$output" | kip_exec jq -e \
            --arg namespace "$namespace" --arg releases "$allowed_releases" \
            --argjson operatorPods "$operator_pods" '
            def allowed_releases: ($releases | split(" ") | map(select(length > 0)));
            def kip_owned:
                .metadata.labels["app.kubernetes.io/managed-by"]
                    == "kube-inference-platform";
            def exact_helm_release:
                (.metadata.annotations["meta.helm.sh/release-namespace"] // "")
                    == $namespace
                and ((.metadata.annotations["meta.helm.sh/release-name"] // "") as $release
                    | (allowed_releases | index($release)) != null);
            def exact_helm_record:
                .kind == "Secret"
                and (.metadata.labels.owner // "") == "helm"
                and ((.metadata.labels.name // "") as $release
                    | (allowed_releases | index($release)) != null);
            def monitoring_generated:
                $namespace == "kip-observability"
                and (allowed_releases | index("kip-monitoring")) != null
                and (
                    (.kind == "PersistentVolumeClaim"
                        and ((.metadata.name == "alertmanager-kip-monitoring-alertmanager-db-alertmanager-kip-monitoring-alertmanager-0"
                                and .metadata.labels["kube-inference-platform.io/volume"] == "alertmanager"
                                and .spec.volumeName == "kip-alertmanager")
                            or (.metadata.name == "prometheus-kip-monitoring-prometheus-db-prometheus-kip-monitoring-prometheus-0"
                                and .metadata.labels["kube-inference-platform.io/volume"] == "prometheus"
                                and .spec.volumeName == "kip-prometheus"))
                        and .metadata.labels["app.kubernetes.io/managed-by"] == "prometheus-operator")
                    or (.kind == "Secret" and .metadata.name == "kip-monitoring-admission"
                        and .type == "Opaque" and (.data | keys) == ["ca", "cert", "key"])
                );
            def gpu_operator_generated:
                $namespace == "gpu-operator"
                and (allowed_releases | index("gpu-operator")) != null
                and .kind == "Lease"
                and (.metadata.name | test("^[0-9]+\\.nvidia\\.com$"))
                and (((.spec.holderIdentity // "") | split("_")[0]) as $holder
                    | ($operatorPods | index($holder)) != null);
            def generated_child:
                ((.metadata.ownerReferences // []) | length) > 0
                or .kind == "Event"
                or (.kind == "ServiceAccount" and .metadata.name == "default")
                or (.kind == "ConfigMap" and .metadata.name == "kube-root-ca.crt")
                or ((.metadata.labels["endpoints.kubernetes.io/managed-by"] // "")
                    | length) > 0
                or ((.metadata.labels["endpointslice.kubernetes.io/managed-by"] // "")
                    | length) > 0;
            all(.items[]?; kip_owned or exact_helm_release or exact_helm_record
                or monitoring_generated or gpu_operator_generated
                or generated_child)
        ' >/dev/null || return 1
    done <<EOF
$resources
EOF
}

kip_destroy_directory_is_owned_or_absent() {
    local expected=$2 marker path=$1 recorded

    marker="$path/.kip-managed-v1"
    if ! kip_run_privileged test -e "$path"; then
        ! kip_run_privileged test -L "$path"
        return
    fi
    kip_run_privileged test -d "$path" || return 1
    ! kip_run_privileged test -L "$path" || return 1
    kip_run_privileged test -f "$marker" || return 1
    ! kip_run_privileged test -L "$marker" || return 1
    recorded=$(kip_run_privileged sed -n 'p' "$marker" 2>/dev/null) || return 1
    [ "$recorded" = "$expected" ]
}

kip_destroy_release_chart_is_owned() {
    local actual_chart expected_chart=$3 expected_digest=$4 namespace=$2 release=$1
    local recorded_digest releases values

    releases=$(kip_run_privileged helm list \
        --namespace "$namespace" --filter "^${release}$" --output json \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" 2>/dev/null) || return 1
    actual_chart=$(printf '%s' "$releases" | kip_exec jq -er \
        --arg release "$release" --arg namespace "$namespace" '
            .[] | select(.name == $release and .namespace == $namespace) | .chart
        ') || return 1
    values=$(kip_run_privileged helm get values "$release" \
        --namespace "$namespace" --output json \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" 2>/dev/null) || return 1
    recorded_digest=$(printf '%s' "$values" | kip_exec jq -er '
        select(.kip.managedBy == "kube-inference-platform") | .kip.chartDigest
    ') || return 1
    [ "$actual_chart" = "$expected_chart" ] \
        && [ "$recorded_digest" = "$expected_digest" ]
}

kip_destroy_monitoring_crds_exist() {
    local crd

    for crd in $KIP_MONITORING_CRDS; do
        kip_destroy_resource_exists customresourcedefinition "$crd" && return 0
    done
    return 1
}

kip_destroy_monitoring_crds_are_owned() {
    local crd digest owner output

    for crd in $KIP_MONITORING_CRDS; do
        kip_destroy_resource_exists customresourcedefinition "$crd" || return 1
        owner=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            get customresourcedefinition "$crd" \
            -o 'jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}' \
            2>/dev/null) || return 1
        digest=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            get customresourcedefinition "$crd" \
            -o 'jsonpath={.metadata.annotations.kube-inference-platform\.io/chart-digest}' \
            2>/dev/null) || return 1
        [ "$owner" = kube-inference-platform ] \
            && [ "$digest" = "$KIP_KUBE_PROMETHEUS_STACK_DIGEST" ] \
            || return 1
        output=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            get "$crd" --all-namespaces -o json 2>/dev/null) || return 1
        printf '%s' "$output" | kip_exec jq -e \
            --arg namespace "$KIP_OBSERVABILITY_NAMESPACE" \
            --arg monitoring_release "$KIP_MONITORING_RELEASE" \
            --arg opencost_release "$KIP_OPENCOST_RELEASE" '
            all(.items[]?;
                .metadata.namespace == $namespace
                and (
                    .metadata.labels["app.kubernetes.io/managed-by"]
                        == "kube-inference-platform"
                    or (
                        (.metadata.annotations["meta.helm.sh/release-name"]
                            == $monitoring_release
                            or .metadata.annotations["meta.helm.sh/release-name"]
                                == $opencost_release)
                        and .metadata.annotations["meta.helm.sh/release-namespace"]
                            == $namespace
                    )
                )
            )
        ' >/dev/null || return 1
    done
}

kip_destroy_benchmark_exists() {
    kip_destroy_namespace_exists "$KIP_BENCHMARK_NAMESPACE" \
        || kip_destroy_resource_exists persistentvolume "$KIP_BENCHMARK_RESULTS_VOLUME"
}

kip_destroy_benchmark_is_owned() {
    if kip_destroy_namespace_exists "$KIP_BENCHMARK_NAMESPACE"; then
        kip_destroy_resource_is_owned namespace "$KIP_BENCHMARK_NAMESPACE" || return 1
        if kip_destroy_resource_exists persistentvolumeclaim \
            "$KIP_BENCHMARK_RESULTS_VOLUME" "$KIP_BENCHMARK_NAMESPACE"; then
            kip_destroy_resource_is_owned persistentvolumeclaim \
                "$KIP_BENCHMARK_RESULTS_VOLUME" "$KIP_BENCHMARK_NAMESPACE" || return 1
        fi
        if kip_destroy_resource_exists serviceaccount kip-benchmark \
            "$KIP_BENCHMARK_NAMESPACE"; then
            kip_destroy_resource_is_owned serviceaccount kip-benchmark \
                "$KIP_BENCHMARK_NAMESPACE" || return 1
        fi
    fi
    if kip_destroy_resource_exists persistentvolume "$KIP_BENCHMARK_RESULTS_VOLUME"; then
        kip_destroy_resource_is_owned persistentvolume \
            "$KIP_BENCHMARK_RESULTS_VOLUME" || return 1
    fi
    if kip_destroy_namespace_exists "$KIP_BENCHMARK_NAMESPACE"; then
        kip_destroy_namespace_contents_are_owned "$KIP_BENCHMARK_NAMESPACE" || return 1
    fi
}

kip_destroy_observability_exists() {
    local volume

    kip_destroy_resource_exists service kip-cilium-agent-metrics kube-system && return 0
    kip_destroy_resource_exists service kip-cilium-operator-metrics kube-system && return 0
    kip_destroy_namespace_exists "$KIP_OBSERVABILITY_NAMESPACE" && return 0
    kip_destroy_helm_release_exists "$KIP_MONITORING_RELEASE" \
        "$KIP_OBSERVABILITY_NAMESPACE" && return 0
    kip_destroy_helm_release_exists "$KIP_OPENCOST_RELEASE" \
        "$KIP_OBSERVABILITY_NAMESPACE" && return 0
    kip_destroy_monitoring_crds_exist && return 0
    kip_destroy_resource_exists storageclass "$KIP_OBSERVABILITY_STORAGE_CLASS" && return 0
    for volume in $KIP_OBSERVABILITY_VOLUMES; do
        kip_destroy_resource_exists persistentvolume "$volume" && return 0
    done
    return 1
}

kip_destroy_observability_is_owned() {
    local release service volume

    for service in kip-cilium-agent-metrics kip-cilium-operator-metrics; do
        if kip_destroy_resource_exists service "$service" kube-system; then
            kip_destroy_resource_is_owned service "$service" kube-system || return 1
        fi
    done

    if kip_destroy_namespace_exists "$KIP_OBSERVABILITY_NAMESPACE"; then
        kip_destroy_resource_is_owned namespace "$KIP_OBSERVABILITY_NAMESPACE" || return 1
    fi
    for release in "$KIP_OPENCOST_RELEASE" "$KIP_MONITORING_RELEASE"; do
        if kip_destroy_helm_release_exists "$release" "$KIP_OBSERVABILITY_NAMESPACE"; then
            case "$release" in
                "$KIP_OPENCOST_RELEASE")
                    kip_destroy_release_chart_is_owned "$release" \
                        "$KIP_OBSERVABILITY_NAMESPACE" \
                        "opencost-$KIP_OPENCOST_CHART_VERSION" \
                        "$KIP_OPENCOST_CHART_DIGEST" || return 1
                    ;;
                "$KIP_MONITORING_RELEASE")
                    kip_destroy_release_chart_is_owned "$release" \
                        "$KIP_OBSERVABILITY_NAMESPACE" \
                        "kube-prometheus-stack-$KIP_KUBE_PROMETHEUS_STACK_VERSION" \
                        "$KIP_KUBE_PROMETHEUS_STACK_DIGEST" || return 1
                    ;;
            esac
        fi
    done
    if kip_destroy_resource_exists storageclass "$KIP_OBSERVABILITY_STORAGE_CLASS"; then
        kip_destroy_resource_is_owned storageclass \
            "$KIP_OBSERVABILITY_STORAGE_CLASS" || return 1
    fi
    for volume in $KIP_OBSERVABILITY_VOLUMES; do
        if kip_destroy_resource_exists persistentvolume "$volume"; then
            kip_destroy_resource_is_owned persistentvolume "$volume" || return 1
        fi
    done
    if kip_destroy_monitoring_crds_exist; then
        kip_destroy_monitoring_crds_are_owned || return 1
    fi
    if kip_destroy_namespace_exists "$KIP_OBSERVABILITY_NAMESPACE"; then
        kip_destroy_namespace_contents_are_owned "$KIP_OBSERVABILITY_NAMESPACE" \
            "$KIP_MONITORING_RELEASE $KIP_OPENCOST_RELEASE" || return 1
    fi
}

kip_destroy_gpu_release_is_owned() {
    local chart driver_enabled expected_driver_enabled list_json recorded_digest recorded_mode
    local status status_json values_json

    status_json=$(kip_run_privileged helm status gpu-operator \
        --namespace gpu-operator --output json \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" 2>/dev/null) || return 1
    list_json=$(kip_run_privileged helm list --namespace gpu-operator \
        --filter '^gpu-operator$' --output json \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" 2>/dev/null) || return 1
    values_json=$(kip_run_privileged helm get values gpu-operator \
        --namespace gpu-operator --output json \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" 2>/dev/null) || return 1
    status=$(printf '%s' "$status_json" | kip_exec jq -er '.info.status') || return 1
    chart=$(printf '%s' "$list_json" | kip_exec jq -er '
        .[] | select(.name == "gpu-operator" and .namespace == "gpu-operator") | .chart
    ') || return 1
    recorded_mode=$(printf '%s' "$values_json" | kip_exec jq -er \
        '.kip.driverMode | select(. == "operator" or . == "preinstalled")') || return 1
    recorded_digest=$(printf '%s' "$values_json" | kip_exec jq -er '.kip.chartDigest') \
        || return 1
    driver_enabled=$(printf '%s' "$values_json" | kip_exec jq -er \
        '.driver.enabled | select(type == "boolean") | tostring') || return 1
    [ "$recorded_mode" = operator ] && expected_driver_enabled=true \
        || expected_driver_enabled=false
    case "$status" in
        deployed | failed | pending-install | pending-upgrade | pending-rollback) ;;
        *) return 1 ;;
    esac
    [ "$chart" = "gpu-operator-$KIP_GPU_OPERATOR_VERSION" ] \
        && [ "$recorded_digest" = "$KIP_GPU_OPERATOR_CHART_DIGEST" ] \
        && [ "$driver_enabled" = "$expected_driver_enabled" ] \
        && printf '%s' "$values_json" | kip_exec jq -e \
            --arg dcgmRepository "$KIP_DCGM_EXPORTER_REPOSITORY" \
            --arg dcgmImage "$KIP_DCGM_EXPORTER_IMAGE" \
            --arg dcgmVersion "$KIP_DCGM_EXPORTER_PINNED_VERSION" \
            --arg dcgmConfig "$KIP_DCGM_METRICS_CONFIG_NAME" \
            --arg dcgmCollectors "$KIP_DCGM_METRICS_COLLECTOR_PATH" \
            --arg driverVersion "$KIP_NVIDIA_DRIVER_VERSION" '
            ((keys - ["clusterPolicy", "gpuCluster", "psa", "nfd", "cdi",
                "sandboxWorkloads", "driver", "toolkit", "devicePlugin", "gfd",
                "dcgm", "dcgmExporter", "mig", "migManager", "gds", "gdrcopy",
                "vgpuManager", "vgpuDeviceManager", "vfioManager", "kataManager",
                "sandboxDevicePlugin", "kataSandboxDevicePlugin", "ccManager", "kip"])
                | length == 0)
            and ((.dcgmExporter | keys
                - ["enabled", "repository", "image", "version", "serviceMonitor", "config", "env"])
                | length == 0)
            and ((.kip | keys
                - ["driverMode", "chartDigest", "dcgmExporterEnabled"])
                | length == 0)
            and .clusterPolicy.deployCR == true
            and .gpuCluster.deployCR == false
            and .psa.enabled == true
            and .nfd.enabled == true
            and .cdi.enabled == true
            and .cdi.nriPluginEnabled == false
            and .toolkit.enabled == true
            and .devicePlugin.enabled == true
            and .dcgm.enabled == false
            and (.dcgmExporter.enabled | type == "boolean")
            and .dcgmExporter.repository == $dcgmRepository
            and .dcgmExporter.image == $dcgmImage
            and .dcgmExporter.version == $dcgmVersion
            and .dcgmExporter.serviceMonitor.enabled == false
            and .dcgmExporter.config == {"name": $dcgmConfig}
            and .dcgmExporter.env == [
                {"name": "DCGM_EXPORTER_INTERVAL", "value": "1000"},
                {"name": "DCGM_EXPORTER_COLLECTORS", "value": $dcgmCollectors}
            ]
            and .mig.strategy == "none"
            and .migManager.enabled == false
            and .driver.version == $driverVersion
            and (((.kip | has("dcgmExporterEnabled")) | not)
                or .kip.dcgmExporterEnabled == .dcgmExporter.enabled)
            and any(.toolkit.env[];
                .name == "CONTAINERD_CONFIG" and .value == "/etc/containerd/config.toml")
            and any(.toolkit.env[];
                .name == "CONTAINERD_SOCKET" and .value == "/run/containerd/containerd.sock")
            and any(.toolkit.env[];
                .name == "RUNTIME_CONFIG_SOURCE"
                and .value == "file=/etc/containerd/config.toml")
            and any(.toolkit.env[];
                .name == "RUNTIME_DROP_IN_CONFIG"
                and .value == "/etc/containerd/conf.d/99-nvidia.toml")
        ' >/dev/null
}

kip_destroy_gpu_exists() {
    kip_destroy_namespace_exists gpu-operator \
        || kip_destroy_helm_release_exists gpu-operator gpu-operator
}

kip_destroy_gpu_is_owned() {
    kip_destroy_helm_release_exists gpu-operator gpu-operator || return 1
    kip_destroy_gpu_release_is_owned || return 1
    kip_destroy_namespace_exists gpu-operator || return 1
    kip_destroy_namespace_contents_are_owned gpu-operator gpu-operator
}

kip_destroy_runtime_exists() {
    local name

    name=$(kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        get namespace "$KIP_RUNTIME_NAMESPACE" \
        -o 'jsonpath={.metadata.name}' 2>/dev/null) || return 1
    [ "$name" = "$KIP_RUNTIME_NAMESPACE" ]
}

kip_destroy_runtime_profile_hash() {
    kip_exec jq -cS . "$KIP_RUNTIME_PROFILE_FILE" \
        | kip_exec sha256sum \
        | command awk '{print $1}'
}

kip_destroy_runtime_resource_exists() {
    local kind=$1 name

    name=$(kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        -n "$KIP_RUNTIME_NAMESPACE" get "$kind" "$KIP_RUNTIME_RESOURCE_NAME" \
        -o 'jsonpath={.metadata.name}' 2>/dev/null) || return 1
    [ "$name" = "$KIP_RUNTIME_RESOURCE_NAME" ]
}

kip_destroy_runtime_is_owned() {
    local digest kind managed model_revision profile_hash recorded_hash resources

    managed=$(kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        get namespace "$KIP_RUNTIME_NAMESPACE" \
        -o 'jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}' \
        2>/dev/null) || return 1
    [ "$managed" = kube-inference-platform ] || return 1
    profile_hash=$(kip_destroy_runtime_profile_hash) || return 1
    recorded_hash=$(kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        get namespace "$KIP_RUNTIME_NAMESPACE" \
        -o 'jsonpath={.metadata.annotations.kube-inference-platform/profile-sha256}' \
        2>/dev/null) || return 1
    [ "$recorded_hash" = "$profile_hash" ] || return 1

    resources=$(kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        -n "$KIP_RUNTIME_NAMESPACE" get deployment,service -o json 2>/dev/null) || return 1
    printf '%s' "$resources" | kip_exec jq -e --arg name "$KIP_RUNTIME_RESOURCE_NAME" '
        all(.items[]?;
            (.kind == "Deployment" or .kind == "Service")
            and .metadata.name == $name
            and .metadata.labels["app.kubernetes.io/managed-by"]
                == "kube-inference-platform")
    ' >/dev/null || return 1

    model_revision=$(kip_exec jq -r '.model.revision' "$KIP_RUNTIME_PROFILE_FILE") || return 1
    for kind in deployment service; do
        kip_destroy_runtime_resource_exists "$kind" || continue
        managed=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            -n "$KIP_RUNTIME_NAMESPACE" get "$kind" "$KIP_RUNTIME_RESOURCE_NAME" \
            -o 'jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}' \
            2>/dev/null) || return 1
        digest=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            -n "$KIP_RUNTIME_NAMESPACE" get "$kind" "$KIP_RUNTIME_RESOURCE_NAME" \
            -o 'jsonpath={.metadata.annotations.kube-inference-platform/image-digest}' \
            2>/dev/null) || return 1
        recorded_hash=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            -n "$KIP_RUNTIME_NAMESPACE" get "$kind" "$KIP_RUNTIME_RESOURCE_NAME" \
            -o 'jsonpath={.metadata.annotations.kube-inference-platform/profile-sha256}' \
            2>/dev/null) || return 1
        [ "$managed" = kube-inference-platform ] \
            && [ "$digest" = "$KIP_VLLM_IMAGE_DIGEST" ] \
            && [ "$recorded_hash" = "$profile_hash" ] || return 1
        [ "$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            -n "$KIP_RUNTIME_NAMESPACE" get "$kind" "$KIP_RUNTIME_RESOURCE_NAME" \
            -o 'jsonpath={.metadata.annotations.kube-inference-platform/model-revision}' \
            2>/dev/null)" = "$model_revision" ] || return 1
    done
    kip_destroy_namespace_contents_are_owned "$KIP_RUNTIME_NAMESPACE"
}

kip_destroy_cluster_scope_is_safe() {
    local cilium_namespace_resources namespaces persistent_volumes workloads

    namespaces=$(kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        get namespaces -o json 2>/dev/null) || return 1
    printf '%s' "$namespaces" | kip_exec jq -e \
        --arg benchmark "$KIP_BENCHMARK_NAMESPACE" \
        --arg observability "$KIP_OBSERVABILITY_NAMESPACE" \
        --arg runtime "$KIP_RUNTIME_NAMESPACE" \
        --arg cilium_version "$KIP_CILIUM_VERSION" '
        all(.items[]?; .metadata.name as $name
            | if $name == "cilium-secrets" then
                .metadata.labels["app.kubernetes.io/managed-by"] == "Helm"
                and .metadata.labels["app.kubernetes.io/part-of"] == "cilium"
                and .metadata.labels["helm.sh/chart"] == ("cilium-" + $cilium_version)
                and .metadata.annotations["meta.helm.sh/release-name"] == "cilium"
                and .metadata.annotations["meta.helm.sh/release-namespace"] == "kube-system"
              else
                (["default", "kube-system", "kube-public", "kube-node-lease",
                    "gpu-operator", $benchmark, $observability, $runtime]
                    | index($name) != null)
              end)
    ' >/dev/null || return 1

    if printf '%s' "$namespaces" | kip_exec jq -e \
        '[.items[]? | select(.metadata.name == "cilium-secrets")] | length == 1' \
        >/dev/null; then
        cilium_namespace_resources=$(kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            get all,secret,configmap,serviceaccount,role,rolebinding,pvc,ingress \
            -n cilium-secrets -o json 2>/dev/null) || return 1
        printf '%s' "$cilium_namespace_resources" | kip_exec jq -e '
            all(.items[]?; .kind as $kind | .metadata.name as $name
                | ($kind == "ConfigMap" and $name == "kube-root-ca.crt")
                or ($kind == "ServiceAccount" and $name == "default")
                or ($kind == "Role" and $name == "cilium-operator-tlsinterception-secrets")
                or ($kind == "Role" and $name == "cilium-tlsinterception-secrets")
                or ($kind == "RoleBinding"
                    and $name == "cilium-operator-tlsinterception-secrets")
                or ($kind == "RoleBinding" and $name == "cilium-tlsinterception-secrets"))
        ' >/dev/null || return 1
    fi

    workloads=$(kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        get deployments,statefulsets,daemonsets,jobs,cronjobs,pods \
        --all-namespaces -o json 2>/dev/null) || return 1
    printf '%s' "$workloads" | kip_exec jq -e \
        --arg benchmark "$KIP_BENCHMARK_NAMESPACE" \
        --arg observability "$KIP_OBSERVABILITY_NAMESPACE" \
        --arg runtime "$KIP_RUNTIME_NAMESPACE" '
        all(.items[]?;
            .metadata.namespace as $namespace
            | .metadata.name as $name
            | if (["gpu-operator", $benchmark, $observability, $runtime]
                    | index($namespace)) != null then
                true
              elif $namespace == "kube-system" then
                ($name | test("^(coredns|kube-proxy|cilium($|-)|cilium-operator($|-)|kube-apiserver-|kube-controller-manager-|kube-scheduler-|etcd-)"))
              else
                false
              end)
    ' >/dev/null || return 1

    persistent_volumes=$(kip_run_privileged kubectl \
        --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
        get persistentvolumes -o json 2>/dev/null) || return 1
    printf '%s' "$persistent_volumes" | kip_exec jq -e '
        all(.items[]?;
            .metadata.labels["app.kubernetes.io/managed-by"]
                == "kube-inference-platform")
    ' >/dev/null
}

kip_destroy_ownership_preflight() {
    if ! kip_destroy_directory_is_owned_or_absent \
        "$KIP_DESTROY_STATE_DIR/benchmark-results" \
        "kube-inference-platform benchmark results v1" \
        || ! kip_destroy_directory_is_owned_or_absent \
            "$KIP_DESTROY_STATE_DIR/observability" \
            "kube-inference-platform observability storage v1" \
        || ! kip_destroy_directory_is_owned_or_absent \
            "$KIP_DESTROY_STATE_DIR/model-cache" \
            "kube-inference-platform model cache v1"; then
        KIP_DESTROY_PREFLIGHT_ERROR="A project-named host storage directory is unmarked or has an invalid ownership marker."
        return 1
    fi
    if ! kip_destroy_api_ready; then
        kip_note "Kubernetes API is unavailable; kubeadm reset will remove local cluster state."
        return 0
    fi
    if ! kip_destroy_cluster_scope_is_safe; then
        KIP_DESTROY_PREFLIGHT_ERROR="The cluster contains a non-system namespace, foreign workload, or unmanaged persistent volume."
        return 1
    fi
    if kip_destroy_benchmark_exists && ! kip_destroy_benchmark_is_owned; then
        KIP_DESTROY_PREFLIGHT_ERROR="A platform release is unmanaged: benchmark state drifted or its namespace contains foreign objects."
        return 1
    fi
    if kip_destroy_observability_exists && ! kip_destroy_observability_is_owned; then
        KIP_DESTROY_PREFLIGHT_ERROR="A platform release is unmanaged: observability state drifted or shares its namespace with foreign objects."
        return 1
    fi
    if kip_destroy_runtime_exists && ! kip_destroy_runtime_is_owned; then
        KIP_DESTROY_PREFLIGHT_ERROR="A platform release is unmanaged: runtime state drifted or shares its namespace with foreign objects."
        return 1
    fi
    if kip_destroy_gpu_exists && ! kip_destroy_gpu_is_owned; then
        KIP_DESTROY_PREFLIGHT_ERROR="A platform release is unmanaged: GPU Operator state drifted or shares its namespace with foreign objects."
        return 1
    fi
}

kip_destroy_cluster_releases() {
    local output release volume

    kip_destroy_api_ready || return 0
    if kip_destroy_benchmark_exists; then
        kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            delete namespace "$KIP_BENCHMARK_NAMESPACE" \
            --wait=true --timeout=10m >/dev/null || return 1
        kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            delete persistentvolume "$KIP_BENCHMARK_RESULTS_VOLUME" \
            --ignore-not-found=true --wait=true --timeout=5m >/dev/null || return 1
    fi
    if kip_destroy_observability_exists; then
        kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            -n kube-system delete service \
            kip-cilium-agent-metrics kip-cilium-operator-metrics \
            --ignore-not-found=true --wait=true --timeout=5m >/dev/null || return 1
        for release in "$KIP_OPENCOST_RELEASE" "$KIP_MONITORING_RELEASE"; do
            if kip_destroy_helm_release_exists "$release" "$KIP_OBSERVABILITY_NAMESPACE"; then
                output=$(kip_run_privileged helm uninstall "$release" \
                    --namespace "$KIP_OBSERVABILITY_NAMESPACE" --wait --timeout 10m \
                    --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" 2>&1) || return 1
                kip_verbose_output "helm uninstall $release" "$output"
            fi
        done
        if kip_destroy_namespace_exists "$KIP_OBSERVABILITY_NAMESPACE"; then
            kip_run_privileged kubectl \
                --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
                delete namespace "$KIP_OBSERVABILITY_NAMESPACE" \
                --wait=true --timeout=10m >/dev/null || return 1
        fi
        for volume in $KIP_OBSERVABILITY_VOLUMES; do
            kip_run_privileged kubectl \
                --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
                delete persistentvolume "$volume" --ignore-not-found=true \
                --wait=true --timeout=5m >/dev/null || return 1
        done
        kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            delete storageclass "$KIP_OBSERVABILITY_STORAGE_CLASS" \
            --ignore-not-found=true --wait=true --timeout=5m >/dev/null || return 1
        if kip_destroy_monitoring_crds_exist; then
            # Helm deliberately retains CRDs. Remove only the exact set carrying
            # KIP's immutable chart digest after namespaced custom resources are gone.
            for crd in $KIP_MONITORING_CRDS; do
                kip_run_privileged kubectl \
                    --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
                    delete customresourcedefinition "$crd" \
                    --wait=true --timeout=5m >/dev/null || return 1
            done
        fi
    fi
    if kip_destroy_runtime_exists; then
        kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            delete namespace "$KIP_RUNTIME_NAMESPACE" --wait=true --timeout=10m >/dev/null \
            || return 1
    fi
    if kip_destroy_gpu_exists; then
        output=$(kip_run_privileged helm uninstall gpu-operator \
            --namespace gpu-operator --wait --timeout 10m \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" 2>&1) || return 1
        kip_verbose_output "helm uninstall gpu-operator" "$output"
        kip_run_privileged kubectl \
            --kubeconfig "$KIP_ETC_ROOT/kubernetes/admin.conf" \
            delete namespace gpu-operator --ignore-not-found=true \
            --wait=true --timeout=5m >/dev/null
    fi
}

kip_destroy_remove_user_kubeconfig() {
    local source_config target_home target_user user_config

    source_config="$KIP_ETC_ROOT/kubernetes/admin.conf"
    [ -e "$source_config" ] || return 0
    target_user=${KIP_TARGET_USER:-}
    if [ -z "$target_user" ]; then
        if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
            target_user=$SUDO_USER
        elif [ "$(kip_effective_uid)" -ne 0 ]; then
            target_user=$(kip_exec id -un) || return 1
        else
            target_user=root
        fi
    fi
    if [ -n "${KIP_TARGET_HOME:-}" ]; then
        target_home=$KIP_TARGET_HOME
    else
        target_home=$(kip_exec getent passwd "$target_user" 2>/dev/null | command cut -d: -f6)
    fi
    [ -n "$target_home" ] || return 1
    user_config="$target_home/.kube/config"
    if [ -r "$user_config" ] && kip_run_privileged cmp -s "$source_config" "$user_config"; then
        kip_run_privileged rm -f "$user_config"
    elif [ -e "$user_config" ]; then
        kip_note "Preserving $user_config because it no longer matches the KIP admin kubeconfig."
    fi
}

kip_destroy_reset_kubernetes() {
    kip_destroy_remove_user_kubeconfig || return 1
    if kip_has_command kubeadm; then
        kip_run_privileged kubeadm reset --force --cri-socket "$KIP_CRI_SOCKET"
    fi
    kip_run_privileged systemctl stop kubelet >/dev/null 2>&1 || true
    kip_run_privileged systemctl disable kubelet >/dev/null 2>&1 || true
    kip_run_privileged systemctl stop containerd >/dev/null 2>&1 || true
    kip_run_privileged systemctl disable containerd >/dev/null 2>&1 || true
}

kip_destroy_network_filtering() {
    local cleaned current family

    for family in iptables ip6tables; do
        kip_has_command "$family-save" || continue
        kip_has_command "$family-restore" || continue
        current=$(command mktemp "${TMPDIR:-/tmp}/kip-${family}.current.XXXXXX")
        cleaned=$(command mktemp "${TMPDIR:-/tmp}/kip-${family}.clean.XXXXXX")
        if ! kip_run_privileged "$family-save" > "$current"; then
            command rm -f "$current" "$cleaned"
            return 1
        fi
        command grep -Ev 'CILIUM_|KUBE-' "$current" > "$cleaned" || true
        if ! kip_run_privileged "$family-restore" < "$cleaned"; then
            command rm -f "$current" "$cleaned"
            return 1
        fi
        command rm -f "$current" "$cleaned"
    done
}

kip_destroy_remove_bpf_state() {
    local attempt=0 pinned

    while [ "$attempt" -lt 30 ]; do
        kip_run_privileged rm -rf "$KIP_SYS_FS_BPF_ROOT/cilium" >/dev/null 2>&1 || true
        if kip_run_privileged test -d "$KIP_SYS_FS_BPF_ROOT/tc/globals"; then
            kip_run_privileged find "$KIP_SYS_FS_BPF_ROOT/tc/globals" \
                -maxdepth 1 -name 'cilium*' -delete >/dev/null 2>&1 || true
        fi
        pinned=$(kip_run_privileged find "$KIP_SYS_FS_BPF_ROOT/tc/globals" \
            -maxdepth 1 -name 'cilium*' -print -quit 2>/dev/null || true)
        if ! kip_run_privileged test -e "$KIP_SYS_FS_BPF_ROOT/cilium" \
            && [ -z "$pinned" ]; then
            return 0
        fi
        attempt=$((attempt + 1))
        if [ "$attempt" -lt 30 ] && [ "${KIP_TESTING:-0}" != 1 ]; then
            command sleep 2
        fi
    done
    return 1
}

kip_destroy_network_and_state() {
    local interface interfaces mount_path namespace namespaces

    for mount_path in "$KIP_RUN_ROOT/nvidia/driver" "$KIP_RUN_ROOT/cilium/cgroupv2"; do
        if kip_exec mountpoint -q "$mount_path"; then
            kip_run_privileged umount "$mount_path" || return 1
        fi
    done
    for interface in cilium_host cilium_net cilium_vxlan; do
        if kip_exec ip link show "$interface" >/dev/null 2>&1; then
            kip_run_privileged ip link delete "$interface" >/dev/null 2>&1 || return 1
        fi
    done
    namespaces=$(kip_exec ip netns list 2>/dev/null \
        | command awk '$1 ~ /^cni-/ {print $1}' || true)
    while IFS= read -r namespace; do
        [ -n "$namespace" ] || continue
        kip_run_privileged ip netns delete "$namespace" || return 1
    done <<EOF
$namespaces
EOF
    interfaces=$(kip_exec ip -o link show 2>/dev/null \
        | command awk -F': ' '
            $2 ~ /^lxc[[:xdigit:]]+(@|$)/ {
                sub(/@.*/, "", $2)
                print $2
            }
        ' || true)
    while IFS= read -r interface; do
        [ -n "$interface" ] || continue
        kip_run_privileged ip link delete "$interface" >/dev/null 2>&1 || return 1
    done <<EOF
$interfaces
EOF
    kip_destroy_remove_bpf_state || return 1
    kip_destroy_network_filtering || return 1
    kip_run_privileged rm -rf \
        "$KIP_ETC_ROOT/cni/net.d" \
        "$KIP_ETC_ROOT/kubernetes" \
        "$KIP_RUN_ROOT/cilium" \
        "$KIP_RUN_ROOT/containerd" \
        "$KIP_RUN_ROOT/nvidia" \
        "$KIP_VAR_ROOT/lib/cilium" \
        "$KIP_VAR_ROOT/lib/cni" \
        "$KIP_VAR_ROOT/lib/containerd" \
        "$KIP_VAR_ROOT/lib/etcd" \
        "$KIP_VAR_ROOT/lib/kubelet" \
        "$KIP_DESTROY_STATE_DIR/benchmark-results" \
        "$KIP_DESTROY_STATE_DIR/observability" \
        "$KIP_DESTROY_STATE_DIR/model-cache" \
        "$KIP_USR_LOCAL_BIN/../nvidia/toolkit"
    kip_run_privileged rm -rf "$KIP_BENCHMARK_WORK_DIR"
    kip_run_privileged rm -f "$KIP_OPT_ROOT/cni/bin/cilium-cni"
}

kip_destroy_packages_and_configuration() {
    local package_name packages=()

    for package_name in containerd.io kubelet kubeadm kubectl cri-tools kubernetes-cni; do
        if kip_package_installed "$package_name"; then
            packages+=("$package_name")
        fi
    done
    if [ "${#packages[@]}" -gt 0 ]; then
        kip_run_privileged apt-mark unhold "${packages[@]}" >/dev/null 2>&1 || true
        kip_run_privileged env DEBIAN_FRONTEND=noninteractive \
            apt-get purge -y "${packages[@]}"
    fi
    kip_run_privileged rm -rf "$KIP_ETC_ROOT/containerd"
    kip_run_privileged rm -f \
        "$KIP_ETC_ROOT/crictl.yaml" \
        "$KIP_ETC_ROOT/apt/sources.list.d/docker.sources" \
        "$KIP_ETC_ROOT/apt/sources.list.d/kubernetes.list" \
        "$KIP_ETC_ROOT/apt/keyrings/kubernetes-apt-keyring.gpg"
    kip_run_privileged systemctl daemon-reload >/dev/null 2>&1 || true
}

kip_destroy_restore_host() {
    local backup="$KIP_ETC_ROOT/fstab.kip-pre-bootstrap"
    local fstab="$KIP_ETC_ROOT/fstab"
    local restored

    if [ -r "$backup" ]; then
        restored=$(command mktemp "${TMPDIR:-/tmp}/kip-fstab.XXXXXX")
        command cp "$backup" "$restored"
        kip_install_file "$restored" "$fstab"
        command rm -f "$restored"
        kip_run_privileged rm -f "$backup"
    elif [ -r "$fstab" ]; then
        restored=$(command mktemp "${TMPDIR:-/tmp}/kip-fstab.XXXXXX")
        command sed 's/^# KIP_DISABLED_SWAP //' "$fstab" > "$restored"
        kip_install_file "$restored" "$fstab"
        command rm -f "$restored"
    fi
    kip_run_privileged rm -f \
        "$KIP_ETC_ROOT/modules-load.d/kube-inference-platform.conf" \
        "$KIP_ETC_ROOT/sysctl.d/99-kube-inference-platform.conf"
    kip_run_privileged sysctl --system >/dev/null 2>&1 || true
    kip_run_privileged swapon -a >/dev/null 2>&1 || true
}

kip_confirm_destroy
trap 'kip_unexpected_error "$?" "$LINENO"' ERR

kip_logo
kip_heading "Platform Teardown · v0.1"
kip_reset_results 8

if ! kip_destroy_has_cluster_state \
    && [ ! -e "$KIP_DESTROY_STATE_FILE" ] \
    && ! kip_destroy_has_runtime_residue; then
    kip_record SKIP "Managed installation" "No KIP cluster state is present; nothing to remove."
    kip_summary "Platform teardown"
    exit 0
fi

if { kip_destroy_has_owned_markers || [ -e "$KIP_DESTROY_STATE_FILE" ]; } \
    && kip_destroy_packages_match; then
    kip_record PASS "Managed installation" "KIP ownership markers and pinned package versions match."
else
    kip_record FAIL "Managed installation" \
        "Cluster ownership is unrecognized or package versions have drifted; refusing teardown."
    kip_summary "Platform teardown"
    exit 1
fi

if kip_acquire_privileges; then
    kip_record PASS "Administrative access" "Root or sudo access acquired."
else
    kip_record FAIL "Administrative access" "Root or functional sudo access is required."
    kip_summary "Platform teardown"
    exit 1
fi

if ! kip_destroy_ownership_preflight; then
    kip_record FAIL "Platform workloads" "$KIP_DESTROY_PREFLIGHT_ERROR Refusing teardown."
    kip_summary "Platform teardown"
    exit 1
fi

if kip_make_directory "$KIP_DESTROY_STATE_DIR" 0755 \
    && kip_run_privileged touch "$KIP_DESTROY_STATE_FILE"; then
    :
else
    kip_record FAIL "Teardown state" "Unable to record recoverable teardown state."
    kip_summary "Platform teardown"
    exit 1
fi

if kip_destroy_cluster_releases; then
    kip_record PASS "Platform workloads" \
        "KIP-managed benchmark, observability, runtime, and GPU workloads were removed."
else
    kip_record FAIL "Platform workloads" \
        "A release is unmanaged, a namespace or volume conflicts, or removal failed."
    kip_summary "Platform teardown"
    exit 1
fi

if kip_destroy_reset_kubernetes; then
    kip_record PASS "Kubernetes reset" "kubeadm state reset and cluster services stopped."
else
    kip_record FAIL "Kubernetes reset" "kubeadm reset or kubeconfig cleanup failed."
    kip_summary "Platform teardown"
    exit 1
fi

if kip_destroy_network_and_state; then
    kip_record PASS "Runtime state" "KIP CNI, Kubernetes, containerd, and GPU runtime state removed."
else
    kip_record FAIL "Runtime state" "Failed to remove project-owned runtime state."
    kip_summary "Platform teardown"
    exit 1
fi

if kip_destroy_packages_and_configuration; then
    kip_record PASS "Cluster packages" "Pinned Kubernetes and containerd packages and configuration removed."
else
    kip_record FAIL "Cluster packages" "Package or configuration removal failed."
    kip_summary "Platform teardown"
    exit 1
fi

if kip_destroy_restore_host; then
    kip_record PASS "Host restoration" "Original fstab restored; KIP sysctl and module files removed."
else
    kip_record FAIL "Host restoration" "Failed to restore host configuration."
    kip_summary "Platform teardown"
    exit 1
fi

if ! kip_destroy_has_cluster_state \
    && ! kip_destroy_has_runtime_residue \
    && [ ! -e "$KIP_ETC_ROOT/sysctl.d/99-kube-inference-platform.conf" ]; then
    kip_run_privileged rm -f "$KIP_DESTROY_STATE_FILE"
    if kip_run_privileged rmdir "$KIP_DESTROY_STATE_DIR"; then
        kip_record PASS "Final verification" "The host is ready for a fresh complete v0.1 bootstrap."
    else
        kip_record FAIL "Final verification" \
            "Unrecognized files remain in the project state directory; refusing recursive deletion."
        kip_summary "Platform teardown"
        exit 1
    fi
else
    kip_record FAIL "Final verification" "KIP-managed cluster state remains on the host."
    kip_summary "Platform teardown"
    exit 1
fi

kip_summary "Platform teardown"
printf '\n%sReady for a fresh run:%s make bootstrap; add COST_PROFILE once reviewed pricing is configured.\n' \
    "$KIP_COLOR_GREEN" "$KIP_COLOR_RESET"
