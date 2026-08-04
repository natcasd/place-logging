# Decision & Next Steps

*Captured 2026-04-17.*

## The honest read

Your girlfriend's concern was right to raise, and the space is busier than you probably realized:

- **Hold My Pin** does the Instagram-DM-bot version of your flow.
- **TokSpot** and **ReelMap** do the iOS-share-sheet version.
- **ReelsToMap** does the paste-URL version.
- **Google Maps itself** can now turn Reel/TikTok screenshots into pins via Gemini.

Building "Reel → pin on map" as the whole product is almost certainly redundant.

## But there's a plausible wedge

The space has converged on *place extraction* ("what restaurant is this?") and largely stopped there. What you actually described goes further:

- **Per-dish extraction from the video** — "the three things the reviewer said to order."
- **Quoted "why it's cool"** pulled from caption / transcript, preserved against the pin.
- **Source Reel permanently linked**, rewatchable at the table.
- **Cross-city unified map with rich filters** (cuisine, vibe, price, who recommended it, neighborhood).

This is the "recipe from cooking reel" analogue applied to places — *contextual memory*, not just pin drops. None of the competitors obviously ship this well, but that's an assumption to verify.

## Recommended next step before writing any code

A tight, 1–2 hour evaluation session:

1. **Try Google Maps' screenshot-to-pin** with 3–5 real Reels you'd want to save. How good is it? Does it remember anything beyond the place name? This is the "do nothing" baseline.
2. **Install Hold My Pin, TokSpot, ReelMap.** Send each the same 3–5 Reels. Score them on:
   - Did it get the right place?
   - Did it extract dishes / what to order?
   - Did it preserve the original video / context?
   - Can you filter later ("ramen in East Village")?
3. **Write a short verdict** in `03_competitor_tryout.md`: which (if any) already does enough, and what specifically is missing.

Possible outcomes after that:
- **One of them is great → use it, don't build.** The winning move.
- **They all stop at place-name extraction → real wedge exists** around rich contextual metadata + retrieval. Build a thin layer that does the Reel-share ingestion *plus* the things they skip. Could even be a Shortcut + backend + Google My Maps write-through before becoming a full app.
- **Google Maps native is already 80% good enough → use it + a lightweight Shortcut** to enrich pins with notes / dish lists from a Reel. Smaller project, still useful.

## Risks of building anyway

- Instagram doesn't love third-party Reel scraping. Multiple competitors have presumably already hit the "Meta changes the API / ToS" wall. Ask: what happens to your product on the day Instagram makes Reel URLs harder to fetch?
- LLM costs per Reel add up if you're scrolling a lot. Economics only work if it's personal-scale or subscription-gated.
- "One unified map across cities" sounds great but is mostly a UX job — the underlying data is already one list.

## What to NOT do next

- Don't start on architecture / stack decisions. The tryout above will materially change what we're building, or whether we're building.
- Don't build a full iOS app as v0. If the tryout says "build," the minimum viable shape is probably: iOS Shortcut or share-sheet → small backend → writes to a Google My Map (or equivalent). Ship that in a weekend, see if you actually use it, then decide on a real app.
