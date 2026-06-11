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
            --image-model; their defaults are gpt-5.5 and gpt-image-1-mini.
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
from openai import APIConnectionError, APITimeoutError, OpenAI
from PIL import Image, ImageDraw, ImageFont, ImageOps

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
        # Debug logging must never interfere with the main run.
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


def _build_openai_image_client(api_key: str, request_timeout: float = 75.0) -> OpenAI:
    # Keep image requests bounded so repeated retries do not stall a full run.
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


_TESTED_TEXT_MODELS = {"gpt-4.1-mini", "gpt-5.5"}


def _is_tested_text_model(text_model: str) -> bool:
    return text_model in _TESTED_TEXT_MODELS


def _should_use_chunked_generation(text_model: str) -> bool:
    # gpt-5.5 long-form calls are more reliable when split into coherent sections.
    return text_model.startswith("gpt-5.5")


def _merge_usage_summaries(summaries: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    merged: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        values: list[int] = []
        for summary in summaries:
            if not summary:
                continue
            value = summary.get(key)
            if isinstance(value, int):
                values.append(value)
        if values:
            merged[key] = sum(values)
    call_count = sum(1 for s in summaries if s)
    if call_count:
        merged["calls"] = call_count
    return merged or None


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
        f"Narrative Evolution and Public Reception of {topic}",
        f"Economic Impact and Industry Effects of {topic}",
        f"Global Cultural Influence of {topic}",
        f"Critiques, Debates, and Counterarguments Around {topic}",
        f"Contemporary Relevance of {topic}",
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


def _truncate_markdown_to_words(text: str, max_words: int) -> str:
    if max_words <= 0:
        return ""
    words = re.findall(r"\S+", text)
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).strip()


def _generate_chunk_plan_once(
    *,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    text_model: str,
):
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
        "Use 7-10 sections, in a logical narrative sequence, ending in a conclusion section.\n\n"
        f"User request:\n{user_prompt}"
    )
    with _build_openai_client(api_key, text_model) as client:
        return client.responses.create(
            model=text_model,
            instructions=system_prompt,
            input=planning_prompt,
            **_responses_request_kwargs(text_model),
        )


def _generate_chunk_section_once(
    *,
    api_key: str,
    system_prompt: str,
    text_model: str,
    title: str,
    style_brief: str,
    outline_sections: list[str],
    section_heading: str,
    section_index: int,
    section_count: int,
    target_words: int,
    prior_context: str,
    max_output_tokens: int,
):
    outline_block = "\n".join(f"{idx + 1}. {heading}" for idx, heading in enumerate(outline_sections))
    section_prompt = (
        "Write one section of an essay in markdown.\n"
        f"Essay title: {title}\n"
        f"Locked style: {style_brief}\n"
        f"Section {section_index + 1} of {section_count}: {section_heading}\n"
        f"Target length for this section: about {target_words} words.\n"
        "Keep voice, rhythm, and terminology consistent with prior sections. "
        "Use factual claims only; if uncertain, qualify briefly instead of inventing details.\n\n"
        "Full outline:\n"
        f"{outline_block}\n\n"
        "Most recent context from already-written sections (for continuity, do not repeat verbatim):\n"
        f"{prior_context or '[none]'}\n\n"
        "Output requirements:\n"
        "- Output markdown only for this section.\n"
        "- Start with a level-2 heading for this section.\n"
        "- Do not include references list or appendix.\n"
        "- Do not include a top-level title heading."
    )
    with _build_openai_client(api_key, text_model) as client:
        return client.responses.create(
            model=text_model,
            instructions=system_prompt,
            input=section_prompt,
            max_output_tokens=max_output_tokens,
            **_responses_request_kwargs(text_model),
        )


def _compress_section_once(
    *,
    api_key: str,
    system_prompt: str,
    text_model: str,
    section_heading: str,
    section_md: str,
    target_words: int,
    max_output_tokens: int,
):
    compress_prompt = (
        "Rewrite the markdown section below to be concise while preserving facts, continuity, and tone.\n"
        f"Target word count: about {target_words} words (not above {target_words + 120}).\n"
        f"Keep the same section heading exactly: ## {section_heading}\n"
        "Return markdown only for this section.\n\n"
        "SECTION TO CONDENSE:\n"
        f"{section_md}"
    )
    with _build_openai_client(api_key, text_model) as client:
        return client.responses.create(
            model=text_model,
            instructions=system_prompt,
            input=compress_prompt,
            max_output_tokens=max_output_tokens,
            **_responses_request_kwargs(text_model),
        )


def _compress_essay_once(
    *,
    api_key: str,
    system_prompt: str,
    text_model: str,
    title: str,
    content_md: str,
    max_total_words: int,
):
    compress_prompt = (
        "Rewrite the full markdown essay below to preserve voice, continuity, and factual claims, "
        f"while keeping it under {max_total_words} words. "
        "Do not add new facts. Output markdown only with the same top-level title.\n\n"
        f"TITLE: {title}\n\n"
        f"ESSAY:\n{content_md}"
    )
    with _build_openai_client(api_key, text_model) as client:
        return client.responses.create(
            model=text_model,
            instructions=system_prompt,
            input=compress_prompt,
            max_output_tokens=2200,
            **_responses_request_kwargs(text_model),
        )


def _generate_essay_chunked(
    *,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    text_model: str,
    run_id: str,
    total_target_words: int = 7000,
    max_total_words: int = 10000,
    section_max_output_tokens: int = 900,
    section_timeout_seconds: float = 180.0,
    total_timeout_seconds: float = 900.0,
    compress_if_over_limit: bool = True,
) -> tuple[str, str, dict[str, Any]]:
    run_started_at = time.monotonic()
    run_deadline = run_started_at + total_timeout_seconds
    _log_script_event(run_id, "essay_chunked_plan_start", text_model=text_model)
    outline_started_at = time.monotonic()
    outline_response = _retry_openai_call(
        lambda: _generate_chunk_plan_once(
            api_key=api_key,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            text_model=text_model,
        ),
        action_name="essay outline generation",
        run_id=run_id,
        deadline_at=run_deadline,
    )
    plan_text = (outline_response.output_text or "").strip()
    plan_title = _extract_plan_field(plan_text, "TITLE")
    style_brief = _extract_plan_field(plan_text, "STYLE_BRIEF") or "Clear, narrative business prose with consistent voice."
    plan_preview_slug = _extract_plan_field(plan_text, "PREVIEW_SLUG")
    plan_preview_text = _extract_plan_field(plan_text, "PREVIEW_TEXT")
    sections = _extract_plan_sections(plan_text)
    if len(sections) < 6:
        sections = _default_chunk_sections(user_prompt)

    title = plan_title or _extract_title(plan_text) or _extract_title(user_prompt)
    outline_usage = _usage_summary(getattr(outline_response, "usage", None))
    outline_elapsed = time.monotonic() - outline_started_at
    _log_script_event(
        run_id,
        "essay_chunked_plan_success",
        text_model=text_model,
        title=title,
        preview_slug=plan_preview_slug,
        section_count=len(sections),
        usage=outline_usage,
        response_id=getattr(outline_response, "id", None),
        elapsed_seconds=round(outline_elapsed, 3),
    )

    per_section_target = max(550, total_target_words // max(len(sections), 1))
    per_section_word_cap = max(per_section_target + 100, int(per_section_target * 1.25))
    written_sections: list[str] = []
    response_ids: list[str] = []
    usage_summaries: list[dict[str, Any] | None] = [outline_usage]
    elapsed_total = outline_elapsed
    first_response_id = getattr(outline_response, "id", None)
    if first_response_id:
        response_ids.append(first_response_id)

    for idx, section_heading in enumerate(sections):
        if time.monotonic() >= run_deadline:
            raise TimeoutError("chunked essay generation exceeded total time budget")
        prior_context = "\n\n".join(written_sections)[-2200:]
        _log_script_event(
            run_id,
            "essay_chunked_section_start",
            text_model=text_model,
            section_index=idx + 1,
            section_count=len(sections),
            section_heading=section_heading,
            target_words=per_section_target,
        )
        section_started_at = time.monotonic()
        section_deadline = min(run_deadline, section_started_at + section_timeout_seconds)
        section_response = _retry_openai_call(
            lambda heading=section_heading, context=prior_context: _generate_chunk_section_once(
                api_key=api_key,
                system_prompt=system_prompt,
                text_model=text_model,
                title=title,
                style_brief=style_brief,
                outline_sections=sections,
                section_heading=heading,
                section_index=idx,
                section_count=len(sections),
                target_words=per_section_target,
                prior_context=context,
                max_output_tokens=section_max_output_tokens,
            ),
            action_name=f"essay section {idx + 1} generation",
            run_id=run_id,
            deadline_at=section_deadline,
        )
        section_md = (section_response.output_text or "").strip()
        section_md = _remove_top_level_heading(section_md)
        section_md = _strip_duplicate_heading(section_md, section_heading)

        section_word_count = _estimate_word_count(section_md)
        if section_word_count > per_section_word_cap:
            _log_script_event(
                run_id,
                "essay_chunked_section_overlength",
                text_model=text_model,
                section_index=idx + 1,
                section_count=len(sections),
                section_heading=section_heading,
                section_words=section_word_count,
                section_word_cap=per_section_word_cap,
            )
            compressed_response = _retry_openai_call(
                lambda current=section_md, heading=section_heading: _compress_section_once(
                    api_key=api_key,
                    system_prompt=system_prompt,
                    text_model=text_model,
                    section_heading=heading,
                    section_md=current,
                    target_words=per_section_target,
                    max_output_tokens=section_max_output_tokens,
                ),
                action_name=f"essay section {idx + 1} compression",
                run_id=run_id,
                attempts=2,
                deadline_at=section_deadline,
            )
            compressed_md = (compressed_response.output_text or "").strip()
            compressed_md = _remove_top_level_heading(compressed_md)
            compressed_md = _strip_duplicate_heading(compressed_md, section_heading)
            compressed_words = _estimate_word_count(compressed_md)
            section_md = compressed_md
            section_word_count = compressed_words
            compressed_usage = _usage_summary(getattr(compressed_response, "usage", None))
            usage_summaries.append(compressed_usage)
            compressed_response_id = getattr(compressed_response, "id", None)
            if compressed_response_id:
                response_ids.append(compressed_response_id)
            _log_script_event(
                run_id,
                "essay_chunked_section_compressed",
                text_model=text_model,
                section_index=idx + 1,
                section_count=len(sections),
                section_heading=section_heading,
                section_words=section_word_count,
                usage=compressed_usage,
                response_id=compressed_response_id,
            )

        written_sections.append(section_md)

        section_usage = _usage_summary(getattr(section_response, "usage", None))
        usage_summaries.append(section_usage)
        section_response_id = getattr(section_response, "id", None)
        if section_response_id:
            response_ids.append(section_response_id)
        section_elapsed = time.monotonic() - section_started_at
        elapsed_total += section_elapsed
        _log_script_event(
            run_id,
            "essay_chunked_section_success",
            text_model=text_model,
            section_index=idx + 1,
            section_count=len(sections),
            section_heading=section_heading,
            output_chars=len(section_md),
            output_words=section_word_count,
            usage=section_usage,
            response_id=section_response_id,
            elapsed_seconds=round(section_elapsed, 3),
        )

    full_md = f"# {title}\n\n" + "\n\n".join(written_sections).strip()
    total_words = _estimate_word_count(full_md)
    if compress_if_over_limit and total_words > max_total_words and time.monotonic() < run_deadline:
        _log_script_event(
            run_id,
            "essay_chunked_total_compress_start",
            text_model=text_model,
            total_words=total_words,
            max_total_words=max_total_words,
        )
        compress_started_at = time.monotonic()
        compressed_response = _retry_openai_call(
            lambda: _compress_essay_once(
                api_key=api_key,
                system_prompt=system_prompt,
                text_model=text_model,
                title=title,
                content_md=full_md,
                max_total_words=max_total_words,
            ),
            action_name="essay total compression",
            run_id=run_id,
            attempts=2,
            deadline_at=run_deadline,
        )
        compressed_md = (compressed_response.output_text or "").strip()
        if compressed_md:
            full_md = compressed_md
            if not full_md.startswith("# "):
                full_md = f"# {title}\n\n{full_md}"
            compressed_usage = _usage_summary(getattr(compressed_response, "usage", None))
            usage_summaries.append(compressed_usage)
            compressed_response_id = getattr(compressed_response, "id", None)
            if compressed_response_id:
                response_ids.append(compressed_response_id)
            elapsed_total += time.monotonic() - compress_started_at
            total_words = _estimate_word_count(full_md)
            _log_script_event(
                run_id,
                "essay_chunked_total_compress_success",
                text_model=text_model,
                total_words=total_words,
                usage=compressed_usage,
                response_id=compressed_response_id,
            )

    if total_words > max_total_words:
        full_md = _truncate_markdown_to_words(full_md, max_total_words)
        total_words = _estimate_word_count(full_md)
        _log_script_event(
            run_id,
            "essay_chunked_total_words_capped",
            text_model=text_model,
            max_total_words=max_total_words,
            final_words=total_words,
        )

    merged_usage = _merge_usage_summaries(usage_summaries)
    preview_slug = re.sub(r"\s+", " ", (plan_preview_slug or "")).strip()[:70]
    preview_text = _normalize_preview_text(plan_preview_text or "", full_md)
    if not preview_slug:
        preview_slug = _fallback_preview_slug(title)
    _log_script_event(
        run_id,
        "essay_chunked_complete",
        text_model=text_model,
        title=title,
        preview_slug=preview_slug,
        section_count=len(sections),
        output_chars=len(full_md),
        output_words=total_words,
        response_ids=response_ids,
        usage=merged_usage,
        elapsed_seconds=round(elapsed_total, 3),
    )
    return title, full_md, {
        "response_id": response_ids[0] if response_ids else None,
        "response_ids": response_ids,
        "api_call_count": len(response_ids),
        "usage": merged_usage,
        "elapsed_seconds": elapsed_total,
        "preview_slug": preview_slug,
        "preview_text": preview_text,
        "generation_mode": "chunked",
    }


def generate_essay(
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    text_model: str,
    run_id: str,
    *,
    chunk_total_target_words: int = 7000,
    chunk_max_total_words: int = 10000,
    chunk_section_max_output_tokens: int = 900,
    chunk_section_timeout_seconds: float = 180.0,
    chunk_total_timeout_seconds: float = 900.0,
    chunk_compress_if_over_limit: bool = True,
) -> tuple[str, str, dict[str, Any]]:
    """Call the OpenAI Responses API and return *(title, markdown_content)*."""
    print(f"Generating essay with OpenAI using {text_model}…")
    generation_mode = "chunked" if _should_use_chunked_generation(text_model) else "single"
    _log_script_event(
        run_id,
        "essay_generation_start",
        text_model=text_model,
        generation_mode=generation_mode,
        prompt_length=len(user_prompt),
    )
    started_at = time.monotonic()
    if _should_use_chunked_generation(text_model):
        title, content, meta = _generate_essay_chunked(
            api_key=api_key,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            text_model=text_model,
            run_id=run_id,
            total_target_words=chunk_total_target_words,
            max_total_words=chunk_max_total_words,
            section_max_output_tokens=chunk_section_max_output_tokens,
            section_timeout_seconds=chunk_section_timeout_seconds,
            total_timeout_seconds=chunk_total_timeout_seconds,
            compress_if_over_limit=chunk_compress_if_over_limit,
        )
        usage = meta.get("usage")
        response_id = meta.get("response_id")
        preview_slug = str(meta.get("preview_slug") or "").strip()
        preview_text = str(meta.get("preview_text") or "").strip()
    else:
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
        content, preview_slug, preview_text = _extract_embedded_preview(response.output_text or "")
        title = _extract_title(content)
        usage = _usage_summary(getattr(response, "usage", None))
        response_id = getattr(response, "id", None)

    if not preview_slug:
        preview_slug = _fallback_preview_slug(title)
    preview_slug = re.sub(r"\s+", " ", preview_slug).strip()[:70]
    preview_text = _normalize_preview_text(preview_text, content)

    elapsed_seconds = time.monotonic() - started_at
    _log_script_event(
        run_id,
        "essay_generation_success",
        text_model=text_model,
        generation_mode=generation_mode,
        title=title,
        output_chars=len(content),
        preview_slug=preview_slug,
        response_id=response_id,
        usage=usage,
        elapsed_seconds=round(elapsed_seconds, 3),
    )
    return title, content, {
        "response_id": response_id,
        "response_ids": [response_id] if response_id else [],
        "api_call_count": 1 if response_id else 0,
        "usage": usage,
        "elapsed_seconds": elapsed_seconds,
        "preview_slug": preview_slug,
        "preview_text": preview_text,
        "generation_mode": generation_mode,
    }


def _append_generation_appendix(
    content_md: str,
    *,
    user_prompt: str,
    text_model: str,
    image_model: str,
    run_id: str,
    essay_meta: dict[str, Any],
) -> str:
    marker_start = "<!-- GENERATED_APPENDIX_START -->"
    marker_end = "<!-- GENERATED_APPENDIX_END -->"
    cleaned = re.sub(
        rf"\n?{re.escape(marker_start)}[\s\S]*?{re.escape(marker_end)}\n?",
        "\n",
        content_md,
        flags=re.MULTILINE,
    ).strip()

    usage_raw = essay_meta.get("usage")
    usage: dict[str, Any] = usage_raw if isinstance(usage_raw, dict) else {}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")

    response_ids = essay_meta.get("response_ids")
    if not isinstance(response_ids, list):
        response_ids = []
    response_ids = [str(response_id).strip() for response_id in response_ids if str(response_id).strip()]

    api_call_count = essay_meta.get("api_call_count")
    if not isinstance(api_call_count, int) or api_call_count <= 0:
        calls_from_usage = usage.get("calls") if isinstance(usage.get("calls"), int) else None
        api_call_count = calls_from_usage or len(response_ids)

    generation_mode = str(essay_meta.get("generation_mode") or "single").strip()
    safe_user_prompt = (user_prompt or "").replace("```", "'''").strip() or "[empty prompt]"

    lines = [
        marker_start,
        "## Appendix: Generation Details",
        "",
        "### User Prompt",
        "",
        "```text",
        safe_user_prompt,
        "```",
        "",
        "### Model and Usage",
        "",
        f"- Text model: {text_model}",
        f"- Image model: {image_model}",
        f"- Generation mode: {generation_mode}",
        f"- Run ID: {run_id}",
        f"- Essay generation API calls: {api_call_count if api_call_count else 'unknown'}",
        f"- Input tokens (essay generation): {input_tokens if isinstance(input_tokens, int) else 'unknown'}",
        f"- Output tokens (essay generation): {output_tokens if isinstance(output_tokens, int) else 'unknown'}",
        f"- Total tokens (essay generation): {total_tokens if isinstance(total_tokens, int) else 'unknown'}",
    ]

    if response_ids:
        lines.extend([
            "",
            "### OpenAI Response IDs (Essay Generation)",
            "",
            *[f"- {response_id}" for response_id in response_ids],
        ])

    lines.extend(["", marker_end])
    return f"{cleaned}\n\n" + "\n".join(lines) + "\n"


def _generate_essay_once(
    *,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    text_model: str,
):
    request_prompt = (
        f"{user_prompt}\n\n"
        "Output requirements:\n"
        "- Write the full essay in markdown.\n"
        "- At the very end, append two HTML comments exactly in this format:\n"
        "  <!-- PREVIEW_SLUG: <2-6 words for a homepage card> -->\n"
        "  <!-- PREVIEW_TEXT: <about 50 words summarizing the essay> -->\n"
        "- Keep PREVIEW_TEXT close to 50 words.\n"
        "- Do not add any other trailing metadata."
    )
    with _build_openai_client(api_key, text_model) as client:
        return client.responses.create(
            model=text_model,
            instructions=system_prompt,
            input=request_prompt,
            **_responses_request_kwargs(text_model),
        )


def _extract_embedded_preview(content_md: str) -> tuple[str, str, str]:
    slug_match = re.search(r"<!--\s*PREVIEW_SLUG:\s*(.+?)\s*-->", content_md, re.IGNORECASE)
    text_match = re.search(r"<!--\s*PREVIEW_TEXT:\s*([\s\S]+?)\s*-->", content_md, re.IGNORECASE)
    preview_slug = slug_match.group(1).strip() if slug_match else ""
    preview_text = text_match.group(1).strip() if text_match else ""
    cleaned = re.sub(r"\n?<!--\s*PREVIEW_(?:SLUG|TEXT):\s*[\s\S]*?-->", "", content_md, flags=re.IGNORECASE)
    return cleaned.strip(), preview_slug, preview_text


def _fallback_preview_slug(title: str) -> str:
    words = re.findall(r"[A-Za-z0-9']+", title)
    if not words:
        return "Essay Preview"
    return " ".join(words[:5]).title()


def _fallback_preview_text(content_md: str) -> str:
    plain = _markdown_to_plain_text(content_md)
    words = re.findall(r"\S+", plain)
    if words:
        return " ".join(words[:50]).strip()
    return "Read the full essay online or download the EPUB edition."


def _normalize_preview_text(preview_text: str, content_md: str) -> str:
    normalized = re.sub(r"\s+", " ", (preview_text or "")).strip()
    words = re.findall(r"\S+", normalized)
    if len(words) < 20:
        normalized = _fallback_preview_text(content_md)
        words = re.findall(r"\S+", normalized)
    if len(words) > 55:
        normalized = " ".join(words[:55]).strip()
    return normalized


def _retry_openai_call(
    callable_fn,
    *,
    action_name: str,
    run_id: str | None = None,
    attempts: int = 4,
    deadline_at: float | None = None,
) -> Any:
    delay_seconds = 1.0
    for attempt in range(1, attempts + 1):
        if deadline_at is not None and time.monotonic() >= deadline_at:
            raise TimeoutError(f"{action_name} exceeded the allotted time budget")
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

        if deadline_at is not None and (time.monotonic() + delay_seconds) >= deadline_at:
            raise TimeoutError(f"{action_name} exceeded the allotted time budget")

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
        # Split multi-intent suggestions so Wikimedia searches stay specific.
        parts = re.split(r"[;|]", suggestion)
        for part in parts:
            cleaned = part.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                unique.append(cleaned)
    return unique[:12]


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
            # Keep the normalized trusted URL even when metadata lookup misses.
            return {
                "url": normalized,
                "title": title.strip() or file_name,
            }

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

    return refs[:12]


_WIKIMEDIA_LAST_REQUEST_AT = 0.0
_WIKIMEDIA_NEXT_ALLOWED_AT = 0.0
_WIKIMEDIA_MIN_REQUEST_INTERVAL = 2.5
_WIKIMEDIA_MAX_ATTEMPTS = 8
_WIKIMEDIA_MAX_BACKOFF_SECONDS = 120.0


def _retry_after_seconds(response: requests.Response | None) -> float | None:
    if response is None:
        return None
    header_value = response.headers.get("Retry-After")
    if not header_value:
        return None
    try:
        seconds = float(header_value.strip())
        return max(0.0, seconds)
    except Exception:
        return None


def _rate_limited_get(url: str, *, params: dict | None = None, stream: bool = False) -> requests.Response:
    global _WIKIMEDIA_LAST_REQUEST_AT, _WIKIMEDIA_NEXT_ALLOWED_AT

    now = time.monotonic()
    delay = max(
        _WIKIMEDIA_MIN_REQUEST_INTERVAL - (now - _WIKIMEDIA_LAST_REQUEST_AT),
        _WIKIMEDIA_NEXT_ALLOWED_AT - now,
    )
    if delay > 0:
        time.sleep(delay)

    last_exc: Exception | None = None
    for attempt in range(_WIKIMEDIA_MAX_ATTEMPTS):
        try:
            _debug_log(
                "http_get_attempt",
                url=url,
                params=params,
                stream=stream,
                attempt=attempt + 1,
            )
            resp = requests.get(
                url,
                params=params,
                timeout=40 if not stream else 75,
                headers={"User-Agent": "emailMeAnEssay/1.0 (educational tool)" if "commons.wikimedia.org" in url else "emailMeAnEssay/1.0"},
                stream=stream,
            )
            _WIKIMEDIA_LAST_REQUEST_AT = time.monotonic()
            if resp.status_code == 429:
                raise requests.HTTPError("429 Too Many Requests", response=resp)
            resp.raise_for_status()
            _debug_log(
                "http_get_success",
                url=url,
                status_code=resp.status_code,
                content_type=resp.headers.get("content-type", ""),
                content_length=resp.headers.get("content-length"),
            )
            _WIKIMEDIA_NEXT_ALLOWED_AT = 0.0
            return resp
        except Exception as exc:
            last_exc = exc
            response = getattr(exc, "response", None)
            is_429 = isinstance(response, requests.Response) and response.status_code == 429

            retry_after = _retry_after_seconds(response) if is_429 else None
            if retry_after is not None:
                wait_seconds = min(max(3.0, retry_after), _WIKIMEDIA_MAX_BACKOFF_SECONDS)
            elif is_429:
                wait_seconds = min(3.0 * (2 ** attempt), _WIKIMEDIA_MAX_BACKOFF_SECONDS)
            elif isinstance(exc, requests.RequestException):
                wait_seconds = min(1.5 * (attempt + 1), 20.0)
            else:
                wait_seconds = min(1.0 * (attempt + 1), 12.0)

            _WIKIMEDIA_NEXT_ALLOWED_AT = max(_WIKIMEDIA_NEXT_ALLOWED_AT, time.monotonic() + wait_seconds)
            _debug_log(
                "http_get_failure",
                url=url,
                params=params,
                stream=stream,
                attempt=attempt + 1,
                error_type=exc.__class__.__name__,
                error_message=str(exc),
                status_code=getattr(response, "status_code", None),
                retry_after_seconds=retry_after,
                wait_seconds=wait_seconds,
            )
            if attempt == _WIKIMEDIA_MAX_ATTEMPTS - 1:
                raise
            time.sleep(wait_seconds)

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
    for separator in (" with ", " showing ", " featuring ", " depicting ", " celebrating ", " and beyond", ",", ";"):
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
        _debug_log(
            "wikimedia_search_failed",
            query=query,
            use_bitmap_filter=use_bitmap_filter,
            error_type=exc.__class__.__name__,
            error_message=str(exc),
        )
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
    _debug_log(
        "wikimedia_search_success",
        query=query,
        use_bitmap_filter=use_bitmap_filter,
        result_count=len(results),
    )
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
            _debug_log(
                "image_download_success",
                url=url,
                content_type=resp.headers.get("content-type", ""),
                byte_count=len(resp.content),
            )
            return resp.content
        _debug_log(
            "image_download_non_image_content_type",
            url=url,
            content_type=resp.headers.get("content-type", ""),
        )
    except Exception as exc:
        print(f"  Warning: download failed – {exc}")
        _debug_log(
            "image_download_failed",
            url=url,
            error_type=exc.__class__.__name__,
            error_message=str(exc),
        )
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


def _attribution_line(title: str, source_url: str | None) -> str:
    clean_title = (title or "Referenced image").strip()
    if source_url:
        return f"Image attribution: [{clean_title}]({source_url}) via Wikimedia Commons."
    return f"Image attribution: {clean_title}."


def _markdown_image_block(image: dict[str, Any], index: int, image_base_path: str) -> str:
    title = (image.get("title") or f"Image {index + 1}").strip()
    local_path = f"{image_base_path.rstrip('/')}/content_{index}.jpg"
    attribution = _attribution_line(title, image.get("source_url"))
    return f"![{title}]({local_path})\n\n*{attribution}*"


def _has_local_markdown_images(content_md: str, image_base_path: str) -> bool:
    escaped_base = re.escape(image_base_path.rstrip("/"))
    pattern = rf"!\[[^\]]*\]\({escaped_base}/content_\d+\.jpg\)"
    return re.search(pattern, content_md) is not None


def _ensure_markdown_embedded_images(
    content_md: str,
    content_images: list[dict[str, Any]],
    *,
    image_base_path: str,
) -> str:
    if not content_images or _has_local_markdown_images(content_md, image_base_path):
        return content_md

    title = _extract_title(content_md)
    chapters = _split_markdown_by_h2(content_md)
    if not chapters:
        return content_md

    image_indices = list(range(len(content_images)))
    cursor = 0
    enriched_chapters: list[str] = []

    for chapter_idx, chapter in enumerate(chapters):
        chapter_md = chapter["markdown"].strip()
        selected: list[int] = []
        if chapter_idx == len(chapters) - 1:
            selected = image_indices[cursor:]
        elif cursor < len(image_indices):
            selected = [image_indices[cursor]]
        cursor += len(selected)

        if selected:
            blocks = "\n\n".join(
                _markdown_image_block(content_images[idx], idx, image_base_path) for idx in selected
            )
            lines = chapter_md.splitlines()
            if lines and lines[0].strip().startswith("## "):
                chapter_md = "\n".join([lines[0], "", blocks, *lines[1:]]).strip()
            else:
                chapter_md = f"{blocks}\n\n{chapter_md}".strip()

        enriched_chapters.append(chapter_md)

    body = "\n\n".join(enriched_chapters).strip()
    if content_md.lstrip().startswith("# "):
        return f"# {title}\n\n{body}".strip()
    return body


def _rewrite_existing_local_image_links(content_md: str, image_base_path: str) -> str:
    base = image_base_path.rstrip("/")
    updated = re.sub(
        r"!\[([^\]]*)\]\(images/content_(\d+)\.jpg\)",
        rf"![\1]({base}/content_\2.jpg)",
        content_md,
    )
    updated = re.sub(
        r"!\[([^\]]*)\]\(/assets/essays/[^/]+/images/content_(\d+)\.jpg\)",
        rf"![\1]({base}/content_\2.jpg)",
        updated,
    )
    return updated


def _append_markdown_image_attribution_section(content_md: str, content_images: list[dict[str, Any]]) -> str:
    marker_start = "<!-- GENERATED_IMAGE_ATTRIBUTION_START -->"
    marker_end = "<!-- GENERATED_IMAGE_ATTRIBUTION_END -->"
    cleaned = re.sub(
        rf"\n?{re.escape(marker_start)}[\s\S]*?{re.escape(marker_end)}\n?",
        "\n",
        content_md,
        flags=re.MULTILINE,
    ).strip()

    if not content_images:
        return cleaned

    lines = [
        marker_start,
        "## Image Attributions",
        "",
    ]
    for image in content_images:
        title = (image.get("title") or "Referenced image").strip()
        source_url = image.get("source_url")
        if source_url:
            lines.append(f"- [{title}]({source_url}) via Wikimedia Commons")
        else:
            lines.append(f"- {title}")
    lines.extend(["", marker_end])

    return f"{cleaned}\n\n" + "\n".join(lines) + "\n"


def _fit_preview_image(raw: bytes, width: int, height: int) -> bytes | None:
    try:
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        fitted = ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        fitted.save(buffer, format="JPEG", quality=88, optimize=True)
        return buffer.getvalue()
    except Exception as exc:
        print(f"  Warning: preview image sizing failed – {exc}")
        return None


def fetch_content_images(
    image_urls: list[dict[str, str]],
    image_queries: list[str],
    fallback_topics: list[str],
    run_id: str,
    *,
    max_images: int = 8,
    max_total_seconds: float = 90.0,
) -> list[dict]:
    """Download one image per query, preferring model-provided image suggestions."""
    print("Fetching content images…")
    direct_urls = [item["url"] for item in image_urls]
    queries = _unique_preserving_order(image_queries) if image_queries else _unique_preserving_order(fallback_topics)
    search_started_at = time.monotonic()
    search_deadline = search_started_at + max_total_seconds
    _log_script_event(
        run_id,
        "content_image_search_start",
        direct_image_urls=direct_urls,
        suggested_queries=image_queries,
        fallback_topics=fallback_topics,
        active_queries=queries,
        max_images=max_images,
        max_total_seconds=max_total_seconds,
    )
    images: list[dict] = []
    seen_titles: set[str] = set()
    if image_urls:
        for ref in image_urls:
            if time.monotonic() >= search_deadline:
                _debug_log("content_image_search_timeout_while_direct_urls", collected=len(images))
                break
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
            if len(images) >= max_images:
                break
        if len(images) >= max_images:
            _log_script_event(
                run_id,
                "content_image_search_complete",
                image_count=len(images),
                query_count=0,
                direct_url_count=len(direct_urls),
                elapsed_seconds=round(time.monotonic() - search_started_at, 3),
            )
            return images

    for query in queries:
        if time.monotonic() >= search_deadline:
            _debug_log("content_image_search_timeout_while_queries", collected=len(images), query=query)
            break
        for meta in search_wikimedia_images(query, max_images=1):
            if meta["title"] in seen_titles:
                continue
            raw = _download_bytes(meta["url"])
            if raw:
                jpeg = _to_jpeg(raw)
                if jpeg:
                    seen_titles.add(meta["title"])
                    images.append({"bytes": jpeg, "title": meta["title"], "source_url": meta["url"]})
                    _debug_log(
                        "content_image_selected_from_query",
                        query=query,
                        title=meta["title"],
                        source_url=meta["url"],
                        collected=len(images),
                    )
                    break  # one image per query
        if len(images) >= max_images:
            break
    _log_script_event(
        run_id,
        "content_image_search_complete",
        image_count=len(images),
        query_count=len(queries),
        direct_url_count=len(direct_urls),
        elapsed_seconds=round(time.monotonic() - search_started_at, 3),
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

def generate_cover_image(
    api_key: str,
    title: str,
    image_model: str,
    run_id: str,
    *,
    request_timeout_seconds: float,
    max_total_seconds: float,
    attempts: int,
) -> bytes:
    """Generate a cover via the GPT image API, falling back to a Pillow-drawn cover."""
    print(f"Generating cover image with {image_model}…")
    _log_script_event(
        run_id,
        "cover_generation_start",
        image_model=image_model,
        title=title,
        request_timeout_seconds=request_timeout_seconds,
        max_total_seconds=max_total_seconds,
        attempts=attempts,
    )
    started_at = time.monotonic()
    deadline_at = started_at + max_total_seconds
    try:
        prompt = (
            f"Professional book cover art for an essay titled '{title}'. "
            "Elegant design with thematically relevant imagery, no text."
        )
        resp = _retry_openai_call(
            lambda: _generate_cover_once(
                api_key=api_key,
                prompt=prompt,
                image_model=image_model,
                request_timeout_seconds=request_timeout_seconds,
            ),
            action_name="cover generation",
            run_id=run_id,
            attempts=attempts,
            deadline_at=deadline_at,
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
        _log_script_event(
            run_id,
            "cover_generation_empty_response",
            image_model=image_model,
            elapsed_seconds=round(time.monotonic() - started_at, 3),
        )
    except Exception as exc:
        print(f"  Warning: cover generation failed – {exc}")
        _log_script_event(
            run_id,
            "cover_generation_failed",
            image_model=image_model,
            error=str(exc),
            elapsed_seconds=round(time.monotonic() - started_at, 3),
        )

    _log_script_event(run_id, "cover_generation_fallback", image_model=image_model)
    return _make_fallback_cover(title)


def _generate_cover_once(*, api_key: str, prompt: str, image_model: str, request_timeout_seconds: float):
    with _build_openai_image_client(api_key, request_timeout=request_timeout_seconds) as client:
        image_kwargs: dict[str, Any] = {
            "model": image_model,
            "prompt": prompt,
            "size": "1024x1024",
        }
        # Lower-cost settings for mini models where supported.
        if image_model.endswith("mini"):
            image_kwargs["quality"] = "low"
            image_kwargs["n"] = 1
        return client.images.generate(**image_kwargs)


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
            "/System/Library/Fonts/Supplemental/Georgia Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Georgia.ttf",
            "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
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

    title_text = title.strip() or "Essay"
    title_area_width = W - 180
    title_area_height = int(H * 0.52)

    def _wrap_for_width(text: str, font: Any, max_width: int) -> list[str]:
        words = text.split()
        if not words:
            return ["Essay"]
        lines: list[str] = []
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}".strip()
            trial_bbox = draw.textbbox((0, 0), trial, font=font)
            trial_width = trial_bbox[2] - trial_bbox[0]
            if trial_width <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    title_font = _font(96, bold=True)
    wrapped_lines = [title_text]
    line_h = 112

    # Find the largest readable font size that fits width and height constraints.
    for size in range(180, 51, -4):
        candidate_font = _font(size, bold=True)
        candidate_lines = _wrap_for_width(title_text, candidate_font, title_area_width)
        line_bbox = draw.textbbox((0, 0), "Ag", font=candidate_font)
        candidate_line_h = int((line_bbox[3] - line_bbox[1]) * 1.22)
        total_h = candidate_line_h * len(candidate_lines)
        max_w = 0
        for line in candidate_lines:
            bbox = draw.textbbox((0, 0), line, font=candidate_font)
            max_w = max(max_w, bbox[2] - bbox[0])

        if max_w <= title_area_width and total_h <= title_area_height:
            title_font = candidate_font
            wrapped_lines = candidate_lines
            line_h = candidate_line_h
            break

    sub_font = _font(40)

    # Draw title centred vertically in upper half
    start_y = int(H * 0.29) - (len(wrapped_lines) * line_h) // 2
    for line in wrapped_lines:
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
    preview_text: str,
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
    book.add_author("ChatGPT prompted by David Rowan")
    book.add_metadata(
        "DC",
        "description",
        "An AI-generated essay produced by emailMeAnEssay.",
    )
    book.add_metadata("DC", "description", f"User prompt: {user_prompt}")
    book.add_metadata("DC", "description", f"Text model: {text_model}")
    book.add_metadata("DC", "description", f"Image model: {image_model}")
    book.add_metadata("DC", "publisher", "Rowan Page")
    book.add_metadata("DC", "subject", (preview_text or "").strip())

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
        epub_img_refs.append(
            {
                "file_name": item.file_name,
                "title": img.get("title", "Referenced image"),
                "source_url": img.get("source_url"),
            }
        )

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

    chapters_md = _split_markdown_by_h2(content_md)
    chapter_items: list[epub.EpubHtml] = []

    image_cursor = 0
    for idx, chapter_meta in enumerate(chapters_md):
        chapter_title = chapter_meta["title"]
        chapter_md = chapter_meta["markdown"]

        chapter_html = markdown.markdown(
            chapter_md,
            extensions=["extra", "tables", "toc"],
        )

        if idx == len(chapters_md) - 1:
            chapter_images = epub_img_refs[image_cursor:]
        elif image_cursor < len(epub_img_refs):
            chapter_images = [epub_img_refs[image_cursor]]
        else:
            chapter_images = []
        image_cursor += len(chapter_images)
        chapter_html = _inject_images(chapter_html, chapter_images)

        chapter_item = epub.EpubHtml(
            title=chapter_title,
            file_name=f"chapter_{idx + 1}.xhtml",
            lang="en",
        )
        chapter_item.content = (
            "<?xml version='1.0' encoding='utf-8'?>\n"
            "<!DOCTYPE html>\n"
            '<html xmlns="http://www.w3.org/1999/xhtml">\n'
            "<head>\n"
            f"  <title>{_xml_escape(chapter_title)}</title>\n"
            '  <link rel="stylesheet" type="text/css" href="style/main.css"/>\n'
            "</head>\n"
            "<body>\n"
            f"{chapter_html}\n"
            "</body>\n"
            "</html>"
        ).encode("utf-8")
        chapter_item.add_item(css_item)
        book.add_item(chapter_item)
        chapter_items.append(chapter_item)

    book.toc = [
        epub.Link(chapter.file_name, chapter.title, f"chapter-{idx + 1}")
        for idx, chapter in enumerate(chapter_items)
    ]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *chapter_items]

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
        title = _xml_escape(img.get("title") or "Referenced image")
        source_url = img.get("source_url")
        if source_url:
            source = _xml_escape(source_url)
            caption = (
                f"<figcaption>{title}<br/>"
                f"<small>Source: <a href=\"{source}\">{source}</a> via Wikimedia Commons</small></figcaption>"
            )
        else:
            caption = f"<figcaption>{title}</figcaption>"
        return (
            f'<figure><img src="{img["file_name"]}" '
            f'alt="{title}" />{caption}</figure>'
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
                chapters.append(
                    {
                        "title": current_title,
                        "markdown": "\n".join(current_lines).strip(),
                    }
                )
                current_lines = []
            current_title = stripped[3:].strip() or "Untitled Section"
            current_lines.append(f"## {current_title}")
            continue

        current_lines.append(line)

    if current_lines:
        chapters.append(
            {
                "title": current_title,
                "markdown": "\n".join(current_lines).strip(),
            }
        )

    chapters = [chapter for chapter in chapters if chapter["markdown"].strip()]
    if not chapters:
        return [{"title": "Essay", "markdown": content_md.strip()}]
    return chapters


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
    cover_bytes: bytes,
    content_md: str,
    content_images: list[dict[str, Any]],
    user_prompt: str,
    text_model: str,
    image_model: str,
    essay_meta: dict[str, Any],
    preview_slug: str,
    preview_text: str,
    output_path: str | None,
    publish_jekyll: bool,
    site_root: str,
    run_id: str,
) -> tuple[Path, Path]:
    output_stem = _slugify_filename(title)
    site_path = Path(site_root)

    if publish_jekyll:
        output_dir = site_path / "assets" / "essays" / output_stem
        post_dir = site_path / "_posts"
        output_dir.mkdir(parents=True, exist_ok=True)
        post_dir.mkdir(parents=True, exist_ok=True)
    elif output_path:
        requested_path = Path(output_path)
        if requested_path.suffix:
            output_dir = requested_path.with_suffix("")
            output_stem = requested_path.stem
        else:
            output_dir = requested_path
            output_stem = requested_path.name or _slugify_filename(title)
    else:
        output_dir = Path("output") / output_stem

    output_dir.mkdir(parents=True, exist_ok=True)
    epub_path = output_dir / f"{output_stem}.epub"

    epub_path.write_bytes(epub_bytes)

    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    for idx, image in enumerate(content_images):
        image_bytes = image.get("bytes")
        if isinstance(image_bytes, (bytes, bytearray)):
            (images_dir / f"content_{idx}.jpg").write_bytes(bytes(image_bytes))

    preview_source = None
    if content_images and isinstance(content_images[0].get("bytes"), (bytes, bytearray)):
        preview_source = bytes(content_images[0]["bytes"])
    elif cover_bytes:
        preview_source = cover_bytes

    preview_image_rel_path = None
    if preview_source:
        preview_wide = _fit_preview_image(preview_source, 1200, 630)
        if preview_wide:
            wide_name = "preview_1200x630.jpg"
            (images_dir / wide_name).write_bytes(preview_wide)
            preview_image_rel_path = (
                f"/assets/essays/{output_stem}/images/{wide_name}"
                if publish_jekyll
                else f"images/{wide_name}"
            )
        preview_card = _fit_preview_image(preview_source, 640, 360)
        if preview_card:
            (images_dir / "preview_640x360.jpg").write_bytes(preview_card)

    image_base_path = f"/assets/essays/{output_stem}/images" if publish_jekyll else "images"
    content_md = _rewrite_existing_local_image_links(content_md, image_base_path)
    prepared_md = _ensure_markdown_embedded_images(
        content_md,
        content_images,
        image_base_path=image_base_path,
    )
    prepared_md = _append_markdown_image_attribution_section(prepared_md, content_images)
    prepared_md = _append_generation_appendix(
        prepared_md,
        user_prompt=user_prompt,
        text_model=text_model,
        image_model=image_model,
        run_id=run_id,
        essay_meta=essay_meta,
    )

    if publish_jekyll:
        _ensure_jekyll_site_scaffold(site_path)
        md_path = _write_jekyll_post(
            site_root=site_path,
            essay_slug=output_stem,
            title=title,
            content_md=prepared_md,
            preview_slug=preview_slug,
            preview_text=preview_text,
            preview_image=preview_image_rel_path,
            epub_download=f"/assets/essays/{output_stem}/{output_stem}.epub",
            word_count=_estimate_word_count(prepared_md),
            run_id=run_id,
        )
    else:
        md_path = output_dir / f"{output_stem}.md"
        md_path.write_text(prepared_md, encoding="utf-8")

    print(f"EPUB saved to: {epub_path}")
    print(f"Essay markdown saved to: {md_path}")
    _log_script_event(
        run_id,
        "local_save_complete",
        output_dir=str(output_dir),
        epub_path=str(epub_path),
        md_path=str(md_path),
        publish_jekyll=publish_jekyll,
        site_root=site_root if publish_jekyll else None,
    )
    return epub_path, md_path


def _markdown_to_plain_text(content_md: str) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", content_md)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!?\[[^\]]*\]\([^\)]+\)", " ", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _essay_excerpt(content_md: str, max_chars: int = 220) -> str:
    plain = _markdown_to_plain_text(content_md)
    if len(plain) <= max_chars:
        return plain
    trimmed = plain[:max_chars].rsplit(" ", 1)[0].strip()
    return (trimmed or plain[:max_chars]).strip() + "..."


def _ensure_jekyll_site_scaffold(site_root: Path) -> None:
    site_root.mkdir(parents=True, exist_ok=True)
    (site_root / "_layouts").mkdir(parents=True, exist_ok=True)
    (site_root / "_posts").mkdir(parents=True, exist_ok=True)
    (site_root / "assets" / "essays").mkdir(parents=True, exist_ok=True)
    (site_root / "assets" / "css").mkdir(parents=True, exist_ok=True)

    config_path = site_root / "_config.yml"
    if not config_path.exists():
        config_path.write_text(
            "\n".join(
                [
                    'title: "Essays"',
                    'description: "Long-form AI-generated essays"',
                    'url: "https://essays.dfbr.co.uk"',
                    'markdown: kramdown',
                    'permalink: "/essays/:title/"',
                    "plugins:",
                    "  - jekyll-feed",
                    "  - jekyll-paginate",
                    "paginate: 5",
                    'paginate_path: "/page/:num/"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    default_layout = site_root / "_layouts" / "default.html"
    if not default_layout.exists():
        default_layout.write_text(
            """<!doctype html>
<html lang=\"en\">
    <head>
        <meta charset=\"utf-8\" />
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
        <title>{% if page.title %}{{ page.title }} | {{ site.title }}{% else %}{{ site.title }}{% endif %}</title>
        <link rel=\"stylesheet\" href=\"{{ '/assets/css/site.css' | relative_url }}\" />
    </head>
    <body>
        <header class=\"site-header\">
            <a href=\"{{ '/' | relative_url }}\" class=\"home-link\">{{ site.title }}</a>
        </header>
        <main class=\"container\">{{ content }}</main>
    </body>
</html>
""",
        encoding="utf-8",
    )

    essay_layout = site_root / "_layouts" / "essay.html"
    if not essay_layout.exists():
        essay_layout.write_text(
            """---
layout: default
---
<article class=\"essay\">
    <header class=\"essay-header\">
        <h1>{{ page.title }}</h1>
        {% if page.generated_at %}<p class=\"meta\">Generated {{ page.generated_at }}</p>{% endif %}
        <p class=\"actions\">
            <a href=\"{{ page.epub_download | relative_url }}\">Download EPUB</a>
        </p>
    </header>
    <section class=\"essay-content\">{{ content }}</section>
</article>
""",
        encoding="utf-8",
    )

    css_path = site_root / "assets" / "css" / "site.css"
    if not css_path.exists():
        css_path.write_text(
            """:root {
    --bg: #f8f6ef;
    --surface: #fffdf7;
    --ink: #1f2a37;
    --accent: #0e6b5f;
    --muted: #5e6b7a;
    --border: #d8d3c3;
}

* { box-sizing: border-box; }
body {
    margin: 0;
    font-family: Georgia, 'Times New Roman', serif;
    color: var(--ink);
    background: linear-gradient(180deg, #f3f1e7 0%, #f8f6ef 100%);
}
.site-header {
    padding: 1rem 1.2rem;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
}
.home-link {
    color: var(--accent);
    text-decoration: none;
    font-weight: 700;
}
.container {
    max-width: 900px;
    margin: 0 auto;
    padding: 1.25rem;
}
.essay-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 0.9rem;
}
.essay-card {
    border: 1px solid var(--border);
    background: var(--surface);
    padding: 1rem;
    border-radius: 10px;
}
.essay-card h2 {
    margin: 0 0 0.5rem;
    font-size: 1.2rem;
}
.essay-card p {
    margin: 0.35rem 0;
}
.meta {
    color: var(--muted);
    font-size: 0.95rem;
}
.actions {
    display: flex;
    gap: 1rem;
    flex-wrap: wrap;
}
.actions a {
    color: var(--accent);
}
.essay-content img {
    max-width: 100%;
    height: auto;
    border-radius: 8px;
}
""",
        encoding="utf-8",
    )

    index_path = site_root / "index.html"
    if not index_path.exists():
        index_path.write_text(
            """---
layout: default
title: Essay Library
---

<h1>Essay Library</h1>

<p>Every now and then I get curious about something and ask an LLM to generate an essay for me on a topic. Here are the results in case anyone else is interested in them. The usual caveats for LLM generated content apply <em>mistakes happen</em>.</p>

{% if paginator.posts and paginator.posts.size > 0 %}
<ul class=\"essay-list\">
{% for post in paginator.posts %}
    <li class=\"essay-card\">
        {% if post.preview_image %}
        <a href=\"{{ post.url | relative_url }}\" class=\"card-image-link\">
            <img src=\"{{ post.preview_image | relative_url }}\" alt=\"Preview image for {{ post.title }}\" class=\"card-image\" />
        </a>
        {% endif %}
        <h2><a href=\"{{ post.url | relative_url }}\">{{ post.title }}</a></h2>
        {% if post.preview_slug %}<p class=\"meta\">{{ post.preview_slug }}</p>{% endif %}
        <p class=\"meta\">{{ post.generated_at | default: post.date | date: \"%Y-%m-%d\" }}{% if post.word_count %} • {{ post.word_count }} words{% endif %}</p>
        <p>{{ post.preview_text | default: post.excerpt }}</p>
        <p class=\"actions\">
            <a href=\"{{ post.url | relative_url }}\">Read online</a>
            {% if post.epub_download %}<a href=\"{{ post.epub_download | relative_url }}\">Download EPUB</a>{% endif %}
        </p>
    </li>
{% endfor %}
</ul>

<nav class=\"pagination\">
    {% if paginator.previous_page %}
        <a href=\"{{ paginator.previous_page_path | relative_url }}\">Newer</a>
    {% endif %}
    <span>Page {{ paginator.page }} of {{ paginator.total_pages }}</span>
    {% if paginator.next_page %}
        <a href=\"{{ paginator.next_page_path | relative_url }}\">Older</a>
    {% endif %}
</nav>
{% else %}
<p>No essays published yet.</p>
{% endif %}
""",
        encoding="utf-8",
    )


def _write_jekyll_post(
    *,
    site_root: Path,
    essay_slug: str,
    title: str,
    content_md: str,
    preview_slug: str,
    preview_text: str,
    preview_image: str | None,
    epub_download: str,
    word_count: int,
    run_id: str,
) -> Path:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    page_path = site_root / "_posts" / f"{generated_at}-{essay_slug}.md"

    safe_title = title.replace('"', '\\"')
    safe_preview_slug = (preview_slug or "").replace('"', '\\"')
    safe_preview_text = (preview_text or "").replace('"', '\\"')
    safe_preview_image = (preview_image or "").replace('"', '\\"')
    page_content = (
        "---\n"
        "layout: essay\n"
        f"title: \"{safe_title}\"\n"
        f"generated_at: {generated_at}\n"
        f"preview_slug: \"{safe_preview_slug}\"\n"
        f"preview_text: \"{safe_preview_text}\"\n"
        f"preview_image: \"{safe_preview_image}\"\n"
        f"epub_download: \"{epub_download}\"\n"
        f"word_count: {word_count}\n"
        f"run_id: \"{run_id}\"\n"
        "---\n\n"
        f"{content_md.strip()}\n"
    )
    page_path.write_text(page_content, encoding="utf-8")
    return page_path


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
        default="gpt-image-1-mini",
        metavar="NAME",
        help="Cover image model (default: gpt-image-1-mini)",
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
        "--cover-mode",
        choices=["auto", "ai", "pillow"],
        default="auto",
        help=(
            "Cover strategy: auto (prefer first content image, then AI, then Pillow), "
            "ai (force AI attempt), or pillow (always local Pillow cover). Default: auto"
        ),
    )
    p.add_argument(
        "--cover-request-timeout-seconds",
        type=float,
        default=75.0,
        metavar="SECONDS",
        help="Timeout per cover API request before retry/fallback (default: 75)",
    )
    p.add_argument(
        "--cover-max-seconds",
        type=float,
        default=180.0,
        metavar="SECONDS",
        help="Maximum total time budget for cover generation across retries (default: 180)",
    )
    p.add_argument(
        "--cover-attempts",
        type=int,
        default=3,
        metavar="N",
        help="Maximum cover generation attempts before fallback (default: 3)",
    )
    p.add_argument(
        "--image-search-max-seconds",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="Maximum total time budget for content image search (default: 300)",
    )
    p.add_argument(
        "--image-max-count",
        type=int,
        default=8,
        metavar="N",
        help="Maximum number of content images to embed (default: 8)",
    )
    p.add_argument(
        "--output",
        metavar="FILE",
        help="Also save the EPUB and markdown into a dedicated subdirectory at this path",
    )
    p.add_argument(
        "--publish-jekyll",
        dest="publish_jekyll",
        action="store_true",
        default=True,
        help="Publish output in a Jekyll-ready site structure under --site-root (default: enabled)",
    )
    p.add_argument(
        "--no-publish-jekyll",
        dest="publish_jekyll",
        action="store_false",
        help="Disable Jekyll website publishing for this run",
    )
    p.add_argument(
        "--site-root",
        default="docs",
        metavar="DIR",
        help="Root directory for Jekyll publishing when --publish-jekyll is enabled (default: docs)",
    )
    p.add_argument(
        "--no-email",
        action="store_true",
        help="Skip sending email (combine with --output to just save locally)",
    )
    p.add_argument(
        "--debug-log",
        action="store_true",
        help="Enable verbose debug logging to output/logs/debug-YYYY-MM-DD.log",
    )
    p.add_argument(
        "--chunk-total-target-words",
        type=int,
        default=7000,
        metavar="N",
        help="Target total words for chunked generation (default: 7000)",
    )
    p.add_argument(
        "--chunk-max-total-words",
        type=int,
        default=10000,
        metavar="N",
        help="Hard maximum total words for chunked generation (default: 10000)",
    )
    p.add_argument(
        "--chunk-section-max-output-tokens",
        type=int,
        default=900,
        metavar="N",
        help="Max output tokens per chunked section call (default: 900)",
    )
    p.add_argument(
        "--chunk-section-timeout-seconds",
        type=float,
        default=180.0,
        metavar="SECONDS",
        help="Time budget per chunked section including retries (default: 180)",
    )
    p.add_argument(
        "--chunk-total-timeout-seconds",
        type=float,
        default=900.0,
        metavar="SECONDS",
        help="Total time budget for chunked essay generation (default: 900)",
    )
    p.add_argument(
        "--no-chunk-compress",
        action="store_true",
        help="Disable final compression pass when chunked output exceeds max words",
    )
    return p


def main() -> None:
    args = build_arg_parser().parse_args()

    debug_log_path = _configure_debug_logging(args.debug_log)
    if debug_log_path is not None:
        print(f"Debug logging enabled: {debug_log_path}")
        _debug_log("debug_logging_enabled", path=str(debug_log_path))

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
    if not _is_tested_text_model(args.text_model):
        tested_models = ", ".join(sorted(_TESTED_TEXT_MODELS))
        print(
            f"Warning: text model '{args.text_model}' is not in tested models ({tested_models}). "
            "Proceeding with safe defaults; behavior may vary."
        )
        _log_script_event(
            run_id,
            "untested_text_model_warning",
            text_model=args.text_model,
            tested_models=sorted(_TESTED_TEXT_MODELS),
        )

    if args.cover_request_timeout_seconds <= 0:
        print("Error: --cover-request-timeout-seconds must be > 0", file=sys.stderr)
        sys.exit(1)
    if args.cover_max_seconds <= 0:
        print("Error: --cover-max-seconds must be > 0", file=sys.stderr)
        sys.exit(1)
    if args.cover_attempts < 1:
        print("Error: --cover-attempts must be >= 1", file=sys.stderr)
        sys.exit(1)
    if args.image_search_max_seconds <= 0:
        print("Error: --image-search-max-seconds must be > 0", file=sys.stderr)
        sys.exit(1)
    if args.image_max_count < 1:
        print("Error: --image-max-count must be >= 1", file=sys.stderr)
        sys.exit(1)
    if args.chunk_total_target_words < 1000:
        print("Error: --chunk-total-target-words must be >= 1000", file=sys.stderr)
        sys.exit(1)
    if args.chunk_max_total_words < args.chunk_total_target_words:
        print("Error: --chunk-max-total-words must be >= --chunk-total-target-words", file=sys.stderr)
        sys.exit(1)
    if args.chunk_section_max_output_tokens < 200:
        print("Error: --chunk-section-max-output-tokens must be >= 200", file=sys.stderr)
        sys.exit(1)
    if args.chunk_section_timeout_seconds <= 0 or args.chunk_total_timeout_seconds <= 0:
        print("Error: chunk timeout values must be > 0", file=sys.stderr)
        sys.exit(1)
    if args.publish_jekyll and not args.site_root.strip():
        print("Error: --site-root must not be empty when --publish-jekyll is enabled", file=sys.stderr)
        sys.exit(1)

    _log_script_event(
        run_id,
        "run_start",
        text_model=args.text_model,
        image_model=args.image_model,
        no_images=args.no_images,
        no_cover=args.no_cover,
        no_email=args.no_email,
        output=args.output,
        publish_jekyll=args.publish_jekyll,
        site_root=args.site_root,
        cover_request_timeout_seconds=args.cover_request_timeout_seconds,
        cover_max_seconds=args.cover_max_seconds,
        cover_attempts=args.cover_attempts,
        cover_mode=args.cover_mode,
        image_search_max_seconds=args.image_search_max_seconds,
        image_max_count=args.image_max_count,
        chunk_total_target_words=args.chunk_total_target_words,
        chunk_max_total_words=args.chunk_max_total_words,
        chunk_section_max_output_tokens=args.chunk_section_max_output_tokens,
        chunk_section_timeout_seconds=args.chunk_section_timeout_seconds,
        chunk_total_timeout_seconds=args.chunk_total_timeout_seconds,
        debug_log=args.debug_log,
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

    run_started_at = time.monotonic()
    run_status = "failed"
    failed_stage = "initialization"
    failure_error: str | None = None

    # 1 – Generate essay
    try:
        failed_stage = "essay_generation"
        title, content_md, essay_meta = generate_essay(
            openai_key,
            args.system_prompt,
            args.user_prompt,
            args.text_model,
            run_id,
            chunk_total_target_words=args.chunk_total_target_words,
            chunk_max_total_words=args.chunk_max_total_words,
            chunk_section_max_output_tokens=args.chunk_section_max_output_tokens,
            chunk_section_timeout_seconds=args.chunk_section_timeout_seconds,
            chunk_total_timeout_seconds=args.chunk_total_timeout_seconds,
            chunk_compress_if_over_limit=not args.no_chunk_compress,
        )
        print(f'Title: "{title}"')
        preview_slug = str(essay_meta.get("preview_slug") or _fallback_preview_slug(title)).strip()
        preview_text = str(essay_meta.get("preview_text") or _fallback_preview_text(content_md)).strip()

        # 2 – Fetch content images
        failed_stage = "content_images"
        content_images: list[dict] = []
        if not args.no_images:
            image_urls = extract_image_urls(content_md)
            image_queries = extract_image_suggestions(content_md)
            fallback_topics = extract_key_topics(title, content_md)
            content_images = fetch_content_images(
                image_urls,
                image_queries,
                fallback_topics,
                run_id,
                max_images=args.image_max_count,
                max_total_seconds=args.image_search_max_seconds,
            )
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
        failed_stage = "cover_generation"
        if args.no_cover:
            cover_bytes = _make_fallback_cover(title)
            _log_script_event(run_id, "cover_generation_skipped", reason="--no-cover")
        elif args.cover_mode == "pillow":
            cover_bytes = _make_fallback_cover(title)
            _log_script_event(run_id, "cover_generation_skipped", reason="--cover-mode pillow")
        elif args.cover_mode == "auto" and content_images:
            cover_bytes = content_images[0]["bytes"]
            _log_script_event(
                run_id,
                "cover_generation_from_content_image",
                source_title=content_images[0].get("title"),
                source_url=content_images[0].get("source_url"),
            )
        else:
            cover_bytes = generate_cover_image(
                openai_key,
                title,
                args.image_model,
                run_id,
                request_timeout_seconds=args.cover_request_timeout_seconds,
                max_total_seconds=args.cover_max_seconds,
                attempts=args.cover_attempts,
            )

        content_md_with_appendix = _append_generation_appendix(
            content_md,
            user_prompt=args.user_prompt,
            text_model=args.text_model,
            image_model=args.image_model,
            run_id=run_id,
            essay_meta=essay_meta,
        )

        # 4 – Build EPUB
        failed_stage = "epub_build"
        epub_bytes = create_epub(
            title,
            content_md_with_appendix,
            cover_bytes,
            content_images,
            preview_text,
            args.user_prompt,
            args.text_model,
            args.image_model,
            run_id,
        )

        # 5 – Save locally
        failed_stage = "local_save"
        epub_path, md_path = _save_outputs(
            title,
            epub_bytes,
            cover_bytes,
            content_md,
            content_images,
            args.user_prompt,
            args.text_model,
            args.image_model,
            essay_meta,
            preview_slug,
            preview_text,
            args.output,
            args.publish_jekyll,
            args.site_root,
            run_id,
        )
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
        failed_stage = "email_send"
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

        run_status = "completed"
        failed_stage = "none"
    except Exception as exc:
        failure_error = str(exc)
        if failed_stage == "essay_generation" and _is_insufficient_quota_error(exc):
            print(
                "Error: OpenAI quota is exhausted for this account. Use a cheaper model for testing, "
                "or add billing/quota before running GPT-5.5 essays.",
                file=sys.stderr,
            )
        _log_essay_run(
            status="failed",
            run_id=run_id,
            title=locals().get("title"),
            user_prompt=args.user_prompt,
            output_path=args.output,
            text_model=args.text_model,
            image_model=args.image_model,
            error=failure_error,
        )
        _log_script_event(run_id, "run_failed", stage=failed_stage, error=failure_error)
        print(
            f"Error: run failed during {failed_stage}. The prompt was logged and you can rerun safely.",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        _log_script_event(
            run_id,
            "run_complete",
            status=run_status,
            failed_stage=failed_stage,
            error=failure_error,
            elapsed_seconds=round(time.monotonic() - run_started_at, 3),
        )


if __name__ == "__main__":
    main()
