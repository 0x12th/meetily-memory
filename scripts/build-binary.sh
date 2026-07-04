#!/usr/bin/env bash
set -euo pipefail

name="${1:-mm}"

rm -rf dist build/pyinstaller

uv run pyinstaller \
  --onefile \
  --name "$name" \
  --clean \
  --distpath dist \
  --workpath build/pyinstaller \
  --specpath build/pyinstaller \
  src/meetily_memory/__main__.py

"./dist/$name" --help >/dev/null
