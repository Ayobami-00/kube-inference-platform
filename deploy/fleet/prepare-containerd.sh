#!/usr/bin/env bash
# Remove an unused Vast image Docker stack before KIP pins containerd 2.3.4.
set -Eeuo pipefail

die() { printf 'pilot runtime preparation: %s\n' "$*" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || die 'run as root in the rented VM guest'
[[ $(ps -p 1 -o comm= | tr -d ' ') == systemd ]] || die 'not a systemd VM guest'

if ! dpkg-query -W -f='${Status}' docker-ce 2>/dev/null | grep -qx 'install ok installed'; then
  if dpkg-query -W -f='${Version}' containerd.io 2>/dev/null | grep -q '^2\.3\.4-2'; then
    printf 'PASS: Docker absent and KIP containerd already pinned\n'
    exit 0
  fi
  die 'Docker is absent but containerd is not the reviewed pinned version'
fi

for kind in 'ps -aq' 'images -q' 'volume ls -q'; do
  # Word splitting is deliberate for this fixed, reviewed command list.
  # shellcheck disable=SC2086
  [[ -z $(docker ${kind}) ]] || die "Docker ${kind} reports existing user state"
done
if [[ -n $(ctr -n k8s.io containers list -q 2>/dev/null) ]]; then
  die 'containerd k8s.io namespace already has containers'
fi
[[ $(dpkg-query -W -f='${Version}' containerd.io) == 1.7.27-1 ]] \
  || die 'preinstalled containerd version differs from the reviewed image'
available_versions=$(apt-cache policy containerd.io)
if ! grep -Fq '2.3.4-2~ubuntu.22.04~jammy' <<<"${available_versions}"; then
  # Fresh Vast images can carry stale apt indexes. Refresh metadata only;
  # never upgrade or remove anything until the exact replacement is visible.
  apt-get update -qq || die 'apt index refresh failed before Docker removal'
  available_versions=$(apt-cache policy containerd.io)
  grep -Fq '2.3.4-2~ubuntu.22.04~jammy' <<<"${available_versions}" \
    || die 'pinned replacement containerd package is unavailable'
fi

readonly packages=(
  docker-ce docker-ce-cli docker-ce-rootless-extras docker-buildx-plugin
  docker-compose-plugin containerd.io
)
simulation=$(apt-get -s remove "${packages[@]}") || die 'apt removal simulation failed'
grep -Fq '0 upgraded' <<<"${simulation}" || die 'apt wants unrelated upgrades'
while read -r marker package _; do
  [[ ${marker} == Remv ]] || continue
  case ${package} in
    docker-ce|docker-ce-cli|docker-ce-rootless-extras|docker-buildx-plugin|docker-compose-plugin|containerd.io) ;;
    *) die "apt would remove unrelated package ${package}" ;;
  esac
done <<<"${simulation}"

readonly config=/etc/containerd/config.toml
readonly backup=/etc/containerd/config.toml.kip-prebootstrap-backup
if [[ -f ${config} && ! -e ${backup} ]]; then
  # KIP refuses to overwrite an unrelated active config. Move the stock one
  # aside rather than deleting it; the bootstrap will render its owned file.
  mv -- "${config}" "${backup}"
elif [[ -e ${config} ]]; then
  die 'active containerd config and prior backup both exist; inspect manually'
fi
systemctl stop docker.socket docker.service containerd.service
DEBIAN_FRONTEND=noninteractive apt-get remove -y "${packages[@]}"
printf 'PASS: unused Docker stack removed; data directories and stock config retained.\n'
