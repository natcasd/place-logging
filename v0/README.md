# v0 — shared recommendation-ingest API and Telegram bot

The service preserves each source post, extracts individual saved things, and
optionally resolves physical locations through Google Places.

## Saved-things model

- Every ingest creates a source record, even when extraction returns no things.
- Instagram media is archived under `WORKDIR/sources` on the mounted Fly volume.
- Each extracted thing has an open-ended type, detailed description, optional
  availability dates, and optional Google location.
- Existing place rows migrate in place with `Place` as their default type. Before
  the first additive migration, the service creates a timestamped SQLite backup
  beside the database.
- `/api/v1/things` and `/api/v1/sources` power new clients. `/api/v1/places`
  remains available for released clients.

## Layout

- `app.py` — FastAPI entry point and HTTP transport
- `bot.py` — Telegram transport adapter
- `ingest_service.py` — shared process-and-persist application service
- `pipeline.py` — platform-aware `ingest → extract → resolve` pipeline
- `store.py` — SQLite schema + `save_ingest()`
- `data/` — temporary Instagram media cache + `places.db` (both gitignored)

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

**Terminal 2 — API and bot:**
```bash
source .venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000
```

Then share a public Instagram image, carousel, Reel, or YouTube video to your bot via the iOS share sheet → Telegram → pick your bot. Or type a message containing a URL. YouTube URLs are sent directly to Gemini. Instagram videos are fetched with `yt-dlp`; image URLs exposed by the same metadata are downloaded directly. All carousel media and the available combined caption text are analyzed together.

The bot replies "🔎 Working on it…", then edits that message with the final result once the pipeline finishes (~10–30s).

## Shared ingest API

Telegram and HTTP clients use the same `IngestService`, so extraction,
resolution, and persistence behave consistently across transports.

```bash
curl https://place-logging.fly.dev/api/v1/ingests \
  --request POST \
  --header "Authorization: Bearer $INGEST_API_TOKEN" \
  --header "Content-Type: application/json" \
  --data '{"source_url":"https://youtu.be/example","delivery":"response_only"}'
```

Set `delivery` to `telegram` to send progress and the final result to
`SHORTCUT_TELEGRAM_CHAT_ID` (or the first allowed Telegram user). The request
stays open until processing and persistence complete, which keeps a
scale-to-zero Fly machine alive for the full job.

### iPhone Shortcut

Create a shortcut named **Save to Place Logger**:

1. Open its details, enable **Show in Share Sheet**, and accept **URLs** and
   **Text**.
2. Add **Get URLs from Shortcut Input**.
3. Add **Get Item from List** and select **First Item**.
4. Add **Get Contents of URL** with:
   - URL: `https://place-logging.fly.dev/api/v1/ingests`
   - Method: `POST`
   - Header: `Authorization` = `Bearer <your INGEST_API_TOKEN>`
   - Request body: JSON
   - `source_url`: the first URL from the previous action
   - `delivery`: `telegram`
5. Add **Show Notification** with “Saved to Place Logger.”

The token is separate from the Telegram bot token and can be rotated without
recreating the bot. Do not publish a Shortcut containing the token; use a
per-user token or an import question before sharing it with another person.

## Sanity-checking a run

```bash
sqlite3 data/places.db 'select id, source_url, created_at from items order by id desc limit 5;'
sqlite3 data/places.db 'select p.id, p.extracted_name, p.resolution_status, p.formatted_address from places p order by p.id desc limit 10;'
```

## Media-reference backfill

`backfill_media_references.py` fills timestamps and carousel slide indexes for
older multi-place posts without rerunning Google Places resolution. Its default
mode writes a reviewable plan and makes no database changes. `--apply` consumes
that exact plan, creates a SQLite backup, verifies every target row is unchanged,
and updates only `timestamp_seconds` and `slide_index`. Plan generation writes
an atomic checkpoint after every post; rerun it with `--resume` after an
interruption to skip completed posts.

```bash
python backfill_media_references.py \
  --db-path data/places.db \
  --workdir data/downloads \
  --plan data/media-reference-backfill-plan.json

python backfill_media_references.py \
  --db-path data/places.db \
  --workdir data/downloads \
  --plan data/media-reference-backfill-plan.json \
  --apply
```

## Known gaps

- Disambiguation of `needs_review` location candidates is not yet available in the UI.
- TikTok is temporarily unsupported while its upstream downloader support is unstable.
- No retry / error recovery for Instagram rate limits.
- URL-only ingest (pure-text + article/tweet URLs are the v0.5 expansion in doc 09).
- Routes use resolvable anchors such as a trailhead or venue; custom route
  geometry is intentionally not synthesized from a post.

## Delivery

- Pull requests and pushes to `main` run `.github/workflows/ci.yml`.
- A successful CI run for a push to `main` triggers `.github/workflows/deploy.yml`.
- Deployment uses an app-scoped `FLY_API_TOKEN`, updates existing Machines only,
  disables Fly high-availability provisioning, and preserves scale-to-zero.
- Pull requests never receive the production Fly token and never deploy.
