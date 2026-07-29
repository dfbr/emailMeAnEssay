# GitHub Secrets Reference

This repository can now run the essay pipeline directly or through the scheduled email trigger workflow.

## Core Secrets

| Secret | Required | Purpose |
|---|---|---|
| `SMTP_HOST` | Yes for email delivery | Outgoing SMTP server host |
| `SMTP_PORT` | Yes for email delivery | SMTP port (`587` for STARTTLS, `465` for SSL) |
| `SMTP_USER` | Yes for email delivery | SMTP login username/email |
| `SMTP_PASSWORD` | Yes for email delivery | SMTP password or app password |
| `FROM_EMAIL` | Optional | From header address; defaults to `SMTP_USER` |
| `TO_EMAIL` | Optional | Default recipient when `--email` is not supplied |
| `TEXT_PROVIDER` | Optional | Default text provider (`openai`, `gemini`, `openrouter`, `groq`, `xai`) |
| `TEXT_MODEL` | Optional | Default text model for the main script |
| `IMAGE_PROVIDER` | Optional | Default image provider (`openai`, `gemini`, `pillow`) |
| `IMAGE_MODEL` | Optional | Default image model for the main script |

## Provider API Secrets

Store only the keys for the providers you actually use.

| Secret | Required When | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | Using `openai` | OpenAI text/image access |
| `GEMINI_API_KEY` | Using `gemini` | Google Gemini / Imagen access |
| `OPENROUTER_API_KEY` | Using `openrouter` | OpenRouter text access |
| `OPENROUTER_TEXT_MODEL` | Optional | Default text model when `TEXT_PROVIDER` or `TRIGGER_TEXT_PROVIDER` is `openrouter` |
| `GROQ_API_KEY` | Using `groq` | Groq text access |
| `GROQ_TEXT_MODEL` | Optional | Default text model when using Groq |
| `XAI_API_KEY` | Using `xai` | xAI text access |
| `XAI_TEXT_MODEL` | Optional | Default text model when using xAI |

## Email Trigger Secrets

| Secret | Required | Purpose |
|---|---|---|
| `IMAP_HOST` | Yes | IMAP host for the trigger inbox |
| `IMAP_PORT` | Yes | IMAP SSL port, usually `993` |
| `IMAP_USER` | Yes | IMAP login username/email |
| `IMAP_PASSWORD` | Yes | IMAP password or app password |
| `TRIGGER_RECIPIENT` | Yes | The inbound email address that must appear in the email recipients |
| `TRIGGER_ALLOWED_FROM` | Yes | Comma-separated allowlist of senders allowed to trigger runs |
| `TRIGGER_MAILBOX` | Optional | Mailbox/folder to poll; defaults to `INBOX` |
| `TRIGGER_MAX_MESSAGES_PER_RUN` | Optional | Max unread messages processed per workflow run; defaults to `3` |
| `TRIGGER_MAX_PROMPT_CHARS` | Optional | Max prompt characters copied from the email body; defaults to `12000` |
| `TRIGGER_TEXT_PROVIDER` | Optional | Provider override for trigger-driven runs |
| `TRIGGER_TEXT_MODEL` | Optional | Model override for trigger-driven runs |
| `TRIGGER_IMAGE_PROVIDER` | Optional | Image provider override for trigger-driven runs |
| `TRIGGER_IMAGE_MODEL` | Optional | Image model override for trigger-driven runs |
| `TRIGGER_EMAIL` | Optional | Recipient override for trigger-driven runs |
| `TRIGGER_SITE_ROOT` | Optional | Site root override for trigger-driven runs |
| `TRIGGER_SYSTEM_PROMPT` | Optional | Full system prompt override passed to the script |
| `TRIGGER_NO_EMAIL` | Optional | Set to `true` to skip SMTP delivery in workflow runs |
| `TRIGGER_NO_PUBLISH_JEKYLL` | Optional | Set to `true` to skip docs publishing in workflow runs |

## Notes

- Keep these values in GitHub repository secrets or local `.env`, never in committed files.
- For Gmail and similar providers, use app passwords for SMTP/IMAP when required.
- The workflow only needs the provider secrets that match the provider/model you configure.
