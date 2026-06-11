# emailMeAnEssay

Generate an essay on any topic using OpenAI, bundle it into a polished EPUB
(with GPT-generated cover art or a Pillow fallback, model-suggested image
placements preferred over Wikimedia Commons, and rate-limited Commons lookups
when needed), save it locally, and email it to any address – including your
Kindle's personal-document address.

---

## Features

| Feature | Detail |
|---|---|
| **Essay generation** | GPT-5.5 via the Responses API, configurable with `--text-model` (GPT-5.5 uses chunked section generation for long-form reliability) |
| **Cover image** | GPT Image, configurable with `--image-model`; falls back to a styled Pillow image if unavailable |
| **Content images** | Model-suggested image hints first, then rate-limited Wikimedia Commons fallback |
| **Essay previews** | Generates a short preview slug and card text for Jekyll listing pages |
| **EPUB output** | Valid EPUB 3 with embedded CSS, cover, images, prompt metadata, and one subdirectory per essay bundle |
| **Run logging** | Records prompt audit entries in `output/logs/essay_runs.jsonl` and detailed script events in `output/logs/script_events.jsonl` |
| **Email delivery** | SMTP with STARTTLS (port 587) or SSL (port 465) |
| **Kindle-ready** | Attach the EPUB directly to a Kindle email address |

---

## Quick start

### 1 – Install dependencies

```bash
pip install -r requirements.txt
```

### 2 – Configure credentials

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
# then edit .env
```

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | ✅ | Your OpenAI API key |
| `SMTP_HOST` | ✅ | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | – | Default `587` (STARTTLS); use `465` for SSL |
| `SMTP_USER` | ✅ | Your email address / login |
| `SMTP_PASSWORD` | ✅ | Your password or App Password |
| `FROM_EMAIL` | – | Sender address (defaults to `SMTP_USER`) |
| `TO_EMAIL` | – | Recipient address used when `--email` is omitted |

> **Gmail users:** generate an [App Password](https://support.google.com/accounts/answer/185833)
> instead of your normal password if you have 2-step verification enabled.

### 3 – Run

```bash
# Send an essay to a Kindle address
python essay_emailer.py \
    --user-prompt "Write an essay about the philosophy of Stoicism" \
    --email yourname_kindle@kindle.com

# Or set the recipient in .env and omit --email
TO_EMAIL=yourname_kindle@kindle.com
python essay_emailer.py \
  --user-prompt "Write an essay about the philosophy of Stoicism"

# Use a custom system prompt
python essay_emailer.py \
    --system-prompt "You are a witty science writer. Use clear language and analogies." \
    --user-prompt "Explain quantum entanglement" \
    --email yourname_kindle@kindle.com

# Use different models if needed
python essay_emailer.py \
  --text-model gpt-5.5 \
  --image-model gpt-image-2 \
  --user-prompt "Write about the history of the World Cup" \
  --email yourname_kindle@kindle.com

# Edit the default system prompt
Edit [system_prompt.txt](system_prompt.txt) to change the prompt used when
`--system-prompt` is not provided.

# Save locally without emailing (useful for testing)
python essay_emailer.py \
    --user-prompt "The history of the printing press" \
    --email unused@example.com \
    --no-email \
    --output printing_press.epub

The EPUB and markdown for that run are written to `printing_press/` as
`printing_press.epub` and `printing_press.md`.

# Skip images (faster / no network calls to Wikimedia)
python essay_emailer.py \
    --user-prompt "Renaissance art" \
    --email you@example.com \
    --no-images

# Publish directly into a GitHub Pages (Jekyll) docs site
python essay_emailer.py \
  --user-prompt "The history of cryptography" \
  --no-email \
  --publish-jekyll
```

---

## Command-line options

```
usage: essay_emailer.py [-h] [--system-prompt TEXT] --user-prompt TEXT
                        [--text-model NAME] [--image-model NAME]
                        --email ADDRESS [--no-images] [--no-cover]
                        [--cover-mode {auto,ai,pillow}]
                        [--cover-request-timeout-seconds SECONDS]
                        [--cover-max-seconds SECONDS]
                        [--cover-attempts N]
                        [--image-search-max-seconds SECONDS]
                        [--image-max-count N]
                        [--output FILE] [--publish-jekyll]
                        [--no-publish-jekyll] [--site-root DIR]
                        [--no-email]
                        [--debug-log]
                        [--chunk-total-target-words N]
                        [--chunk-max-total-words N]
                        [--chunk-section-max-output-tokens N]
                        [--chunk-section-timeout-seconds SECONDS]
                        [--chunk-total-timeout-seconds SECONDS]
                        [--no-chunk-compress]

optional arguments:
  --text-model NAME     Text generation model (default: gpt-5.5)
  --image-model NAME    Cover image model (default: gpt-image-1-mini)
  --system-prompt TEXT  System prompt for the AI
                        (default loaded from system_prompt.txt)
  --user-prompt TEXT    The essay topic, question, or instruction  [required]
  --email ADDRESS       Recipient email address (defaults to TO_EMAIL)
  --no-images           Skip embedding content images in the EPUB
  --no-cover            Use a simple generated cover instead of DALL-E
  --cover-mode {auto,ai,pillow}
                        Cover strategy: auto (prefer first content image,
                        then AI, then Pillow), ai (force AI attempt), or
                        pillow (always local Pillow cover) (default: auto)
  --cover-request-timeout-seconds SECONDS
                        Timeout per cover API request before retry/fallback
                        (default: 75)
  --cover-max-seconds SECONDS
                        Maximum total time budget for cover generation across
                        retries (default: 180)
  --cover-attempts N    Maximum cover generation attempts before fallback
                        (default: 3)
  --image-search-max-seconds SECONDS
                        Maximum total time budget for content image search
                        (default: 300)
  --image-max-count N   Maximum number of content images to embed (default: 8)
  --output FILE         Also save the EPUB and markdown into a dedicated
                        subdirectory at this path
  --publish-jekyll      Publish output in a Jekyll-ready site structure under
                        --site-root (default: enabled)
  --no-publish-jekyll   Disable Jekyll website publishing for this run
  --site-root DIR       Root directory for Jekyll publishing when
                        --publish-jekyll is enabled (default: docs)
  --no-email            Skip sending email (combine with --output to just save
                        locally)
  --debug-log           Enable verbose debug logging to output/logs/debug-
                        YYYY-MM-DD.log
  --chunk-total-target-words N
                        Target total words for chunked generation
                        planning (default: 7000)
  --chunk-max-total-words N
                        Hard maximum total words for chunked output
                        (default: 10000)
  --chunk-section-max-output-tokens N
                        Hard cap on output tokens per chunk section call
                        (default: 900)
  --chunk-section-timeout-seconds SECONDS
                        Maximum time budget for a single chunk section
                        call with retries (default: 180)
  --chunk-total-timeout-seconds SECONDS
                        Maximum time budget for all chunked essay calls
                        combined (default: 900)
  --no-chunk-compress   Disable automatic final compression pass when
                        chunked output exceeds max words
```

---

## How it works

```
┌──────────────────────────────────────────────────────────────┐
│  1. GPT-5.5 (or your chosen text model) generates the essay  │
│  2. The prompt and run metadata are logged locally           │
│  3. GPT Image (or your chosen image model) creates a cover   │
│  4. Model image hints are used first; Wikimedia is fallback  │
│  5. ebooklib assembles a valid EPUB 3 file                   │
│  6. The EPUB is saved locally and can be emailed via SMTP    │
└──────────────────────────────────────────────────────────────┘
```

Images are inserted after section headings so they sit naturally within the
text flow. The EPUB has embedded CSS for comfortable reading on all e-readers,
and the prompt used to generate the essay is included in the EPUB metadata.
The script logs the run before generation starts so prompts are captured even
if the OpenAI request fails, then logs a completion record after the EPUB is
saved. When the OpenAI response is available, the run log includes the response
ID, token usage, and elapsed time. A second JSONL log captures per-step script
events such as retries, content-image search results, EPUB creation, and email
delivery outcomes.

---

## Notes

* **Kindle:** add your Gmail (or other sending address) to the list of
  approved senders in your Amazon account before sending.
* **Model safety:** the script has tested profiles for `gpt-4.1-mini` and
  `gpt-5.5`. Other text models are still allowed, but the script prints and logs
  a warning that behavior may vary.
* **Cover reliability:** default `--cover-mode auto` prefers a downloaded
  content image as cover first (cheap and reliable), then attempts OpenAI image
  generation, then falls back to a local Pillow cover.
* **Cover cost control:** the default cover model is now `gpt-image-1-mini`.
* **Costs:** each run uses GPT-5.5 for text generation and, unless
  `--no-cover` is passed, GPT Image for cover creation by default. You can
  override both with `--text-model` and `--image-model`. Check
  [OpenAI pricing](https://openai.com/pricing) for current rates.
* **Images:** model-generated image hints are preferred. Wikimedia Commons
  images are freely licensed but attribution requirements vary, and lookups
  are rate-limited with retries. The image title/caption is embedded in the EPUB.
* **Logging:** `output/logs/essay_runs.jsonl` records the prompt audit trail
  plus response metadata such as token usage and elapsed time when available,
  and `output/logs/script_events.jsonl` records detailed step-by-step events,
  including retries, image selection, EPUB creation, and email delivery.
  Use `--debug-log` for extra per-request diagnostics in
  `output/logs/debug-YYYY-MM-DD.log`.
* **GitHub Pages publishing:** website publishing is now enabled by default.
  Use `--no-publish-jekyll` to disable it for one run. Published essays are
  written using standard Jekyll posts and assets:
  * `docs/_posts/YYYY-MM-DD-<slug>.md` with front matter for preview metadata
  * `docs/assets/essays/<slug>/<slug>.epub` for downloads
  * `docs/assets/essays/<slug>/images/content_*.jpg` for inline markdown images
  * `docs/assets/essays/<slug>/images/preview_1200x630.jpg` (and card variant)
  Index listing uses `paginator.posts` from `docs/index.html`.

---

## GitHub Pages Setup

This repo now includes a Pages workflow at `.github/workflows/pages.yml` and a
Jekyll site scaffold under `docs/`.

1. In GitHub repo settings, set Pages source to **GitHub Actions**.
2. Ensure your DNS has a CNAME record for `essays.dfbr.co.uk` pointing to
   `<your-github-username>.github.io`.
3. Push to `main` and the workflow will build/deploy `docs/`.
4. New runs appear on the index page by default (use `--no-publish-jekyll` to opt out).

---

## GitHub Scheduled Email Trigger

If you do not want to run from your laptop, this repository includes
`.github/workflows/email-trigger.yml` and `email_trigger_runner.py`.

How it works:

1. GitHub Actions runs on a schedule (twice daily) or manually.
2. The runner connects to IMAP and reads unread messages.
3. A message is accepted only when both conditions are true:
   - Sender is in your approved allowlist.
   - Message recipients include your configured trigger address.
4. The email body is used as `--user-prompt` for `essay_emailer.py`.
5. The normal pipeline sends the EPUB to your Kindle via SMTP.

No magic subject/body words are required.

Required GitHub Secrets:

Core runtime secrets:

* `OPENAI_API_KEY`
* `SMTP_HOST`
* `SMTP_PORT`
* `SMTP_USER`
* `SMTP_PASSWORD`
* `FROM_EMAIL`
* `TO_EMAIL`

IMAP trigger secrets:

* `IMAP_HOST`
* `IMAP_PORT` (usually `993`)
* `IMAP_USER`
* `IMAP_PASSWORD`
* `TRIGGER_RECIPIENT` (the inbox address that must appear in recipients)
* `TRIGGER_ALLOWED_FROM` (comma-separated allowed sender addresses)

Optional trigger tuning secrets:

* `TRIGGER_MAILBOX` (default: `INBOX`)
* `TRIGGER_MAX_MESSAGES_PER_RUN` (default: `3`)
* `TRIGGER_MAX_PROMPT_CHARS` (default: `12000`)
* `TRIGGER_TEXT_MODEL` (default: `gpt-4.1-mini`)
* `TRIGGER_IMAGE_MODEL` (default: `gpt-image-1-mini`)
* `TRIGGER_NO_IMAGES` (`true`/`false`)
* `TRIGGER_NO_COVER` (`true`/`false`)
* `TRIGGER_NO_EMAIL` (`true`/`false`)
* `TRIGGER_OUTPUT_PATH` (optional)

Operational note:

* The trigger runner writes poller events to `output/logs/trigger_runs.jsonl`
  in the workflow workspace for diagnostics.
