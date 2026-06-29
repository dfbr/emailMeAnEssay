# GitHub Secrets Reference

This file lists all GitHub Actions secrets used by this repository and what each one is for.

## Required For Core Essay Runs

| Secret | What It Is | Example |
|---|---|---|
| OPENAI_API_KEY | OpenAI API key used for text and cover generation. | sk-... |
| SMTP_HOST | Outgoing SMTP server host used to send EPUB emails. | smtp.fastmail.com |
| SMTP_PORT | Outgoing SMTP server port. Use 587 (STARTTLS) or 465 (SSL). | 465 |
| SMTP_USER | SMTP login username/email. | you@example.com |
| SMTP_PASSWORD | SMTP password or app password. | app-password-value |
| FROM_EMAIL | Sender address shown in outgoing email From header. | you@example.com |
| TO_EMAIL | Default destination email (for example, your Kindle address). | your_kindle@kindle.com |

## Required For Scheduled Email Trigger (GitHub Workflow)

| Secret | What It Is | Example |
|---|---|---|
| IMAP_HOST | IMAP server host for trigger mailbox polling. | imap.fastmail.com |
| IMAP_PORT | IMAP server port (usually SSL). | 993 |
| IMAP_USER | IMAP mailbox username/email to poll. | essay-trigger@example.com |
| IMAP_PASSWORD | IMAP mailbox password or app password. | app-password-value |
| TRIGGER_RECIPIENT | Recipient address that must appear in an inbound message for it to trigger. | essay-trigger@example.com |
| TRIGGER_ALLOWED_FROM | Comma-separated list of approved sender emails allowed to trigger runs. | you@example.com,other@example.com |

## Optional Trigger Tuning Secrets

| Secret | What It Is | Default If Missing |
|---|---|---|
| TRIGGER_MAILBOX | IMAP mailbox/folder to poll. | INBOX |
| TRIGGER_MAX_MESSAGES_PER_RUN | Max unread messages processed per scheduled workflow run. | 3 |
| TRIGGER_MAX_PROMPT_CHARS | Max characters copied from email body into prompt. | 12000 |
| TRIGGER_TEXT_MODEL | Text model used when trigger launches essay_emailer.py. | gpt-4.1-mini |
| TRIGGER_IMAGE_MODEL | Cover model used when trigger launches essay_emailer.py. | gpt-image-2 |
| TRIGGER_NO_IMAGES | If true, skip content images. | false |
| TRIGGER_NO_COVER | If true, skip generated cover. | false |
| TRIGGER_NO_EMAIL | If true, do not send resulting EPUB via SMTP. | false |
| TRIGGER_OUTPUT_PATH | Optional output path passed to script. | (empty) |

## Notes

- Keep all values in GitHub repository secrets, not committed files.
- For Gmail/Fastmail and similar providers, use app passwords where required.
- If you do not use the scheduled email trigger workflow, IMAP/TRIGGER secrets are not needed.
