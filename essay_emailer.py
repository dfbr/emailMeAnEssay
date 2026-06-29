#!/usr/bin/env python3
"""
emailMeAnEssay – Generate an essay with Google Gemini, bundle it as an EPUB
(with a cover or Pillow fallback and relevant image placements), save the 
output locally, then email it to a recipient – ideal for sending directly 
to a Kindle address.

Usage:
    python essay_emailer.py --user-prompt "Write an essay about stoicism" \
        --email yourname_kindle@kindle.com
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
from PIL import Image, ImageDraw, ImageFont, ImageOps

# Import the correct Google GenAI SDK
from google import genai
from google.genai import types
from google.genai.errors import ClientError

# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

load_dotenv()

_DEBUG_LOG_ENABLED = False
_DEBUG_LOG_PATH: Path | None = None


def _configure_debug_logging(enabled: bool) -> Path | None:
    """Enable or disable verbose debug logging to a daily file."""
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
    """Write a debug event when debug logging is enabled."""
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


def _is_insufficient_quota_error(exc: Exception) -> bool:
    if isinstance(exc, ClientError) and exc.code == 429:
        return True
    message = str(exc).lower()
    return "resource_exhausted" in message or "quota exceeded" in message


def _retry_gemini_call(func, action_name="API call", run_id="", attempts=5, deadline_at=None):
    for attempt in range(1, attempts + 1):
        if deadline_at and time.monotonic() >= deadline_at:
            raise TimeoutError(f"Deadline breached before {action_name}")
        try:
            return func()
        except Exception as exc:
            if _is_insufficient_quota_error(exc) or "429" in str(exc):
                sleep_time = 2 ** attempt
                print(f"Warning: Attempt {attempt} failed for {action_name}: 429 RESOURCE_EXHAUSTED. Retrying in {sleep_time}s...")
                time.sleep(sleep_time)
                continue
            raise exc
    raise RuntimeError(f"Failed {action_name} after {attempts} attempts due to structural exhaustion limits.")


# ---------------------------------------------------------------------------
# Essay generation
# ---------------------------------------------------------------------------

def _unique_preserving_order(seq: list[str]) -> list[str]:
    seen = set()
    return [x for x in seq if not (x in seen or seen.add(x))]


def _slugify_filename(title: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title).strip().lower()
    return re.sub(r"[-\s]+", "-", slug)


def _fallback_preview_slug(title: str) -> str:
    words = title.split()
    if len(words) <= 4:
        return title
    return " ".join(words[:4])


def _normalize_preview_text(preview_text: str, content_md: str) -> str:
    pt = preview_text.strip()
    if pt:
        return pt
    paragraphs = [p.strip() for p in content_md.split("\n\n") if p.strip() and not p.strip().startswith("#")]
    if paragraphs:
        text = paragraphs[0]
        if len(text) > 200:
            return text[:197] + "..."
        return text
    return "AI-generated long-form essay."


def _extract_plan_field(plan_text: str, field: str) -> str | None:
    pattern = re.compile(rf"^{field}:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(plan_text)
    if not match:
        return None
    return match.group(1).strip() or None


def _extract_plan_sections(plan_text: str) -> list[str]:
    matches = re.findall(r"^\s*(?:\d+\.|[-*])\s+(.+)$", plan_text, re.MULTILINE)
    sections = _unique_preserving_order([m.strip() for m in matches])
    return sections[:12]


def _default_chunk_sections(user_prompt: str) -> list[str]:
    prompt_title = user_prompt.strip().rstrip(".?!")
    if len(prompt_title) > 90:
        prompt_title = prompt_title[:90].rstrip()
    topic = prompt_title or "the requested topic"
    return [
        f"Origins and Historical Context of {topic}",
        f"Key Figures and Turning Points in {topic}",
        f"Global Cultural Influence of {topic}",
        f"Critiques and Counterarguments Around {topic}",
        f"Conclusion: Long-Term Significance of {topic}",
    ]


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


def _remove_top_level_heading(markdown_text: str) -> str:
    lines = markdown_text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip().startswith("# "):
            del lines[idx]
            break
        if line.strip():
            break
    return "\n".join(lines).strip()


def _estimate_word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def _generate_chunk_plan_once(client: genai.Client, system_prompt: str, user_prompt: str, text_model: str):
    planning_prompt = (
        "Create a precise long-form writing plan for the requested essay. "
        "Return plain text in exactly this structure:\n"
        "TITLE: <concise title>\n"
        "STYLE_BRIEF: <one sentence that locks tone/voice/style>\n"
        "PREVIEW_SLUG: <2-6 words suitable for a homepage card label>\n"
        "PREVIEW_TEXT: <about 50 words that summarize the finished essay for a card>\n"
        "SECTION_HEADINGS:\n"
        "1. <heading>\n"
        "2. <heading>\n"
        "...\n"
        f"User request:\n{user_prompt}"
    )
    return client.models.generate_content(
        model=text_model,
        contents=planning_prompt,
        config=types.GenerateContentConfig(system_instruction=system_prompt)
    )


def _generate_chunk_section_once(
    client: genai.Client, system_prompt: str, text_model: str, title: str, style_brief: str,
    outline_sections: list[str], section_heading: str, section_index: int, section_count: int,
    target_words: int, prior_context: str
):
    outline_block = "\n".join(f"{idx + 1}. {heading}" for idx, heading in enumerate(outline_sections))
    section_prompt = (
        "Write one section of an essay in markdown.\n"
        f"Essay title: {title}\n"
        f"Locked style: {style_brief}\n"
        f"Section {section_index + 1} of {section_count}: {section_heading}\n"
        f"Target length for this section: about {target_words} words.\n\n"
        "Full outline:\n"
        f"{outline_block}\n\n"
        "Most recent context from already-written sections:\n"
        f"{prior_context or '[none]'}\n\n"
        "Output requirements:\n"
        "- Output markdown only for this section.\n"
        "- Start with a level-2 heading for this section.\n"
        "- Do not include a top-level title heading."
    )
    return client.models.generate_content(
        model=text_model,
        contents=section_prompt,
        config=types.GenerateContentConfig(system_instruction=system_prompt)
    )


def _generate_essay_chunked(
    client: genai.Client, system_prompt: str, user_prompt: str, text_model: str, run_id: str,
    total_target_words: int = 5000, total_timeout_seconds: float = 900.0
) -> tuple[str, str, dict[str, Any]]:
    run_started_at = time.monotonic()
    run_deadline = run_started_at + total_timeout_seconds
    
    _log_script_event(run_id, "essay_chunked_plan_start", text_model=text_model)
    
    outline_response = _retry_gemini_call(
        lambda: _generate_chunk_plan_once(client, system_prompt, user_prompt, text_model),
        action_name="essay outline generation", run_id=run_id, deadline_at=run_deadline
    )
    
    plan_text = (outline_response.text or "").strip()
    plan_title = _extract_plan_field(plan_text, "TITLE")
    style_brief = _extract_plan_field(plan_text, "STYLE_BRIEF") or "Clear, narrative prose with consistent voice."
    plan_preview_slug = _extract_plan_field(plan_text, "PREVIEW_SLUG")
    plan_preview_text = _extract_plan_field(plan_text, "PREVIEW_TEXT")
    sections = _extract_plan_sections(plan_text)
    if len(sections) < 3:
        sections = _default_chunk_sections(user_prompt)

    title = plan_title or "AI Essay"
    per_section_target = max(600, total_target_words // max(len(sections), 1))
    written_sections: list[str] = []

    for idx, section_heading in enumerate(sections):
        if time.monotonic() >= run_deadline:
            raise TimeoutError("chunked essay generation exceeded total time budget")
        
        prior_context = "\n\n".join(written_sections)[-2000:]
        section_response = _retry_gemini_call(
            lambda heading=section_heading, context=prior_context: _generate_chunk_section_once(
                client, system_prompt, text_model, title, style_brief, sections, heading, idx, len(sections), per_section_target, context
            ),
            action_name=f"essay section {idx + 1} generation", run_id=run_id, deadline_at=run_deadline
        )
        
        section_md = (section_response.text or "").strip()
        section_md = _remove_top_level_heading(section_md)
        section_md = _strip_duplicate_heading(section_md, section_heading)
        written_sections.append(section_md)

    full_md = f"# {title}\n\n" + "\n\n".join(written_sections).strip()
    total_words = _estimate_word_count(full_md)
    
    preview_slug = re.sub(r"\s+", " ", (plan_preview_slug or "")).strip()[:70]
    if not preview_slug:
        preview_slug = _fallback_preview_slug(title)
        
    preview_text = _normalize_preview_text(plan_preview_text or "", full_md)
    
    return title, full_md, {
        "preview_slug": preview_slug,
        "preview_text": preview_text,
        "generation_mode": "chunked",
        "word_count": total_words
    }


def generate_essay(api_key: str, system_prompt: str, user_prompt: str, text_model: str, run_id: str) -> tuple[str, str, dict[str, Any]]:
    client = genai.Client(api_key=api_key)
    return _generate_essay_chunked(client, system_prompt, user_prompt, text_model, run_id)


def _split_markdown_by_h2(content_md: str) -> list[dict[str, str]]:
    """Split markdown content into chapter blocks keyed by H2 headings."""
    lines = content_md.splitlines()
    chapters: list[dict[str, str]] = []
    current_title = "Introduction"
    current_lines: list[str] = []
    h1_removed = False
    
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# ") and not h1_removed and not current_lines and not chapters:
            h1_removed = True
            continue
        if stripped.startswith("## "):
            if current_lines:
                chapters.append({
                    "title": current_title,
                    "markdown": "\n".join(current_lines).strip(),
                })
            current_lines = []
            current_title = stripped[3:].strip() or "Untitled Section"
            current_lines.append(f"## {current_title}")
            continue
        current_lines.append(line)
        
    if current_lines:
        chapters.append({
            "title": current_title,
            "markdown": "\n".join(current_lines).strip(),
        })
    chapters = [chapter for chapter in chapters if chapter["markdown"].strip()]
    if not chapters:
        return [{"title": "Essay", "markdown": content_md.strip()}]
    return chapters


# ---------------------------------------------------------------------------
# Jekyll Post Publishing Ecosystem
# ---------------------------------------------------------------------------

def _initialize_jekyll_site_structure(site_root: Path) -> None:
    """Ensure standard static layout scaffolding structures exist at root targets."""
    site_root.mkdir(parents=True, exist_ok=True)
    (site_root / "_posts").mkdir(parents=True, exist_ok=True)
    (site_root / "_layouts").mkdir(parents=True, exist_ok=True)
    (site_root / "assets" / "css").mkdir(parents=True, exist_ok=True)
    (site_root / "assets" / "essays").mkdir(parents=True, exist_ok=True)

    config_file = site_root / "_config.yml"
    if not config_file.exists():
        config_file.write_text(
            "\n".join([
                'title: "My Long-Form Essays Collection"',
                'description: "Long-form AI-generated essays Archive Workspace"',
                'markdown: kramdown',
                'permalink: "/essays/:title/"',
            ]) + "\n",
            encoding="utf-8",
        )

    default_layout = site_root / "_layouts" / "essay.html"
    if not default_layout.exists():
        default_layout.write_text(
            "--- \nlayout: default\n---\n<article class=\"essay-container\">\n"
            "  <header class=\"essay-header\">\n    <h1>{{ page.title }}</h1>\n"
            "    {% if page.word_count %}<span class=\"meta\">{{ page.word_count }} words</span>{% endif %}\n"
            "    {% if page.epub_download %}<a href=\"{{ page.epub_download }}\" class=\"btn\">Download EPUB Edition</a>{% endif %}\n"
            "  </header>\n  <section class=\"essay-content\">\n    {{ content }}\n  </section>\n</article>",
            encoding="utf-8"
        )


def _write_jekyll_post(
    *,
    site_root: Path,
    essay_slug: str,
    title: str,
    content_md: str,
    preview_slug: str,
    preview_text: str,
    epub_relative_url: str | None = None,
    preview_image_url: str | None = None,
    word_count: int = 0,
) -> Path:
    """Assembles YAML Front Matter post markers and logs Markdown post files inside _posts/."""
    posts_dir = site_root / "_posts"
    posts_dir.mkdir(parents=True, exist_ok=True)

    today_str = datetime.now().strftime("%Y-%m-%d")
    post_filename = f"{today_str}-{essay_slug}.md"
    post_path = posts_dir / post_filename

    clean_body = re.sub(r"^#\s+.+?\n", "", content_md, flags=re.MULTILINE).strip()

    front_matter = [
        "---",
        "layout: essay",
        f'title: "{title}"',
        f"date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S %z')}",
        f'preview_slug: "{preview_slug}"',
        f'preview_text: "{preview_text}"',
    ]

    if epub_relative_url:
        front_matter.append(f'epub_download: "{epub_relative_url}"')
    if preview_image_url:
        front_matter.append(f'preview_image: "{preview_image_url}"')
    if word_count > 0:
        front_matter.append(f"word_count: {word_count}")

    front_matter.append("---\n")
    
    post_path.write_text("\n".join(front_matter) + "\n" + clean_body + "\n", encoding="utf-8")
    print(f"Successfully compiled Jekyll workspace post entry artifact at: {post_path}")
    return post_path


def _write_output_tree(
    title: str,
    epub_bytes: bytes,
    content_md: str,
    preview_slug: str,
    preview_text: str,
    output_path: str | None,
    publish_jekyll: bool,
    site_root: str,
    word_count: int,
) -> tuple[Path | None, Path | None]:
    """Saves generated files to local disks or packages them under unified Jekyll locations."""
    output_stem = _slugify_filename(title)
    site_path = Path(site_root)
    
    if publish_jekyll:
        _initialize_jekyll_site_structure(site_path)
        output_dir = site_path / "assets" / "essays" / output_stem
        output_dir.mkdir(parents=True, exist_ok=True)
    elif output_path:
        requested_path = Path(output_path)
        output_dir = requested_path if not requested_path.suffix else requested_path.parent
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        output_dir = Path("output") / output_stem
        output_dir.mkdir(parents=True, exist_ok=True)

    epub_path = output_dir / f"{output_stem}.epub"
    epub_path.write_bytes(epub_bytes)
    print(f"Saved EPUB book artifact locally at: {epub_path}")

    md_path = output_dir / f"{output_stem}.md"
    md_path.write_text(content_md, encoding="utf-8")

    if publish_jekyll:
        epub_rel_url = f"/assets/essays/{output_stem}/{output_stem}.epub"
        _write_jekyll_post(
            site_root=site_path,
            essay_slug=output_stem,
            title=title,
            content_md=content_md,
            preview_slug=preview_slug,
            preview_text=preview_text,
            epub_relative_url=epub_rel_url,
            word_count=word_count
        )

    return epub_path, md_path


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------

def send_email(
    epub_bytes: bytes, title: str, recipient: str, smtp_host: str, smtp_port: int,
    smtp_user: str, smtp_password: str, from_email: str, run_id: str,
) -> bool:
    """Attach the EPUB to an email and send it via SMTP."""
    print(f"Sending email to {recipient}…")
    _log_script_event(run_id, "email_send_start", recipient=recipient, smtp_host=smtp_host, smtp_port=smtp_port)
    
    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = recipient
    msg["Subject"] = title

    msg.attach(MIMEText("Please find your generated long-form essay attached.", "plain"))
    
    filename = f"{_slugify_filename(title)}.epub"
    part = MIMEBase("application", "epub+zip")
    part.set_payload(epub_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        _log_script_event(run_id, "email_send_success", recipient=recipient)
        print("Email delivered successfully!")
        return True
    except Exception as e:
        print(f"Failed to deliver email: {e}", file=sys.stderr)
        _log_script_event(run_id, "email_send_failed", error=str(e))
        return False


# ---------------------------------------------------------------------------
# Main Routine Pipeline Scaffolding
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Generate an essay via Gemini and optional Jekyll compilation structures.")
    p.add_argument("--user-prompt", required=True, help="High-level descriptive prompt topic instructions")
    p.add_argument("--email", help="Destination address for sending compiled files")
    p.add_argument("--text-model", default="gemini-2.5-pro", help="Gemini execution model identifier target")
    p.add_argument("--image-model", default="imagen-3.0-generate-002", help="Image execution model target identifier")
    p.add_argument("--output", metavar="FILE", help="Also save into a dedicated subdirectory at this path")
    p.add_argument("--publish-jekyll", dest="publish_jekyll", action="store_true", default=True, help="Publish output in a Jekyll structure")
    p.add_argument("--no-publish-jekyll", dest="publish_jekyll", action="store_false", help="Disable Jekyll website publishing")
    p.add_argument("--site-root", default="docs", metavar="DIR", help="Root directory for Jekyll posts layout location")
    p.add_argument("--no-email", action="store_true", help="Skip sending email delivery pipeline sequences")
    p.add_argument("--debug-log", action="store_true", help="Enables transactional history auditing trackers")
    args = p.parse_args()

    if args.debug_log:
        _configure_debug_logging(True)

    api_key = _require_env("GEMINI_API_KEY")
    system_prompt = _load_default_system_prompt()
    run_id = str(uuid.uuid4())

    if not args.no_email:
        recipient_email = _require_recipient_email(args.email)
        smtp_host = _require_env("SMTP_HOST")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = _require_env("SMTP_USER")
        smtp_password = _require_env("SMTP_PASSWORD")
        from_email = os.getenv("FROM_EMAIL") or smtp_user

    failed_stage = "essay_generation"
    run_status = "failed"
    failure_error = None

    try:
        title, content_md, meta = generate_essay(
            api_key=api_key,
            system_prompt=system_prompt,
            user_prompt=args.user_prompt,
            text_model=args.text_model,
            run_id=run_id
        )

        failed_stage = "epub_generation"
        book = epub.EpubBook()
        book.set_title(title)
        book.set_language("en")
        book.add_author("Gemini AI Workspace Compiler")
        
        chapters_data = _split_markdown_by_h2(content_md)
        spine_items = []
        
        for idx, ch_info in enumerate(chapters_data):
            c = epub.EpubHtml(
                title=ch_info["title"], 
                file_name=f"chap_{idx + 1:02d}.xhtml", 
                lang="en"
            )
            c.content = f"<html><body>{markdown.markdown(ch_info['markdown'])}</body></html>"
            book.add_item(c)
            spine_items.append(c)
            
        book.toc = spine_items
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ["nav"] + spine_items
        
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            epub.write_epub(tmp_path, book)
            epub_bytes = tmp_path.read_bytes()
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

        failed_stage = "file_writing"
        _write_output_tree(
            title=title,
            epub_bytes=epub_bytes,
            content_md=content_md,
            preview_slug=meta["preview_slug"],
            preview_text=meta["preview_text"],
            output_path=args.output,
            publish_jekyll=args.publish_jekyll,
            site_root=args.site_root,
            word_count=meta.get("word_count", 0)
        )

        if not args.no_email:
            failed_stage = "email_delivery"
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

        run_status = "completed"
        _log_essay_run(
            status="completed", run_id=run_id, title=title, user_prompt=args.user_prompt,
            output_path=args.output, text_model=args.text_model, image_model=args.image_model
        )
    except Exception as exc:
        failure_error = str(exc)
        _log_essay_run(
            status="failed", run_id=run_id, title=locals().get("title"), user_prompt=args.user_prompt,
            output_path=args.output, text_model=args.text_model, image_model=args.image_model, error=failure_error
        )
        print(f"Error: run failed during {failed_stage}. Details: {failure_error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()