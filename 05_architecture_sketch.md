# Architecture Sketch

*Captured 2026-04-17. Concrete enough to build from, loose enough to still move.*

## Decision forced by reality: Google My Maps cannot be the source of truth

There is **no public API** for Google My Maps or Google Maps saved lists — reading or writing. This has been an open feature request [since 2017](https://issuetracker.google.com/issues/35820262) with no movement. The only readback path is Google Takeout (a one-shot export as KML). Google's paid *Maps Platform* APIs (Places, Geocoding, Maps JavaScript) are separate — they query Google's global place database, not your personal map.

**Consequence:** we own the data. A real local/hosted store is the source of truth; Google Maps is used only for the place-lookup step during ingest and (optionally) as a rendering backend in the viewer. My Maps becomes a write-only KML export if Nate ever wants a secondary view there.

This is the *right* decision for the "personal triage system" framing anyway — we need the data to be portable and extensible across verticals, which Google My Maps couldn't have supported regardless.

## Spine

```
  [ one ingest endpoint ]
            │
            ▼
  ┌────────────────────┐
  │  Fetcher           │   pulls the raw content (Reel metadata, mp4, caption)
  └─────────┬──────────┘
            ▼
  ┌────────────────────┐
  │  Classifier (LLM)  │   → { vertical: "place" | "product" | "recipe" | "read-later" | ... }
  └─────────┬──────────┘
            ▼
  ┌────────────────────┐
  │  Extractor (LLM)   │   vertical-specific; returns structured records (array — one video can yield many places)
  │   - place: name, neighborhood_hint, city_hint, dishes, why, tags
  │   - (others: placeholder interfaces, unimplemented in v0)
  └─────────┬──────────┘
            ▼
  ┌────────────────────┐
  │  Resolver          │   (place vertical) hybrid two-tier:
  │                    │     fast path — Places Text Search w/ Reel-derived bias
  │                    │     escalation — agentic loop (web_search + places + fetch_page)
  │                    │   outputs: canonical place_id / lat / lng / address, or needs_review with candidates — see doc 07
  └─────────┬──────────┘
            ▼
  ┌────────────────────┐
  │  Store (SQLite)    │   source of truth; items 1:N places; every item has NOT-NULL source_url
  └─────────┬──────────┘
            ▼
  ┌────────────────────┐
  │  Viewer (web)      │   MapLibre GL JS + Protomaps (see doc 05 below); list views for non-place verticals later
  └────────────────────┘
```

Why this shape:

- **Single ingest endpoint** decouples UX from logic. The share-sheet, a Shortcut, a bot, a web form — all post the same JSON shape to the same URL. Changing the entry point later doesn't touch the pipeline.
- **Classifier and extractor are separate LLM calls.** Could be merged into one prompt, but separating them means a new vertical = write a new extractor, not re-architect a mega-prompt. Cost is one extra API call per save; at personal scale that's nothing.
- **Source URL is a required column on every record.** Enforced at the schema level — records without a source can't be inserted. This is the wedge from the competitor tryout made durable.
- **SQLite on disk (or Postgres if hosted)** over anything cloud-proprietary. Trivial backup, trivial export, Nate owns the bytes.

## v0 scope (ruthless)

The only thing we ship in v0 is "Reel → place pin with source preserved and rich context." Everything else in the diagram is built as an interface but left unimplemented.

Concretely:

- **Entry point (pick one):**
  - **iOS Shortcut → HTTPS POST** to the backend. Recommended — builds in an hour, no App Store, works from the iOS share sheet.
  - **Telegram bot** Nate DMs from his phone. Also builds in an hour, arguably lower friction if he already uses Telegram.
  - *Not* a native iOS share extension — that requires shipping an app, which is way too much build for v0.
  - *Not* a DM-my-Instagram-bot approach — Meta will break it.
- **Fetcher (v0 = layer 1 only):** when the Shortcut / bot fires from the iOS share sheet, Instagram already hands us the **URL, caption text, and a thumbnail**. That's the input. No scraping, no Instagram API, nothing to break. Captions alone usually contain the place name and often the "why it's cool," which is enough for a useful v0.
  - *Layer 2 (public-page scrape for tagged location, hashtags, etc.)* — moderately fragile, add later if needed.
  - *Layer 3 (audio transcript + frame OCR from the video itself)* — brittle, usually requires a paid scraping service. This is where dishes-from-audio lives. **Treat as v0.2+ upgrade, not a v0 requirement.** Competitor apps do it, but often via paid services and they break periodically; not the layer to bet v0 on.
- **Classifier:** hardcoded to return `"place"` in v0. The pluggable step exists; it's just trivial.
- **Extractor (place):** LLM call returning `{ place_name, neighborhood, city, dishes_to_order[], why_its_cool, tags[], source_url }`. Then a deterministic geocode step against Google Places API to resolve `place_name + city` → `{ google_place_id, lat, lng, address }`.
- **Store:** SQLite. Tables: `items(id, vertical, source_url, raw_payload_json, created_at)` + `places(item_id, place_name, google_place_id, lat, lng, address, dishes, why, tags_json)`. Relational split leaves room for other verticals to add their own tables.
- **Viewer:** tiny self-hosted web page. Map UI shows a pin for every place; clicking a pin opens a drawer with the extracted context (dishes, why-it's-cool, tags) and the embedded Reel. Filters by tag / city in v0.1.
  - **Rendering library (recommended): MapLibre GL JS + Protomaps tiles.** MapLibre is the open-source fork of the last pre-closed-source Mapbox GL release — same API, same look, fully free, no vendor lock-in. Protomaps is a self-hostable single-file tile format (`.pmtiles`, MIT licensed) with good-looking styles. Together they give vector tiles, smooth zoom, modern aesthetic, no billing account, and data-ownership ethos intact.
  - **Fallback: Google Maps JavaScript API.** If MapLibre/Protomaps plumbing is annoying, Google Maps is fine at personal scale — the $200/mo free credit easily covers one user — and you're already using Google Places API for geocoding, so there's ecosystem cohesion. The cost is aesthetic (raster-ish look) and modest vendor lock-in.
  - **Not Leaflet for this project.** Fine library, but its raster default looks dated next to vector alternatives, and MapLibre is barely more complex to set up.
  - **Not Mapbox GL JS.** No technical reason to pay when MapLibre is a literal fork.

## What's deliberately not in v0

- Any vertical besides places.
- Any social / sharing features.
- A native mobile app.
- Reservations / booking / reviews.
- Automated Reel ingestion ("scan my saved Reels every night"). v0 is strictly one-Reel-at-a-time, user-initiated.
- Nearby / distance filters. `"ramen near me"` is a v0.1 feature — easy once data is there, don't let it block the first ship.

## Open questions to resolve before writing code

1. ~~**Is Instagram Reel fetching reliable enough to bet on?**~~ Answered: depends which layer. V0 uses only the caption + URL delivered by the iOS share sheet, which is 100% reliable (Instagram is handing it to us, we're not scraping). Audio/frame extraction is fragile and deferred to v0.2+.
2. **Shortcut vs. Telegram bot for the entry point?** Two separate hour-long prototypes would settle it — pick whichever feels less annoying on the 10th save.
3. **Where does this run?** Fly.io / Railway / a tiny VPS / just local with Tailscale + a Shortcut? Personal-scale, so "cheapest and simplest" wins; revisit when it works.
4. ~~**Rendering backend for the viewer?**~~ Answered above — **MapLibre GL JS + Protomaps** is the default, Google Maps JavaScript API is the fallback if MapLibre plumbing annoys.

## Extension story (proof the spine holds)

When Nate later wants "save a product Reel" to work:

1. Add a case to the classifier prompt so it can return `"product"`.
2. Write `extractor/product.py` returning `{ product_name, price, retailer_url, why_i_want_it, source_url }`.
3. Add a `products` table.
4. Add a new view to the viewer (or a tab).

Zero changes to ingest, fetcher, classifier infrastructure, or storage spine. That's the test of whether the architecture is right.
