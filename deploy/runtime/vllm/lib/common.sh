#!/usr/bin/env bash

# Shared vLLM profile, ownership, and command helpers.

KIP_RUNTIME_NAMESPACE=""
KIP_RUNTIME_RESOURCE_NAME=""
KIP_RUNTIME_PROFILE_HASH=""
KIP_RUNTIME_MODEL_ID=""
KIP_RUNTIME_MODEL_REVISION=""
KIP_RUNTIME_SERVED_MODEL_NAME=""
KIP_RUNTIME_HF_SECRET_NAME=""
KIP_RUNTIME_REPLICAS=0
KIP_RUNTIME_SERVICE_PORT=0
KIP_RUNTIME_CACHE_PATH=""
KIP_RUNTIME_SHM_SIZE=""
KIP_RUNTIME_CPU_REQUEST=""
KIP_RUNTIME_MEMORY_REQUEST=""
KIP_RUNTIME_EPHEMERAL_REQUEST=""
KIP_RUNTIME_GPU_REQUEST=0
KIP_RUNTIME_CPU_LIMIT=""
KIP_RUNTIME_MEMORY_LIMIT=""
KIP_RUNTIME_EPHEMERAL_LIMIT=""
KIP_RUNTIME_GPU_LIMIT=0
KIP_RUNTIME_DTYPE=""
KIP_RUNTIME_TENSOR_PARALLEL_SIZE=0
KIP_RUNTIME_MAX_MODEL_LENGTH=0
KIP_RUNTIME_GPU_MEMORY_UTILIZATION=""
KIP_RUNTIME_MAX_NUMBER_OF_SEQUENCES=0
KIP_RUNTIME_SEED=0
KIP_RUNTIME_PREFLIGHT_ERROR=""
KIP_RUNTIME_PREFLIGHT_STATE=""
KIP_RUNTIME_RESTARTS=0

kip_runtime_profile_file() {
    printf '%s/profiles/%s.json' "$KIP_RUNTIME_ROOT" "$KIP_MODEL_PROFILE"
}

kip_runtime_validate_profile_schema() {
    local profile=$1

    kip_exec jq -e '
        (keys | sort) == ["kubernetes", "model", "name", "resourceName", "schemaVersion", "vllm"]
        and (.model | keys | sort) == ["huggingFaceSecretName", "id", "revision", "servedName"]
        and (.kubernetes | keys | sort) == ["cachePath", "namespace", "replicas", "resources", "servicePort", "sharedMemorySize"]
        and (.kubernetes.resources | keys | sort) == ["limits", "requests"]
        and (.kubernetes.resources.requests | keys | sort) == ["cpu", "ephemeralStorage", "gpu", "memory"]
        and (.kubernetes.resources.limits | keys | sort) == ["cpu", "ephemeralStorage", "gpu", "memory"]
        and (.vllm | keys | sort) == ["cpuOffloadGiB", "distributed", "dtype", "gpuMemoryUtilization", "maxModelLength", "maxNumberOfSequences", "prefixCaching", "quantization", "seed", "speculativeDecoding", "tensorParallelSize", "trustRemoteCode"]
        and .schemaVersion == 1
        and (.name | length <= 63)
        and (.name | test("^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$"))
        and (.resourceName | length <= 63)
        and (.resourceName | test("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"))
        and (.model.id | test("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$"))
        and (.model.revision | test("^[0-9a-f]{40}$"))
        and (.model.servedName | test("^[A-Za-z0-9._-]+$"))
        and ((.model.huggingFaceSecretName == null)
            or ((.model.huggingFaceSecretName | length <= 253)
                and (.model.huggingFaceSecretName | test("^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$"))))
        and (.kubernetes.namespace | length <= 63)
        and (.kubernetes.namespace | test("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"))
        and .kubernetes.replicas == 1
        and (.kubernetes.servicePort | type == "number" and . >= 1 and . <= 65535)
        and (.kubernetes.cachePath | startswith("/var/lib/kube-inference-platform/model-cache/"))
        and (.kubernetes.sharedMemorySize | test("^[1-9][0-9]*(Mi|Gi)$"))
        and (.kubernetes.resources.requests.cpu | test("^[1-9][0-9]*(m)?$"))
        and (.kubernetes.resources.requests.memory | test("^[1-9][0-9]*(Mi|Gi)$"))
        and (.kubernetes.resources.requests.ephemeralStorage | test("^[1-9][0-9]*(Mi|Gi)$"))
        and .kubernetes.resources.requests.gpu == 1
        and (.kubernetes.resources.limits.cpu | test("^[1-9][0-9]*(m)?$"))
        and (.kubernetes.resources.limits.memory | test("^[1-9][0-9]*(Mi|Gi)$"))
        and (.kubernetes.resources.limits.ephemeralStorage | test("^[1-9][0-9]*(Mi|Gi)$"))
        and .kubernetes.resources.limits.gpu == 1
        and .vllm.dtype == "bfloat16"
        and .vllm.tensorParallelSize == 1
        and (.vllm.maxModelLength | type == "number" and . > 0)
        and (.vllm.gpuMemoryUtilization | type == "number" and . > 0 and . < 1)
        and (.vllm.maxNumberOfSequences | type == "number" and . > 0)
        and (.vllm.seed | type == "number" and . >= 0)
        and .vllm.quantization == null
        and .vllm.cpuOffloadGiB == 0
        and .vllm.trustRemoteCode == false
        and .vllm.distributed == false
        and .vllm.speculativeDecoding == false
        and .vllm.prefixCaching == false
    ' "$profile" >/dev/null
}

kip_runtime_load_profile() {
    local profile

    profile=$(kip_runtime_profile_file)
    [ -r "$profile" ] || kip_usage_error "runtime profile is not readable: $KIP_MODEL_PROFILE."
    kip_runtime_validate_profile_schema "$profile" \
        || kip_usage_error "runtime profile failed schema or safety validation: $KIP_MODEL_PROFILE."
    [ "$(kip_exec jq -r '.name' "$profile")" = "$KIP_MODEL_PROFILE" ] \
        || kip_usage_error "MODEL_PROFILE does not match the profile name."

    KIP_RUNTIME_PROFILE_HASH=$(kip_exec jq -cS . "$profile" \
        | kip_exec sha256sum \
        | command awk '{print $1}') || return 1
    KIP_RUNTIME_NAMESPACE=$(kip_exec jq -r '.kubernetes.namespace' "$profile")
    KIP_RUNTIME_RESOURCE_NAME=$(kip_exec jq -r '.resourceName' "$profile")
    KIP_RUNTIME_MODEL_ID=$(kip_exec jq -r '.model.id' "$profile")
    KIP_RUNTIME_MODEL_REVISION=$(kip_exec jq -r '.model.revision' "$profile")
    KIP_RUNTIME_SERVED_MODEL_NAME=$(kip_exec jq -r '.model.servedName' "$profile")
    KIP_RUNTIME_HF_SECRET_NAME=$(kip_exec jq -r '.model.huggingFaceSecretName // ""' "$profile")
    KIP_RUNTIME_REPLICAS=$(kip_exec jq -r '.kubernetes.replicas' "$profile")
    KIP_RUNTIME_SERVICE_PORT=$(kip_exec jq -r '.kubernetes.servicePort' "$profile")
    KIP_RUNTIME_CACHE_PATH=$(kip_exec jq -r '.kubernetes.cachePath' "$profile")
    if [ "${KIP_TESTING:-0}" = 1 ] && [ "$KIP_VAR_ROOT" != /var ]; then
        KIP_RUNTIME_CACHE_PATH="$KIP_VAR_ROOT${KIP_RUNTIME_CACHE_PATH#/var}"
    fi
    KIP_RUNTIME_SHM_SIZE=$(kip_exec jq -r '.kubernetes.sharedMemorySize' "$profile")
    KIP_RUNTIME_CPU_REQUEST=$(kip_exec jq -r '.kubernetes.resources.requests.cpu' "$profile")
    KIP_RUNTIME_MEMORY_REQUEST=$(kip_exec jq -r '.kubernetes.resources.requests.memory' "$profile")
    KIP_RUNTIME_EPHEMERAL_REQUEST=$(kip_exec jq -r '.kubernetes.resources.requests.ephemeralStorage' "$profile")
    KIP_RUNTIME_GPU_REQUEST=$(kip_exec jq -r '.kubernetes.resources.requests.gpu' "$profile")
    KIP_RUNTIME_CPU_LIMIT=$(kip_exec jq -r '.kubernetes.resources.limits.cpu' "$profile")
    KIP_RUNTIME_MEMORY_LIMIT=$(kip_exec jq -r '.kubernetes.resources.limits.memory' "$profile")
    KIP_RUNTIME_EPHEMERAL_LIMIT=$(kip_exec jq -r '.kubernetes.resources.limits.ephemeralStorage' "$profile")
    KIP_RUNTIME_GPU_LIMIT=$(kip_exec jq -r '.kubernetes.resources.limits.gpu' "$profile")
    KIP_RUNTIME_DTYPE=$(kip_exec jq -r '.vllm.dtype' "$profile")
    KIP_RUNTIME_TENSOR_PARALLEL_SIZE=$(kip_exec jq -r '.vllm.tensorParallelSize' "$profile")
    KIP_RUNTIME_MAX_MODEL_LENGTH=$(kip_exec jq -r '.vllm.maxModelLength' "$profile")
    KIP_RUNTIME_GPU_MEMORY_UTILIZATION=$(kip_exec jq -r '.vllm.gpuMemoryUtilization' "$profile")
    KIP_RUNTIME_MAX_NUMBER_OF_SEQUENCES=$(kip_exec jq -r '.vllm.maxNumberOfSequences' "$profile")
    KIP_RUNTIME_SEED=$(kip_exec jq -r '.vllm.seed' "$profile")
}

kip_runtime_required_commands_available() {
    local command_name

    for command_name in curl df jq kubectl sha256sum; do
        kip_has_command "$command_name" || return 1
    done
}

kip_runtime_run_cluster_check() {
    KIP_NO_LOGO=1 KIP_VERBOSE="$KIP_VERBOSE" \
        "${KIP_CLUSTER_CHECK_SCRIPT:-$KIP_CLUSTER_ROOT/validate.sh}"
}

kip_runtime_run_gpu_check() {
    KIP_NO_LOGO=1 KIP_VERBOSE="$KIP_VERBOSE" \
        "${KIP_GPU_CHECK_SCRIPT:-$KIP_GPU_ROOT/validate.sh}"
}

kip_runtime_annotation_value() {
    local kind=$1 name=$2 key=$3

    kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" get "$kind" "$name" \
        -o "jsonpath={.metadata.annotations.$key}" 2>/dev/null
}

kip_runtime_resource_exists() {
    kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" get "$1" "$2" >/dev/null 2>&1
}

kip_runtime_resource_is_owned() {
    local kind=$1 name=$2 value

    value=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" get "$kind" "$name" \
        -o 'jsonpath={.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null) \
        || return 1
    [ "$value" = kube-inference-platform ]
}

kip_runtime_identity_matches() {
    local kind=$1 name=$2
    local digest model_revision profile profile_hash runtime_version

    kip_runtime_resource_is_owned "$kind" "$name" || return 1
    profile=$(kip_runtime_annotation_value "$kind" "$name" \
        'kube-inference-platform/profile') || return 1
    profile_hash=$(kip_runtime_annotation_value "$kind" "$name" \
        'kube-inference-platform/profile-sha256') || return 1
    runtime_version=$(kip_runtime_annotation_value "$kind" "$name" \
        'kube-inference-platform/runtime-version') || return 1
    model_revision=$(kip_runtime_annotation_value "$kind" "$name" \
        'kube-inference-platform/model-revision') || return 1
    digest=$(kip_runtime_annotation_value "$kind" "$name" \
        'kube-inference-platform/image-digest') || return 1
    [ "$profile" = "$KIP_MODEL_PROFILE" ] \
        && [ "$profile_hash" = "$KIP_RUNTIME_PROFILE_HASH" ] \
        && [ "$runtime_version" = "$KIP_VLLM_VERSION" ] \
        && [ "$model_revision" = "$KIP_RUNTIME_MODEL_REVISION" ] \
        && [ "$digest" = "$KIP_VLLM_IMAGE_DIGEST" ]
}
