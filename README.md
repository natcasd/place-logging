# place_logging — ideation log

Ideation workspace for a personal place-logging app idea. The numbered documents
are an archival design record and intentionally preserve superseded alternatives.
For supported behavior, use `v0/README.md` and `ios/README.md`.

The working product lives in two directories: `v0/` contains the Fly-hosted
API and processing pipeline, while `ios/` contains the native SwiftUI app and
share extension.

- [00_concept.md](00_concept.md) — the core idea and v0 user flow
- [01_competitive_landscape.md](01_competitive_landscape.md) — what already exists in this space
- [02_decision_next_steps.md](02_decision_next_steps.md) — honest read + recommended next step (try the competitors before building)
- [03_competitor_tryout.md](03_competitor_tryout.md) — live notes from trying each competitor on real Reels
- [04_reframe.md](04_reframe.md) — scope shift from "place logger" to general personal-triage system (places are just the first vertical)
- [05_architecture_sketch.md](05_architecture_sketch.md) — ingest → classify → extract → store → view spine, with a ruthlessly scoped v0
- [06_content_access_research.md](06_content_access_research.md) — what's actually available for fetching Reels/TikToks (no sanctioned API; paths grounded)
- [07_place_resolution.md](07_place_resolution.md) — the extracted-name → physical-restaurant step (Google Places Text Search, locationBias, multi-place-per-Reel schema)
- [08_full_architecture.md](08_full_architecture.md) — historical April 2026 architecture exploration
- [09_generalized_ingest.md](09_generalized_ingest.md) — historical generalized-ingest exploration

## Ingest API boundaries

- `POST /api/v1/ingests` is the canonical authenticated JSON API. It accepts
  `source_url` as a normal string.
- `POST /api/v1/shortcut/ingests` is an Apple Shortcuts transport adapter. It
  accepts `source_url_base64`, decodes it, and immediately delegates to the
  canonical ingest flow. This isolates Shortcuts-specific content/provenance
  behavior from the core API and processing service.
- `POST /api/v1/shortcut/diagnostics` is an authenticated debugging endpoint.
  It logs a bounded, redacted description of exactly what Shortcuts sent and
  never processes or persists a place.
- `GET /api/v1/places` is the authenticated, read-only list API used by the
  native iPhone app. It returns saved places newest-first.
