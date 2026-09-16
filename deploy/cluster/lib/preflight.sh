#!/usr/bin/env bash

# Read-only platform, address, and existing-state discovery.

KIP_OS_ID=""
KIP_OS_VERSION=""
KIP_OS_CODENAME=""
KIP_OS_NAME=""
KIP_NODE_IP_RESOLVED=""
KIP_NODE_NAME=""

kip_os_release_value() {
    local key=$1

    command awk -F= -v requested_key="$key" '
        $1 == requested_key {
            value = substr($0, index($0, "=") + 1)
            gsub(/^"|"$/, "", value)
            print value
            exit
        }
    ' "$KIP_OS_RELEASE_FILE"
}

kip_cluster_detect_os() {
    [ -r "$KIP_OS_RELEASE_FILE" ] || return 1
    KIP_OS_ID=$(kip_os_release_value ID)
    KIP_OS_VERSION=$(kip_os_release_value VERSION_ID)
    KIP_OS_CODENAME=$(kip_os_release_value VERSION_CODENAME)
    KIP_OS_NAME=$(kip_os_release_value PRETTY_NAME)
    [ -n "$KIP_OS_CODENAME" ] || {
        case "$KIP_OS_VERSION" in
            22.04) KIP_OS_CODENAME=jammy ;;
            24.04) KIP_OS_CODENAME=noble ;;
        esac
    }
}

kip_cluster_supported_platform() {
    local architecture

    kip_cluster_detect_os || return 1
    architecture=$(kip_exec uname -m 2>/dev/null) || return 1
    [ "$KIP_OS_ID" = "ubuntu" ] \
        && { [ "$KIP_OS_VERSION" = "22.04" ] || [ "$KIP_OS_VERSION" = "24.04" ]; } \
        && { [ "$architecture" = "x86_64" ] || [ "$architecture" = "amd64" ]; }
}

kip_kernel_supported() {
    local release major minor

    release=$(kip_exec uname -r 2>/dev/null) || return 1
    major=${release%%.*}
    release=${release#*.}
    minor=${release%%.*}
    case "$major:$minor" in
        *[!0-9:]* | :) return 1 ;;
    esac
    [ "$major" -gt 5 ] || { [ "$major" -eq 5 ] && [ "$minor" -ge 10 ]; }
}

kip_hostname_valid() {
    KIP_NODE_NAME=$(kip_exec hostname 2>/dev/null) || return 1
    case "$KIP_NODE_NAME" in
        "" | *[!a-zA-Z0-9.-]* | -* | *- | .* | *.) return 1 ;;
    esac
    [ "${#KIP_NODE_NAME}" -le 253 ]
}

kip_ipv4_to_int() {
    local address=$1
    local first second third fourth extra octet
    local old_ifs=$IFS

    IFS=.
    read -r first second third fourth extra <<EOF
$address
EOF
    IFS=$old_ifs
    [ -n "$first" ] && [ -n "$second" ] && [ -n "$third" ] && [ -n "$fourth" ] \
        && [ -z "$extra" ] || return 1
    for octet in "$first" "$second" "$third" "$fourth"; do
        case "$octet" in
            "" | *[!0-9]*) return 1 ;;
        esac
        if [ "${#octet}" -gt 1 ] && [ "${octet#0}" != "$octet" ]; then
            return 1
        fi
        [ "$octet" -le 255 ] || return 1
    done
    printf '%s' "$((first * 16777216 + second * 65536 + third * 256 + fourth))"
}

kip_cidr_bounds() {
    local cidr=$1
    local address prefix address_int mask network broadcast

    case "$cidr" in
        */*) ;;
        *) return 1 ;;
    esac
    address=${cidr%/*}
    prefix=${cidr#*/}
    case "$prefix" in
        "" | *[!0-9]*) return 1 ;;
    esac
    [ "$prefix" -ge 1 ] && [ "$prefix" -le 32 ] || return 1
    address_int=$(kip_ipv4_to_int "$address") || return 1
    mask=$(( (4294967295 << (32 - prefix)) & 4294967295 ))
    network=$((address_int & mask))
    broadcast=$((network | (4294967295 ^ mask)))
    printf '%s %s' "$network" "$broadcast"
}

kip_cidr_is_network() {
    local cidr=$1 address address_int bounds start

    bounds=$(kip_cidr_bounds "$cidr") || return 1
    address=${cidr%/*}
    address_int=$(kip_ipv4_to_int "$address") || return 1
    start=${bounds% *}
    [ "$address_int" -eq "$start" ]
}

kip_cidrs_overlap() {
    local first_bounds second_bounds first_start first_end second_start second_end

    first_bounds=$(kip_cidr_bounds "$1") || return 2
    second_bounds=$(kip_cidr_bounds "$2") || return 2
    first_start=${first_bounds% *}
    first_end=${first_bounds#* }
    second_start=${second_bounds% *}
    second_end=${second_bounds#* }
    [ "$first_start" -le "$second_end" ] && [ "$second_start" -le "$first_end" ]
}

kip_ip_in_cidr() {
    local ip_int bounds start end

    ip_int=$(kip_ipv4_to_int "$1") || return 2
    bounds=$(kip_cidr_bounds "$2") || return 2
    start=${bounds% *}
    end=${bounds#* }
    [ "$ip_int" -ge "$start" ] && [ "$ip_int" -le "$end" ]
}

kip_host_global_addresses() {
    kip_exec ip -4 -o addr show scope global 2>/dev/null \
        | command awk '{for (i=1; i<=NF; i++) if ($i == "inet") print $(i+1)}'
}

kip_node_ip_is_assigned() {
    local candidate=$1 address

    while IFS= read -r address; do
        [ "${address%/*}" = "$candidate" ] && return 0
    done <<EOF
$(kip_host_global_addresses)
EOF
    return 1
}

kip_node_ip_is_usable() {
    kip_ipv4_to_int "$1" >/dev/null || return 1
    case "$1" in
        0.* | 127.* | 169.254.* | 224.* | 225.* | 226.* | 227.* | 228.* | 229.* \
            | 23[0-9].* | 24[0-9].* | 25[0-5].*)
            return 1
            ;;
    esac
}

kip_resolve_node_ip() {
    local defaults default_count selected interface address

    if [ -n "${KIP_NODE_IP:-}" ]; then
        kip_node_ip_is_usable "$KIP_NODE_IP" || return 1
        kip_node_ip_is_assigned "$KIP_NODE_IP" || return 1
        KIP_NODE_IP_RESOLVED=$KIP_NODE_IP
        return 0
    fi

    defaults=$(kip_exec ip -4 route show default 2>/dev/null) || return 1
    default_count=$(printf '%s\n' "$defaults" | command awk 'NF {count++} END {print count + 0}')
    [ "$default_count" -eq 1 ] || return 1
    selected=$(printf '%s\n' "$defaults" | command awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')
    if [ -z "$selected" ]; then
        interface=$(printf '%s\n' "$defaults" | command awk '{for (i=1; i<=NF; i++) if ($i == "dev") {print $(i+1); exit}}')
        address=$(kip_exec ip -4 -o addr show dev "$interface" scope global 2>/dev/null \
            | command awk '{for (i=1; i<=NF; i++) if ($i == "inet") {print $(i+1); exit}}')
        selected=${address%/*}
    fi
    [ -n "$selected" ] \
        && kip_node_ip_is_usable "$selected" \
        && kip_node_ip_is_assigned "$selected" \
        || return 1
    KIP_NODE_IP_RESOLVED=$selected
}

kip_networks_valid() {
    local route route_cidr address existing_state=${1:-fresh}

    kip_cidr_is_network "$KIP_POD_CIDR" || return 1
    kip_cidr_is_network "$KIP_SERVICE_CIDR" || return 1
    if kip_cidrs_overlap "$KIP_POD_CIDR" "$KIP_SERVICE_CIDR"; then
        return 1
    fi
    if kip_ip_in_cidr "$KIP_NODE_IP_RESOLVED" "$KIP_POD_CIDR" \
        || kip_ip_in_cidr "$KIP_NODE_IP_RESOLVED" "$KIP_SERVICE_CIDR"; then
        return 1
    fi
    if [ "$existing_state" = healthy ]; then
        return 0
    fi

    while IFS= read -r address; do
        [ -n "$address" ] || continue
        if kip_cidrs_overlap "$KIP_POD_CIDR" "$address" \
            || kip_cidrs_overlap "$KIP_SERVICE_CIDR" "$address"; then
            return 1
        fi
    done <<EOF
$(kip_host_global_addresses)
EOF

    while IFS= read -r route; do
        route_cidr=$(printf '%s\n' "$route" | command awk '{print $1}')
        case "$route_cidr" in
            default | "" | *:* | */32) continue ;;
            */*) ;;
            *) continue ;;
        esac
        if kip_cidrs_overlap "$KIP_POD_CIDR" "$route_cidr" \
            || kip_cidrs_overlap "$KIP_SERVICE_CIDR" "$route_cidr"; then
            return 1
        fi
    done <<EOF
$(kip_exec ip -4 route show 2>/dev/null || true)
EOF
}

kip_dns_and_time_ready() {
    local endpoint synchronized

    for endpoint in pkgs.k8s.io download.docker.com quay.io registry.k8s.io github.com; do
        kip_exec getent hosts "$endpoint" >/dev/null 2>&1 || return 1
    done
    synchronized=$(kip_exec timedatectl show --property=NTPSynchronized --value 2>/dev/null) \
        || return 1
    [ "$synchronized" = "yes" ]
}

kip_containerd_package_version() {
    case "$KIP_OS_VERSION" in
        22.04) printf '%s' "${KIP_CONTAINERD_VERSION}-${KIP_CONTAINERD_PACKAGE_REVISION}~ubuntu.22.04~jammy" ;;
        24.04) printf '%s' "${KIP_CONTAINERD_VERSION}-${KIP_CONTAINERD_PACKAGE_REVISION}~ubuntu.24.04~noble" ;;
        *) return 1 ;;
    esac
}

kip_existing_cluster_state() {
    local admin_conf="$KIP_ETC_ROOT/kubernetes/admin.conf"
    local kubelet_config="$KIP_VAR_ROOT/lib/kubelet/config.yaml"
    local kubelet_conf="$KIP_ETC_ROOT/kubernetes/kubelet.conf"
    local manifests="$KIP_ETC_ROOT/kubernetes/manifests"
    local etcd="$KIP_VAR_ROOT/lib/etcd"
    local pki="$KIP_ETC_ROOT/kubernetes/pki"

    if [ -r "$admin_conf" ]; then
        if kip_kubectl get --raw=/readyz >/dev/null 2>&1; then
            printf '%s' healthy
        else
            printf '%s' partial
        fi
    elif [ -e "$admin_conf" ] \
        && kip_run_privileged kubectl --kubeconfig "$admin_conf" get --raw=/readyz \
            >/dev/null 2>&1; then
        printf '%s' healthy
    elif kip_cluster_directory_has_state "$manifests" \
        || kip_cluster_directory_has_state "$etcd" \
        || [ -e "$kubelet_config" ] \
        || [ -e "$kubelet_conf" ] \
        || kip_cluster_directory_has_state "$pki"; then
        printf '%s' partial
    else
        printf '%s' fresh
    fi
}

kip_cluster_directory_has_state() {
    local entry path=$1

    [ -d "$path" ] || return 1
    for entry in "$path"/* "$path"/.[!.]* "$path"/..?*; do
        [ -e "$entry" ] || continue
        case "${entry##*/}" in
            .kubelet-keep) continue ;;
        esac
        return 0
    done
    return 1
}

kip_swap_is_supported() {
    local active source fstab_source resolved matched

    active=$(kip_exec swapon --show --noheadings --raw --output NAME 2>/dev/null) || active=""
    [ -z "$active" ] && return 0
    [ -r "$KIP_ETC_ROOT/fstab" ] || return 1
    while IFS= read -r source; do
        [ -n "$source" ] || continue
        matched=0
        while IFS= read -r fstab_source; do
            [ -n "$fstab_source" ] || continue
            resolved=$fstab_source
            case "$fstab_source" in
                UUID=*) resolved=$(kip_exec blkid -U "${fstab_source#UUID=}" 2>/dev/null || true) ;;
                LABEL=*) resolved=$(kip_exec blkid -L "${fstab_source#LABEL=}" 2>/dev/null || true) ;;
            esac
            if [ "$source" = "$fstab_source" ] || [ "$source" = "$resolved" ]; then
                matched=1
                break
            fi
        done <<EOF
$(command awk '$0 !~ /^[[:space:]]*#/ && $3 == "swap" {print $1}' "$KIP_ETC_ROOT/fstab")
EOF
        [ "$matched" -eq 1 ] || return 1
    done <<EOF
$active
EOF
}

kip_kubernetes_packages_compatible() {
    local package_name installed_version

    for package_name in kubelet kubeadm kubectl; do
        if kip_package_installed "$package_name"; then
            installed_version=$(kip_package_version "$package_name") || return 1
            [ "$installed_version" = "$KIP_KUBERNETES_PACKAGE_VERSION" ] || return 1
        elif [ "${KIP_TESTING:-0}" != "1" ] && kip_has_command "$package_name"; then
            return 1
        fi
    done
}
