#!/usr/bin/env bash

# Common execution, privilege, and filesystem helpers for the cluster slice.

KIP_ETC_ROOT=${KIP_ETC_ROOT:-/etc}
KIP_RUN_ROOT=${KIP_RUN_ROOT:-/run}
KIP_VAR_ROOT=${KIP_VAR_ROOT:-/var}
KIP_USR_LOCAL_BIN=${KIP_USR_LOCAL_BIN:-/usr/local/bin}
KIP_OS_RELEASE_FILE=${KIP_OS_RELEASE_FILE:-$KIP_ETC_ROOT/os-release}
KIP_USE_SUDO=0

kip_cluster_validate_boolean() {
    case "$2" in
        0 | 1) return 0 ;;
        *) kip_usage_error "$1 must be 0 or 1; received '$2'." ;;
    esac
}

kip_has_command() {
    local command_name=$1

    if [ "${KIP_TESTING:-0}" = "1" ] && [ -x "${KIP_TEST_COMMAND_DIR:?}/$command_name" ]; then
        return 0
    fi
    command -v "$command_name" >/dev/null 2>&1
}

kip_exec() {
    local command_name=$1
    local exit_code
    shift

    if [ "${KIP_TESTING:-0}" = "1" ]; then
        if [ -x "${KIP_TEST_COMMAND_DIR:?}/$command_name" ]; then
            "${KIP_TEST_COMMAND_DIR}/$command_name" "$@" || {
                exit_code=$?
                return "$exit_code"
            }
            return
        fi

        case "$command_name" in
            awk | basename | chmod | cp | cut | dirname | grep | head | install | mkdir | \
                mktemp | mv | rm | sed | sort | tail | tr | wc)
                command "$command_name" "$@"
                return
                ;;
            *) return 127 ;;
        esac
    fi

    command "$command_name" "$@" || {
        exit_code=$?
        return "$exit_code"
    }
}

kip_effective_uid() {
    printf '%s' "${KIP_TEST_EUID:-$EUID}"
}

kip_acquire_privileges() {
    if [ "$(kip_effective_uid)" -eq 0 ]; then
        KIP_USE_SUDO=0
        return 0
    fi
    if ! kip_has_command sudo || ! kip_exec sudo -v; then
        return 1
    fi
    KIP_USE_SUDO=1
}

kip_run_privileged() {
    if [ "$KIP_USE_SUDO" = "1" ]; then
        kip_exec sudo "$@"
    else
        kip_exec "$@"
    fi
}

kip_make_directory() {
    local path=$1
    local mode=${2:-0755}

    kip_run_privileged mkdir -p "$path" && kip_run_privileged chmod "$mode" "$path"
}

kip_install_file() {
    local source_file=$1
    local destination=$2
    local mode=${3:-0644}
    local destination_dir

    destination_dir=$(dirname "$destination")
    kip_make_directory "$destination_dir" 0755
    kip_run_privileged install -m "$mode" "$source_file" "$destination"
}

kip_backup_once() {
    local source_file=$1
    local backup_file=$2

    if [ -e "$source_file" ] && [ ! -e "$backup_file" ]; then
        kip_run_privileged cp -p "$source_file" "$backup_file"
    fi
}

kip_package_installed() {
    local package_name=$1
    local expected_version=${2:-}
    local installed_version

    installed_version=$(kip_package_version "$package_name") || return 1
    if [ -n "$expected_version" ]; then
        [ "$installed_version" = "$expected_version" ]
    else
        [ -n "$installed_version" ]
    fi
}

kip_package_version() {
    local output package_name=$1

    output=$(kip_exec dpkg-query -W -f='${Status}|${Version}' "$package_name" 2>/dev/null) \
        || return 1
    case "$output" in
        'install ok installed|'*) printf '%s' "${output#*|}" ;;
        *) return 1 ;;
    esac
}

kip_verify_sha256() {
    local file_path=$1
    local expected=$2
    local actual

    actual=$(kip_exec sha256sum "$file_path" 2>/dev/null | command awk '{print $1}') || return 1
    [ "$actual" = "$expected" ]
}

kip_trim() {
    printf '%s' "$1" | command awk '{$1=$1; print}'
}
