# Building an essay EPUB

How to turn an essay in `docs/_posts` into the downloadable EPUB on
dfbr.co.uk. This applies to essays written by hand or by an AI assistant and
committed straight to the repo. (`essay_emailer.py` has its own EPUB builder;
see the README.)

## Quick version

```bash
python make_cover.py <background.jpg> docs/assets/essays/<slug> \
    --title "<title>" --subtitle "<subtitle>" --credit "<image credit>"
./build_epub.sh docs/_posts/YYYY-MM-DD-<slug>.md
```

Then commit the essay, its `docs/assets/essays/<slug>/` folder and the epub.

## 1. Lay out the files

Each essay has a markdown post and a folder of assets with the same slug:

```text
docs/_posts/YYYY-MM-DD-<slug>.md          the essay (Jekyll post)
docs/assets/essays/<slug>/
    <slug>.epub                           built by build_epub.sh
    cover.jpg                             1600×2400, from make_cover.py
    images/
        preview_1200x630.jpg              site preview (make_cover.py)
        preview_640x360.jpg               site preview (make_cover.py)
        <image files used in the essay>
        credits.json                      where each image came from (newer essays)
```

A few older essays keep their epub directly in `docs/assets/essays/`. Leave
those as they are; new essays get their own folder.

## 2. Front matter the EPUB uses

pandoc reads the post's YAML front matter for the book's metadata, so fill
these in carefully:

| Field | Used for |
|---|---|
| `title`, `subtitle` | title page and e-reader library |
| `author` | `ClaudeAI` |
| `date` | `YYYY-MM-DD`, the publishing date |
| `lang` | `en-GB` |
| `publisher` | `dfbr.co.uk` |
| `description`, `subject`, `keywords`, `rights` | book metadata |
| `identifier` | `urn:uuid:YYYY-MM-DD-<slug>` |
| `epub_download` | **where the epub is written**, e.g. `"/assets/essays/<slug>/<slug>.epub"` |

`build_epub.sh` reads `epub_download` to decide where to save the epub, so it
must match the asset folder.

## 3. Images

Download each image into `docs/assets/essays/<slug>/images/` and reference it
with a site path, so the website and the epub use the same file:

```markdown
![A hen enjoying a dust bath](/assets/essays/<slug>/images/dust-bath.jpg)
```

Images must be public domain or suitably licensed, with the credit given in
the essay (and, for new essays, recorded in `credits.json`). Local files make the build
repeatable; the script rewrites the `/assets/...` paths so pandoc can find
them.

Some older essays link to images by full URL instead. Those only end up in the
epub if your pandoc can download them while building (on the maintainer's
machine that is `/usr/local/bin/pandoc`; pass it with `PANDOC=`, below).

## 4. Make the cover

```bash
python make_cover.py <background.jpg> docs/assets/essays/<slug> \
    --title "<title>" --subtitle "<subtitle>" --author "ClaudeAI" \
    --credit "<Artist, Title (year), public domain>"
```

This writes `cover.jpg` and both preview images. Run
`python make_cover.py --help` for more options.

## 5. Build the epub

From the repo root:

```bash
./build_epub.sh docs/_posts/YYYY-MM-DD-<slug>.md
```

To use a particular pandoc, or a cover somewhere else:

```bash
PANDOC=/usr/local/bin/pandoc ./build_epub.sh docs/_posts/YYYY-MM-DD-<slug>.md
COVER=path/to/cover.jpg ./build_epub.sh docs/_posts/YYYY-MM-DD-<slug>.md
```

The script runs pandoc with:

- `--css docs/_posts/epub.css`, the house stylesheet (see below);
- `--epub-cover-image` pointing at `cover.jpg`;
- `--toc --toc-depth=2` for the contents page;
- the essay's front matter as book metadata.

It then checks the result and prints a one-line summary.

### Don't skip the house stylesheet

`docs/_posts/epub.css` is pandoc's default EPUB stylesheet without the page
background colour. Some pandoc versions add `background-color: #fdfdfd` to
`html`, which paints an off-white box behind all the text and overrides the
e-reader's own theme. Passing `--css` replaces pandoc's stylesheet entirely, so
the book looks the same whichever pandoc builds it. If you change how books
look, edit `epub.css`, and keep every rule you still need in it.

If you ever build by hand instead of with the script, include
`--css docs/_posts/epub.css`.

## 6. Check it

`build_epub.sh` stops with an error if the epub has a page background colour,
and runs [epubcheck](https://pypi.org/project/epubcheck/) when it's installed
(`pip install epubcheck`). A warning that the identifier "is marked as a UUID,
but is an invalid UUID" is expected and harmless.

Before committing, open the epub in a reader and check:

- the cover, title page and contents appear;
- every image shows, with its credit;
- the text has no coloured background, in both light and dark reading modes.

## Requirements

- pandoc 3.x
- Python 3 with Pillow (for `make_cover.py`)
- `unzip`
- optional: `pip install epubcheck` (needs Java)
