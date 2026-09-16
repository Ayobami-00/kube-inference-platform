#!/usr/bin/env bash

# Render the committed profile into temporary Kubernetes manifests.

kip_runtime_render_file() {
    local source=$1 destination=$2

    command awk \
        -v namespace="$KIP_RUNTIME_NAMESPACE" \
        -v resource_name="$KIP_RUNTIME_RESOURCE_NAME" \
        -v profile_name="$KIP_MODEL_PROFILE" \
        -v profile_hash="$KIP_RUNTIME_PROFILE_HASH" \
        -v vllm_version="$KIP_VLLM_VERSION" \
        -v vllm_image="$KIP_VLLM_IMAGE" \
        -v image_digest="$KIP_VLLM_IMAGE_DIGEST" \
        -v model_id="$KIP_RUNTIME_MODEL_ID" \
        -v model_revision="$KIP_RUNTIME_MODEL_REVISION" \
        -v served_model="$KIP_RUNTIME_SERVED_MODEL_NAME" \
        -v replicas="$KIP_RUNTIME_REPLICAS" \
        -v service_port="$KIP_RUNTIME_SERVICE_PORT" \
        -v cache_path="$KIP_RUNTIME_CACHE_PATH" \
        -v shm_size="$KIP_RUNTIME_SHM_SIZE" \
        -v cpu_request="$KIP_RUNTIME_CPU_REQUEST" \
        -v memory_request="$KIP_RUNTIME_MEMORY_REQUEST" \
        -v ephemeral_request="$KIP_RUNTIME_EPHEMERAL_REQUEST" \
        -v gpu_request="$KIP_RUNTIME_GPU_REQUEST" \
        -v cpu_limit="$KIP_RUNTIME_CPU_LIMIT" \
        -v memory_limit="$KIP_RUNTIME_MEMORY_LIMIT" \
        -v ephemeral_limit="$KIP_RUNTIME_EPHEMERAL_LIMIT" \
        -v gpu_limit="$KIP_RUNTIME_GPU_LIMIT" \
        -v dtype="$KIP_RUNTIME_DTYPE" \
        -v tensor_parallel="$KIP_RUNTIME_TENSOR_PARALLEL_SIZE" \
        -v max_model_len="$KIP_RUNTIME_MAX_MODEL_LENGTH" \
        -v gpu_util="$KIP_RUNTIME_GPU_MEMORY_UTILIZATION" \
        -v max_sequences="$KIP_RUNTIME_MAX_NUMBER_OF_SEQUENCES" \
        -v seed="$KIP_RUNTIME_SEED" \
        -v hf_secret="$KIP_RUNTIME_HF_SECRET_NAME" '
        /__HF_TOKEN_ENV__/ {
            if (hf_secret != "") {
                print "            - name: HF_TOKEN"
                print "              valueFrom:"
                print "                secretKeyRef:"
                print "                  name: " hf_secret
                print "                  key: token"
            }
            next
        }
        {
            gsub(/__NAMESPACE__/, namespace)
            gsub(/__RESOURCE_NAME__/, resource_name)
            gsub(/__PROFILE_NAME__/, profile_name)
            gsub(/__PROFILE_HASH__/, profile_hash)
            gsub(/__VLLM_VERSION__/, vllm_version)
            gsub(/__VLLM_IMAGE__/, vllm_image)
            gsub(/__IMAGE_DIGEST__/, image_digest)
            gsub(/__MODEL_ID__/, model_id)
            gsub(/__MODEL_REVISION__/, model_revision)
            gsub(/__SERVED_MODEL_NAME__/, served_model)
            gsub(/__REPLICAS__/, replicas)
            gsub(/__SERVICE_PORT__/, service_port)
            gsub(/__CACHE_PATH__/, cache_path)
            gsub(/__SHARED_MEMORY_SIZE__/, shm_size)
            gsub(/__CPU_REQUEST__/, cpu_request)
            gsub(/__MEMORY_REQUEST__/, memory_request)
            gsub(/__EPHEMERAL_REQUEST__/, ephemeral_request)
            gsub(/__GPU_REQUEST__/, gpu_request)
            gsub(/__CPU_LIMIT__/, cpu_limit)
            gsub(/__MEMORY_LIMIT__/, memory_limit)
            gsub(/__EPHEMERAL_LIMIT__/, ephemeral_limit)
            gsub(/__GPU_LIMIT__/, gpu_limit)
            gsub(/__DTYPE__/, dtype)
            gsub(/__TENSOR_PARALLEL_SIZE__/, tensor_parallel)
            gsub(/__MAX_MODEL_LENGTH__/, max_model_len)
            gsub(/__GPU_MEMORY_UTILIZATION__/, gpu_util)
            gsub(/__MAX_NUMBER_OF_SEQUENCES__/, max_sequences)
            gsub(/__SEED__/, seed)
            print
        }
    ' "$source" > "$destination"
}

kip_runtime_render_manifests() {
    kip_runtime_render_file "$KIP_RUNTIME_ROOT/manifests/namespace.yaml" \
        "$KIP_WORK_DIR/namespace.yaml"
    kip_runtime_render_file "$KIP_RUNTIME_ROOT/manifests/service.yaml.tpl" \
        "$KIP_WORK_DIR/service.yaml"
    kip_runtime_render_file "$KIP_RUNTIME_ROOT/manifests/deployment.yaml.tpl" \
        "$KIP_WORK_DIR/deployment.yaml"
    kip_runtime_render_file "$KIP_RUNTIME_ROOT/manifests/smoke-pod.yaml.tpl" \
        "$KIP_WORK_DIR/smoke-pod.yaml"
}

kip_runtime_apply_manifests() {
    local output

    output=$(kip_kubectl apply -f "$KIP_WORK_DIR/namespace.yaml" 2>&1) || return 1
    kip_verbose_output "runtime namespace apply" "$output"
    output=$(kip_kubectl apply -f "$KIP_WORK_DIR/service.yaml" 2>&1) || return 1
    kip_verbose_output "runtime Service apply" "$output"
    output=$(kip_kubectl apply -f "$KIP_WORK_DIR/deployment.yaml" 2>&1) || return 1
    kip_verbose_output "runtime Deployment apply" "$output"
    kip_kubectl -n "$KIP_RUNTIME_NAMESPACE" rollout status \
        "deployment/$KIP_RUNTIME_RESOURCE_NAME" --timeout=45m
}
