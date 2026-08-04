# v0 — Telegram bot scaffold

Minimal working v0: Telegram → Fetcher → Extractor → Resolver → SQLite.

## Layout

- `bot.py` — Telegram webhook handler (entry point)
- `pipeline.py` — `fetch → extract → resolve` as a single `process_ingest()` fn
- `store.py` — SQLite schema + `save_ingest()`
- `data/` — mp4 download cache + `places.db` (both gitignored)

## First-time setup

```bash
cd v0
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in .env (see below for how to get each value)
```

### Getting `.env` values

- `GEMINI_API_KEY` — copy from `../extractor-test/.env`
- `GOOGLE_PLACES_API_KEY` — copy from `../extractor-test/.env`
- `TELEGRAM_BOT_TOKEN` — message `@BotFather` on Telegram → `/newbot` → follow prompts → BotFather returns a token
- `TELEGRAM_ALLOWED_USER_IDS` — message `@userinfobot` on Telegram → it replies with your numeric ID
- `PUBLIC_URL` — from Cloudflare tunnel (see below)

## Running

Two terminals.

**Terminal 1 — tunnel:**
```bash
cloudflared tunnel --url http://localhost:8000
```
Copy the `https://....trycloudflare.com` URL it prints into `.env` as `PUBLIC_URL`.

**Terminal 2 — bot:**
```bash
source .venv/bin/activate
python bot.py
```

Then share an Instagram Reel / TikTok / YouTube to your bot via the iOS share sheet → Telegram → pick your bot. Or type a message containing a URL.

The bot replies "🔎 Working on it…", then edits that message with the final result once the pipeline finishes (~10–30s).

## Sanity-checking a run

```bash
sqlite3 data/places.db 'select id, source_url, created_at from items order by id desc limit 5;'
sqlite3 data/places.db 'select p.id, p.extracted_name, p.resolution_status, p.formatted_address from places p order by p.id desc limit 10;'
```

## Known gaps (intentional for v0)

- No viewer yet — disambiguation of `needs_review` items currently isn't possible through a UI.
- No retry / error recovery for yt-dlp rate limits.
- URL-only ingest (pure-text + article/tweet URLs are the v0.5 expansion in doc 09).
- No nearby-search, filters, or map yet.
