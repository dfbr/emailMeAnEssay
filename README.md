# emailMeAnEssay

Generate an essay with an AI model, package it as an EPUB, publish it into the Jekyll site under `docs/`, and optionally email it.

## What it does

1. Uses a configured text provider/model to plan and write the essay.
2. Uses a configured image provider/model to create a cover, with a Pillow fallback.
3. Builds an EPUB from the generated markdown.
4. Writes the result into `docs/` for GitHub Pages or into `output/`.
5. Optionally emails the EPUB to a configured address.
6. Optionally polls an IMAP inbox so an email sent to a trigger address becomes the essay prompt.

## Supported providers

### Text providers

- `openai`
- `gemini`
- `openrouter`
- `groq`
- `xai`

### Image providers

- `openai`
- `gemini`
- `pillow`

Defaults stay close to the existing setup:

- `TEXT_PROVIDER=openai`
- `TEXT_MODEL=gpt-5.5`
- `IMAGE_PROVIDER=openai`
- `IMAGE_MODEL=gpt-image-2`

## Installation

```bash
pip install -r requirements.txt
cp .env.example .env
```

Fill in the values you need in `.env`.

## Local usage

```bash
python essay_emailer.py \
  --user-prompt "Write an essay about the philosophy of Stoicism" \
  --email your_kindle@kindle.com
```

Use different providers/models when needed:

```bash
python essay_emailer.py \
  --user-prompt "Explain the history of cryptography" \
  --text-provider gemini \
  --text-model gemini-2.5-flash \
  --image-provider gemini \
  --image-model imagen-3.0-generate-002 \
  --email your_kindle@kindle.com
```

OpenAI-compatible examples:

```bash
python essay_emailer.py \
  --user-prompt "Explain Roman engineering" \
  --text-provider openrouter \
  --text-model openai/gpt-4o-mini \
  --image-provider pillow \
  --email your_kindle@kindle.com
```

Useful flags:

- `--system-prompt TEXT`
- `--text-provider NAME`
- `--text-model NAME`
- `--image-provider NAME`
- `--image-model NAME`
- `--email ADDRESS`
- `--site-root DIR`
- `--no-publish-jekyll`
- `--no-email`

## Trigger an essay from email

The repository now includes:

- `email_trigger_runner.py`
- `.github/workflows/email-trigger.yml`

How it works:

1. GitHub Actions runs on a schedule or manually.
2. `email_trigger_runner.py` connects to your IMAP inbox.
3. It only accepts unread emails when:
   - the sender is in `TRIGGER_ALLOWED_FROM`
   - the email recipients include `TRIGGER_RECIPIENT`
4. The email body becomes the `--user-prompt` sent to `essay_emailer.py`.
5. The normal EPUB/email/publishing pipeline runs.

## What you need to configure

### Always needed for the email-trigger workflow

- SMTP credentials: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`
- IMAP credentials: `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD`
- Trigger rules: `TRIGGER_RECIPIENT`, `TRIGGER_ALLOWED_FROM`

### Needed for the providers you choose

- OpenAI: `OPENAI_API_KEY`
- Gemini: `GEMINI_API_KEY`
- OpenRouter: `OPENROUTER_API_KEY`
- Groq: `GROQ_API_KEY`
- xAI: `XAI_API_KEY`

See `GITHUB_SECRETS.md` for the full list and what each secret should contain.

## GitHub Actions

- `pages.yml` deploys the `docs/` site.
- `email-trigger.yml` polls the trigger mailbox, runs the generator, and commits any updated `docs/` content back to the repository.

## Logs

- `output/logs/essay_runs.jsonl` records essay runs.
- `output/logs/trigger_runs.jsonl` records trigger polling and processing.

## Notes

- Use app passwords for Gmail/Google Workspace where required.
- For Kindle delivery, make sure the sender address is on your approved senders list in Amazon.
- If you use `openrouter`, `groq`, or `xai`, set an explicit text model unless you have also configured the provider-specific default model env var.
