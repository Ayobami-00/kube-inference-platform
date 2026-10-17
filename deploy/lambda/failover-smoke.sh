#!/usr/bin/env bash
# Exercise one Qwen3-8B replica loss through the private, authenticated Gateway.
# This is a Pod/Deployment availability checkpoint, not a whole-worker failure.
set -Eeuo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$script_dir/../.." && pwd)
namespace=kip-inference
victim=qwen3-8b-small
survivor=qwen3-8b-large
KUBECONFIG=${KUBECONFIG:-/etc/kubernetes/admin.conf}
export KUBECONFIG

die() { printf 'Lambda replica failover: %s\n' "$*" >&2; exit 1; }
for tool in kubectl jq python3; do
    command -v "$tool" >/dev/null || die "missing $tool"
done
[ -r "$KUBECONFIG" ] || die 'admin kubeconfig is unavailable'
[ "$#" -eq 0 ] || die 'this checkpoint accepts no arguments'

victim_json=$(kubectl -n "$namespace" get deployment "$victim" -o json) \
    || die 'cannot inspect the selected replica Deployment'
survivor_json=$(kubectl -n "$namespace" get deployment "$survivor" -o json) \
    || die 'cannot inspect the surviving replica Deployment'
for object in "$victim_json" "$survivor_json"; do
    jq -e '
        .metadata.labels["app.kubernetes.io/managed-by"] == "kube-inference-platform"
        and .spec.replicas == 1
        and .status.availableReplicas == 1
    ' >/dev/null <<<"$object" || die 'reviewed model replicas are not both owned and Ready'
done
kubectl -n "$namespace" rollout status deployment/qwen3-next-80b --timeout=30s \
    >/dev/null || die 'large model is not Ready before the test'

restore() {
    local original_status=$?
    trap - EXIT INT TERM HUP
    if ! kubectl -n "$namespace" scale "deployment/$victim" --replicas=1 >/dev/null \
        || ! kubectl -n "$namespace" rollout status "deployment/$victim" --timeout=15m >/dev/null; then
        printf 'FAIL: %s could not be restored to one Ready replica.\n' "$victim" >&2
        exit 1
    fi
    printf 'PASS: %s restored to one Ready replica.\n' "$victim"
    exit "$original_status"
}
trap restore EXIT
trap 'exit 1' INT TERM HUP

kubectl -n "$namespace" scale "deployment/$victim" --replicas=0 >/dev/null
for _ in $(seq 1 60); do
    state=$(kubectl -n "$namespace" get deployment "$victim" -o json \
        | jq -r '[(.status.replicas // 0), (.status.availableReplicas // 0)] | join(":")') \
        || die 'cannot inspect scaled-down replica'
    [ "$state" = '0:0' ] && break
    sleep 2
done
[ "$state" = '0:0' ] || die 'selected replica did not leave service within two minutes'
kubectl -n "$namespace" rollout status "deployment/$survivor" --timeout=30s \
    >/dev/null || die 'surviving Qwen3-8B replica is not Ready'

gateway=$(PYTHONPATH="$repo_root" python3 -c \
    'from bench.lambda_gateway_smoke import gateway_address; print(gateway_address())') \
    || die 'private Gateway address could not be validated'
PYTHONPATH="$repo_root" python3 "$script_dir/tenant-smoke.py" "$gateway" \
    || die 'authenticated streaming failed while one small-model replica was absent'
printf 'PASS: private Gateway served both models while %s was unavailable.\n' "$victim"
