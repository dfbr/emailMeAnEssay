#!/usr/bin/env bash
# Build the EPUB for one essay. Full guide: BUILDING_EPUBS.md
#
# Usage:
#   ./build_epub.sh docs/_posts/YYYY-MM-DD-slug.md
#
# Environment overrides:
#   PANDOC=/usr/local/bin/pandoc   pandoc binary to use (default: pandoc on PATH)
#   COVER=path/to/cover.jpg        cover image (default: cover.jpg next to the epub)
#
# The epub is written to the path in the essay's `epub_download` front matter,
# relative to docs/ (e.g. docs/assets/essays/<slug>/<slug>.epub).

set -euo pipefail

post="${1:?Usage: ./build_epub.sh docs/_posts/YYYY-MM-DD-slug.md}"
PANDOC="${PANDOC:-pandoc}"

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
docs="$repo/docs"
css="$docs/_posts/epub.css"

[[ -f "$post" ]] || { echo "No such file: $post" >&2; exit 1; }
post="$(cd "$(dirname "$post")" && pwd)/$(basename "$post")"
command -v "$PANDOC" >/dev/null || { echo "pandoc not found ($PANDOC)" >&2; exit 1; }
[[ -f "$css" ]] || { echo "Missing house stylesheet: $css" >&2; exit 1; }

# Where the epub goes: the epub_download line in the front matter.
rel="$(grep -m1 '^epub_download:' "$post" | sed -E 's/^epub_download:[[:space:]]*//; s/^"//; s/"[[:space:]]*$//')"
[[ -n "$rel" ]] || { echo "No epub_download line in the front matter of $post" >&2; exit 1; }
out="$docs/${rel#/}"
mkdir -p "$(dirname "$out")"

cover="${COVER:-$(dirname "$out")/cover.jpg}"
cover_args=()
if [[ -f "$cover" ]]; then
  cover_args=(--epub-cover-image="$cover")
else
  echo "Warning: no cover found at $cover, building without one." >&2
fi

# Images use site paths (/assets/...). pandoc would read those as paths from
# the filesystem root, so make them relative to docs/ in a temporary copy.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
sed -E 's#\]\(/assets/#](assets/#g; s#src="/assets/#src="assets/#g' "$post" > "$tmp/essay.md"

cd "$docs"
"$PANDOC" "$tmp/essay.md" \
  --from markdown \
  --to epub3 \
  --output "$out" \
  --css "$css" \
  "${cover_args[@]}" \
  --toc --toc-depth=2 \
  --resource-path="$docs"

# Checks: the forced page background must not come back.
if unzip -p "$out" '*.css' | grep -qE '^[[:space:]]*background-color:[[:space:]]*#fdfdfd'; then
  echo "ERROR: the epub sets a page background colour. Was epub.css used?" >&2
  exit 1
fi
echo "Built ${out#"$repo"/} ($(du -h "$out" | cut -f1), $(unzip -l "$out" | grep -c 'EPUB/media/') images incl. cover)"

if python3 -c 'import epubcheck' 2>/dev/null; then
  python3 -m epubcheck "$out" 2>&1 | grep -E 'ERROR|FATAL' && {
    echo "epubcheck found errors (above)." >&2; exit 1; } || echo "epubcheck: no errors"
else
  echo "Tip: pip install epubcheck to validate the epub automatically."
fi
