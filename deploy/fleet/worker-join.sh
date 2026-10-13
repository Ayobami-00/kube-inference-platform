#!/usr/bin/env bash
# Target-local, ownership-safe kubeadm worker join over the KIP WireGuard mesh.
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
KIP_DEPLOY_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
readonly KIP_DEPLOY_ROOT
KIP_CLUSTER_ROOT="$KIP_DEPLOY_ROOT/cluster"
readonly KIP_CLUSTER_ROOT

# shellcheck source=deploy/lib/ui.sh
source "$KIP_DEPLOY_ROOT/lib/ui.sh"
# shellcheck source=deploy/cluster/versions.env
source "$KIP_CLUSTER_ROOT/versions.env"
# shellcheck source=deploy/cluster/lib/common.sh
source "$KIP_CLUSTER_ROOT/lib/common.sh"
# shellcheck source=deploy/cluster/lib/preflight.sh
source "$KIP_CLUSTER_ROOT/lib/preflight.sh"
# shellcheck source=deploy/cluster/lib/runtime.sh
source "$KIP_CLUSTER_ROOT/lib/runtime.sh"

export LC_ALL=C
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_NODE_IP=${KIP_NODE_IP:-}
KIP_POD_CIDR=${KIP_POD_CIDR:-$KIP_DEFAULT_POD_CIDR}
KIP_SERVICE_CIDR=${KIP_SERVICE_CIDR:-$KIP_DEFAULT_SERVICE_CIDR}
KIP_WORK_DIR=""

kip_ui_init
kip_cluster_validate_boolean VERBOSE "$KIP_VERBOSE"
[ "$#" -eq 0 ] || kip_usage_error "worker-join does not accept positional arguments."
[ -n "$KIP_NODE_IP" ] || kip_usage_error "worker-join requires the explicit WireGuard NODE_IP."

KIP_WORK_DIR=$(command mktemp -d "${TMPDIR:-/tmp}/kip-worker.XXXXXX")
readonly KIP_WORK_DIR
kip_cleanup() { command rm -rf "$KIP_WORK_DIR"; }
trap kip_cleanup EXIT

join_plan="$KIP_WORK_DIR/join-plan.json"
join_config="$KIP_WORK_DIR/join-config.yaml"
umask 077
command cat > "$join_plan"
if ! jq -e '
    (keys == ["caHash", "controlIpv4", "schemaVersion", "token"])
    and .schemaVersion == 1
    and (.token | type == "string" and test("^[a-z0-9]{6}\\.[a-z0-9]{16}$"))
    and (.caHash | type == "string" and test("^sha256:[a-f0-9]{64}$"))
    and (.controlIpv4 == "10.200.0.1")
' "$join_plan" >/dev/null; then
    kip_usage_error "worker-join requires a reviewed, exact-schema private join plan."
fi
join_token=$(command jq -r .token "$join_plan")
join_hash=$(command jq -r .caHash "$join_plan")
control_ip=$(command jq -r .controlIpv4 "$join_plan")
readonly join_token join_hash control_ip
trap 'kip_unexpected_error "$?" "$LINENO"' ERR

kip_logo
kip_heading "Fleet GPU Worker Join · pilot"
kip_reset_results 8

if kip_cluster_supported_platform && kip_kernel_supported && kip_hostname_valid; then
    case "$KIP_NODE_NAME" in
        kip-gpu-1 | kip-gpu-2 | kip-speech-1)
            kip_record PASS "Supported worker" "$KIP_NODE_NAME on Ubuntu $KIP_OS_VERSION"
            ;;
        *)
            kip_record FAIL "Supported worker" "Set a reviewed, unique fleet hostname first."
            kip_summary "Fleet worker join"
            exit 1
            ;;
    esac
else
    kip_record FAIL "Supported worker" "Ubuntu 22.04/24.04 x86_64 and kernel >= 5.10 required."
    kip_summary "Fleet worker join"
    exit 1
fi

if KIP_NO_LOGO=1 KIP_GPU_REQUIRED=0 "$KIP_DEPLOY_ROOT/host/validate.sh"; then
    kip_record PASS "Host validation" "Required host checks passed."
else
    kip_record FAIL "Host validation" "Run host-setup and resolve required host failures."
    kip_summary "Fleet worker join"
    exit 1
fi

if kip_acquire_privileges; then
    kip_record PASS "Administrative access" "Root or sudo acquired."
else
    kip_record FAIL "Administrative access" "Root or functional sudo is required."
    kip_summary "Fleet worker join"
    exit 1
fi

resolved_api=$(kip_exec getent ahostsv4 api.kip.internal 2>/dev/null \
    | command awk 'NR == 1 {print $1}')
if kip_node_ip_is_usable "$KIP_NODE_IP" \
    && kip_node_ip_is_assigned "$KIP_NODE_IP" \
    && [ "$resolved_api" = "$control_ip" ] \
    && kip_exec ip link show wg-kip >/dev/null 2>&1 \
    && kip_exec ping -c 2 -W 2 "$control_ip" >/dev/null 2>&1; then
    KIP_NODE_IP_RESOLVED=$KIP_NODE_IP
    kip_record PASS "Private API path" "$KIP_NODE_IP → api.kip.internal ($control_ip)"
else
    kip_record FAIL "Private API path" "WireGuard, unique node IP, and private API DNS are required."
    kip_summary "Fleet worker join"
    exit 1
fi

state=fresh
if [ -e "$KIP_ETC_ROOT/kubernetes/admin.conf" ] \
    || kip_cluster_directory_has_state "$KIP_ETC_ROOT/kubernetes/manifests" \
    || kip_cluster_directory_has_state "$KIP_VAR_ROOT/lib/etcd"; then
    kip_record FAIL "Join state" "Control-plane or unrelated kubeadm state exists; no reset is automatic."
    kip_summary "Fleet worker join"
    exit 1
fi
if [ -e "$KIP_ETC_ROOT/kubernetes/kubelet.conf" ]; then
    state=joined
    if ! command grep -Fq 'server: https://api.kip.internal:6443' \
        "$KIP_ETC_ROOT/kubernetes/kubelet.conf" \
        || ! kip_exec systemctl is-active --quiet kubelet; then
        kip_record FAIL "Join state" "Existing worker is partial or joined to another API."
        kip_summary "Fleet worker join"
        exit 1
    fi
elif kip_cluster_directory_has_state "$KIP_ETC_ROOT/kubernetes/pki" \
    || [ -e "$KIP_VAR_ROOT/lib/kubelet/config.yaml" ]; then
    kip_record FAIL "Join state" "Partial kubeadm state requires manual recovery."
    kip_summary "Fleet worker join"
    exit 1
fi
network_state=$state
[ "$network_state" = joined ] && network_state=healthy
if kip_dns_and_time_ready \
    && kip_networks_valid "$network_state" \
    && kip_swap_is_supported \
    && kip_check_runtime_conflicts \
    && kip_kubernetes_packages_compatible; then
    kip_record PASS "Join preflight" "Network CIDRs, time, swap, and package ownership passed."
else
    kip_record FAIL "Join preflight" "Resolve network, swap, runtime, or version drift."
    kip_summary "Fleet worker join"
    exit 1
fi
kip_record PASS "Join state" "$state; no destructive reset is permitted."

if kip_prepare_kernel && kip_disable_swap && kip_install_cluster_packages \
    && kip_configure_containerd && kip_verify_containerd; then
    kip_record PASS "Pinned runtime" "containerd and Kubernetes packages are ready."
else
    kip_record FAIL "Pinned runtime" "Kernel, package, or containerd preparation failed."
    kip_summary "Fleet worker join"
    exit 1
fi

if [ "$state" = fresh ]; then
    command cat > "$join_config" <<EOF
apiVersion: kubeadm.k8s.io/v1beta4
kind: JoinConfiguration
discovery:
  bootstrapToken:
    apiServerEndpoint: api.kip.internal:6443
    token: "$join_token"
    caCertHashes:
      - "$join_hash"
nodeRegistration:
  name: "$KIP_NODE_NAME"
  criSocket: "$KIP_CRI_SOCKET"
  kubeletExtraArgs:
    - name: node-ip
      value: "$KIP_NODE_IP"
EOF
    if ! kip_exec kubeadm config validate --config "$join_config" >/dev/null 2>&1; then
        kip_record FAIL "kubeadm join" "Rendered JoinConfiguration failed validation."
        kip_summary "Fleet worker join"
        exit 1
    fi
    if ! kip_run_privileged kubeadm join --config "$join_config" \
        >"$KIP_WORK_DIR/kubeadm-join.log" 2>&1; then
        kip_record FAIL "kubeadm join" \
            "Join failed; inspect kubelet journal and kubeadm state before retrying."
        kip_summary "Fleet worker join"
        exit 1
    fi
fi
if [ -r "$KIP_ETC_ROOT/kubernetes/kubelet.conf" ] \
    && kip_exec systemctl is-active --quiet kubelet; then
    kip_record PASS "kubeadm join" "Kubelet joined through private API; fleet-check will verify Ready."
else
    kip_record FAIL "kubeadm join" "Kubelet config or service is missing."
    kip_summary "Fleet worker join"
    exit 1
fi

kip_summary "Fleet worker join"
