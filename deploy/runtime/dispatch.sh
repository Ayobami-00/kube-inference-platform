#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
KIP_DEPLOY_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
readonly KIP_DEPLOY_ROOT

# shellcheck source=deploy/lib/ui.sh
source "$KIP_DEPLOY_ROOT/lib/ui.sh"

KIP_RUNTIME=${KIP_RUNTIME:-vllm}
KIP_MODEL_PROFILE=${KIP_MODEL_PROFILE:-qwen2.5-7b-instruct}

kip_ui_init

[ "$#" -eq 1 ] || kip_usage_error "runtime dispatcher requires exactly one action: deploy, check, or smoke."
case "$1" in
    deploy) entrypoint=deploy.sh ;;
    check) entrypoint=validate.sh ;;
    smoke) entrypoint=smoke.sh ;;
    *) kip_usage_error "runtime action must be deploy, check, or smoke; received '$1'." ;;
esac

case "$KIP_RUNTIME" in
    vllm) ;;
    *) kip_usage_error "RUNTIME must identify a committed adapter; received '$KIP_RUNTIME'." ;;
esac
case "$KIP_MODEL_PROFILE" in
    qwen2.5-7b-instruct) ;;
    *) kip_usage_error "MODEL_PROFILE must identify a committed profile; received '$KIP_MODEL_PROFILE'." ;;
esac

profile_path="$SCRIPT_DIR/$KIP_RUNTIME/profiles/$KIP_MODEL_PROFILE.json"
[ -r "$profile_path" ] \
    || kip_usage_error "runtime profile is not readable: $KIP_RUNTIME/$KIP_MODEL_PROFILE."

export KIP_RUNTIME KIP_MODEL_PROFILE
exec "$SCRIPT_DIR/$KIP_RUNTIME/$entrypoint"
