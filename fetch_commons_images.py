#!/usr/bin/env python3
"""Fetch freely licensed photos from Wikimedia Commons for an essay.

Takes a JSON manifest mapping image keys to search queries, e.g.
    {"light-sussex": "Light Sussex hen", "silkie": "Silkie chicken"}

Step 1, review candidates (downloads a few per key plus a contact sheet):
    python fetch_commons_images.py manifest.json review_dir --candidates 4

Step 2, install the chosen ones (index from the contact sheet, default 0):
    python fetch_commons_images.py manifest.json docs/assets/essays/<slug>/images \
        --pick silkie=2 --pick marans=1

Installed images are saved as <key>.jpg, and credits.json records the title,
author, licence and file page for each. Step 3 swaps each {{credit:<key>}}
placeholder in a post for a linked credit line:
    python fetch_commons_images.py manifest.json docs/assets/essays/<slug>/images \
        --fill-credits docs/_posts/<date>-<slug>.md
Only CC0, public domain, CC BY and CC BY-SA images are accepted.
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "emailMeAnEssay/1.0 (https://essays.dfbr.co.uk)"
ALLOWED = re.compile(r"^(CC0|Public domain|PD\b|CC BY(-SA)? \d)", re.I)
MIN_WIDTH = 800
THUMB_WIDTH = 1280  # a standard Commons thumbnail size; odd sizes are rate-limited harder
DELAY = 1.0  # seconds between requests; raise with --delay if Commons rate-limits you


def _get(url: str, attempts: int = 6) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
            time.sleep(DELAY)
            return data
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == attempts - 1:
                raise
            wait = int(error.headers.get("Retry-After") or 0) or 30 * 2**attempt
            print(f"  rate-limited; waiting {wait}s", flush=True)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _plain(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def search(query: str, limit: int) -> list[dict]:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap",
        "gsrnamespace": 6,
        "gsrlimit": 30,
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": THUMB_WIDTH,
    }
    data = json.loads(_get(f"{API}?{urllib.parse.urlencode(params)}"))
    pages = sorted(data.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    results = []
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        licence = _plain(meta.get("LicenseShortName", {}).get("value", ""))
        if info.get("mime") not in ("image/jpeg", "image/png") or info.get("width", 0) < MIN_WIDTH:
            continue
        if not ALLOWED.match(licence):
            continue
        results.append({
            "title": page["title"].removeprefix("File:"),
            "author": _plain(meta.get("Artist", {}).get("value", "")) or "Unknown author",
            "licence": licence,
            "page": info.get("descriptionurl", ""),
            "thumb": info.get("thumburl") or info["url"],
        })
        if len(results) >= limit:
            break
    return results


def download(candidate: dict) -> Image.Image:
    image = Image.open(BytesIO(_get(candidate["thumb"])))
    return ImageOps.exif_transpose(image).convert("RGB")


def contact_sheet(key: str, images: list[Image.Image], path: Path) -> None:
    tile = 360
    sheet = Image.new("RGB", (tile * len(images), tile + 40), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(28)
    for i, image in enumerate(images):
        sheet.paste(ImageOps.contain(image, (tile - 10, tile - 10)), (i * tile + 5, 5))
        draw.text((i * tile + 10, tile + 4), f"{key} #{i}", fill="black", font=font)
    sheet.save(path, quality=80)


def main() -> None:
    global DELAY
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest")
    parser.add_argument("out_dir")
    parser.add_argument("--candidates", type=int, default=0, help="Review mode: candidates per key")
    parser.add_argument("--pick", action="append", default=[], help="key=index to install (default index 0)")
    parser.add_argument("--only", nargs="*", help="Limit to these keys")
    parser.add_argument("--delay", type=float, default=DELAY, help="Seconds between requests")
    parser.add_argument("--fill-credits", metavar="POST", help="Replace {{credit:key}} placeholders in POST")
    args = parser.parse_args()

    DELAY = args.delay
    manifest = json.loads(Path(args.manifest).read_text())
    keys = args.only or list(manifest)
    picks = dict(p.split("=", 1) for p in args.pick)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    credits_path = out / "credits.json"
    credits = json.loads(credits_path.read_text()) if credits_path.exists() else {}

    if args.fill_credits:
        post = Path(args.fill_credits)
        text = post.read_text()

        def credit(match: re.Match) -> str:
            c = credits.get(match.group(1))
            if not c:
                return match.group(0)
            return f"Photo: {c['author']}, [{c['licence']}]({c['page']}), via Wikimedia Commons."

        filled = re.sub(r"\{\{credit:([a-z0-9-]+)\}\}", credit, text)
        post.write_text(filled)
        missing = re.findall(r"\{\{credit:([a-z0-9-]+)\}\}", filled)
        print(f"credits filled; missing: {missing or 'none'}")
        return

    for key in keys:
        if args.candidates:
            if (out / f"{key}.json").exists():
                continue
            found = search(manifest[key], args.candidates)
            images = []
            for candidate in list(found):
                try:
                    images.append(download(candidate))
                except urllib.error.HTTPError as error:
                    print(f"  skipped {candidate['title']}: {error}", flush=True)
                    found.remove(candidate)
            (out / f"{key}.json").write_text(json.dumps(found, indent=2))
            if images:
                contact_sheet(key, images, out / f"{key}-sheet.jpg")
            print(f"{key}: {len(images)} candidates", flush=True)
            continue
        index = int(picks.get(key, 0))
        found = search(manifest[key], index + 1)
        if len(found) <= index:
            print(f"{key}: no acceptable image found", file=sys.stderr)
            continue
        chosen = found[index]
        download(chosen).save(out / f"{key}.jpg", quality=85, optimize=True)
        credits[key] = {k: chosen[k] for k in ("title", "author", "licence", "page")}
        print(f"{key}: {chosen['title']} ({chosen['licence']}, {chosen['author']})")

    if not args.candidates:
        credits_path.write_text(json.dumps(credits, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
