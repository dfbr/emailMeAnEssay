# Building an essay EPUB

How an essay in `docs/_posts` becomes the downloadable EPUB on dfbr.co.uk.
This applies to essays written by hand or by an AI assistant and committed
straight to the repo. (`essay_emailer.py` has its own EPUB builder; see the
README.)

## Quick version

Push the essay to `main`. That's it.

The **Build EPUB** GitHub Action (`.github/workflows/build-epub.yml`) then:

1. fills in the front matter the site needs (below);
2. makes a cover if the essay has none;
3. builds the EPUB with every image embedded and the house stylesheet;
4. checks it, and commits the EPUB, cover and front matter back to `main`;
5. redeploys the site, so the download link works a few minutes later.

**Then `git pull`** before you edit that essay again, because the Action will
have edited its front matter.

If a build fails, nothing is committed for that essay, the run is marked as
failed (GitHub emails you), and the run summary says which essay and why: a
missing image, an image host refusing the download, or an epubcheck error.

## When it runs

| You push a change to… | The Action rebuilds |
|---|---|
| `docs/_posts/<post>.md` | that essay |
| a file in `docs/assets/essays/<slug>/` (an image, the cover) | that essay |
| `docs/_posts/epub.css`, `.github/scripts/build_epub.py`, `.github/scripts/epub_ids.lua` or `make_cover.py` | every essay |

To rebuild by hand: **Actions → Build EPUB → Run workflow**, then give post
file names separated by spaces, or `all`.

Deleting an essay does not delete its EPUB; remove that yourself if you want
it gone.

## 1. Lay out the files

Each essay has a markdown post and a folder of assets with the same slug:

```text
docs/_posts/YYYY-MM-DD-<slug>.md          the essay (Jekyll post)
docs/assets/essays/<slug>/
    <slug>.epub                           built by the Action
    cover.jpg                             1600×2400; yours, or made by the Action
    images/
        preview_1200x630.jpg              site preview
        preview_640x360.jpg               site preview
        <image files used in the essay>
        credits.json                      where each image came from (newer essays)
```

A few older essays keep their EPUB (and `<name>-cover.jpg`) directly in
`docs/assets/essays/`. Leave those as they are; the Action keeps using their
existing paths. New essays get their own folder.

## 2. Front matter

### What the Action fills in

It edits only the lines it needs to, leaving the rest of your front matter
exactly as written.

| Field | What the Action does |
|---|---|
| `epub_download` | if blank, sets `"/assets/essays/<slug>/<slug>.epub"`; an existing path is never changed |
| `layout` | sets `"essay"` if missing (without it the page has no layout or download link) |
| `lang` | sets `"en-GB"` if missing |
| `publisher` | sets `"dfbr.co.uk"` if missing |
| `preview_image` | if blank, points it at `images/preview_1200x630.jpg` when there is one (and makes one when it makes the cover) |
| `word_count` | recounted on every build: words of prose, not counting markup, links or image captions |

### What goes into the book

pandoc reads the front matter for the book's metadata:

| Field | Used for |
|---|---|
| `title`, `subtitle` | title page and e-reader library (required: `title`) |
| `author` | e.g. `ClaudeAI Claude Opus 5.5` |
| `date` | publication date; the time part, if any, is dropped (falls back to the date in the file name) |
| `lang` | `en-GB` |
| `publisher` | `dfbr.co.uk` |
| `description` | book description; falls back to `preview_text` |
| `subject`, `keywords` | book metadata, if present |
| `rights` | falls back to `© <year> dfbr.co.uk` |
| `identifier` | optional. If missing, or a `urn:uuid:` that isn't a real UUID, a stable UUID is made from it or from the slug |

These fallbacks are applied to the book only; they are not written into the
post.

## 3. Images

Download each image into `docs/assets/essays/<slug>/images/` and reference it
with a site path, so the website and the EPUB use the same file:

```markdown
![A hen enjoying a dust bath](/assets/essays/<slug>/images/dust-bath.jpg)
```

Images given by full URL (e.g. Wikimedia Commons) also work: the builder
downloads each one, identifying itself politely and retrying if the host is
busy, and checks that what came back really is an image. If any image can't
be fetched, the build fails rather than producing a book with a missing or
broken picture. Local files are more reliable, so prefer them.

Images must be public domain or suitably licensed, with the credit given in
the essay (and, for new essays, recorded in `credits.json`).

## 4. The cover

If there is no `cover.jpg` (or `cover.png`) in the essay's folder, the Action
makes one with `make_cover.py`, using the first of these that works:

1. the `preview_image`;
2. the first image in the essay;
3. a plain dark background.

If `credits.json` has a short credit for that image (a `cover_credit`,
`short_credit` or `credit` of 90 characters or less), it is printed at the
foot of the cover.

To use your own cover, make it before pushing:

```bash
python make_cover.py <background.jpg> docs/assets/essays/<slug> \
    --title "<title>" --subtitle "<subtitle>" --author "ClaudeAI" \
    --credit "<Artist, Title (year), public domain>"
```

This writes `cover.jpg` and both preview images. Replacing `cover.jpg` later
and pushing rebuilds the EPUB with the new cover.

## 5. The house stylesheet

`docs/_posts/epub.css` is pandoc's default EPUB stylesheet without the page
background colour. Some pandoc versions add `background-color: #fdfdfd` to
`html`, which paints an off-white box behind all the text and overrides the
e-reader's own theme. Passing `--css` replaces pandoc's stylesheet entirely,
so the book looks the same whichever pandoc builds it. If you change how books
look, edit `epub.css`, keep every rule you still need in it, and push: every
EPUB is rebuilt.

## 6. Checks

The builder refuses to write an EPUB unless:

- every image in the essay, and the cover, is embedded and is a real image;
- no stylesheet or inline style puts a background colour behind text
  (`transparent` is fine, as is the colour used to draw a horizontal rule);
- [epubcheck](https://pypi.org/project/epubcheck/) reports no errors
  (warnings are shown but allowed).

`epub_ids.lua` stops a common epubcheck error: when an essay has no level-1
headings, pandoc wraps it in a section named after the title, and a heading
with the same words (e.g. "2046: Remember, remember") would get the same ID.

It's still worth opening a new EPUB in a reader and checking the cover, the
contents, the images, and that the text has no coloured background in light
and dark modes.

## Building locally

The Action runs the same script as this, so the results match:

```bash
./build_epub.sh docs/_posts/YYYY-MM-DD-<slug>.md
./build_epub.sh --all
PANDOC=/usr/local/bin/pandoc ./build_epub.sh docs/_posts/YYYY-MM-DD-<slug>.md
```

Requirements:

- pandoc 3.x (the Action uses 3.13)
- Python 3.10+ with Pillow and PyYAML: `pip install pillow pyyaml`
- optional: `pip install epubcheck` (needs Java) to validate
