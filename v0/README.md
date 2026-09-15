# v0 — shared recommendation-ingest API

The service preserves each source post, extracts individual saved entries, and
optionally resolves physical locations through Google Places.

## Capture and recommendation model

- Every ingest creates one `captures` row, even when extraction returns no
  recommendations. A Capture is one unit of information supplied to Jot; social
  URLs are the currently supported input, not a constraint of the model.
- `recommendations` stores one canonical, deduplicated saved thing. `locations`
  stores an optional Google-resolved venue, and `recommendation_mentions`
  preserves what one Capture said about one Recommendation, including its
  description and media reference.
- A Recommendation has zero or one Location. Many Recommendations can share a
  Location, and many Captures can mention the same Recommendation.
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
- Existing databases migrate idempotently from `items`, `entries`, and
  `entry_sources` to the terminology above. The service creates a timestamped
  SQLite backup beside the database before the first destructive rename.
- New extraction saves only distinct principal recommendations; it excludes
  scenery, background posters, host venues, suppliers, and creator CTAs unless
  independently recommended. Generic unnamed records such as `Cafe` are dropped.
- Temporary media-download and Gemini analysis failures receive bounded,
  provider-aware retries. Retry-After instructions are honored. Failures that
  outlive the request are kept as durable Activity records and retried by the
  server after restarts without creating failed or duplicate Captures.
- Activity distinguishes analysis, media-download, save, and general processing
  failures. The iOS Activity detail presents the failure and lets the user retry
  the same logical ingest immediately or delete the failure and cancel its
  scheduled retries.
- `/api/v1/entries`, `/api/v1/sources`, and `/api/v1/activity` are the supported
  read APIs.

## Layout

- `app.py` — FastAPI entry point and HTTP transport
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

- `GEMINI_API_KEY` — create an API key for the configured Gemini project
- `GOOGLE_PLACES_API_KEY` — create an API key with Places API access
- `INGEST_API_TOKEN` — a private bearer token for authenticated API requests

## Running

```bash
source .venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000
```

The native iOS share extension submits public Instagram images, carousels and
Reels, TikTok videos, and YouTube videos to this API. YouTube URLs are sent
directly to Gemini. Instagram and TikTok videos are fetched with `yt-dlp`;
image URLs exposed by Instagram metadata are downloaded directly. All supplied
media and available caption text are analyzed together. TikTok support covers
public, individual videos and does not use account cookies.

## Shared ingest API

```bash
curl https://place-logging.fly.dev/api/v1/ingests \
  --request POST \
  --header "Authorization: Bearer $INGEST_API_TOKEN" \
  --header "Content-Type: application/json" \
  --data '{"source_url":"https://youtu.be/example"}'
```

The request stays open until processing and persistence complete, which keeps a
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
5. Add **Show Notification** with “Saved to Place Logger.”

Do not publish a Shortcut containing the token; use a per-user token or an
import question before sharing it with another person.

## Sanity-checking a run

```bash
sqlite3 data/places.db 'select id, source_url, created_at from captures order by id desc limit 5;'
sqlite3 data/places.db 'select id, name, entry_type, location_id from recommendations order by id desc limit 10;'
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

- Automatic retry completion is reflected in Activity when the app refreshes;
  it does not currently send a remote push notification.
- URL-only ingest (pure-text + article/tweet URLs are the v0.5 expansion in doc 09).
- Routes use resolvable anchors such as a trailhead or venue; custom route
  geometry is intentionally not synthesized from a post.

## Delivery

- Pull requests and pushes to `main` run `.github/workflows/ci.yml`.
- A successful CI run for a push to `main` triggers `.github/workflows/deploy.yml`.
- Deployment uses an app-scoped `FLY_API_TOKEN`, updates existing Machines only,
  disables Fly high-availability provisioning, and preserves scale-to-zero.
- Pull requests never receive the production Fly token and never deploy.

## One-time retirement checklist

This checklist applies only to the release that removes the former chat-bot
transport. It is not part of normal setup or deployment.

1. Install an iOS build that sends only `source_url`, and verify one save against
   the old backend. The old API accepts the reduced request, so this is
   backward-compatible.
2. Merge and deploy the backend cleanup. Confirm `/healthz`, make one authenticated
   ingest, and verify that `user_prompt` is absent from both `captures` and
   `ingest_runs`. Startup creates a timestamped database backup before removing
   either legacy column.
3. While the old bot token is still available to the running Fly Machine, call
   the Bot API's `deleteWebhook` method with `drop_pending_updates=true`. Do not
   print or copy the token into shell history.
4. List deployed Fly secrets, then unset `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_ALLOWED_USER_IDS`, `TELEGRAM_WEBHOOK_SECRET`, and
   `SHORTCUT_TELEGRAM_CHAT_ID` if each is present. This causes a Fly release, so
   wait for it to become healthy before continuing.
5. Confirm `/webhook` returns `404`, `/healthz` remains healthy, and another native
   iOS save succeeds.
