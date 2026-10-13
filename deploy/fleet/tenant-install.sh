#!/usr/bin/env bash
# Install a small, fail-closed tenant identity boundary for the private Gateway.
set -Eeuo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly script_dir
readonly kubeconfig=${KIP_KUBECONFIG:-/etc/kubernetes/admin.conf}
readonly namespace=kip-inference
readonly keys_path=/root/kube-inference-platform/.secrets/pilot-tenant-keys.json
work_dir=''

die() { printf 'tenant installation: %s\n' "$*" >&2; exit 1; }
kctl() { command kubectl --kubeconfig "$kubeconfig" "$@"; }
cleanup() { [ -z "$work_dir" ] || command rm -rf -- "$work_dir"; }
trap cleanup EXIT

[ "$#" -eq 0 ] || { printf '%s\n' 'tenant-install accepts no arguments' >&2; exit 2; }
[ "$(hostname)" = kip-control ] || die 'run on the owned kip-control VM'
[ "$(id -u)" -eq 0 ] || die 'run as root'
[ -r "$kubeconfig" ] || die 'admin kubeconfig is unavailable'
for tool in base64 curl jq kubectl python3 sed sha256sum; do
    command -v "$tool" >/dev/null || die "missing $tool"
done
for asset in tenant-auth.py tenant-keys.py tenant-auth.yaml tenant-security-policy.yaml \
    inference-objectives.yaml flow-control-lock.json llmd-router-flow-values.yaml \
    llmd-install.sh epp-metrics-rbac.yaml; do
    [ -f "$script_dir/$asset" ] || die "reviewed asset $asset is missing"
done

model_pods=$(kctl -n "$namespace" get pods -l app.kubernetes.io/instance=kip-qwen25-pilot \
    -o json) || die 'cannot inspect model Pods'
jq -e '(.items | length) == 2 and all(.items[];
    any(.status.conditions[]?; .type == "Ready" and .status == "True"))' \
    >/dev/null <<<"$model_pods" || die 'both pinned model replicas must be Ready'
gateway=$(kctl -n "$namespace" get gateway kip-inference-gateway -o json) \
    || die 'private Gateway is missing'
jq -e 'any(.status.conditions[]?; .type == "Programmed" and .status == "True")' \
    >/dev/null <<<"$gateway" || die 'private Gateway is not Programmed'
route=$(kctl -n "$namespace" get httproute kip-qwen25-pilot -o json) \
    || die 'llm-d HTTPRoute is missing'
jq -e 'any(.status.parents[]?.conditions[]?; .type == "Accepted" and .status == "True")' \
    >/dev/null <<<"$route" || die 'llm-d HTTPRoute is not Accepted'
namespace_json=$(kctl get namespace "$namespace" -o json) \
    || die 'runtime namespace is unavailable'
jq -e '.metadata.labels["app.kubernetes.io/managed-by"] == "kube-inference-platform"' \
    >/dev/null <<<"$namespace_json" || die 'runtime namespace is not KIP-owned'

script_hash=$(sha256sum "$script_dir/tenant-auth.py" | awk '{print $1}')
readonly script_hash
readonly image='docker.io/library/python:3.12.15-slim-bookworm@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d'
owned_json() {
    local kind=$1 name=$2
    kctl -n "$namespace" get "$kind" "$name" --ignore-not-found -o json \
        || die "cannot inspect $kind/$name"
}
require_owned() {
    local kind=$1 name=$2 existing
    existing=$(owned_json "$kind" "$name")
    if [ -n "$existing" ]; then
        jq -e '.metadata.labels["app.kubernetes.io/managed-by"] == "kube-inference-platform"' \
            >/dev/null <<<"$existing" || die "$kind/$name is unmanaged"
    fi
    printf '%s' "$existing"
}

secret=$(require_owned secret kip-tenant-credentials)
configmap=$(require_owned configmap kip-tenant-auth-code)
deployment=$(require_owned deployment kip-tenant-auth)
service=$(require_owned service kip-tenant-auth)
policy=$(require_owned securitypolicy kip-tenant-auth)
objective_crd=$(kctl get crd inferenceobjectives.llm-d.ai --ignore-not-found -o json) \
    || die 'cannot inspect the InferenceObjective CRD'
if [ -n "$objective_crd" ]; then
    jq -e '.metadata.labels["app.kubernetes.io/managed-by"] ==
        "kube-inference-platform"' >/dev/null <<<"$objective_crd" \
        || die 'InferenceObjective CRD is unmanaged'
    for objective in kip-voice kip-interactive kip-batch; do
        require_owned inferenceobjective "$objective" >/dev/null
    done
fi
if [ -n "$configmap" ]; then
    existing_hash=$(jq -j '.data["auth.py"] // ""' <<<"$configmap" | sha256sum | awk '{print $1}')
    [ "$existing_hash" = "$script_hash" ] || die 'authorization source has drifted'
fi
if [ -n "$deployment" ]; then
    jq -e --arg image "$image" --arg hash "$script_hash" '
        .spec.replicas == 1 and .spec.template.spec.containers[0].image == $image and
        .spec.template.metadata.annotations["kube-inference-platform.io/auth-script-sha256"] == $hash and
        .spec.template.spec.nodeSelector["kube-inference-platform.io/fleet-role"] == "control-plane"
    ' >/dev/null <<<"$deployment" || die 'authorization Deployment has immutable-profile drift'
fi
if [ -n "$service" ]; then
    jq -e '.spec.type == "ClusterIP" and
        .spec.selector["app.kubernetes.io/name"] == "kip-tenant-auth"' \
        >/dev/null <<<"$service" || die 'authorization Service is not private or has drifted'
fi
if [ -n "$policy" ]; then
    jq -e '.spec.extAuth.failOpen == false and .spec.extAuth.http.backendRefs[0].name ==
        "kip-tenant-auth" and .spec.targetRefs[0].name == "kip-qwen25-pilot"' \
        >/dev/null <<<"$policy" || die 'tenant SecurityPolicy has drifted'
fi

python3 "$script_dir/tenant-keys.py" "$keys_path" \
    || die 'could not create or validate private tenant credentials'
python3 - "$script_dir/tenant-auth.py" "$keys_path" <<'PY' \
    || die 'private tenant credential schema is invalid'
import importlib.util
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("kip_auth", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.load_credentials(Path(sys.argv[2]))
PY
if [ -n "$secret" ]; then
    remote_hash=$(jq -er '.data["tokens.json"]' <<<"$secret" \
        | base64 -d | sha256sum | awk '{print $1}') \
        || die 'existing credential Secret is malformed'
    local_hash=$(sha256sum "$keys_path" | awk '{print $1}')
    [ "$remote_hash" = "$local_hash" ] || die 'local tenant keys and owned Secret differ'
else
    python3 - "$keys_path" <<'PY' | kctl create -f - >/dev/null \
        || die 'could not create the owned credential Secret'
import json
import sys
from pathlib import Path
print(json.dumps({
    "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
    "metadata": {"name": "kip-tenant-credentials", "namespace": "kip-inference",
                 "labels": {"app.kubernetes.io/managed-by": "kube-inference-platform",
                            "app.kubernetes.io/component": "tenant-auth"}},
    "stringData": {"tokens.json": Path(sys.argv[1]).read_text(encoding="utf-8")},
}))
PY
fi

if [ -z "$configmap" ]; then
    kctl -n "$namespace" create configmap kip-tenant-auth-code \
        --from-file=auth.py="$script_dir/tenant-auth.py" --dry-run=client -o json \
        | jq '.metadata.labels={"app.kubernetes.io/managed-by":"kube-inference-platform",
                "app.kubernetes.io/component":"tenant-auth"}' \
        | kctl create -f - >/dev/null || die 'could not create owned authorization ConfigMap'
fi
work_dir=$(mktemp -d /tmp/kip-tenant.XXXXXX)
sed "s/__AUTH_SCRIPT_SHA256__/$script_hash/g" "$script_dir/tenant-auth.yaml" \
    >"$work_dir/tenant-auth.yaml"
kctl apply -f "$work_dir/tenant-auth.yaml" >/dev/null \
    || die 'authorization Deployment/Service apply failed'
kctl -n "$namespace" rollout status deployment/kip-tenant-auth --timeout=5m \
    || die 'authorization Pod did not become Ready'
auth_pods=$(kctl -n "$namespace" get pods -l app.kubernetes.io/name=kip-tenant-auth -o json) \
    || die 'cannot inspect authorization Pod'
jq -e '(.items | length) == 1 and .items[0].spec.nodeName == "kip-control" and
    any(.items[0].status.conditions[]?; .type == "Ready" and .status == "True")' \
    >/dev/null <<<"$auth_pods" || die 'authorization Pod is not Ready on the control plane'
crd_url=$(jq -er '.inferenceObjectiveManifestUrl' "$script_dir/flow-control-lock.json")
crd_hash=$(jq -er '.inferenceObjectiveManifestSha256' "$script_dir/flow-control-lock.json")
curl -fsSL --retry 3 --max-time 120 "$crd_url" -o "$work_dir/llmd-crds.yaml" \
    || die 'official pinned llm-d CRD manifest is unavailable'
printf '%s  %s\n' "$crd_hash" "$work_dir/llmd-crds.yaml" | sha256sum --check --status \
    || die 'official llm-d CRD manifest digest mismatch'
kctl create --dry-run=client -f "$work_dir/llmd-crds.yaml" -o json \
    | jq -s 'map(select(.metadata.name == "inferenceobjectives.llm-d.ai")) | .[0] |
        .metadata.labels["app.kubernetes.io/managed-by"] = "kube-inference-platform"' \
    >"$work_dir/inferenceobjective-crd.json" \
    || die 'could not extract the pinned InferenceObjective CRD'
jq -e '.kind == "CustomResourceDefinition" and
    .metadata.name == "inferenceobjectives.llm-d.ai"' \
    >/dev/null "$work_dir/inferenceobjective-crd.json" \
    || die 'pinned InferenceObjective CRD was not found'
kctl apply --server-side --field-manager=kip-pilot \
    -f "$work_dir/inferenceobjective-crd.json" >/dev/null \
    || die 'could not apply the owned InferenceObjective CRD'
kctl wait --for=condition=Established crd/inferenceobjectives.llm-d.ai --timeout=2m \
    >/dev/null || die 'InferenceObjective CRD is not Established'
kctl apply -f "$script_dir/inference-objectives.yaml" >/dev/null \
    || die 'pilot InferenceObjectives apply failed'
kctl apply -f "$script_dir/tenant-security-policy.yaml" >/dev/null \
    || die 'fail-closed Gateway SecurityPolicy apply failed'
accepted=0
for _ in $(seq 1 30); do
    policy=$(kctl -n "$namespace" get securitypolicy kip-tenant-auth -o json) \
        || die 'cannot inspect tenant SecurityPolicy status'
    if jq -e 'any(.status.ancestors[]?.conditions[]?;
        .type == "Accepted" and .status == "True")' >/dev/null <<<"$policy"; then
        accepted=1
        break
    fi
    sleep 2
done
[ "$accepted" -eq 1 ] || die 'tenant SecurityPolicy was not Accepted'
gateway_ip=$(jq -r '.status.addresses[0].value // empty' <<<"$gateway")
[ -n "$gateway_ip" ] || die 'private Gateway has no address'
status=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
    "http://$gateway_ip/v1/models") || die 'private Gateway is unreachable'
[ "$status" = 403 ] || die "unauthenticated Gateway request was not denied (HTTP $status)"
bash "$script_dir/llmd-install.sh" || die 'tenant-aware llm-d flow-control upgrade failed'
require_owned serviceaccount kip-metrics-reader >/dev/null
for resource in clusterrole/kip-epp-metrics-reader \
    clusterrolebinding/kip-epp-metrics-reader clusterrolebinding/kip-epp-auth-delegator; do
    existing=$(kctl get "$resource" --ignore-not-found -o json) \
        || die "cannot inspect $resource"
    if [ -n "$existing" ]; then
        jq -e '.metadata.labels["app.kubernetes.io/managed-by"] ==
            "kube-inference-platform"' >/dev/null <<<"$existing" \
            || die "$resource is unmanaged"
    fi
done
kctl apply -f "$script_dir/epp-metrics-rbac.yaml" >/dev/null \
    || die 'EPP metrics authentication RBAC apply failed'
epp_logs=$(kctl -n "$namespace" logs deployment/kip-qwen25-pilot-epp -c epp \
    --since=5m) || die 'cannot inspect endpoint-picker flow-control activation'
grep -Fq 'Initializing Flow Control layer' <<<"$epp_logs" \
    || die 'endpoint picker did not activate Flow Control'
printf '%s\n' 'PASS: private Gateway auth and three tenant-aware flow-control bands are active.'
