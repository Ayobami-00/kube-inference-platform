#!/usr/bin/env bash
# KIP-owned two-replica Qwen deployment on exactly two certified GPU workers.
set -Eeuo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
KIP_KUBECONFIG=${KIP_KUBECONFIG:-/etc/kubernetes/admin.conf}
readonly namespace=kip-inference
readonly name=kip-qwen25-pilot
readonly revision=a09a35458c702b33eeacc393d103063234e8bc28

die() { printf 'pilot model installation: %s\n' "$*" >&2; exit 1; }
kctl() { command kubectl --kubeconfig "${KIP_KUBECONFIG}" "$@"; }
for tool in kubectl jq; do command -v "${tool}" >/dev/null || die "missing ${tool}"; done
[[ -r ${KIP_KUBECONFIG} ]] || die 'control-plane admin kubeconfig is unavailable'
[[ -f ${script_dir}/model.yaml ]] || die 'reviewed model manifest is missing'
image=$(jq -er '.inference.vllmLinuxAmd64Image' "${script_dir}/versions.json") \
  || die 'pinned vLLM image is missing'
readonly image

nodes=$(kctl get nodes -o json) || die 'Kubernetes API is unavailable'
jq -e '
  (.items | length) == 3 and
  ([.items[].metadata.name] | sort) == ["kip-control","kip-gpu-1","kip-gpu-2"] and
  all(.items[]; any(.status.conditions[]?; .type == "Ready" and .status == "True")) and
  ([.items[] | select(.metadata.name == "kip-control") |
    (.status.allocatable["nvidia.com/gpu"] // "0" | tonumber)] == [0]) and
  ([.items[] | select(.metadata.name == "kip-gpu-1" or .metadata.name == "kip-gpu-2") |
    (.status.allocatable["nvidia.com/gpu"] // "0" | tonumber)] | sort) == [1,1]
' >/dev/null <<<"${nodes}" || die 'exact three-node Ready/GPU inventory gate failed'

for worker in kip-gpu-1 kip-gpu-2; do
  current=$(jq -r --arg name "${worker}" '.items[] | select(.metadata.name == $name) |
    .metadata.labels["kube-inference-platform.io/fleet-role"] // ""' <<<"${nodes}")
  [[ -z ${current} || ${current} == gpu-worker ]] \
    || die "${worker} already has an unrelated fleet role"
done
control_role=$(jq -r '.items[] | select(.metadata.name == "kip-control") |
  .metadata.labels["kube-inference-platform.io/fleet-role"] // ""' <<<"${nodes}")
[[ -z ${control_role} || ${control_role} == control-plane ]] \
  || die 'control plane already has an unrelated fleet role'

existing_ns=$(kctl get namespace "${namespace}" --ignore-not-found -o json) \
  || die 'cannot inspect the runtime namespace'
if [[ -n ${existing_ns} ]]; then
  jq -e '.metadata.labels["app.kubernetes.io/managed-by"] == "kube-inference-platform"' \
    >/dev/null <<<"${existing_ns}" || die 'runtime namespace is not KIP-owned'
fi
existing_deploy=$(kctl -n "${namespace}" get deployment "${name}" --ignore-not-found -o json) \
  || die 'cannot inspect the model Deployment'
if [[ -n ${existing_deploy} ]]; then
  jq -e --arg image "${image}" --arg revision "${revision}" '
    .metadata.labels["app.kubernetes.io/managed-by"] == "kube-inference-platform" and
    .metadata.annotations["kube-inference-platform.io/model-revision"] == $revision and
    .spec.replicas == 2 and
    .spec.template.spec.containers[0].image == $image and
    .spec.template.spec.containers[0].resources.limits["nvidia.com/gpu"] == "1" and
    ([.spec.template.spec.containers[0].env[]? | select(.name == "VLLM_HTTP_TIMEOUT_KEEP_ALIVE") | .value] | . == [] or . == ["60"]) and
    .spec.template.spec.nodeSelector["kube-inference-platform.io/fleet-role"] == "gpu-worker"
  ' >/dev/null <<<"${existing_deploy}" || die 'existing model Deployment has ownership or immutable-profile drift'
fi
existing_service=$(kctl -n "${namespace}" get service "${name}" --ignore-not-found -o json) \
  || die 'cannot inspect the model Service'
if [[ -n ${existing_service} ]]; then
  jq -e '.metadata.labels["app.kubernetes.io/managed-by"] == "kube-inference-platform" and
    .spec.type == "ClusterIP" and
    .spec.selector["app.kubernetes.io/instance"] == "kip-qwen25-pilot"' \
    >/dev/null <<<"${existing_service}" || die 'existing model Service is unmanaged or drifted'
fi

for worker in kip-gpu-1 kip-gpu-2; do
  kctl label node "${worker}" kube-inference-platform.io/fleet-role=gpu-worker --overwrite \
    >/dev/null || die "could not label ${worker}"
done
kctl label node kip-control kube-inference-platform.io/fleet-role=control-plane --overwrite \
  >/dev/null || die 'could not label control plane'

kctl apply -f "${script_dir}/model.yaml" >/dev/null || die 'model manifest apply failed'
kctl -n "${namespace}" rollout status deployment/"${name}" --timeout=45m \
  || {
    kctl -n "${namespace}" get pods -o wide >&2 || true
    kctl -n "${namespace}" describe deployment "${name}" >&2 || true
    die 'model rollout did not become healthy'
  }
pods=$(kctl -n "${namespace}" get pods -l app.kubernetes.io/instance="${name}" -o json) \
  || die 'cannot inspect model Pods'
jq -e --arg image "${image}" '
  (.items | length) == 2 and
  ([.items[].spec.nodeName] | sort) == ["kip-gpu-1","kip-gpu-2"] and
  all(.items[];
    .status.phase == "Running" and
    .spec.containers[0].image == $image and
    any(.status.conditions[]?; .type == "Ready" and .status == "True"))
' >/dev/null <<<"${pods}" || die 'two identical model Pods are not Ready on distinct GPU workers'
printf 'PASS: two KIP-owned Qwen replicas Ready on distinct GPU workers.\n'
