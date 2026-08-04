# Place Resolution: Extracted Name → Physical Restaurant

*Captured 2026-04-17. The step between "the LLM said 'Lucali in Brooklyn'" and "here's a pin at 575 Henry St with a Google place_id."*

## The problem

Gemini's output for a food Reel looks something like:

```json
{
  "places": [
    { "name": "Lucali", "neighborhood_hint": "Carroll Gardens", "city_hint": "Brooklyn" },
    { "name": "Di Fara Pizza", "city_hint": "Brooklyn" }
  ]
}
```

That's not yet a pin — it's a string. We need to turn each of those into:

```json
{
  "google_place_id": "ChIJ...",
  "lat": 40.683,
  "lng": -73.997,
  "formatted_address": "575 Henry St, Brooklyn, NY 11231",
  "google_maps_url": "https://maps.google.com/?cid=..."
}
```

And we have to handle three real complications:

1. **Multiple places per Reel.** "Top 5 NYC ramen" videos have five. Pipeline must return an array, not a single place.
2. **Ambiguous names.** "Joe's Pizza" has many NYC locations and hundreds nationwide. Need context to disambiguate.
3. **Missing or vague location hints.** Captions often just say "this slice is crazy" with no city name. We need fallbacks.

## Approach: Google Places API — Text Search

The right endpoint is **[Places API (New) — Text Search](https://developers.google.com/maps/documentation/places/web-service/text-search)**: `POST https://places.googleapis.com/v1/places:searchText`.

```jsonc
// Request
{
  "textQuery": "Lucali Carroll Gardens Brooklyn",
  "locationBias": {
    "rectangle": {  // Nate's "active cities" bounding box
      "low":  { "latitude": 40.50, "longitude": -74.25 },
      "high": { "latitude": 40.92, "longitude": -73.70 }
    }
  },
  "maxResultCount": 5
}

// Response includes:
// places[].id                -- "ChIJ..."
// places[].displayName.text  -- "Lucali"
// places[].formattedAddress
// places[].location          -- { lat, lng }
// places[].types             -- ["restaurant", "pizza_restaurant", ...]
// places[].googleMapsUri
```

Why Google specifically (vs. Mapbox, Foursquare, OSM/Nominatim):

- **Best restaurant coverage in the US** — Google's POI data is denser than any alternative because of Google Maps' user-review feedback loop. For trendy/new spots especially, competitors trail by months.
- **`place_id` is universally stable** — once resolved, this is our canonical key. Survives renames, moves, category changes.
- **Cost at personal scale is effectively zero** — Google restructured Maps Platform pricing in March 2025 (the old $200/mo universal credit is gone, replaced by per-SKU free quotas). Text Search with Basic fields sits in the Pro tier: **5,000 free calls/month**, ~$17/1000 after. Personal-scale usage (~200 calls/mo) is well under the free cap.
- **Ecosystem cohesion** — the same `place_id` lets us deep-link into Google Maps, pull photos/hours later via Place Details, or export to Google My Maps if we want.

Fallback consideration: if you ever ditch Google's ecosystem, Mapbox Geocoding is the cleanest swap-in and MapLibre already integrates with Mapbox data.

## The Resolver step in the pipeline

```
... → Gemini extractor → Resolver → SQLite store → Viewer
                            │
                            │  FAST PATH
                            ├──► Places Text Search (Reel-derived bias)
                            │       │
                            │       ├── high-confidence match → done
                            │       └── ambiguous / zero / low-conf → ↓
                            │
                            │  ESCALATION
                            └──► Agentic loop (web_search + places + fetch_page)
                                    │
                                    ├── agent resolves → done
                                    └── agent gives up → store candidates, flag needs_review
```

The Resolver is **two-tier**: a cheap deterministic fast path, and an agentic fallback for the hard cases.

### Tier 1: fast path

For each extracted place:

1. Build a query from `name + neighborhood_hint + city_hint` (drop nulls).
2. Call Places Text Search with `locationBias` derived purely from Reel signals (see next section).
3. Score:
   - One result, or top result's relevance far above #2 → **auto-accept** → done.
   - Multiple similar-confidence candidates → **escalate to agent**.
   - Zero results → **escalate to agent**.
   - Gemini flagged the extraction as low-confidence (e.g., nickname, partial name) → **escalate to agent** regardless of Places result.

~80% of food Reels with clear identifying info terminate here. Cheap, fast, done.

### Tier 2: agentic loop

For escalated cases, spin up an agent (one per extracted place, run in parallel) with this tool surface:

| Tool | Purpose |
|---|---|
| `places_text_search(query, bias?)` | Primary geo candidates |
| `places_details(place_id)` | Verify a specific candidate — does it have the dish/feature the Reel cited? |
| `web_search(query)` | Eater / Infatuation / Reddit / TimeOut snippets for disambiguation. Tavily or Brave Search free tiers handle this. |
| `fetch_page(url)` | Open a specific review when a snippet isn't enough. |

The loop runs until the agent has high confidence or hits a turn limit (e.g., 8). It outputs:

- **Resolved:** `{ place_id, confidence: high, reasoning: "..." }` — store it.
- **Uncertain:** `{ candidates: [...], reasoning: "..." }` — store with `resolution_status = needs_review`, surface to user.

#### Example loop

```
Input: { extracted_name: "Lucali", dishes: ["calzone", "hand-stretched margherita"] }

Turn 1: places_text_search("Lucali Brooklyn pizza")
     →  [Lucali (Carroll Gardens), Lucali's Pizza (Queens), Lucali Westchester]

Turn 2: agent reasons: "Reel cited calzone + hand-stretched. Which one is that famous for?"
         web_search("Lucali Brooklyn calzone famous")
     →  Eater snippet: "Lucali on Henry St is legendary for calzones."

Turn 3: agent decides: candidate 1 matches. Return { place_id, confidence: high }.
```

Multiple places per Reel run their agents **in parallel** — a "Top 5 ramen" video takes one agent's runtime, not five.

### Why the hybrid

Pure single-shot is too weak for nicknames, pop-ups, and moved places. Pure agentic is slow + expensive for the 80% of Reels where the answer is obvious.

Cost/latency at personal scale:
- **Fast path:** ~$0.001/place, ~1s.
- **Agentic path (when triggered):** ~$0.10–$0.30/place, ~10–15s.
- **Monthly average** at ~50 saves × 2 places with 20% agent escalation: ≈ $3/mo marginal agent cost.

Shortcut UX note: the agentic path is too slow for an inline "tap Share → result" Shortcut. Use the **Telegram bot pattern** instead — user shares, bot replies "working on it," and fires the result back in chat when done. Share-and-forget.

## Location signals — driven entirely by the Reel

**Design principle: all identifying information comes from the Reel itself.** No pre-configured "active cities" list, no user-maintained location preferences, no guessing based on which cities Nate usually saves from. If a Reel doesn't give us enough to place it, we surface that rather than guess.

Why: pre-configured preferences smuggle external context into the resolution step. A Reel from Nate's Tokyo trip shouldn't resolve to a NYC place just because he mostly saves NYC food. A Reel from an LA creator shouldn't be treated as NYC just because NYC is his default. Pre-config also rots — the list becomes stale whenever he moves or travels.

Signal sources the Resolver uses, ranked by strength (strongest first):

1. **Instagram's native location tag.** yt-dlp surfaces this as a `location` field in the metadata JSON when the creator tags a venue or place. This is a near-perfect signal — the creator literally told Instagram where this was — and should short-circuit most of the resolution step when present.
2. **On-screen text extracted by Gemini.** Food Reels often burn the restaurant name and city/neighborhood into the video as overlays ("Joe's Pizza, NYC"). Vision models read this reliably.
3. **Audio transcript mentions.** "Here in the East Village at..." — Gemini's video understanding picks this up as part of the same call.
4. **Caption + hashtags.** Often the weakest but still useful. `#nyceats` or "best slice in Brooklyn" gives a city-level hint.
5. **Visual context.** Landmarks in the background (Brooklyn Bridge, Eiffel Tower), street signs, subway station signs. Gemini can infer city-level location from these when nothing else is explicit.

The Resolver combines whichever of these are present into a single `textQuery + locationBias` call against Places API. If nothing pins the city down, **we do a globally-unbiased search and surface the top candidates to the user** rather than guessing — same mechanism as any other ambiguous resolution.

One deliberate non-signal: **creator history.** Knowing that `@nyc_eats` usually posts NYC food is tempting but violates the principle — the content of *this* Reel should drive the answer. Leave that as a possible v1+ enhancement if Reel-only signal turns out to miss too often.

## Schema implications (updates to the sketch in doc 05)

v0 schema needed a small change to support multiple places per Reel:

```sql
items (
  id INTEGER PRIMARY KEY,
  vertical TEXT NOT NULL,        -- "place" for v0
  source_url TEXT NOT NULL,      -- original Reel URL; enforced NOT NULL
  raw_payload_json TEXT,          -- what yt-dlp returned
  llm_output_json TEXT,           -- Gemini's structured extraction
  created_at TIMESTAMP
)

places (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  ordinal INTEGER NOT NULL,                 -- 0, 1, 2... position within the item
  extracted_name TEXT,                      -- what Gemini said
  google_place_id TEXT,                     -- null if unresolved
  lat REAL, lng REAL,
  formatted_address TEXT,
  google_maps_url TEXT,
  dishes_json TEXT,                         -- array of strings
  why_its_cool TEXT,
  tags_json TEXT,                           -- array of strings
  resolution_status TEXT NOT NULL,          -- "auto" | "user_confirmed" | "needs_review" | "unresolved"
  resolution_candidates_json TEXT           -- only populated for needs_review
)
```

Two key points:

- **`places` has a 1-to-many relation with `items`** so "Top 5 ramen" becomes five pins with one shared source Reel.
- **`resolution_status` on the record makes "partial wins" first-class** — even an unresolved place is stored (so it's not lost), but it's visually distinct in the viewer until the user disambiguates it.

## Multi-place extraction prompt for Gemini

Small detail but load-bearing — the Gemini prompt has to explicitly ask for an array, not a single place:

> "Extract every restaurant, bar, café, or other physical place discussed in this video. For each one, return: name, neighborhood_hint (if mentioned or visually obvious), city_hint (if mentioned or visually obvious), dishes (array of specific dishes mentioned), why_its_cool (one-sentence summary of what the creator said about it), tags (cuisine, price_hint, vibe, etc.). If no place is mentioned, return an empty array."

Empty-array-allowed is important so non-place Reels (the bike reel from the test) cleanly fail-closed.

## What we're NOT building for v0

- Auto-learning from user corrections (if Nate keeps picking candidate #2 for a certain uploader, remember it). Nice but premature — and also crosses the "only use signal from the Reel" line, so treat as v1+ at earliest.
- Re-resolution over time (if a resolution was marked needs_review and Google's data improves later, re-run). Batch job for v1.
- Creator-history heuristics. Deliberately out-of-bounds per the Reel-only design principle above.

## Cost check at personal scale

Rough math for 50 saves/month, each averaging 2 places, ~20% needing agentic escalation:
- **Gemini video understanding** (~1 min video): ~$0.05 × 50 = $2.50/mo
- **Places API Text Search (fast path)**: ~100 calls/mo — under the Pro tier's 5,000/mo free quota → $0
- **Agentic escalation** (~20 of 100 places × ~$0.20): ~$4/mo
- **Web search API** (Tavily/Brave, free tier at this volume): $0
- **yt-dlp**: free
- **Apify fallback** (~2 Reels/mo): ~$0.04
- **Hosting** (Fly.io small instance or similar): ~$5/mo

**Total: ~$12/mo all-in** for a personal-scale setup with the agentic resolver. That's still the number to beat any "just use the existing $4.99/mo app" framing, and it buys disambiguation quality none of those apps offer.
