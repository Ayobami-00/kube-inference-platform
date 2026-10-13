#!/usr/bin/env bash
# Fleet-only containerd override for multi-GB vLLM image layer extraction.
set -Eeuo pipefail

readonly config=/etc/containerd/config.toml
readonly override=/etc/containerd/conf.d/kip-fleet-large-images.toml
temp=""
created=0
cleanup() { [ -z "$temp" ] || rm -f -- "$temp"; }
trap cleanup EXIT
die() { printf 'fleet large-image pull: %s\n' "$*" >&2; exit 1; }

[ "$#" -eq 0 ] || { printf '%s\n' 'large-image-pull accepts no arguments.' >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || die 'run as root on a GPU worker'
case "$(hostname)" in kip-gpu-1 | kip-gpu-2) ;; *) die 'unreviewed GPU worker hostname' ;; esac
dpkg-query -W -f='${Version}' containerd.io | grep -q '^2\.3\.4-2' \
    || die 'containerd package differs from the pinned 2.3.4 release'
[ -f "$config" ] && [ ! -L "$config" ] \
    && grep -Fq 'imports = ["/etc/containerd/conf.d/*.toml"]' "$config" \
    || die 'KIP containerd import path is absent or unsafe'
[ -d /etc/containerd/conf.d ] && [ ! -L /etc/containerd/conf.d ] \
    || die 'KIP containerd import directory is absent or unsafe'
[ ! -L "$override" ] || die 'override is a symlink'
[ -z "$(crictl ps --name vllm -q 2>/dev/null)" ] \
    || die 'a vLLM container is running; schedule a controlled maintenance window'

temp=$(mktemp /tmp/kip-large-image-pull.XXXXXX)
cat >"$temp" <<'TOML'
# Managed by Kube Inference Platform fleet pilot.
# containerd 2.3 transfer service can count slow unpack as "no progress";
# retain a bounded timeout rather than disabling the guard entirely.
version = 3
[plugins.'io.containerd.cri.v1.images']
  image_pull_progress_timeout = "30m0s"
TOML
if [ -e "$override" ]; then
    cmp -s "$temp" "$override" || die 'existing pull timeout override differs from KIP ownership'
else
    install -m 0644 "$temp" "$override"
    created=1
fi
rendered=$(containerd --config "$config" config dump 2>/dev/null) \
    || die 'containerd config dump failed'
if [[ "$rendered" != *"image_pull_progress_timeout = '30m0s'"* ]]; then
    [ "$created" -eq 0 ] || rm -f -- "$override"
    die 'containerd did not import the bounded pull-progress override'
fi
override_time=$(stat -c %Y "$override")
active_time=$(date -d "$(systemctl show containerd -p ActiveEnterTimestamp --value)" +%s) \
    || active_time=0
if [ "$active_time" -ge "$override_time" ] && systemctl is-active --quiet containerd \
    && crictl info >/dev/null 2>&1; then
    printf '%s\n' 'PASS: owned large-image pull timeout already active'
    exit 0
fi
systemctl restart containerd || die 'containerd restart failed after owned override'
systemctl is-active --quiet containerd || die 'containerd did not become active'
crictl info >/dev/null || die 'CRI is not healthy after containerd restart'
printf '%s\n' 'PASS: containerd large-image pull timeout is 30m0s; CRI is healthy'
