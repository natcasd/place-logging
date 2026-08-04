# Competitor Tryout

*Started 2026-04-17. Real Reels sent to each tool, notes on what came back.*

## Hold My Pin

**Flow tested:** DM a Reel/post to `@holdmypin` on Instagram.

**What it returned:** A Google Maps place URL (e.g. a link straight to the Stanford Dish Hiking Trail page in Google Maps). That's it — a pointer, not a pin on any map.

**What's missing vs. what we want:**
- No pin is actually added anywhere. Tapping the link opens Google Maps to the place; you'd still have to hit "Save" and pick a list to persist it. That's barely better than copy-pasting the name into a search bar.
- No accessible web dashboard / personal map within Hold My Pin itself — i.e. no "all the places you've sent me" view was discoverable.
- No dish-level extraction, no "why it's cool" notes preserved, no link back to the source Reel.

**Verdict:** Not useful as-is. Functionally a fancy geocoder that returns a Google Maps URL. Even setting aside the thin output, the model is unappealing because **anything it stored would be siloed to their platform** — Nate wants data he owns and can take with him, not a vendor-locked list.

**Implication for building:** Reinforces the wedge. Place-name-extraction-as-a-service is table stakes; the actual product problem (persistent, rich, portable personal map with dish-level context) is untouched here.

---

## TokSpot

**Flow tested:** iOS share-sheet from Instagram → TokSpot extracts place.

**What's missing:** Locked to restaurants/food-adjacent places. No way to bend it toward hikes, shops, exhibits, or any other kind of "place I saw in a Reel" — let alone non-place things.

**Verdict:** Rejected. Too narrow a vertical; not extensible.

---

## ReelMap by Craig Saves

**Flow tested:** Same share-sheet pattern.

**Verdict:** Rejected. Similar problem — opinionated narrow app, not a flexible system. This is the moment the framing shifts (see `04_reframe.md`).

---

## Google Maps native (screenshot → Gemini → list)

**Flow tested:** Screenshot a Reel → Google Maps auto-scans the screenshot (via the Screenshots list in the You tab) → extracts the place → lets you add it to a list.

**What works:** Place recognition itself is fine. Built-in, free, writes to lists Nate already uses.

**What's missing:**
- **No link back to the original Instagram Reel.** This is the dealbreaker. A pin without its source is just a name on a map — it doesn't capture *why* the place piqued interest, and a week later when deciding whether to actually go, there's no way to rewatch the context (the reviewer's take, dishes called out, vibe of the spot).
- By extension: no captured transcript / quoted "why it's cool" text, no extracted dish list from the video.

**Verdict:** Not enough. Good as a fallback "just get the place name" shortcut but fails the core value prop — discovery context is what makes these saves useful later.

**Implication for building:** This confirms the wedge. **Source-link preservation + extracted context is the real product.** Place extraction alone is commoditized (Google, Hold My Pin, etc. all do it). The differentiated thing is that when you look at a pin six months later, you can see the Reel that got you excited about the place, rewatch it, and remember what to order.

---

## On The Grid — Explore Socially

*Discovered 2026-04-18, after the v0 scaffold was already working end-to-end.*

**Flow:** Import/paste links from Instagram, TikTok, Google Maps, or Beli → save into custom lists (cafés, bars, restaurants, museums, gyms, trips, etc.) → app surfaces other users who saved the same places to connect with.

**Positioning:** Explicitly *social*. Store listing's headline is *"Find people through places"* — the whole point is matching with other users over shared venues. DMs, shared lists with connections, 18+ age gate.

**What's missing vs. what we want:**
- **App Store listing doesn't mention dish-level / "why it's cool" extraction.** It takes a link and identifies the place — probably uses IG oembed + Places API under the hood, same as the commoditized tier of the Reel → pin apps we rejected.
- **Social-first is a non-goal for us.** Nate explicitly rejected social / sharing features in doc 00 and revised-concept in doc 04. On The Grid's entire value prop is the opposite.
- **Data is vendor-siloed.** Same data-ownership issue we flagged for Hold My Pin et al.

**Verdict:** Not a real threat to the thesis. Overlaps on the shallow "paste link → pin on map → categorize" layer (commoditized), diverges on everything that actually makes place_logging distinctive (rich context, personal, owned data, no social). Worth keeping on the radar as the space evolves — if they ship dish-level extraction + a no-social personal mode, that's the moment to reconsider. Until then, positioning is unchanged.

**UX note (2026-04-18):** Their flow is manual copy-paste, not iOS share-sheet. App Store description only says "import or paste links." Apps that had share-sheet integration would lead with it — meaningful signal they don't. That's a real friction gap against our Telegram-share-sheet flow: their users do "open IG → copy link → switch apps → paste" (≥5 actions); ours do "Share → Telegram → bot" (2 actions, no context switch). For a tool used dozens of times a month, that friction delta is larger than the extraction-depth delta.

---

## Running takeaways

- **Data portability / "I want my own thing"** is a hard requirement. Any tool that locks places inside a proprietary app ecosystem is a non-starter. Anything we build (or adopt) needs to write through to something Nate controls — Google My Maps via API, a self-hosted store, or an export-first format.
- **Source-link preservation is the wedge.** Both tools tested so far fail this: Hold My Pin returns a Google Maps URL with no link back to the Reel; Google Maps native recognizes the place but drops the source entirely. The product insight is that a pin is only useful later if you can answer *"why did I save this?"* — which means the original Reel URL has to be first-class metadata on the pin, alongside any transcript / caption / dish list extracted from it. None of the existing players treat the source as durable.
