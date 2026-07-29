#!/usr/bin/env python3
"""
Generate an essay, package it as an EPUB, optionally publish it into docs/, and
optionally email it to a recipient.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import smtplib
import ssl
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
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
from google import genai
from google.genai import types
from PIL import Image, ImageDraw

try:
    from openai import OpenAI

    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    OpenAI = Any  # type: ignore[assignment]

load_dotenv()

OPENAI_COMPATIBLE_PROVIDERS: dict[str, dict[str, str | None]] = {
    "openai": {
        "api_key_env": "OPENAI_API_KEY",
        "base_url": None,
    },
    "openrouter": {
        "api_key_env": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "groq": {
        "api_key_env": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "xai": {
        "api_key_env": "XAI_API_KEY",
        "base_url": "https://api.x.ai/v1",
    },
}

DEFAULT_TEXT_PROVIDER = "openai"
DEFAULT_IMAGE_PROVIDER = "openai"
DEFAULT_TEXT_MODELS = {
    "openai": "gpt-5.5",
    "gemini": "gemini-2.5-flash",
}
DEFAULT_IMAGE_MODELS = {
    "openai": "gpt-image-2",
    "gemini": "imagen-3.0-generate-002",
    "pillow": "pillow-local",
}


@dataclass
class ProviderSelection:
    provider: str
    model: str
    client: Any | None


class ConfigurationError(RuntimeError):
    pass


class ProviderError(RuntimeError):
    pass


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigurationError(f"Environment variable {name} is required.")
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_provider(raw_provider: str, allowed: set[str], kind: str) -> str:
    provider = raw_provider.strip().lower().replace("_", "-")
    aliases = {
        "google": "gemini",
        "google-gemini": "gemini",
        "open-ai": "openai",
        "pillow-local": "pillow",
    }
    provider = aliases.get(provider, provider)
    if provider not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ConfigurationError(f"Unsupported {kind} provider '{raw_provider}'. Choose one of: {choices}.")
    return provider


def _resolve_provider_selection(
    *,
    kind: str,
    cli_provider: str | None,
    cli_model: str | None,
) -> ProviderSelection:
    provider_env = f"{kind.upper()}_PROVIDER"
    model_env = f"{kind.upper()}_MODEL"
    default_provider = DEFAULT_TEXT_PROVIDER if kind == "text" else DEFAULT_IMAGE_PROVIDER
    allowed = {"gemini"} | set(OPENAI_COMPATIBLE_PROVIDERS)
    if kind == "image":
        allowed |= {"pillow"}

    raw_provider = cli_provider or os.getenv(provider_env) or default_provider
    provider = _normalize_provider(raw_provider, allowed, kind)

    model = (
        cli_model
        or os.getenv(model_env)
        or os.getenv(f"{provider.upper().replace('-', '_')}_{kind.upper()}_MODEL")
        or (DEFAULT_TEXT_MODELS if kind == "text" else DEFAULT_IMAGE_MODELS).get(provider)
    )
    if not model:
        raise ConfigurationError(
            f"No {kind} model configured for provider '{provider}'. Set --{kind}-model or {model_env}."
        )

    if kind == "image" and provider == "pillow":
        return ProviderSelection(provider=provider, model="pillow-local", client=None)
    if provider == "gemini":
        return ProviderSelection(provider=provider, model=model, client=genai.Client(api_key=_require_env("GEMINI_API_KEY")))
    if provider in OPENAI_COMPATIBLE_PROVIDERS:
        if not OPENAI_AVAILABLE:
            raise ConfigurationError("The openai package is required for OpenAI-compatible providers.")
        config = OPENAI_COMPATIBLE_PROVIDERS[provider]
        api_key = _require_env(str(config["api_key_env"]))
        client = OpenAI(api_key=api_key, base_url=config["base_url"])
        return ProviderSelection(provider=provider, model=model, client=client)
    raise ConfigurationError(f"Unsupported provider '{provider}'.")


def _load_default_system_prompt() -> str:
    prompt_path = Path(__file__).with_name("system_prompt.txt")
    try:
        return prompt_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "You are an expert academic research essayist. Write exhaustively and structure with markdown headings."


def _require_recipient_email(cli_email: str | None) -> str:
    recipient = cli_email or os.getenv("TO_EMAIL")
    if not recipient:
        raise ConfigurationError("Recipient email is not set. Provide --email or TO_EMAIL.")
    return recipient


def _log_essay_run(
    *,
    status: str,
    run_id: str,
    title: str | None,
    user_prompt: str,
    output_path: str | None,
    text_provider: str,
    text_model: str,
    image_provider: str,
    image_model: str,
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
        "text_provider": text_provider,
        "text_model": text_model,
        "image_provider": image_provider,
        "image_model": image_model,
    }
    if error:
        entry["error"] = error
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return log_path


def _retry_call(func, action_name: str = "API call", attempts: int = 5):
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            if "429" in message or "quota" in message or "rate_limit" in message:
                sleep_time = 2**attempt
                print(f"Warning: {action_name} hit rate limits. Retrying in {sleep_time}s...")
                time.sleep(sleep_time)
                continue
            raise
    raise RuntimeError(f"Exhausted retries for {action_name}.")


def _generate_text(selection: ProviderSelection, *, system_prompt: str, user_prompt: str) -> str:
    if selection.provider == "gemini":
        response = selection.client.models.generate_content(
            model=selection.model,
            contents=user_prompt,
            config=types.GenerateContentConfig(system_instruction=system_prompt),
        )
        if not response.text:
            raise ProviderError(f"Gemini model {selection.model} returned no text.")
        return response.text

    response = selection.client.chat.completions.create(
        model=selection.model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    text = response.choices[0].message.content
    if not text:
        raise ProviderError(f"Provider {selection.provider} model {selection.model} returned no text.")
    return text


def _extract_field(text: str, field: str) -> str | None:
    match = re.search(rf"^{field}:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _extract_sections(text: str) -> list[str]:
    matches = re.findall(r"^\s*(?:\d+\.|[-*])\s+(.+)$", text, re.MULTILINE)
    return [match.strip() for match in matches if match.strip()][:12]


def generate_essay_pipeline(
    text_selection: ProviderSelection,
    *,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, str, dict[str, Any]]:
    print(
        f"Drafting generation plan using provider [{text_selection.provider.upper()}] "
        f"with model {text_selection.model}..."
    )
    planning_prompt = (
        "Create an essay structure. Return text exactly formatted as:\n"
        "TITLE: <title>\n"
        "STYLE_BRIEF: <one sentence style definition>\n"
        "PREVIEW_SLUG: <slug label>\n"
        "PREVIEW_TEXT: <summary text>\n"
        "SECTION_HEADINGS:\n"
        "1. Introduction\n"
        "2. Historical Analysis\n"
        "3. Contemporary Global Dynamics\n"
        "4. Comparative Shared Principles\n"
        "5. Future Horizons and Secular Trends\n\n"
        f"Topic request:\n{user_prompt}"
    )

    plan_text = _retry_call(
        lambda: _generate_text(text_selection, system_prompt=system_prompt, user_prompt=planning_prompt),
        "plan generation",
    ).strip()

    title = _extract_field(plan_text, "TITLE") or "AI Essay Exhibit"
    style_brief = _extract_field(plan_text, "STYLE_BRIEF") or "Academic tone."
    preview_slug = _extract_field(plan_text, "PREVIEW_SLUG") or "essay-anthology"
    preview_text = _extract_field(plan_text, "PREVIEW_TEXT") or "Long-form analytical exposition."
    sections = _extract_sections(plan_text) or [
        "Introduction",
        "Historical Analysis",
        "Contemporary Global Dynamics",
        "Comparative Shared Principles",
        "Future Horizons and Secular Trends",
    ]

    written_chunks: list[str] = []
    for index, section in enumerate(sections, start=1):
        print(f"Writing chapter {index}/{len(sections)}: {section}...")
        prior_context = "\n\n".join(written_chunks)[-2000:]
        section_prompt = (
            f"Write a comprehensive section of the essay '{title}' based on this locked brief: '{style_brief}'. "
            f"Heading: '## {section}'. Focus specifically on this subtopic. "
            "Use around 500–800 words as a rough guide, but always finish your final sentence and paragraph completely. "
            f"Prior context:\n{prior_context}"
        )
        chunk_md = _retry_call(
            lambda prompt=section_prompt: _generate_text(
                text_selection,
                system_prompt=system_prompt,
                user_prompt=prompt,
            ),
            f"section generation for {section}",
        )
        chunk_md = chunk_md.strip()
        if not chunk_md.startswith("##"):
            chunk_md = f"## {section}\n\n{chunk_md}"
        written_chunks.append(chunk_md)

    full_markdown = f"# {title}\n\n" + "\n\n".join(written_chunks)
    return title, full_markdown, {
        "preview_slug": preview_slug,
        "preview_text": preview_text,
        "word_count": len(full_markdown.split()),
    }


def _fetch_remote_image(url: str) -> bytes:
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response.content


def generate_cover_image(image_selection: ProviderSelection, *, prompt: str) -> bytes:
    if image_selection.provider == "pillow":
        return generate_fallback_pillow_cover(prompt)

    print(
        f"Assembling cover image via provider [{image_selection.provider.upper()}] "
        f"with model {image_selection.model}..."
    )
    enhanced_prompt = f"Minimalist elegant book cover graphic design artwork for a volume titled: {prompt[:100]}"
    try:
        if image_selection.provider == "gemini":
            response = image_selection.client.models.generate_images(
                model=image_selection.model,
                prompt=enhanced_prompt,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio="3:4",
                    output_mime_type="image/jpeg",
                ),
            )
            return response.generated_images[0].image.image_bytes

        response = image_selection.client.images.generate(model=image_selection.model, prompt=enhanced_prompt, n=1)
        item = response.data[0]
        if getattr(item, "b64_json", None):
            return base64.b64decode(item.b64_json)
        if getattr(item, "url", None):
            return _fetch_remote_image(item.url)
        raise ProviderError(f"Image provider {image_selection.provider} returned no image payload.")
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: cover generation failed ({exc}). Falling back to Pillow.")
        return generate_fallback_pillow_cover(prompt)


def generate_fallback_pillow_cover(title: str) -> bytes:
    image = Image.new("RGB", (600, 800), color="#0f172a")
    draw = ImageDraw.Draw(image)
    draw.rectangle([20, 20, 580, 780], outline="#cbd5e1", width=2)
    clean_title = title.replace('"', "").replace("'", "")
    title_text = clean_title[:40] + ("..." if len(clean_title) > 40 else "")
    draw.text((40, 200), title_text, fill="#f8fafc")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def _split_markdown_by_h2(content_md: str) -> list[dict[str, str]]:
    chapters: list[dict[str, str]] = []
    current_title = "Introduction"
    current_lines: list[str] = []
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
    return [chapter for chapter in chapters if chapter["markdown"].strip()] or [{"title": "Essay Content", "markdown": content_md}]


def _write_output_tree(
    *,
    title: str,
    epub_bytes: bytes,
    cover_bytes: bytes,
    content_md: str,
    meta: dict[str, Any],
    site_root: str,
    publish_jekyll: bool,
) -> None:
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
        front_matter = (
            "---\n"
            "layout: essay\n"
            f"title: \"{title}\"\n"
            f"word_count: {meta['word_count']}\n"
            "---\n"
        )
        post_path.write_text(front_matter + content_md, encoding="utf-8")
        print(f"Jekyll post written to: {post_path}")
        return

    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / f"{slug}.epub").write_bytes(epub_bytes)
    (base_dir / "cover.jpg").write_bytes(cover_bytes)
    (base_dir / f"{slug}.md").write_text(content_md, encoding="utf-8")
    print(f"Output files stored in: {base_dir}")


def send_email(*, epub_bytes: bytes, title: str, recipient: str) -> None:
    sender = os.getenv("FROM_EMAIL") or _require_env("SMTP_USER")
    smtp_user = _require_env("SMTP_USER")
    smtp_password = _require_env("SMTP_PASSWORD")
    smtp_host = _require_env("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))

    print(f"Sending EPUB to {recipient} via {smtp_host}:{smtp_port}...")
    message = MIMEMultipart()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = title
    message.attach(MIMEText("Your generated essay is attached as an EPUB.", "plain"))

    part = MIMEBase("application", "epub+zip")
    part.set_payload(epub_bytes)
    encoders.encode_base64(part)
    attachment_name = re.sub(r"[^a-z0-9-]+", "-", title.lower()).strip("-") or "essay"
    part.add_header("Content-Disposition", f'attachment; filename="{attachment_name}.epub"')
    message.attach(part)

    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ssl.create_default_context()) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(message)
    else:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(smtp_user, smtp_password)
            server.send_message(message)
    print("Email sent.")


def _build_epub(title: str, content_md: str, cover_bytes: bytes) -> bytes:
    book = epub.EpubBook()
    book.set_title(title)
    book.set_language("en")
    book.set_cover("cover.jpg", cover_bytes)

    spine_items = []
    for index, chapter in enumerate(_split_markdown_by_h2(content_md)):
        item = epub.EpubHtml(title=chapter["title"], file_name=f"ch_{index}.xhtml", lang="en")
        body_html = markdown.markdown(chapter["markdown"])
        xhtml_document = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">\n'
            '<head>\n'
            f'  <title>{chapter["title"]}</title>\n'
            '</head>\n'
            '<body>\n'
            f'  {body_html}\n'
            '</body>\n'
            '</html>'
        )
        item.content = xhtml_document.encode("utf-8")
        book.add_item(item)
        spine_items.append(item)

    book.toc = spine_items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + spine_items

    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        epub.write_epub(temp_path, book)
        return temp_path.read_bytes()
    finally:
        if temp_path.exists():
            temp_path.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-prompt", required=True)
    parser.add_argument("--system-prompt")
    parser.add_argument("--email")
    parser.add_argument("--text-provider")
    parser.add_argument("--text-model")
    parser.add_argument("--image-provider")
    parser.add_argument("--image-model")
    parser.add_argument("--site-root", default=os.getenv("SITE_ROOT", "docs"))
    parser.add_argument("--no-publish-jekyll", action="store_true")
    parser.add_argument("--no-email", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    run_id = str(uuid.uuid4())
    system_prompt = args.system_prompt or _load_default_system_prompt()
    text_selection = _resolve_provider_selection(
        kind="text",
        cli_provider=args.text_provider,
        cli_model=args.text_model,
    )
    image_selection = _resolve_provider_selection(
        kind="image",
        cli_provider=args.image_provider,
        cli_model=args.image_model,
    )
    publish_jekyll = not args.no_publish_jekyll and not _bool_env("NO_PUBLISH_JEKYLL", False)

    try:
        title, content_md, meta = generate_essay_pipeline(
            text_selection,
            system_prompt=system_prompt,
            user_prompt=args.user_prompt,
        )
        cover_bytes = generate_cover_image(image_selection, prompt=title)
        epub_bytes = _build_epub(title, content_md, cover_bytes)
        _write_output_tree(
            title=title,
            epub_bytes=epub_bytes,
            cover_bytes=cover_bytes,
            content_md=content_md,
            meta=meta,
            site_root=args.site_root,
            publish_jekyll=publish_jekyll,
        )
        if not args.no_email:
            send_email(epub_bytes=epub_bytes, title=title, recipient=_require_recipient_email(args.email))
        _log_essay_run(
            status="completed",
            run_id=run_id,
            title=title,
            user_prompt=args.user_prompt,
            output_path=args.site_root,
            text_provider=text_selection.provider,
            text_model=text_selection.model,
            image_provider=image_selection.provider,
            image_model=image_selection.model,
        )
    except Exception as exc:  # noqa: BLE001
        _log_essay_run(
            status="failed",
            run_id=run_id,
            title=None,
            user_prompt=args.user_prompt,
            output_path=None,
            text_provider=text_selection.provider,
            text_model=text_selection.model,
            image_provider=image_selection.provider,
            image_model=image_selection.model,
            error=str(exc),
        )
        print(f"Error: pipeline execution failed -> {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
