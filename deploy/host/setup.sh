#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
KIP_HOST_ROOT=$SCRIPT_DIR
readonly KIP_HOST_ROOT

# shellcheck source=deploy/host/lib/ui.sh
source "$KIP_HOST_ROOT/lib/ui.sh"
# shellcheck source=deploy/host/lib/checks.sh
source "$KIP_HOST_ROOT/lib/checks.sh"

export LC_ALL=C
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_GPU_REQUIRED=${KIP_GPU_REQUIRED:-0}
KIP_OS_RELEASE_FILE=${KIP_OS_RELEASE_FILE:-/etc/os-release}
KIP_REQUIRED_PACKAGES=(ca-certificates curl gpg jq pciutils procps iproute2 util-linux)
KIP_USE_SUDO=0

kip_ui_init
trap 'kip_unexpected_error "$?" "$LINENO"' ERR

kip_validate_boolean VERBOSE "$KIP_VERBOSE"
kip_validate_boolean GPU_REQUIRED "$KIP_GPU_REQUIRED"

kip_logo
kip_heading "Host Preparation · v0.1"
kip_reset_results 4

if kip_detect_os && kip_supported_os && kip_supported_architecture; then
    kip_record PASS "Supported platform" "${KIP_OS_NAME}; x86_64"
else
    detected_platform="${KIP_OS_NAME:-unknown OS} / $(kip_exec uname -m 2>/dev/null || printf unknown)"
    kip_record FAIL "Supported platform" \
        "Ubuntu 22.04/24.04 x86_64 required; detected $detected_platform."
    kip_summary "Host preparation"
    exit 1
fi

if [ "$EUID" -eq 0 ]; then
    kip_record PASS "Administrative access" "Running as root"
elif ! kip_has_command sudo; then
    kip_record FAIL "Administrative access" "sudo is required for a non-root user."
    kip_summary "Host preparation"
    exit 1
elif kip_exec sudo -v; then
    KIP_USE_SUDO=1
    kip_record PASS "Administrative access" "sudo credentials acquired"
else
    kip_record FAIL "Administrative access" "Unable to acquire sudo credentials."
    kip_summary "Host preparation"
    exit 1
fi

kip_run_privileged() {
    if [ "$KIP_USE_SUDO" = "1" ]; then
        kip_exec sudo "$@"
    else
        kip_exec "$@"
    fi
}

missing_packages=()
for package_name in "${KIP_REQUIRED_PACKAGES[@]}"; do
    if ! kip_exec dpkg-query -W -f='${Status}' "$package_name" 2>/dev/null \
        | command grep -q '^install ok installed$'; then
        missing_packages+=("$package_name")
    fi
done

if [ "${#missing_packages[@]}" -eq 0 ]; then
    kip_record SKIP "Inspection packages" "All required packages are already installed."
else
    kip_note "Installing missing packages: ${missing_packages[*]}"
    if kip_run_privileged apt-get update \
        && kip_run_privileged env DEBIAN_FRONTEND=noninteractive \
            apt-get install -y --no-install-recommends "${missing_packages[@]}"; then
        kip_record PASS "Inspection packages" "Installed: ${missing_packages[*]}"
    else
        kip_record FAIL "Inspection packages" "Package installation failed."
        kip_summary "Host preparation"
        exit 1
    fi
fi

if KIP_NO_LOGO=1 "$KIP_HOST_ROOT/validate.sh"; then
    kip_record PASS "Final host validation" "Required checks passed."
else
    kip_record FAIL "Final host validation" "One or more required checks failed."
    kip_summary "Host preparation"
    exit 1
fi

kip_summary "Host preparation"
printf '\n%sNext milestone:%s implement and run the single-node Kubernetes bootstrap.\n' \
    "$KIP_COLOR_CYAN" "$KIP_COLOR_RESET"
