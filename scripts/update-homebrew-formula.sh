#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <version> <tap-repo-path>" >&2
  echo "example: $0 0.1.0 ../homebrew-meetily-memory" >&2
  exit 2
fi

version="$1"
tap_repo="$2"
formula="$tap_repo/Formula/meetily-memory.rb"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

arm_archive="meetily-memory-v$version-macos-arm64.tar.gz"
intel_archive="meetily-memory-v$version-macos-x86_64.tar.gz"

curl -fsSL -o "$tmpdir/$arm_archive" \
  "https://github.com/0x12th/meetily-memory/releases/download/v$version/$arm_archive"
curl -fsSL -o "$tmpdir/$intel_archive" \
  "https://github.com/0x12th/meetily-memory/releases/download/v$version/$intel_archive"

arm_sha="$(shasum -a 256 "$tmpdir/$arm_archive" | awk '{print $1}')"
intel_sha="$(shasum -a 256 "$tmpdir/$intel_archive" | awk '{print $1}')"

perl -0pi -e "s{/download/v[0-9]+\\.[0-9]+\\.[0-9]+/}{/download/v$version/}g" "$formula"
perl -0pi -e "s{meetily-memory-v[0-9]+\\.[0-9]+\\.[0-9]+-macos-arm64\\.tar\\.gz}{$arm_archive}g" "$formula"
perl -0pi -e "s{meetily-memory-v[0-9]+\\.[0-9]+\\.[0-9]+-macos-x86_64\\.tar\\.gz}{$intel_archive}g" "$formula"
perl -0pi -e "s/sha256 \"[^\"]+\"/sha256 \"$arm_sha\"/s" "$formula"
perl -0pi -e "s/sha256 \"$arm_sha\"(.*)sha256 \"[^\"]+\"/sha256 \"$arm_sha\"\$1sha256 \"$intel_sha\"/s" "$formula"

echo "Updated $formula"
echo "macos-arm64: $arm_sha"
echo "macos-x86_64: $intel_sha"
