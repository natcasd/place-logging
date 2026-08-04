# Generalized Ingest — Beyond Reels

*Captured 2026-04-17. Noting for the record; not implementing in the first-pass v0 but the ingest layer should leave room.*

## The broader use case

The tool eventually needs to accept:

- **Reels / TikToks / YouTube** — the v0 case.
- **Tweets / X posts** — often just text + one image, sometimes with an ambiguous reference to a place.
- **Articles / blog posts** — Eater, Infatuation, someone's Substack.
- **Pure text** — "that new izakaya Bobby Flay tweeted about, East Village" typed straight into Telegram, no URL.
- **User-augmented input** — the user's own note accompanying any of the above: *"find this bar in NYC"*, *"skip the hype, just log Lucali."*

A single ingest shape covers all of it:

```
{
  "source_url":  "<optional URL>",
  "user_prompt": "<optional user text>",
  "source_text": "<optional literal text if no URL>"
}
```

At least one field must be present. In Telegram this is trivial — the incoming message has `.text`, and we just extract URLs from it via regex: URL(s) → `source_url`, everything else → `user_prompt`.

## Fetcher as a platform router

The existing Fetcher gets more branches:

```
instagram.com / tiktok.com     → yt-dlp mp4 → Gemini(video + metadata + user_prompt)
youtube.com                    → Gemini native URL + user_prompt
twitter.com / x.com            → scrape tweet (text + images) → Gemini(text + images + user_prompt)
generic article URL            → trafilatura/readability → Gemini(clean text + user_prompt)
no URL, only user_prompt       → Gemini(user_prompt as standalone description)
```

Output shape is constant: `{ places: [...] }`. Downstream (Resolver → Store → Viewer) doesn't care which platform fed it.

## User prompt as highest-authority signal

Extractor prompt gets a new directive:

> "If `user_prompt` is provided, treat it as the highest-authority signal — it may clarify, correct, or override what you'd otherwise infer from the source content."

Concretely: a tweet says "best martini, unmarked door, jazz playing" with no name; user_prompt says "find this bar." Extractor emits `{ extracted_name: null, low_confidence }` → Resolver escalates to agentic.

## Why agentic resolution is load-bearing here (not just a nice-to-have)

The generalized ingest creates more cases where Places API alone can't answer:

- Tweet describes a place by vibe, not name → Places can't search "by vibe."
- User supplies nickname ("the orange Thai spot") → Places doesn't know nicknames.
- Ambiguous name + no location → Places returns noise without context.

In each, the agentic loop's `web_search` + `places_text_search` + `places_details` tool use is the only way through. The agent uses Places as *a* tool, not *the* tool. This is the real justification for building agentic resolution from day one.

## V0 scope — still tight

- First ship: Reels + TikToks + YouTube, as already designed in doc 08.
- The ingest layer's **payload shape** should accept the generalized fields (`source_url`, `user_prompt`, `source_text`) from day one, even if only the video branches are implemented — leaving the tweet / article / pure-text branches as ~2-day follow-ups instead of architecture changes.
- Agentic resolver is built in v0 (not deferred) because it's the only way the generalized cases ever work, and the architecture only holds up if it's there from the start.

## What this changes about doc 08

Minimal:
- Ingest box: add `user_prompt` and `source_text` to the payload shape, mark as "present but only video branches wired for v0."
- Extractor prompt: add the "user_prompt is highest-authority" directive.
- Fetcher box: explicitly show the unimplemented branches so the extension story is visible.

Nothing in the Resolver, Store, or Viewer changes.
