#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <version> <tap-repo-path>" >&2
  echo "example: $0 0.2.1 ../homebrew-meetily-memory" >&2
  exit 2
fi

version="$1"
tap_repo="$2"
formula="$tap_repo/Formula/meetily-memory.rb"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

arm_archive="meetily-memory-v$version-macos-arm64.tar.gz"
intel_archive="meetily-memory-v$version-macos-x86_64.tar.gz"

arm_url="https://github.com/0x12th/meetily-memory/releases/download/v$version/$arm_archive"
intel_url="https://github.com/0x12th/meetily-memory/releases/download/v$version/$intel_archive"

curl -fsSL -o "$tmpdir/$arm_archive" "$arm_url"
curl -fsSL -o "$tmpdir/$intel_archive" "$intel_url"

arm_sha="$(shasum -a 256 "$tmpdir/$arm_archive" | awk '{print $1}')"
intel_sha="$(shasum -a 256 "$tmpdir/$intel_archive" | awk '{print $1}')"

cat > "$formula" <<EOF
class MeetilyMemory < Formula
  desc "Local-first Meetily history index and CLI"
  homepage "https://github.com/0x12th/meetily-memory"
  version "$version"
  license "Apache-2.0"

  on_macos do
    if Hardware::CPU.arm?
      url "$arm_url"
      sha256 "$arm_sha"
    elsif Hardware::CPU.intel?
      url "$intel_url"
      sha256 "$intel_sha"
    end
  end

  def install
    libexec.install Dir["*"]

    bin.install_symlink libexec/"mm" => "mm"
    bin.install_symlink libexec/"mm" => "meetily-memory"
  end

  test do
    system bin/"mm", "--help"
  end
end
EOF

echo "Updated $formula"
echo "macos-arm64: $arm_sha"
echo "macos-x86_64: $intel_sha"
