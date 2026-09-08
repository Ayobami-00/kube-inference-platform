#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
KIP_HOST_ROOT=$SCRIPT_DIR
readonly KIP_HOST_ROOT
KIP_DEPLOY_ROOT=$(cd "$KIP_HOST_ROOT/.." && pwd)
readonly KIP_DEPLOY_ROOT

# shellcheck source=deploy/lib/ui.sh
source "$KIP_DEPLOY_ROOT/lib/ui.sh"
# shellcheck source=deploy/host/lib/checks.sh
source "$KIP_HOST_ROOT/lib/checks.sh"

export LC_ALL=C
KIP_VERBOSE=${KIP_VERBOSE:-0}
KIP_GPU_REQUIRED=${KIP_GPU_REQUIRED:-0}
KIP_OS_RELEASE_FILE=${KIP_OS_RELEASE_FILE:-/etc/os-release}

kip_ui_init
trap 'kip_unexpected_error "$?" "$LINENO"' ERR

kip_validate_boolean VERBOSE "$KIP_VERBOSE"
kip_validate_boolean GPU_REQUIRED "$KIP_GPU_REQUIRED"

kip_logo
kip_heading "Host Validation · v0.1"

if kip_run_host_checks; then
    exit 0
fi

exit 1
