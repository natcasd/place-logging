# Competitive Landscape

*Captured 2026-04-17. This is the uncomfortable part: the exact flow Nate described (share a Reel → AI extracts place → drops on map) has multiple shipped apps already. We need to either find a real wedge or happily pick an existing tool.*

## Direct competitors (Reel → AI → pin)

| Product | Flow | Notes |
|---|---|---|
| **[Hold My Pin](https://holdmypin.com)** | Share a Reel/post to `@holdmypin` on Instagram (DM a bot). AI reads caption, geotags, and visuals. Free. | This is the closest to Nate's envisioned flow. The Instagram-DM-bot pattern is *lower friction* than a share-sheet target because it doesn't require a native app install. |
| **[ReelsToMap](https://reelstomap.com/)** | Paste Reel URL into a web app, get Google Maps links for every place shown. | Web-first, not a share-sheet. Weaker for "in the middle of scrolling Reels." |
| **[TokSpot](https://mwm.ai/apps/tokspot/6756735375)** | Share a video to the app via iOS share sheet → AI extracts location, address, details. Covers TikTok + Instagram. | Exactly the iOS share-sheet flow Nate described. |
| **[ReelMap by Craig Saves](https://apps.apple.com/us/app/reelmap-by-craig-saves/id6745059478)** | One-tap share from Instagram to app. Smart place detection from saved reels. | Same flow, different team. |
| **[Rezz](https://apps.apple.com/us/app/rezz-map-save-restaurants/id6720711761)** | Map & save restaurants, share with friends. Free tier limited to 20 saves. | Social-lean, lower AI emphasis. |
| **Reelary** (xicu.net) | Similar concept — indie project. | Signals that this is a common "I'll build it myself" idea. |

## Adjacent / personal-pin maps (no Reel AI)

- **[Mapstr](https://en.mapstr.com)** — personal place-saving with tags, one unified world map. Free tier ~300 pins, then paid. Doesn't ingest Reels.
- **[Pin Drop](https://www.pindrop.it)** — positioned as the "no pin limit" Mapstr alternative; imports from Mapstr.
- **Google My Maps** — what Nate uses today. Manual, tedious.
- **Google Maps Saved Lists** — built-in, syncs across devices, shareable.
- **Apple Maps Guides** — built-in alternative, similar model.

## Platform moves to watch

- **Google Maps (native) now has AI screenshot → map pin via Gemini.** ([coverage](https://matadornetwork.com/read/google-maps-screenshot-map/)) Screenshot a Reel / TikTok, Google Maps recognizes the place, offers to add it to a list. This is the *most dangerous competitor* because it's free, built-in, and doesn't require any extra app. Nate should try this today before building anything — it may already solve 70% of the problem.
- **Instagram Map** ([TechCrunch](https://techcrunch.com/2025/08/08/how-to-use-instagram-map-and-protect-your-privacy/)) — Meta's own Map feature exists but is about location-sharing with friends, not a personal bookmark system. Not a direct competitor, but signals Meta is thinking about maps + Reels.

## Tangentially related (ranking/social for food, not pinning)

- **Beli** — restaurant ranking + social graph. Different shape of problem (rank spots you've *been* to, social recs) rather than "log places you found in a Reel."
- **[Savor](https://www.savortheapp.com)** — food journaling.
- **Thatch** — travel itinerary publishing, more creator-oriented.

## Read of the space

1. **"Reel → map pin" is the obvious AI-era app idea, and ~5 teams shipped it in the last 12–18 months.** This isn't a green field.
2. **Google Maps' screenshot-to-pin feature is the existential threat to all of them.** If Gemini-in-Maps keeps improving, standalone apps get squeezed. Nate should try it first.
3. **None of the competitors obviously nail the "rich metadata per pin" angle** (dishes to order extracted from the video, quoted why-it's-cool, source link back). They mostly stop at "here's the place." That's the *potential* wedge — but it needs to be verified by actually trying them, not assumed.
4. **The personal-only, no-social stance is probably underserved.** Most of these apps bolt on sharing / social as the monetization story. A tool that stays private and just gets out of your way is a real positioning.
