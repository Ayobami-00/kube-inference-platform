#!/usr/bin/env bash
# Target-local preparation for each owned GPU worker's persistent model cache.
set -Eeuo pipefail

readonly cache=/var/lib/kube-inference-platform/model-cache/vllm
die() { printf 'pilot cache preparation: %s\n' "$*" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || die 'run as root on a GPU worker'
[[ $(hostname) == kip-gpu-1 || $(hostname) == kip-gpu-2 ]] \
  || die 'cache preparation is restricted to reviewed GPU-worker hostnames'
[[ ! -L /var/lib/kube-inference-platform && ! -L /var/lib/kube-inference-platform/model-cache \
   && ! -L ${cache} ]] || die 'model-cache path must not contain a symlink'
available_kib=$(df -Pk /var/lib | awk 'NR == 2 {print $4}')
[[ ${available_kib} =~ ^[0-9]+$ && ${available_kib} -ge 26214400 ]] \
  || die 'model-cache filesystem needs at least 25 GiB free'
install -d -m 0755 -- "${cache}"
[[ -d ${cache} && ! -L ${cache} ]] || die 'model-cache directory could not be verified'
printf 'PASS: persistent per-worker model cache ready at %s\n' "${cache}"
