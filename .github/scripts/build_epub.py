#!/usr/bin/env python3
"""Build the EPUB for an essay, with every image embedded.

Pandoc can fetch remote images itself, but Wikimedia Commons (where many essay
images live) refuses requests that do not identify themselves, and pandoc then
silently embeds the refusal message instead of the picture. This script
downloads every image first, politely and with retries, checks that each file
really is an image, and only then runs pandoc on local copies. If any image
cannot be fetched, no EPUB is written.

Usage
    python .github/scripts/build_epub.py docs/_posts/2026-10-20-history-of-maps.md [more posts...]

The EPUB is written to the path in the post's epub_download front matter
(under docs/). The cover is taken from, in order:
    --cover PATH (only when building a single post)
    cover.jpg / cover.png in the EPUB's own folder (assets/essays/<slug>/)
    <epub name>-cover.jpg / -cover.png next to the EPUB
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
USER_AGENT = "emailMeAnEssay-epub-builder/1.0 (+https://essays.dfbr.co.uk)"
MD_URL = r"<?((?:[^()\s<>]|\([^()\s]*\))+)>?"
MD_IMAGE = re.compile(r"(!\[[^\]]*\]\(\s*)" + MD_URL + r"((?:\s+\"[^\"]*\")?\s*\))")
HTML_IMG = re.compile(r"""(<img\b[^>]*?\bsrc\s*=\s*["'])([^"']+)(["'])""", re.I)
MAGIC = [(b"\xff\xd8\xff", ".jpg"), (b"\x89PNG", ".png"), (b"GIF8", ".gif"), (b"RIFF", ".webp"), (b"<svg", ".svg"), (b"<?xml", ".svg")]


def front_matter(text: str) -> tuple[dict, str]:
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            kv = re.match(r"^([A-Za-z_-]+)\s*:\s*(.*?)\s*$", line)
            if kv:
                fm[kv.group(1)] = kv.group(2).strip().strip("\"'")
    return fm, text[m.end():] if m else text


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
            ext = next((e for magic, e in MAGIC if data.startswith(magic)), None)
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
    raise RuntimeError(f"could not download {url}: {last}")


def find_cover(epub: Path) -> Path | None:
    candidates = []
    if epub.parent != DOCS / "assets" / "essays":
        candidates += [epub.parent / "cover.jpg", epub.parent / "cover.png"]
    candidates += [epub.with_name(epub.stem + "-cover.jpg"), epub.with_name(epub.stem + "-cover.png")]
    return next((c for c in candidates if c.is_file()), None)


def build(post: Path, cover: Path | None) -> None:
    text = post.read_text(encoding="utf-8")
    fm, _ = front_matter(text)
    epub_rel = fm.get("epub_download", "").lstrip("/")
    if not epub_rel.endswith(".epub"):
        raise SystemExit(f"{post}: no epub_download path in front matter")
    epub = DOCS / epub_rel
    cover = cover or find_cover(epub)
    print(f"== {post.name} -> {epub.relative_to(ROOT)} (cover: {cover.relative_to(ROOT) if cover else 'none'})")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fetched: dict = {}
        local_files: set = set()
        count = 0

        def local(url: str) -> str:
            nonlocal count
            if url.startswith("/") and not url.startswith("//"):
                path = DOCS / url.lstrip("/").split("?")[0]
                if not path.is_file():
                    raise RuntimeError(f"missing local image {url}")
                local_files.add(str(path))
                return str(path)
            if url.startswith(("http://", "https://", "//")):
                full = "https:" + url if url.startswith("//") else url
                if full not in fetched:
                    count += 1
                    print(f"  [{count}] {full}")
                    fetched[full] = str(download(full, tmp / f"img{count:03d}"))
                    time.sleep(1.5)  # be polite to the image host
                return fetched[full]
            return url

        body = MD_IMAGE.sub(lambda m: m.group(1) + local(m.group(2)) + m.group(3), text)
        body = HTML_IMG.sub(lambda m: m.group(1) + local(m.group(2)) + m.group(3), body)
        source = tmp / "essay.md"
        source.write_text(body, encoding="utf-8")

        expected = len(fetched) + len(local_files)
        out = tmp / "out.epub"
        cmd = ["pandoc", str(source), "-o", str(out), "--toc", "--toc-depth", "2"]
        if cover:
            cmd += ["--epub-cover-image", str(cover)]
        if not fm.get("lang"):
            cmd += ["--metadata", "lang=en-GB"]
        subprocess.run(cmd, check=True)

        with zipfile.ZipFile(out) as z:
            media = [n for n in z.namelist() if "/media/" in n]
        bad = [n for n in media if not n.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"))]
        if bad:
            raise RuntimeError(f"non-image files ended up in the EPUB: {bad}")
        wanted = expected + (1 if cover else 0)
        if len(media) < wanted:
            raise RuntimeError(f"expected at least {wanted} images in the EPUB, found {len(media)}")
        epub.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out, epub)
        print(f"  ok: {len(media)} images embedded, {epub.stat().st_size // 1024} KB")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("posts", nargs="+", help="Post files (paths, or names inside docs/_posts)")
    parser.add_argument("--cover", help="Cover image (single post only)")
    args = parser.parse_args()
    if args.cover and len(args.posts) > 1:
        parser.error("--cover can only be used with a single post")
    failures = 0
    for name in args.posts:
        post = Path(name)
        if not post.is_file():
            post = DOCS / "_posts" / Path(name).name
        try:
            build(post, Path(args.cover) if args.cover else None)
        except Exception as err:  # keep going, report at the end
            failures += 1
            print(f"  FAILED: {err}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
