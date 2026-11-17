# Workana Crawler

A bot that monitors new projects on [Workana](https://www.workana.com), drafts proposals with OpenAI and lets you review and submit them from Telegram.

## How it works

1. Every 30 minutes (configurable), a headless Chromium session (Playwright) reads the Workana job listing. Jobs it has already seen are tracked in a local SQLite database.
2. Each new job is sent to Telegram as a card. From the card you can generate a **proposal**, generate a **question** for the client, or **ignore** the job.
3. You choose a writing style and can add your price or extra context. The proposal text is then generated with the OpenAI API.
4. You review the draft, then **send** it (the bot fills in and submits the bid form, including skills) or **regenerate** it.
5. If Workana shows a Cloudflare challenge or captcha, the bot switches to manual mode. It prepares a draft, holds the page open and sends you a link to finish the bid yourself.

The bot also warns you on Telegram when the session cookies are about to expire or the listing keeps coming back empty.

## Stack

Python 3.10+, Playwright, BeautifulSoup/lxml, SQLAlchemy (aiosqlite), python-telegram-bot, OpenAI SDK, Click, Rich.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp config.example.yaml config.yaml   # then fill in your credentials
```

`config.yaml` holds:
- Telegram bot token and chat ID
- OpenAI API key and model
- Workana login (email/password) and session cookies
- the job listing URL to watch
- manual-submit settings

The file is git-ignored.

The freelancer profile used in the prompts is the `PROFILE` constant in `src/gpt.py`. Replace it with your own.

## Usage

```bash
python main.py run          # start the bot (scheduled collection + Telegram handlers)
python main.py run --once   # collect once and exit
python main.py -v run       # verbose logging
```

Logs and screenshots are written to `logs/`. Seen jobs are stored in `workana.db`.

> The prompts and Telegram UI are in Portuguese. Automating a third-party platform may conflict with its terms of service, so use this at your own risk.
