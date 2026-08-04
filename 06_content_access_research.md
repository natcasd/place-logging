# Content Access: Instagram & TikTok

*Captured 2026-04-17. Revised after digging into how shipped competitors actually work.*

## TL;DR (corrected)

There **are** working programmatic paths to Reel / TikTok / YouTube content, and the story is platform-specific:

- **YouTube → trivial.** Gemini has native YouTube URL ingestion. Pass the URL, get full audio + visual understanding. No scraping, sanctioned, lowest-friction path of any platform. This is a big deal.
- **Instagram:**
  1. Sanctioned — **Instagram Messaging API (webhook)**. How Hold My Pin actually works. Requires a Business account + Meta app review.
  2. Unsanctioned but widely used — **GraphQL scraping with reverse-engineered `doc_id`**. How most URL-based tools (ReelsToMap, indie projects) actually work. Breaks every 2–4 weeks, patched by maintained libraries.
  3. Paid abstractions over #2 — Apify, Scrapfly, Bright Data. ~$0.01–$0.05 per Reel; at personal scale ≈ $1–3/mo.
- **TikTok:** same three-bucket story as Instagram, but the scrape path is markedly more reliable (TikTok is less hostile to scrapers than Meta).

Previous version of this doc was too pessimistic. Across all three platforms there are robust paths — you pick your cost/reliability tradeoff.

## How Hold My Pin actually works (the sanctioned path)

Hold My Pin is a DM bot on Instagram (`@holdmypin`). The mechanism:

1. User DMs the bot a Reel from the share sheet.
2. Because `@holdmypin` is an **Instagram Business/Creator account with the Messaging API enabled**, Meta fires a webhook to Hold My Pin's backend. ([Meta docs](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api/))
3. The webhook payload contains an `attachments` array with a `"share"` type that includes `payload.url` — a URL to the shared Reel. Sometimes it's a direct permalink; sometimes it's an `asset_id` inside a `lookaside.fbsbx.com` URL that requires another API call to resolve.
4. The bot fetches caption + metadata from there, runs its AI, and replies in the chat.

This is a fully legitimate Meta API path. It requires:
- An Instagram Business/Creator account
- App Review approval from Meta (days to weeks)
- A public webhook endpoint
- Handling "unsupported media" cases (some Reels arrive as webhook events marked `UNSUPPORTED` — fallback required)

**Tradeoff:** cleanest legal path, heaviest setup.

## How ReelsToMap / most URL-based tools actually work (the GraphQL scrape path)

Public Instagram Reels expose their data through Instagram's internal GraphQL API. No login required.

The technique:
- Hit Instagram's GraphQL endpoint with the right `doc_id` (an opaque query hash Meta assigns) and the shortcode from the Reel URL.
- Response is JSON: caption, hashtags, video URL (time-limited, signed), username, view count, sometimes location.

Key fact from the research: **Instagram rotates `doc_id` values every 2–4 weeks**, which is the source of the "it keeps breaking" reputation. But there's an active open-source ecosystem (`instagram-media-scraper`, `instaloader`, `yt-dlp`, various Apify actors) that tracks the rotation and patches quickly. Using a maintained library means you inherit someone else's "kept current" work.

This is what most share-sheet apps (TokSpot, ReelMap) and URL-paste tools (ReelsToMap) do under the hood: take a URL, run it through a GraphQL scrape (or a paid service that does the same), get structured data back.

**Tradeoff:** gray ToS, periodically breaks, but robust when you use a maintained library + accept occasional updates.

## How the paid services (Apify, Scrapfly, Bright Data) work

They do the GraphQL scrape for you, behind a stable HTTPS API. Typical shape:

```
POST https://api.apify.com/v2/acts/apify~instagram-reel-scraper/runs
  { "urls": ["https://instagram.com/reel/ABC123/"] }
→ { caption, hashtags, videoUrl, owner, location, ... }
```

- Cost: roughly **$0.01–$0.05 per Reel** depending on the vendor and depth of extraction.
- At personal scale (~50 saves/month) → **$0.50–$2.50/month**.
- You get reliability without maintaining scrapers. They handle proxy rotation, `doc_id` updates, anti-bot defenses.

**Tradeoff:** money for reliability.

## YouTube (separate league — this one is clean)

Unlike Instagram and TikTok, YouTube has a **fully sanctioned, trivially easy path** to full video understanding: **Gemini's native YouTube URL input.**

How it works ([Gemini docs](https://ai.google.dev/gemini-api/docs/video-understanding)):

```python
# Pseudocode
genai.generate_content(
  model="gemini-2.x",
  contents=[
    {"file_data": {"file_uri": "https://www.youtube.com/watch?v=..."}},
    "Extract any places, dishes, and why they're recommended."
  ],
)
```

You pass the YouTube URL. Google handles everything — no download, no scraping, no Whisper, no frame extraction. Gemini has internal access to YouTube (same company) and processes audio + visual content natively. Lowest-latency path of any video-analysis workflow.

**Limits:**
- Video must be public or owned by the signed-in account.
- One YouTube URL per request.
- Up to **1 hour** for 1M-context models, **2 hours** for 2M-context.
- Cost is standard Gemini token pricing + a small video-tokens rate.

**What this means for the architecture:**

YouTube becomes an explicit first-class ingestion source, separate from the Instagram/TikTok path. When Nate watches a "top 10 NYC ramen" YouTube video and wants to log every spot, he shares the URL to our bot/shortcut, and the entire extraction pipeline for that item is literally:

```
youtube_url → Gemini(video_understanding) → structured_places[] → store
```

No scraping layer. No fetcher. Gemini is the fetcher + extractor in one call. This is **much** stronger than anything we can do for Instagram/TikTok, and it genuinely changes how Nate discovers places — "YouTube review videos" go from "tedious to log" to "one share → 8 pins appear."

The architecture's pluggable-fetcher design already supports this — we just register a YouTube fetcher that's an empty pass-through, since Gemini does all the work.

## TikTok

Same three-bucket picture as Instagram, with better vibes:

- **No sanctioned consumer-facing API** for arbitrary videos. The official TikTok API is for publishing, business analytics, or research access.
- **GraphQL/endpoint scraping works more reliably than Instagram.** `yt-dlp` supports TikTok well.
- **Paid services** (same vendors) cover it at similar per-video pricing.

TikTok is less aggressive about breaking scrapers than Instagram.

## Recommended concrete pipeline for Reels/TikToks

Instagram Reels is Nate's primary source (YouTube is secondary), so this pipeline is the load-bearing one.

### Key insight

Gemini's native-URL trick only works for YouTube (Google owns both). But Gemini's **Files API accepts uploaded mp4s up to 2GB**. So the Reel/TikTok path is just "download the mp4 locally, then upload it to Gemini for the same native video understanding we get with YouTube URLs."

### The fetcher choice

**Primary: `yt-dlp` self-hosted.**
- Open-source, actively maintained, supports both Instagram and TikTok (and hundreds of other sites).
- `yt-dlp 'https://instagram.com/reel/ABC'` → mp4 + metadata JSON (caption, hashtags, uploader).
- Works for public content without login. **Do not pass cookies** — that's the only thing that creates account-ban risk. Without cookies, there's nothing to ban.
- Reliability: TikTok solid; Instagram breaks every 2–6 weeks and gets fixed within days. At personal scale, the maintenance story is `pip install -U yt-dlp` when it breaks.
- Known real-world pain points (from Reddit `r/youtubedl` and [yt-dlp issues](https://github.com/yt-dlp/yt-dlp/issues/11151)): `"No csrf token"`, `"rate-limit reached or login required"`. These are the update-the-library symptoms.

**Fallback: Apify `instagram-reel-scraper` (or similar vendor).**
- Only invoked when yt-dlp fails on a specific Reel.
- Pay-per-call, ~$0.01–$0.05 per video; at personal-scale fallback use, tops out around $1/mo.
- **Important:** Apify's downloader is literally [powered by yt-dlp internally](https://apify.com/scrapepilot/instagram-reels-video-downloader----mp4-likes-captions). The value it adds over self-hosted is residential proxies, managed IP rotation, and maintenance ownership — not a different extraction engine. So the choice between self-hosted yt-dlp and Apify is really "do I want to pay for proxies and someone else's upkeep." At one-user scale, self-hosted is almost always fine.

**How confident am I that this is what competitor apps use?** Moderately. Hold My Pin / TokSpot / ReelsToMap don't publish their stack. But the signal is strong: yt-dlp dominates community discussions, the biggest paid scraping service uses it under the hood, and it's the only name that appears consistently across Reddit, Stack Overflow, and GitHub for this use case. The inference is "the industry convergence is on yt-dlp," not "X specific app uses yt-dlp." For a personal tool, the convergence is good enough to build on.

**Not worth for v0: Instagram Messaging API / DM bot.**
- Requires Business account + Meta App Review (days to weeks).
- Reel shares sometimes arrive as `UNSUPPORTED` webhook events, so you'd still need yt-dlp as a fallback. Revisit if this ever leaves personal scale.

### The full pipeline

```
1. Nate taps Share on a Reel → Shortcut / Telegram bot fires
2. Backend receives { url, caption, thumbnail } from the share payload
3. Fetcher: yt-dlp (Apify fallback) → mp4 + richer metadata
4. Upload mp4 to Gemini Files API
5. Gemini prompt: extract { place_name, neighborhood, dishes[], why_its_cool, tags[] }
6. Geocode place_name → { google_place_id, lat, lng, address } via Places API
7. Write to SQLite with source_url pinned
8. Reply in Shortcut/chat: "Saved: Lucali (Carroll Gardens). Dishes: margherita, calzone. Link: <map>"
```

The fetcher is pluggable, so the pipeline shape is identical regardless of source:

- **YouTube:** fetcher is a no-op — pass URL straight to Gemini.
- **Instagram / TikTok:** fetcher downloads mp4 via yt-dlp (→ Apify fallback), uploads to Gemini Files API.
- **Manually shared mp4 from camera roll:** fetcher is a no-op — file is already local.

Same extractor, same storage, same viewer. Platforms differ only in how the bytes arrive.

## What this means for the build

### Entry-point choice gets more interesting

I'd been framing this as "Shortcut vs. Telegram bot." With the corrected picture, there's a third option:

| Option | Entry UX | Data source | Setup cost |
|---|---|---|---|
| **iOS Shortcut** → backend | Tap Share → Shortcuts → your shortcut | Share payload (URL + caption); backend may also scrape | ~1 hour |
| **Telegram bot** | Tap Share → Telegram → your bot | Same as above, plus chat-as-log side-benefit | ~1 hour |
| **Instagram DM bot** (Hold My Pin style) | Tap Share → Instagram → DM `@your_bot` | Meta webhook with shared-media payload — cleanest sanctioned access | Days-to-weeks (Meta App Review + Business account) |

The Instagram DM bot is more work but is the only fully-legitimate path that gets Meta-provided media access. Shortcut and Telegram both rely on scraping (either self-hosted or paid) for anything beyond the share-sheet caption.

For a **personal tool** used by one person: Shortcut or Telegram + occasional GraphQL scrape (or a $2/mo Apify plan) is dramatically less work. Save the Instagram DM bot route for if this ever becomes a shared product.

### V0 scope stands, with a sharper story

- **V0:** Shortcut (or Telegram bot) → backend receives URL + caption from the share payload → backend runs a GraphQL scrape against the Reel URL (using a maintained library like `instaloader` or `yt-dlp`'s `--skip-download --write-info-json` mode) to pick up tagged location, hashtags, and full caption. LLM extracts place from the combined text. No mp4, no audio transcript yet.
- **V0.5:** if captions alone miss too often, add an optional audio/frame extraction path using either Apify (paid, ~$2/mo) or self-hosted `yt-dlp` + Whisper (free, occasional breakage). Swap in as a component without touching the spine.
- **V1:** re-evaluate the DM bot route only if the tool grows past a personal audience.

## Bottom line

Yes, programmatic access is real. The "honest" claim is:

- The **Instagram Messaging API** is the only fully-sanctioned path, and requires a Business-account + Meta-approved app.
- The **GraphQL scrape** is the path most shipped apps actually use. Periodically breaks, but a maintained open-source library absorbs that pain.
- **Paid scrapers** are a drop-in upgrade that costs pocket change at personal scale.

For v0, none of this matters yet — the share-sheet caption is enough to ship useful v0. But when we eventually want dishes-from-audio, the path is: Apify or `yt-dlp` + Whisper, not "we need to build something Meta has never allowed."
