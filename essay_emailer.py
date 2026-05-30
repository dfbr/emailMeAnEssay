#!/usr/bin/env python3
"""
emailMeAnEssay – Generate an essay with OpenAI, bundle it as an EPUB
(with a GPT-generated cover or Pillow fallback and relevant image
placements), save the output locally, then email it to a recipient –
ideal for sending directly to a Kindle address.

Usage:
    python essay_emailer.py --user-prompt "Write an essay about stoicism" \
        --email yourname_kindle@kindle.com

Environment variables (or .env file):
    OPENAI_API_KEY   – required
    SMTP_HOST        – required (e.g. smtp.gmail.com)
    SMTP_PORT        – optional, default 587
    SMTP_USER        – required
    SMTP_PASSWORD    – required
    FROM_EMAIL       – optional, defaults to SMTP_USER
    TO_EMAIL         – optional, recipient email address

Runtime notes:
    - The essay body is generated with GPT-5.5 via the Responses API.
    - The run prompt is logged to output/logs/essay_runs.jsonl.
    - The generated EPUB metadata includes the user prompt.
    - Content images prefer model-provided image suggestions, then fall
      back to rate-limited Wikimedia Commons lookups.
"""

from __future__ import annotations

import argparse
import base64
import json
import io
import os
import re
import smtplib
import sys
import tempfile
import time
import uuid
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import markdown
import requests
from dotenv import load_dotenv
from ebooklib import epub
from openai import APIConnectionError, APITimeoutError, OpenAI
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

load_dotenv()


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"Error: environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return value


def _require_recipient_email(cli_email: str | None) -> str:
    recipient = cli_email or os.getenv("TO_EMAIL")
    if not recipient:
        print(
            "Error: recipient email is not set. Provide --email or TO_EMAIL.",
            file=sys.stderr,
        )
        sys.exit(1)
    return recipient


def _load_default_system_prompt() -> str:
    prompt_path = Path(__file__).with_name("system_prompt.txt")
    try:
        return prompt_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        print(
            f"Error: system prompt file not found: {prompt_path}",
            file=sys.stderr,
        )
        sys.exit(1)


def _log_essay_run(
    title: str,
    user_prompt: str,
    output_path: str | None,
    text_model: str,
    image_model: str,
) -> Path:
    log_path = Path("output") / "logs" / "essay_runs.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "title": title,
        "user_prompt": user_prompt,
        "output_path": output_path,
        "text_model": text_model,
        "image_model": image_model,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return log_path


# ---------------------------------------------------------------------------
# Essay generation
# ---------------------------------------------------------------------------

def generate_essay(
    client: OpenAI,
    system_prompt: str,
    user_prompt: str,
    text_model: str,
) -> tuple[str, str]:
    """Call the OpenAI Responses API and return *(title, markdown_content)*."""
    print("Generating essay with OpenAI…")
    response = _retry_openai_call(
        lambda: client.responses.create(
            model=text_model,
            instructions=system_prompt,
            input=user_prompt,
            reasoning={"effort": "medium"},
            text={"verbosity": "medium"},
        ),
        action_name="essay generation",
    )
    content = response.output_text or ""
    title = _extract_title(content)
    return title, content


def _retry_openai_call(callable_fn, *, action_name: str, attempts: int = 4) -> Any:
    delay_seconds = 1.0
    for attempt in range(1, attempts + 1):
        try:
            return callable_fn()
        except (APIConnectionError, APITimeoutError) as exc:
            if attempt == attempts:
                raise
            print(
                f"  Warning: {action_name} failed ({exc.__class__.__name__}); retrying in {delay_seconds:.1f}s…"
            )
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code not in {429, 500, 502, 503, 504} or attempt == attempts:
                raise
            print(
                f"  Warning: {action_name} got HTTP {status_code}; retrying in {delay_seconds:.1f}s…"
            )

        time.sleep(delay_seconds)
        delay_seconds = min(delay_seconds * 2, 8.0)


def _extract_title(content: str) -> str:
    """Return the first Markdown H1 heading, or the first non-empty line."""
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
        if line:
            # Strip any leading # characters that don't have a space
            return re.sub(r"^#+\s*", "", line)[:80]
    return "Essay"


def extract_key_topics(title: str, content: str) -> list[str]:
    """Return up to three search topics derived from the essay."""
    topics = [title]
    headings = re.findall(r"^#{1,3}\s+(.+)$", content, re.MULTILINE)
    topics.extend(h.strip() for h in headings if h.strip())
    # De-duplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for t in topics:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique[:3]


def extract_image_suggestions(content: str) -> list[str]:
    """Return concise image suggestions embedded by the model in HTML comments."""
    suggestions = re.findall(r"<!--\s*IMAGE:\s*(.+?)\s*-->", content, re.IGNORECASE)
    seen: set[str] = set()
    unique: list[str] = []
    for suggestion in suggestions:
        cleaned = suggestion.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)
    return unique[:5]


_WIKIMEDIA_LAST_REQUEST_AT = 0.0
_WIKIMEDIA_MIN_REQUEST_INTERVAL = 1.0


def _rate_limited_get(url: str, *, params: dict | None = None, stream: bool = False) -> requests.Response:
    global _WIKIMEDIA_LAST_REQUEST_AT

    delay = _WIKIMEDIA_MIN_REQUEST_INTERVAL - (time.monotonic() - _WIKIMEDIA_LAST_REQUEST_AT)
    if delay > 0:
        time.sleep(delay)

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(
                url,
                params=params,
                timeout=15 if not stream else 20,
                headers={"User-Agent": "emailMeAnEssay/1.0 (educational tool)" if "commons.wikimedia.org" in url else "emailMeAnEssay/1.0"},
                stream=stream,
            )
            _WIKIMEDIA_LAST_REQUEST_AT = time.monotonic()
            if resp.status_code == 429:
                raise requests.HTTPError("429 Too Many Requests", response=resp)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_exc = exc
            if attempt == 2:
                raise
            time.sleep(0.75 * (attempt + 1))

    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Image sourcing – Wikimedia Commons (no API key required)
# ---------------------------------------------------------------------------

def search_wikimedia_images(query: str, max_images: int = 2) -> list[dict]:
    """Search Wikimedia Commons for freely-licensed bitmap images."""
    print(f"  Searching Wikimedia Commons for: {query!r}")
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrnamespace": "6",
        "gsrsearch": f"filetype:bitmap {query}",
        "gsrlimit": str(max_images * 4),
        "prop": "imageinfo",
        "iiprop": "url|size|mediatype",
        "iiurlwidth": "800",
    }
    try:
        resp = _rate_limited_get("https://commons.wikimedia.org/w/api.php", params=params)
        data = resp.json()
    except Exception as exc:
        print(f"  Warning: image search failed – {exc}")
        return []

    results: list[dict] = []
    for page in data.get("query", {}).get("pages", {}).values():
        if len(results) >= max_images:
            break
        for info in page.get("imageinfo", []):
            if info.get("mediatype", "").lower() == "bitmap":
                url = info.get("thumburl") or info.get("url", "")
                if url:
                    results.append(
                        {
                            "url": url,
                            "title": page.get("title", "").replace("File:", "").strip(),
                        }
                    )
                    break
    return results


def _download_bytes(url: str) -> bytes | None:
    """Download a URL and return raw bytes, or *None* on failure."""
    try:
        resp = _rate_limited_get(url, stream=True)
        if "image" in resp.headers.get("content-type", ""):
            return resp.content
    except Exception as exc:
        print(f"  Warning: download failed – {exc}")
    return None


def fetch_content_images(image_queries: list[str], fallback_topics: list[str]) -> list[dict]:
    """Download one image per query, preferring model-provided image suggestions."""
    print("Fetching content images…")
    images: list[dict] = []
    queries = _unique_preserving_order(image_queries + fallback_topics)
    for query in queries:
        for meta in search_wikimedia_images(query, max_images=1):
            raw = _download_bytes(meta["url"])
            if raw:
                jpeg = _to_jpeg(raw)
                if jpeg:
                    images.append({"bytes": jpeg, "title": meta["title"]})
                    break  # one image per query
        if len(images) >= 5:
            break
    return images


def _unique_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)
    return unique


def _to_jpeg(raw: bytes) -> bytes | None:
    """Convert raw image bytes to JPEG, returning *None* on failure."""
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as exc:
        print(f"  Warning: image conversion failed – {exc}")
        return None


# ---------------------------------------------------------------------------
# Cover image
# ---------------------------------------------------------------------------

def generate_cover_image(client: OpenAI, title: str) -> bytes:
    """Generate a cover via the GPT image API, falling back to a Pillow-drawn cover."""
    print("Generating cover image…")

    raise NotImplementedError


def generate_cover_image(client: OpenAI, title: str, image_model: str) -> bytes:
    """Generate a cover via the GPT image API, falling back to a Pillow-drawn cover."""
    print("Generating cover image…")
    try:
        prompt = (
            f"Professional book cover art for an essay titled '{title}'. "
            "Elegant design with thematically relevant imagery, no text."
        )
        resp = _retry_openai_call(
            lambda: client.images.generate(
                model=image_model,
                prompt=prompt,
                size="1024x1024",
                quality="high",
            ),
            action_name="cover generation",
        )
        raw = _extract_generated_image_bytes(resp)
        if raw:
            return raw
    except Exception as exc:
        print(f"  Warning: cover generation failed – {exc}")

    return _make_fallback_cover(title)


def _extract_generated_image_bytes(response) -> bytes | None:
    data = getattr(response, "data", None) or []
    if not data:
        return None

    first = data[0]
    b64_json = getattr(first, "b64_json", None)
    if b64_json:
        try:
            return base64.b64decode(b64_json)
        except Exception:
            return None

    url = getattr(first, "url", None)
    if url:
        return _download_bytes(url)

    return None


def _make_fallback_cover(title: str) -> bytes:
    """Create a simple but attractive cover using Pillow."""
    W, H = 1200, 1600
    bg_color = (28, 48, 85)
    accent_color = (210, 185, 100)
    text_color = (255, 248, 210)

    img = Image.new("RGB", (W, H), color=bg_color)
    draw = ImageDraw.Draw(img)

    # Border
    b = 45
    for offset in range(3):
        draw.rectangle([b + offset, b + offset, W - b - offset, H - b - offset],
                       outline=accent_color)

    # Load fonts with graceful fallback
    def _font(size: int, bold: bool = False) -> Any:
        candidates = [
            f"/usr/share/fonts/truetype/dejavu/DejaVuSerif{'Bold' if bold else ''}.ttf",
            f"/usr/share/fonts/truetype/liberation/LiberationSerif-{'Bold' if bold else 'Regular'}.ttf",
        ]
        for path in candidates:
            if Path(path).exists():
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    pass
        return ImageFont.load_default()

    title_font = _font(90, bold=True)
    sub_font = _font(40)

    # Word-wrap title to ~18 chars per line
    words = title.split()
    lines: list[str] = []
    cur: list[str] = []
    for word in words:
        cur.append(word)
        if len(" ".join(cur)) > 18:
            lines.append(" ".join(cur[:-1]))
            cur = [word]
    if cur:
        lines.append(" ".join(cur))

    # Draw title centred vertically in upper half
    line_h = 110
    start_y = H // 3 - (len(lines) * line_h) // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=title_font)
        w = bbox[2] - bbox[0]
        draw.text(((W - w) // 2, start_y), line, font=title_font, fill=text_color)
        start_y += line_h

    # Divider
    draw.line([(W // 4, H // 2 + 40), (3 * W // 4, H // 2 + 40)],
              fill=accent_color, width=2)

    # Sub-label
    label = "An Essay"
    bbox = draw.textbbox((0, 0), label, font=sub_font)
    w = bbox[2] - bbox[0]
    draw.text(((W - w) // 2, H // 2 + 60), label, font=sub_font, fill=accent_color)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# EPUB creation
# ---------------------------------------------------------------------------

def create_epub(
    title: str,
    content_md: str,
    cover_bytes: bytes,
    content_images: list[dict],
    user_prompt: str,
    text_model: str,
    image_model: str,
) -> bytes:
    """Build an EPUB and return the raw file bytes."""
    print("Creating EPUB…")

    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(title)
    book.set_language("en")
    book.add_author("ChatGPT")
    book.add_metadata(
        "DC",
        "description",
        "An AI-generated essay produced by emailMeAnEssay.",
    )
    book.add_metadata("DC", "description", f"User prompt: {user_prompt}")
    book.add_metadata("DC", "description", f"Text model: {text_model}")
    book.add_metadata("DC", "description", f"Image model: {image_model}")
    book.add_metadata("DC", "publisher", "OpenAI")
    book.add_metadata("DC", "subject", "Essay")

    # Cover image
    book.set_cover("images/cover.jpg", cover_bytes)

    # Add content images to the book spine
    epub_img_refs: list[dict] = []
    for i, img in enumerate(content_images):
        item = epub.EpubImage()
        item.uid = f"img-{i}"  # type: ignore[attr-defined]
        item.file_name = f"images/content_{i}.jpg"
        item.media_type = "image/jpeg"
        item.content = img["bytes"]
        book.add_item(item)
        epub_img_refs.append({"file_name": item.file_name, "title": img["title"]})

    # Convert markdown → HTML
    body_html = markdown.markdown(
        content_md,
        extensions=["extra", "tables", "toc"],
    )
    body_html = _inject_images(body_html, epub_img_refs)

    chapter_css = """
        body  { font-family: Georgia, "Times New Roman", serif; line-height: 1.7;
                margin: 1.5em 2em; color: #222; }
        h1    { font-size: 1.8em; color: #1a2e55; border-bottom: 2px solid #1a2e55;
                padding-bottom: 0.3em; margin-top: 1em; }
        h2    { font-size: 1.4em; color: #2c4070; margin-top: 2em; }
        h3    { font-size: 1.15em; color: #3a527a; }
        p     { text-align: justify; margin: 0.6em 0; }
        blockquote { border-left: 4px solid #c0c8d8; padding-left: 1em;
                     color: #444; margin: 1em 0; }
        figure { margin: 1.5em auto; text-align: center; }
        figure img { max-width: 100%; height: auto; border-radius: 4px; }
        figcaption { font-style: italic; color: #666; font-size: 0.88em;
                     margin-top: 0.4em; }
        table  { border-collapse: collapse; width: 100%; margin: 1em 0; }
        th, td { border: 1px solid #ccc; padding: 0.4em 0.7em; }
        th     { background: #eef0f5; }
        code   { font-family: monospace; background: #f4f4f4; padding: 0.1em 0.3em; }
        pre    { background: #f4f4f4; padding: 1em; overflow-x: auto; }
    """

    css_item = epub.EpubItem(
        uid="style",
        file_name="style/main.css",
        media_type="text/css",
        content=chapter_css,
    )
    book.add_item(css_item)

    chapter = epub.EpubHtml(title=title, file_name="chapter.xhtml", lang="en")
    chapter.content = (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        "<head>\n"
        f"  <title>{_xml_escape(title)}</title>\n"
        '  <link rel="stylesheet" type="text/css" href="style/main.css"/>\n'
        "</head>\n"
        "<body>\n"
        f"{body_html}\n"
        "</body>\n"
        "</html>"
    ).encode("utf-8")
    chapter.add_item(css_item)
    book.add_item(chapter)

    book.toc = [epub.Link("chapter.xhtml", title, "chapter")]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", chapter]

    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        epub.write_epub(tmp_path, book)
        return Path(tmp_path).read_bytes()
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
    )


def _inject_images(html: str, images: list[dict]) -> str:
    """Insert image figures after section headings in the HTML."""
    if not images:
        return html

    def _figure(img: dict) -> str:
        caption = (
            f"<figcaption>{_xml_escape(img['title'])}</figcaption>"
            if img["title"]
            else ""
        )
        return (
            f'<figure><img src="{img["file_name"]}" '
            f'alt="{_xml_escape(img["title"])}" />{caption}</figure>'
        )

    # Insert after the first occurrence of </h1>, </h2>, </h3> etc.
    img_iter = iter(images)
    def _replacer(m: re.Match) -> str:
        try:
            img = next(img_iter)
            return m.group(0) + "\n" + _figure(img)
        except StopIteration:
            return m.group(0)

    result = re.sub(r"</h[1-3]>", _replacer, html)

    # Append any remaining images at the end
    remaining = [_figure(img) for img in img_iter]
    if remaining:
        result += "\n" + "\n".join(remaining)

    return result


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------

def send_email(
    epub_bytes: bytes,
    title: str,
    recipient: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_email: str,
) -> bool:
    """Attach the EPUB to an email and send it via SMTP."""
    print(f"Sending email to {recipient}…")

    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = recipient
    msg["Subject"] = title

    msg.attach(
        MIMEText(
            f"Your essay '{title}' is attached as an EPUB.\n\n"
            "— Generated by emailMeAnEssay",
            "plain",
        )
    )

    safe = re.sub(r"[^\w\s\-]", "", title)[:60].strip().replace(" ", "_")
    filename = f"{safe or 'essay'}.epub"

    part = MIMEBase("application", "epub+zip")
    part.set_payload(epub_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(part)

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
                server.login(smtp_user, smtp_password)
                server.sendmail(from_email, recipient, msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.ehlo()
                if smtp_port != 25:
                    server.starttls()
                    server.ehlo()
                server.login(smtp_user, smtp_password)
                server.sendmail(from_email, recipient, msg.as_string())
    except smtplib.SMTPAuthenticationError:
        print(
            "  Warning: SMTP authentication failed. Check SMTP_USER, SMTP_PASSWORD, "
            "and whether your provider requires an app password.",
        )
        return False
    except Exception as exc:
        print(f"  Warning: email send failed – {exc}")
        return False

    print("Email sent successfully.")
    return True


def _slugify_filename(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text).strip().lower()
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug[:80] or "essay"


def _save_outputs(title: str, epub_bytes: bytes, content_md: str, output_path: str | None) -> Path:
    if output_path:
        epub_path = Path(output_path)
    else:
        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)
        epub_path = output_dir / f"{_slugify_filename(title)}.epub"

    epub_path.parent.mkdir(parents=True, exist_ok=True)
    epub_path.write_bytes(epub_bytes)

    md_path = epub_path.with_suffix(".md")
    md_path.write_text(content_md, encoding="utf-8")

    print(f"EPUB saved to: {epub_path}")
    print(f"Essay markdown saved to: {md_path}")
    return epub_path


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    default_system_prompt = _load_default_system_prompt()
    p = argparse.ArgumentParser(
        description=(
            "Generate an essay with OpenAI, create an EPUB with images, "
            "and email it to the specified address."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--text-model",
        default="gpt-5.5",
        metavar="NAME",
        help="Text generation model (default: gpt-5.5)",
    )
    p.add_argument(
        "--image-model",
        default="gpt-image-2",
        metavar="NAME",
        help="Cover image model (default: gpt-image-2)",
    )
    p.add_argument(
        "--system-prompt",
        default=default_system_prompt,
        metavar="TEXT",
        help="System prompt for the AI (default: expert essayist instructions)",
    )
    p.add_argument(
        "--user-prompt",
        required=True,
        metavar="TEXT",
        help="The essay topic, question, or instruction",
    )
    p.add_argument(
        "--email",
        metavar="ADDRESS",
        help="Recipient email address (defaults to TO_EMAIL from .env)",
    )
    p.add_argument(
        "--no-images",
        action="store_true",
        help="Skip embedding content images in the EPUB",
    )
    p.add_argument(
        "--no-cover",
        action="store_true",
        help="Use a simple generated cover instead of DALL-E",
    )
    p.add_argument(
        "--output",
        metavar="FILE",
        help="Also save the EPUB to this local file path",
    )
    p.add_argument(
        "--no-email",
        action="store_true",
        help="Skip sending email (combine with --output to just save locally)",
    )
    return p


def main() -> None:
    args = build_arg_parser().parse_args()

    # Validate required env vars
    openai_key = _require_env("OPENAI_API_KEY")

    smtp_host = smtp_user = smtp_password = from_email = ""
    smtp_port = 587
    recipient_email = ""
    if not args.no_email:
        recipient_email = _require_recipient_email(args.email)
        smtp_host = _require_env("SMTP_HOST")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = _require_env("SMTP_USER")
        smtp_password = _require_env("SMTP_PASSWORD")
        from_email = os.getenv("FROM_EMAIL") or smtp_user

    client = OpenAI(api_key=openai_key)

    # 1 – Generate essay
    title, content_md = generate_essay(
        client,
        args.system_prompt,
        args.user_prompt,
        args.text_model,
    )
    print(f'Title: "{title}"')

    # 2 – Fetch content images
    content_images: list[dict] = []
    if not args.no_images:
        image_queries = extract_image_suggestions(content_md)
        fallback_topics = extract_key_topics(title, content_md)
        content_images = fetch_content_images(image_queries, fallback_topics)
        print(f"  {len(content_images)} content image(s) collected.")

    # 3 – Generate cover
    if args.no_cover:
        cover_bytes = _make_fallback_cover(title)
    else:
        cover_bytes = generate_cover_image(client, title, args.image_model)

    # 4 – Build EPUB
    epub_bytes = create_epub(
        title,
        content_md,
        cover_bytes,
        content_images,
        args.user_prompt,
        args.text_model,
        args.image_model,
    )

    # 5 – Save locally
    _save_outputs(title, epub_bytes, content_md, args.output)
    prompt_log_path = _log_essay_run(
        title,
        args.user_prompt,
        args.output,
        args.text_model,
        args.image_model,
    )
    print(f"Essay prompt logged to: {prompt_log_path}")

    # 6 – Email
    if not args.no_email:
        send_email(
            epub_bytes=epub_bytes,
            title=title,
            recipient=recipient_email,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            from_email=from_email,
        )


if __name__ == "__main__":
    main()
