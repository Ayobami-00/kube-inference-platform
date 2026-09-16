#!/usr/bin/env bash

# kubeadm rendering, initialization, kubeconfig, and Kubernetes health helpers.

kip_kubectl() {
    local kubeconfig=${KIP_KUBECONFIG:-}

    if [ -z "$kubeconfig" ] && [ -r "$KIP_ETC_ROOT/kubernetes/admin.conf" ]; then
        kubeconfig="$KIP_ETC_ROOT/kubernetes/admin.conf"
    fi
    if [ -n "$kubeconfig" ]; then
        kip_exec kubectl --kubeconfig "$kubeconfig" "$@"
    else
        kip_exec kubectl "$@"
    fi
}

kip_render_kubeadm_config() {
    local rendered=$1

    command sed \
        -e "s|__NODE_IP__|$KIP_NODE_IP_RESOLVED|g" \
        -e "s|__NODE_NAME__|$KIP_NODE_NAME|g" \
        -e "s|__CRI_SOCKET__|$KIP_CRI_SOCKET|g" \
        -e "s|__KUBERNETES_VERSION__|$KIP_KUBERNETES_VERSION|g" \
        -e "s|__DNS_DOMAIN__|$KIP_DNS_DOMAIN|g" \
        -e "s|__POD_CIDR__|$KIP_POD_CIDR|g" \
        -e "s|__SERVICE_CIDR__|$KIP_SERVICE_CIDR|g" \
        "$KIP_CLUSTER_ROOT/templates/kubeadm-config.yaml" > "$rendered"
}

kip_existing_cluster_matches() {
    local version cluster_config node_ip

    version=$(kip_kubectl version -o json 2>/dev/null \
        | command sed -n 's/.*"gitVersion"[[:space:]]*:[[:space:]]*"v\([0-9.]*\)".*/\1/p' \
        | command tail -n 1) || return 1
    [ "$version" = "$KIP_KUBERNETES_VERSION" ] || return 1
    cluster_config=$(kip_kubectl -n kube-system get configmap kubeadm-config \
        -o 'jsonpath={.data.ClusterConfiguration}' 2>/dev/null) || return 1
    printf '%s\n' "$cluster_config" | command grep -Fq "podSubnet: $KIP_POD_CIDR" || return 1
    printf '%s\n' "$cluster_config" | command grep -Fq "serviceSubnet: $KIP_SERVICE_CIDR" || return 1
    node_ip=$(kip_kubectl get node "$KIP_NODE_NAME" \
        -o 'jsonpath={.status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null) || return 1
    [ "$node_ip" = "$KIP_NODE_IP_RESOLVED" ]
}

kip_initialize_kubernetes() {
    local state=$1
    local config="$KIP_WORK_DIR/kubeadm-config.yaml"
    local images output

    kip_render_kubeadm_config "$config"
    if [ "$state" = healthy ]; then
        kip_existing_cluster_matches
        return
    fi
    [ "$state" = fresh ] || return 1
    kip_exec kubeadm config validate --config "$config"
    images=$(kip_exec kubeadm config images list --config "$config") || return 1
    kip_verbose_output "kubeadm control-plane images" "$images"
    output=$(kip_run_privileged kubeadm config images pull --config "$config" 2>&1) \
        || return 1
    kip_verbose_output "kubeadm image pre-pull" "$output"
    kip_run_privileged kubeadm init --config "$config"
}

kip_configure_user_kubeconfig() {
    local target_group target_home target_user kube_dir source_config

    source_config="$KIP_ETC_ROOT/kubernetes/admin.conf"
    [ -e "$source_config" ] || return 1
    target_user=${KIP_TARGET_USER:-}
    if [ -z "$target_user" ]; then
        if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
            target_user=$SUDO_USER
        elif [ "$(kip_effective_uid)" -ne 0 ]; then
            target_user=$(kip_exec id -un) || return 1
        else
            target_user=root
        fi
    fi
    if [ -n "${KIP_TARGET_HOME:-}" ]; then
        target_home=$KIP_TARGET_HOME
    else
        target_home=$(kip_exec getent passwd "$target_user" 2>/dev/null | command cut -d: -f6)
    fi
    [ -n "$target_home" ] || return 1
    kube_dir="$target_home/.kube"
    kip_make_directory "$kube_dir" 0700
    kip_run_privileged cp "$source_config" "$kube_dir/config"
    kip_run_privileged chmod 0600 "$kube_dir/config"
    if [ "${KIP_TESTING:-0}" != "1" ]; then
        target_group=$(kip_exec id -gn "$target_user") || return 1
        kip_run_privileged chown -R "$target_user:$target_group" "$kube_dir"
    fi
    KIP_KUBECONFIG="$kube_dir/config"
    KUBECONFIG=$KIP_KUBECONFIG
    export KIP_KUBECONFIG KUBECONFIG
}

kip_make_single_node_schedulable() {
    local taints output

    taints=$(kip_kubectl get nodes -o 'jsonpath={range .items[*].spec.taints[*]}{.key}:{.effect}{"\n"}{end}' \
        2>/dev/null) || return 1
    if printf '%s\n' "$taints" | command grep -q '^node-role.kubernetes.io/control-plane:NoSchedule$'; then
        output=$(kip_kubectl taint nodes --all node-role.kubernetes.io/control-plane- 2>&1) \
            || return 1
        kip_verbose_output "kubectl taint" "$output"
    fi
}

kip_wait_for_cluster() {
    kip_exec systemctl is-active --quiet kubelet || return 1
    kip_kubectl get --raw=/readyz >/dev/null || return 1
    kip_kubectl wait --for=condition=Ready node --all --timeout=5m >/dev/null || return 1
    kip_nodes_ready_and_schedulable || return 1
    kip_kubectl -n kube-system rollout status daemonset/kube-proxy --timeout=5m >/dev/null \
        || return 1
    kip_kubectl -n kube-system rollout status deployment/coredns --timeout=5m >/dev/null \
        || return 1
}

kip_nodes_ready_and_schedulable() {
    local output

    output=$(kip_kubectl get nodes \
        -o 'jsonpath={range .items[*]}{.metadata.name}|{.spec.unschedulable}|{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' \
        2>/dev/null) || return 1
    [ -n "$output" ] || return 1
    kip_verbose_output "kubectl get nodes" "$output"
    printf '%s\n' "$output" | command awk -F'|' \
        'NF < 3 || $2 == "true" || $3 != "True" {bad=1} END {exit bad}'
}

kip_component_ready() {
    local label=$1 output

    output=$(kip_kubectl -n kube-system get pods -l "$label" \
        -o 'jsonpath={range .items[*]}{.status.phase}|{range .status.containerStatuses[*]}{.ready},{end}{"\n"}{end}' \
        2>/dev/null) || return 1
    [ -n "$output" ] || return 1
    printf '%s\n' "$output" | command awk -F'|' '
        NF < 2 || $1 != "Running" || $2 !~ /true/ || $2 ~ /false/ {bad=1}
        END {exit bad}
    '
}

kip_all_system_pods_healthy() {
    local output

    output=$(kip_kubectl -n kube-system get pods \
        -o 'jsonpath={range .items[*]}{.metadata.name}|{.status.phase}|{range .status.containerStatuses[*]}{.ready},{end}{"\n"}{end}' \
        2>/dev/null) || return 1
    [ -n "$output" ] || return 1
    kip_verbose_output "kubectl get pods -n kube-system" "$output"
    printf '%s\n' "$output" | command awk -F'|' '
        $2 == "Succeeded" {next}
        $2 != "Running" || $3 !~ /true/ || $3 ~ /false/ {bad=1}
        END {exit bad}
    '
}

kip_cluster_versions_match() {
    local output

    output=$(kip_exec containerd --version 2>/dev/null) || return 1
    printf '%s\n' "$output" | command grep -Fq "$KIP_CONTAINERD_VERSION" || return 1
    output=$(kip_exec kubeadm version -o short 2>/dev/null) || return 1
    [ "$output" = "v$KIP_KUBERNETES_VERSION" ] || return 1
    output=$(kip_exec kubelet --version 2>/dev/null) || return 1
    printf '%s\n' "$output" | command grep -Fq "v$KIP_KUBERNETES_VERSION" || return 1
    output=$(kip_exec kubectl version --client -o json 2>/dev/null) || return 1
    printf '%s\n' "$output" | command grep -Fq "v$KIP_KUBERNETES_VERSION" || return 1
}
