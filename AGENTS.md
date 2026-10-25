# Notes for AI assistants

This repo publishes essays to dfbr.co.uk (Jekyll site in `docs/`).

## Building an essay's EPUB

Follow [BUILDING_EPUBS.md](BUILDING_EPUBS.md). In short:

1. Essay in `docs/_posts/YYYY-MM-DD-<slug>.md`; images, `cover.jpg` and the
   epub in `docs/assets/essays/<slug>/`.
2. Make the cover with `make_cover.py`.
3. Build with `./build_epub.sh docs/_posts/YYYY-MM-DD-<slug>.md`.

Rules:

- Always build with the house stylesheet `docs/_posts/epub.css` (the script
  does this). Never let the epub set a page background colour.
- Don't hand-edit epub files or call pandoc without `--css
  docs/_posts/epub.css`. Fix the source or the stylesheet, then rebuild.
- Images must be public domain or suitably licensed, with credits.
