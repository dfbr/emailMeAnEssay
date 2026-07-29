#!/usr/bin/env python3
"""
emailMeAnEssay – Generate an essay using Google Gemini or OpenAI, bundle it as an EPUB
(with a cover generated natively via Imagen 3 / DALL-E or a local Pillow fallback), 
save the output locally, and publish it into your Jekyll workspace.

Usage:
    python essay_emailer.py --user-prompt "Write an essay about world religions" \
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
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import markdown
from dotenv import load_dotenv
from ebooklib import epub
from PIL import Image, ImageDraw

# Multi-Provider Clients
from google import genai
from google.genai import types
from google.genai.errors import ClientError as GeminiClientError

try:
    import openai
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration loading & Logging Scaffold
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


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"Error: environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return value


def _require_recipient_email(cli_email: str | None) -> str:
    recipient = cli_email or os.getenv("TO_EMAIL")
    if not recipient:
        print("Error: recipient email is not set. Provide --email or TO_EMAIL.", file=sys.stderr)
        sys.exit(1)
    return recipient


def _load_default_system_prompt() -> str:
    prompt_path = Path(__file__).with_name("system_prompt.txt")
    try:
        return prompt_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "You are an expert academic research essayist. Write exhaustively and structure with markdown headings."


def _log_essay_run(*, status: str, run_id: str, title: str | None, user_prompt: str, output_path: str | None, text_model: str, image_model: str, error: str | None = None) -> Path:
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
    if error:
        entry["error"] = error
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return log_path


def _retry_call(func, action_name="API call", attempts=5):
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:
            msg = str(exc).lower()
            if "429" in msg or "quota" in msg or "rate_limit" in msg:
                sleep_time = 2 ** attempt
                print(f"Warning: {action_name} hit 429/Quota limits. Retrying in {sleep_time}s...")
                time.sleep(sleep_time)
                continue
            raise exc
    raise RuntimeError(f"Exhausted structural access retries for {action_name}.")

# ---------------------------------------------------------------------------
# Dynamic Interactive Dual-Provider Pre-Selection Menu
# ---------------------------------------------------------------------------

def select_models_interactively(gemini_client: genai.Client, openai_client: OpenAI | None) -> tuple[str, str, str]:
    """Queries live configurations on Google and OpenAI to shield code from regional 0 RPM traps."""
    print("Fetching active models catalogs across providers...")
    
    gemini_models = []
    try:
        gemini_models = [m.name for m in gemini_client.models.list()]
    except Exception as e:
        print(f"Warning: Could not fetch Google live catalog ({e}). Using core targets fallback.")
        gemini_models = ["models/gemini-1.5-flash", "models/imagen-3.0-generate-002"]

    openai_models = []
    if openai_client:
        try:
            openai_models = [m.id for m in openai_client.models.list()]
        except Exception as e:
            print(f"Warning: Could not fetch OpenAI live catalog ({e}).")

    text_options = [
        {"provider": "gemini", "id": "gemini-1.5-flash", "desc": "Gemini 1.5 Flash (Recommended Free Tier - 15 RPM)"},
        {"provider": "gemini", "id": "gemini-1.5-pro", "desc": "Gemini 1.5 Pro (High reasoning - 2 RPM)"},
        {"provider": "gemini", "id": "gemini-2.5-flash", "desc": "Gemini 2.5 Flash (Latest High Speed)"},
        {"provider": "gemini", "id": "gemini-2.5-pro", "desc": "Gemini 2.5 Pro (Advanced Reasoning)"},
        {"provider": "openai", "id": "gpt-5.4-mini", "desc": "OpenAI GPT-5.4 Mini (Fast Subagent)"},
        {"provider": "openai", "id": "gpt-5.5", "desc": "OpenAI GPT-5.5 (Flagship Intelligence)"},
    ]

    image_options = [
        {"provider": "gemini", "id": "imagen-3.0-generate-002", "desc": "Google Imagen 3 Core"},
        {"provider": "openai", "id": "gpt-image-2", "desc": "OpenAI GPT Image 2 (DALL-E Equivalent)"},
    ]

    def test_text_quota(opt: dict) -> tuple[str | None, str]:
        if opt["provider"] == "gemini":
            real_name = next((n for n in gemini_models if opt["id"] in n), None)
            if not real_name: return None, "[Unsupported Key]"
            try:
                gemini_client.models.generate_content(model=real_name, contents="ping", config=types.GenerateContentConfig(max_output_tokens=1))
                return real_name, "[Available]"
            except Exception as e:
                if any(x in str(e).lower() for x in ["quota exceeded", "limit: 0", "429"]):
                    return real_name, "[Quota Blocked / 0 RPM Region]"
                return real_name, "[Available]"
        else:
            if not openai_client or opt["id"] not in openai_models: return None, "[Unsupported Key/No ENV]"
            try:
                openai_client.chat.completions.create(model=opt["id"], messages=[{"role": "user", "content": "p"}], max_tokens=1)
                return opt["id"], "[Available]"
            except Exception as e:
                if "quota" in str(e).lower() or "429" in str(e).lower():
                    return opt["id"], "[Quota Blocked / Account Balance Depleted]"
                return opt["id"], "[Available]"

    print("Probing endpoints configuration constraints...")
    validated_text = []
    print("\n=== SELECT TEXT GENERATION MODEL ===")
    for idx, opt in enumerate(text_options, 1):
        real_name, status = test_text_quota(opt)
        validated_text.append({"opt": opt, "real_name": real_name, "status": status})
        print(f" {idx}. [{opt['provider'].upper()}] {opt['desc']} {status}")

    while True:
        try:
            choice = int(input("\nChoose text model (number): ").strip())
            if 1 <= choice <= len(validated_text):
                target = validated_text[choice - 1]
                if target["status"] == "[Available]":
                    chosen_text_model = target["real_name"]
                    chosen_provider = target["opt"]["provider"]
                    break
                print(f"Target unavailable. Status is {target['status']}. Pick another option.")
        except ValueError: pass

    print("\n=== SELECT IMAGE GENERATION MODEL ===")
    validated_img = []
    for idx, opt in enumerate(image_options, 1):
        status = "[Unsupported Key]"
        real_name = None
        if opt["provider"] == "gemini":
            real_name = next((n for n in gemini_models if opt["id"] in n), None)
            if real_name: status = "[Available]"
        else:
            if openai_client and opt["id"] in openai_models:
                real_name = opt["id"]
                status = "[Available]"
        validated_img.append({"opt": opt, "real_name": real_name, "status": status})
        print(f" {idx}. [{opt['provider'].upper()}] {opt['desc']} {status}")

    while True:
        try:
            choice = int(input("\nChoose image model (number): ").strip())
            if 1 <= choice <= len(validated_img):
                target = validated_img[choice - 1]
                if target["status"] == "[Available]":
                    chosen_img_model = target["real_name"]
                else:
                    print("Selected image generation is unavailable. Defaulting to local Pillow graphics engine.")
                    chosen_img_model = "pillow-local"
                break
        except ValueError: pass

    return chosen_provider, chosen_text_model, chosen_img_model

# ---------------------------------------------------------------------------
# Generation Engines
# ---------------------------------------------------------------------------

def _extract_field(text: str, field: str) -> str | None:
    match = re.search(rf"^{field}:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _extract_sections(text: str) -> list[str]:
    matches = re.findall(r"^\s*(?:\d+\.|[-*])\s+(.+)$", text, re.MULTILINE)
    return [m.strip() for m in matches if m.strip()][:12]


def generate_essay_pipeline(provider: str, gemini_client: genai.Client, openai_client: OpenAI | None, system_prompt: str, user_prompt: str, model_name: str) -> tuple[str, str, dict[str, Any]]:
    print(f"Drafting generation plan utilizing provider [{provider.upper()}] with model {model_name}...")
    
    planning_prompt = (
        "Create an essay structure. Return text exactly formatted as:\n"
        "TITLE: <title>\nSTYLE_BRIEF: <one sentence style definition>\n"
        "PREVIEW_SLUG: <slug label>\nPREVIEW_TEXT: <summary text>\n"
        "SECTION_HEADINGS:\n1. Introduction\n2. Historical Analysis\n3. Contemporary Global Dynamics\n4. Comparative Shared Principles\n5. Future Horizons and Secular Trends\n"
        f"Topic request:\n{user_prompt}"
    )

    def run_plan():
        if provider == "gemini":
            return gemini_client.models.generate_content(model=model_name, contents=planning_prompt, config=types.GenerateContentConfig(system_instruction=system_prompt)).text
        else:
            res = openai_client.chat.completions.create(model=model_name, messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": planning_prompt}])
            return res.choices[0].message.content

    plan_text = _retry_call(run_plan, "Plan Framework Generation").strip()
    title = _extract_field(plan_text, "TITLE") or "AI Essay Exhibit"
    style_brief = _extract_field(plan_text, "STYLE_BRIEF") or "Academic tone."
    preview_slug = _extract_field(plan_text, "PREVIEW_SLUG") or "essay-anthology"
    preview_text = _extract_field(plan_text, "PREVIEW_TEXT") or "Long-form analytical exposition."
    sections = _extract_sections(plan_text) or ["Introduction", "Historical Analysis", "Contemporary Global Dynamics", "Comparative Shared Principles", "Future Horizons and Secular Trends"]

    written_chunks = []
    for idx, sec in enumerate(sections):
        print(f"Writing chapter {idx+1}/{len(sections)}: {sec}...")
        context_str = "\n\n".join(written_chunks)[-2000:]
        sec_prompt = (
            f"Write a comprehensive section of the essay '{title}' based on locked brief: '{style_brief}'. "
            f"Heading: '## {sec}'. Focus specifically on this subtopic within the global comparative analysis framework. "
            f"Use around 500–800 words as a rough guide, but always finish your final sentence and paragraph completely — "
            f"never cut off mid-sentence or mid-thought to meet a word limit. "
            f"Prior Context: {context_str}"
        )
        
        def run_section():
            if provider == "gemini":
                return gemini_client.models.generate_content(model=model_name, contents=sec_prompt, config=types.GenerateContentConfig(system_instruction=system_prompt)).text
            else:
                res = openai_client.chat.completions.create(model=model_name, messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": sec_prompt}])
                return res.choices[0].message.content

        chunk_md = _retry_call(run_section, f"Section {sec} compilation")
        if not chunk_md.strip().startswith("##"):
            chunk_md = f"## {sec}\n\n{chunk_md.strip()}"
        written_chunks.append(chunk_md.strip())

    full_md = f"# {title}\n\n" + "\n\n".join(written_chunks)
    return title, full_md, {"preview_slug": preview_slug, "preview_text": preview_text, "word_count": len(full_md.split())}


def generate_cover_image(provider: str, gemini_client: genai.Client, openai_client: OpenAI | None, prompt: str, image_model: str) -> bytes:
    if image_model == "pillow-local":
        return generate_fallback_pillow_cover(prompt)
        
    print(f"Assembling cover canvas imagery via engine [{provider.upper()}] model: {image_model}...")
    enhanced_prompt = f"Minimalist elegant book cover graphic design artwork for a volume titled: {prompt[:100]}"
    try:
        if provider == "gemini":
            res = gemini_client.models.generate_images(model=image_model, prompt=enhanced_prompt, config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio="3:4", output_mime_type="image/jpeg"))
            return res.generated_images[0].image.image_bytes
        else:
            # Clean strict parameter requirements for gpt-image models
            if "gpt-image" in image_model.lower():
                res = openai_client.images.generate(model=image_model, prompt=enhanced_prompt, n=1)
            else:
                res = openai_client.images.generate(model=image_model, prompt=enhanced_prompt, n=1, size="1024x1024", response_format="b64_json")
            return base64.b64decode(res.data[0].b64_json)
    except Exception as e:
        print(f"Cover compilation exception tracking error ({e}). Using Pillow fallback.")
        return generate_fallback_pillow_cover(prompt)


def generate_fallback_pillow_cover(title: str) -> bytes:
    img = Image.new("RGB", (600, 800), color="#0f172a")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 580, 780], outline="#cbd5e1", width=2)
    clean_title = title.replace('"', '').replace("'", "")
    draw.text((40, 200), clean_title[:40] + ("..." if len(clean_title) > 40 else ""), fill="#f8fafc")
    out = io.BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()

# ---------------------------------------------------------------------------
# File Tree Packaging & Publishing (Jekyll ecosystem)
# ---------------------------------------------------------------------------

def _split_markdown_by_h2(content_md: str) -> list[dict[str, str]]:
    chapters = []
    current_title = "Introduction"
    current_lines = []
    for line in content_md.splitlines():
        if line.strip().startswith("## "):
            if current_lines:
                chapters.append({"title": current_title, "markdown": "\n".join(current_lines).strip()})
            current_title = line.strip()[3:].strip()
            current_lines = [line]
            continue
        if not (line.strip().startswith("# ") and not chapters and not current_lines):
            current_lines.append(line)
    if current_lines:
        chapters.append({"title": current_title, "markdown": "\n".join(current_lines).strip()})
        
    cleaned_chapters = [ch for ch in chapters if ch["markdown"].strip()]
    if not cleaned_chapters:
        return [{"title": "Essay Content", "markdown": content_md}]
        
    return cleaned_chapters


def _write_output_tree(title: str, epub_bytes: bytes, cover_bytes: bytes, content_md: str, meta: dict, site_root: str, publish_jekyll: bool) -> None:
    slug = re.sub(r"[^\w\s-]", "", title).strip().lower().replace(" ", "-")
    base_dir = Path(site_root) if publish_jekyll else Path("output") / slug
    
    if publish_jekyll:
        posts_dir = base_dir / "_posts"
        assets_dir = base_dir / "assets" / "essays" / slug
        posts_dir.mkdir(parents=True, exist_ok=True)
        assets_dir.mkdir(parents=True, exist_ok=True)
        
        (assets_dir / f"{slug}.epub").write_bytes(epub_bytes)
        (assets_dir / "cover.jpg").write_bytes(cover_bytes)
        
        post_path = posts_dir / f"{datetime.now().strftime('%Y-%m-%d')}-{slug}.md"
        front_matter = f"---\nlayout: essay\ntitle: \"{title}\"\nword_count: {meta['word_count']}\n---\n"
        post_path.write_text(front_matter + content_md, encoding="utf-8")
        print(f"Jekyll web post compiled and verified at: {post_path}")
    else:
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / f"{slug}.epub").write_bytes(epub_bytes)
        (base_dir / "cover.jpg").write_bytes(cover_bytes)
        (base_dir / f"{slug}.md").write_text(content_md, encoding="utf-8")
        print(f"Output files stored inside directory: {base_dir}")

# ---------------------------------------------------------------------------
# Email sequence delivery
# ---------------------------------------------------------------------------

def send_email(epub_bytes: bytes, title: str, recipient: str) -> None:
    print(f"Delivering assignment via transport vectors to {recipient}...")
    msg = MIMEMultipart()
    msg["From"] = _require_env("SMTP_USER")
    msg["To"] = recipient
    msg["Subject"] = title
    msg.attach(MIMEText("Your compiled AI long-form essay collection asset book.", "plain"))
    
    part = MIMEBase("application", "epub+zip")
    part.set_payload(epub_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="essay.epub"')
    msg.attach(part)
    
    with smtplib.SMTP(_require_env("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587"))) as server:
        server.starttls()
        server.login(_require_env("SMTP_USER"), _require_env("SMTP_PASSWORD"))
        server.send_message(msg)
    print("Email successfully dispatched.")

# ---------------------------------------------------------------------------
# Main Routine Orchestrator
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--user-prompt", required=True)
    p.add_argument("--email")
    p.add_argument("--site-root", default="docs")
    p.add_argument("--no-publish-jekyll", action="store_true")
    p.add_argument("--no-email", action="store_true")
    args = p.parse_args()

    run_id = str(uuid.uuid4())
    g_client = genai.Client(api_key=_require_env("GEMINI_API_KEY"))
    
    o_client = None
    if OPENAI_AVAILABLE and os.getenv("OPENAI_API_KEY"):
        o_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    provider, text_model, img_model = select_models_interactively(g_client, o_client)
    system_prompt = _load_default_system_prompt()

    try:
        title, content_md, meta = generate_essay_pipeline(provider, g_client, o_client, system_prompt, args.user_prompt, text_model)
        cover_bytes = generate_cover_image(provider, g_client, o_client, title, img_model)

        # EPUB assembly
        book = epub.EpubBook()
        book.set_title(title)
        book.set_language("en")
        book.set_cover("cover.jpg", cover_bytes)

        # Encode item contents as UTF-8 bytes to fix the XML empty document parse error
        spine_items = []
        for idx, ch in enumerate(_split_markdown_by_h2(content_md)):
            item = epub.EpubHtml(title=ch["title"], file_name=f"ch_{idx}.xhtml", lang="en")
            body_html = markdown.markdown(ch["markdown"])
            
            xhtml_document = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">\n'
                '<head>\n'
                f'  <title>{ch["title"]}</title>\n'
                '</head>\n'
                '<body>\n'
                f'  {body_html}\n'
                '</body>\n'
                '</html>'
            )
            # CRITICAL FIX: Encode raw Python string content into explicit UTF-8 bytes
            item.content = xhtml_document.encode('utf-8')
            book.add_item(item)
            spine_items.append(item)

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
            if tmp_path.exists(): tmp_path.unlink()

        _write_output_tree(title, epub_bytes, cover_bytes, content_md, meta, args.site_root, not args.no_publish_jekyll)

        if not args.no_email:
            send_email(epub_bytes, title, _require_recipient_email(args.email))

        _log_essay_run(status="completed", run_id=run_id, title=title, user_prompt=args.user_prompt, output_path=args.site_root, text_model=text_model, image_model=img_model)
    except Exception as e:
        _log_essay_run(status="failed", run_id=run_id, title=None, user_prompt=args.user_prompt, output_path=None, text_model=text_model, image_model=img_model, error=str(e))
        print(f"Error: Pipeline execution failure -> {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()