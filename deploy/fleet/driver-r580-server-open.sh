#!/usr/bin/env bash
# Same-flavor R580 upgrade for Vast Ubuntu guests already using server-open.
# Preserves the provider's package flavor and changes NVIDIA packages only.
set -Eeuo pipefail

readonly target_version=580.178.04
readonly target_release="${target_version}-0ubuntu0.22.04.1"
readonly packages=(
  "nvidia-driver-580-server-open=${target_release}"
  "nvidia-utils-580-server=${target_release}"
  "nvidia-compute-utils-580-server=${target_release}"
)

die() { printf 'pilot server-open driver preparation: %s\n' "$*" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || die 'run as root on the rented VM guest'
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID} == ubuntu && ${VERSION_ID} == 22.04 ]] || die 'only Ubuntu 22.04 is validated'
[[ $(ps -p 1 -o comm= | tr -d ' ') == systemd ]] || die 'not a systemd VM guest'
[[ $(uname -m) == x86_64 ]] || die 'only x86_64 is validated'
[[ ! -e /etc/kubernetes/admin.conf && ! -e /etc/kubernetes/kubelet.conf ]] \
  || die 'change the driver before joining Kubernetes, not on an active node'
command -v nvidia-smi >/dev/null || die 'GPU passthrough / nvidia-smi is unavailable'
dpkg-query -W -f='${Status}' nvidia-driver-580-server-open 2>/dev/null \
  | grep -qx 'install ok installed' || die 'provider server-open package ownership differs'

current_version=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | sort -u)
if [[ ${current_version} == "${target_version}" ]]; then
  installed=$(dpkg-query -W -f='${Version}' nvidia-driver-580-server-open)
  [[ ${installed} == "${target_release}" ]] \
    || die 'running R580 driver does not have the pinned server-open package release'
  printf 'PASS: NVIDIA server-open %s already installed; no change\n' "${target_version}"
  exit 0
fi
[[ ${current_version} == 580.95.05 ]] \
  || die "unexpected running driver ${current_version}; refusing replacement"

for package in "${packages[@]}"; do
  name=${package%%=*}
  available_versions=$(apt-cache policy "${name}")
  grep -Fq "${target_release}" <<<"${available_versions}" \
    || die "pinned NVIDIA package is unavailable: ${name}"
done

simulation=$(apt-get -s --no-install-recommends install "${packages[@]}") \
  || die 'apt simulation failed'
grep -Fq '0 to remove' <<<"${simulation}" \
  || die 'apt simulation would remove existing packages'
while read -r marker package remainder; do
  case ${marker} in
    Remv) die "apt would remove package ${package}" ;;
    Inst)
      case ${package} in
        nvidia-*|libnvidia-*|xserver-xorg-video-nvidia-*) ;;
        *) die "apt would change unrelated package ${package}" ;;
      esac
      [[ ${remainder} == *"${target_version}"* ]] \
        || die "apt would select another NVIDIA version for ${package}"
      ;;
  esac
done <<<"${simulation}"

printf 'Installing NVIDIA server-open %s; reboot required afterward.\n' "${target_version}"
DEBIAN_FRONTEND=noninteractive apt-get --no-install-recommends install -y "${packages[@]}"
installed=$(dpkg-query -W -f='${Version}' nvidia-driver-580-server-open)
[[ ${installed} == "${target_release}" ]] || die 'pinned package did not finish installing'
printf 'PASS: server-open driver package installed. Reboot and verify nvidia-smi reports %s.\n' \
  "${target_version}"
