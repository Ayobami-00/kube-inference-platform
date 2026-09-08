#!/usr/bin/env bash

# Shared terminal output for deployment setup and validation stages.

KIP_COLOR_ENABLED=0
KIP_COLOR_RESET=""
KIP_COLOR_BLUE=""
KIP_COLOR_GREEN=""
KIP_COLOR_YELLOW=""
KIP_COLOR_RED=""
KIP_COLOR_CYAN=""
KIP_CURRENT_CHECK=0
KIP_TOTAL_CHECKS=0
KIP_PASSED=0
KIP_WARNED=0
KIP_FAILED=0
KIP_SKIPPED=0
KIP_DEFERRED=0

kip_ui_init() {
    if [ -z "${NO_COLOR+x}" ] && [ "${TERM:-}" != "dumb" ]; then
        if [ "${KIP_FORCE_COLOR:-0}" = "1" ] || [ -t 1 ]; then
            KIP_COLOR_ENABLED=1
            KIP_COLOR_RESET=$(printf '\033[0m')
            KIP_COLOR_BLUE=$(printf '\033[1;34m')
            KIP_COLOR_GREEN=$(printf '\033[1;32m')
            KIP_COLOR_YELLOW=$(printf '\033[1;33m')
            KIP_COLOR_RED=$(printf '\033[1;31m')
            KIP_COLOR_CYAN=$(printf '\033[1;36m')
        fi
    fi
}

kip_logo() {
    if [ "${KIP_NO_LOGO:-0}" = "1" ]; then
        return
    fi

    printf '%s' "$KIP_COLOR_CYAN"
    if [ -r "$KIP_DEPLOY_ROOT/assets/logo.txt" ]; then
        command cat "$KIP_DEPLOY_ROOT/assets/logo.txt"
    else
        printf '%s\n' "KUBE INFERENCE PLATFORM"
    fi
    printf '%s\n' "$KIP_COLOR_RESET"
}

kip_heading() {
    printf '%s%s%s\n\n' "$KIP_COLOR_BLUE" "$1" "$KIP_COLOR_RESET"
}

kip_note() {
    printf '%s\n' "$1"
}

kip_reset_results() {
    KIP_CURRENT_CHECK=0
    KIP_TOTAL_CHECKS=$1
    KIP_PASSED=0
    KIP_WARNED=0
    KIP_FAILED=0
    KIP_SKIPPED=0
    KIP_DEFERRED=0
}

kip_status_color() {
    case "$1" in
        PASS) printf '%s' "$KIP_COLOR_GREEN" ;;
        WARN | DEFERRED) printf '%s' "$KIP_COLOR_YELLOW" ;;
        FAIL) printf '%s' "$KIP_COLOR_RED" ;;
        SKIP) printf '%s' "$KIP_COLOR_BLUE" ;;
        *) printf '%s' "$KIP_COLOR_RESET" ;;
    esac
}

kip_record() {
    local status=$1
    local label=$2
    local detail=$3
    local color

    KIP_CURRENT_CHECK=$((KIP_CURRENT_CHECK + 1))
    color=$(kip_status_color "$status")
    printf '[%02d/%02d] %-34s %s%-8s%s %s\n' \
        "$KIP_CURRENT_CHECK" \
        "$KIP_TOTAL_CHECKS" \
        "$label" \
        "$color" \
        "$status" \
        "$KIP_COLOR_RESET" \
        "$detail"

    case "$status" in
        PASS) KIP_PASSED=$((KIP_PASSED + 1)) ;;
        WARN) KIP_WARNED=$((KIP_WARNED + 1)) ;;
        FAIL) KIP_FAILED=$((KIP_FAILED + 1)) ;;
        SKIP) KIP_SKIPPED=$((KIP_SKIPPED + 1)) ;;
        DEFERRED) KIP_DEFERRED=$((KIP_DEFERRED + 1)) ;;
    esac
}

kip_verbose_output() {
    local label=$1
    local output=$2
    local line

    if [ "${KIP_VERBOSE:-0}" != "1" ]; then
        return
    fi

    printf '        --- %s ---\n' "$label"
    while IFS= read -r line; do
        printf '        %s\n' "$line"
    done <<< "$output"
}

kip_summary() {
    local stage=${1:-Validation}

    printf '\n%s completed: ' "$stage"
    printf '%s%d passed%s, ' "$KIP_COLOR_GREEN" "$KIP_PASSED" "$KIP_COLOR_RESET"
    printf '%s%d warned%s, ' "$KIP_COLOR_YELLOW" "$KIP_WARNED" "$KIP_COLOR_RESET"
    printf '%s%d failed%s, ' "$KIP_COLOR_RED" "$KIP_FAILED" "$KIP_COLOR_RESET"
    printf '%d skipped, ' "$KIP_SKIPPED"
    printf '%s%d deferred%s\n' "$KIP_COLOR_YELLOW" "$KIP_DEFERRED" "$KIP_COLOR_RESET"
}

kip_usage_error() {
    printf '%sERROR:%s %s\n' "$KIP_COLOR_RED" "$KIP_COLOR_RESET" "$1" >&2
    exit 2
}

kip_unexpected_error() {
    local exit_code=$1
    local line=$2

    printf '\n%sFAIL:%s unexpected error at line %s (exit %s)\n' \
        "$KIP_COLOR_RED" "$KIP_COLOR_RESET" "$line" "$exit_code" >&2
    exit "$exit_code"
}
