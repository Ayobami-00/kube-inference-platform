#!/usr/bin/env bash

# Kernel, swap, package, and containerd setup.

KIP_CONTAINERD_WAS_PRESENT=0

kip_prepare_kernel() {
    local modules_file sysctl_file

    modules_file="$KIP_WORK_DIR/modules.conf"
    sysctl_file="$KIP_WORK_DIR/sysctl.conf"
    printf '%s\n' overlay br_netfilter > "$modules_file"
    cat > "$sysctl_file" <<'EOF'
# Managed by Kube Inference Platform v0.1.
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
EOF

    kip_install_file "$modules_file" \
        "$KIP_ETC_ROOT/modules-load.d/kube-inference-platform.conf"
    kip_install_file "$sysctl_file" \
        "$KIP_ETC_ROOT/sysctl.d/99-kube-inference-platform.conf"
    kip_run_privileged modprobe overlay
    kip_run_privileged modprobe br_netfilter
    kip_run_privileged sysctl --system >/dev/null
}

kip_disable_swap() {
    local fstab="$KIP_ETC_ROOT/fstab"
    local rendered="$KIP_WORK_DIR/fstab"

    kip_swap_is_supported || return 1
    kip_run_privileged swapoff -a
    if [ -r "$fstab" ]; then
        kip_backup_once "$fstab" "$KIP_ETC_ROOT/fstab.kip-pre-bootstrap"
        command awk '
            /^[[:space:]]*#/ {print; next}
            NF >= 3 && $3 == "swap" {print "# KIP_DISABLED_SWAP " $0; next}
            {print}
        ' "$fstab" > "$rendered"
        kip_install_file "$rendered" "$fstab"
    fi
    [ -z "$(kip_exec swapon --show --noheadings 2>/dev/null || true)" ]
}

kip_check_runtime_conflicts() {
    local package_name expected_containerd installed_containerd

    expected_containerd=$(kip_containerd_package_version) || return 1
    if kip_package_installed containerd.io; then
        KIP_CONTAINERD_WAS_PRESENT=1
        installed_containerd=$(kip_package_version containerd.io)
        [ "$installed_containerd" = "$expected_containerd" ] || return 1
    fi
    for package_name in containerd runc docker.io docker-ce cri-o; do
        if kip_package_installed "$package_name"; then
            return 1
        fi
    done
    if [ -e "$KIP_ETC_ROOT/containerd/config.toml" ] \
        && ! kip_run_privileged grep -q 'Managed by Kube Inference Platform' \
            "$KIP_ETC_ROOT/containerd/config.toml"; then
        return 1
    fi
    if [ "${KIP_TESTING:-0}" != "1" ] \
        && [ "$KIP_CONTAINERD_WAS_PRESENT" -eq 0 ] \
        && kip_has_command containerd; then
        return 1
    fi
}

kip_install_repository_files() {
    local docker_key docker_keyring docker_sources kubernetes_key kubernetes_keyring
    local kubernetes_sources

    docker_key="$KIP_WORK_DIR/docker.asc"
    docker_keyring="$KIP_ETC_ROOT/apt/keyrings/docker.asc"
    docker_sources="$KIP_WORK_DIR/docker.sources"
    kubernetes_key="$KIP_WORK_DIR/kubernetes-release.key"
    kubernetes_keyring="$KIP_WORK_DIR/kubernetes-apt-keyring.gpg"
    kubernetes_sources="$KIP_WORK_DIR/kubernetes.list"

    kip_exec curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$docker_key"
    kip_install_file "$docker_key" "$docker_keyring"
    cat > "$docker_sources" <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $KIP_OS_CODENAME
Components: stable
Architectures: amd64
Signed-By: $docker_keyring
EOF
    kip_install_file "$docker_sources" "$KIP_ETC_ROOT/apt/sources.list.d/docker.sources"

    kip_exec curl -fsSL \
        "https://pkgs.k8s.io/core:/stable:/v1.36/deb/Release.key" \
        -o "$kubernetes_key"
    kip_exec gpg --dearmor --yes --output "$kubernetes_keyring" "$kubernetes_key"
    kip_install_file "$kubernetes_keyring" "$KIP_ETC_ROOT/apt/keyrings/kubernetes-apt-keyring.gpg"
    printf '%s\n' \
        "deb [signed-by=$KIP_ETC_ROOT/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.36/deb/ /" \
        > "$kubernetes_sources"
    kip_install_file "$kubernetes_sources" "$KIP_ETC_ROOT/apt/sources.list.d/kubernetes.list"
}

kip_install_cluster_packages() {
    local containerd_package

    containerd_package=$(kip_containerd_package_version) || return 1
    if kip_package_installed containerd.io "$containerd_package" \
        && kip_package_installed kubelet "$KIP_KUBERNETES_PACKAGE_VERSION" \
        && kip_package_installed kubeadm "$KIP_KUBERNETES_PACKAGE_VERSION" \
        && kip_package_installed kubectl "$KIP_KUBERNETES_PACKAGE_VERSION" \
        && kip_package_installed cri-tools \
        && kip_package_installed kubernetes-cni; then
        kip_run_privileged apt-mark hold \
            containerd.io kubelet kubeadm kubectl cri-tools kubernetes-cni
        return 0
    fi
    kip_install_repository_files
    kip_run_privileged apt-get update
    kip_run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y \
        --no-install-recommends \
        "containerd.io=$containerd_package" \
        "kubelet=$KIP_KUBERNETES_PACKAGE_VERSION" \
        "kubeadm=$KIP_KUBERNETES_PACKAGE_VERSION" \
        "kubectl=$KIP_KUBERNETES_PACKAGE_VERSION" \
        cri-tools kubernetes-cni
    kip_run_privileged apt-mark hold \
        containerd.io kubelet kubeadm kubectl cri-tools kubernetes-cni
    kip_package_installed containerd.io "$containerd_package" \
        && kip_package_installed kubelet "$KIP_KUBERNETES_PACKAGE_VERSION" \
        && kip_package_installed kubeadm "$KIP_KUBERNETES_PACKAGE_VERSION" \
        && kip_package_installed kubectl "$KIP_KUBERNETES_PACKAGE_VERSION" \
        && kip_package_installed cri-tools \
        && kip_package_installed kubernetes-cni
}

kip_install_release_binary() {
    local name=$1
    local archive_url=$2
    local expected_sha=$3
    local archive_path=$4
    local extracted_path=$5

    kip_exec curl -fsSL "$archive_url" -o "$archive_path"
    kip_verify_sha256 "$archive_path" "$expected_sha" || return 1
    kip_exec tar -xzf "$archive_path" -C "$KIP_WORK_DIR"
    kip_install_file "$extracted_path" "$KIP_USR_LOCAL_BIN/$name" 0755
}

kip_install_helm() {
    local output

    if kip_has_command helm; then
        output=$(kip_exec helm version --short 2>/dev/null) || return 1
        case "$output" in
            "v$KIP_HELM_VERSION"*) return 0 ;;
            *) return 1 ;;
        esac
    fi
    kip_install_release_binary \
        helm \
        "https://get.helm.sh/helm-v$KIP_HELM_VERSION-linux-amd64.tar.gz" \
        "$KIP_HELM_LINUX_AMD64_SHA256" \
        "$KIP_WORK_DIR/helm.tar.gz" \
        "$KIP_WORK_DIR/linux-amd64/helm"
}

kip_install_cilium_cli() {
    local output

    if kip_has_command cilium; then
        output=$(kip_exec cilium version --client 2>/dev/null) || return 1
        printf '%s\n' "$output" | command grep -q "$KIP_CILIUM_CLI_VERSION" && return 0
        return 1
    fi
    kip_install_release_binary \
        cilium \
        "https://github.com/cilium/cilium-cli/releases/download/v$KIP_CILIUM_CLI_VERSION/cilium-linux-amd64.tar.gz" \
        "$KIP_CILIUM_CLI_LINUX_AMD64_SHA256" \
        "$KIP_WORK_DIR/cilium.tar.gz" \
        "$KIP_WORK_DIR/cilium"
}

kip_render_containerd_config() {
    local pause_image=$1
    local rendered="$KIP_WORK_DIR/containerd-config.toml"

    command sed "s|__PAUSE_IMAGE__|$pause_image|g" \
        "$KIP_CLUSTER_ROOT/templates/containerd-config.toml" > "$rendered"
    printf '%s' "$rendered"
}

kip_configure_containerd() {
    local pause_image config_file rendered crictl_config

    pause_image=$(kip_exec kubeadm config images list \
        --kubernetes-version "v$KIP_KUBERNETES_VERSION" 2>/dev/null \
        | command awk '/\/pause:/ {print; exit}') || return 1
    [ -n "$pause_image" ] || return 1
    config_file="$KIP_ETC_ROOT/containerd/config.toml"
    if [ -r "$config_file" ] \
        && ! command grep -q 'Managed by Kube Inference Platform' "$config_file"; then
        [ "$KIP_CONTAINERD_WAS_PRESENT" -eq 0 ] || return 1
        kip_backup_once "$config_file" "$KIP_ETC_ROOT/containerd/config.toml.kip-pre-bootstrap"
    fi
    rendered=$(kip_render_containerd_config "$pause_image") || return 1
    kip_install_file "$rendered" "$config_file"
    kip_make_directory "$KIP_ETC_ROOT/containerd/conf.d"

    crictl_config="$KIP_WORK_DIR/crictl.yaml"
    cat > "$crictl_config" <<EOF
# Managed by Kube Inference Platform v0.1.
runtime-endpoint: $KIP_CRI_SOCKET
image-endpoint: $KIP_CRI_SOCKET
timeout: 10
debug: false
EOF
    kip_install_file "$crictl_config" "$KIP_ETC_ROOT/crictl.yaml"

    kip_run_privileged systemctl daemon-reload
    kip_run_privileged systemctl enable --now containerd
    kip_run_privileged systemctl restart containerd
}

kip_verify_containerd() {
    local compact info_output version_output

    kip_exec systemctl is-active --quiet containerd || return 1
    [ -S "$KIP_RUN_ROOT/containerd/containerd.sock" ] \
        || [ "${KIP_TESTING:-0}" = "1" ] \
        || return 1
    version_output=$(kip_exec crictl --runtime-endpoint "$KIP_CRI_SOCKET" version 2>&1) \
        || return 1
    printf '%s\n' "$version_output" \
        | command grep -Eq '^RuntimeApiVersion:[[:space:]]*v1$' \
        || return 1
    info_output=$(kip_exec crictl --runtime-endpoint "$KIP_CRI_SOCKET" info 2>&1) \
        || return 1
    compact=$(printf '%s' "$info_output" | command tr -d '[:space:]')
    printf '%s\n' "$compact" \
        | command grep -Eq '"type":"RuntimeReady"[^}]*"status":true|"runtimeReady":true'
}
