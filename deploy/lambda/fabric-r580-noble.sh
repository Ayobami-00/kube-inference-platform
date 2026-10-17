#!/usr/bin/env bash
# Exact, idempotent Fabric Manager setup for the reviewed eight-A100 NVSwitch VM.
set -Eeuo pipefail

readonly PACKAGE=nvidia-fabricmanager-580
readonly VERSION=580.178.04-0ubuntu0.24.04.1

die() { printf 'Lambda A100 fabric: %s\n' "$*" >&2; exit 1; }
[ "$#" -eq 0 ] || { printf '%s\n' 'fabric setup accepts no arguments' >&2; exit 2; }
for tool in apt-get apt-cache dpkg-query nvidia-smi systemctl lsb_release; do
    command -v "$tool" >/dev/null || die "missing $tool"
done
[ "$(lsb_release -is)" = Ubuntu ] && [ "$(lsb_release -rs)" = 24.04 ] \
    && [ "$(uname -m)" = x86_64 ] || die 'requires Ubuntu 24.04 x86-64'
[ "$(nvidia-smi --query-gpu=name --format=csv,noheader | grep -c 'A100-SXM4-80GB')" -eq 8 ] \
    || die 'requires exactly eight NVIDIA A100 SXM4 80-GB GPUs'
driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | sort -u)
[ "$driver" = 580.178.04 ] || die 'running GPU driver is not the reviewed R580 build'
candidate=$(apt-cache policy "$PACKAGE" | awk '/Candidate:/ {print $2}')
[ "$candidate" = "$VERSION" ] || die 'exact Fabric Manager build is unavailable'
installed=$(dpkg-query -W -f='${Version}' "$PACKAGE" 2>/dev/null || true)
if [ -n "$installed" ] && [ "$installed" != "$VERSION" ]; then
    die 'an unrelated Fabric Manager version is installed'
fi
if [ -z "$installed" ]; then
    simulation=$(apt-get -s install "$PACKAGE=$VERSION") || die 'apt simulation failed'
    changes=$(printf '%s\n' "$simulation" | awk '/^(Inst|Remv|Purg)/ {print $1, $2}')
    [ "$changes" = "Inst $PACKAGE" ] \
        || die 'apt would change packages beyond the exact Fabric Manager package'
    if [ "$(id -u)" -eq 0 ]; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            "$PACKAGE=$VERSION" || die 'Fabric Manager installation failed'
    else
        sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y \
            --no-install-recommends "$PACKAGE=$VERSION" \
            || die 'Fabric Manager installation failed'
    fi
fi
if [ "$(id -u)" -eq 0 ]; then
    systemctl enable --now nvidia-fabricmanager || die 'service did not start'
else
    sudo -n systemctl enable --now nvidia-fabricmanager || die 'service did not start'
fi
systemctl is-active --quiet nvidia-fabricmanager || die 'service is not active'
topology=$(nvidia-smi topo -m) || die 'GPU topology query failed'
printf '%s\n' "$topology" | awk '
    /^GPU[0-7][[:space:]]/ {
        for (i=2; i<=9; i++) if ($i ~ /^NV[0-9]+$/) links++
        rows++
    }
    END {exit !(rows == 8 && links >= 8)}
' || die 'NVLink fabric is not initialized'
printf 'PASS: exact R580 Fabric Manager active; eight A100 GPUs expose NVLink topology.\n'
