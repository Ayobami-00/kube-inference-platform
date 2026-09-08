#!/usr/bin/env bash

# Provider-neutral checks for Ubuntu Kubernetes hosts.

KIP_HOST_CHECK_COUNT=17
KIP_OS_ID=""
KIP_OS_VERSION=""
KIP_OS_NAME=""
KIP_NVIDIA_SMI_OK=0

kip_validate_boolean() {
    case "$2" in
        0 | 1) return 0 ;;
        *) kip_usage_error "$1 must be 0 or 1; received '$2'." ;;
    esac
}

kip_has_command() {
    local command_name=$1

    if [ "${KIP_TESTING:-0}" = "1" ]; then
        [ -x "${KIP_TEST_COMMAND_DIR:?}/$command_name" ]
        return
    fi

    command -v "$command_name" >/dev/null 2>&1
}

kip_exec() {
    local command_name=$1
    local exit_code
    shift

    if [ "${KIP_TESTING:-0}" = "1" ]; then
        if [ ! -x "${KIP_TEST_COMMAND_DIR:?}/$command_name" ]; then
            return 127
        fi
        "${KIP_TEST_COMMAND_DIR}/$command_name" "$@" || {
            exit_code=$?
            return "$exit_code"
        }
        return
    fi

    command "$command_name" "$@" || {
        exit_code=$?
        return "$exit_code"
    }
}

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

kip_detect_os() {
    if [ ! -r "$KIP_OS_RELEASE_FILE" ]; then
        return 1
    fi

    KIP_OS_ID=$(kip_os_release_value ID)
    KIP_OS_VERSION=$(kip_os_release_value VERSION_ID)
    KIP_OS_NAME=$(kip_os_release_value PRETTY_NAME)
    return 0
}

kip_supported_os() {
    [ "$KIP_OS_ID" = "ubuntu" ] && {
        [ "$KIP_OS_VERSION" = "22.04" ] || [ "$KIP_OS_VERSION" = "24.04" ]
    }
}

kip_supported_architecture() {
    local architecture

    architecture=$(kip_exec uname -m 2>/dev/null) || return 1
    [ "$architecture" = "x86_64" ] || [ "$architecture" = "amd64" ]
}

kip_check_operating_system() {
    if ! kip_detect_os; then
        kip_record FAIL "Operating system" "Cannot read $KIP_OS_RELEASE_FILE."
    elif kip_supported_os; then
        kip_record PASS "Operating system" "${KIP_OS_NAME:-Ubuntu $KIP_OS_VERSION}"
    else
        kip_record FAIL "Operating system" \
            "Ubuntu 22.04/24.04 required; detected ${KIP_OS_NAME:-unknown}."
    fi
}

kip_check_architecture() {
    local architecture

    architecture=$(kip_exec uname -m 2>/dev/null) || architecture="unknown"
    if [ "$architecture" = "x86_64" ] || [ "$architecture" = "amd64" ]; then
        kip_record PASS "CPU architecture" "$architecture"
    else
        kip_record FAIL "CPU architecture" "x86_64 required; detected $architecture."
    fi
}

kip_check_systemd() {
    local init_process

    init_process=$(kip_exec ps -p 1 -o comm= 2>/dev/null) || init_process="unknown"
    init_process=$(printf '%s' "$init_process" | command tr -d '[:space:]')
    if [ "$init_process" = "systemd" ]; then
        kip_record PASS "Init system" "systemd"
    else
        kip_record FAIL "Init system" "systemd required; PID 1 is $init_process."
    fi
}

kip_check_inspection_commands() {
    local command_name
    local missing=""

    for command_name in lscpu free lsblk ip; do
        if ! kip_has_command "$command_name"; then
            missing="${missing}${missing:+, }${command_name}"
        fi
    done

    if [ -z "$missing" ]; then
        kip_record PASS "Inspection commands" "lscpu, free, lsblk, and ip are available."
    else
        kip_record FAIL "Inspection commands" "Missing: $missing. Run make host-setup."
    fi
}

kip_check_hostname() {
    local host_name

    host_name=$(kip_exec hostname 2>/dev/null) || host_name=""
    if [ -n "$host_name" ]; then
        kip_record PASS "Hostname" "$host_name"
    else
        kip_record WARN "Hostname" "Unable to determine the hostname."
    fi
}

kip_check_cpu() {
    local output
    local cpu_count
    local model

    if ! kip_has_command lscpu; then
        kip_record FAIL "CPU inventory" "lscpu is unavailable; run make host-setup."
        return
    fi

    if output=$(kip_exec lscpu 2>&1); then
        cpu_count=$(printf '%s\n' "$output" | command awk -F: '$1 == "CPU(s)" {gsub(/^[ \t]+/, "", $2); print $2; exit}')
        model=$(printf '%s\n' "$output" | command awk -F: '$1 == "Model name" {gsub(/^[ \t]+/, "", $2); print $2; exit}')
        kip_record PASS "CPU inventory" "${cpu_count:-unknown} CPUs; ${model:-model unavailable}"
        kip_verbose_output "lscpu" "$output"
    else
        kip_record FAIL "CPU inventory" "lscpu failed: ${output:-no output}."
    fi
}

kip_check_memory() {
    local output
    local memory

    if ! kip_has_command free; then
        kip_record FAIL "Memory inventory" "free is unavailable; run make host-setup."
        return
    fi

    if output=$(kip_exec free -h 2>&1); then
        memory=$(printf '%s\n' "$output" | command awk '$1 == "Mem:" {print $2; exit}')
        kip_record PASS "Memory inventory" "${memory:-total unavailable}"
        kip_verbose_output "free -h" "$output"
    else
        kip_record FAIL "Memory inventory" "free -h failed: ${output:-no output}."
    fi
}

kip_check_storage() {
    local output
    local device_output
    local device_count

    if ! kip_has_command lsblk; then
        kip_record FAIL "Block devices" "lsblk is unavailable; run make host-setup."
        return
    fi

    if output=$(kip_exec lsblk 2>&1); then
        device_output=$(kip_exec lsblk -dn -o TYPE 2>/dev/null) || device_output=""
        device_count=$(printf '%s\n' "$device_output" | command awk '$1 == "disk" {count++} END {print count + 0}')
        if [ "$device_count" -gt 0 ]; then
            kip_record PASS "Block devices" "$device_count disk device(s)"
        else
            kip_record FAIL "Block devices" "No disk devices reported by lsblk."
        fi
        kip_verbose_output "lsblk" "$output"
    else
        kip_record FAIL "Block devices" "lsblk failed: ${output:-no output}."
    fi
}

kip_check_interfaces() {
    local output
    local active_output
    local active_count

    if ! kip_has_command ip; then
        kip_record FAIL "Network interfaces" "ip is unavailable; run make host-setup."
        return
    fi

    if output=$(kip_exec ip link 2>&1); then
        active_output=$(kip_exec ip -o link show up 2>/dev/null) || active_output=""
        active_count=$(printf '%s\n' "$active_output" | command awk -F': ' '$2 != "lo" && $2 != "" {count++} END {print count + 0}')
        if [ "$active_count" -gt 0 ]; then
            kip_record PASS "Network interfaces" "$active_count active non-loopback interface(s)"
        else
            kip_record FAIL "Network interfaces" "No active non-loopback interface found."
        fi
        kip_verbose_output "ip link" "$output"
    else
        kip_record FAIL "Network interfaces" "ip link failed: ${output:-no output}."
    fi
}

kip_check_default_route() {
    local output

    if ! kip_has_command ip; then
        kip_record WARN "Default route" "ip is unavailable."
        return
    fi

    output=$(kip_exec ip route show default 2>/dev/null) || output=""
    if [ -n "$output" ]; then
        kip_record PASS "Default route" "Available"
        kip_verbose_output "ip route show default" "$output"
    else
        kip_record WARN "Default route" "No default route detected."
    fi
}

kip_check_dns() {
    local output

    if ! kip_has_command getent; then
        kip_record WARN "DNS resolution" "getent is unavailable."
    elif output=$(kip_exec getent hosts pkgs.k8s.io 2>&1); then
        kip_record PASS "DNS resolution" "pkgs.k8s.io resolves."
        kip_verbose_output "getent hosts pkgs.k8s.io" "$output"
    else
        kip_record WARN "DNS resolution" "Cannot resolve pkgs.k8s.io."
    fi
}

kip_check_time_sync() {
    local synchronized

    if ! kip_has_command timedatectl; then
        kip_record WARN "Time synchronization" "timedatectl is unavailable."
        return
    fi

    synchronized=$(kip_exec timedatectl show --property=NTPSynchronized --value 2>/dev/null) || synchronized="unknown"
    if [ "$synchronized" = "yes" ]; then
        kip_record PASS "Time synchronization" "NTP synchronized"
    else
        kip_record WARN "Time synchronization" "NTP status: $synchronized"
    fi
}

kip_check_swap() {
    local output

    if ! kip_has_command swapon; then
        kip_record WARN "Swap" "swapon is unavailable."
        return
    fi

    output=$(kip_exec swapon --show --noheadings 2>/dev/null) || output=""
    if [ -z "$output" ]; then
        kip_record PASS "Swap" "No active swap"
    else
        kip_record WARN "Swap" "Active swap detected; cluster bootstrap will handle it."
        kip_verbose_output "swapon --show" "$output"
    fi
}

kip_check_cluster_software() {
    local command_name
    local detected=""

    for command_name in containerd kubeadm kubelet kubectl; do
        if kip_has_command "$command_name"; then
            detected="${detected}${detected:+, }${command_name}"
        fi
    done

    if [ -n "$detected" ]; then
        kip_record WARN "Existing cluster software" "Detected: $detected"
    else
        kip_record SKIP "Existing cluster software" "Not installed (expected before bootstrap)."
    fi
}

kip_gpu_not_ready() {
    local label=$1
    local detail=$2

    if [ "$KIP_GPU_REQUIRED" = "1" ]; then
        kip_record FAIL "$label" "$detail"
    else
        kip_record DEFERRED "$label" "$detail"
    fi
}

kip_check_nvidia_hardware() {
    local output

    if ! kip_has_command lspci; then
        kip_gpu_not_ready "NVIDIA hardware" "lspci is unavailable; run make host-setup."
    elif output=$(kip_exec lspci -Dnn -d 10de: 2>&1) && [ -n "$output" ]; then
        kip_record PASS "NVIDIA hardware" "NVIDIA PCI device detected."
        kip_verbose_output "lspci -Dnn -d 10de:" "$output"
    else
        kip_gpu_not_ready "NVIDIA hardware" "No NVIDIA PCI device detected."
    fi
}

kip_check_nvidia_driver() {
    local output

    KIP_NVIDIA_SMI_OK=0
    if ! kip_has_command nvidia-smi; then
        kip_gpu_not_ready "NVIDIA driver" "nvidia-smi is unavailable."
    elif output=$(kip_exec nvidia-smi 2>&1); then
        KIP_NVIDIA_SMI_OK=1
        kip_record PASS "NVIDIA driver" "nvidia-smi completed successfully."
        kip_verbose_output "nvidia-smi" "$output"
    else
        kip_gpu_not_ready "NVIDIA driver" "nvidia-smi failed: ${output:-no output}."
    fi
}

kip_check_nvidia_topology() {
    local output

    if [ "$KIP_NVIDIA_SMI_OK" != "1" ]; then
        kip_gpu_not_ready "GPU topology" "Requires a healthy nvidia-smi command."
    elif output=$(kip_exec nvidia-smi topo -m 2>&1); then
        kip_record PASS "GPU topology" "nvidia-smi topo -m completed successfully."
        kip_verbose_output "nvidia-smi topo -m" "$output"
    else
        kip_gpu_not_ready "GPU topology" "nvidia-smi topo -m failed: ${output:-no output}."
    fi
}

kip_run_host_checks() {
    kip_reset_results "$KIP_HOST_CHECK_COUNT"
    kip_check_operating_system
    kip_check_architecture
    kip_check_systemd
    kip_check_inspection_commands
    kip_check_hostname
    kip_check_cpu
    kip_check_memory
    kip_check_storage
    kip_check_interfaces
    kip_check_default_route
    kip_check_dns
    kip_check_time_sync
    kip_check_swap
    kip_check_cluster_software
    kip_check_nvidia_hardware
    kip_check_nvidia_driver
    kip_check_nvidia_topology
    kip_summary

    [ "$KIP_FAILED" -eq 0 ]
}
