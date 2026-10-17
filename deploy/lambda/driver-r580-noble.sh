#!/usr/bin/env bash
# Install only the reviewed Ubuntu 24.04 R580 server-open driver on a fresh Lambda VM.
set -Eeuo pipefail

die() { printf 'Lambda GPU driver preflight: %s\n' "$*" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || die 'run as root or with sudo'
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID} == ubuntu && ${VERSION_ID} == 24.04 ]] || die 'Ubuntu 24.04 is required'
[[ $(uname -m) == x86_64 ]] || die 'x86-64 is required'
[[ $(ps -p 1 -o comm= | tr -d ' ') == systemd ]] || die 'systemd is required'
[[ ! -e /etc/kubernetes/admin.conf && ! -e /etc/kubernetes/kubelet.conf ]] \
  || die 'install the driver before creating or joining Kubernetes'
command -v lspci >/dev/null || die 'pciutils/lspci is required'
lspci -nn | grep -i NVIDIA | grep -Eiq '3D controller|VGA compatible controller' \
  || die 'no NVIDIA GPU is visible on PCI'
dpkg-query -W "linux-headers-$(uname -r)" >/dev/null 2>&1 \
  || die 'matching kernel headers are not installed'

readonly version=580.178.04
readonly release="${version}-0ubuntu0.24.04.1"
readonly packages=(
  "nvidia-headless-580-server-open=${release}"
  "nvidia-utils-580-server=${release}"
  "nvidia-compute-utils-580-server=${release}"
)
if command -v nvidia-smi >/dev/null 2>&1; then
  running=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | sort -u) \
    || die 'an existing NVIDIA driver is unhealthy; refusing replacement'
  [[ ${running} == "${version}" ]] || die "unrelated driver ${running} is already installed"
  dpkg-query -W -f='${Version}' nvidia-headless-580-server-open \
    | grep -Fxq "${release}" || die 'running driver package is not the reviewed release'
  printf 'PASS: pinned NVIDIA server-open %s is healthy\n' "${version}"
  exit 0
fi
if dpkg-query -W -f='${Status}' 'nvidia-driver-*' 2>/dev/null \
  | grep -q 'install ok installed'; then
  die 'an existing NVIDIA driver package is present but nvidia-smi is unavailable'
fi

policy=$(apt-cache policy nvidia-headless-580-server-open)
if ! grep -Fq "${release}" <<<"${policy}"; then
  apt-get update -qq || die 'apt index refresh failed'
fi
for package in "${packages[@]}"; do
  name=${package%%=*}
  policy=$(apt-cache policy "${name}")
  grep -Fq "${release}" <<<"${policy}" \
    || die "the reviewed package is unavailable: ${name}"
done
simulation=$(apt-get -s --no-install-recommends install "${packages[@]}") \
  || die 'apt simulation failed'
grep -Eq '[0-9]+ upgraded, [0-9]+ newly installed, 0 to remove' <<<"${simulation}" \
  || die 'apt proposes removals or an unexpected transaction'
mapfile -t upgrades < <(awk '$1=="Inst" && $3 ~ /^\[/ {print $2}' <<<"${simulation}")
for package in "${upgrades[@]}"; do
  case ${package} in
    dpkg|gcc-14-base|libstdc++6|libgcc-s1|libbz2-1.0) ;;
    *) die "apt would upgrade an unrelated package: ${package}" ;;
  esac
done
printf 'Installing pinned NVIDIA headless server-open %s; %s reviewed dependency upgrades.\n' \
  "${version}" "${#upgrades[@]}"
DEBIAN_FRONTEND=noninteractive apt-get -y --no-install-recommends install "${packages[@]}"
dpkg-query -W -f='${Version}' nvidia-headless-580-server-open \
  | grep -Fxq "${release}" || die 'the reviewed driver package did not finish installing'
printf 'PASS: driver package installed; verify nvidia-smi after module load or reboot.\n'
