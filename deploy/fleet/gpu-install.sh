#!/usr/bin/env bash
# Fleet-only GPU Operator checkpoint; run on the owned control plane.
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
KIP_DEPLOY_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
readonly KIP_DEPLOY_ROOT
KIP_CLUSTER_ROOT="$KIP_DEPLOY_ROOT/cluster"
KIP_GPU_ROOT="$KIP_DEPLOY_ROOT/gpu"
readonly KIP_CLUSTER_ROOT KIP_GPU_ROOT

# shellcheck source=deploy/lib/ui.sh
source "$KIP_DEPLOY_ROOT/lib/ui.sh"
# shellcheck source=deploy/cluster/versions.env
source "$KIP_CLUSTER_ROOT/versions.env"
# shellcheck source=deploy/gpu/versions.env
source "$KIP_GPU_ROOT/versions.env"
# shellcheck source=deploy/cluster/lib/common.sh
source "$KIP_CLUSTER_ROOT/lib/common.sh"
# shellcheck source=deploy/cluster/lib/kubernetes.sh
source "$KIP_CLUSTER_ROOT/lib/kubernetes.sh"
# shellcheck source=deploy/gpu/lib/common.sh
source "$KIP_GPU_ROOT/lib/common.sh"
# shellcheck source=deploy/gpu/lib/preflight.sh
source "$KIP_GPU_ROOT/lib/preflight.sh"
# shellcheck source=deploy/gpu/lib/operator.sh
source "$KIP_GPU_ROOT/lib/operator.sh"
# shellcheck source=deploy/gpu/lib/health.sh
source "$KIP_GPU_ROOT/lib/health.sh"

export LC_ALL=C
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_FLEET_GPU_COUNT=${KIP_FLEET_GPU_COUNT:-1}
KIP_GPU_DRIVER_MODE=preinstalled
KIP_WORK_DIR=""
kip_ui_init
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
case "$KIP_FLEET_GPU_COUNT" in 1 | 2) ;; *) kip_usage_error "FLEET_GPU_COUNT must be 1 or 2." ;; esac
[ "$#" -eq 0 ] || kip_usage_error "fleet-gpu-install accepts no positional arguments."
KIP_WORK_DIR=$(command mktemp -d "${TMPDIR:-/tmp}/kip-fleet-gpu.XXXXXX")
readonly KIP_WORK_DIR
KIP_FLEET_GPU_SMOKE_CREATED=""
kip_fleet_gpu_cleanup() {
    local status=$? pod
    trap - EXIT
    for pod in $KIP_FLEET_GPU_SMOKE_CREATED; do
        if ! kip_kubectl -n default delete pod "$pod" --wait=true --timeout=2m \
            >/dev/null 2>&1; then
            printf 'Could not remove temporary Pod/%s.\n' "$pod" >&2
            status=1
        fi
    done
    command rm -rf "$KIP_WORK_DIR"
    exit "$status"
}
trap kip_fleet_gpu_cleanup EXIT
trap 'kip_unexpected_error "$?" "$LINENO"' ERR

kip_fleet_gpu_nodes_ready() {
    local nodes
    [ "$(command hostname)" = kip-control ] || return 1
    nodes=$(kip_kubectl get nodes -o json) || return 1
    printf '%s' "$nodes" | kip_exec jq -e --argjson count "$KIP_FLEET_GPU_COUNT" '
        (.items | length) == ($count + 1)
        and ([.items[].metadata.name] | sort)
            == (if $count == 1 then ["kip-control", "kip-gpu-1"]
                else ["kip-control", "kip-gpu-1", "kip-gpu-2"] end)
        and all(.items[];
            .spec.unschedulable != true
            and any(.status.conditions[]?; .type == "Ready" and .status == "True")
            and (.metadata.name as $name
                | if $name == "kip-control" then
                    (.metadata.labels["nvidia.com/gpu.deploy.operands"] // "false")
                        == "false"
                    and (.metadata.labels["kube-inference-platform.io/node-role"]
                        // "control-plane") == "control-plane"
                  else
                    (.metadata.labels["kube-inference-platform.io/node-role"]
                        // "gpu-worker") == "gpu-worker"
                  end))
    ' >/dev/null
}

kip_fleet_gpu_label_nodes() {
    local node label role
    for node in kip-control kip-gpu-1; do
        if [ "$node" = kip-control ]; then
            label='nvidia.com/gpu.deploy.operands=false'
            role=control-plane
        else
            label='kube-inference-platform.io/fleet-role=gpu-worker'
            role=gpu-worker
        fi
        kip_kubectl label node "$node" "$label" --overwrite >/dev/null || return 1
        kip_kubectl label node "$node" "kube-inference-platform.io/node-role=$role" \
            --overwrite >/dev/null || return 1
    done
    if [ "$KIP_FLEET_GPU_COUNT" = 2 ]; then
        kip_kubectl label node kip-gpu-2 \
            kube-inference-platform.io/fleet-role=gpu-worker --overwrite >/dev/null || return 1
        kip_kubectl label node kip-gpu-2 \
            kube-inference-platform.io/node-role=gpu-worker --overwrite >/dev/null || return 1
    fi
}

kip_fleet_gpu_capacity_exact() {
    local nodes
    nodes=$(kip_kubectl get nodes -o json) || return 1
    printf '%s' "$nodes" | kip_exec jq -e --argjson count "$KIP_FLEET_GPU_COUNT" '
        all(.items[];
            .metadata.name as $name
            | ((.status.capacity["nvidia.com/gpu"] // "0") | tonumber) as $capacity
            | ((.status.allocatable["nvidia.com/gpu"] // "0") | tonumber) as $allocatable
            | if $name == "kip-control" then $capacity == 0 and $allocatable == 0
              else $capacity == 1 and $allocatable == 1 end)
        and ([.items[] | select(.metadata.name | startswith("kip-gpu-"))] | length) == $count
    ' >/dev/null
}

kip_fleet_gpu_wait() {
    local deadline=$(( $(command date +%s) + 1500 ))
    while true; do
        if kip_gpu_cluster_policy_ready && kip_gpu_operand_pods_healthy \
            && kip_gpu_no_partitioning_or_sharing && kip_fleet_gpu_capacity_exact; then
            return 0
        fi
        [ "$(command date +%s)" -lt "$deadline" ] || return 1
        command sleep 10
    done
}

kip_fleet_gpu_smoke_each() {
    local index node pod logs scheduled
    for index in $(command seq 1 "$KIP_FLEET_GPU_COUNT"); do
        node="kip-gpu-$index"
        pod="kip-fleet-gpu-smoke-$index"
        if kip_kubectl -n default get pod "$pod" >/dev/null 2>&1; then
            printf 'Existing Pod/%s requires inspection; refusing overwrite.\n' "$pod" >&2
            return 1
        fi
        kip_exec jq -n --arg name "$pod" --arg node "$node" \
            --arg image "$KIP_CUDA_SMOKE_IMAGE" '{
            apiVersion: "v1", kind: "Pod",
            metadata: {name: $name, namespace: "default",
                labels: {"app.kubernetes.io/managed-by": "kube-inference-platform"}},
            spec: {restartPolicy: "Never", activeDeadlineSeconds: 300,
                nodeSelector: {"kubernetes.io/hostname": $node},
                containers: [{name: "nvidia-smi", image: $image,
                    imagePullPolicy: "IfNotPresent",
                    command: ["nvidia-smi"],
                    args: ["--query-gpu=index,name,uuid,driver_version,memory.total",
                        "--format=csv,noheader"],
                    resources: {requests: {"nvidia.com/gpu": "1"},
                        limits: {"nvidia.com/gpu": "1"}}}]}
        }' | kip_kubectl apply -f - >/dev/null || return 1
        KIP_FLEET_GPU_SMOKE_CREATED="$KIP_FLEET_GPU_SMOKE_CREATED $pod"
        if ! kip_kubectl -n default wait --for='jsonpath={.status.phase}=Succeeded' \
            "pod/$pod" --timeout=5m >/dev/null; then
            kip_kubectl -n default describe pod "$pod" >&2 || true
            kip_kubectl -n default logs "$pod" >&2 || true
            return 1
        fi
        scheduled=$(kip_kubectl -n default get pod "$pod" -o 'jsonpath={.spec.nodeName}') \
            || return 1
        logs=$(kip_kubectl -n default logs "$pod") || return 1
        [ "$scheduled" = "$node" ] || return 1
        printf '%s\n' "$logs" | command awk -F',' '
            NF >= 5 && $1 ~ /^[[:space:]]*[0-9]+[[:space:]]*$/ && $2 != "" &&
                $3 ~ /GPU-/ && $4 != "" && $5 != "" {good++}
            END {exit !(good > 0)}
        ' || return 1
        printf 'GPU smoke: %s completed inside a GPU Pod.\n' "$node"
        kip_kubectl -n default delete pod "$pod" --wait=true --timeout=2m \
            >/dev/null || return 1
        KIP_FLEET_GPU_SMOKE_CREATED=$(printf '%s\n' "$KIP_FLEET_GPU_SMOKE_CREATED" \
            | command sed "s/ $pod//")
    done
}

kip_logo
kip_heading "Fleet NVIDIA GPU Operator · pilot"
kip_reset_results 7
if kip_fleet_gpu_nodes_ready; then
    kip_record PASS "Fleet nodes" "Control and $KIP_FLEET_GPU_COUNT GPU worker(s) Ready."
else
    kip_record FAIL "Fleet nodes" "Expected exact Ready control/GPU-worker node set."
    kip_summary "Fleet GPU installation"
    exit 1
fi
state=$(kip_gpu_preflight_state) || {
    kip_record FAIL "GPU ownership" "Existing GPU stack is unmanaged or drifted."
    kip_summary "Fleet GPU installation"
    exit 1
}
if [ "$state" = recoverable ]; then
    kip_record FAIL "GPU ownership" "Partial or drifted GPU release requires diagnosis."
    kip_summary "Fleet GPU installation"
    exit 1
fi
if kip_gpu_no_partitioning_or_sharing; then
    kip_record PASS "GPU ownership" "$state release; whole-GPU allocation only."
else
    kip_record FAIL "GPU ownership" "MIG or sharing state is not allowed."
    kip_summary "Fleet GPU installation"
    exit 1
fi
if kip_gpu_verify_chart; then
    kip_record PASS "Signed GPU chart" "Pinned OCI digest and NVIDIA signature verified."
else
    kip_record FAIL "Signed GPU chart" "Pinned chart, checksum, or signature did not verify."
    kip_summary "Fleet GPU installation"
    exit 1
fi
if kip_fleet_gpu_label_nodes; then
    kip_record PASS "Node roles" "Control operands disabled; GPU workers explicitly labelled."
else
    kip_record FAIL "Node roles" "Could not set reviewed control/worker GPU labels."
    kip_summary "Fleet GPU installation"
    exit 1
fi
dcgm_state=false
if kip_gpu_release_exists; then
    dcgm_state=$(kip_gpu_dcgm_exporter_state) || exit 1
fi
if kip_gpu_reconcile_operator "$dcgm_state"; then
    kip_record PASS "GPU Operator" "Pinned preinstalled-driver release reconciled."
else
    kip_record FAIL "GPU Operator" "Helm install failed; operator Pods remain for diagnosis."
    kip_summary "Fleet GPU installation"
    exit 1
fi
if kip_fleet_gpu_wait; then
    kip_record PASS "Whole GPU checkpoint" \
        "$KIP_FLEET_GPU_COUNT worker(s) each advertise exactly one nvidia.com/gpu."
else
    kip_record FAIL "Whole GPU checkpoint" "Operator, operand, or per-node capacity unhealthy."
    kip_summary "Fleet GPU installation"
    exit 1
fi
if ! kip_fleet_gpu_smoke_each; then
    kip_record FAIL "CUDA smoke Pod" "One or more node-pinned GPU Pods failed."
    kip_summary "Fleet GPU installation"
    exit 1
fi
kip_record PASS "CUDA smoke Pod" "nvidia-smi succeeded on every GPU worker."
kip_summary "Fleet GPU installation"
