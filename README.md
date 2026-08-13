# place_logging — ideation log

Ideation workspace for a personal place-logging app idea. Docs are numbered in rough reading order.

- [00_concept.md](00_concept.md) — the core idea and v0 user flow
- [01_competitive_landscape.md](01_competitive_landscape.md) — what already exists in this space
- [02_decision_next_steps.md](02_decision_next_steps.md) — honest read + recommended next step (try the competitors before building)
- [03_competitor_tryout.md](03_competitor_tryout.md) — live notes from trying each competitor on real Reels
- [04_reframe.md](04_reframe.md) — scope shift from "place logger" to general personal-triage system (places are just the first vertical)
- [05_architecture_sketch.md](05_architecture_sketch.md) — ingest → classify → extract → store → view spine, with a ruthlessly scoped v0
- [06_content_access_research.md](06_content_access_research.md) — what's actually available for fetching Reels/TikToks (no sanctioned API; paths grounded)
- [07_place_resolution.md](07_place_resolution.md) — the extracted-name → physical-restaurant step (Google Places Text Search, locationBias, multi-place-per-Reel schema)
- [**08_full_architecture.md**](08_full_architecture.md) — **current consolidated architecture.** Read this first for the complete v0 picture.
- [09_generalized_ingest.md](09_generalized_ingest.md) — generalized ingest (tweets, articles, pure text, user_prompt augmentation); notes what stays behind an expand-later seam in v0

## Ingest API boundaries

- `POST /api/v1/ingests` is the canonical authenticated JSON API. It accepts
  `source_url` as a normal string.
- `POST /api/v1/shortcut/ingests` is an Apple Shortcuts transport adapter. It
  accepts `source_url_base64`, decodes it, and immediately delegates to the
  canonical ingest flow. This isolates Shortcuts-specific content/provenance
  behavior from the core API and processing service.
