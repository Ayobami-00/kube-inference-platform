#!/usr/bin/env bash

# Ownership-safe handoff from the GPU checkpoint to observability. GPU Operator
# continues to own the exporter workload; this layer only enables its exact,
# digest-pinned configuration and later owns the ServiceMonitor.

KIP_OBSERVABILITY_DCGM_ERROR=""
if [ -z "${KIP_OBSERVABILITY_ROOT:-}" ]; then
    KIP_OBSERVABILITY_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
fi
KIP_DCGM_METRICS_FILE="$KIP_OBSERVABILITY_ROOT/assets/dcgm-metrics.csv"

kip_observability_dcgm_config_hash() {
    kip_exec sha256sum "$KIP_DCGM_METRICS_FILE" | command awk '{print $1}'
}

kip_observability_dcgm_config_safe() {
    local desired_hash object owner recorded_hash

    object=$(kip_kubectl -n "$KIP_GPU_NAMESPACE" get configmap \
        "$KIP_DCGM_METRICS_CONFIG_NAME" --ignore-not-found -o json 2>/dev/null) \
        || return 1
    [ -n "$object" ] || return 0
    owner=$(printf '%s' "$object" | kip_exec jq -er \
        '.metadata.labels["app.kubernetes.io/managed-by"] // ""') || return 1
    recorded_hash=$(printf '%s' "$object" | kip_exec jq -er \
        '.metadata.annotations["kube-inference-platform.io/metrics-sha256"] // ""') \
        || return 1
    desired_hash=$(kip_observability_dcgm_config_hash) || return 1
    [ "$owner" = kube-inference-platform ] \
        && [ "$recorded_hash" = "$KIP_DCGM_METRICS_SHA256" ] \
        && [ "$desired_hash" = "$KIP_DCGM_METRICS_SHA256" ] \
        && printf '%s' "$object" | kip_exec jq -e \
            --arg key "$KIP_DCGM_METRICS_CONFIG_KEY" \
            --rawfile expected "$KIP_DCGM_METRICS_FILE" \
            '.data[$key] == $expected' >/dev/null
}

kip_observability_apply_dcgm_config() {
    local desired_hash manifest

    desired_hash=$(kip_observability_dcgm_config_hash) || return 1
    [ "$desired_hash" = "$KIP_DCGM_METRICS_SHA256" ] || return 1
    manifest=$(kip_exec jq -n \
        --arg namespace "$KIP_GPU_NAMESPACE" \
        --arg name "$KIP_DCGM_METRICS_CONFIG_NAME" \
        --arg key "$KIP_DCGM_METRICS_CONFIG_KEY" \
        --arg hash "$desired_hash" \
        --rawfile metrics "$KIP_DCGM_METRICS_FILE" '
        {
          apiVersion:"v1", kind:"ConfigMap",
          metadata:{
            name:$name, namespace:$namespace,
            labels:{"app.kubernetes.io/managed-by":"kube-inference-platform"},
            annotations:{"kube-inference-platform.io/metrics-sha256":$hash}
          },
          data:{($key):$metrics}
        }
    ') || return 1
    printf '%s\n' "$manifest" | kip_kubectl apply -f - >/dev/null
}

kip_observability_dcgm_config_exact() {
    kip_kubectl -n "$KIP_GPU_NAMESPACE" get configmap \
        "$KIP_DCGM_METRICS_CONFIG_NAME" >/dev/null 2>&1 \
        && kip_observability_dcgm_config_safe
}

kip_observability_recover_dcgm_config() {
    if kip_observability_dcgm_config_exact; then
        return 0
    fi
    kip_observability_dcgm_config_safe || {
        KIP_OBSERVABILITY_DCGM_ERROR="The DCGM collector ConfigMap is unmanaged or its pinned content has drifted."
        return 1
    }
    kip_observability_apply_dcgm_config || {
        KIP_OBSERVABILITY_DCGM_ERROR="The missing pinned DCGM collector ConfigMap could not be restored."
        return 1
    }
    kip_observability_dcgm_config_exact || {
        KIP_OBSERVABILITY_DCGM_ERROR="The restored DCGM collector ConfigMap does not match the pinned content."
        return 1
    }
}

kip_observability_dcgm_cluster_policy_safe() {
    local object

    object=$(kip_kubectl get clusterpolicy cluster-policy \
        --ignore-not-found -o json 2>/dev/null) || return 1
    [ -n "$object" ] || return 0
    printf '%s' "$object" | kip_exec jq -e \
        --arg release "$KIP_GPU_RELEASE" \
        --arg namespace "$KIP_GPU_NAMESPACE" '
        .metadata.annotations["meta.helm.sh/release-name"] == $release
        and .metadata.annotations["meta.helm.sh/release-namespace"] == $namespace
        and .metadata.labels["app.kubernetes.io/managed-by"] == "Helm"' >/dev/null
}

kip_observability_dcgm_daemonset_safe() {
    local cluster_policy objects owner_uid

    objects=$(kip_kubectl -n "$KIP_GPU_NAMESPACE" get daemonsets -o json 2>/dev/null) \
        || return 1
    if printf '%s' "$objects" | kip_exec jq -e '
        any(.items[]?;
          .metadata.name == "nvidia-dcgm-exporter"
          or .metadata.labels.app == "nvidia-dcgm-exporter"
          or any(.spec.template.spec.containers[]?;
              .name == "nvidia-dcgm-exporter"))' >/dev/null; then
        cluster_policy=$(kip_kubectl get clusterpolicy cluster-policy \
            --ignore-not-found -o json 2>/dev/null) || return 1
        [ -n "$cluster_policy" ] || return 1
        owner_uid=$(printf '%s' "$cluster_policy" | kip_exec jq -er \
            '.metadata.uid | select(type == "string" and length > 0)') || return 1
    else
        owner_uid=""
    fi
    printf '%s' "$objects" | kip_exec jq -e \
        --arg ownerUid "$owner_uid" '
        [
          .items[]?
          | select(
              .metadata.name == "nvidia-dcgm-exporter"
              or .metadata.labels.app == "nvidia-dcgm-exporter"
              or any(.spec.template.spec.containers[]?;
                  .name == "nvidia-dcgm-exporter")
            )
        ] as $exporters
        | ($exporters | length) <= 1
        and all($exporters[];
          any(.metadata.ownerReferences[]?;
            .apiVersion == "nvidia.com/v1"
            and .kind == "ClusterPolicy"
            and .name == "cluster-policy"
            and .controller == true
            and .uid == $ownerUid))' >/dev/null
}

kip_observability_dcgm_live_state_safe() {
    kip_observability_dcgm_cluster_policy_safe \
        && kip_observability_dcgm_daemonset_safe
}

kip_observability_dcgm_cluster_policy_exact() {
    local object

    object=$(kip_kubectl get clusterpolicy cluster-policy -o json 2>/dev/null) \
        || return 1
    printf '%s' "$object" | kip_exec jq -e \
        --arg release "$KIP_GPU_RELEASE" \
        --arg namespace "$KIP_GPU_NAMESPACE" \
        --arg repository "$KIP_DCGM_EXPORTER_REPOSITORY" \
        --arg image "$KIP_DCGM_EXPORTER_IMAGE" \
        --arg version "$KIP_DCGM_EXPORTER_PINNED_VERSION" \
        --arg config "$KIP_DCGM_METRICS_CONFIG_NAME" \
        --arg collectors "$KIP_DCGM_METRICS_COLLECTOR_PATH" '
        .metadata.annotations["meta.helm.sh/release-name"] == $release
        and .metadata.annotations["meta.helm.sh/release-namespace"] == $namespace
        and .metadata.labels["app.kubernetes.io/managed-by"] == "Helm"
        and .spec.dcgmExporter.enabled == true
        and .spec.dcgmExporter.repository == $repository
        and .spec.dcgmExporter.image == $image
        and .spec.dcgmExporter.version == $version
        and .spec.dcgmExporter.config == {"name": $config}
        and ((.spec.dcgmExporter.serviceMonitor.enabled // false) == false)
        and .spec.dcgmExporter.env == [
          {"name": "DCGM_EXPORTER_INTERVAL", "value": "1000"},
          {"name": "DCGM_EXPORTER_COLLECTORS", "value": $collectors}
        ]' >/dev/null
}

kip_observability_dcgm_daemonset_exact() {
    local cluster_policy objects owner_uid

    cluster_policy=$(kip_kubectl get clusterpolicy cluster-policy -o json 2>/dev/null) \
        || return 1
    owner_uid=$(printf '%s' "$cluster_policy" | kip_exec jq -er \
        '.metadata.uid | select(type == "string" and length > 0)') || return 1
    objects=$(kip_kubectl -n "$KIP_GPU_NAMESPACE" get daemonsets -o json 2>/dev/null) \
        || return 1
    printf '%s' "$objects" | kip_exec jq -e \
        --arg image "$KIP_DCGM_EXPORTER_IMAGE_REF" \
        --arg config "$KIP_DCGM_METRICS_CONFIG_NAME" \
        --arg collectors "$KIP_DCGM_METRICS_COLLECTOR_PATH" \
        --arg ownerUid "$owner_uid" '
        [
          .items[]?
          | select(
              .metadata.name == "nvidia-dcgm-exporter"
              or .metadata.labels.app == "nvidia-dcgm-exporter"
              or any(.spec.template.spec.containers[]?;
                  .name == "nvidia-dcgm-exporter")
            )
        ] as $exporters
        | ($exporters | length) == 1
        and ($exporters[0] as $exporter
          | any($exporter.metadata.ownerReferences[]?;
              .apiVersion == "nvidia.com/v1"
              and .kind == "ClusterPolicy"
              and .name == "cluster-policy"
              and .controller == true
              and .uid == $ownerUid)
          and ([$exporter.spec.template.spec.containers[]?
            | select(.name == "nvidia-dcgm-exporter")] | length) == 1
          and ($exporter.spec.template.spec.containers[]
            | select(.name == "nvidia-dcgm-exporter")) as $container
          | $container.image == $image
          and ([$container.env[]?
            | select(.name == "DCGM_EXPORTER_INTERVAL" and .value == "1000")]
            | length) == 1
          and ([$container.env[]?
            | select(.name == "DCGM_EXPORTER_COLLECTORS" and .value == $collectors)]
            | length) == 1
          and ([$exporter.spec.template.spec.volumes[]?
            | select(.configMap.name == $config) | .name] | unique) as $volumeNames
          | ($volumeNames | length) == 1
          and any($container.volumeMounts[]?;
            . as $mount
            | ($volumeNames | index($mount.name)) != null
            and ($mount.mountPath == $collectors
              or ($collectors | startswith($mount.mountPath + "/")))))' >/dev/null
}

kip_observability_dcgm_live_state_exact() {
    kip_observability_dcgm_cluster_policy_exact \
        && kip_observability_dcgm_daemonset_exact
}

kip_observability_wait_for_dcgm_live_state() {
    local attempt=0

    while [ "$attempt" -lt 60 ]; do
        kip_observability_dcgm_live_state_exact && return 0
        attempt=$((attempt + 1))
        [ "$attempt" -lt 60 ] && kip_exec sleep 5
    done
    return 1
}

kip_observability_wait_for_dcgm_rollout() {
    kip_kubectl -n "$KIP_GPU_NAMESPACE" rollout status \
        daemonset/nvidia-dcgm-exporter --timeout=5m >/dev/null
}

kip_observability_gpu_operator_safe() {
    local recorded_mode

    KIP_OBSERVABILITY_DCGM_ERROR=""
    if ! kip_gpu_release_exists; then
        KIP_OBSERVABILITY_DCGM_ERROR="The KIP GPU Operator release is absent."
        return 1
    fi
    recorded_mode=$(kip_gpu_recorded_mode) || {
        KIP_OBSERVABILITY_DCGM_ERROR="The GPU Operator driver mode is missing or invalid."
        return 1
    }
    case "$recorded_mode" in
        operator | preinstalled) ;;
        *)
            KIP_OBSERVABILITY_DCGM_ERROR="The GPU Operator driver mode is missing or invalid."
            return 1
            ;;
    esac
    KIP_GPU_DRIVER_MODE=$recorded_mode
    if ! kip_gpu_release_is_exact; then
        KIP_OBSERVABILITY_DCGM_ERROR="The GPU Operator release is unmanaged or its chart, digest, mode, or values have drifted."
        return 1
    fi
}

kip_observability_dcgm_exporter_exact() {
    kip_observability_gpu_operator_safe \
        && kip_gpu_release_is_exact_for_dcgm_state true \
        && kip_observability_dcgm_config_exact \
        && kip_observability_dcgm_live_state_exact
}

kip_observability_recover_gpu_operator() {
    local current_state recorded_mode

    kip_gpu_release_exists || return 1
    recorded_mode=$(kip_gpu_recorded_mode) || return 1
    case "$recorded_mode" in
        operator | preinstalled) KIP_GPU_DRIVER_MODE=$recorded_mode ;;
        *) return 1 ;;
    esac
    if ! kip_gpu_release_is_exact; then
        kip_gpu_release_identity_exact || return 1
        kip_gpu_install_operator || return 1
    fi
    kip_observability_recover_dcgm_config || return 1

    current_state=$(kip_gpu_dcgm_exporter_state) || return 1
    case "$current_state" in
        false) return 0 ;;
        true) ;;
        *) return 1 ;;
    esac
    kip_observability_dcgm_live_state_safe || return 1
    if ! kip_observability_dcgm_live_state_exact; then
        kip_gpu_reconcile_operator true || return 1
        kip_observability_wait_for_dcgm_live_state || return 1
    fi
    kip_observability_wait_for_dcgm_rollout
}

kip_observability_reconcile_dcgm_exporter() {
    local current_state

    kip_observability_gpu_operator_safe || return 1
    kip_observability_recover_dcgm_config || return 1
    kip_observability_dcgm_live_state_safe || {
        KIP_OBSERVABILITY_DCGM_ERROR="The live ClusterPolicy or DCGM Exporter DaemonSet is not owned by the pinned GPU Operator release."
        return 1
    }
    current_state=$(kip_gpu_dcgm_exporter_state) || {
        KIP_OBSERVABILITY_DCGM_ERROR="The GPU Operator DCGM Exporter state is missing or invalid."
        return 1
    }
    case "$current_state" in
        false)
            kip_gpu_reconcile_operator true || {
                KIP_OBSERVABILITY_DCGM_ERROR="GPU Operator could not enable its pinned DCGM Exporter workload."
                return 1
            }
            kip_observability_wait_for_dcgm_live_state || {
                KIP_OBSERVABILITY_DCGM_ERROR="GPU Operator did not converge to the pinned DCGM Exporter state within five minutes."
                return 1
            }
            ;;
        true)
            if ! kip_observability_dcgm_live_state_exact; then
                kip_gpu_reconcile_operator true || {
                    KIP_OBSERVABILITY_DCGM_ERROR="GPU Operator could not reconcile its drifted DCGM Exporter workload."
                    return 1
                }
                kip_observability_wait_for_dcgm_live_state || {
                    KIP_OBSERVABILITY_DCGM_ERROR="GPU Operator did not restore the pinned DCGM Exporter state within five minutes."
                    return 1
                }
            fi
            ;;
        *)
            KIP_OBSERVABILITY_DCGM_ERROR="The GPU Operator DCGM Exporter state is invalid."
            return 1
            ;;
    esac
    if ! kip_gpu_release_is_exact_for_dcgm_state true \
        || ! kip_observability_dcgm_config_exact \
        || ! kip_observability_dcgm_live_state_exact \
        || ! kip_observability_wait_for_dcgm_rollout; then
        KIP_OBSERVABILITY_DCGM_ERROR="DCGM Exporter reconciliation did not produce the exact pinned state."
        return 1
    fi
}
