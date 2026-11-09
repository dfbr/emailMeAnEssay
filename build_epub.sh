#!/usr/bin/env bash
# Build the EPUB for one or more essays locally. Full guide: BUILDING_EPUBS.md
#
# You don't normally need this: the "Build EPUB" GitHub Action builds the
# EPUB whenever an essay is pushed to main. This runs the same builder
# (.github/scripts/build_epub.py), so a local build matches the Action's.
#
# Usage:
#   ./build_epub.sh docs/_posts/YYYY-MM-DD-slug.md [more posts...]
#   ./build_epub.sh --all
#
# Environment overrides:
#   PANDOC=/usr/local/bin/pandoc   pandoc binary to use (default: pandoc on PATH)
#
# Needs pandoc 3.x and Python 3 with Pillow and PyYAML
# (pip install pillow pyyaml; add epubcheck to validate, which needs Java).

set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${PANDOC:-}" ]]; then
  PATH="$(cd "$(dirname "$(command -v "$PANDOC")")" && pwd):$PATH"
  export PATH
fi
command -v pandoc >/dev/null || { echo "pandoc not found" >&2; exit 1; }

exec python3 "$repo/.github/scripts/build_epub.py" "$@"
