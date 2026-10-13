#!/usr/bin/env bash
# Test the R580 branch required by GPU Operator 26.7's CUDA validator.
# The separately pinned vLLM 0.30 CUDA 13.0 image has passed a GPU tensor smoke.
# The llm-d/Gateway request path still requires full target validation.
set -Eeuo pipefail

readonly target_version=580.178.04
readonly target_release="${target_version}-1ubuntu1"
readonly target_packages=(
  "nvidia-driver-580=${target_release}"
  "nvidia-utils-580=${target_release}"
  "nvidia-compute-utils-580=${target_release}"
  "nvidia-modprobe=${target_release}"
  "nvidia-persistenced=${target_release}"
)

die() { printf 'pilot driver preparation: %s\n' "$*" >&2; exit 1; }

[[ ${EUID} -eq 0 ]] || die 'run as root on the rented VM guest'
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID} == ubuntu && ${VERSION_ID} == 22.04 ]] || die 'only Ubuntu 22.04 is validated'
[[ $(ps -p 1 -o comm= | tr -d ' ') == systemd ]] || die 'not a systemd VM guest'
[[ $(uname -m) == x86_64 ]] || die 'only x86_64 is validated'
command -v nvidia-smi >/dev/null || die 'GPU passthrough / nvidia-smi is unavailable'

current_version=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | sort -u)
if [[ ${current_version} == "${target_version}" ]]; then
  dpkg-query -W -f='${Status}' nvidia-driver-580 2>/dev/null | grep -qx 'install ok installed' \
    || die 'R580 driver is running but package ownership is not verified'
  printf 'PASS: NVIDIA %s already installed; no change\n' "${target_version}"
  exit 0
fi
[[ ! -e /etc/kubernetes/admin.conf ]] \
  || die 'change the host driver before Kubernetes bootstrap, not on an active cluster'
case ${current_version} in
  575.57.08)
    dpkg-query -W -f='${Status}' nvidia-driver-575 2>/dev/null \
      | grep -qx 'install ok installed' || die 'preinstalled 575 package ownership differs'
    ;;
  580.95.05)
    dpkg-query -W -f='${Status}' nvidia-driver-580-server-open 2>/dev/null \
      | grep -qx 'install ok installed' || die 'provider R580 package ownership differs'
    ;;
  *) die "unexpected driver ${current_version}; refusing replacement" ;;
esac

for package in "${target_packages[@]}"; do
  name=${package%%=*}
  available_versions=$(apt-cache policy "${name}")
  grep -Fq "${target_release}" <<<"${available_versions}" \
    || die "pinned NVIDIA package unavailable: ${name}"
done

simulation=$(apt-get -s --no-install-recommends install "${target_packages[@]}") \
  || die 'apt simulation failed'
while read -r marker package _; do
  case ${marker} in
    Remv|Inst)
      case ${package} in
        nvidia-*|libnvidia-*|xserver-xorg-video-nvidia-*) ;;
        *) die "apt would change unrelated package ${package}" ;;
      esac
      ;;
  esac
done <<<"${simulation}"

if dpkg-query -W -f='${Status}' libnvidia-extra-575 2>/dev/null \
  | grep -qx 'install ok installed'; then
  # This residual package owns nvidia-drm_gbm.so even though apt's simulation
  # does not show the conflict with libnvidia-gl-580.
  dpkg --no-act --remove libnvidia-extra-575 >/dev/null \
    || die 'stale NVIDIA 575 extra package cannot be safely removed'
  dpkg --remove libnvidia-extra-575 \
    || die 'could not remove the stale NVIDIA 575 extra package'
fi

printf 'Installing pinned NVIDIA %s; a reboot is required afterward.\n' "${target_version}"
DEBIAN_FRONTEND=noninteractive apt-get --no-install-recommends install -y "${target_packages[@]}"
dpkg-query -W -f='${Status}' nvidia-driver-580 \
  | grep -qx 'install ok installed' || die 'pinned package did not finish installing'
printf 'PASS: driver package installed. Reboot this VM, then verify nvidia-smi reports %s.\n' \
  "${target_version}"
