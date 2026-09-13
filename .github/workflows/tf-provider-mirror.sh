#!/usr/bin/env bash
# tf-provider-mirror.sh — every provider version the lock files name, as the zip for one platform,
# in a local filesystem mirror.
#
# The lock files are written on macs, so their h1: checksums are the macs' own. init checks a zip
# from a filesystem mirror against the zh: checksums, which cover every platform, so a linux runner
# installs each version from one download and the lock files stay as they are. A zip no lock file
# names any more is removed, so a mirror restored from an older cache doesn't grow.
#
#   bash .github/workflows/tf-provider-mirror.sh <mirror dir> [platform]   # platform default linux_amd64
#
# terraform.yaml points init at it with a CLI config (filesystem_mirror, then direct).

set -euo pipefail

MIRROR="$1"
PLATFORM="${2:-linux_amd64}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# "registry.terraform.io/hashicorp/aws 6.63.0", once per provider version
mapfile -t pins < <(find "$REPO_ROOT" -name .terraform -prune -o -name .terraform.lock.hcl -type f -print0 |
  xargs -0 awk '/^provider "/ { gsub(/"/, "", $2); p = $2 } /^  version / { gsub(/"/, "", $3); print p, $3 }' |
  sort -u)

zip_of() {  # <addr> <version> → the packed-layout path filesystem_mirror reads
  echo "$MIRROR/$1/terraform-provider-${1##*/}_${2}_${PLATFORM}.zip"
}

mkdir -p "$MIRROR"
wanted="$(for pin in "${pins[@]}"; do zip_of $pin; done)"
while IFS= read -r -d '' zip; do
  grep -Fxq "$zip" <<<"$wanted" || { echo "remove ${zip#$MIRROR/}"; rm -f "$zip"; }
done < <(find "$MIRROR" -type f -print0)

for pin in "${pins[@]}"; do
  read -r addr version <<<"$pin"
  zip="$(zip_of "$addr" "$version")"
  if [[ -f "$zip" ]]; then
    echo "have   $addr $version"
    continue
  fi
  echo "fetch  $addr $version"
  IFS=/ read -r host namespace type <<<"$addr"
  url="$(curl -fsSL "https://$host/v1/providers/$namespace/$type/$version/download/${PLATFORM%%_*}/${PLATFORM#*_}" |
    jq -r .download_url)"
  mkdir -p "$(dirname "$zip")"
  curl -fsSL -o "$zip.part" "$url"
  mv "$zip.part" "$zip"
done
