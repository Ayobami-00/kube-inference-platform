#!/usr/bin/env bash
set -Eeuo pipefail
export LANG=C
export LC_ALL=C

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

# shellcheck source=../deploy/cluster/versions.env
source "$REPOSITORY_ROOT/deploy/cluster/versions.env"
# shellcheck source=../deploy/observability/versions.env
source "$REPOSITORY_ROOT/deploy/observability/versions.env"
# shellcheck source=artifact-validator-checksums.env
source "$SCRIPT_DIR/artifact-validator-checksums.env"

CACHE_ROOT=${KIP_ARTIFACT_VALIDATOR_CACHE:-$REPOSITORY_ROOT/.cache/artifact-validators}
BIN_DIR=$CACHE_ROOT/bin
CHART_DIR=$CACHE_ROOT/charts
PROMTOOL_PATH=$BIN_DIR/promtool
HELM_PATH=$BIN_DIR/helm
MONITORING_ARCHIVE=$CHART_DIR/kube-prometheus-stack.tgz
OPENCOST_ARCHIVE=$CHART_DIR/opencost.tgz

fail() {
    printf 'FAIL %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "$1 is required to prepare artifact validators"
}

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        fail "sha256sum or shasum is required"
    fi
}

require_sha256() {
    local actual path=$1 expected=$2
    actual=$(sha256_file "$path")
    [ "$actual" = "$expected" ] \
        || fail "checksum mismatch for $(basename -- "$path"): expected $expected, got $actual"
}

normalize_platform() {
    local machine system
    system=$(uname -s)
    machine=$(uname -m)
    case "$system" in
        Darwin) VALIDATOR_OS=darwin ;;
        Linux) VALIDATOR_OS=linux ;;
        *) fail "artifact-validator setup supports only Darwin and Linux (found $system)" ;;
    esac
    case "$machine" in
        x86_64 | amd64) VALIDATOR_ARCH=amd64 ;;
        arm64 | aarch64) VALIDATOR_ARCH=arm64 ;;
        *) fail "artifact-validator setup supports only amd64 and arm64 (found $machine)" ;;
    esac
}

select_checksums() {
    case "$VALIDATOR_OS-$VALIDATOR_ARCH" in
        darwin-amd64)
            PROMETHEUS_ARCHIVE_SHA256=$KIP_VALIDATOR_PROMETHEUS_DARWIN_AMD64_SHA256
            HELM_ARCHIVE_SHA256=$KIP_VALIDATOR_HELM_DARWIN_AMD64_SHA256
            ;;
        darwin-arm64)
            PROMETHEUS_ARCHIVE_SHA256=$KIP_VALIDATOR_PROMETHEUS_DARWIN_ARM64_SHA256
            HELM_ARCHIVE_SHA256=$KIP_VALIDATOR_HELM_DARWIN_ARM64_SHA256
            ;;
        linux-amd64)
            PROMETHEUS_ARCHIVE_SHA256=$KIP_VALIDATOR_PROMETHEUS_LINUX_AMD64_SHA256
            HELM_ARCHIVE_SHA256=$KIP_VALIDATOR_HELM_LINUX_AMD64_SHA256
            ;;
        linux-arm64)
            PROMETHEUS_ARCHIVE_SHA256=$KIP_VALIDATOR_PROMETHEUS_LINUX_ARM64_SHA256
            HELM_ARCHIVE_SHA256=$KIP_VALIDATOR_HELM_LINUX_ARM64_SHA256
            ;;
        *) fail "no validator checksums exist for $VALIDATOR_OS-$VALIDATOR_ARCH" ;;
    esac
}

prometheus_version=${KIP_PROMETHEUS_VERSION#v}
prometheus_version=${prometheus_version%-distroless}

cache_is_exact() {
    local helm_version promtool_version
    [ -x "$PROMTOOL_PATH" ] && [ -x "$HELM_PATH" ] \
        && [ -f "$MONITORING_ARCHIVE" ] && [ -f "$OPENCOST_ARCHIVE" ] || return 1
    promtool_version=$("$PROMTOOL_PATH" --version 2>&1 | sed -n '1s/.*version \([^ ]*\).*/\1/p')
    helm_version=$("$HELM_PATH" version --template '{{.Version}}' 2>/dev/null)
    [ "$promtool_version" = "$prometheus_version" ] \
        && [ "$helm_version" = "v$KIP_HELM_VERSION" ] \
        && [ "$(sha256_file "$MONITORING_ARCHIVE")" = "$KIP_VALIDATOR_KUBE_PROMETHEUS_STACK_ARCHIVE_SHA256" ] \
        && [ "$(sha256_file "$OPENCOST_ARCHIVE")" = "$KIP_VALIDATOR_OPENCOST_ARCHIVE_SHA256" ]
}

normalize_platform
select_checksums

if cache_is_exact; then
    printf 'PASS artifact validators already cached at %s\n' "$CACHE_ROOT"
    exit 0
fi

require_command curl
require_command tar
require_command awk
require_command mktemp

mkdir -p "$CACHE_ROOT"
temporary_directory=$(mktemp -d "$CACHE_ROOT/.prepare.XXXXXX")
cleanup() {
    rm -rf -- "$temporary_directory"
}
trap cleanup EXIT HUP INT TERM

prometheus_archive=prometheus-$prometheus_version.$VALIDATOR_OS-$VALIDATOR_ARCH.tar.gz
helm_archive=helm-v$KIP_HELM_VERSION-$VALIDATOR_OS-$VALIDATOR_ARCH.tar.gz

curl --fail --location --proto '=https' --tlsv1.2 --silent --show-error \
    --output "$temporary_directory/$prometheus_archive" \
    "https://github.com/prometheus/prometheus/releases/download/v$prometheus_version/$prometheus_archive"
require_sha256 "$temporary_directory/$prometheus_archive" "$PROMETHEUS_ARCHIVE_SHA256"
tar -xzf "$temporary_directory/$prometheus_archive" -C "$temporary_directory" \
    "prometheus-$prometheus_version.$VALIDATOR_OS-$VALIDATOR_ARCH/promtool"

curl --fail --location --proto '=https' --tlsv1.2 --silent --show-error \
    --output "$temporary_directory/$helm_archive" \
    "https://get.helm.sh/$helm_archive"
require_sha256 "$temporary_directory/$helm_archive" "$HELM_ARCHIVE_SHA256"
tar -xzf "$temporary_directory/$helm_archive" -C "$temporary_directory" \
    "$VALIDATOR_OS-$VALIDATOR_ARCH/helm"

temporary_helm=$temporary_directory/$VALIDATOR_OS-$VALIDATOR_ARCH/helm
mkdir -p "$temporary_directory/charts"
"$temporary_helm" pull \
    "$KIP_KUBE_PROMETHEUS_STACK_CHART@$KIP_KUBE_PROMETHEUS_STACK_DIGEST" \
    --destination "$temporary_directory/charts"
"$temporary_helm" pull "$KIP_OPENCOST_CHART@$KIP_OPENCOST_CHART_DIGEST" \
    --destination "$temporary_directory/charts"

downloaded_monitoring=$temporary_directory/charts/kube-prometheus-stack@${KIP_KUBE_PROMETHEUS_STACK_DIGEST/:/-}.tgz
downloaded_opencost=$temporary_directory/charts/opencost@${KIP_OPENCOST_CHART_DIGEST/:/-}.tgz
require_sha256 "$downloaded_monitoring" "$KIP_VALIDATOR_KUBE_PROMETHEUS_STACK_ARCHIVE_SHA256"
require_sha256 "$downloaded_opencost" "$KIP_VALIDATOR_OPENCOST_ARCHIVE_SHA256"

mkdir -p "$BIN_DIR" "$CHART_DIR"
install -m 0755 \
    "$temporary_directory/prometheus-$prometheus_version.$VALIDATOR_OS-$VALIDATOR_ARCH/promtool" \
    "$PROMTOOL_PATH"
install -m 0755 "$temporary_helm" "$HELM_PATH"
install -m 0644 "$downloaded_monitoring" "$MONITORING_ARCHIVE"
install -m 0644 "$downloaded_opencost" "$OPENCOST_ARCHIVE"

cache_is_exact || fail "prepared artifact-validator cache did not pass its integrity check"
printf 'PASS cached pinned promtool, Helm, and chart archives at %s\n' "$CACHE_ROOT"
