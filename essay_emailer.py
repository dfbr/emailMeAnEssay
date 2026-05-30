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
        - The text and image models can be overridden with --text-model and
            --image-model; their defaults are gpt-5.5 and gpt-image-2.
    - The run prompt is logged to output/logs/essay_runs.jsonl.
        - Detailed script events, retries, and outcomes are logged to
            output/logs/script_events.jsonl.
        - OpenAI response IDs, token usage, and elapsed time are captured in
            both logs when available.
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
from urllib.parse import quote, unquote, urlparse

import markdown
import httpx
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
    *,
    status: str,
    run_id: str,
    title: str | None,
    user_prompt: str,
    output_path: str | None,
    text_model: str,
    image_model: str,
    response_id: str | None = None,
    usage: dict[str, Any] | None = None,
    elapsed_seconds: float | None = None,
    error: str | None = None,
) -> Path:
    log_path = Path("output") / "logs" / "essay_runs.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": run_id,
        "status": status,
        "title": title,
        "user_prompt": user_prompt,
        "output_path": output_path,
        "text_model": text_model,
        "image_model": image_model,
    }
    if response_id is not None:
        entry["response_id"] = response_id
    if usage is not None:
        entry["usage"] = usage
    if elapsed_seconds is not None:
        entry["elapsed_seconds"] = round(elapsed_seconds, 3)
    if error:
        entry["error"] = error
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return log_path


def _log_script_event(run_id: str, event: str, **details: Any) -> Path:
    log_path = Path("output") / "logs" / "script_events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": run_id,
        "event": event,
        **details,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return log_path


def _usage_summary(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        dumped = usage.model_dump()
    elif isinstance(usage, dict):
        dumped = usage
    else:
        dumped = {
            name: getattr(usage, name)
            for name in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "input_tokens_details",
                "output_tokens_details",
            )
            if hasattr(usage, name)
        }

    summary: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens", "input_tokens_details", "output_tokens_details"):
        if key in dumped and dumped[key] is not None:
            summary[key] = dumped[key]
    return summary or None


def _is_insufficient_quota_error(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) != 429:
        return False
    if getattr(exc, "code", None) == "insufficient_quota":
        return True
    if getattr(exc, "type", None) == "insufficient_quota":
        return True
    message = str(exc)
    return "insufficient_quota" in message or "exceeded your current quota" in message


# ---------------------------------------------------------------------------
# Essay generation
# ---------------------------------------------------------------------------

def _responses_timeout(text_model: str | None = None) -> float:
    if text_model and text_model.startswith("gpt-5"):
        return 600.0
    return 120.0


def _build_openai_client(api_key: str, text_model: str | None = None) -> OpenAI:
    request_timeout = _responses_timeout(text_model)
    http_client = httpx.Client(
        http2=False,
        trust_env=False,
        timeout=httpx.Timeout(request_timeout, connect=30.0),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=0),
    )
    return OpenAI(api_key=api_key, timeout=request_timeout, max_retries=0, http_client=http_client)


def _build_openai_image_client(api_key: str) -> OpenAI:
    # Image generation can take longer to produce the first bytes than text responses.
    request_timeout = 300.0
    http_client = httpx.Client(
        http2=False,
        trust_env=False,
        timeout=httpx.Timeout(request_timeout, connect=30.0),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=0),
    )
    return OpenAI(api_key=api_key, timeout=request_timeout, max_retries=0, http_client=http_client)


def _error_details(exc: Exception) -> str:
    message = str(exc)
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        cause_message = str(cause)
        if cause_message and cause_message != message:
            return f"{message} | cause={cause.__class__.__name__}: {cause_message}"
    return message


def _responses_request_kwargs(text_model: str) -> dict[str, Any]:
    request_kwargs: dict[str, Any] = {
        "text": {"verbosity": "medium"},
    }
    if text_model.startswith("gpt-5"):
        request_kwargs["reasoning"] = {"effort": "low"}
    return request_kwargs


def generate_essay(
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    text_model: str,
    run_id: str,
) -> tuple[str, str, dict[str, Any]]:
    """Call the OpenAI Responses API and return *(title, markdown_content)*."""
    print(f"Generating essay with OpenAI using {text_model}…")
    _log_script_event(
        run_id,
        "essay_generation_start",
        text_model=text_model,
        prompt_length=len(user_prompt),
    )
    started_at = time.monotonic()
    response = _retry_openai_call(
        lambda: _generate_essay_once(
            api_key=api_key,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            text_model=text_model,
        ),
        action_name="essay generation",
        run_id=run_id,
    )
    content = response.output_text or ""
    title = _extract_title(content)
    usage = _usage_summary(getattr(response, "usage", None))
    elapsed_seconds = time.monotonic() - started_at
    response_id = getattr(response, "id", None)
    _log_script_event(
        run_id,
        "essay_generation_success",
        text_model=text_model,
        title=title,
        output_chars=len(content),
        response_id=response_id,
        usage=usage,
        elapsed_seconds=round(elapsed_seconds, 3),
    )
    return title, content, {
        "response_id": response_id,
        "usage": usage,
        "elapsed_seconds": elapsed_seconds,
    }


def _generate_essay_once(
    *,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    text_model: str,
):
    with _build_openai_client(api_key, text_model) as client:
        return client.responses.create(
            model=text_model,
            instructions=system_prompt,
            input=user_prompt,
            **_responses_request_kwargs(text_model),
        )


def _retry_openai_call(
    callable_fn,
    *,
    action_name: str,
    run_id: str | None = None,
    attempts: int = 4,
) -> Any:
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
            if run_id:
                _log_script_event(
                    run_id,
                    f"{action_name.replace(' ', '_')}_retry",
                    attempt=attempt,
                    delay_seconds=delay_seconds,
                    error_type=exc.__class__.__name__,
                    error_message=str(exc),
                    error_details=_error_details(exc),
                )
        except Exception as exc:
            if _is_insufficient_quota_error(exc):
                if run_id:
                    _log_script_event(
                        run_id,
                        f"{action_name.replace(' ', '_')}_quota_exceeded",
                        status_code=getattr(exc, "status_code", 429),
                        error_message=str(exc),
                    )
                raise
            status_code = getattr(exc, "status_code", None)
            if status_code not in {429, 500, 502, 503, 504} or attempt == attempts:
                raise
            print(
                f"  Warning: {action_name} got HTTP {status_code}; retrying in {delay_seconds:.1f}s…"
            )
            if run_id:
                _log_script_event(
                    run_id,
                    f"{action_name.replace(' ', '_')}_retry",
                    attempt=attempt,
                    delay_seconds=delay_seconds,
                    status_code=status_code,
                    error_message=str(exc),
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


_TRUSTED_IMAGE_HOSTS = (
    "upload.wikimedia.org",
    "commons.wikimedia.org",
    "wikipedia.org",
    "wikimedia.org",
)


def _is_trusted_image_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    if parsed.scheme != "https" or not host:
        return False
    return any(host == trusted or host.endswith(f".{trusted}") for trusted in _TRUSTED_IMAGE_HOSTS)


def _default_image_title(url: str) -> str:
    name = Path(urlparse(url).path).name
    return name or "Referenced image"


def _commons_file_name_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path

    if host == "commons.wikimedia.org":
        if path.startswith("/wiki/File:"):
            file_name = unquote(path[len("/wiki/File:"):]).strip()
            return file_name or None
        if path.startswith("/wiki/Special:FilePath/"):
            file_name = unquote(path[len("/wiki/Special:FilePath/"):]).strip()
            return file_name or None
        if path.startswith("/wiki/Special:Redirect/file/"):
            file_name = unquote(path[len("/wiki/Special:Redirect/file/"):]).strip()
            return file_name or None

    if host == "upload.wikimedia.org":
        name = unquote(Path(path).name).strip()
        return name or None

    return None


def _resolve_commons_file_url(file_name: str) -> tuple[str, str] | None:
    params = {
        "action": "query",
        "format": "json",
        "titles": f"File:{file_name}",
        "prop": "imageinfo",
        "iiprop": "url",
    }
    try:
        resp = _rate_limited_get("https://commons.wikimedia.org/w/api.php", params=params)
        data = resp.json()
    except Exception:
        return None

    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        if "missing" in page:
            return None
        title = (page.get("title") or "").replace("File:", "").strip()
        for info in page.get("imageinfo", []):
            url = info.get("url")
            if url:
                return url, (title or file_name)
    return None


def _normalize_image_url(url: str) -> str | None:
    if not _is_trusted_image_url(url):
        return None
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path
    if host == "commons.wikimedia.org" and path.startswith("/wiki/File:"):
        file_name = unquote(path[len("/wiki/File:"):]).strip()
        if not file_name:
            return None
        return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(file_name)}"
    return url


def _resolve_image_reference(url: str, title: str) -> dict[str, str] | None:
    normalized = _normalize_image_url(url)
    if not normalized:
        return None

    parsed = urlparse(normalized)
    host = (parsed.netloc or "").lower()
    if host.endswith("wikimedia.org") or host.endswith("wikipedia.org"):
        file_name = _commons_file_name_from_url(normalized)
        if file_name:
            resolved = _resolve_commons_file_url(file_name)
            if resolved:
                resolved_url, resolved_title = resolved
                return {
                    "url": resolved_url,
                    "title": title.strip() or resolved_title,
                }
            return None

    return {
        "url": normalized,
        "title": title.strip() or _default_image_title(normalized),
    }


def extract_image_urls(content: str) -> list[dict[str, str]]:
    """Return trusted direct image URLs embedded in markdown or hidden comments."""
    seen: set[str] = set()
    refs: list[dict[str, str]] = []

    for alt, url in re.findall(r"!\[([^\]]*)\]\((https?://[^)\s]+)(?:\s+\&quot;[^\&]*\&quot;|\s+\"[^\"]*\")?\)", content):
        resolved = _resolve_image_reference(url.strip(), alt.strip())
        if not resolved:
            continue
        cleaned_url = resolved["url"]
        if cleaned_url in seen:
            continue
        seen.add(cleaned_url)
        refs.append(resolved)

    for url in re.findall(r"<!--\s*IMAGE_URL:\s*(https?://.+?)\s*-->", content, re.IGNORECASE):
        resolved = _resolve_image_reference(url.strip(), "")
        if not resolved:
            continue
        cleaned_url = resolved["url"]
        if cleaned_url in seen:
            continue
        seen.add(cleaned_url)
        refs.append(resolved)

    return refs[:5]


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

def _wikimedia_query_variants(query: str) -> list[str]:
    variants: list[str] = []

    def _add(value: str) -> None:
        cleaned = re.sub(r"\s+", " ", value).strip(" ,.-")
        if cleaned and cleaned not in variants:
            variants.append(cleaned)

    _add(query)

    lowered = query.lower()
    for separator in (" with ", " showing ", " featuring ", " depicting ", " celebrating ", " and beyond", ","):
        if separator in lowered:
            prefix = query[:lowered.index(separator)]
            _add(prefix)

    token_stopwords = {
        "a", "an", "and", "at", "between", "beyond", "by", "during", "for",
        "from", "in", "into", "of", "on", "showing", "the", "to", "visible", "with",
    }
    tokens = re.findall(r"[A-Za-z0-9']+", query)
    keyword_tokens = [
        token
        for token in tokens
        if token.lower() not in token_stopwords and (token.isdigit() or len(token) >= 4 or token.upper() == token)
    ]
    _add(" ".join(keyword_tokens[:8]))

    years = [token for token in tokens if re.fullmatch(r"\d{4}", token)]
    if years and {"fifa", "world", "cup"}.issubset({token.lower() for token in tokens}):
        _add(f"{years[0]} FIFA World Cup")

    return variants[:5]


def _search_wikimedia_images_once(query: str, max_images: int, *, use_bitmap_filter: bool) -> list[dict]:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrnamespace": "6",
        "gsrsearch": f"filetype:bitmap {query}" if use_bitmap_filter else query,
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


def search_wikimedia_images(query: str, max_images: int = 2) -> list[dict]:
    """Search Wikimedia Commons for freely-licensed bitmap images."""
    print(f"  Searching Wikimedia Commons for: {query!r}")
    variants = _wikimedia_query_variants(query)
    seen_titles: set[str] = set()
    results: list[dict] = []
    for variant in variants:
        for use_bitmap_filter in (True, False):
            for item in _search_wikimedia_images_once(variant, max_images=max_images, use_bitmap_filter=use_bitmap_filter):
                title = item["title"]
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                results.append(item)
                if len(results) >= max_images:
                    return results
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


def _rewrite_markdown_image_urls(content_md: str, url_map: dict[str, str]) -> str:
    if not url_map:
        return content_md

    def _replace(match: re.Match) -> str:
        alt_text = match.group(1)
        source_url = match.group(2).strip()
        mapped = url_map.get(source_url)
        if not mapped:
            return match.group(0)
        return f"![{alt_text}]({mapped})"

    return re.sub(r"!\[([^\]]*)\]\((https?://[^)\s]+)(?:\s+\&quot;[^\&]*\&quot;|\s+\"[^\"]*\")?\)", _replace, content_md)


def fetch_content_images(
    image_urls: list[dict[str, str]],
    image_queries: list[str],
    fallback_topics: list[str],
    run_id: str,
) -> list[dict]:
    """Download one image per query, preferring model-provided image suggestions."""
    print("Fetching content images…")
    direct_urls = [item["url"] for item in image_urls]
    queries = _unique_preserving_order(image_queries) if image_queries else _unique_preserving_order(fallback_topics)
    _log_script_event(
        run_id,
        "content_image_search_start",
        direct_image_urls=direct_urls,
        suggested_queries=image_queries,
        fallback_topics=fallback_topics,
        active_queries=queries,
    )
    images: list[dict] = []
    seen_titles: set[str] = set()
    if image_urls:
        for ref in image_urls:
            raw = _download_bytes(ref["url"])
            if not raw:
                continue
            jpeg = _to_jpeg(raw)
            if not jpeg:
                continue
            title = ref["title"] or _default_image_title(ref["url"])
            seen_titles.add(title)
            images.append(
                {
                    "bytes": jpeg,
                    "title": title,
                    "inline": True,
                    "source_url": ref["url"],
                }
            )
            if len(images) >= 5:
                break
        if len(images) >= 5:
            _log_script_event(
                run_id,
                "content_image_search_complete",
                image_count=len(images),
                query_count=0,
                direct_url_count=len(direct_urls),
            )
            return images

    for query in queries:
        for meta in search_wikimedia_images(query, max_images=1):
            if meta["title"] in seen_titles:
                continue
            raw = _download_bytes(meta["url"])
            if raw:
                jpeg = _to_jpeg(raw)
                if jpeg:
                    seen_titles.add(meta["title"])
                    images.append({"bytes": jpeg, "title": meta["title"]})
                    break  # one image per query
        if len(images) >= 5:
            break
    _log_script_event(
        run_id,
        "content_image_search_complete",
        image_count=len(images),
        query_count=len(queries),
        direct_url_count=len(direct_urls),
    )
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

def generate_cover_image(api_key: str, title: str, image_model: str, run_id: str) -> bytes:
    """Generate a cover via the GPT image API, falling back to a Pillow-drawn cover."""
    print(f"Generating cover image with {image_model}…")
    _log_script_event(run_id, "cover_generation_start", image_model=image_model, title=title)
    started_at = time.monotonic()
    try:
        prompt = (
            f"Professional book cover art for an essay titled '{title}'. "
            "Elegant design with thematically relevant imagery, no text."
        )
        resp = _retry_openai_call(
            lambda: _generate_cover_once(api_key=api_key, prompt=prompt, image_model=image_model),
            action_name="cover generation",
            run_id=run_id,
            attempts=5,
        )
        raw = _extract_generated_image_bytes(resp)
        if raw:
            elapsed_seconds = time.monotonic() - started_at
            _log_script_event(
                run_id,
                "cover_generation_success",
                image_model=image_model,
                bytes=len(raw),
                elapsed_seconds=round(elapsed_seconds, 3),
                response_id=getattr(resp, "id", None),
            )
            return raw
    except Exception as exc:
        print(f"  Warning: cover generation failed – {exc}")
        _log_script_event(run_id, "cover_generation_failed", image_model=image_model, error=str(exc))

    _log_script_event(run_id, "cover_generation_fallback", image_model=image_model)
    return _make_fallback_cover(title)


def _generate_cover_once(*, api_key: str, prompt: str, image_model: str):
    with _build_openai_image_client(api_key) as client:
        return client.images.generate(
            model=image_model,
            prompt=prompt,
            size="1024x1024",
        )


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
    run_id: str,
) -> bytes:
    """Build an EPUB and return the raw file bytes."""
    print("Creating EPUB…")
    _log_script_event(run_id, "epub_build_start", title=title, content_image_count=len(content_images))

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
    epub_bytes: bytes
    try:
        epub.write_epub(tmp_path, book)
        epub_bytes = Path(tmp_path).read_bytes()
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    _log_script_event(run_id, "epub_build_complete", title=title, content_image_count=len(content_images))
    return epub_bytes


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
    run_id: str,
) -> bool:
    """Attach the EPUB to an email and send it via SMTP."""
    print(f"Sending email to {recipient}…")
    _log_script_event(run_id, "email_send_start", recipient=recipient, smtp_host=smtp_host, smtp_port=smtp_port)

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
        _log_script_event(run_id, "email_send_failed", recipient=recipient, smtp_host=smtp_host, smtp_port=smtp_port, error="SMTPAuthenticationError")
        return False
    except Exception as exc:
        print(f"  Warning: email send failed – {exc}")
        _log_script_event(run_id, "email_send_failed", recipient=recipient, smtp_host=smtp_host, smtp_port=smtp_port, error=str(exc))
        return False

    print("Email sent successfully.")
    _log_script_event(run_id, "email_send_success", recipient=recipient, smtp_host=smtp_host, smtp_port=smtp_port)
    return True


def _slugify_filename(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text).strip().lower()
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug[:80] or "essay"


def _save_outputs(
    title: str,
    epub_bytes: bytes,
    content_md: str,
    output_path: str | None,
    run_id: str,
) -> tuple[Path, Path]:
    if output_path:
        requested_path = Path(output_path)
        if requested_path.suffix:
            output_dir = requested_path.with_suffix("")
            output_stem = requested_path.stem
        else:
            output_dir = requested_path
            output_stem = requested_path.name or _slugify_filename(title)
    else:
        output_stem = _slugify_filename(title)
        output_dir = Path("output") / output_stem

    output_dir.mkdir(parents=True, exist_ok=True)
    epub_path = output_dir / f"{output_stem}.epub"

    epub_path.write_bytes(epub_bytes)

    md_path = output_dir / f"{output_stem}.md"
    md_path.write_text(content_md, encoding="utf-8")

    print(f"EPUB saved to: {epub_path}")
    print(f"Essay markdown saved to: {md_path}")
    _log_script_event(
        run_id,
        "local_save_complete",
        output_dir=str(output_dir),
        epub_path=str(epub_path),
        md_path=str(md_path),
    )
    return epub_path, md_path


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
        help="Use a simple generated cover instead of the image model",
    )
    p.add_argument(
        "--output",
        metavar="FILE",
        help="Also save the EPUB and markdown into a dedicated subdirectory at this path",
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

    print(
        f"Using text model {args.text_model} and image model {args.image_model}. "
        "If the API is slow, the script will retry transient connection failures."
    )

    run_id = str(uuid.uuid4())
    _log_script_event(
        run_id,
        "run_start",
        text_model=args.text_model,
        image_model=args.image_model,
        no_images=args.no_images,
        no_cover=args.no_cover,
        no_email=args.no_email,
        output=args.output,
    )
    prompt_log_path = _log_essay_run(
        status="started",
        run_id=run_id,
        title=None,
        user_prompt=args.user_prompt,
        output_path=args.output,
        text_model=args.text_model,
        image_model=args.image_model,
    )
    print(f"Essay prompt logged to: {prompt_log_path}")

    # 1 – Generate essay
    try:
        title, content_md, essay_meta = generate_essay(
            openai_key,
            args.system_prompt,
            args.user_prompt,
            args.text_model,
            run_id,
        )
    except Exception as exc:
        if _is_insufficient_quota_error(exc):
            print(
                "Error: OpenAI quota is exhausted for this account. Use a cheaper model for testing, "
                "or add billing/quota before running GPT-5.5 essays.",
                file=sys.stderr,
            )
        _log_essay_run(
            status="failed",
            run_id=run_id,
            title=None,
            user_prompt=args.user_prompt,
            output_path=args.output,
            text_model=args.text_model,
            image_model=args.image_model,
            error=str(exc),
        )
        _log_script_event(run_id, "run_failed", stage="essay_generation", error=str(exc))
        print(
            "Error: essay generation failed after retries. The prompt was logged and you can rerun safely.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f'Title: "{title}"')

    # 2 – Fetch content images
    content_images: list[dict] = []
    if not args.no_images:
        image_urls = extract_image_urls(content_md)
        image_queries = extract_image_suggestions(content_md)
        fallback_topics = extract_key_topics(title, content_md)
        content_images = fetch_content_images(image_urls, image_queries, fallback_topics, run_id)
        inline_url_map: dict[str, str] = {}
        for idx, item in enumerate(content_images):
            source_url = item.get("source_url")
            if source_url:
                inline_url_map[source_url] = f"images/content_{idx}.jpg"
        if inline_url_map:
            content_md = _rewrite_markdown_image_urls(content_md, inline_url_map)
        print(f"  {len(content_images)} content image(s) collected.")
    else:
        _log_script_event(run_id, "content_image_search_skipped", reason="--no-images")

    # 3 – Generate cover
    if args.no_cover:
        cover_bytes = _make_fallback_cover(title)
        _log_script_event(run_id, "cover_generation_skipped", reason="--no-cover")
    else:
        cover_bytes = generate_cover_image(openai_key, title, args.image_model, run_id)

    # 4 – Build EPUB
    epub_bytes = create_epub(
        title,
        content_md,
        cover_bytes,
        content_images,
        args.user_prompt,
        args.text_model,
        args.image_model,
        run_id,
    )

    # 5 – Save locally
    epub_path, md_path = _save_outputs(title, epub_bytes, content_md, args.output, run_id)
    _log_essay_run(
        status="completed",
        run_id=run_id,
        title=title,
        user_prompt=args.user_prompt,
        output_path=str(epub_path),
        text_model=args.text_model,
        image_model=args.image_model,
        response_id=essay_meta.get("response_id"),
        usage=essay_meta.get("usage"),
        elapsed_seconds=essay_meta.get("elapsed_seconds"),
    )

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
            run_id=run_id,
        )
    else:
        _log_script_event(run_id, "email_send_skipped", reason="--no-email")


if __name__ == "__main__":
    main()
