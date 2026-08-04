# Full Architecture — Current View

*Captured 2026-04-17. This is the consolidated, current picture of how we want v0 to work. Supersedes the earlier sketch in doc 05 where they disagree. Detail on specific components lives in docs 05–07.*

## End-to-end diagram

```
 ┌───────────────────────────────────────────────────────────────────┐
 │  USER on iPhone, watching a Reel / TikTok / YouTube               │
 └─────────────────────────────┬─────────────────────────────────────┘
                               │  taps Share → Telegram → our bot
                               ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │  TELEGRAM BOT                                                     │
 │    receives the URL in a DM                                       │
 │    forwards webhook → backend                                     │
 │    (bot replies "working on it…" immediately so share-and-forget) │
 └─────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │  BACKEND — single pluggable pipeline                              │
 │                                                                   │
 │  ╔═══════════════════════════════════════════════════════════╗    │
 │  ║  1. INGEST                                                ║    │
 │  ║     { source_url, raw_text_from_share, user_id } → queue  ║    │
 │  ╚══════════════════════════╤════════════════════════════════╝    │
 │                             ▼                                     │
 │  ╔═══════════════════════════════════════════════════════════╗    │
 │  ║  2. FETCHER  (per-platform strategy)                      ║    │
 │  ║                                                           ║    │
 │  ║   YouTube        → pass URL directly to Gemini (native)   ║    │
 │  ║   Instagram/TT   → yt-dlp → mp4 + metadata.json           ║    │
 │  ║                      ↳ Apify fallback on yt-dlp failure   ║    │
 │  ║   shared mp4     → already local                          ║    │
 │  ║                                                           ║    │
 │  ║   Output: local mp4 path OR URL for Gemini, plus caption  ║    │
 │  ║           + native location tag (if present) + metadata   ║    │
 │  ╚══════════════════════════╤════════════════════════════════╝    │
 │                             ▼                                     │
 │  ╔═══════════════════════════════════════════════════════════╗    │
 │  ║  3. CLASSIFIER  (LLM, one short prompt)                   ║    │
 │  ║     → { vertical: "place" }    [hardcoded to "place" v0]  ║    │
 │  ║     Designed pluggable for future verticals (product,     ║    │
 │  ║     recipe, read-later, media-rec, …).                    ║    │
 │  ╚══════════════════════════╤════════════════════════════════╝    │
 │                             ▼                                     │
 │  ╔═══════════════════════════════════════════════════════════╗    │
 │  ║  4. EXTRACTOR  (Gemini 2.5 Flash, video understanding)    ║    │
 │  ║     Input: (mp4 uploaded via Files API or YT URL)         ║    │
 │  ║          + caption/description                            ║    │
 │  ║          + uploader, hashtags, native_location_tag,       ║    │
 │  ║            upload_date, engagement counts                 ║    │
 │  ║       — all as supporting evidence alongside the video    ║    │
 │  ║     Output: places[] — each with                          ║    │
 │  ║       { extracted_name,                                   ║    │
 │  ║         location_hints:                                   ║    │
 │  ║           { native_tag?, neighborhood?, city?,            ║    │
 │  ║             region_or_country?, on_screen_text?,          ║    │
 │  ║             visual_landmarks? },                          ║    │
 │  ║         dishes[], why_its_cool, tags[],                   ║    │
 │  ║         extraction_confidence }                           ║    │
 │  ║     Empty array allowed for non-place content.            ║    │
 │  ║     Confirmed behavior: returns [] cleanly on non-place   ║    │
 │  ║     Reels — so a separate classifier step is unnecessary  ║    │
 │  ║     in v0 (kept as a plan for future verticals).          ║    │
 │  ╚══════════════════════════╤════════════════════════════════╝    │
 │                             ▼                                     │
 │  ╔═══════════════════════════════════════════════════════════╗    │
 │  ║  5. RESOLVER  (per place — deterministic, basic-only)     ║    │
 │  ║                                                           ║    │
 │  ║     Google Places Text Search                             ║    │
 │  ║       query = extracted_name + location_hints             ║    │
 │  ║       bias  = derived from Reel signals ONLY              ║    │
 │  ║                                                           ║    │
 │  ║     Outcome routing:                                      ║    │
 │  ║     • exactly 1 candidate → status=auto, DONE             ║    │
 │  ║     • 2+ candidates → status=needs_review, stash up to 5  ║    │
 │  ║     • 0 candidates → status=unresolved                    ║    │
 │  ║                                                           ║    │
 │  ║     No LLM in this step. ~20 lines of real code.          ║    │
 │  ║     v0.5+ upgrade: agentic loop w/ web_search tool for    ║    │
 │  ║     nickname-only / vibe-description / missing-name cases ║    │
 │  ╚══════════════════════════╤════════════════════════════════╝    │
 │                             ▼                                     │
 │  ╔═══════════════════════════════════════════════════════════╗    │
 │  ║  6. STORE  (SQLite — source of truth, Nate owns it)       ║    │
 │  ║                                                           ║    │
 │  ║     items(id, vertical, source_url NOT NULL,              ║    │
 │  ║           raw_payload_json, llm_output_json, created_at)  ║    │
 │  ║                                                           ║    │
 │  ║     places(id, item_id, ordinal, extracted_name,          ║    │
 │  ║            google_place_id, lat, lng,                     ║    │
 │  ║            formatted_address, google_maps_url,            ║    │
 │  ║            dishes_json, why_its_cool, tags_json,          ║    │
 │  ║            resolution_status,                             ║    │
 │  ║            resolution_candidates_json)                    ║    │
 │  ╚══════════════════════════╤════════════════════════════════╝    │
 │                             ▼                                     │
 │  ╔═══════════════════════════════════════════════════════════╗    │
 │  ║  7. CONFIRMATION REPLY (Telegram chat)                    ║    │
 │  ║     "Saved: Lucali (Carroll Gardens).                     ║    │
 │  ║      Dishes: margherita, calzone.                         ║    │
 │  ║      [view on map] [pick different] [edit]"               ║    │
 │  ║     — if any place is needs_review, show candidate list   ║    │
 │  ╚═══════════════════════════════════════════════════════════╝    │
 │                                                                   │
 └─────────────────────────────┬─────────────────────────────────────┘
                               ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │  VIEWER  (self-hosted web app)                                    │
 │    MapLibre GL JS + Protomaps vector tiles                        │
 │    Every place is a pin. Tap → drawer shows:                      │
 │      • embedded source Reel                                       │
 │      • dishes, why-it's-cool, tags                                │
 │      • "resolve" button if needs_review                           │
 │    Filters: tag, cuisine, city, resolution_status.                │
 └───────────────────────────────────────────────────────────────────┘
```

## Concrete walkthrough — saving a Lucali Reel

1. **Nate sees a Reel about Lucali.** Taps iOS share → Telegram → `@place_bot`.
2. **Bot replies instantly:** "🔎 Working on https://instagram.com/reel/ABC123/…" Webhook fires to backend.
3. **Fetcher** identifies Instagram, runs `yt-dlp https://instagram.com/reel/ABC123/` → 23MB mp4 + info.json (caption: *"You can't leave Brooklyn without hitting Lucali. Waited 2hrs, worth it. Cacio calzone + margherita."*; native location tag: none).
4. **Classifier** returns `{ vertical: "place" }` (trivial in v0).
5. **Extractor** uploads the mp4 to Gemini Files API, prompts for places extraction. Gemini returns:
   ```json
   [{
     "extracted_name": "Lucali",
     "location_hints": { "neighborhood": null, "city": "Brooklyn",
                         "on_screen_text": null, "visual_landmarks": null },
     "dishes": ["cacio calzone", "margherita"],
     "why_its_cool": "Hype is real, wait worth it",
     "tags": ["pizza", "italian", "brooklyn", "hype"],
     "extraction_confidence": "high"
   }]
   ```
6. **Resolver — fast path** queries Places Text Search with `"Lucali Brooklyn"`. Returns one strong match: `ChIJexample…`, 575 Henry St, Carroll Gardens. Auto-accept. Done in ~1s.
7. **Store** writes one `items` row (with source_url) and one `places` row (ordinal=0, resolved).
8. **Confirmation** reply: *"Saved: Lucali (Carroll Gardens, Brooklyn). Dishes: cacio calzone, margherita. [view on map]"*
9. **Viewer** now shows a new pin at 575 Henry St. Clicking it opens a drawer with the embedded Reel, the dishes, and the why.

Contrast with a hard case — a Reel that only says "this orange Thai spot on Rivington" with no name:

- Fast path returns zero / low-relevance results.
- Escalates to agentic loop.
- Agent runs `web_search("orange Thai restaurant Rivington Lower East Side")` → finds Eater piece identifying "Wayla" → runs `places_text_search("Wayla Rivington")` → confirms → done in ~12s.

## Data model (final for v0)

```sql
CREATE TABLE items (
  id                 INTEGER PRIMARY KEY,
  vertical           TEXT NOT NULL,      -- "place" in v0
  source_url         TEXT NOT NULL,      -- Reel / TikTok / YouTube URL — required
  raw_payload_json   TEXT,               -- what yt-dlp returned
  llm_output_json    TEXT,               -- Gemini's structured extraction
  created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE places (
  id                          INTEGER PRIMARY KEY,
  item_id                     INTEGER NOT NULL REFERENCES items(id),
  ordinal                     INTEGER NOT NULL,  -- 0,1,2… position within the item
  extracted_name              TEXT NOT NULL,     -- what Gemini saw
  google_place_id             TEXT,              -- null if unresolved
  lat                         REAL,
  lng                         REAL,
  formatted_address           TEXT,
  google_maps_url             TEXT,
  dishes_json                 TEXT,              -- JSON array of strings
  why_its_cool                TEXT,
  tags_json                   TEXT,              -- JSON array of strings
  resolution_status           TEXT NOT NULL,     -- auto | user_confirmed | needs_review | unresolved
  resolution_candidates_json  TEXT               -- populated only when needs_review
);

CREATE INDEX idx_places_item    ON places(item_id);
CREATE INDEX idx_places_lookup  ON places(google_place_id);
```

`source_url NOT NULL` is the schema-enforced version of the "never lose the source" rule from the competitor tryout. `items` 1-to-many `places` supports "Top 5 NYC ramen" → 5 pins sharing one Reel.

## Video understanding — the model choice

For non-YouTube sources (Instagram / TikTok / shared mp4s) we need an LLM that can look at a video file directly. As of April 2026 **Gemini is the only major model that handles video natively**:

- **Claude 4.7** — images only, no video at the API level.
- **GPT-5** — images only natively. A video pipeline on OpenAI means extracting frames + audio yourself, running Whisper + GPT-4o Vision separately, and stitching. Multi-step, loses Gemini's native "video as a whole" reasoning.
- **Gemini 2.5 / 3.1** — native video + audio in one API call.

**Pick: Gemini 2.5 Flash** as the default extractor, with Pro as a fallback for cases where Flash misses.

Tested on an Instagram food Reel (Bo-Ky, a Teochew-Vietnamese spot in NYC Chinatown) and Flash nailed it:
- Extracted the name, 4 specific dishes mentioned in the audio, visual landmarks including reading Chinese characters off the building sign, and a nuanced why-it's-cool.
- 17.6s end-to-end, 25.6K input tokens, ~$0.008 per Reel.

| Tier | Input / output per 1M tokens | Use |
|---|---|---|
| Gemini 2.5 Flash-Lite | $0.10 / $0.40 | Cheaper fallback if Flash is too expensive at higher volumes |
| **Gemini 2.5 Flash** | **~$0.30 / $2.50** | **Default — confirmed capable, on the free tier (no billing required for dev)** |
| Gemini 2.5 Pro | $1.25 / $10 | Upgrade target for cases where Flash fails. Requires billing enabled. |
| Gemini 3.1 Pro | $2 / ~$16 | Overkill for now |

**Free tier note:** Gemini 2.5 Pro has a `limit: 0` on the free tier — you can only use it with billing enabled. Flash and Flash-Lite are on the free tier with real quotas. This is a strong argument for Flash-default during dev; we can switch to Pro without code changes (via a `GEMINI_MODEL` env var) once the project has billing enabled.

**Mp4 upload path: Gemini Files API.** POST the mp4 to `files.generativelanguage.googleapis.com`, get back a file URI, reference it in the prompt. Up to 2GB; files kept for 48h (more than enough). Inline-base64 uploads work under ~20MB but die on longer videos — just use Files API from day one.

**Video tokenization (measured, not estimated):** an 18.9MB / ~40s Reel came back as 25,611 input tokens. Roughly 500–650 tokens/sec including audio — higher than the 258/sec "docs" number, presumably because audio is non-trivial here. At this rate, **~$0.008 per Reel on Flash**, **~$0.035 per Reel on Pro**.

## External dependencies and their role

| Dependency | Role | v0 cost at ~50 saves/mo |
|---|---|---|
| **yt-dlp** | Fetcher for Instagram / TikTok | Free |
| **Apify Instagram Reel Scraper** | Fetcher fallback when yt-dlp breaks | ~$0.04 |
| **Gemini 2.5 Flash (Files API + video)** | Extractor — mp4 → structured places array | ~$0.40 |
| **Google Places API (New)** | Resolver — Text Search lookup | $0 (well under Pro tier's 5,000 free calls/mo) |
| **MapLibre GL JS + Protomaps** | Viewer map | Free |
| **SQLite** | Store | Free |
| **Hosting (Fly.io / Cloud Run / VPS)** | Backend (dev: local Mac + `cloudflared`) | $0 dev / ~$5 prod |
| **Telegram Bot API** | Entry point | Free |

**Total monthly cost at ~50 saves/mo: ~$0.50 in dev (local, Flash-default), ~$5 in prod (Flash + small host). Basic-only Resolver (no agentic in v0) is the reason this dropped from earlier estimates.**

## Entry point decision: iOS Shortcut

Nate doesn't use Telegram, and requiring a third-party chat app install for his own personal tool is a bad tradeoff. **iOS Shortcut is the v0 entry point.**

The real constraint driving earlier Telegram thinking was agentic latency (10–15s for the ~20% of hard cases). That's solvable without a chat surface:

**Fast path (~80% of saves): synchronous.**
- Shortcut POSTs to backend → fast path returns in ~2s → Shortcut shows a result card: *"Saved: Lucali (Carroll Gardens). 2 dishes logged. [view]"*.
- Blocking for 2s is fine UX.

**Escalation (~20%): asynchronous.**
- Backend detects escalation needed → returns `"Working on it..."` in <1s → Shortcut closes.
- Agent runs in background → writes pin to SQLite when done.
- User sees it appear in the viewer next time they open it.
- Optional: local push via `ntfy.sh` (free, self-hostable) or Pushover ($5 one-time). Not v0-required.

**Disambiguation UI lives in the viewer, not inline.**
- `needs_review` items accumulate in a "To Review" queue in the viewer.
- Each shows candidate buttons — tap to resolve.
- Batch-processing backlog matches how people actually handle these, better than interrupting the share flow.

Why this beats the Telegram plan:
- No third-party app install required.
- Native iOS share-sheet integration — the exact flow Nate already has muscle memory for.
- Disambiguation-in-a-queue is better cognitive ergonomics than disambiguation-in-a-chat.
- Nothing depends on keeping a chat app open.

A Telegram bot *can* still exist later as a parallel entry point (desktop, Android, permanent chat log) — but it's not v0-load-bearing.

## V0 scope — what's in, what's out

**In:**
- Instagram Reels + TikTok + YouTube ingestion (all three hit the same pipeline).
- Place vertical extraction + resolution (multiple places per item).
- Two-tier resolver (fast path + agentic escalation).
- SQLite storage with source URL enforced.
- Telegram bot entry point + confirmation replies.
- Self-hosted map viewer with tag/city filters.

**Out (deferred to v0.5+):**
- **Agentic Resolver** (web_search + fetch_page tool loop for nickname-only / vibe-description / missing-name cases). Basic Places lookup handles the ~80% case; the other ~20% surface as `needs_review` / `unresolved` for manual disambiguation in the viewer.
- Other verticals (products, recipes, read-later, media recs) — spine is pluggable, but none implemented.
- Automated Reel ingestion (scan saved Reels periodically) — v0 is strictly user-initiated.
- Nearby / distance filters ("ramen near me").
- iOS Shortcut entry point.
- KML export to Google My Maps.
- Auto-learning from user corrections.
- Re-resolution over time (retry needs_review items as Google's data improves).
- Creator-history heuristics.
- Any social / sharing / multi-user features.

## How extension works (spine validity check)

Adding TikTok YouTube/Reel parity — **already in v0**; they flow through the same pipeline (platform-specific fetcher, identical downstream).

Adding a new **platform** (e.g., Twitter/X): write a new branch in the Fetcher, everything downstream is unchanged.

Adding a new **vertical** (e.g., products from shopping Reels):
1. Teach the Classifier to return `"product"` as a vertical.
2. Write an `extractor_product` that returns `{ product_name, price, retailer_url, why, source_url }`.
3. Add a `products` table with the vertical-specific columns.
4. Add a list view for products (not all verticals need a map).
5. No changes to Ingest, Fetcher, Store-spine, or existing place logic.

If that extension path holds up in practice, the spine is right.

## Deployment

### Dev phase — decided

Backend runs **locally on Nate's Mac**, exposed to Telegram via **Cloudflare Tunnel** (temporary URL mode):

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:3000
# → public https://<random>.trycloudflare.com URL points at the local backend
```

Trade-off: the URL rotates each session (fine for dev, you re-register the Telegram webhook when you start a new session). No Cloudflare account needed in this mode.

Why this is right for dev:
- Fastest possible iteration loop — edit in Cursor, no deploy cycle.
- DB, mp4 cache, logs all on local disk for easy inspection.
- Zero cost.
- Swapping to "production" later is trivial because the backend is just a container.

### Production phase — decision deferred, options noted

When the pipeline works and Nate wants it 24/7 independent of his laptop, pick from:

| Option | Model | Free tier at v0 scale | Notes |
|---|---|---|---|
| **Fly.io Machines** | Scale-to-zero containers with persistent volumes | 3 small VMs + 3GB volume free | Likely simplest. Sleeps when idle, wakes in ~500ms on webhook. SQLite lives on an attached volume. |
| **Google Cloud Run** | Scale-to-zero containers | 2M requests + 360K vCPU-sec/mo free | Also excellent. Needs Cloud Run v2 with volume mount, or swap SQLite for Cloud SQL (adds cost/complexity). |
| **Small always-on VPS** (Hetzner, DigitalOcean) | Traditional rented box | ~$4–5/mo | Boring, reliable. Good if scale-to-zero cold starts feel annoying. |
| **Railway / Render** | Managed platforms | Small free credit | Nicer DX than raw VPS, slightly more expensive at scale. |

Picking is a v0.5 decision — defer until after the pipeline works locally. Our code is just a container, so all these hosts are one `Dockerfile` away.

## What this doesn't answer yet

- **How does the confirmation UI handle multiple needs_review places in one Reel?** Probably: one Telegram message per unresolved place. Spec when building.
- **Rate-limit / error handling policies.** When yt-dlp gets rate-limited or Gemini errors, what does the bot say? Minimum viable: "try again later." Harden when it bites.
- **Authentication on the backend.** Solo tool, but the ingest webhook still needs a bearer token so a random Telegram user can't spam it. Trivial but don't skip.
