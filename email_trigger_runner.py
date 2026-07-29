#!/usr/bin/env python3
"""Poll an IMAP inbox and turn approved emails into essay generation runs."""

from __future__ import annotations

import email
import imaplib
import json
import os
import subprocess
import sys
import time
from email.message import Message
from email.utils import getaddresses, parseaddr
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent
LOG_PATH = REPO_ROOT / "output" / "logs" / "trigger_runs.jsonl"


class TriggerConfigurationError(RuntimeError):
    pass


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise TriggerConfigurationError(f"Environment variable {name} is required.")
    return value


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _log_event(event: str, **fields: object) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        **fields,
    }
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _allowed_senders() -> set[str]:
    raw = _require_env("TRIGGER_ALLOWED_FROM")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _collect_recipients(message: Message) -> set[str]:
    values: list[str] = []
    for header in ("to", "cc", "bcc", "resent-to", "delivered-to", "x-original-to"):
        values.extend(message.get_all(header, []))
    return {address.lower() for _, address in getaddresses(values) if address}


def _extract_body(message: Message) -> str:
    if message.is_multipart():
        plain_parts: list[str] = []
        html_parts: list[str] = []
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_filename():
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace").strip()
            content_type = part.get_content_type()
            if content_type == "text/plain" and text:
                plain_parts.append(text)
            elif content_type == "text/html" and text:
                html_parts.append(text)
        if plain_parts:
            return "\n\n".join(plain_parts).strip()
        if html_parts:
            stripped = html_parts[0]
            stripped = stripped.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
            stripped = stripped.replace("</p>", "\n\n")
            import re

            stripped = re.sub(r"<[^>]+>", " ", stripped)
            return " ".join(stripped.split())
        return ""

    payload = message.get_payload(decode=True) or b""
    charset = message.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace").strip()


def _build_command(prompt: str) -> list[str]:
    script = REPO_ROOT / "essay_emailer.py"
    command = [sys.executable, str(script), "--user-prompt", prompt]

    optional_pairs = [
        ("TRIGGER_TEXT_PROVIDER", "--text-provider"),
        ("TRIGGER_TEXT_MODEL", "--text-model"),
        ("TRIGGER_IMAGE_PROVIDER", "--image-provider"),
        ("TRIGGER_IMAGE_MODEL", "--image-model"),
        ("TRIGGER_EMAIL", "--email"),
        ("TRIGGER_SITE_ROOT", "--site-root"),
        ("TRIGGER_SYSTEM_PROMPT", "--system-prompt"),
    ]
    for env_name, flag in optional_pairs:
        value = os.getenv(env_name)
        if value:
            command.extend([flag, value])

    if _bool_env("TRIGGER_NO_EMAIL"):
        command.append("--no-email")
    if _bool_env("TRIGGER_NO_PUBLISH_JEKYLL"):
        command.append("--no-publish-jekyll")
    return command


def _should_process(message: Message, trigger_recipient: str, allowed_from: set[str]) -> tuple[bool, str]:
    sender = parseaddr(message.get("from", ""))[1].lower()
    if sender not in allowed_from:
        return False, f"sender_not_allowed:{sender}"
    recipients = _collect_recipients(message)
    if trigger_recipient not in recipients:
        return False, "recipient_missing"
    return True, "accepted"


def process_messages() -> int:
    host = _require_env("IMAP_HOST")
    port = _int_env("IMAP_PORT", 993)
    user = _require_env("IMAP_USER")
    password = _require_env("IMAP_PASSWORD")
    mailbox = os.getenv("TRIGGER_MAILBOX", "INBOX")
    trigger_recipient = _require_env("TRIGGER_RECIPIENT").lower()
    allowed_from = _allowed_senders()
    max_messages = _int_env("TRIGGER_MAX_MESSAGES_PER_RUN", 3)
    max_prompt_chars = _int_env("TRIGGER_MAX_PROMPT_CHARS", 12000)

    processed = 0
    with imaplib.IMAP4_SSL(host, port) as client:
        client.login(user, password)
        status, _ = client.select(mailbox)
        if status != "OK":
            raise RuntimeError(f"Failed to select mailbox {mailbox}.")

        status, data = client.search(None, "UNSEEN")
        if status != "OK":
            raise RuntimeError("Failed to search for unread messages.")

        message_ids = data[0].split()[:max_messages]
        _log_event("poll_started", mailbox=mailbox, unread_count=len(message_ids))

        for message_id in message_ids:
            status, payload = client.fetch(message_id, "(RFC822)")
            if status != "OK":
                _log_event("fetch_failed", message_id=message_id.decode())
                continue

            raw_bytes = payload[0][1]
            message = email.message_from_bytes(raw_bytes)
            should_process, reason = _should_process(message, trigger_recipient, allowed_from)
            sender = parseaddr(message.get("from", ""))[1].lower()
            subject = message.get("subject", "")

            if not should_process:
                client.store(message_id, "+FLAGS", "\\Seen")
                _log_event(
                    "message_skipped",
                    message_id=message_id.decode(),
                    sender=sender,
                    subject=subject,
                    reason=reason,
                )
                continue

            prompt = _extract_body(message)
            prompt = prompt[:max_prompt_chars].strip()
            if not prompt:
                client.store(message_id, "+FLAGS", "\\Seen")
                _log_event(
                    "message_skipped",
                    message_id=message_id.decode(),
                    sender=sender,
                    subject=subject,
                    reason="empty_prompt",
                )
                continue

            command = _build_command(prompt)
            result = subprocess.run(
                command,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
            )
            if result.returncode == 0:
                client.store(message_id, "+FLAGS", "\\Seen")
                processed += 1
                _log_event(
                    "message_processed",
                    message_id=message_id.decode(),
                    sender=sender,
                    subject=subject,
                    command=command,
                )
                continue

            _log_event(
                "message_failed",
                message_id=message_id.decode(),
                sender=sender,
                subject=subject,
                command=command,
                stdout=result.stdout[-4000:],
                stderr=result.stderr[-4000:],
                returncode=result.returncode,
            )

        client.close()
        client.logout()

    _log_event("poll_finished", processed=processed)
    return processed


def main() -> None:
    try:
        processed = process_messages()
        print(f"Processed {processed} message(s).")
    except Exception as exc:  # noqa: BLE001
        _log_event("runner_failed", error=str(exc))
        print(f"Error: trigger runner failed -> {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
