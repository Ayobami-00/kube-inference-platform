#!/usr/bin/env bash
# Install the pinned, private Envoy AI Gateway and llm-d endpoint picker.
set -Eeuo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly script_dir
readonly kubeconfig=${KIP_KUBECONFIG:-/etc/kubernetes/admin.conf}
readonly lock="$script_dir/versions.json"
work_dir=""

die() { printf 'fleet llm-d installation: %s\n' "$*" >&2; exit 1; }
kctl() { command kubectl --kubeconfig "$kubeconfig" "$@"; }
cleanup() { [ -z "$work_dir" ] || command rm -rf -- "$work_dir"; }
trap cleanup EXIT

[ "$#" -eq 0 ] || { printf '%s\n' 'llmd-install accepts no arguments.' >&2; exit 2; }
[ "$(command hostname)" = kip-control ] || die 'run on the owned kip-control VM'
[ "$(id -u)" -eq 0 ] || die 'run as root'
[ -r "$kubeconfig" ] || die 'admin kubeconfig is unavailable'
for tool in curl helm jq kubectl sha256sum mktemp; do
    command -v "$tool" >/dev/null || die "missing $tool"
done
for asset in versions.json envoy-gateway-values.yaml envoy-ai-gateway-values.yaml \
    llmd-router-values.yaml llmd-router-flow-values.yaml flow-control-lock.json \
    gateway.yaml gateway-rbac.yaml; do
    [ -f "$script_dir/$asset" ] || die "reviewed asset $asset is missing"
done
work_dir=$(command mktemp -d /tmp/kip-llmd.XXXXXX)

version() { jq -er "$1" "$lock"; }
hash_file() { sha256sum "$1" | awk '{print $1}'; }
verify_chart() {
    local ref=$1 tag=$2 expected=$3 output
    output=$(helm pull "$ref" --version "$tag" --destination "$work_dir" 2>&1) \
        || die "could not pull official pinned chart $ref"
    printf '%s\n' "$output" | grep -Fqx "Digest: $expected" \
        || die "official chart digest mismatch for $ref"
}
verify_manifest() {
    local url=$1 expected=$2 target=$3
    curl -fsSL --retry 3 --max-time 120 "$url" -o "$target" \
        || die "official pinned CRD manifest is unavailable"
    printf '%s  %s\n' "$expected" "$target" | sha256sum --check --status \
        || die "official CRD manifest digest mismatch"
}
require_owned_namespace() {
    local name=$1 existing
    existing=$(kctl get namespace "$name" --ignore-not-found -o json) \
        || die "cannot inspect namespace/$name"
    if [ -n "$existing" ]; then
        jq -e '.metadata.labels["app.kubernetes.io/managed-by"] == "kube-inference-platform"' \
            >/dev/null <<<"$existing" || die "namespace/$name is unmanaged"
    else
        kctl apply -f - >/dev/null <<YAML || die "cannot create owned namespace/$name"
apiVersion: v1
kind: Namespace
metadata:
  name: $name
  labels:
    app.kubernetes.io/managed-by: kube-inference-platform
    app.kubernetes.io/part-of: kube-inference-platform
YAML
    fi
}
require_owned_resource() {
    local kind=$1 name=$2 namespace=${3:-} existing
    if [ -n "$namespace" ]; then
        existing=$(kctl -n "$namespace" get "$kind" "$name" --ignore-not-found -o json) \
            || die "cannot inspect $kind/$name"
    else
        existing=$(kctl get "$kind" "$name" --ignore-not-found -o json) \
            || die "cannot inspect $kind/$name"
    fi
    if [ -n "$existing" ]; then
        jq -e '.metadata.labels["app.kubernetes.io/managed-by"] == "kube-inference-platform"' \
            >/dev/null <<<"$existing" || die "$kind/$name is unmanaged"
    fi
}
require_owned_crd_group() {
    local group=$1
    kctl get crd -o json | jq -e --arg group "$group" '
        all(.items[] | select(.spec.group == $group);
            .metadata.labels["app.kubernetes.io/managed-by"] == "kube-inference-platform")
    ' >/dev/null || die "existing $group CRDs are unmanaged"
}
apply_owned_crds() {
    local manifest=$1 resource
    while IFS= read -r resource; do
        [ -n "$resource" ] || continue
        require_owned_resource "${resource%%/*}" "${resource#*/}"
    done < <(kctl create --dry-run=client -f "$manifest" -o name \
        | grep '^customresourcedefinition.apiextensions.k8s.io/')
    kctl apply --server-side --field-manager=kip-pilot -f "$manifest" >/dev/null \
        || die "could not apply pinned CRDs from $manifest"
    while IFS= read -r resource; do
        [ -n "$resource" ] || continue
        kctl label "$resource" app.kubernetes.io/managed-by=kube-inference-platform \
            --overwrite >/dev/null || die "could not mark owned $resource"
    done < <(kctl create --dry-run=client -f "$manifest" -o name \
        | grep '^customresourcedefinition.apiextensions.k8s.io/')
}
require_chart_release() {
    local name=$1 namespace=$2 chart_prefix=$3 digest=$4 values_hash=$5 match values
    local failed_hash='' pre_tenant_hash='' pre_weighted_hash='' pre_placement_hash=''
    local pre_tenant_placement_hash='' failed_untolerated_hash=''
    match=$(helm list --all-namespaces --all -o json \
        | jq -c --arg name "$name" '.[] | select(.name == $name)') \
        || die 'cannot inspect Helm releases'
    [ -z "$match" ] || jq -e --arg namespace "$namespace" --arg chart "$chart_prefix" '
        .namespace == $namespace and (.chart | startswith($chart)) and
        (.status == "deployed" or .status == "failed" or .status == "pending-upgrade")
    ' >/dev/null <<<"$match" || die "Helm release $name has unrelated ownership or version"
    if [ -n "$match" ]; then
        values=$(helm get values "$name" --namespace "$namespace" -o json) \
            || die "cannot inspect Helm release $name values"
        # The first pilot router release failed only because GAIE rejected the
        # obsolete FailClosed enum. Permit that exact failed revision to be
        # repaired once; any deployed release or other hash must still match.
        if [ "$name" = kip-qwen25-pilot ]; then
            failed_hash=7850c66fa262fac20e06991a0a44ed67a498e8d4c2c52bb4efefd6c2e107ed41
            if [ "$tenant_enabled" -eq 1 ]; then
                pre_tenant_hash=$(jq -er '.preTenantRouterValuesSha256' \
                    "$script_dir/flow-control-lock.json")
                pre_weighted_hash=$(jq -er '.preWeightedPickerValuesSha256' \
                    "$script_dir/flow-control-lock.json")
                pre_placement_hash=$(jq -er '.preControlPlanePlacementValuesSha256' \
                    "$script_dir/flow-control-lock.json")
                pre_tenant_placement_hash=$(jq -er \
                    '.preTenantControlPlanePlacementValuesSha256' \
                    "$script_dir/flow-control-lock.json")
                failed_untolerated_hash=$(jq -er \
                    '.failedUntoleratedPlacementValuesSha256' \
                    "$script_dir/flow-control-lock.json")
            fi
        fi
        jq -e --arg digest "$digest" --arg hash "$values_hash" \
            --arg failed_hash "$failed_hash" --arg pre_tenant_hash "$pre_tenant_hash" \
            --arg pre_weighted_hash "$pre_weighted_hash" \
            --arg pre_placement_hash "$pre_placement_hash" \
            --arg pre_tenant_placement_hash "$pre_tenant_placement_hash" \
            --arg failed_untolerated_hash "$failed_untolerated_hash" \
            --arg status "$(jq -r '.status' <<<"$match")" '
            .kip.owner == "kube-inference-platform" and
            .kip.chartDigest == $digest and
            (.kip.valuesSha256 == $hash or
                ($status == "failed" and $failed_hash != "" and
                 .kip.valuesSha256 == $failed_hash and
                 .router.inferencePool.failureMode == "FailClosed") or
                ($status == "failed" and $failed_untolerated_hash != "" and
                 .kip.valuesSha256 == $failed_untolerated_hash and
                 .router.epp.pluginsConfigFile == "kip-pilot-flow-plugins.yaml") or
                ($status == "deployed" and $pre_tenant_hash != "" and
                 .kip.valuesSha256 == $pre_tenant_hash and
                 .router.epp.pluginsConfigFile == "kip-pilot-plugins.yaml") or
                ($status == "deployed" and $pre_tenant_placement_hash != "" and
                 .kip.valuesSha256 == $pre_tenant_placement_hash and
                 .router.epp.pluginsConfigFile == "kip-pilot-plugins.yaml") or
                ($status == "deployed" and $pre_weighted_hash != "" and
                 .kip.valuesSha256 == $pre_weighted_hash and
                 .router.epp.pluginsConfigFile == "kip-pilot-flow-plugins.yaml") or
                ($status == "deployed" and $pre_placement_hash != "" and
                 .kip.valuesSha256 == $pre_placement_hash and
                 .router.epp.pluginsConfigFile == "kip-pilot-flow-plugins.yaml"))
        ' >/dev/null <<<"$values" || die "Helm release $name values or digest have drifted"
    fi
}
require_model_ready() {
    local pods
    pods=$(kctl -n kip-inference get pods -l app.kubernetes.io/instance=kip-qwen25-pilot \
        -o json) || die 'cannot inspect KIP model Pods'
    jq -e '
        (.items | length) == 2 and
        ([.items[].spec.nodeName] | sort) == ["kip-gpu-1", "kip-gpu-2"] and
        all(.items[]; .metadata.labels["app.kubernetes.io/managed-by"] ==
            "kube-inference-platform" and
            any(.status.conditions[]?; .type == "Ready" and .status == "True"))
    ' >/dev/null <<<"$pods" || die 'two KIP-owned model replicas are not Ready'
}

require_model_ready
require_owned_namespace kip-inference
require_owned_crd_group gateway.networking.k8s.io
require_owned_crd_group inference.networking.k8s.io
require_owned_crd_group gateway.envoyproxy.io
require_owned_crd_group aigateway.envoyproxy.io
if kctl get crd gatewayclasses.gateway.networking.k8s.io -o name >/dev/null 2>&1; then
    require_owned_resource gatewayclass kip-private-inference
    require_owned_resource gateway kip-inference-gateway kip-inference
fi
if kctl get crd envoyproxies.gateway.envoyproxy.io -o name >/dev/null 2>&1; then
    require_owned_resource envoyproxy kip-private-proxy envoy-gateway-system
    existing_proxy=$(kctl -n envoy-gateway-system get envoyproxy kip-private-proxy \
        --ignore-not-found -o json) || die 'cannot inspect the private EnvoyProxy'
    if [ -n "$existing_proxy" ]; then
        jq -e '
            .spec.provider.kubernetes.envoyService.type == "ClusterIP" and
            ((.spec.provider.kubernetes.envoyDeployment.pod.nodeSelector // {}) as $selector |
                ($selector == {} or $selector == {"kubernetes.io/hostname":"kip-control"})) and
            .spec.telemetry.accessLog.settings[0].format.json.x_request_id ==
                "%REQ(X-REQUEST-ID)%"
        ' >/dev/null <<<"$existing_proxy" || die 'private EnvoyProxy placement or access log drifted'
    fi
fi
require_owned_resource clusterrole kip-envoy-inference-access
require_owned_resource clusterrolebinding kip-envoy-inference-access
tenant_enabled=0
router_values="$script_dir/llmd-router-values.yaml"
tenant_policy=''
if kctl get crd securitypolicies.gateway.envoyproxy.io -o name >/dev/null 2>&1; then
    tenant_policy=$(kctl -n kip-inference get securitypolicy kip-tenant-auth \
        --ignore-not-found -o json) || die 'cannot inspect tenant SecurityPolicy'
fi
if [ -n "$tenant_policy" ]; then
    jq -e '.metadata.labels["app.kubernetes.io/managed-by"] ==
        "kube-inference-platform" and
        any(.status.ancestors[]?.conditions[]?; .type == "Accepted" and .status == "True")' \
        >/dev/null <<<"$tenant_policy" || die 'tenant policy is unmanaged or not Accepted'
    kctl get crd inferenceobjectives.llm-d.ai -o json | jq -e '
        .metadata.labels["app.kubernetes.io/managed-by"] == "kube-inference-platform"
    ' >/dev/null || die 'owned InferenceObjective CRD is missing'
    for objective in kip-voice kip-interactive kip-batch; do
        objective_json=$(kctl -n kip-inference get inferenceobjective "$objective" -o json) \
            || die "InferenceObjective/$objective is missing"
        jq -e '.metadata.labels["app.kubernetes.io/managed-by"] ==
            "kube-inference-platform" and .spec.poolRef.name == "kip-qwen25-pilot"' \
            >/dev/null <<<"$objective_json" || die "InferenceObjective/$objective drifted"
    done
    tenant_enabled=1
    router_values="$script_dir/llmd-router-flow-values.yaml"
fi
eg_values_hash=$(hash_file "$script_dir/envoy-gateway-values.yaml")
ai_values_hash=$(hash_file "$script_dir/envoy-ai-gateway-values.yaml")
router_values_hash=$(hash_file "$router_values")
lock_hash=$(hash_file "$lock")
require_chart_release eg envoy-gateway-system gateway-helm-v1.8.1 \
    "$(version '.gateway.envoyGatewayChartDigest')" "$eg_values_hash"
require_chart_release envoy-ai-gateway envoy-ai-gateway-system ai-gateway-helm-v0.7.0 \
    "$(version '.gateway.envoyAiGatewayChartDigest')" "$ai_values_hash"
require_chart_release envoy-ai-gateway-crd envoy-ai-gateway-system ai-gateway-crds-helm-v0.7.0 \
    "$(version '.gateway.envoyAiGatewayCrdsChartDigest')" "$lock_hash"
require_chart_release kip-qwen25-pilot kip-inference llm-d-router-gateway-v0.10.0 \
    "$(version '.inference.routerGatewayChartDigest')" "$router_values_hash"
kctl get node kip-control -o json | jq -e '
    .spec.unschedulable != true and
    any(.status.conditions[]?; .type == "Ready" and .status == "True") and
    all(.spec.taints[]?; .key == "node-role.kubernetes.io/control-plane" and
        .effect == "NoSchedule")
' >/dev/null || die 'request-path control node is not schedulable and Ready'

gateway_version=$(version '.gateway.gatewayApiVersion')
inference_version=$(version '.gateway.inferenceExtensionVersion')
verify_manifest "https://github.com/kubernetes-sigs/gateway-api/releases/download/$gateway_version/standard-install.yaml" \
    "$(version '.gateway.gatewayApiStandardManifestSha256')" "$work_dir/gateway-api.yaml"
verify_manifest "https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/$inference_version/v1-manifests.yaml" \
    "$(version '.gateway.inferenceExtensionManifestSha256')" "$work_dir/inference-extension.yaml"

verify_chart oci://docker.io/envoyproxy/gateway-crds-helm \
    "$(version '.gateway.envoyGatewayVersion')" "$(version '.gateway.envoyGatewayCrdsChartDigest')"
verify_chart oci://docker.io/envoyproxy/gateway-helm \
    "$(version '.gateway.envoyGatewayVersion')" "$(version '.gateway.envoyGatewayChartDigest')"
verify_chart oci://docker.io/envoyproxy/ai-gateway-crds-helm \
    "$(version '.gateway.envoyAiGatewayVersion')" "$(version '.gateway.envoyAiGatewayCrdsChartDigest')"
verify_chart oci://docker.io/envoyproxy/ai-gateway-helm \
    "$(version '.gateway.envoyAiGatewayVersion')" "$(version '.gateway.envoyAiGatewayChartDigest')"
verify_chart "$(version '.inference.routerGatewayChart')" \
    "$(version '.inference.routerGatewayChartVersion')" \
    "$(version '.inference.routerGatewayChartDigest')"
printf '%s\n' 'PASS: all official chart and CRD digests match the pilot lock.'

apply_owned_crds "$work_dir/gateway-api.yaml"
apply_owned_crds "$work_dir/inference-extension.yaml"
helm template eg-crds \
    "oci://docker.io/envoyproxy/gateway-crds-helm@$(version '.gateway.envoyGatewayCrdsChartDigest')" \
    --set crds.gatewayAPI.enabled=false --set crds.envoyGateway.enabled=true \
    >"$work_dir/envoy-gateway-crds.yaml" \
    || die 'could not render Envoy Gateway CRDs'
apply_owned_crds "$work_dir/envoy-gateway-crds.yaml"
require_owned_namespace envoy-gateway-system
require_owned_namespace envoy-ai-gateway-system
kctl apply -f "$script_dir/gateway-rbac.yaml" >/dev/null \
    || die 'Envoy InferencePool RBAC apply failed'
helm upgrade --install eg \
    "oci://docker.io/envoyproxy/gateway-helm@$(version '.gateway.envoyGatewayChartDigest')" \
    --namespace envoy-gateway-system --skip-crds \
    --values "$script_dir/envoy-gateway-values.yaml" --wait --timeout 10m \
    --set-string kip.owner=kube-inference-platform \
    --set-string "kip.chartDigest=$(version '.gateway.envoyGatewayChartDigest')" \
    --set-string "kip.valuesSha256=$eg_values_hash" \
    || die 'Envoy Gateway reconciliation failed'
helm upgrade --install envoy-ai-gateway-crd \
    "oci://docker.io/envoyproxy/ai-gateway-crds-helm@$(version '.gateway.envoyAiGatewayCrdsChartDigest')" \
    --namespace envoy-ai-gateway-system --wait --timeout 5m \
    --set-string kip.owner=kube-inference-platform \
    --set-string "kip.chartDigest=$(version '.gateway.envoyAiGatewayCrdsChartDigest')" \
    --set-string "kip.valuesSha256=$lock_hash" \
    || die 'Envoy AI Gateway CRD reconciliation failed'
while IFS= read -r crd; do
    kctl label crd "$crd" app.kubernetes.io/managed-by=kube-inference-platform \
        --overwrite >/dev/null || die "could not mark AI Gateway CRD/$crd as owned"
done < <(kctl get crd -o json | jq -r '.items[] |
    select(.spec.group == "aigateway.envoyproxy.io") | .metadata.name')
helm upgrade --install envoy-ai-gateway \
    "oci://docker.io/envoyproxy/ai-gateway-helm@$(version '.gateway.envoyAiGatewayChartDigest')" \
    --namespace envoy-ai-gateway-system \
    --values "$script_dir/envoy-ai-gateway-values.yaml" --wait --timeout 10m \
    --set-string kip.owner=kube-inference-platform \
    --set-string "kip.chartDigest=$(version '.gateway.envoyAiGatewayChartDigest')" \
    --set-string "kip.valuesSha256=$ai_values_hash" \
    || die 'Envoy AI Gateway reconciliation failed'
kctl apply -f "$script_dir/gateway.yaml" >/dev/null \
    || die 'private Gateway resource apply failed'
helm upgrade --install kip-qwen25-pilot \
    "$(version '.inference.routerGatewayChart')@$(version '.inference.routerGatewayChartDigest')" \
    --namespace kip-inference --values "$router_values" \
    --set-string kip.owner=kube-inference-platform \
    --set-string "kip.chartDigest=$(version '.inference.routerGatewayChartDigest')" \
    --set-string "kip.valuesSha256=$router_values_hash" \
    --wait --timeout 10m || die 'llm-d endpoint picker reconciliation failed'

# EPP reads its plugin ConfigMap only at process startup. The upstream chart
# does not checksum that ConfigMap into the Deployment Pod template, so a Helm
# values change alone leaves the old picker running. Record the exact reviewed
# values hash on this Helm-owned Deployment and roll only EPP when it differs.
epp_deployment=$(kctl -n kip-inference get deployment kip-qwen25-pilot-epp -o json) \
    || die 'cannot inspect llm-d endpoint picker Deployment'
jq -e '
    .metadata.annotations["meta.helm.sh/release-name"] == "kip-qwen25-pilot" and
    .metadata.annotations["meta.helm.sh/release-namespace"] == "kip-inference"
' >/dev/null <<<"$epp_deployment" || die 'endpoint picker Deployment is not owned by the reviewed release'
current_epp_hash=$(jq -r \
    '.spec.template.metadata.annotations["kube-inference-platform.io/router-values-sha256"] // ""' \
    <<<"$epp_deployment")
if [ "$current_epp_hash" != "$router_values_hash" ]; then
    epp_patch=$(jq -nc --arg hash "$router_values_hash" \
        '{spec:{template:{metadata:{annotations:{"kube-inference-platform.io/router-values-sha256":$hash}}}}}')
    kctl -n kip-inference patch deployment kip-qwen25-pilot-epp --type merge \
        -p "$epp_patch" >/dev/null || die 'could not roll the owned endpoint picker config'
fi
kctl -n kip-inference rollout status deployment/kip-qwen25-pilot-epp --timeout=5m \
    >/dev/null || die 'endpoint picker did not become Ready after configuration rollout'
epp_pods=$(kctl -n kip-inference get pods \
    -l llm-d-router-gateway=kip-qwen25-pilot-epp -o json) \
    || die 'cannot inspect endpoint picker placement'
jq -e '
    (.items | length) == 1 and .items[0].spec.nodeName == "kip-control" and
    any(.items[0].status.conditions[]?; .type == "Ready" and .status == "True")
' >/dev/null <<<"$epp_pods" || die 'endpoint picker is not Ready on the control node'

kctl -n envoy-gateway-system wait --for=condition=Available deployment/envoy-gateway \
    --timeout=5m >/dev/null || die 'Envoy Gateway controller is not Available'
kctl -n envoy-ai-gateway-system wait --for=condition=Available \
    deployment/ai-gateway-controller --timeout=5m >/dev/null \
    || die 'Envoy AI Gateway controller is not Available'
kctl -n kip-inference wait --for=jsonpath='{.status.conditions[?(@.type=="Programmed")].status}'=True \
    gateway/kip-inference-gateway --timeout=5m >/dev/null \
    || die 'private Gateway is not Programmed'
proxy_deployments=$(kctl -n envoy-gateway-system get deployments \
    -l gateway.envoyproxy.io/owning-gateway-name=kip-inference-gateway -o json) \
    || die 'cannot inspect the owned Envoy data plane'
proxy_deployment=$(jq -er '
    if (.items | length) == 1 and
       .items[0].metadata.labels["gateway.envoyproxy.io/owning-gateway-namespace"] == "kip-inference" and
       .items[0].spec.template.spec.nodeSelector["kubernetes.io/hostname"] == "kip-control"
    then .items[0].metadata.name else error("data-plane placement has not reconciled") end
' <<<"$proxy_deployments") || die 'Envoy data plane is not pinned to kip-control'
kctl -n envoy-gateway-system rollout status "deployment/$proxy_deployment" --timeout=5m \
    >/dev/null || die 'Envoy data plane did not become Ready on kip-control'
service_types=$(kctl get services -A -o json | jq -r '
    [.items[] | select(.metadata.namespace == "envoy-gateway-system" or
        .metadata.namespace == "envoy-ai-gateway-system") | .spec.type] | unique | join(",")') \
    || die 'cannot inspect Gateway Services'
[ "$service_types" = ClusterIP ] || die 'a Gateway Service is not internal-only'
require_model_ready
printf '%s\n' 'PASS: two-replica llm-d Gateway path is installed and internal-only.'
