#!/usr/bin/env bash

# Read-only Deployment, Pod, Service, and API health checks.

KIP_RUNTIME_PORT_FORWARD_PID=""

kip_runtime_deployment_contract_matches() {
    local deployment

    deployment=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" get deployment \
        "$KIP_RUNTIME_RESOURCE_NAME" -o json 2>/dev/null) || return 1
    printf '%s' "$deployment" | kip_exec jq -e \
        --arg image "$KIP_VLLM_IMAGE" \
        --arg profile_hash "$KIP_RUNTIME_PROFILE_HASH" \
        --arg model "$KIP_RUNTIME_MODEL_ID" \
        --arg revision "$KIP_RUNTIME_MODEL_REVISION" \
        --arg served "$KIP_RUNTIME_SERVED_MODEL_NAME" \
        --arg port "$KIP_RUNTIME_SERVICE_PORT" \
        --arg cache_path "$KIP_RUNTIME_CACHE_PATH" \
        --arg shm_size "$KIP_RUNTIME_SHM_SIZE" \
        --arg cpu_request "$KIP_RUNTIME_CPU_REQUEST" \
        --arg memory_request "$KIP_RUNTIME_MEMORY_REQUEST" \
        --arg ephemeral_request "$KIP_RUNTIME_EPHEMERAL_REQUEST" \
        --arg gpu_request "$KIP_RUNTIME_GPU_REQUEST" \
        --arg cpu_limit "$KIP_RUNTIME_CPU_LIMIT" \
        --arg memory_limit "$KIP_RUNTIME_MEMORY_LIMIT" \
        --arg ephemeral_limit "$KIP_RUNTIME_EPHEMERAL_LIMIT" \
        --arg gpu_limit "$KIP_RUNTIME_GPU_LIMIT" \
        --arg dtype "$KIP_RUNTIME_DTYPE" \
        --arg tensor_parallel "$KIP_RUNTIME_TENSOR_PARALLEL_SIZE" \
        --arg max_model_len "$KIP_RUNTIME_MAX_MODEL_LENGTH" \
        --arg gpu_util "$KIP_RUNTIME_GPU_MEMORY_UTILIZATION" \
        --arg max_sequences "$KIP_RUNTIME_MAX_NUMBER_OF_SEQUENCES" \
        --arg seed "$KIP_RUNTIME_SEED" '
        .metadata.labels["app.kubernetes.io/managed-by"] == "kube-inference-platform"
        and .metadata.annotations["kube-inference-platform/profile-sha256"] == $profile_hash
        and .spec.replicas == 1
        and .spec.strategy.type == "Recreate"
        and .spec.progressDeadlineSeconds == 2700
        and .spec.template.spec.enableServiceLinks == false
        and .spec.template.spec.terminationGracePeriodSeconds == 120
        and .spec.template.spec.hostNetwork != true
        and .spec.template.spec.hostPID != true
        and .spec.template.spec.hostIPC != true
        and (.spec.template.spec.runtimeClassName == null)
        and .spec.template.spec.securityContext.seccompProfile.type == "RuntimeDefault"
        and .spec.template.spec.containers[0].image == $image
        and .spec.template.spec.containers[0].command == ["vllm", "serve"]
        and .spec.template.spec.containers[0].args == [
            $model, "--revision", $revision, "--tokenizer-revision", $revision,
            "--served-model-name", $served, "--host", "0.0.0.0", "--port", $port,
            "--dtype", $dtype, "--tensor-parallel-size", $tensor_parallel,
            "--max-model-len", $max_model_len, "--gpu-memory-utilization", $gpu_util,
            "--max-num-seqs", $max_sequences, "--seed", $seed,
            "--no-enable-prefix-caching"
        ]
        and .spec.template.spec.containers[0].resources.requests.cpu == $cpu_request
        and .spec.template.spec.containers[0].resources.requests.memory == $memory_request
        and .spec.template.spec.containers[0].resources.requests["ephemeral-storage"] == $ephemeral_request
        and .spec.template.spec.containers[0].resources.requests["nvidia.com/gpu"] == $gpu_request
        and .spec.template.spec.containers[0].resources.limits.cpu == $cpu_limit
        and .spec.template.spec.containers[0].resources.limits.memory == $memory_limit
        and .spec.template.spec.containers[0].resources.limits["ephemeral-storage"] == $ephemeral_limit
        and .spec.template.spec.containers[0].resources.limits["nvidia.com/gpu"] == $gpu_limit
        and .spec.template.spec.containers[0].securityContext.allowPrivilegeEscalation == false
        and (.spec.template.spec.containers[0].securityContext.capabilities.drop | index("ALL") != null)
        and .spec.template.spec.containers[0].startupProbe.httpGet.path == "/health"
        and .spec.template.spec.containers[0].startupProbe.periodSeconds == 10
        and .spec.template.spec.containers[0].startupProbe.timeoutSeconds == 5
        and .spec.template.spec.containers[0].startupProbe.failureThreshold == 180
        and .spec.template.spec.containers[0].readinessProbe.httpGet.path == "/health"
        and .spec.template.spec.containers[0].readinessProbe.periodSeconds == 5
        and .spec.template.spec.containers[0].readinessProbe.timeoutSeconds == 3
        and .spec.template.spec.containers[0].readinessProbe.failureThreshold == 3
        and .spec.template.spec.containers[0].livenessProbe.httpGet.path == "/health"
        and .spec.template.spec.containers[0].livenessProbe.periodSeconds == 15
        and .spec.template.spec.containers[0].livenessProbe.timeoutSeconds == 3
        and .spec.template.spec.containers[0].livenessProbe.failureThreshold == 4
        and any(.spec.template.spec.containers[0].env[]?;
            .name == "HF_HOME" and .value == "/var/cache/kip/huggingface")
        and any(.spec.template.spec.containers[0].volumeMounts[]?;
            .name == "model-cache" and .mountPath == "/var/cache/kip")
        and any(.spec.template.spec.containers[0].volumeMounts[]?;
            .name == "shared-memory" and .mountPath == "/dev/shm")
        and any(.spec.template.spec.volumes[]?;
            .name == "model-cache" and .hostPath.path == $cache_path
            and .hostPath.type == "Directory")
        and any(.spec.template.spec.volumes[]?;
            .name == "shared-memory" and .emptyDir.medium == "Memory"
            and .emptyDir.sizeLimit == $shm_size)
    ' >/dev/null
}

kip_runtime_deployment_exact() {
    local deployment

    kip_runtime_deployment_contract_matches || return 1
    deployment=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" get deployment \
        "$KIP_RUNTIME_RESOURCE_NAME" -o json 2>/dev/null) || return 1
    printf '%s' "$deployment" | kip_exec jq -e \
        '.status.availableReplicas == 1 and .status.readyReplicas == 1' >/dev/null
}

kip_runtime_pod_healthy() {
    local node pods

    pods=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" get pods \
        -l "app.kubernetes.io/name=vllm,app.kubernetes.io/instance=$KIP_MODEL_PROFILE" \
        -o json 2>/dev/null) || return 1
    KIP_RUNTIME_RESTARTS=$(printf '%s' "$pods" | kip_exec jq -r \
        '[.items[].status.containerStatuses[]?.restartCount] | add // 0') || return 1
    printf '%s' "$pods" | kip_exec jq -e '
        .items | length == 1
        and .[0].status.phase == "Running"
        and (.[0].spec.nodeName | length > 0)
        and any(.[0].status.conditions[]?; .type == "Ready" and .status == "True")
        and all(.[0].status.containerStatuses[]?;
            .ready == true
            and ((.state.waiting.reason // "")
                | test("CrashLoopBackOff|ImagePullBackOff|ErrImagePull|CreateContainerError") | not))
    ' >/dev/null || return 1
    node=$(printf '%s' "$pods" | kip_exec jq -r '.items[0].spec.nodeName') || return 1
    kip_kubectl get node "$node" -o json 2>/dev/null \
        | kip_exec jq -e '((.status.allocatable["nvidia.com/gpu"] // "0") | tonumber) > 0' \
            >/dev/null
}

kip_runtime_service_contract_matches() {
    local service

    service=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" get service \
        "$KIP_RUNTIME_RESOURCE_NAME" -o json 2>/dev/null) || return 1
    printf '%s' "$service" | kip_exec jq -e \
        --arg profile "$KIP_MODEL_PROFILE" \
        --argjson port "$KIP_RUNTIME_SERVICE_PORT" '
        .metadata.labels["app.kubernetes.io/managed-by"] == "kube-inference-platform"
        and .spec.type == "ClusterIP"
        and .spec.selector["app.kubernetes.io/name"] == "vllm"
        and .spec.selector["app.kubernetes.io/instance"] == $profile
        and (.spec.ports | length == 1)
        and .spec.ports[0].port == $port
        and .spec.ports[0].targetPort == "http"
    ' >/dev/null
}

kip_runtime_service_healthy() {
    local endpoint_slices

    kip_runtime_service_contract_matches || return 1
    endpoint_slices=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" get endpointslice \
        -l "kubernetes.io/service-name=$KIP_RUNTIME_RESOURCE_NAME" \
        -o json 2>/dev/null) || return 1
    printf '%s' "$endpoint_slices" | kip_exec jq -e '
        [.items[].endpoints[]?
            | select(.conditions.ready != false)
            | .addresses[]?]
        | length == 1
    ' >/dev/null
}

kip_runtime_stop_port_forward() {
    if [ -n "$KIP_RUNTIME_PORT_FORWARD_PID" ]; then
        command kill "$KIP_RUNTIME_PORT_FORWARD_PID" >/dev/null 2>&1 || true
        command wait "$KIP_RUNTIME_PORT_FORWARD_PID" >/dev/null 2>&1 || true
        KIP_RUNTIME_PORT_FORWARD_PID=""
    fi
}

kip_runtime_api_healthy() {
    local attempts=0 base_url health models result port=${KIP_RUNTIME_LOCAL_PORT:-18080}

    base_url="http://127.0.0.1:$port"
    if [ "${KIP_TESTING:-0}" != 1 ]; then
        kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" port-forward \
            "service/$KIP_RUNTIME_RESOURCE_NAME" "$port:$KIP_RUNTIME_SERVICE_PORT" \
            >"$KIP_WORK_DIR/port-forward.log" 2>&1 &
        KIP_RUNTIME_PORT_FORWARD_PID=$!
        while [ "$attempts" -lt 30 ]; do
            if kip_exec curl --fail --silent --show-error "$base_url/health" >/dev/null 2>&1; then
                break
            fi
            command sleep 1
            attempts=$((attempts + 1))
        done
        [ "$attempts" -lt 30 ] || {
            kip_runtime_stop_port_forward
            return 1
        }
    fi

    health=$(kip_exec curl --fail --silent --show-error "$base_url/health" 2>/dev/null) \
        || {
            kip_runtime_stop_port_forward
            return 1
        }
    models=$(kip_exec curl --fail --silent --show-error "$base_url/v1/models" 2>/dev/null) \
        || {
            kip_runtime_stop_port_forward
            return 1
        }
    kip_verbose_output "vLLM /health" "$health"
    kip_verbose_output "vLLM /v1/models" "$models"
    printf '%s' "$models" | kip_exec jq -e --arg model "$KIP_RUNTIME_SERVED_MODEL_NAME" \
        '[.data[].id] == [$model]' >/dev/null
    result=$?
    kip_runtime_stop_port_forward
    return "$result"
}

kip_runtime_diagnostics() {
    local output

    printf '\nRuntime diagnostics:\n' >&2
    for command_name in deployment pods logs events; do
        case "$command_name" in
            deployment)
                output=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" describe deployment \
                    "$KIP_RUNTIME_RESOURCE_NAME" 2>&1 || true)
                ;;
            pods)
                output=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" get pods -o wide 2>&1 || true)
                ;;
            logs)
                output=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" logs \
                    "deployment/$KIP_RUNTIME_RESOURCE_NAME" --all-containers --tail=200 \
                    2>&1 || true)
                ;;
            events)
                output=$(kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" get events \
                    --sort-by=.lastTimestamp 2>&1 || true)
                ;;
        esac
        printf '%s\n%s\n' "--- $command_name ---" "${output:-no output}" >&2
    done
}
