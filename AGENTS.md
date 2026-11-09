# Notes for AI assistants

This repo publishes essays to dfbr.co.uk (Jekyll site in `docs/`).

## Adding or changing an essay

Follow [BUILDING_EPUBS.md](BUILDING_EPUBS.md). In short:

1. Essay in `docs/_posts/YYYY-MM-DD-<slug>.md`; its images (and cover, if you
   make one) in `docs/assets/essays/<slug>/`.
2. Push to `main`. The **Build EPUB** Action builds the EPUB, fills in the
   front matter (`epub_download`, `layout`, `word_count`...), makes a cover if
   there is none, commits it all back to `main` and redeploys the site.
3. Pull before editing the essay again.

To build locally, use `./build_epub.sh docs/_posts/YYYY-MM-DD-<slug>.md`,
which runs the same builder as the Action.

Rules:

- EPUBs are built only by `.github/scripts/build_epub.py` (directly, via
  `build_epub.sh`, or via the Action). It always uses the house stylesheet
  `docs/_posts/epub.css`. Never let an EPUB set a background colour behind
  the text.
- Don't hand-edit EPUB files or call pandoc yourself. Fix the source, the
  stylesheet or the builder, then rebuild.
- Images must be public domain or suitably licensed, with credits.
