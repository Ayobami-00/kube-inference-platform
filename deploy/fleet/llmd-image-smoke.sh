#!/usr/bin/env bash
# Prove the locked llm-d vLLM image can execute CUDA on a schedulable pilot GPU.
set -Eeuo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
lock_file="${script_dir}/versions.json"
pod_name=kip-llmd-image-smoke
created=0
KIP_KUBECONFIG=${KIP_KUBECONFIG:-/etc/kubernetes/admin.conf}

die() { printf 'llm-d image smoke: %s\n' "$*" >&2; exit 1; }
kctl() { command kubectl --kubeconfig "$KIP_KUBECONFIG" "$@"; }
cleanup() {
  local status=$?
  trap - EXIT
  if [[ ${created} == 1 ]]; then
    if ! kctl delete pod "${pod_name}" --ignore-not-found --wait=true --timeout=90s \
      >/dev/null; then
      printf 'Could not delete %s; inspect the owned smoke Pod.\n' "${pod_name}" >&2
      status=1
    fi
  fi
  exit "$status"
}
trap cleanup EXIT

for command_name in kubectl jq; do
  command -v "${command_name}" >/dev/null || die "missing ${command_name}"
done
[[ -f ${lock_file} ]] || die 'pilot compatibility lock is missing'
image=$(jq -er '.inference.vllmLinuxAmd64Image' "${lock_file}") \
  || die 'pinned vLLM image is missing'
[[ ${image} == docker.io/vllm/vllm-openai:v0.30.0@sha256:* ]] \
  || die 'pinned image does not match reviewed vLLM version'
[[ -z $(kctl get pod "${pod_name}" --ignore-not-found -o name) ]] \
  || die 'smoke Pod name already exists; refusing to replace it'

jq -n --arg image "${image}" --arg name "${pod_name}" '{
  apiVersion: "v1",
  kind: "Pod",
  metadata: {
    name: $name,
    labels: {
      "app.kubernetes.io/managed-by": "kube-inference-platform",
      "app.kubernetes.io/component": "fleet-image-smoke"
    }
  },
  spec: {
    restartPolicy: "Never",
    nodeSelector: {"kubernetes.io/hostname": "kip-gpu-1"},
    containers: [{
      name: "cuda",
      image: $image,
      imagePullPolicy: "IfNotPresent",
      command: ["python3", "-c", "import torch; assert torch.cuda.is_available(); x=torch.arange(1024,device=\"cuda\"); y=(x*x).sum(); torch.cuda.synchronize(); assert int(y)>0; print(torch.cuda.get_device_name(0),torch.__version__,torch.version.cuda)"] ,
      resources: {requests: {"nvidia.com/gpu": 1}, limits: {"nvidia.com/gpu": 1}}
    }]
  }
}' | kctl create -f - >/dev/null || die 'could not create pinned-image smoke Pod'
created=1

for ((attempt=0; attempt<120; attempt++)); do
  phase=$(kctl get pod "${pod_name}" -o jsonpath='{.status.phase}') \
    || die 'smoke Pod disappeared before completion'
  case ${phase} in
    Succeeded)
      kctl logs "${pod_name}" -c cuda
      printf 'PASS: pinned llm-d vLLM image executed a CUDA tensor operation.\n'
      exit 0
      ;;
    Failed)
      kctl logs "${pod_name}" -c cuda --tail=80 || true
      kctl describe pod "${pod_name}" | tail -50 >&2 || true
      die 'pinned llm-d vLLM image failed its CUDA operation'
      ;;
  esac
  sleep 5
done
kctl describe pod "${pod_name}" | tail -50 >&2 || true
die 'pinned llm-d vLLM image did not finish within 10 minutes'
