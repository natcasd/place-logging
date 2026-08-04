# Place Logging — Core Concept

*Captured 2026-04-17*

## The problem

Nate keeps a running mental (and Google My Maps) list of restaurants / things to do, sourced mostly from **YouTube, Instagram Reels, and friends**. Today the workflow is:

1. See a cool spot in a Reel.
2. Manually add a pin in Google My Maps.
3. Try to remember why it was cool, which dish was hyped, who recommended it.

This is tedious enough that most discoveries never get logged, and the ones that do are stripped of context (you end up with a pin that just says "Lucali" with no memory of *why*).

Moving to New York makes this acute — there's too much to keep in your head, and the "I want dinner somewhere cool near me *right now*" query should be a lookup, not a memory test.

## The vision

One unified, personal map across **every city Nate cares about** (not one map per city), where:

- **Adding a place is near-frictionless.** Primary entry point: share an Instagram Reel → something classifies the place, geocodes it, and drops it on the map. Secondary entry points: YouTube links, manual text, maybe a browser extension later.
- **Every pin carries its discovery context as metadata.** Source (which Reel / who posted it), why it's interesting ("best cacio e pepe in Brooklyn according to @foodguy"), any specific dishes or things to order pulled out of the video, tags (neighborhood, cuisine, price, vibe, indoor/outdoor, etc.).
- **The original media is linked back.** If a Reel showed three dishes by name, those dishes are logged as "order-this" notes against the pin, with a link back to the video so you can rewatch at the table.
- **Retrieval is the real payoff.** "I'm in the East Village and want ramen" → filtered pins with their context. "What did that guy say was the best slice in Bed-Stuy?" → pin + original Reel.

## Why now

The wedge is that **LLMs + vision models have just gotten good enough** to:

- Watch/transcribe a Reel and extract: place name, neighborhood/city, dishes mentioned, the "why."
- Disambiguate "that Thai spot in the LES" into an actual Google Place ID.
- Do this cheaply enough that individuals can run it at the rate they scroll Instagram.

The inspiration was a tool that turns a cooking Reel into a written recipe — same mechanic, applied to places instead of recipes.

## Core user flow (v0)

1. Nate is scrolling Reels, sees a NYC pizza spot.
2. Hits iOS share sheet → picks "Place Logging."
3. Behind the scenes: the Reel is pulled, transcribed, parsed for place + dishes + why-it's-cool.
4. Nate gets a confirmation card: "Add *Lucali* (Carroll Gardens) — notes: *'hand-stretched, wait is worth it'*, dishes: *margherita, calzone*. Tags: pizza, brooklyn." He taps confirm (or edits).
5. Pin lands on the unified map.

## Non-goals (at least at first)

- Social / sharing features. This is a **personal** tool — the Beli / Mapstr angle of "publish your list" is explicitly out of scope for v0.
- Reviews, ratings. The value is context + retrieval, not a rating system.
- Booking / reservations.
- Multi-city *separation*. It's one map with filters, not many maps.

## Open questions

- iOS share-sheet target: custom app? Shortcut? Telegram-style bot? (Each has different friction / build cost.)
- How reliably can the Reel itself give us the place name? Worst case the caption has it; better case the audio/OCR has it; sometimes neither and we need the user to type.
- Storage: own backend vs. just writing to a Google My Map via API vs. something like Mapstr's data model.
- Android / web parity — probably not v0.
