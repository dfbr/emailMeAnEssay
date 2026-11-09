#!/usr/bin/env python3
"""Build the EPUB for an essay in docs/_posts, and fix up its front matter.

This is the only EPUB builder for posts in docs/_posts. The GitHub Action
(.github/workflows/build-epub.yml) runs it on every push that adds or changes
an essay, and ./build_epub.sh runs it locally, so both give the same result.

For each post it:

1. Fills in front matter the site needs, editing only the lines it changes:
   epub_download (if blank), layout, lang, publisher, preview_image (if blank
   and a preview exists) and word_count (recounted every time).
2. Makes a cover with make_cover.py if the essay has none, from the preview
   image, or the first image in the essay, or a plain background.
3. Downloads every remote image (politely: Wikimedia refuses anonymous
   requests and pandoc would silently embed the error page instead) and checks
   each really is an image. Local /assets/... images are used from docs/.
4. Runs pandoc with the house stylesheet docs/_posts/epub.css, the cover, a
   contents page and the book metadata.
5. Checks the result: every image embedded, no background colour behind the
   text, and no epubcheck errors.

Nothing is written (EPUB, cover or front matter) unless every check passes.

Usage
    python .github/scripts/build_epub.py docs/_posts/2026-11-05-remember-remember.md [more...]
    python .github/scripts/build_epub.py --all
    python .github/scripts/build_epub.py --from-push-event   # used by the Action

Requirements: pandoc 3.x, Python 3.10+, Pillow, PyYAML, and for validation
epubcheck (pip install epubcheck; needs Java).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
POSTS = DOCS / "_posts"
ESSAYS = DOCS / "assets" / "essays"
CSS = POSTS / "epub.css"
ID_FILTER = Path(__file__).with_name("epub_ids.lua")
SITE = "https://essays.dfbr.co.uk"
PUBLISHER = "dfbr.co.uk"
LANG = "en-GB"
USER_AGENT = "emailMeAnEssay-epub-builder/2.0 (+https://essays.dfbr.co.uk)"

# Changing any of these rebuilds every essay.
GLOBAL_INPUTS = {"docs/_posts/epub.css", ".github/scripts/build_epub.py", ".github/scripts/epub_ids.lua", "make_cover.py"}

MD_URL = r"<?((?:[^()\s<>]|\([^()\s]*\))+)>?"
MD_IMAGE = re.compile(r"(!\[[^\]]*\]\(\s*)" + MD_URL + r"((?:\s+\"[^\"]*\")?\s*\))")
HTML_IMG = re.compile(r"""(<img\b[^>]*?\bsrc\s*=\s*["'])([^"']+)(["'])""", re.I)
MAGIC = [(b"\xff\xd8\xff", ".jpg"), (b"\x89PNG", ".png"), (b"GIF8", ".gif"), (b"RIFF", ".webp"),
         (b"<svg", ".svg"), (b"<?xml", ".svg")]
FM_BLOCK = re.compile(r"\A---[ \t]*\r?\n(.*?\r?\n)?---[ \t]*\r?\n", re.S)
DATE_IN_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)$")


class BuildError(Exception):
    pass


# --------------------------------------------------------------------------
# Front matter: read with YAML, write by editing single lines so the rest of
# the file (quoting, order, comments) stays exactly as the author wrote it.
# --------------------------------------------------------------------------

def split_post(text: str) -> tuple[str, str]:
    m = FM_BLOCK.match(text)
    if not m:
        raise BuildError("no YAML front matter (--- ... ---) at the top of the file")
    return m.group(1) or "", text[m.end():]


def read_fm(fm_text: str) -> dict:
    data = yaml.safe_load(fm_text) or {}
    if not isinstance(data, dict):
        raise BuildError("front matter is not a YAML mapping")
    return data


def yaml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # JSON strings are valid YAML double-quoted scalars


def set_line(fm_text: str, key: str, rendered: str) -> str:
    line = f"{key}: {rendered}"
    pattern = re.compile(rf"^{re.escape(key)}[ \t]*:.*$", re.M)
    if pattern.search(fm_text):
        return pattern.sub(lambda _: line, fm_text, count=1)
    return fm_text + line + "\n"


def blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def slug_of(post: Path) -> str:
    m = DATE_IN_NAME.match(post.stem)
    return m.group(2) if m else post.stem


def date_of(post: Path, fm: dict) -> str:
    value = fm.get("date")
    if isinstance(value, (dt.date, dt.datetime)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return value.strip()[:10]
    m = DATE_IN_NAME.match(post.stem)
    return m.group(1) if m else dt.date.today().isoformat()


def site_path_to_file(url: str) -> Path:
    return DOCS / url.lstrip("/").split("?")[0].split("#")[0]


def download(url: str, dest_stem: Path) -> Path:
    last = ""
    for attempt in range(7):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.5"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
                ctype = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            # Trust the file's own signature, not the server's label: an error
            # page served with a misleading type must never be embedded.
            ext = next((e for magic, e in MAGIC if data.lstrip()[:5].startswith(magic) or data.startswith(magic)), None)
            if not ext:
                raise ValueError(f"not an image (Content-Type {ctype or 'none'}, starts {data[:40]!r})")
            path = dest_stem.with_suffix(ext)
            path.write_bytes(data)
            return path
        except urllib.error.HTTPError as err:
            last = f"HTTP {err.code}"
            if err.code in (404, 410):
                break
            wait = err.headers.get("Retry-After")
            delay = min(int(wait), 120) if wait and wait.isdigit() else 10 * (attempt + 1)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
            last = str(err)
            delay = 10 * (attempt + 1)
        print(f"    retrying in {delay}s ({last})")
        time.sleep(delay)
    raise BuildError(f"could not download {url}: {last}")


def book_identifier(value, slug: str) -> str:
    """A valid identifier for the book. 'urn:uuid:' values that are not real
    UUIDs (e.g. urn:uuid:2026-10-20-slug) become a stable UUID made from them."""
    value = "" if blank(value) else str(value).strip()
    if not value:
        return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'{SITE}/essays/{slug}/')}"
    if value.lower().startswith("urn:uuid:"):
        try:
            uuid.UUID(value[9:])
        except ValueError:
            return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, value)}"
    return value


def word_count(body: str) -> int:
    """Words of prose, counted the way a reader would: no markup, URLs or image captions."""
    lua = "function Image() return {} end\nfunction Figure() return {} end\n"
    with tempfile.NamedTemporaryFile("w", suffix=".lua", delete=False) as f:
        f.write(lua)
    try:
        plain = subprocess.run(["pandoc", "--from", "markdown", "--to", "plain", "--wrap", "none",
                                "--lua-filter", f.name], input=body, capture_output=True, text=True, check=True).stdout
    finally:
        os.unlink(f.name)
    return len([w for w in plain.split() if re.search(r"\w", w)])


# --------------------------------------------------------------------------
# Cover
# --------------------------------------------------------------------------

def cover_paths(epub: Path) -> list[Path]:
    paths = []
    if epub.parent != ESSAYS:  # the essay has its own folder
        paths += [epub.parent / "cover.jpg", epub.parent / "cover.png"]
    paths += [epub.with_name(epub.stem + "-cover.jpg"), epub.with_name(epub.stem + "-cover.png")]
    return paths


def image_credit(epub: Path, image_url: str) -> str:
    credits = epub.parent / "images" / "credits.json"
    if not credits.is_file():
        return ""
    try:
        data = json.loads(credits.read_text(encoding="utf-8"))
    except ValueError:
        return ""
    name = Path(image_url.split("?")[0]).name
    entries = data.items() if isinstance(data, dict) else enumerate(data if isinstance(data, list) else [])
    for key, entry in entries:
        if isinstance(entry, dict) and (key == name or name in json.dumps(entry)):
            for field in ("cover_credit", "short_credit", "credit"):
                if isinstance(entry.get(field), str) and len(entry[field]) <= 90:
                    return entry[field]
    return ""


def make_cover(fm: dict, body: str, epub: Path, tmp: Path) -> tuple[Path, list[tuple[Path, Path]], str]:
    """Return (cover file in tmp, [(tmp file, final path)] to install, preview_image site path or '')."""
    sys.path.insert(0, str(ROOT))
    from PIL import Image, ImageFilter  # noqa: E402
    import make_cover as mc  # noqa: E402

    candidates = []
    if not blank(fm.get("preview_image")):
        candidates.append(str(fm["preview_image"]).strip())
    candidates += [m.group(2) for m in MD_IMAGE.finditer(body)] + [m.group(2) for m in HTML_IMG.finditer(body)]

    background, used = None, ""
    for url in candidates:
        try:
            if url.startswith("/") and not url.startswith("//"):
                path = site_path_to_file(url)
                if not path.is_file():
                    continue
            elif url.startswith(("http://", "https://", "//")):
                path = download("https:" + url if url.startswith("//") else url, tmp / "cover-background")
            else:
                continue
            if path.suffix.lower() == ".svg":
                continue
            background, used = Image.open(path), url
            background.load()
            break
        except (BuildError, OSError) as err:
            print(f"    cover: skipping {url} ({err})")
    if background is None:
        print("    cover: no usable image, using a plain background")
        background = Image.new("RGB", mc.COVER_SIZE, (28, 44, 66))

    softened = background.convert("RGB").filter(ImageFilter.GaussianBlur(radius=1.2))
    cover = mc.build_cover(softened, str(fm.get("title", "")), str(fm.get("subtitle") or ""), "ClaudeAI",
                           image_credit(epub, used) if used else "")
    tmp_cover = tmp / "cover.jpg"
    cover.save(tmp_cover, quality=90, optimize=True)
    installs = [(tmp_cover, cover_paths(epub)[0])]

    # Site previews, only for essays with their own folder and none yet.
    preview_site = ""
    if epub.parent != ESSAYS and used:
        for w, h in mc.PREVIEW_SIZES:
            final = epub.parent / "images" / f"preview_{w}x{h}.jpg"
            if not final.exists():
                tmp_prev = tmp / final.name
                mc.build_preview(background, (w, h)).save(tmp_prev, quality=85, optimize=True)
                installs.append((tmp_prev, final))
        preview_site = "/" + (epub.parent / "images" / "preview_1200x630.jpg").relative_to(DOCS).as_posix()
    print(f"    cover: generated from {used or 'a plain background'}")
    return tmp_cover, installs, preview_site


# --------------------------------------------------------------------------
# Checks on the built EPUB
# --------------------------------------------------------------------------

ALLOWED_BACKGROUND = {"transparent", "none", "inherit", "initial", "unset"}


def check_no_background(z: zipfile.ZipFile) -> None:
    problems = []
    for name in z.namelist():
        if name.endswith(".css"):
            css = re.sub(r"/\*.*?\*/", "", z.read(name).decode("utf-8", "replace"), flags=re.S)
            for selector, decls in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
                selector = " ".join(selector.split())
                for prop, value in re.findall(r"(background(?:-color)?)\s*:\s*([^;]+)", decls):
                    value = value.strip().lower().replace("!important", "").strip()
                    # A horizontal rule is drawn with a background colour; that is not behind text.
                    if value not in ALLOWED_BACKGROUND and selector != "hr":
                        problems.append(f"{name}: {selector} {{ {prop}: {value} }}")
        elif name.endswith((".xhtml", ".html")):
            html = z.read(name).decode("utf-8", "replace")
            for m in re.finditer(r"""style\s*=\s*["'][^"']*background[^"']*["']|\bbgcolor\s*=""", html, re.I):
                problems.append(f"{name}: {m.group(0)[:80]}")
    if problems:
        raise BuildError("the EPUB puts a background colour behind text:\n      " + "\n      ".join(problems))


def run_epubcheck(path: Path, required: bool) -> None:
    try:
        from epubcheck import EpubCheck
    except ImportError:
        if required:
            raise BuildError("epubcheck is not installed (pip install epubcheck)")
        print("    epubcheck: not installed, skipped (pip install epubcheck)")
        return
    result = EpubCheck(str(path))
    errors = [m for m in result.messages if m.level in ("ERROR", "FATAL")]
    warnings = [m for m in result.messages if m.level == "WARNING"]
    for m in warnings:
        print(f"    epubcheck warning: {m.id} {m.message} ({m.location})")
    if errors:
        raise BuildError("epubcheck errors:\n      " + "\n      ".join(f"{m.id} {m.message} ({m.location})" for m in errors))
    print(f"    epubcheck: no errors ({len(warnings)} warnings)")


# --------------------------------------------------------------------------
# Build one post
# --------------------------------------------------------------------------

def build(post: Path, require_epubcheck: bool) -> list[Path]:
    """Build one post. Returns the files written."""
    if not CSS.is_file():
        raise BuildError(f"missing house stylesheet {CSS.relative_to(ROOT)}")
    text = post.read_text(encoding="utf-8")
    fm_text, body = split_post(text)
    fm = read_fm(fm_text)
    slug = slug_of(post)
    if blank(fm.get("title")):
        raise BuildError("no title in the front matter")

    # 1. Front matter the site needs.
    epub_rel = str(fm.get("epub_download") or "").strip()
    if not epub_rel.endswith(".epub"):
        epub_rel = f"/assets/essays/{slug}/{slug}.epub"
        fm_text = set_line(fm_text, "epub_download", yaml_str(epub_rel))
    for key, value in (("layout", "essay"), ("lang", LANG), ("publisher", PUBLISHER)):
        if blank(fm.get(key)):
            fm_text = set_line(fm_text, key, yaml_str(value))
    epub = DOCS / epub_rel.lstrip("/")
    print(f"== {post.name} -> {epub.relative_to(ROOT)}")

    written: list[Path] = []
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)

        # 2. Cover.
        installs: list[tuple[Path, Path]] = []
        cover = next((c for c in cover_paths(epub) if c.is_file()), None)
        if cover is None:
            cover, installs, preview_site = make_cover(fm, body, epub, tmp)
            if preview_site and blank(fm.get("preview_image")):
                fm_text = set_line(fm_text, "preview_image", yaml_str(preview_site))
        elif blank(fm.get("preview_image")):
            existing = epub.parent / "images" / "preview_1200x630.jpg"
            if epub.parent != ESSAYS and existing.is_file():
                fm_text = set_line(fm_text, "preview_image", yaml_str("/" + existing.relative_to(DOCS).as_posix()))
        print(f"    cover: {cover.name if cover.parent == tmp else cover.relative_to(ROOT)}")

        count = word_count(body)
        if fm.get("word_count") != count:
            fm_text = set_line(fm_text, "word_count", str(count))

        # 3. Images: local copies of everything.
        fetched: dict[str, str] = {}
        local_files: set[str] = set()

        def local(url: str) -> str:
            if url.startswith("/") and not url.startswith("//"):
                path = site_path_to_file(url)
                if not path.is_file():
                    raise BuildError(f"missing local image {url}")
                local_files.add(str(path))
                return str(path)
            if url.startswith(("http://", "https://", "//")):
                full = "https:" + url if url.startswith("//") else url
                if full not in fetched:
                    print(f"    [{len(fetched) + 1}] {full}")
                    fetched[full] = str(download(full, tmp / f"img{len(fetched) + 1:03d}"))
                    time.sleep(1.5)  # be polite to the image host
                return fetched[full]
            return url

        new_text = f"---\n{fm_text}---\n{body}"
        source_body = MD_IMAGE.sub(lambda m: m.group(1) + local(m.group(2)) + m.group(3), body)
        source_body = HTML_IMG.sub(lambda m: m.group(1) + local(m.group(2)) + m.group(3), source_body)
        source = tmp / "essay.md"
        source.write_text(f"---\n{fm_text}---\n{source_body}", encoding="utf-8")

        # 4. pandoc. Command-line metadata overrides the front matter, so the
        # book gets clean values without the post having to change.
        final_fm = read_fm(fm_text)
        meta = {
            "date": date_of(post, final_fm),
            "lang": str(final_fm.get("lang") or LANG),
            "publisher": str(final_fm.get("publisher") or PUBLISHER),
            "identifier": book_identifier(final_fm.get("identifier"), slug),
            "rights": str(final_fm.get("rights") or f"© {date_of(post, final_fm)[:4]} {PUBLISHER}"),
        }
        if blank(final_fm.get("description")) and not blank(final_fm.get("preview_text")):
            meta["description"] = str(final_fm["preview_text"])
        out = tmp / "out.epub"
        cmd = ["pandoc", str(source), "--from", "markdown", "--to", "epub3", "--output", str(out),
               "--css", str(CSS), "--lua-filter", str(ID_FILTER), "--epub-cover-image", str(cover), "--toc", "--toc-depth", "2",
               "--resource-path", str(DOCS)]
        for key, value in meta.items():
            cmd += ["--metadata", f"{key}={value}"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stderr.strip():
            print("    pandoc: " + result.stderr.strip().replace("\n", "\n    pandoc: "))
        if result.returncode:
            raise BuildError(f"pandoc failed (exit {result.returncode})")

        # 5. Checks.
        with zipfile.ZipFile(out) as z:
            media = [n for n in z.namelist() if "/media/" in n]
            bad = [n for n in media if not n.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"))]
            if bad:
                raise BuildError(f"non-image files ended up in the EPUB: {bad}")
            wanted = len(fetched) + len(local_files) + 1  # + cover
            if len(media) < wanted:
                raise BuildError(f"expected at least {wanted} images in the EPUB, found {len(media)}")
            check_no_background(z)
        run_epubcheck(out, require_epubcheck)

        # All good: install everything.
        epub.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out, epub)
        written.append(epub)
        for src, dest in installs:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            written.append(dest)
        if new_text != text:
            post.write_text(new_text, encoding="utf-8")
            written.append(post)
        print(f"    ok: {len(media)} images embedded, {epub.stat().st_size // 1024} KB, {count} words")
    return written


# --------------------------------------------------------------------------
# Which posts to build
# --------------------------------------------------------------------------

def all_posts() -> list[Path]:
    return sorted(POSTS.glob("*.md"))


def posts_from_push_event() -> list[Path]:
    """Posts affected by the commits in the push that triggered the Action."""
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    changed: set[str] = set()
    for commit in event.get("commits", []):
        changed.update(commit.get("added", []))
        changed.update(commit.get("modified", []))
    if changed & GLOBAL_INPUTS:
        print("A shared input changed (" + ", ".join(sorted(changed & GLOBAL_INPUTS)) + "): rebuilding every essay.")
        return all_posts()

    posts: set[Path] = set()
    asset_dirs: set[str] = set()
    for name in changed:
        p = ROOT / name
        if name.startswith("docs/_posts/") and name.endswith(".md") and p.is_file():
            posts.add(p)
        elif name.startswith("docs/assets/essays/") and not name.endswith(".epub"):
            parts = Path(name).relative_to("docs/assets/essays").parts
            if len(parts) > 1:  # a file inside an essay's own folder
                asset_dirs.add(parts[0])
    for post in all_posts():
        if post in posts or not asset_dirs:
            continue
        try:
            fm = read_fm(split_post(post.read_text(encoding="utf-8"))[0])
        except BuildError:
            continue
        epub_dir = Path(str(fm.get("epub_download") or "").lstrip("/")).parent.name
        if slug_of(post) in asset_dirs or epub_dir in asset_dirs:
            posts.add(post)
    return sorted(posts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("posts", nargs="*", help="Post files (paths, or names inside docs/_posts)")
    parser.add_argument("--all", action="store_true", help="Build every post")
    parser.add_argument("--from-push-event", action="store_true",
                        help="Build the posts changed by the push in $GITHUB_EVENT_PATH (GitHub Actions)")
    parser.add_argument("--require-epubcheck", action="store_true", help="Fail if epubcheck is not installed")
    parser.add_argument("--written-list", help="Append the paths of files written to this file")
    args = parser.parse_args()

    if args.all:
        posts = all_posts()
    elif args.from_push_event:
        posts = posts_from_push_event()
    else:
        posts = []
        for name in args.posts:
            p = Path(name)
            posts.append(p if p.is_file() else POSTS / p.name)
    if not posts:
        print("No essays to build.")
        return 0

    failures, written = [], []
    for post in posts:
        try:
            written += build(post.resolve(), args.require_epubcheck)
        except (BuildError, subprocess.CalledProcessError, OSError, yaml.YAMLError) as err:
            failures.append(post.name)
            print(f"    FAILED: {err}", file=sys.stderr)
    if args.written_list:
        with open(args.written_list, "a", encoding="utf-8") as f:
            f.writelines(f"{p.relative_to(ROOT).as_posix()}\n" for p in written)
    print(f"\nBuilt {len(posts) - len(failures)} of {len(posts)} essay(s).")
    if failures:
        print("Failed: " + ", ".join(failures), file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
