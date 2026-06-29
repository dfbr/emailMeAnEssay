#!/usr/bin/env python3
"""
emailMeAnEssay – Generate an essay with Google Gemini, bundle it as an EPUB
(with an Imagen-generated cover or Pillow fallback and relevant image
placements), save the output locally, then email it to a recipient –
ideal for sending directly to a Kindle address.

Usage:
    python essay_emailer.py --user-prompt "Write an essay about stoicism" \
        --email yourname_kindle@kindle.com

Environment variables (or .env file):
    GEMINI_API_KEY   – required
    SMTP_HOST        – required (e.g. smtp.gmail.com)
    SMTP_PORT        – optional, default 587
    SMTP_USER        – required
    SMTP_PASSWORD    – required
    FROM_EMAIL       – optional, defaults to SMTP_USER
    TO_EMAIL         – optional, recipient email address
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
from datetime import datetime, timezone
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
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

load_dotenv()

_DEBUG_LOG_ENABLED = False
_DEBUG_LOG_PATH: Path | None = None


def _configure_debug_logging(enabled: bool) -> Path | None:
    global _DEBUG_LOG_ENABLED, _DEBUG_LOG_PATH
    _DEBUG_LOG_ENABLED = enabled
    if not enabled:
        _DEBUG_LOG_PATH = None
        return None

    log_dir = Path("output") / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    daily_name = time.strftime("debug-%Y-%m-%d.log", time.gmtime())
    _DEBUG_LOG_PATH = log_dir / daily_name
    return _DEBUG_LOG_PATH


def _debug_log(event: str, **details: Any) -> None:
    if not _DEBUG_LOG_ENABLED or _DEBUG_LOG_PATH is None:
        return
    entry = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        **details,
    }
    try:
        with _DEBUG_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


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


def _usage_summary(response: Any) -> dict[str, Any] | None:
    if not response or not hasattr(response, 'usage_metadata') or response.usage_metadata is None:
        return None
    meta = response.usage_metadata
    return {
        "input_tokens": getattr(meta, "prompt_token_count", 0),
        "output_tokens": getattr(meta, "candidates_token_count", 0),
        "total_tokens": getattr(meta, "total_token_count", 0),
    }


def _merge_usage_summaries(summaries: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    merged: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        values = [s[key] for s in summaries if s and key in s]
        if values:
            merged[key] = sum(values)
    call_count = sum(1 for s in summaries if s)
    if call_count:
        merged["calls"] = call_count
    return merged or None


# ---------------------------------------------------------------------------
# Gemini API Helpers
# ---------------------------------------------------------------------------

def _build_gemini_client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key)


def _retry_gemini_call(func, action_name: str, run_id: str, attempts: int = 3, deadline_at: float | None = None) -> Any:
    for attempt in range(1, attempts + 1):
        if deadline_at and time.monotonic() >= deadline_at:
            break
        try:
            return func()
        except Exception as exc:
            print(f"Warning: Attempt {attempt} failed for {action_name}: {exc}")
            _log_script_event(run_id, "api_retry_warning", action=action_name, attempt=attempt, error=str(exc))
            if attempt == attempts:
                raise exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed {action_name} after {attempts} attempts")


def _extract_plan_field(plan_text: str, field: str) -> str | None:
    pattern = re.compile(rf"^{field}:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(plan_text)
    if not match:
        return None
    return match.group(1).strip() or None


def _extract_plan_sections(plan_text: str) -> list[str]:
    matches = re.findall(r"^\s*(?:\d+\.|[-*])\s+(.+)$", plan_text, re.MULTILINE)
    sections = []
    for m in matches:
        s = m.strip()
        if s and s not in sections:
            sections.append(s)
    return sections[:12]


def _extract_title(text: str) -> str:
    lines = text.splitlines()
    for line in lines:
        if line.strip().startswith("# "):
            return line.strip()[2:].strip()
    return "Untitled Essay"


def _estimate_word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def _truncate_markdown_to_words(text: str, max_words: int) -> str:
    if max_words <= 0:
        return ""
    words = re.findall(r"\S+", text)
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).strip()


def _remove_top_level_heading(markdown_text: str) -> str:
    lines = markdown_text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip().startswith("# "):
            del lines[idx]
            break
        if line.strip():
            break
    return "\n".join(lines).strip()


def _strip_duplicate_heading(section_md: str, section_heading: str) -> str:
    lines = section_md.splitlines()
    if not lines:
        return section_md
    first_non_empty = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first_non_empty is None:
        return section_md
    first_line = lines[first_non_empty].strip()
    normalized = section_heading.strip().lower()
    if first_line.startswith("## ") and first_line[3:].strip().lower() == normalized:
        return section_md
    if first_line.startswith("# ") and first_line[2:].strip().lower() == normalized:
        lines[first_non_empty] = f"## {section_heading}"
        return "\n".join(lines).strip()
    return f"## {section_heading}\n\n{section_md.strip()}"


# ---------------------------------------------------------------------------
# Long-form Essay Generation Mechanics
# ---------------------------------------------------------------------------

def _generate_essay_chunked(
    *,
    client: genai.Client,
    system_prompt: str,
    user_prompt: str,
    text_model: str,
    run_id: str,
    total_target_words: int = 7000,
    max_total_words: int = 10000,
) -> tuple[str, str, dict[str, Any]]:
    run_deadline = time.monotonic() + 900.0
    _log_script_event(run_id, "essay_chunked_plan_start", text_model=text_model)
    
    planning_prompt = (
        "Create a precise long-form writing plan for the requested essay. "
        "The final essay target is 5000-10000 words with a single coherent voice. "
        "Return plain text in exactly this structure:\n"
        "TITLE: <concise title>\n"
        "STYLE_BRIEF: <one sentence that locks tone/voice/style>\n"
        "PREVIEW_SLUG: <2-6 words suitable for a homepage card label>\n"
        "PREVIEW_TEXT: <about 50 words that summarize the finished essay for a card>\n"
        "SECTION_HEADINGS:\n"
        "1. <heading>\n"
        "2. <heading>\n"
        "...\n"
        "Use 7-10 sections, ending in a conclusion section.\n\n"
        f"User request:\n{user_prompt}"
    )

    outline_response = _retry_gemini_call(
        lambda: client.models.generate_content(
            model=text_model,
            contents=planning_prompt,
            config=types.GenerateContentConfig(system_instruction=system_prompt)
        ),
        action_name="essay outline generation",
        run_id=run_id,
        deadline_at=run_deadline,
    )
    
    plan_text = (outline_response.text or "").strip()
    plan_title = _extract_plan_field(plan_text, "TITLE")
    style_brief = _extract_plan_field(plan_text, "STYLE_BRIEF") or "Clear, narrative prose."
    plan_preview_slug = _extract_plan_field(plan_text, "PREVIEW_SLUG")
    plan_preview_text = _extract_plan_field(plan_text, "PREVIEW_TEXT")
    sections = _extract_plan_sections(plan_text)
    
    title = plan_title or _extract_title(plan_text) or "Essay"
    outline_usage = _usage_summary(outline_response)
    
    per_section_target = max(550, total_target_words // max(len(sections), 1))
    written_sections = []
    usage_summaries = [outline_usage]

    for idx, section_heading in enumerate(sections):
        if time.monotonic() >= run_deadline:
            raise TimeoutError("chunked essay generation exceeded total time budget")
            
        prior_context = "\n\n".join(written_sections)[-2000:]
        section_prompt = (
            f"Write section {idx + 1} of {len(sections)}: '{section_heading}' for the essay '{title}'.\n"
            f"Style details: {style_brief}\n"
            f"Target length: {per_section_target} words. Output Markdown only.\n"
            f"Prior context:\n{prior_context}"
        )
        
        section_response = _retry_gemini_call(
            lambda: client.models.generate_content(
                model=text_model,
                contents=section_prompt,
                config=types.GenerateContentConfig(system_instruction=system_prompt)
            ),
            action_name=f"essay section {idx + 1}",
            run_id=run_id,
            deadline_at=run_deadline
        )
        
        section_md = (section_response.text or "").strip()
        section_md = _remove_top_level_heading(section_md)
        section_md = _strip_duplicate_heading(section_md, section_heading)
        written_sections.append(section_md)
        usage_summaries.append(_usage_summary(section_response))

    full_md = f"# {title}\n\n" + "\n\n".join(written_sections).strip()
    total_words = _estimate_word_count(full_md)
    if total_words > max_total_words:
        full_md = _truncate_markdown_to_words(full_md, max_total_words)
        total_words = _estimate_word_count(full_md)

    merged_usage = _merge_usage_summaries(usage_summaries)
    return title, full_md, {
        "usage": merged_usage,
        "preview_slug": plan_preview_slug or "Essay Preview",
        "preview_text": plan_preview_text or "Long-form written assignment.",
        "generation_mode": "chunked",
    }


def generate_essay(
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    text_model: str,
    run_id: str,
) -> tuple[str, str, dict[str, Any]]:
    print(f"Generating essay with Gemini using {text_model}…")
    client = _build_gemini_client(api_key)
    
    title, content, meta = _generate_essay_chunked(
        client=client,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        text_model=text_model,
        run_id=run_id,
    )
    return title, content, meta


def _append_generation_appendix(
    content_md: str,
    *,
    user_prompt: str,
    text_model: str,
    image_model: str,
    run_id: str,
    essay_meta: dict[str, Any],
) -> str:
    marker_start = ""
    marker_end = ""
    cleaned = re.sub(rf"\n?{re.escape(marker_start)}[\s\S]*?{re.escape(marker_end)}\n?", "\n", content_md, flags=re.MULTILINE).strip()

    usage = essay_meta.get("usage") or {}
    lines = [
        marker_start,
        "## Appendix: Generation Details",
        "",
        "### User Prompt",
        f"```text\n{user_prompt}\n```",
        "",
        "### Model and Usage",
        f"- Text model: {text_model}",
        f"- Image model: {image_model}",
        f"- Run ID: {run_id}",
        f"- Input tokens: {usage.get('input_tokens', 'unknown')}",
        f"- Output tokens: {usage.get('output_tokens', 'unknown')}",
        f"- Total tokens: {usage.get('total_tokens', 'unknown')}",
        marker_end
    ]
    return f"{cleaned}\n\n" + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Image generation via Imagen
# ---------------------------------------------------------------------------

def generate_cover_image(api_key: str, prompt: str, image_model: str, run_id: str) -> bytes | None:
    print(f"Attempting cover generation using Imagen ({image_model})…")
    try:
        client = _build_gemini_client(api_key)
        result = client.models.generate_images(
            model=image_model,
            prompt=prompt,
            config=types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio="3:4",
                output_mime_type="image/jpeg",
            )
        )
        if result.generated_images:
            return result.generated_images[0].image.image_bytes
    except Exception as e:
        print(f"Imagen cover creation failed: {e}")
    return None


# Placeholder routines for local Pillow fallback, asset writing, or emailing
def _write_jekyll_post(*, site_root: Path, essay_slug: str, title: str, content_md: str, preview_slug: str, preview_text: str, preview_image: str | None, epub_download: str | None, generated_at: str, word_count: int) -> Path:
    posts_dir = site_root / "_posts"
    posts_dir.mkdir(parents=True, exist_ok=True)
    post_file = posts_dir / f"{generated_at[:10]}-{essay_slug}.md"
    
    front_matter = [
        "---",
        f"layout: essay",
        f"title: \"{title.replace('\"', '\\\"')}\"",
        f"generated_at: {generated_at}",
        f"preview_slug: \"{preview_slug}\"",
        f"preview_text: \"{preview_text}\"",
        f"word_count: {word_count}",
    ]
    if preview_image:
        front_matter.append(f"preview_image: \"{preview_image}\"")
    if epub_download:
        front_matter.append(f"epub_download: \"{epub_download}\"")
    front_matter.append("---\n")
    
    post_file.write_text("\n".join(front_matter) + content_md, encoding="utf-8")
    return post_file


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--user-prompt", required=True)
    p.add_argument("--email")
    p.add_argument("--text-model", default="gemini-2.5-pro")
    p.add_argument("--image-model", default="imagen-3.0-generate-002")
    p.add_argument("--site-root", default="docs")
    p.add_argument("--no-email", action="store_true")
    args = p.add_argument_parser().parse_args() if hasattr(p, 'add_argument_parser') else p.parse_args()

    api_key = _require_env("GEMINI_API_KEY")
    sys_prompt = _load_default_system_prompt()
    run_id = str(uuid.uuid4())
    
    title, content_md, meta = generate_essay(
        api_key=api_key,
        system_prompt=sys_prompt,
        user_prompt=args.user_prompt,
        text_model=args.text_model,
        run_id=run_id
    )
    
    final_md = _append_generation_appendix(
        content_md,
        user_prompt=args.user_prompt,
        text_model=args.text_model,
        image_model=args.image_model,
        run_id=run_id,
        essay_meta=meta
    )
    
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    # Generate static markdown/Jekyll posts layout
    _write_jekyll_post(
        site_root=Path(args.site_root),
        essay_slug=slug,
        title=title,
        content_md=final_md,
        preview_slug=meta["preview_slug"],
        preview_text=meta["preview_text"],
        preview_image=None,
        epub_download=f"/assets/essays/{slug}.epub",
        generated_at=now_str,
        word_count=_estimate_word_count(final_md)
    )
    print(f"Successfully wrote post to {args.site_root}/_posts/")

if __name__ == "__main__":
    main()