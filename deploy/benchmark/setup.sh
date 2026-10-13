#!/usr/bin/env bash
# Install only the pinned uv binary needed for target-local AIPerf orchestration.
set -Eeuo pipefail

readonly uv_version=0.11.31
readonly uv_sha256=8cc1cd82d434ec565376f98bd938d4b715b5791a80ff2d3aa78821cf85091b4b
readonly uv_archive=uv-x86_64-unknown-linux-gnu.tar.gz
readonly uv_url="https://releases.astral.sh/github/uv/releases/download/${uv_version}/${uv_archive}"

die() { printf 'benchmark setup: %s\n' "$*" >&2; exit 1; }

if command -v uv >/dev/null 2>&1; then
  [[ $(uv --version) == "uv ${uv_version}"* ]] \
    || die "existing uv version differs from pinned ${uv_version}; refusing replacement"
  printf 'PASS: pinned uv %s is already available.\n' "${uv_version}"
  exit 0
fi

[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] \
  || die 'automatic uv installation supports only Linux x86_64'
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID} == ubuntu && ( ${VERSION_ID} == 22.04 || ${VERSION_ID} == 24.04 ) ]] \
  || die 'automatic uv installation supports only Ubuntu 22.04/24.04'
for required in curl sha256sum tar mktemp install; do
  command -v "${required}" >/dev/null || die "missing prerequisite ${required}"
done
if [[ ${EUID} -ne 0 ]]; then
  command -v sudo >/dev/null || die 'root or passwordless sudo is required'
  sudo -n true || die 'passwordless sudo is unavailable'
fi

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/kip-uv.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT
curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
  --retry 3 "${uv_url}" --output "${work_dir}/${uv_archive}" \
  || die 'official pinned uv archive download failed'
actual_sha256=$(sha256sum "${work_dir}/${uv_archive}" | awk '{print $1}')
[[ ${actual_sha256} == "${uv_sha256}" ]] || die 'pinned uv archive checksum mismatch'
tar -xzf "${work_dir}/${uv_archive}" -C "${work_dir}" \
  || die 'verified uv archive extraction failed'
binary="${work_dir}/uv-x86_64-unknown-linux-gnu/uv"
[[ -f ${binary} && ! -L ${binary} ]] || die 'verified archive lacks a regular uv binary'

if [[ ${EUID} -eq 0 ]]; then
  install -m 0755 "${binary}" /usr/local/bin/uv
else
  sudo -n install -m 0755 "${binary}" /usr/local/bin/uv
fi
[[ $(uv --version) == "uv ${uv_version}"* ]] || die 'installed uv version check failed'
printf 'PASS: checksum-verified uv %s installed for benchmark orchestration.\n' "${uv_version}"
