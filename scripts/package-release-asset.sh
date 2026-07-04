#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <version-tag> <suffix>" >&2
  echo "example: $0 v0.2.1 macos-arm64" >&2
  exit 2
fi

version="$1"
suffix="$2"
name="meetily-memory-${version}-${suffix}"
release_root="target/release-assets"
package_dir="$release_root/$name"
archive="$release_root/$name.tar.gz"

test -x dist/mm/mm

rm -rf "$package_dir" "$archive"
mkdir -p "$package_dir"

cp -R dist/mm/. "$package_dir/"
cp README.md "$package_dir/README.md"
cp CHANGELOG.md "$package_dir/CHANGELOG.md"
cp LICENSE "$package_dir/LICENSE"

tar -C "$release_root" -czf "$archive" "$name"

if command -v shasum >/dev/null 2>&1; then
  shasum -a 256 "$archive"
else
  sha256sum "$archive"
fi
