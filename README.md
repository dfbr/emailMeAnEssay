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
```

---

## Command-line options

```
usage: essay_emailer.py [-h] [--system-prompt TEXT] --user-prompt TEXT
                        [--text-model NAME] [--image-model NAME]
                        --email ADDRESS [--no-images] [--no-cover]
                        [--output FILE] [--no-email]

optional arguments:
  --text-model NAME     Text generation model (default: gpt-5.5)
  --image-model NAME    Cover image model (default: gpt-image-2)
  --system-prompt TEXT  System prompt for the AI
                        (default loaded from system_prompt.txt)
  --user-prompt TEXT    The essay topic, question, or instruction  [required]
  --email ADDRESS       Recipient email address (defaults to TO_EMAIL)
  --no-images           Skip embedding content images in the EPUB
  --no-cover            Use a simple generated cover instead of DALL-E
  --output FILE         Also save the EPUB and markdown into a dedicated
                        subdirectory at this path
  --no-email            Skip sending email (combine with --output to save only)
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
