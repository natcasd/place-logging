# Reframe: from "place logger" to "personal triage system"

*Captured 2026-04-17, after trying Hold My Pin, Google Maps native, TokSpot, and ReelMap.*

## What changed in Nate's head

Every competitor tested is a **single-vertical app**: Reel → restaurant pin, and nothing else. After bouncing off four of them, the real preference is clearer:

> *"I want the ability to log anything from Instagram or whatever and have it triaged appropriately for me."*

The restaurants-from-Reels flow is still the first concrete use case — the thing Nate will actually exercise daily when he moves to NYC — but it's **one lane of a broader system**, not the whole product.

## The bigger vision

A single "universal save" endpoint (share sheet, DM bot, whatever — the entry point is flexible) that takes in *anything* Nate found interesting online and routes it to the right place:

- **A Reel about a restaurant** → extract place + dishes + source → pin on personal map with Reel linked back.
- **A Reel / TikTok showing a hike** → same pipeline, but it lands in a "Places > Outdoors" bucket, not restaurants.
- **A product someone recommended** → routes to a "want to buy" list with price / link / source preserved.
- **A cooking Reel** → recipe extraction → routes to a recipe store.
- **An article / thread worth reading later** → read-later bucket.
- **A movie / book / show recommendation** → watchlist / reading list.

The common spine across all of these:
1. **One ingest endpoint.** Nate doesn't pick the destination; he just sends stuff.
2. **AI triage.** Classifies what kind of thing it is (place, product, recipe, article, media rec, etc.).
3. **Vertical extractors.** Each type has its own enrichment (geocode + dishes for places, price + product page for products, ingredients + steps for recipes, etc.).
4. **Source preservation as a hard rule.** Every saved item keeps the original URL / Reel / screenshot. No exceptions.
5. **Portable storage Nate owns.** Not siloed in a vendor app — writes through to systems he controls.
6. **Retrieval is the payoff.** The value compounds because everything Nate has ever found interesting is searchable/filterable in one place.

## Why this is different from existing tools

- **Readwise Reader / Pocket / Instapaper** — solve read-later well, but that's one vertical.
- **Raindrop.io / Pinboard / browser bookmarks** — flat bookmarks, no extraction or routing.
- **Notion Web Clipper** — general but dumb; clips a page, doesn't triage or enrich.
- **Hold My Pin / TokSpot / ReelMap** — single-vertical place extractors.

The novel combination here is **universal ingest + AI triage + per-vertical enrichment + user-owned storage**. None of the above does all four.

## Honest scope warning

This is a bigger project than "Reel → map pin." The risk is scope creep → never ships. Mitigation:

- **Build the spine for one vertical first (places from Reels), but design it so adding a new vertical = writing a new extractor + a new storage target.** Don't actually implement other verticals in v0.
- The v0 user-facing surface can still be "it logs restaurants from Reels." The internals are the triage system; the first triage rule just happens to be "this is a restaurant." Second and third verticals cost days, not months, once the spine is right.

## Revised v0 proposal

1. **Entry point:** iOS share sheet (or a tiny iOS Shortcut that calls a backend). One share target, not many.
2. **Backend:** receives a Reel URL, fetches its transcript/caption, runs triage + extraction with an LLM, returns a structured record.
3. **Classifier:** starts dumb — always classifies as "place." But structured as a pluggable step so "product / recipe / article" can be added later without touching the ingest or storage layers.
4. **Place extractor:** place name + geocode + dishes + "why it's cool" snippet + original Reel URL. Writes to a Google My Map (or a local SQLite + a Leaflet viewer he owns).
5. **Retrieval UI:** start as a map view of pins, where tapping a pin shows the source Reel and extracted context. Worth less than the ingest side at first — filters can come later.

## What to do next

- Rename / re-scope the project folder? `place_logging` is now a misleading name. Options: `triage` / `personal_ingest` / `save_everything`. Not urgent but worth flagging.
- Write `05_architecture_sketch.md` with the pluggable-triage spine (ingest → classifier → extractor → store) before writing code.
- Decide on the single entry-point surface for v0 (share sheet vs. Shortcut vs. DM bot) — each has real build-cost differences.
