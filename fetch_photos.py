#!/usr/bin/env python3
"""Fetch openly licensed photos for an essay from Wikimedia Commons, Openverse or iNaturalist.

Takes a JSON manifest mapping image keys to searches. A plain string searches
Wikimedia Commons; an object picks the source:
    {
      "light-sussex": "Light Sussex hen",
      "silkie": {"source": "openverse", "q": "Silkie chicken"},
      "red-fox": {"source": "inaturalist", "taxon": "Vulpes vulpes"},
      "geese": {"source": "inaturalist", "taxon": "Anser anser", "captive": true, "q": "domestic"}
    }

Step 1, review: download a few candidates per key, plus a contact sheet each:
    python fetch_photos.py manifest.json review_dir --candidates 4

Step 2, install the chosen candidate (index from the contact sheet, default 0)
from the review directory, so nothing is downloaded twice:
    python fetch_photos.py manifest.json docs/assets/essays/<slug>/images \
        --from-review review_dir --pick silkie=2 --pick marans=1

Installed images are saved as <key>.jpg, and credits.json records the title,
author, licence, source and page for each. Step 3 swaps each {{credit:<key>}}
placeholder in a post for a linked credit line:
    python fetch_photos.py manifest.json docs/assets/essays/<slug>/images \
        --fill-credits docs/_posts/<date>-<slug>.md

Only CC0, public domain, CC BY and CC BY-SA images are accepted.
"""

import argparse
import html
import json
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
OPENVERSE_API = "https://api.openverse.org/v1/images/"
INATURALIST_API = "https://api.inaturalist.org/v1/observations"
USER_AGENT = "emailMeAnEssay/1.0 (https://essays.dfbr.co.uk)"
ALLOWED = re.compile(r"^(CC0|Public domain|PD\b|CC BY(-SA)?\b)", re.I)
MIN_WIDTH = 800
THUMB_WIDTH = 1280  # a standard Commons thumbnail size; odd sizes are rate-limited harder
MAX_WIDTH = 1600
DELAY = 1.0  # seconds between requests; raise with --delay if a source rate-limits you
SOURCE_NAMES = {"commons": "Wikimedia Commons", "inaturalist": "iNaturalist", "flickr": "Flickr"}


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


def _get_json(url: str, params: dict) -> dict:
    return json.loads(_get(f"{url}?{urllib.parse.urlencode(params)}"))


def _plain(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def search_commons(spec: dict, limit: int) -> list[dict]:
    data = _get_json(COMMONS_API, {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"{spec['q']} filetype:bitmap",
        "gsrnamespace": 6,
        "gsrlimit": 30,
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": THUMB_WIDTH,
    })
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
            "source": "commons",
            "page": info.get("descriptionurl", ""),
            "image": info.get("thumburl") or info["url"],
        })
    return results[:limit]


def search_openverse(spec: dict, limit: int) -> list[dict]:
    # Commons images are searched directly; leaving them out here avoids its rate limits.
    data = _get_json(OPENVERSE_API, {
        "q": spec["q"],
        "license": "by,by-sa,cc0,pdm",
        "excluded_source": "wikimedia",
        "mature": "false",
        "page_size": 20,
    })
    results = []
    for item in data.get("results", []):
        if (item.get("width") or 0) < MIN_WIDTH:
            continue
        licence = {"cc0": "CC0", "pdm": "Public domain"}.get(
            item["license"], f"CC {item['license'].upper()} {item.get('license_version') or ''}".strip()
        )
        results.append({
            "title": item.get("title") or "Untitled",
            "author": item.get("creator") or "Unknown author",
            "licence": licence,
            "source": item.get("source", "openverse"),
            "page": item.get("foreign_landing_url", ""),
            "image": item["url"],
        })
    return results[:limit]


def search_inaturalist(spec: dict, limit: int) -> list[dict]:
    params = {
        "taxon_name": spec["taxon"],
        "photo_license": "cc-by,cc-by-sa,cc0",
        "photos": "true",
        "order_by": "votes",
        "per_page": 30,
    }
    if spec.get("captive"):
        params["captive"] = "true"
    else:
        params["quality_grade"] = "research"
    if spec.get("q"):
        params["q"] = spec["q"]
    data = _get_json(INATURALIST_API, params)
    licences = {"cc-by": "CC BY 4.0", "cc-by-sa": "CC BY-SA 4.0", "cc0": "CC0"}
    results = []
    for observation in data.get("results", []):
        for photo in observation.get("photos", []):
            licence = licences.get(photo.get("license_code") or "")
            if not licence or (photo.get("original_dimensions") or {}).get("width", 0) < MIN_WIDTH:
                continue
            author = re.sub(r"^\(c\)\s*|,\s*some rights reserved.*$|\s*\(CC.*$", "", photo.get("attribution", ""))
            results.append({
                "title": (observation.get("taxon") or {}).get("preferred_common_name") or spec["taxon"],
                "author": author.strip() or "Unknown author",
                "licence": licence,
                "source": "inaturalist",
                "page": f"https://www.inaturalist.org/observations/{observation['id']}",
                "image": photo["url"].replace("/square.", "/large."),
            })
            break
    return results[:limit]


SEARCHES = {"commons": search_commons, "openverse": search_openverse, "inaturalist": search_inaturalist}


def search(entry, limit: int) -> list[dict]:
    spec = {"source": "commons", "q": entry} if isinstance(entry, str) else entry
    return SEARCHES[spec.get("source", "commons")](spec, limit)


def download(candidate: dict) -> Image.Image:
    image = ImageOps.exif_transpose(Image.open(BytesIO(_get(candidate["image"])))).convert("RGB")
    if image.width > MAX_WIDTH:
        image = image.resize((MAX_WIDTH, round(image.height * MAX_WIDTH / image.width)), Image.LANCZOS)
    return image


def contact_sheet(key: str, images: list[Image.Image], path: Path) -> None:
    tile = 360
    sheet = Image.new("RGB", (tile * len(images), tile + 40), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(28)
    for i, image in enumerate(images):
        sheet.paste(ImageOps.contain(image, (tile - 10, tile - 10)), (i * tile + 5, 5))
        draw.text((i * tile + 10, tile + 4), f"{key} #{i}", fill="black", font=font)
    sheet.save(path, quality=80)


def credit_line(c: dict) -> str:
    source = SOURCE_NAMES.get(c.get("source", "commons"), c.get("source", "").title())
    return f"Photo: {c['author']}, [{c['licence']}]({c['page']}), via {source}."


def main() -> None:
    global DELAY
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest")
    parser.add_argument("out_dir")
    parser.add_argument("--candidates", type=int, default=0, help="Review mode: candidates per key")
    parser.add_argument("--from-review", metavar="DIR", help="Install picks from a review directory")
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

        def credit(match: re.Match) -> str:
            c = credits.get(match.group(1))
            return credit_line(c) if c else match.group(0)

        filled = re.sub(r"\{\{credit:([a-z0-9-]+)\}\}", credit, post.read_text())
        post.write_text(filled)
        missing = re.findall(r"\{\{credit:([a-z0-9-]+)\}\}", filled)
        print(f"credits filled; missing: {missing or 'none'}")
        return

    for key in keys:
        if args.candidates:
            if (out / f"{key}.json").exists():
                continue
            try:
                found = search(manifest[key], args.candidates)
            except (urllib.error.URLError, TimeoutError) as error:
                print(f"{key}: search failed: {error}", flush=True)
                continue
            kept, images = [], []
            for candidate in found:
                try:
                    image = download(candidate)
                except (urllib.error.URLError, TimeoutError, OSError) as error:
                    print(f"  skipped {candidate['title']}: {error}", flush=True)
                    continue
                image.save(out / f"{key}-{len(kept)}.jpg", quality=88)
                kept.append(candidate)
                images.append(image)
            (out / f"{key}.json").write_text(json.dumps(kept, indent=2, ensure_ascii=False))
            if images:
                contact_sheet(key, images, out / f"{key}-sheet.jpg")
            print(f"{key}: {len(images)} candidates", flush=True)
            continue

        index = int(picks.get(key, 0))
        if args.from_review:
            review = Path(args.from_review)
            found = json.loads((review / f"{key}.json").read_text())
            if len(found) <= index:
                print(f"{key}: no candidate #{index} in review", flush=True)
                continue
            shutil.copyfile(review / f"{key}-{index}.jpg", out / f"{key}.jpg")
            chosen = found[index]
        else:
            found = search(manifest[key], index + 1)
            if len(found) <= index:
                print(f"{key}: no acceptable image found", flush=True)
                continue
            chosen = found[index]
            download(chosen).save(out / f"{key}.jpg", quality=88)
        credits[key] = {k: chosen[k] for k in ("title", "author", "licence", "source", "page")}
        print(f"{key}: {chosen['title']} ({chosen['licence']}, {chosen['author']})", flush=True)

    if not args.candidates:
        credits_path.write_text(json.dumps(credits, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
