# v0 — shared recommendation-ingest API and Telegram bot

The service preserves each source post, extracts individual saved entries, and
optionally resolves physical locations through Google Places.

## Saved-entries model

- Every ingest creates a source record, even when extraction returns no entries.
- `entries` stores one canonical recommendation, `locations` stores an optional
  Google-resolved venue, and `entry_sources` records which source recommended
  which Entry together with that source's description and media reference.
- An Entry has zero or one Location. Many Entries can share a Location, and many
  Sources can recommend the same Entry.
- Matching is deliberately conservative: permanent venues match by Google Place
  ID and compatible type; temporary entries additionally require the same title
  and dates; non-location entries require the same title and type. Uncertain
  recommendations remain separate.
- Instagram and TikTok media is downloaded into temporary machine storage for
  extraction, then deleted. The source URL, caption, and extracted source text
  are retained.
- Each extracted entry uses one stable browse type from
  `entry_type_catalog.json`, plus a detailed description and optional timing and
  Google location. Location and timing are properties of an entry rather than
  category allowlists.
- Resolved locations retain Google's display name and Google Place ID. Clients can
  show one map pin per location while keeping distinct saved entries at that pin.
- Repeated saves of the same logical entry can be presented as one card with all of
  its source posts. The newest source description is displayed for now while every
  source-specific description remains stored. Deleting that card removes only its
  Entry and source connections; source posts and other entries at the same location
  remain saved.
- Movie entries are conservatively matched against Wikidata using their title plus
  an extracted release year or director when available. A confident match stores
  its Wikidata ID and an IMDb-based Letterboxd redirect. Google search links are
  generated locally for every Movie. Wikidata read access requires no API key.
- Activity detail shows every recommendation extracted from one source. Resolved
  recommendations appear on its map, unresolved location-based recommendations can
  be deleted, and ambiguous recommendations expand so the user can confirm one of
  the stored location candidates. General field editing is intentionally absent.
- Existing place rows migrate in place with `Unknown` as their temporary type. Before
  the first additive migration, the service creates a timestamped SQLite backup
  beside the database.
- New extraction saves only distinct principal recommendations; it excludes
  scenery, background posters, host venues, suppliers, and creator CTAs unless
  independently recommended. Generic unnamed records such as `Cafe` are dropped.
- Temporary Gemini capacity and rate-limit errors receive bounded exponential
  retries. If extraction still fails, the source URL, caption, and error are
  saved with zero entries so the source remains visible for later review.
- `/api/v1/entries` and `/api/v1/sources` power new clients. `/api/v1/places`
  remains available for released clients.

## Layout

- `app.py` — FastAPI entry point and HTTP transport
- `bot.py` — Telegram transport adapter
- `ingest_service.py` — shared process-and-persist application service
- `pipeline.py` — platform-aware `ingest → extract → resolve` pipeline
- `entry_type_catalog.json` — authoritative type names, classifier definitions,
  icons, ordering, and optional enrichment triggers
- `generate_ios_entry_types.py` — regenerates the checked-in iOS catalog table
- `movie_enrichment.py` — credential-free Wikidata movie matching
- `store.py` — SQLite schema + `save_ingest()`
- `data/` — local SQLite database (gitignored); temporary Instagram and TikTok
  media uses the machine's temp directory and is deleted after each attempt

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

Then share a public Instagram image, carousel, Reel, TikTok, or YouTube video to
your bot via the iOS share sheet → Telegram → pick your bot. Or type a message
containing a URL. YouTube URLs are sent directly to Gemini. Instagram and
TikTok videos are fetched with `yt-dlp`; image URLs exposed by Instagram
metadata are downloaded directly. All supplied media and available caption text
are analyzed together. TikTok support covers public, individual videos and does
not use account cookies.

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

## Generic-type backfill

`backfill_entry_types.py` reclassifies every legacy `Place` row using its saved
source context and description. Its default mode creates a checkpointed,
reviewable plan. Applying a complete plan backs up SQLite, verifies each target
is still generic, updates only `entry_type`, and refuses to commit if any
`Place` rows would remain.

```bash
python backfill_entry_types.py \
  --db-path data/places.db \
  --plan data/type-backfill-plan.json

python backfill_entry_types.py \
  --db-path data/places.db \
  --plan data/type-backfill-plan.json \
  --apply
```

## Type-catalog regression evaluation

Before planning any Bar/Store split backfill, run the new catalog against a
small stratified sample and the checked-in edge-case fixtures. This command is
read-only and cannot update SQLite. Its JSON output shows every current versus
proposed type and scores the fixture set.

```bash
python evaluate_entry_type_migration.py \
  --db-path data/places.db \
  --per-type 5 \
  --output data/type-catalog-evaluation.json
```

Review that report before implementing or applying a separate migration plan.
The intended first pass is roughly 40–60 saved entries, not the full corpus.

## Location-name backfill

`backfill_location_names.py` retrieves Google Places `displayName` for legacy
Locations that retained a Place ID but predate name storage. Plan generation is
checkpointed and read-only with respect to SQLite. Applying a complete plan
backs up the database, verifies every Location is still unnamed, and updates
both the normalized Location and its compatibility rows.

```bash
python backfill_location_names.py \
  --db-path data/places.db \
  --plan data/location-name-backfill-plan.json

python backfill_location_names.py \
  --db-path data/places.db \
  --plan data/location-name-backfill-plan.json \
  --apply
```

## Duplicate-source backfill

`backfill_duplicate_sources.py` groups Instagram and YouTube share-URL variants
by their underlying post/video, keeps the newest successfully processed Source,
and preserves any recommendation found only by an older processing pass. The
apply step verifies the reviewed plan and creates a full database backup first.

```bash
python backfill_duplicate_sources.py \
  --db-path data/places.db \
  --plan data/duplicate-source-backfill-plan.json

python backfill_duplicate_sources.py \
  --db-path data/places.db \
  --plan data/duplicate-source-backfill-plan.json \
  --apply
```

## Movie-link backfill

`backfill_movie_enrichments.py` matches existing Movie entries against Wikidata
and persists exact IMDb-based Letterboxd links. No API credential is required.
Matches are intentionally left unresolved when a title is ambiguous without a
supporting year or director. Pass `--retry` to replace previously saved lookup
results.

```bash
python backfill_movie_enrichments.py --db-path data/places.db
```

## Known gaps

- Instagram or TikTok download/rate-limit failures that remain after bounded
  retries require the source to be shared again.
- URL-only ingest (pure-text + article/tweet URLs are the v0.5 expansion in doc 09).
- Routes use resolvable anchors such as a trailhead or venue; custom route
  geometry is intentionally not synthesized from a post.

## Delivery

- Pull requests and pushes to `main` run `.github/workflows/ci.yml`.
- A successful CI run for a push to `main` triggers `.github/workflows/deploy.yml`.
- Deployment uses an app-scoped `FLY_API_TOKEN`, updates existing Machines only,
  disables Fly high-availability provisioning, and preserves scale-to-zero.
- Pull requests never receive the production Fly token and never deploy.
